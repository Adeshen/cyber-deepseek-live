"""Official BreezeBlue hosted TTS, returning native 24 kHz mono s16le PCM.

Only https://api.breeze.blue/v1 is accepted: a configurable endpoint must never
redirect the server-side key to another host. Keys are read from key_file for
each request, never stored in presets, responses, or a persistent client.

Voice directories contain their own marker, ``breezeblue.json``::

    {"label": "Breeze02", "voice_id": "voc_example",
     "params": {"model_id": "breeze-tts-2", "language_code": "zh",
                "instructions": "温柔地说", "voice_settings": {"speed": 1.0}}}

The inherited async entry point serializes requests and drains already-sent
requests on cancellation. No request is retried, including 429 and timeouts.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from pathlib import Path
import re
from typing import AsyncIterator
from urllib.parse import quote, quote_plus

import httpx

from .breeze import BreezeTTS

OFFICIAL_BASE = "https://api.breeze.blue/v1"
_PARAM_NAMES = {"model_id", "language_code", "instructions", "voice_settings"}
_SETTING_RANGES = {"guidance_scale": (1.0, 10.0), "speed": (0.5, 2.0), "volume": (0.01, 2.0)}
_STREAM_QUEUE_SIZE = 8
_STREAM_PREFIX_BYTES = 16
log = logging.getLogger(__name__)


class BreezeBlueTTSError(RuntimeError):
    def __init__(self, code: str, detail: str, status_code: int | None = None):
        self.code = code
        self.detail = detail
        self.status_code = status_code
        status = f" (HTTP {status_code})" if status_code is not None else ""
        super().__init__(f"BreezeBlue {code}{status}: {detail}")


def _voice_id(preset: dict) -> str:
    value = preset.get("voice_id")
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", value):
        raise BreezeBlueTTSError("preset", "音色缺少有效的官方 voice_id")
    return value


def _params(preset: dict) -> dict:
    raw = preset.get("params", {})
    if raw is None:
        raw = {}
    if not isinstance(raw, dict) or set(raw) - _PARAM_NAMES:
        raise BreezeBlueTTSError("preset", "音色 params 只允许官方语音参数")
    params = {"model_id": "breeze-tts-2", "language_code": "zh"}
    params.update({key: value for key, value in raw.items() if value is not None})
    model = params["model_id"]
    if not isinstance(model, str) or not 1 <= len(model) <= 120:
        raise BreezeBlueTTSError("preset", "model_id 必须是有效模型名称")
    language = params["language_code"]
    if not isinstance(language, str) or not re.fullmatch(r"[A-Za-z]{2}", language):
        raise BreezeBlueTTSError("preset", "language_code 必须是两位语言代码，例如 zh")
    if "instructions" in params and not isinstance(params["instructions"], str):
        raise BreezeBlueTTSError("preset", "instructions 必须是文字")
    if "voice_settings" in params:
        settings = params["voice_settings"]
        if not isinstance(settings, dict) or set(settings) - (_SETTING_RANGES.keys() | {"use_speaker_boost"}):
            raise BreezeBlueTTSError("preset", "voice_settings 包含不支持的参数")
        settings = {key: value for key, value in settings.items() if value is not None}
        for name, (minimum, maximum) in _SETTING_RANGES.items():
            if name in settings:
                value = settings[name]
                if isinstance(value, bool) or not isinstance(value, (float, int)) \
                        or not math.isfinite(value) or not minimum <= value <= maximum:
                    raise BreezeBlueTTSError("preset", f"voice_settings.{name} 应在 {minimum}–{maximum} 之间")
        if "use_speaker_boost" in settings and not isinstance(settings["use_speaker_boost"], bool):
            raise BreezeBlueTTSError("preset", "use_speaker_boost 必须是布尔值")
        params["voice_settings"] = settings
    return params


def scan_voices(voices_dir: str) -> dict[str, dict]:
    """Load saved official voices only, ignoring local Breeze/OmniVoice markers."""
    base = Path(voices_dir)
    if not base.is_dir():
        return {}
    out = {}
    for entry in sorted(base.iterdir()):
        marker = entry / "breezeblue.json"
        if not entry.is_dir() or not marker.is_file():
            continue
        try:
            raw = json.loads(marker.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("marker 必须是 JSON 对象")
            # Credentials must never become preset fields or appear in /voices.
            if set(raw) - {"label", "voice_id", "params"}:
                raise ValueError("marker 只允许 label、voice_id 和 params")
            preset = {"name": entry.name, "label": str(raw.get("label") or entry.name),
                      "kind": "saved", "voice_id": _voice_id(raw), "provider": "breezeblue",
                      "params": _params(raw)}
            out[entry.name] = preset
        except (OSError, ValueError, TypeError, BreezeBlueTTSError):
            # Do not log raw marker values: malformed files may contain secrets.
            log.warning("忽略无效的 BreezeBlue 音色预设")
    return out


def _redact(text: str, key: str) -> str:
    for form in {key, quote(key, safe=""), quote_plus(key, safe="")}:
        if form:
            text = text.replace(form, "[REDACTED]")
    return text


def _detail(response: httpx.Response, key: str) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            payload = payload.get("detail", payload.get("error", payload.get("message", payload)))
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    except (ValueError, UnicodeDecodeError):
        text = response.text
    # Redact before truncation, including keys repeated across long error bodies.
    return " ".join(_redact(str(text), key).split())[:500] or "接口未返回错误说明"


def _looks_like_container_or_text(data: bytes) -> bool:
    if data.startswith((b"RIFF", b"RF64", b"fLaC", b"OggS", b"ID3")):
        return True
    stripped = data.lstrip()
    if stripped.lower().startswith((b"<!doctype", b"<html", b"<head", b"<body", b"<?xml")):
        return True
    if stripped.startswith((b"{", b"[")):
        try:
            json.loads(stripped)
            return True
        except (ValueError, UnicodeDecodeError):
            pass
    return False


def _invalid_stream_prefix(data: bytes) -> bool:
    """Inspect a small prefix before any purported PCM reaches playback.

    A JSON error can span network chunks, so a complete JSON parse alone is
    insufficient here. Printable JSON-looking prefixes are rejected as well.
    Binary PCM that merely contains a brace is not classified as JSON.
    """
    if _looks_like_container_or_text(data):
        return True
    stripped = data.lstrip()
    if stripped.startswith((b"{", b"[")):
        try:
            preview = stripped.decode("utf-8")
        except UnicodeDecodeError:
            return False
        return all(char.isprintable() or char.isspace() for char in preview)
    return False


class BreezeBlueTTS(BreezeTTS):
    def __init__(self, base: str, key_file: str, timeout_sec: float = 120):
        super().__init__(base, timeout_sec)
        self.key_file = str(key_file or "")

    @property
    def cfg_base(self) -> str:
        return self._cfg_base

    @cfg_base.setter
    def cfg_base(self, value: str) -> None:
        if str(value or "").strip().rstrip("/") != OFFICIAL_BASE:
            raise BreezeBlueTTSError("config", "官方语音接口地址必须为 https://api.breeze.blue/v1")
        self._cfg_base = OFFICIAL_BASE
        self._active_base = ""

    def _read_key(self) -> str:
        try:
            if not self.key_file:
                raise ValueError("missing key file")
            with Path(self.key_file).open("r", encoding="utf-8") as stream:
                key = stream.read(8193).strip()
            if not key or len(key) > 8192 or not key.isascii() or any(c.isspace() for c in key):
                raise ValueError("invalid key file")
        except (OSError, UnicodeError, ValueError):
            raise BreezeBlueTTSError("config", "无法读取有效的 BreezeBlue Key 文件，请检查服务端配置") from None
        return key

    async def stream_pcm(self, text: str, preset: dict) -> AsyncIterator[bytes]:
        """Yield native 24 kHz mono s16le chunks before synthesis completes.

        Callers must close the iterator when leaving early (``aclosing`` is
        useful). Cancellation stops delivery immediately, but the producer
        drains its already-sent HTTP request before releasing the shared TTS
        lock. Closing a socket alone does not promise cancellation upstream.
        """
        if not text.strip():
            return
        async with self._loop_lock:
            queue: asyncio.Queue[bytes | BreezeBlueTTSError | None] = asyncio.Queue(
                maxsize=_STREAM_QUEUE_SIZE)
            aborted = asyncio.Event()
            producer = asyncio.create_task(self._stream_producer(text, preset, queue, aborted))
            try:
                while True:
                    item = await queue.get()
                    if item is None:
                        break
                    if isinstance(item, BreezeBlueTTSError):
                        raise item from None
                    yield item
            finally:
                # A producer blocked on a full queue must become a discard-only
                # reader. Never cancel it before the HTTP response reaches EOF.
                aborted.set()
                while not queue.empty():
                    queue.get_nowait()
                cancelled_while_draining = False
                while not producer.done():
                    try:
                        await asyncio.shield(producer)
                    except asyncio.CancelledError:
                        cancelled_while_draining = True
                    except Exception:
                        break
                if producer.done() and not producer.cancelled():
                    producer.exception()
                if cancelled_while_draining:
                    raise asyncio.CancelledError

    @staticmethod
    async def _stream_publish(queue: asyncio.Queue, aborted: asyncio.Event,
                              item: bytes | BreezeBlueTTSError | None) -> None:
        if aborted.is_set():
            return
        put = asyncio.create_task(queue.put(item))
        stop = asyncio.create_task(aborted.wait())
        try:
            await asyncio.wait((put, stop), return_when=asyncio.FIRST_COMPLETED)
        finally:
            # This also releases a queue.put parked behind a slow consumer when
            # that consumer closes. All helper tasks are joined, never orphaned.
            for task in (put, stop):
                if not task.done():
                    task.cancel()
            await asyncio.gather(put, stop, return_exceptions=True)

    async def _stream_producer(self, text: str, preset: dict, queue: asyncio.Queue,
                               aborted: asyncio.Event) -> None:
        key = ""
        try:
            base = self.cfg_base
            voice_id = _voice_id(preset)
            params = _params(preset)
            params["text"] = text
            try:
                timeout = float(self.timeout_sec)
                if not math.isfinite(timeout) or timeout <= 0:
                    raise ValueError("invalid timeout")
            except (TypeError, ValueError):
                raise BreezeBlueTTSError("config", "语音请求超时设置必须为正数") from None
            key = self._read_key()
            async with httpx.AsyncClient(timeout=timeout, trust_env=False,
                                         follow_redirects=False) as client:
                async with client.stream(
                    "POST", base + "/text-to-speech/" + quote(voice_id, safe="") + "/stream",
                    params={"output_format": "pcm"},
                    headers={"xi-api-key": key, "Accept": "audio/pcm, application/octet-stream"},
                    json=params,
                ) as response:
                    if 300 <= response.status_code < 400:
                        raise BreezeBlueTTSError("redirect", "官方接口返回重定向，已拒绝转发认证信息",
                                                 response.status_code)
                    if response.status_code != 200:
                        await response.aread()
                        code = "auth" if response.status_code in {401, 403} else \
                            "rate_limit" if response.status_code == 429 else "http"
                        raise BreezeBlueTTSError(code, _detail(response, key), response.status_code)
                    mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if mime not in {"audio/pcm", "application/octet-stream"}:
                        await response.aread()
                        detail = _detail(response, key) if "json" in mime else "接口未返回原始 PCM 音频"
                        raise BreezeBlueTTSError("response", detail, response.status_code)
                    pending = bytearray()
                    checked_prefix = False
                    format_error = False
                    received = 0
                    async for chunk in response.aiter_bytes():
                        received += len(chunk)
                        if aborted.is_set() or format_error:
                            # Inference may still be active: consume without
                            # accumulating audio or publishing into a full queue.
                            pending.clear()
                            continue
                        pending.extend(chunk)
                        if not checked_prefix:
                            if len(pending) < _STREAM_PREFIX_BYTES:
                                continue
                            # A whitespace-only prefix cannot identify PCM or
                            # text yet. Bound this probe rather than buffering
                            # an arbitrary textual error response in memory.
                            if not pending.lstrip() and len(pending) < 512:
                                continue
                            if not pending.lstrip() or _invalid_stream_prefix(bytes(pending)):
                                format_error = True
                                pending.clear()
                                continue
                            checked_prefix = True
                        size = len(pending) // 2 * 2
                        if size:
                            piece = bytes(pending[:size])
                            del pending[:size]
                            await self._stream_publish(queue, aborted, piece)
                    if not aborted.is_set():
                        if format_error:
                            raise BreezeBlueTTSError("audio", "接口返回了其他格式，无法作为原始 PCM 播放", 200)
                        if not received or len(pending) % 2:
                            raise BreezeBlueTTSError("audio", "接口返回的原始 PCM 为空或采样不完整", 200)
                        if not checked_prefix and _invalid_stream_prefix(bytes(pending)):
                            raise BreezeBlueTTSError("audio", "接口返回了其他格式，无法作为原始 PCM 播放", 200)
                        if pending:
                            await self._stream_publish(queue, aborted, bytes(pending))
                        self._active_base = base if self.cfg_base == base else ""
        except httpx.TimeoutException:
            await self._stream_publish(queue, aborted, BreezeBlueTTSError(
                "timeout", "语音合成超时；未自动重试，以免重复计费"))
        except httpx.RequestError:
            await self._stream_publish(queue, aborted, BreezeBlueTTSError(
                "connection", "无法连接官方语音接口；请检查网络"))
        except BreezeBlueTTSError as exc:
            await self._stream_publish(queue, aborted, exc)
        except Exception:
            # Never echo an unexpected transport/iterator exception: it can
            # include authenticated request details, even in its repr or chain.
            await self._stream_publish(queue, aborted, BreezeBlueTTSError(
                "response", "读取官方语音流失败"))
        finally:
            await self._stream_publish(queue, aborted, None)

    async def _synth_pcm(self, text: str, preset: dict) -> bytes:
        base = self.cfg_base
        voice_id = _voice_id(preset)
        params = _params(preset)
        params["text"] = text
        try:
            timeout = float(self.timeout_sec)
            if not math.isfinite(timeout) or timeout <= 0:
                raise ValueError("invalid timeout")
        except (ValueError, TypeError):
            raise BreezeBlueTTSError("config", "语音请求超时设置必须为正数") from None
        key = self._read_key()
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False) as client:
                response = await client.post(
                    base + "/text-to-speech/" + quote(voice_id, safe=""),
                    params={"output_format": "pcm_24000", "delivery": "sync"},
                    headers={"xi-api-key": key, "Accept": "audio/pcm, application/octet-stream"},
                    json=params,
                )
        except httpx.TimeoutException:
            raise BreezeBlueTTSError("timeout", "语音合成超时；未自动重试，以免重复计费") from None
        except httpx.RequestError:
            # Do not chain upstream exceptions, which may echo request headers.
            raise BreezeBlueTTSError("connection", "无法连接官方语音接口；请检查网络") from None
        if 300 <= response.status_code < 400:
            raise BreezeBlueTTSError("redirect", "官方接口返回重定向，已拒绝转发认证信息", response.status_code)
        if response.status_code != 200:
            code = "auth" if response.status_code in {401, 403} else \
                "rate_limit" if response.status_code == 429 else "http"
            raise BreezeBlueTTSError(code, _detail(response, key), response.status_code)
        mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        content = response.content
        if mime not in {"audio/pcm", "application/octet-stream"}:
            detail = _detail(response, key) if "json" in mime else "接口未返回原始 PCM 音频"
            raise BreezeBlueTTSError("response", detail, response.status_code)
        if not content or len(content) % 2 or _looks_like_container_or_text(content):
            raise BreezeBlueTTSError("audio", "接口返回的原始 PCM 为空、不完整或包含其他格式", response.status_code)
        self._active_base = base if self.cfg_base == base else ""
        return content
