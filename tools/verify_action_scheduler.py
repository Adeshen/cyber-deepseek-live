"""真实链路验证: 动作调度器(唱歌窗口节流 + 最新优先合并)。

会花钱(建真实 Vidu 会话, 按 1 积分/秒计)。跑法(项目根目录):
  .venv\\Scripts\\python.exe tools\\verify_action_scheduler.py --yes --file "R16 渡月桥思君.flac"

它驱动**正在运行的桥**(http://127.0.0.1:8795), 不自己建引擎 —— 这样测到的就是
直播用的那条真实路径(/api/cmd → engine.cmd → type9, /api/say/file → say_file)。

验的五件事(对应 docs 开发笔记条目 28):
  T1 非唱歌时连发: 零闸门(全部 deferred=false) —— 尊重"取消动作冷却"的拍板
  T2 唱歌窗口: 第 1 条即时, 紧跟的连点被合并(deferred=true)
  T3 节流到期: 只补发"最新一条", 中间的永不发出
  T4 特效一致性: dm_feed 里的 manual fire 只有真正发出的那些
  T5 停止演唱: 不补发多余信号(自动复位已撤掉) + 停歌后动作照常即时
  T6 挂断计费正常
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8795"
LOG = ROOT / "logs" / "bridge.log"

PASS = FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [ok] {name}")
    else:
        FAIL += 1
        print(f"  [X]  {name}  {detail}")


def api(path: str, payload: dict | None = None, timeout: float = 30):
    """POST(带 body)/GET(无 body)。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return {"_http_error": e.code, "_body": e.read().decode("utf-8", "replace")[:200]}
    except Exception as e:  # noqa: BLE001
        return {"_error": f"{type(e).__name__}: {e}"}


def log_lines() -> list[str]:
    if not LOG.is_file():
        return []
    try:
        return LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:  # noqa: BLE001
        return []


def new_since(mark: int, *needles: str) -> list[str]:
    return [l for l in log_lines()[mark:] if any(n in l for n in needles)]


def wait_phase(want: str, timeout: float = 90) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        h = api("/api/health")
        if h.get("phase") == want:
            return True
        time.sleep(1.0)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="R16 渡月桥思君.flac", help="点播用的音频(歌单/files_dir 里的名字)")
    ap.add_argument("--mode", default="managed")
    ap.add_argument("--gap", type=float, default=6.0, help="预期唱歌窗口间隔(与 config 一致)")
    ap.add_argument("--yes", action="store_true", help="跳过确认(会真实扣积分)")
    args = ap.parse_args()

    h = api("/api/health")
    if not h.get("ok"):
        print(f"桥不可达({BASE}): {h}")
        return 2
    print(f"桥 build={h.get('build')}  phase={h.get('phase')}")
    if h.get("phase") == "live":
        print("⚠ 桥已有进行中的会话, 先挂断再测")
        api("/api/live/stop", {})
        wait_phase("idle", 20)

    if not args.yes:
        print(f"⚠ 将创建真实会话并点播「{args.file}」, 约 60-90s(1 积分/秒)。回车继续, Ctrl+C 取消。")
        input()

    mark = len(log_lines())
    t_start = time.time()
    sent: list[str] = []

    # ── 建会话 ──
    r = api("/api/live/start", {"mode": args.mode, "action": {"enabled": True}})
    if not r.get("ok") and not r.get("live_id"):
        print(f"建会话失败: {json.dumps(r, ensure_ascii=False)[:300]}")
        return 2
    check("建会话成功(phase=live)", wait_phase("live", 60), json.dumps(r, ensure_ascii=False)[:200])

    try:
        # ── T1 非唱歌: 按当前配置断言(全局间隔 0 = 零闸门; >0 = 第 1 条即时/其余合并) ──
        print("\n== T1 非唱歌连发(按配置断言) ==")
        gap_all = float(((api("/api/settings").get("action") or {}).get("action_gap_s") or 0))
        print(f"      当前 action.action_gap_s = {gap_all:g}s")
        res = []
        for t in ("对镜头比心", "飞吻", "吃雪糕"):
            res.append(api("/api/cmd", {"text": t}))
            sent.append(t)
        if gap_all <= 0:
            check("3 连发全部 deferred=false(零闸门未变)",
                  all(x.get("ok") and x.get("deferred") is False for x in res),
                  json.dumps(res, ensure_ascii=False)[:220])
        else:
            check(f"第 1 条即时 + 后 2 条被合并(全局间隔 {gap_all:g}s)",
                  res[0].get("deferred") is False and all(x.get("deferred") is True for x in res[1:]),
                  json.dumps(res, ensure_ascii=False)[:220])
            time.sleep(gap_all + 1.0)      # 让待发的那条落地, 免得干扰后面的点播测试

        # ── 点播(唱歌窗口) ──
        print(f"\n== 点播「{args.file}」 ==")
        rs = api("/api/say/file", {"file_name": args.file})
        check("点播受理(返回秒数)", bool(rs.get("ok")), json.dumps(rs, ensure_ascii=False)[:200])
        song_on = False
        end = time.time() + 30
        while time.time() < end:
            if api("/api/health").get("song_playing"):
                song_on = True
                break
            time.sleep(0.5)
        check("进入唱歌窗口(song_playing=True)", song_on)
        # ⚠ 时序前提: 上面 T1 刚发过动作, 若立刻连点, 第一条也在间隔内(本就该合并)。
        #   先等满一个间隔, 才能测出"安静之后的第一下应当即时发出"。
        print(f"      (等 {args.gap + 1:.0f}s 让上一个动作的间隔走完, 再测'第 1 下即时')")
        time.sleep(args.gap + 1.0)
        check("等到间隔走完仍在唱歌窗口", bool(api("/api/health").get("song_playing")))

        # ── T2 唱歌窗口: 第 1 条即时 + 连点合并 ──
        print(f"\n== T2 唱歌中连点 4 下(间隔不足 {args.gap:.0f}s) ==")
        mark2 = len(log_lines())
        burst = ["歪头笑", "双马尾", "冲镜头挥手", "凑近眨眼比耶"]
        t0 = time.time()
        r1 = api("/api/cmd", {"text": burst[0]})
        sent.append(burst[0])
        rest = []
        for t in burst[1:]:
            rest.append(api("/api/cmd", {"text": t}))
            sent.append(t)
        print(f"      连点耗时 {time.time() - t0:.2f}s")
        check("第 1 条即时发出(deferred=false)", r1.get("deferred") is False, json.dumps(r1, ensure_ascii=False))
        check(f"紧跟的 {len(rest)} 条被合并(deferred=true)",
              all(x.get("deferred") is True for x in rest), json.dumps(rest, ensure_ascii=False)[:220])

        # ── T3 到期只补发最新一条 ──
        print(f"\n== T3 等 {args.gap + 2:.0f}s 看补发(应只发 {burst[-1]}) ==")
        time.sleep(args.gap + 2)
        merged = new_since(mark2, "[dmcmd] ⏳ 动作节流", "[dmcmd] ⏳ 节流后补发")
        for l in merged:
            print("      " + l.strip()[:150])
        check("节流日志出现(合并告警)", any("动作节流" in l for l in merged))
        check("只补发最新一条(凑近眨眼比耶)",
              any("节流后补发" in l and burst[-1] in l for l in merged),
              str([l.strip()[:80] for l in merged]))
        check("中间两条一次都没发出(无补发记录)",
              not any("节流后补发" in l and (burst[1] in l or burst[2] in l) for l in merged))

        # ── T4 特效一致性: dm_feed 只记真正发出的 ──
        print("\n== T4 指令流水(特效)只记真发出的动作 ==")
        feed = (api("/api/live/status").get("dm_feed") or {}).get("recent") or []
        # ⚠ dm_feed 是最近 24 条的滚动窗口, 里面有**上一场/上一次跑的旧条目** → 只算本轮
        manual_since = [f["text"] for f in feed
                        if f.get("source") == "manual" and float(f.get("ts") or 0) >= t_start]
        print(f"      manual fire = {manual_since}")
        expected = {"对镜头比心", "飞吻", "吃雪糕", burst[0], burst[-1]}   # 真正发出过的
        check("被合并的中间两条不在流水里(特效不谎报)",
              burst[1] not in manual_since and burst[2] not in manual_since, str(manual_since))
        check("真发出的两条在流水里",
              burst[0] in manual_since and burst[-1] in manual_since, str(manual_since))
        check("流水里没有「没真发却记了」的条目",
              set(manual_since) <= expected, str(sorted(set(manual_since) - expected)))

        # ── T5 停止演唱: 不应再自动补发 type7(2026-10-16 晚用户澄清该症状从未出现) ──
        print("\n== T5 停止演唱 → 不补发多余信号(症状不存在, 已撤掉自动复位) ==")
        mark3 = len(log_lines())
        api("/api/interrupt", {})
        time.sleep(3.0)
        rst = new_since(mark3, "🧹 歌结束复位")
        check("停止演唱后没有多余 type7(自动复位已撤掉)", not rst,
              str([l.strip()[:90] for l in rst]))
        check("停止演唱后 song_playing 归位", not api("/api/health").get("song_playing"))

        # T5b 停歌之后动作照常即时发出(非唱歌窗口 = 零闸门)
        r_after = api("/api/cmd", {"text": "托腮发呆"})
        check("停歌后动作照常即时发出", r_after.get("deferred") is False, json.dumps(r_after, ensure_ascii=False))

    finally:
        stop = api("/api/live/stop", {})
        ended = stop.get("ended") or {}
        print(f"\n== T6 挂断 ==\n      {json.dumps(ended, ensure_ascii=False)[:220]}")
        check("挂断成功并拿到计费", bool(ended.get("status") or ended.get("reason")),
              json.dumps(stop, ensure_ascii=False)[:200])

    print(f"\n本次共发动作 {len(sent)} 个: {' / '.join(sent)}")
    print(f"结果: {PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
