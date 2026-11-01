#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""douyin_relay.py —— 把本地抖音弹幕采集服务的消息，转发进舞台桥。

背景
----
抖音弹幕不能直接读（需要签名），社区通行做法是跑一个**本地采集服务**：
  · 上游开源项目: https://github.com/jwwsjlm/douyinLive （Go 写的，自带 ws + http）
  · 默认监听 1089，配置见它自己的 config.yaml
  · 房间弹幕流: ws://127.0.0.1:1089/ws/<房间号>
  · 房间状态:   http://127.0.0.1:1089/api/v1/rooms/<房间号>/status

本脚本做的事很简单：订阅上面那条 ws，把 JSON 消息翻译成舞台桥认识的格式，
POST 到 http://127.0.0.1:8795/api/danmaku。**不依赖第三方采集器内部实现**，
只要它发的是同样的 JSON（method/user/content/gift 字段）就能用。

用法
----
    python tools/douyin_relay.py --room 123456789
    python tools/douyin_relay.py --room 123456789 --bridge http://127.0.0.1:8795
    python tools/douyin_relay.py --room 123456789 --verbose     # 打印忽略掉的消息类型

依赖: websockets + httpx（都在 requirements.txt 里，桥本身也在用）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys

import httpx
import websockets


def _as_int(v, default=0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def to_bridge_payload(msg: dict) -> dict | None:
    """把 douyinLive 的一条消息翻译成桥的 /api/danmaku 请求体；不关心的消息返回 None。"""
    if not isinstance(msg, dict) or msg.get("type") == "system":
        return None
    method = msg.get("method") or ""
    user = msg.get("user") or {}
    nick = str(user.get("nickname") or "").strip()

    if method == "WebcastChatMessage":
        content = str(msg.get("content") or "").strip()
        if not nick or not content:
            return None
        return {"nick": nick, "content": content, "text": content,
                "platform": "抖音", "kind": ""}

    if method in ("WebcastGiftMessage", "WebcastLightGiftMessage", "WebcastBindingGiftMessage"):
        inner = msg.get("msg") if method == "WebcastBindingGiftMessage" else msg
        if not isinstance(inner, dict) or not inner:
            return None
        if method == "WebcastBindingGiftMessage":
            nick = str(((inner.get("user") or {}).get("nickname")) or nick).strip()
        gift = inner.get("gift") or {}
        item = str(gift.get("name") or "").strip()
        if not nick or not item:
            return None
        # 连击礼物会连续推很多帧：只在「连击结束」或非连击礼物时转发，避免把数量算重
        grouped = bool(inner.get("groupId"))
        if grouped and not _as_int(inner.get("repeatEnd")):
            return None
        num = max(_as_int(inner.get("repeatCount")), _as_int(inner.get("comboCount")),
                  _as_int(inner.get("totalCount")), _as_int(inner.get("count")),
                  _as_int(inner.get("groupCount")), 1)
        return {"kind": "gift", "nick": nick, "gift_item": item, "gift_num": num,
                "gift_value": _as_int(gift.get("diamondCount")), "platform": "抖音",
                "content": item, "text": item}

    return None


async def relay(room: str, ws_base: str, bridge: str, verbose: bool) -> None:
    url = f"{ws_base.rstrip('/')}/ws/{room}"
    push_url = f"{bridge.rstrip('/')}/api/danmaku"
    backoff = 3
    async with httpx.AsyncClient(timeout=8.0) as cli:
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    print(f"[relay] 已连接 {url}", flush=True)
                    backoff = 3
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                        except (ValueError, TypeError):
                            continue
                        if isinstance(msg, dict) and msg.get("type") == "system":
                            if verbose:
                                print(f"[relay] 系统: {msg.get('code')} {msg.get('message') or ''}", flush=True)
                            continue
                        payload = to_bridge_payload(msg)
                        if payload is None:
                            if verbose:
                                print(f"[relay] 忽略 {msg.get('method')}", flush=True)
                            continue
                        try:
                            r = await cli.post(push_url, json=payload)
                            if r.status_code >= 400:
                                print(f"[relay] 桥返回 {r.status_code}: {r.text[:120]}", flush=True)
                        except Exception as e:                    # noqa: BLE001
                            print(f"[relay] 转发失败: {type(e).__name__}: {e}", flush=True)
            except asyncio.CancelledError:
                raise
            except Exception as e:                                # noqa: BLE001
                print(f"[relay] 连接断开: {type(e).__name__}: {e} · {backoff}s 后重连", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff + 3, 30)


def main() -> None:
    ap = argparse.ArgumentParser(description="抖音弹幕 → 舞台桥 转发器")
    ap.add_argument("--room", required=True, help="抖音直播间号（房间号，不是昵称）")
    ap.add_argument("--ws-base", default="ws://127.0.0.1:1089", help="douyinLive 服务地址")
    ap.add_argument("--bridge", default="http://127.0.0.1:8795", help="舞台桥地址")
    ap.add_argument("--verbose", action="store_true", help="打印被忽略的消息类型/系统事件")
    args = ap.parse_args()
    try:
        asyncio.run(relay(args.room, args.ws_base, args.bridge, args.verbose))
    except KeyboardInterrupt:
        print("\n[relay] 已退出", flush=True)


if __name__ == "__main__":
    main()
