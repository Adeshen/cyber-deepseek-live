#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""test_dm_t0.py — dm_router T0 离线冒烟(不起桥不联网): 归一化+别名命中断言。"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import dm_router  # noqa: E402

NAMES = ["对镜头比心", "眨单眼", "飞吻", "轻轻地跳舞", "转一圈", "拿出一只猫", "拿出麦克风唱歌",
         "吃西瓜", "举杯奶茶", "照镜子补口红", "双手合十拜托", "傲娇扭头哼", "委屈瘪嘴", "点关注"]

r = dm_router.DanmakuRouter()
r.init(engine=None, fetch_names=None, conf_get=lambda: {"enabled": True, "mode": "confirm"})
r._names = set(NAMES)
r._rebuild_dict()

CASES = [
    ("小雅，我要看比个心", "对镜头比心"),
    ("小雅给我飞吻一个", "飞吻"),
    ("可以亲我一个吗", "凑近亲镜头"),
    ("吃个西瓜呗", "吃西瓜"),
    ("猫", "拿出一只猫"),
    ("来只猫", "拿出一只猫"),
    ("跳个舞吧", "轻轻地跳舞"),
    ("唱首歌", "拿出麦克风唱歌"),
    ("求个关注", "点关注"),
    ("比心", "对镜头比心"),
    ("今天天气怎么样", None),
    ("你多大了", None),
    ("主播你好好笑哈哈哈哈", None),
]

fails = 0
for raw, want in CASES:
    got = r.match(raw)
    mark = "✓" if got == want else "✗"
    if got != want:
        fails += 1
    print(f"{mark} {raw!r:28} → {got!r} (期望 {want!r})")

print(f"\n归一化样例: '可以亲我一个吗' → {r._normalize('可以亲我一个吗')!r}")
print(f"{'T0 冒烟通过 ✓' if fails == 0 else f'T0 冒烟失败 {fails} 例 ✗'}")
sys.exit(1 if fails else 0)
