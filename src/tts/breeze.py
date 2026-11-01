"""Breeze-TTS-2 HTTP gateway adapter and explicitly marked voice presets.

Each voice directory contains ``breeze.json`` (never OmniVoice's design.json)::

    {"label": "Breeze02", "kind": "clone", "ref_audio": "ref_audio.wav",
     "ref_text": "The exact transcript of the reference audio.",
     "params": {"language": "Chinese", "seed": 202, "speed": 1.0}}

For voice design, use ``kind: design`` and ``params.instruction``. Clone audio
is uploaded as bytes to /api/tts/clone/upload; local paths never cross the wire.
Requests are serialized. Cancelling a caller drains its already-sent request
before releasing the lock, since disconnecting does not stop server inference.
There are deliberately no synthesis retries.
"""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
from pathlib import Path
import subprocess
from typing import Any

import httpx

from . import audio

log = logging.getLogger(__name__)
AUDIO_EXTS = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac"}
_COMMON_PARAMS = {
    "language", "model", "seed", "guidance_scale", "temperature",
    "depth_temperature", "top_k", "top_p", "max_tokens", "do_split",
    "text_chunk_size", "cross_fade_ms", "speed", "instruction",
}
_CLONE_PARAMS = _COMMON_PARAMS | {"instruct", "cfg", "cfg_scale"}


class BreezeTTSError(RuntimeError):
    """A user-facing failure with a stable code for callers that need it."""

    def __init__(self, code: str, detail: str, status_code: int | None = None):
        self.code = code
        self.detail = detail
        self.status_code = status_code
        status = f" (HTTP {status_code})" if status_code is not None else ""
        super().__init__(f"Breeze {code}{status}: {detail}")


def _validated_params(preset: dict) -> dict[str, Any]:
    kind = preset.get("kind")
    if kind not in {"clone", "design"}:
        raise BreezeTTSError("preset", "音色 kind 必须是 clone 或 design")
    raw = preset.get("params") or {}
    if not isinstance(raw, dict):
        raise BreezeTTSError("preset", "音色 params 必须是 JSON 对象")
    allowed = _CLONE_PARAMS if kind == "clone" else _COMMON_PARAMS
    unknown = set(raw) - allowed
    if unknown:
        raise BreezeTTSError("preset", "不支持的音色参数: " + ", ".join(sorted(unknown)))
    # text and reference_text come from this call/preset, never stale params.
    params = {"language": "Chinese", "model": "breeze-tts-2"}
    params.update({key: value for key, value in raw.items() if value is not None})
    if any(not isinstance(value, (str, int, float, bool)) for value in params.values()):
        raise BreezeTTSError("preset", "音色参数必须是字符串、数字或布尔值")
    if kind == "design" and not str(params.get("instruction") or "").strip():
        raise BreezeTTSError("preset", "设计音色缺少 params.instruction")
    return params


def scan_voices(voices_dir: str) -> dict[str, dict]:
    """Load only subdirectories marked by breeze.json; invalid presets are skipped."""
    base = Path(voices_dir)
    if not base.is_dir():
        return {}
    out: dict[str, dict] = {}
    for entry in sorted(base.iterdir()):
        marker = entry / "breeze.json"
        if not entry.is_dir() or not marker.is_file():
            continue
        try:
            raw = json.loads(marker.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("breeze.json 必须是 JSON 对象")
            preset = {"name": entry.name, "label": str(raw.get("label") or entry.name),
                      "kind": raw.get("kind"), "params": raw.get("params") or {},
                      "provider": "breeze"}
            _validated_params(preset)
            if preset["kind"] == "clone":
                ref = raw.get("ref_audio")
                if ref:
                    ref_path = Path(ref)
                    if not ref_path.is_absolute():
                        ref_path = entry / ref_path
                else:
                    ref_path = next((p for p in sorted(entry.iterdir())
                                     if p.is_file() and p.suffix.lower() in AUDIO_EXTS), None)
                if ref_path is None or not ref_path.is_file():
                    raise ValueError("克隆音色缺少本地参考音频")
                ref_text = raw.get("ref_text")
                if ref_text is None:
                    text_file = entry / "ref_text.txt"
                    ref_text = text_file.read_text(encoding="utf-8").strip() \
                        if text_file.is_file() else ""
                if not isinstance(ref_text, str):
                    raise ValueError("ref_text 必须是参考音频的文字")
                preset.update(ref_audio=str(ref_path.resolve()), ref_text=ref_text)
            out[entry.name] = preset
        except (OSError, ValueError, TypeError, BreezeTTSError) as exc:
            log.warning("忽略 Breeze 音色 %s: %s", entry.name, exc)
    return out


def _response_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
        if isinstance(payload, dict):
            payload = payload.get("detail", payload.get("error", payload.get("message", payload)))
        detail = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    except (ValueError, UnicodeDecodeError):
        detail = response.text
    return " ".join(str(detail).split())[:500] or "服务没有返回错误说明"


class BreezeTTS:
    def __init__(self, base: str, timeout_sec: float = 300):
        self._active_base = ""
        self.cfg_base = base
        self.timeout_sec = float(timeout_sec)
        if self.timeout_sec <= 0:
            raise ValueError("Breeze timeout_sec 必须大于零")
        self._loop_lock = asyncio.Lock()

    @property
    def cfg_base(self) -> str:
        return self._cfg_base

    @cfg_base.setter
    def cfg_base(self, value: str) -> None:
        self._cfg_base = str(value or "").strip().rstrip("/")
        self._active_base = ""

    def active_base(self) -> str:
        return self._active_base or self.cfg_base

    async def synth_pcm(self, text: str, preset: dict) -> bytes:
        """Return raw signed 16-bit little-endian / 24kHz / mono PCM."""
        if not text.strip():
            return b""
        async with self._loop_lock:
            task = asyncio.create_task(self._synth_pcm(text, preset))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # Keep ownership until the server responds. Merely closing the
                # connection can leave inference running, overlapping the next call.
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                if task.done() and not task.cancelled():
                    task.exception()  # Retrieve any failure from the drained call.
                raise

    async def _synth_pcm(self, text: str, preset: dict) -> bytes:
        base = self.cfg_base
        try:
            url = httpx.URL(base)
            if url.scheme not in {"http", "https"} or not url.host or url.query or url.fragment:
                raise ValueError("地址必须是 HTTP(S) 网关地址，且不能包含查询参数")
        except (ValueError, httpx.InvalidURL) as exc:
            raise BreezeTTSError("config", "Breeze 服务地址无效") from exc
        params = _validated_params(preset)
        params["text"] = text
        try:
            # The SSH tunnel must not go through HTTP_PROXY/HTTPS_PROXY.
            async with httpx.AsyncClient(timeout=self.timeout_sec, trust_env=False,
                                         follow_redirects=False) as client:
                if preset["kind"] == "design":
                    response = await client.post(base + "/api/tts/design", json=params)
                else:
                    ref_path = Path(str(preset.get("ref_audio") or ""))
                    if not ref_path.is_file():
                        raise BreezeTTSError("preset", "克隆参考音频不存在")
                    params["reference_text"] = str(preset.get("ref_text") or "")
                    # FastAPI multipart booleans must be textual true/false.
                    form = {key: str(value).lower() if isinstance(value, bool) else str(value)
                            for key, value in params.items()}
                    mime = mimetypes.guess_type(ref_path.name)[0] or "application/octet-stream"
                    with ref_path.open("rb") as ref_file:
                        response = await client.post(base + "/api/tts/clone/upload", data=form,
                                                     files={"voice_ref": (ref_path.name, ref_file, mime)})
        except httpx.TimeoutException as exc:
            raise BreezeTTSError("timeout", "合成请求超时；未自动重试，请检查服务日志后再试") from exc
        except httpx.RequestError as exc:
            raise BreezeTTSError("connection", f"无法连接 Breeze 服务 ({type(exc).__name__})") from exc
        except OSError as exc:
            raise BreezeTTSError("preset", "无法读取克隆参考音频") from exc
        if not response.is_success:
            raise BreezeTTSError("http", _response_detail(response), response.status_code)
        content = response.content
        if len(content) < 12 or content[:4] not in {b"RIFF", b"RF64"} or content[8:12] != b"WAVE":
            detail = _response_detail(response) if "json" in response.headers.get("content-type", "") \
                or content.lstrip().startswith((b"{", b"[")) else "服务返回了非 WAV 数据"
            raise BreezeTTSError("response", detail, response.status_code)
        try:
            pcm = await asyncio.to_thread(audio.decode_to_pcm, None, content, "audio/wav")
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            raise BreezeTTSError("audio", f"返回音频无法解码: {exc}") from exc
        if not pcm or len(pcm) % 2:
            raise BreezeTTSError("audio", "返回音频为空或 PCM 采样不完整")
        # A settings change can occur while this request is still in flight.
        self._active_base = base if self.cfg_base == base else ""
        return pcm
