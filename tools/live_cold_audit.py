"""live_cold_audit.py — 直播冷场体检(2026-10-20 用户要求)。

做三件事(只读, 不动线上):
  1. 拉 `/api/live/status?since=0` 的全量字幕时间轴;
  2. 找**冷场**: 她两次开口之间的间隔 > 阈值, 并列出冷场期间有没有人发弹幕/指令;
  3. 汇总系统事件(挂断/让位/歌/参考图/礼物)与她的开口节奏, 判断冷场原因。

用法: .venv\\Scripts\\python.exe tools\\live_cold_audit.py [--gap 20] [--base http://127.0.0.1:8795]
"""
from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys
import urllib.request
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parent.parent


def get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:                                        # noqa: BLE001
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--gap", type=float, default=20.0, help="超过多少秒算冷场")
    ap.add_argument("--base", default="http://127.0.0.1:8795")
    args = ap.parse_args()

    st = get(args.base + "/api/live/status?since=0")
    rows = sorted(st.get("new_transcripts") or st.get("transcripts") or [], key=lambda x: x.get("ts") or 0)
    if not rows:
        print("  没有字幕数据")
        return 1
    t0 = rows[0]["ts"]
    span = rows[-1]["ts"] - t0

    def hm(ts: float) -> str:
        return datetime.fromtimestamp(ts).strftime("%H:%M:%S")

    dh = [r for r in rows if r.get("role") == "dh"]
    usr = [r for r in rows if r.get("role") == "user"]
    sysr = [r for r in rows if r.get("role") == "sys"]

    print(f"\n[冷场体检] 时间轴 {hm(t0)} → {hm(rows[-1]['ts'])}，跨度 {span:.0f}s")
    print(f"  她开口(dh) {len(dh)} 段 · 输入(user: 弹幕/指令) {len(usr)} 条 · 系统事件 {len(sysr)} 条")

    # 两条 dh 之间的静默
    gaps = []
    for a, b in zip(dh, dh[1:]):
        g = b["ts"] - a["ts"]
        between_usr = [r for r in usr if a["ts"] < r["ts"] < b["ts"]]
        between_sys = [r for r in sysr if a["ts"] < r["ts"] < b["ts"]]
        gaps.append({"gap": g, "from": a, "to": b, "user": between_usr, "sys": between_sys})

    if gaps:
        vals = [x["gap"] for x in gaps]
        print(f"  她的开口间隔: 中位 {statistics.median(vals):.1f}s · 最大 {max(vals):.1f}s")
        cold = [x for x in gaps if x["gap"] >= args.gap]
        print(f"  ≥{args.gap:.0f}s 的冷场: {len(cold)} 次"
              + (f"，最长 {max(x['gap'] for x in cold):.0f}s" if cold else ""))
        for x in sorted(cold, key=lambda y: -y["gap"])[:6]:
            after = x["from"]["text"][:20].replace("\n", " ")
            before = x["to"]["text"][:20].replace("\n", " ")
            print(f"\n    ▸ 冷场 {x['gap']:.0f}s  {hm(x['from']['ts'])} → {hm(x['to']['ts'])}")
            print(f"      她说:「{after}…」 → 下一句:「{before}…」")
            if x["user"]:
                print(f"      期间有人说了 {len(x['user'])} 条:" +
                      " / ".join(u["text"][:16].replace("【弹幕】", "") for u in x["user"][:5]))
            else:
                print("      期间**没人发弹幕/指令**")
            for s in x["sys"][:3]:
                print(f"      系统: {s['text'][:46]}")

    # 系统事件清单
    if sysr:
        print("\n  系统事件:")
        for s in sysr:
            tag = "🚨" if ("挂断" in s["text"] or "结束" in s["text"]) else ("🎵" if "歌" in s["text"] else "·")
            print(f"    {tag} {hm(s['ts'])} {s['text'][:52]}")

    # 最近这一段她说了什么
    print("\n  最近 6 段她说的话:")
    for r in dh[-6:]:
        print(f"    {hm(r['ts'])} {r['text'][:52]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
