# 一次性查询: 10/09-10/10 连线时间最长的两场 live
import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from vidu import client as vu  # noqa: E402
from config import load as cfg_load  # noqa: E402


def epoch(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def fmt(sec):
    sec = int(sec)
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m{sec % 60:02d}s" if sec >= 3600 else f"{sec // 60}m{sec % 60:02d}s"


async def main():
    vu.configure(cfg_load().get("vidu") or {})
    t0 = int(datetime(2026, 10, 9, 0, 0, 0).timestamp())
    t1 = int(datetime(2026, 10, 11, 0, 0, 0).timestamp())
    items, page = [], 0
    while page < 15:
        d = await vu.list_lives(page, 50)
        got = d.get("items") or d.get("lives") or d.get("data") or []
        items += got
        if len(got) < 50 or any(epoch(x.get("created_at")) < t0 for x in got):
            break
        page += 1
    picked = [it for it in items if t0 <= epoch(it.get("created_at")) < t1]
    print(f"拉到 {len(items)} 场, 其中 10/09-10/10 共 {len(picked)} 场\n")
    for day in ("2026-10-10", "2026-10-09"):
        day_items = [it for it in picked
                     if datetime.fromtimestamp(epoch(it["created_at"])).strftime("%Y-%m-%d") == day]
        day_items.sort(key=lambda it: int(it.get("billed_seconds") or 0), reverse=True)
        print(f"── {day} (共 {len(day_items)} 场, 按计费时长降序, 列前 12) ──")
        for it in day_items[:12]:
            st = datetime.fromtimestamp(epoch(it.get("start_time"))).strftime("%H:%M:%S") if epoch(it.get("start_time")) else "?"
            en = datetime.fromtimestamp(epoch(it.get("end_time"))).strftime("%H:%M:%S") if epoch(it.get("end_time")) else "?"
            print(f"  live_id={it.get('id')}")
            print(f"    {st} → {en}  时长 {fmt(int(it.get('live_duration') or 0))}  "
                  f"计费 {it.get('billed_seconds', '?')}s = {it.get('credits_cost', '?')} 积分  "
                  f"[{it.get('status')}/{it.get('close_reason')}] model={it.get('model')}")
        print()


asyncio.run(main())
