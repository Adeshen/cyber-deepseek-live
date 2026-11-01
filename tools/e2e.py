"""真实端到端(会花钱!): 组件版全链路 —— 建会话 → conn_init → 文字驱动 → 查转写 → 挂断看计费。

跑法(项目根目录):
  .venv\\Scripts\\python.exe tools\\e2e.py --mode managed --text "你好,做个自我介绍" --max-wait 60
  .venv\\Scripts\\python.exe tools\\e2e.py --mode customer --provider artc   # 自备阿里频道
  .venv\\Scripts\\python.exe tools\\e2e.py --say-file audio\\xxx.mp3        # 只试点播喂流(需已有会话则先建)

不加 --yes 会先打印将执行的动作并要求回车确认。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config as appconf          # noqa: E402
from src.engine import Engine              # noqa: E402
from src.vidu import client as vu          # noqa: E402


async def run(args) -> int:
    conf = appconf.load()
    c = conf.get("vidu") or {}
    vu.configure(c)          # 积分查询等 HTTP 也要先注入配置
    print(f"host={c.get('host')}  create={c.get('create_path')}  mode={args.mode}")
    try:
        cr = await vu.credits()
        print(f"积分余额: {json.dumps(cr, ensure_ascii=False)[:160]}")
    except Exception as e:  # noqa: BLE001
        print(f"(积分查询失败, 不影响: {e})")

    eng = Engine()
    t0 = time.time()
    res = await eng.start({"mode": args.mode, **({"customer_provider": args.provider} if args.provider else {})})
    print(f"[{time.time()-t0:.1f}s] 会话已建立 live_id={res['live_id']} rtc={json.dumps(res['rtc'], ensure_ascii=False)[:220]}")

    if args.say_file:
        r = await eng.say_file(args.say_file, script=args.script or "")
        print(f"点播: {r} (等它播完…)")
        await asyncio.sleep(r.get("seconds", 3) + 2)
    else:
        await eng.chat(args.text)
        print(f"已发送: {args.text!r} (等 LLM→TTS→喂流…)")
        deadline = time.time() + args.max_wait
        seen = 0
        while time.time() < deadline:
            await asyncio.sleep(2)
            st = eng.status(seen)
            for t in (st.get("new_transcripts") or []):
                seen = max(seen, t["i"])
                print(f"  [{t['role']}] {t['text'][:120]}")
            if st.get("ws") and st["ws"].get("audio_seconds", 0) > 0 and not st.get("speaking"):
                # 播完了
                await asyncio.sleep(1.5)
                break
    st = eng.status(-1)
    print(f"WS stats: {st.get('ws')}")
    stop = await eng.stop("e2e_done")
    print(f"已挂断: {json.dumps(stop.get('ended'), ensure_ascii=False)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="managed", choices=["managed", "customer"])
    ap.add_argument("--provider", default="", help="customer 模式的 provider(artc/trtc)")
    ap.add_argument("--text", default="你好呀，简单介绍一下你自己")
    ap.add_argument("--say-file", default="", help="跳过 LLM, 直接点播该音频文件")
    ap.add_argument("--script", default="", help="点播时同步的 type10 文本")
    ap.add_argument("--max-wait", type=float, default=60)
    ap.add_argument("--yes", action="store_true", help="跳过确认(确认会真实扣积分)")
    args = ap.parse_args()
    if not args.yes:
        est = args.max_wait
        print(f"⚠ 将创建真实会话并可能播放 ~{est:.0f}s 音频(按 1 积分/秒计费)。回车继续, Ctrl+C 取消。")
        input()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n(中断)")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
