"""Owned subscription to a normalized local danmaku WebSocket.

This module never starts Vidu or generates speech. The caller decides whether
dispatch is allowed (for example, only while its engine is live). Event text,
nicknames, source URLs, and upstream exception messages never enter status/logs.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
import json
import math
import time
import unicodedata
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.client import connect


_CACHE_LIMIT = 4096
_CONTENT_TTL_SECONDS = 20.0
_FOLLOW_TTL_SECONDS = 300.0
_ROOM_STATES = {"connecting", "live", "closed", "error", "stopped"}
_CONTROL_TYPES = {"__hello", "__connecting", "__connected", "__error"}
_COUNT_NAMES = (
    "received", "control", "invalid", "unsupported", "wrong_room", "duplicates",
    "content_duplicates", "stale",
    "chat", "gift", "follow", "follow_duplicates", "social_ignored",
    "gift_progress", "gift_duplicates", "dispatch_attempts",
    "dispatched", "queued", "idle", "not_queued", "dispatch_errors",
    "connections", "disconnects", "reconnects", "connection_errors", "source_errors",
)


def _identity(value) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    text = str(value).strip()
    return text if 0 < len(text) <= 256 else ""


def _nonnegative_int(value, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
        if not math.isfinite(number) or number < 0:
            return default
        return int(number)
    except (ValueError, TypeError, OverflowError):
        return default


class DanmakuSource:
    """One cancellable WS reader, with bounded at-most-once event consumption."""

    def __init__(self, conf_get: Callable[[], dict],
                 dispatch: Callable[[dict], Awaitable[dict]]):
        self._conf_get = conf_get
        self._dispatch = dispatch
        self._enabled = False
        self._url = ""
        self._room_id = ""
        self._platform = "抖音"
        self._max_age_ms = 20000
        self._state = "stopped"
        self._room_status = "unknown"
        self._last_error = ""
        self._counts = dict.fromkeys(_COUNT_NAMES, 0)
        self._seen: OrderedDict[tuple[str, str, str], None] = OrderedDict()
        self._content_seen: OrderedDict[tuple[str, str, str, str], float] = OrderedDict()
        self._gift_totals: OrderedDict[tuple[str, str, str, str], int] = OrderedDict()
        self._follow_users: OrderedDict[str, float] = OrderedDict()
        self._task: asyncio.Task | None = None
        self._ws = None
        self._stopping = False

    async def start(self) -> None:
        """Read configuration once per start; repeated starts share one reader."""
        if self._task is not None and not self._task.done():
            return
        try:
            conf = self._conf_get() or {}
            if not isinstance(conf, dict):
                raise ValueError
            enabled = conf.get("enabled", False)
            if not isinstance(enabled, bool):
                raise ValueError
            room_id = _identity(conf.get("room_id"))
            url = conf.get("url", "")
            if not isinstance(url, str):
                raise ValueError
            platform = conf.get("platform") or "抖音"
            if not isinstance(platform, str):
                raise ValueError
            max_age_ms = max(1, _nonnegative_int(conf.get("max_age_ms"), 20000))
            if enabled:
                parsed = urlsplit(url)
                if parsed.scheme not in {"ws", "wss"} or not parsed.hostname or not room_id:
                    raise ValueError
                _ = parsed.port  # Validate the port without exposing an invalid URL.
        except Exception:
            self._enabled = False
            self._state = "error"
            self._last_error = "弹幕源配置无效"
            return

        if room_id != self._room_id or url != self._url:
            self._seen.clear()
            self._content_seen.clear()
            self._gift_totals.clear()
            self._follow_users.clear()
            self._room_status = "unknown"
        self._enabled = enabled
        self._url = url
        self._room_id = room_id
        self._platform = platform.strip()[:20] or "抖音"
        self._max_age_ms = max_age_ms
        self._last_error = ""
        self._stopping = False
        if not enabled:
            self._state = "disabled"
            return
        self._state = "connecting"
        self._task = asyncio.create_task(self._run(), name="danmaku-source")

    async def stop(self) -> None:
        """Cancel the owned reader/backoff and close only its owned socket."""
        self._stopping = True
        task, ws = self._task, self._ws
        if task is not None:
            task.cancel()
        if ws is not None:
            try:
                await asyncio.wait_for(ws.close(), timeout=3)
            except (Exception, asyncio.CancelledError):
                pass
        if task is not None:
            try:
                await task
            except (Exception, asyncio.CancelledError):
                pass
        self._task = None
        self._ws = None
        self._state = "stopped"

    def status(self) -> dict:
        """WS connectivity and collector room status are deliberately separate."""
        return {"enabled": self._enabled, "state": self._state,
                "room_id": self._room_id, "room_status": self._room_status,
                "subscription_types": [kind for kind in parse_qs(urlsplit(self._url).query)
                                       .get("types", [""])[0].split(",")
                                       if kind in {"chat", "gift", "follow", "roomStatus"}],
                "counts": dict(self._counts), "last_error": self._last_error}

    async def _run(self) -> None:
        delay = 1.0
        while not self._stopping:
            connected_at = None
            self._state = "connecting" if not self._counts["reconnects"] else "reconnecting"
            try:
                async with connect(self._url, proxy=None, open_timeout=10,
                                   ping_interval=20, ping_timeout=20,
                                   close_timeout=3, max_size=1024 * 1024) as ws:
                    self._ws = ws
                    connected_at = asyncio.get_running_loop().time()
                    self._counts["connections"] += 1
                    self._state = "connected"
                    self._room_status = "unknown"
                    self._last_error = ""
                    async for raw in ws:
                        await self._handle_message(raw)
                if not self._stopping:
                    self._counts["disconnects"] += 1
                    self._last_error = "弹幕连接已断开"
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._stopping:
                    self._counts["connection_errors"] += 1
                    # Class names are sufficient for diagnostics; exception text
                    # can contain credentials or the complete source URL.
                    self._last_error = f"弹幕连接异常（{type(exc).__name__}）"
            finally:
                self._ws = None
            if self._stopping:
                return
            if connected_at is not None and asyncio.get_running_loop().time() - connected_at >= 30:
                delay = 1.0
            self._counts["reconnects"] += 1
            self._state = "reconnecting"
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    @staticmethod
    def _remember(cache: OrderedDict, key, value) -> None:
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > _CACHE_LIMIT:
            cache.popitem(last=False)

    def _set_room_status(self, value) -> None:
        self._room_status = value if isinstance(value, str) and value in _ROOM_STATES else "unknown"

    def _chat_fingerprint(self, user_id: str, nick: str, content: str) -> tuple[str, str, str, str]:
        # Preserve punctuation while unifying width, case and redundant spaces.
        normalized = " ".join(unicodedata.normalize("NFKC", content).casefold().split())
        identity = "id:" + user_id if user_id else "nick:" + nick
        return self._platform, self._room_id, identity, normalized

    def _content_recently_queued(self, fingerprint) -> bool:
        now = time.monotonic()
        # Successful queue timestamps stay in insertion order; duplicate reads do
        # not extend the window or reorder entries ahead of older timestamps.
        while self._content_seen:
            oldest = next(iter(self._content_seen))
            if now - self._content_seen[oldest] < _CONTENT_TTL_SECONDS:
                break
            self._content_seen.popitem(last=False)
        queued_at = self._content_seen.get(fingerprint)
        return queued_at is not None and now - queued_at < _CONTENT_TTL_SECONDS

    async def _handle_message(self, raw) -> None:
        self._counts["received"] += 1
        try:
            event = json.loads(raw)
        except (ValueError, TypeError, UnicodeError):
            self._counts["invalid"] += 1
            return
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            self._counts["invalid"] += 1
            return
        if _identity(event.get("roomId")) != self._room_id:
            self._counts["wrong_room"] += 1
            return
        kind = event["type"]
        if kind in _CONTROL_TYPES:
            self._counts["control"] += 1
            if kind == "__connecting":
                self._room_status = "connecting"
            elif kind == "__connected":
                room = event.get("room")
                self._set_room_status(room.get("status") if isinstance(room, dict) else None)
            elif kind == "__error":
                self._counts["source_errors"] += 1
                self._room_status = "error"
                self._last_error = "弹幕源报告房间错误"
            return
        if kind == "roomStatus":
            self._counts["control"] += 1
            data = event.get("data")
            self._set_room_status(data.get("status") if isinstance(data, dict) else None)
            return
        if kind not in {"chat", "gift", "follow"}:
            self._counts["unsupported"] += 1
            return

        data, user = event.get("data"), event.get("user")
        event_id = _identity(event.get("id"))
        if not event_id or not isinstance(data, dict):
            self._counts["invalid"] += 1
            return
        event_key = (self._room_id, kind, event_id)
        if event_key in self._seen:
            self._seen.move_to_end(event_key)
            self._counts["duplicates"] += 1
            return
        # Consume once even when dispatch fails ambiguously. Never replay a
        # failed event into a paid pipeline after reconnecting.
        self._remember(self._seen, event_key, None)
        user = user if isinstance(user, dict) else {}
        user_id = _identity(user.get("id"))
        event_ts = _nonnegative_int(event.get("ts"))  # Normalized upstream time is milliseconds.
        nick = user.get("nickname")
        nick = nick.strip()[:40] if isinstance(nick, str) else ""
        nick = nick or "观众"
        fingerprint = None
        follow_identity = None
        if kind == "follow":
            # Douyin social action: 1 follow, 2 unfollow, 3 share.
            if _nonnegative_int(data.get("action")) != 1:
                self._counts["social_ignored"] += 1
                return
            if event_ts and time.time() * 1000 - event_ts > self._max_age_ms:
                self._counts["stale"] += 1
                return
            follow_identity = user_id or (nick if nick != "观众" else event_id)
            previous = self._follow_users.get(follow_identity)
            if previous is not None and time.monotonic() - previous < _FOLLOW_TTL_SECONDS:
                self._counts["follow_duplicates"] += 1
                return
            notification = "平台通知：这位观众刚刚关注了直播间。请自然简短感谢TA的关注；这不是观众发言，也不是送礼。"
            payload = {"text": notification, "content": notification, "nick": nick,
                       "platform": self._platform, "kind": "follow",
                       "priority": "vip", "answer_only": True}
        elif kind == "chat":
            content = data.get("content")
            if not isinstance(content, str) or not content.strip():
                self._counts["invalid"] += 1
                return
            content = content.strip()[:200]
            if event_ts and time.time() * 1000 - event_ts > self._max_age_ms:
                self._counts["stale"] += 1
                return
            self._room_status = "live"
            fingerprint = self._chat_fingerprint(user_id, nick, content)
            if self._content_recently_queued(fingerprint):
                self._counts["content_duplicates"] += 1
                return
            payload = {"text": content, "content": content, "nick": nick,
                       "platform": self._platform, "kind": ""}
        else:
            item = data.get("giftName")
            if not isinstance(item, str) or not item.strip():
                self._counts["invalid"] += 1
                return
            item = item.strip()[:80]
            # With types=chat,gift, roomStatus updates may not be subscribed.
            # A valid gift (including a combo progress frame) shows activity.
            self._room_status = "live"
            if data.get("combo") is True and data.get("repeatEnd") is not True:
                self._counts["gift_progress"] += 1
                return
            total = max(_nonnegative_int(data.get("repeatCount")),
                        _nonnegative_int(data.get("comboCount")),
                        _nonnegative_int(data.get("groupCount")), 1)
            uid, gift_id, group_id = (_identity(user.get("id")),
                                      _identity(data.get("giftId")),
                                      _identity(data.get("groupId")))
            quantity = total
            if uid and gift_id and group_id:
                group_key = (self._room_id, uid, gift_id, group_id)
                previous = self._gift_totals.get(group_key, 0)
                if total <= previous:
                    self._gift_totals.move_to_end(group_key)
                    self._counts["gift_duplicates"] += 1
                    return
                quantity = total - previous
                # Reserve cumulative consumption before awaiting dispatch so
                # repeated final notifications cannot retry ambiguous failures.
                self._remember(self._gift_totals, group_key, total)
            payload = {"text": item, "content": item, "nick": nick,
                       "platform": self._platform, "kind": "gift", "gift_item": item,
                       "gift_num": quantity,
                       "gift_group_id": group_id, "gift_total": total,
                       "gift_value": _nonnegative_int(data.get("diamondCount")) * quantity}

        payload.update({"event_id": event_id, "user_id": user_id,
                        "room_id": self._room_id, "event_ts": event_ts})
        self._room_status = "live"
        self._counts[kind] += 1
        self._counts["dispatch_attempts"] += 1
        try:
            result = await self._dispatch(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._counts["dispatch_errors"] += 1
            self._last_error = f"弹幕转发异常（{type(exc).__name__}）"
            return
        self._counts["dispatched"] += 1
        if isinstance(result, dict) and result.get("queued") is True:
            if follow_identity is not None:
                self._remember(self._follow_users, follow_identity, time.monotonic())
            self._counts["queued"] += 1
            if fingerprint is not None:
                self._remember(self._content_seen, fingerprint, time.monotonic())
        elif isinstance(result, dict) and result.get("reason") == "idle":
            self._counts["idle"] += 1
        else:
            self._counts["not_queued"] += 1
