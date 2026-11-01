"""Bridge a changed subscription until the running pre-fix bridge is restarted.

Only normalized chat/gift/follow events go to the existing live session. No provider
session is created. Exit automatically when the built-in subscriber takes over.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx
from src.danmaku_source import DanmakuSource

BASE = "http://127.0.0.1:8795"
STATUS_FILE = ROOT / "logs/danmaku-relay-status.json"


async def main() -> None:
    source_conf = {}
    report = {"pid": os.getpid(), "started_at": time.time(), "mode": "starting"}
    client = httpx.AsyncClient(trust_env=False, timeout=6)

    def save():
        report["updated_at"] = time.time()
        temporary = STATUS_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(STATUS_FILE)

    async def dispatch(payload: dict) -> dict:
        health = await client.get(BASE + "/api/health")
        health.raise_for_status()
        if health.json().get("phase") != "live":
            return {"ok": False, "queued": False, "reason": "idle"}
        event_kind = payload.get("kind") or "chat"
        forwarded = dict(payload)
        # The already-running pre-update bridge accepts VIP notifications via
        # its normal queue. Future bridges have the dedicated follow branch.
        if event_kind == "follow":
            forwarded["kind"] = ""
        reply = await client.post(BASE + "/api/danmaku", json=forwarded)
        reply.raise_for_status()
        result = reply.json()
        report["last_event_at"] = time.time()
        report["last_event_kind"] = event_kind
        report["last_queued"] = result.get("queued") is True
        totals = report.setdefault("forwarded_by_kind", {})
        totals[event_kind] = totals.get(event_kind, 0) + 1
        return result

    source = DanmakuSource(lambda: source_conf, dispatch)
    try:
        while True:
            try:
                response = await client.get(BASE + "/api/settings")
                response.raise_for_status()
                latest = response.json().get("danmaku_source") or {}
                builtin_reply = await client.get(BASE + "/api/danmaku/source")
                builtin_reply.raise_for_status()
                builtin = builtin_reply.json()
                if (latest.get("enabled") and builtin.get("room_id") == latest.get("room_id")
                        and builtin.get("state") == "connected"
                        and {"chat", "gift", "follow"}.issubset(set(builtin.get("subscription_types") or []))):
                    report["mode"] = "builtin_took_over"
                    break
                if latest != source_conf:
                    await source.stop()
                    source_conf = latest
                    await source.start()
                report["mode"] = "forwarding"
                report["source"] = source.status()
                report["builtin_room_id"] = builtin.get("room_id")
                report.pop("error_type", None)
            except Exception as exc:
                report["mode"] = "waiting_for_bridge"
                report["error_type"] = type(exc).__name__
            save()
            await asyncio.sleep(2)
    finally:
        await source.stop()
        await client.aclose()
        report["stopped_at"] = time.time()
        save()


if __name__ == "__main__":
    asyncio.run(main())
