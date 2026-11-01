"""离线回归测试: **歌曲播放优先权最高 > 弹幕(复读/回答)**。

为什么要有这个测试: "点播歌被弹幕打断"这个问题反复出现(2026-10-09 三次线上事故),
根因都是喂流权/优先级的竞态, 靠直播碰运气验证太贵。这里用假 sig/tts/llm + 假喂流,
**不需要真实连线、不消耗 Vidu 额度**, 3 秒内跑完。

规则(用户拍板):
  ① 歌在播时, 弹幕复读与 LLM 回答一律排队, 绝不抢占;
  ② 歌播完(或手动停止演唱)后, 排队的最新弹幕自动接着播;
  ③ 点播新歌可以打断旧歌(用户主动切换);
  ④ 送礼观众(VIP)弹幕插队优先, 但**同样不能打断歌曲**。

跑法: .venv\\Scripts\\python.exe tools\\test_danmaku_priority.py
"""
import asyncio
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))          # 让 `src` 成为包, engine.py 里是相对导入

from src import engine as engmod  # noqa: E402

FEED_SECONDS = 3.0          # 假歌曲时长
PASS = []
FAIL = []


def check(name: str, ok: bool, detail: str = ""):
    (PASS if ok else FAIL).append(name)
    print(f"  {'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))


class FakeSig:
    def __init__(self):
        self.texts = []
        self.interrupts = 0

    async def send_audio(self, piece):
        return None

    async def send_output_text(self, t):
        self.texts.append(t)

    async def send_input_text(self, t):
        return None

    async def interrupt(self):
        self.interrupts += 1

    async def close(self):
        return None

    async def conn_init(self, **kw):
        return None

    def stats(self):
        return {}


class FakeTTS:
    async def synth_pcm(self, text, preset=None):
        return b"\x00" * 4800

    def active_base(self):
        return "fake"


class FakeLLM:
    def __init__(self):
        self.prompts = []          # 记录实际发给 LLM 的提示词(验证标签)

    async def stream(self, text):
        self.prompts.append(text)
        yield "好的"
        yield "知道了。"

    def trim_history(self, *a, **k):
        return None


def make_engine():
    eng = engmod.Engine()
    eng.phase = "live"
    eng.sig = FakeSig()
    eng.tts = FakeTTS()
    eng.llm = FakeLLM()
    eng.started_at = time.time()
    eng._dan_last_read = 0.0          # 20s 闸门立即放行(测试聚焦优先级)

    async def fake_feed(pcm, evt, events=None):
        """模拟实时喂流: FEED_SECONDS 秒, 被打断立即返回 False。"""
        steps = int(FEED_SECONDS / 0.1)
        for _ in range(steps):
            if evt.is_set() or eng.sig is None or eng.phase != "live":
                return False
            eng._feed_steps += 1
            await asyncio.sleep(0.1)
        return True

    eng._feed_pcm = fake_feed
    eng._feed_steps = 0
    return eng


def fast_feed():
    """极快喂流(0.2s 一条), 用于把"弹幕本身耗时"压到最小, 从而单独检验间隔闸门。"""
    async def _feed(pcm, evt, events=None):
        for _ in range(2):
            if evt.is_set():
                return False
            await asyncio.sleep(0.1)
        return True
    return _feed


async def stubborn_holder(eng, secs: float):
    """模拟"1 秒内死不掉"的旧发声者: 持有喂流权并忽略取消, secs 秒后才退出。
    线上触发过: 弹幕 item 卡在不可即时取消的 await 里时, 点歌会走进抢占分支。"""
    await eng._own_audio(preempt=True, wait_only=True)
    end = time.time() + secs
    try:
        while time.time() < end:
            try:
                await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                continue                      # 故意忽略取消
    finally:
        eng._release_audio()


def pick_audio() -> pathlib.Path:
    d = ROOT / "audio"
    for p in sorted(d.glob("*")):
        if p.suffix.lower() in (".wav", ".mp3", ".flac", ".m4a", ".ogg"):
            return p
    raise SystemExit(f"audio 目录里没有可用音频: {d}")


def spoke_danmaku(eng, keyword: str) -> bool:
    return any(keyword in (t.get("text") or "") for t in eng.transcripts)


async def _rejects(eng) -> bool:
    """chat() 在断线后必须抛 RuntimeError(而不是 AttributeError)。"""
    try:
        await eng.chat("断线后还能聊吗")
        return False
    except RuntimeError:
        return True
    except Exception:  # noqa: BLE001
        return False


async def main():
    audio_file = pick_audio()
    print(f"测试音频: {audio_file.name} · 假歌曲时长 {FEED_SECONDS}s\n")

    # ── 场景1: 歌在播 → 弹幕(普通)必须排队, 歌不被打断; 歌完自动接上 ──
    print("场景1: 歌曲播放中来的普通弹幕")
    eng = make_engine()
    await eng.say_file(audio_file.name)
    await asyncio.sleep(0.3)
    check("点播后立刻置位歌曲优先标记(_song_playing)", eng._song_playing is True)
    song_task = eng._speak_task
    r = eng.enqueue_danmaku("水友测试说你好呀")
    check("弹幕受理成功(queued)", bool(r.get("queued")), str(r))
    await asyncio.sleep(1.2)
    check("歌曲仍在播放(未被弹幕抢占)", bool(song_task and not song_task.done()))
    check("歌在播期间弹幕没有出声", not spoke_danmaku(eng, "水友测试说你好呀"))
    await asyncio.sleep(FEED_SECONDS - 1.0)
    for _ in range(40):                      # 最多等 4s 让弹幕接上
        if spoke_danmaku(eng, "水友测试说你好呀"):
            break
        await asyncio.sleep(0.1)
    check("歌播完后排队弹幕自动接上", spoke_danmaku(eng, "水友测试说你好呀"))
    await eng.finalize("test")

    # ── 场景2: VIP(送礼观众)弹幕也不能打断歌曲 ──
    print("\n场景2: 歌曲播放中来的 VIP 弹幕")
    eng = make_engine()
    await eng.say_file(audio_file.name)
    await asyncio.sleep(0.3)
    song_task = eng._speak_task
    r = eng.enqueue_danmaku("水友大哥说唱得真好", priority="vip")
    check("VIP 弹幕受理(reason=vip)", r.get("reason") == "vip", str(r))
    await asyncio.sleep(1.2)
    check("歌曲仍在播放(VIP 也没抢占)", bool(song_task and not song_task.done()))
    check("歌在播期间 VIP 也没出声", not spoke_danmaku(eng, "水友大哥说唱得真好"))
    await asyncio.sleep(FEED_SECONDS - 1.0)
    for _ in range(40):
        if spoke_danmaku(eng, "水友大哥说唱得真好"):
            break
        await asyncio.sleep(0.1)
    check("歌播完后 VIP 弹幕接上", spoke_danmaku(eng, "水友大哥说唱得真好"))
    await eng.finalize("test")

    # ── 场景3: 弹幕播报中, 用户点播新歌 → 歌必须赢(弹幕让位) ──
    print("\n场景3: 弹幕播报中用户点播歌")
    eng = make_engine()
    eng.enqueue_danmaku("水友测试说正在念我这条")
    for _ in range(30):                      # 等弹幕开始播
        if spoke_danmaku(eng, "水友测试说正在念我这条"):
            break
        await asyncio.sleep(0.1)
    check("弹幕已开始播报", spoke_danmaku(eng, "水友测试说正在念我这条"))
    await eng.say_file(audio_file.name)
    await asyncio.sleep(0.8)
    check("点播后歌曲拿到喂流权(弹幕被让位)", eng._song_playing is True and eng.speaking_text.startswith("[文件]"))
    await eng.finalize("test")

    # ── 场景4: 手动「停止演唱」→ 歌立即停, 排队弹幕接上 ──
    print("\n场景4: 播放中手动停止演唱")
    eng = make_engine()
    await eng.say_file(audio_file.name)
    await asyncio.sleep(0.3)
    eng.enqueue_danmaku("水友测试说停歌以后我要被念")
    await eng.interrupt()                    # 等价于页面「⏹ 停止演唱」
    await asyncio.sleep(0.5)
    check("停止演唱后歌曲标记清除", eng._song_playing is False)
    for _ in range(50):
        if spoke_danmaku(eng, "水友测试说停歌以后我要被念"):
            break
        await asyncio.sleep(0.1)
    check("停歌后排队弹幕立即接上", spoke_danmaku(eng, "水友测试说停歌以后我要被念"))
    await eng.finalize("test")

    # ── 场景5: 打字聊天也不能掐歌(歌曲 > 打字聊天), 歌完后再回话 ──
    print("\n场景5: 歌曲播放中用户打字聊天")
    eng = make_engine()
    await eng.say_file(audio_file.name)
    await asyncio.sleep(0.3)
    song_task = eng._speak_task
    await eng.chat("你唱得真好听")            # 打字聊天
    await asyncio.sleep(1.2)
    check("打字聊天没有掐掉歌曲", bool(song_task and not song_task.done()))
    check("歌在播时聊天回复没出声", not any("好" == (t.get("text") or "").strip() for t in eng.transcripts
                                            if t.get("role") == "dh" and not t.get("text", "").startswith("【")))
    await asyncio.sleep(FEED_SECONDS - 1.0)
    got_reply = False
    for _ in range(60):
        if len(eng.sig.texts) > 0:           # 聊天回复逐句 send_output_text
            got_reply = True
            break
        await asyncio.sleep(0.1)
    check("歌播完后聊天回复念出来", got_reply)
    await eng.finalize("test")

    # ── 场景6: 打字聊天 优先于 排队中的弹幕 ──
    print("\n场景6: 弹幕播报中用户打字聊天 → 聊天抢占弹幕")
    eng = make_engine()
    eng.enqueue_danmaku("水友测试说这条弹幕应该被聊天打断")
    for _ in range(30):
        if spoke_danmaku(eng, "水友测试说这条弹幕应该被聊天打断"):
            break
        await asyncio.sleep(0.1)
    check("弹幕已开始播报", spoke_danmaku(eng, "水友测试说这条弹幕应该被聊天打断"))
    dan_item = eng._speak_task                # 正在播报的弹幕 item 任务
    await eng.chat("先回答我的话")
    await asyncio.sleep(0.6)
    check("弹幕 item 被聊天抢占(已结束)", bool(dan_item and dan_item.done()))
    for _ in range(200):                      # 最多 20s: 等聊天整条说完(逐句喂流很慢)
        if not eng._chat_busy and any("好的" in t for t in eng.sig.texts):
            break
        await asyncio.sleep(0.1)
    check("打字聊天的回复被念出(抢占了弹幕)",
          any("好的" in t for t in eng.sig.texts), f"texts={eng.sig.texts[:3]}")
    await eng.finalize("test")

    # ── 场景7: 打字聊天播报中来的弹幕, 必须等聊天说完才能播(聊天不被打断) ──
    print("\n场景7: 打字聊天播报中来的弹幕")
    eng = make_engine()
    await eng.chat("这句话必须完整说完")
    for _ in range(30):                      # 等聊天开始出声
        if len(eng.sig.texts) > 0:
            break
        await asyncio.sleep(0.1)
    check("聊天回复已开始播报", len(eng.sig.texts) > 0, f"texts={eng.sig.texts[:1]}")
    chat_task = eng._speak_task
    r = eng.enqueue_danmaku("水友测试说我要等聊天说完")
    check("弹幕受理", bool(r.get("queued")), str(r))
    await asyncio.sleep(1.5)
    check("聊天仍在播报(没被弹幕打断)", bool(chat_task and not chat_task.done()) and eng._chat_busy is True)
    check("聊天期间弹幕没有出声", not spoke_danmaku(eng, "水友测试说我要等聊天说完"))
    for _ in range(120):                     # 聊天说完整(2句×3s)+ 弹幕接上
        if spoke_danmaku(eng, "水友测试说我要等聊天说完"):
            break
        await asyncio.sleep(0.1)
    check("聊天完整说完后弹幕才接上", spoke_danmaku(eng, "水友测试说我要等聊天说完"))
    await eng.finalize("test")

    # ── 场景8: 旧发声者"拖 1.2 秒才死"时点歌 → 歌必须真的唱起来(不得自杀) ──
    # 线上事故复现: 弹幕 item 卡住时点歌 → 抢权分支 set 了歌自己的 _cur_evt
    # → 歌喂流第一行就 evt_set → 秒死, 表现为"点歌放不出来/被弹幕打断"。
    print("\n场景8: 旧发声者拖延退出时点歌")
    eng = make_engine()
    holder = asyncio.create_task(stubborn_holder(eng, 1.2))
    await asyncio.sleep(0.3)
    check("旧发声者已持有喂流权", eng._audio_owner is not None)
    await eng.say_file(audio_file.name)
    await asyncio.sleep(1.8)
    check("歌仍在播放(没被自己的中断标记掐死)", eng._song_playing is True,
          f"song_playing={eng._song_playing} speaking={eng.speaking_text!r} steps={eng._feed_steps}")
    for _ in range(60):                          # 等歌把 FEED_SECONDS 喂完
        if not eng._song_playing:
            break
        await asyncio.sleep(0.1)
    check("歌曲完整播完(喂流步数≈30)", eng._feed_steps >= 25, f"steps={eng._feed_steps}")
    if not holder.done():
        holder.cancel()
    await eng.finalize("test")

    # ── 场景9: VIP 弹幕优先插队, 但同样遵守最小间隔(不能连播刷屏) ──
    print("\n场景9: VIP 弹幕遵守最小间隔")
    eng = make_engine()
    eng._feed_pcm = fast_feed()                  # 弹幕本身很快, 单独检验间隔闸门
    orig_cfg = eng._dan_cfg
    eng._dan_cfg = lambda k, d: 1500 if k == "vip_min_interval_ms" else orig_cfg(k, d)
    t0 = time.time()
    first = second = None
    eng.enqueue_danmaku("水友大哥说VIP第一条", priority="vip")
    while time.time() - t0 < 5:
        if spoke_danmaku(eng, "VIP第一条"):
            first = time.time() - t0
            break
        await asyncio.sleep(0.05)
    check("VIP 第一条立刻播报(优先)", first is not None and first < 1.0,
          f"t={first:.2f}s" if first else "")
    eng.enqueue_danmaku("水友大哥说VIP第二条", priority="vip")
    await asyncio.sleep(0.8)
    check("VIP 第二条没有立刻接上(守最小间隔)", not spoke_danmaku(eng, "VIP第二条"))
    while time.time() - t0 < 8:
        if spoke_danmaku(eng, "VIP第二条"):
            second = time.time() - t0
            break
        await asyncio.sleep(0.05)
    check("VIP 第二条随后正常播出", second is not None)
    if first and second:
        check("两条 VIP 间隔 ≥ 1.5s", second - first >= 1.4, f"间隔={second - first:.2f}s")
    await eng.finalize("test")

    # ── 场景10: 只答不念(answer_only): 不复读弹幕, 但回答要念出来 + 提示词带标签 ──
    print("\n场景10: 只答不念模式(托盘关闭「念弹幕」)")
    eng = make_engine()
    r = eng.enqueue_danmaku("阿糖说今天唱什么", answer_only=True, nick="阿糖",
                            content="今天唱什么", platform="抖音")
    check("受理并标记 answer_only", r.get("answer_only") is True and r.get("queued"), str(r))
    spoke_repeat = False
    for _ in range(60):
        if any("【弹幕" in (t.get("text") or "") and t.get("role") == "dh" for t in eng.transcripts):
            spoke_repeat = True
            break
        if eng.llm.prompts:
            break
        await asyncio.sleep(0.1)
    check("弹幕内容没有被复读(dh 字幕里没有它)",
          not any("阿糖说今天唱什么" == (t.get("text") or "").strip() for t in eng.transcripts
                  if t.get("role") == "dh"))
    check("字幕区仍记录这条弹幕(标·未念)",
          any("【弹幕·未念】" in (t.get("text") or "") for t in eng.transcripts))
    for _ in range(80):
        if eng.sig.texts:
            break
        await asyncio.sleep(0.1)
    check("LLM 回答被念出来", len(eng.sig.texts) > 0, f"texts={eng.sig.texts[:2]}")
    check("LLM 回答文本进了本地字幕流(dh) —— 不写的话面板/画面层就看不到回答",
          any(t.get("role") == "dh" and "好的" in (t.get("text") or "") for t in eng.transcripts),
          str([t.get("text") for t in eng.transcripts if t.get("role") == "dh"]))
    check("提示词带「直播间弹幕·抖音」标签", eng.llm.prompts and "直播间弹幕·抖音" in eng.llm.prompts[0],
          (eng.llm.prompts[0][:60] if eng.llm.prompts else ""))
    check("提示词带观众名", eng.llm.prompts and "阿糖" in eng.llm.prompts[0])
    check("提示词要求不要复述原话", eng.llm.prompts and "不要复述" in eng.llm.prompts[0])
    await eng.finalize("test")

    # ── 场景11: 只答不念也要让位于歌曲(优先级不变) ──
    print("\n场景11: 只答不念 + 歌曲播放中")
    eng = make_engine()
    await eng.say_file(audio_file.name)
    await asyncio.sleep(0.3)
    song_task = eng._speak_task
    eng.enqueue_danmaku("阿糖说唱一个", answer_only=True, nick="阿糖", content="唱一个",
                        platform="抖音")
    await asyncio.sleep(1.2)
    check("歌曲没被打断", bool(song_task and not song_task.done()))
    check("歌在播时没有发起 LLM 请求", not eng.llm.prompts)
    await asyncio.sleep(FEED_SECONDS - 1.0)
    for _ in range(80):
        if eng.llm.prompts:
            break
        await asyncio.sleep(0.1)
    check("歌播完之后才去问 LLM", bool(eng.llm.prompts), f"prompts={len(eng.llm.prompts)}")
    await eng.finalize("test")

    # ── 场景12: 说话途中"断流"(sig 被清 None) 不得再崩 ──
    # 线上事故复现(2026-10-10): 用户边唱歌边打字聊几个回合后, 面板出现
    #   "喂流异常: 'NoneType' object has no attribute 'send_output_text'"
    # 根因: 挂断(finalize/start)会把 engine.sig 置 None, 而喂流/字幕上报是
    # **多个 await 之间**才检查一次 → 缝里挂断就 AttributeError, 整条回复崩掉。
    # 这里在"正念着的时候"把 sig 拔掉, 断言: 不崩、写的是可读的提示、不再调 sig。
    print("\n场景12: 说话途中断流(sig=None)不崩")
    eng = make_engine()
    eng._feed_pcm = fast_feed()                 # 让回复在几拍内就念上, 方便中途拔线
    await eng.chat("这句话说到一半会断线")
    for _ in range(30):                          # 等它开始念
        if eng.sig.texts:
            break
        await asyncio.sleep(0.1)
    check("断线前已开始念", len(eng.sig.texts) > 0, f"texts={eng.sig.texts[:1]}")
    dead = eng.sig                               # 留引用用于断言"确实不再碰它"
    sent_before = len(dead.texts)
    eng.sig = None                               # ← 模拟挂断清理
    await asyncio.sleep(1.2)
    check("断线后喂流自动收尾(没有 NoneType 崩溃)",
          not any("NoneType" in (t.get("text") or "") for t in eng.transcripts),
          str([t.get("text") for t in eng.transcripts if t.get("role") == "sys"]))
    check("没有出现「喂流异常」", not any("喂流异常" in (t.get("text") or "") for t in eng.transcripts))
    check("断线后不再向 WS 发字幕", len(dead.texts) == sent_before,
          f"断线前={sent_before} 断线后={len(dead.texts)}")
    check("_out_text 在无连线时安全返回 False", await eng._out_text("断线后的字幕") is False)
    check("_ws_ok 在 sig=None 时为 False", eng._ws_ok() is False)
    check("断线后 chat() 直接拒绝(不再抛 AttributeError)", 
          (await _rejects(eng)) is True)
    await eng.finalize("test")

    # ── 场景13: 断线判据统一(selfcheck 之外: 各入口都要抛"没有进行中的会话") ──
    print("\n场景13: 断线后各入口的报错统一")
    eng = make_engine()
    eng.sig = None
    for name, coro in (("say_text", eng.say_text("喂")), ("say_file", eng.say_file("x.wav")),
                       ("chat", eng.chat("在吗"))):
        try:
            await coro
            check(f"{name} 断线时应当拒绝", False)
        except RuntimeError as e:
            check(f"{name} 断线时拒绝: {e}", "没有进行中的会话" in str(e))
    r = eng.enqueue_danmaku("断线时来的弹幕")
    check("断线时弹幕返回 idle(不抛异常)", r.get("reason") == "idle", str(r))

    print(f"\n结果: {len(PASS)} 通过 / {len(FAIL)} 失败")
    if FAIL:
        print("失败项: " + ", ".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
