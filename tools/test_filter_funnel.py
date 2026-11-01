"""离线漏斗单测(2026-10-19): 不连线、不花 token(T2 用例用假的 _t2)。

覆盖这次改动的三块:
  ① 复合指令直通  —— 「先戴上墨镜，然后涂口红」不再只执行一个
  ② 道具玩具包装  —— 「拿出AK47」→「拿出玩具AK47」; 真实暴力语境不包装
  ③ 长句子串约束  —— 「…那个小蛋糕」不再被"小蛋糕"骗走
  另外补: 否定句不触发、洪峰队列全扫、T2 预算每条周期只花 1 次。

用法: .venv\\Scripts\\python.exe tools\\test_filter_funnel.py
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import dm_router                                    # noqa: E402

# 假动作库(真实库 96 条里挑代表) + 一条长名动作
NAMES = ["对镜头比心", "凑近看弹幕", "别头发", "拿出墨镜戴上", "涂口红", "轻轻地跳舞",
         "双马尾", "高马尾", "拿出麦克风唱歌", "凑近眨眼比耶", "歪头笑", "拿出小蛋糕",
         "把鼻子上的头发别到耳朵后面", "转一圈", "站起来"]

RESULTS: list[tuple[str, bool, str]] = []


class FakeEngine:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def cmd(self, text: str, source: str = "auto") -> dict:
        self.sent.append((text, source))
        return {"ok": True}


def make_router() -> dm_router.DanmakuRouter:
    r = dm_router.DanmakuRouter()
    r._names = set(NAMES)
    r.init(FakeEngine(), fetch_names=None,
           conf_get=lambda: {"enabled": True, "mode": "auto", "cooldown_s": 0,
                             "read_interval_s": 4, "same_action_windows": [0, 0, 0]})
    r._trusted = {}                       # 别让真实 stats 里的信任表影响单测
    r._load_aliases()
    return r


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(("  ✅ " if ok else "  ❌ ") + name + ("  " + detail if detail else ""))


async def one(r: dm_router.DanmakuRouter, content: str, allow_t2: bool = False):
    r.engine.sent.clear()
    status, used = await r._process_one("观众", content, allow_t2=allow_t2)
    return status, used, list(r.engine.sent)


async def main() -> int:
    r = make_router()
    print("\n① 复合指令直通")
    st, _, sent = await one(r, "小雅，先戴上墨镜，然后涂口红")
    check("两个动作都保留(整句直通)", len(sent) == 1 and "墨镜" in sent[0][0] and "口红" in sent[0][0],
          f"→ {sent}")
    check("复合指令走 t0c 通道", sent and sent[0][1] == "t0c", f"source={sent[0][1] if sent else '-'}")

    st, _, sent = await one(r, "把头发别到耳朵后，然后凑近看弹幕")
    check("顺序/标点原样交给 Vidu", len(sent) == 1 and "别到耳朵后" in sent[0][0] and "凑近看弹幕" in sent[0][0],
          f"→ {sent}")

    st, _, sent = await one(r, "不要再跳舞了")
    check("否定句不触发(不要再…)", not sent, f"status={st} → {sent}")

    st, _, sent = await one(r, "先别哭，乖乖的")
    check("劝阻句不触发(先别…)", not sent, f"status={st} → {sent}")

    print("\n② 道具玩具包装")
    for raw, want in [("拿出AK47", "拿出一把迷你玩具AK47"),
                      ("拿出火箭筒", "拿出一把迷你玩具火箭筒"),
                      ("给我看看你的M4", "拿出一把迷你玩具M4"),
                      ("掏出匕首", "拿出一把迷你玩具匕首")]:
        st, _, sent = await one(r, raw)
        check(f"{raw} → {want}", bool(sent) and sent[0][0] == want, f"实际 {sent}")

    st, _, sent = await one(r, "拿枪打死他们")
    check("真实暴力不包装(fire 不触发)", not sent, f"status={st} → {sent}")

    st, _, sent = await one(r, "拿出玩具AK47")
    check("已带玩具前缀不重复加", not sent or sent[0][0].count("玩具") == 1, f"→ {sent}")

    print("\n③ T0 子串长度约束")
    st, _, sent = await one(r, "拿出手机看看我上次给你买的那个小蛋糕")
    check("长句里的短动作名不直发", not sent, f"status={st} → {sent}")
    st, _, sent = await one(r, "比个心")
    check("短句正常命中(比个心→对镜头比心)", bool(sent) and "比心" in sent[0][0], f"→ {sent}")
    st, _, sent = await one(r, "你唱歌真好听")
    check("评价句不触发", not sent, f"status={st} → {sent}")

    print("\n④ 洪峰队列: 一拍拍全扫 T0")
    r2 = make_router()
    for c in ["小雅比个心", "拿出火箭筒", "先戴上墨镜，然后涂口红", "今天天气不错呀", "歪头笑"]:
        r2.enqueue("观众", c)
    items = r2._drain_inbox()
    check("队列取到 5 条(不再只剩最后 1 条)", len(items) == 5, f"drain={len(items)}")
    r2.engine.sent.clear()
    for it in items:
        await r2._process_one(it["nick"], it["content"], allow_t2=False)
    check("其中 4 条命中全部发出", len(r2.engine.sent) == 4, f"→ {[t for t, _ in r2.engine.sent]}")

    print("\n⑤ T2 预算: 每拍最多 1 次")
    r3 = make_router()
    calls = {"n": 0}

    async def fake_t2(content: str):
        calls["n"] += 1
        return {"result": "none"}

    r3._t2 = fake_t2                      # type: ignore[assignment]
    for c in ["表演个魔术", "给我变个魔术", "来段魔术吧"]:
        r3.enqueue("观众", c)
    items = r3._drain_inbox()
    t2_left = 1
    for it in items:
        st, used = await r3._process_one(it["nick"], it["content"], allow_t2=(t2_left > 0))
        if used:
            t2_left -= 1
    check("3 条不确定弹幕只花 1 次 T2", calls["n"] == 1, f"T2 调用 {calls['n']} 次")

    print("\n⑥ 词表热读(外置 dm-filter.json)")
    fdata = r._filt()
    check("dm-filter.json 读到 prefilter_words", bool(fdata.get("prefilter_words")),
          f"{len(fdata.get('prefilter_words') or [])} 词")
    check("道具包装规则在位", bool(r._prop_cfg().get("pattern")))
    check("复合指令开关在位", r._composite_cfg()["enabled"])

    print("\n⑦ 动作类弹幕不念(播报通道 → answer_only)")
    for raw, want in [("比个心", True), ("先戴上墨镜，然后涂口红", True),
                      ("拿出AK47", True), ("今天天气不错", False),
                      ("你唱歌真好听", False), ("不要再跳舞了", False)]:
        got = r.looks_like_cmd(raw)
        check(f"{raw} → {'动作类(不念)' if want else '普通弹幕(照念)'}", got == want,
              f"looks_like_cmd={got}")

    print("\n⑧ 动作黑名单（站起来 这类，任何通道都不许进 Vidu）")
    for raw in ["站起来", "小雅站起来", "站起来转个身", "站起来跳个舞", "起身看看", "站直了"]:
        st, _, sent = await one(r, raw)
        check(f"{raw} → 不触发", (not sent) and st == "blocked", f"status={st} → {sent}")
    for raw in ["跳个舞", "转一圈", "比个心"]:
        st, _, sent = await one(r, raw)
        check(f"黑名单不误伤「{raw}」", bool(sent), f"→ {sent}")

    fails = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n结果: {len(RESULTS) - len(fails)}/{len(RESULTS)} 通过")
    if fails:
        print("失败项: " + "; ".join(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
