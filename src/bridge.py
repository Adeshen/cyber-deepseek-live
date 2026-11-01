"""FastAPI 桥: 舞台页 + 会话/聊天/点播/设置/动作库 路由。密钥只在本进程, 不下浏览器。

端点一览:
  页面    GET  /stage                     舞台页
  会话    POST /api/live/start            创建组件版会话 + conn_init(可带覆盖参数)
          GET  /api/live/status?since=N   状态 + 增量字幕
          POST /api/live/stop             挂断
          POST /api/interrupt             打断(type7)
  对话    POST /api/chat {content}        用户话 → LLM → TTS → 喂流(自动打断上一轮)
          POST /api/llm/reset             清对话记忆
  开口    POST /api/say {text}            直接开口(TTS)
          POST /api/say/file {file_name}  点播音频目录里的文件
          GET  /api/tts/files             音频目录列表
          POST /api/tts/upload            上传音频文件到点播目录(multipart)
  声音    GET  /api/tts/voices            音色预设列表
          POST /api/tts/voice {name}      热切音色(立即生效, 持久化)
          POST /api/tts/preview {text}    试听(返回 wav, 不进会话)
  设置    GET/POST /api/settings          读取(脱敏)/深合并更新 config.json
          POST /api/settings/reload       外部手改 config.json 后强制重读
  动作库  GET/POST /api/actions/libs      列表/新建; .../actions 列表/新增
  其他    GET  /api/credits               积分余额
"""
from __future__ import annotations

import asyncio
import base64
import pathlib
import re
import shutil
import time

_FILE_RE = re.compile(r"^[^\\/]+$")          # 点播文件名: 只允许纯文件名(防目录穿越)

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from . import config as appconf
from . import logging_setup
from . import dm_router
from .danmaku_source import DanmakuSource
from .engine import Engine
from .tts import audio
from .vidu import client as vu

# 日志落盘(必须在 engine 实例化之前): 事故现场(完整堆栈/断线原因)不再只留在那个
# 最小化的控制台窗口里 → logs/bridge.log, 用 tools/watch-bridge-log.ps1 跟屏看。
LOG_PATH = logging_setup.attach()

ROOT = pathlib.Path(__file__).resolve().parent.parent
STAGE = ROOT / "stage"

app = FastAPI(title="vidu-dh-s2-component")
engine = Engine()
# 动作**真正发出**时的回调(2026-10-16): 手动直发登记改由 engine 回调触发, 而不是
# 在 /api/cmd 里"受理即登记" —— 唱歌窗口内连点会被节流合并, 特效高亮/日志必须反映
# 真正发给 Vidu 的那一条(否则画面亮 10 次、她只做 3 个)。
engine.on_action_fire = dm_router.note_manual

# ── 弹幕→指令 自动触发路由(Stage B): T0本地匹配 / T2编译器 / 确认条 / Top10 ──
async def _fetch_action_names() -> list[str]:
    """dm_router 的动态词表来源: 连线所带动作库(默认自建库)的全部动作名。"""
    _configure_vu()
    libs = await vu.list_action_libraries()
    items = libs.get("items") or []
    lib_id = str((appconf.load().get("action") or {}).get("library_id") or "")
    lib = next((l for l in items if str(l.get("id")) == lib_id), None) or (items[0] if items else {})
    if not lib:
        return []
    acts = await vu.list_actions(str(lib.get("id")))
    return [a.get("name") for a in (acts.get("items") or []) if a.get("name")]

dm_router.init(engine, fetch_names=_fetch_action_names,
               conf_get=lambda: (appconf.load().get("danmaku_cmd") or {}))

@app.on_event("startup")
async def _dmcmd_startup():
    await dm_router.start()
    await danmaku_source.start()
    asyncio.create_task(_tts_cache_gc_loop())


@app.on_event("shutdown")
async def _source_shutdown():
    await danmaku_source.stop()


async def _tts_cache_gc_loop():
    """gradio 客户端缓存兜底清理(详见 omnivoice.sweep_client_cache)。
    启动即清一次, 之后每 30 分钟一次。为什么不能只靠合成后的逐个删:
    `%TEMP%\\gradio` 与 另一个本地语音桥(8765, 可选)**共用同一个目录**,
    它下载的 wav 我们没机会删 → 这里按年龄兜底(>2 小时)。开销: 每 30 分钟一次目录遍历。"""
    from .tts import omnivoice as _ov
    while True:
        try:
            n = await asyncio.to_thread(_ov.sweep_client_cache, 7200)
            if n:
                print(f"[tts] 清理 gradio 客户端缓存: {n} 个超过 2 小时的旧文件", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[tts] 缓存清理异常(忽略): {type(e).__name__}: {e}", flush=True)
        await asyncio.sleep(1800)

print(f"[bridge] BUILD = {engine.BUILD} (进程加载的代码版本; 改代码后必须重启桥才生效)", flush=True)
if LOG_PATH:
    print(f"[bridge] 日志落盘 = {LOG_PATH}", flush=True)


def _configure_vu() -> None:
    """把当前配置(含切换后的 key)注入 Vidu 客户端 —— 每次打平台接口前都调,
    空闲态也能查资产/积分, 且 key 切换立即对查询类接口生效。"""
    vu.configure(appconf.load().get("vidu") or {})

NO_CACHE = {"Cache-Control": "no-cache, no-store, must-revalidate"}


def err(status: int, msg: str) -> HTTPException:
    return HTTPException(status, msg)


# ───────────────────────── 页面 ─────────────────────────

@app.get("/")
async def index():
    return RedirectResponse("/stage")


@app.get("/stage")
async def stage():
    return FileResponse(STAGE / "index.html", headers=NO_CACHE)


@app.get("/api/health")
async def health():
    """build = 当前进程加载的代码版本 —— 确认"改动是否真的生效/要不要重启桥"。
    末尾四项是本场"断流/断续"取证(2026-10-10): 一次断流到底发生在哪一步。"""
    sig = engine.sig
    return {"ok": True, "ts": time.time(), "phase": engine.phase,
            "build": engine.BUILD,
            "tts_engine": engine._tts_engine,
            "tts_base": engine.tts.active_base(),
            "song_playing": engine._song_playing,
            "chat_busy": engine._chat_busy,
            "danmaku_pending": len(engine._dan_vipq) + (1 if engine._dan_latest else 0),
            "danmaku_source": danmaku_source.status(),
            "audio_owner": "busy" if (engine._audio_owner and not engine._audio_owner.done()) else "free",
            "feed_lag_n": engine._feed_lag_n,
            "ended": engine.ended,
            "ws": (sig.stats() if sig else None),
            "close_info": (getattr(sig, "close_info", {}) or None) if sig else None,
            "log": str(LOG_PATH) if LOG_PATH else None}


# ───────────────────────── 会话 ─────────────────────────

class StartBody(BaseModel):
    model: str | None = None
    avatar_id: str | None = None
    image_uri: str | None = None
    mode: str | None = None            # managed | customer
    managed_provider: str | None = None
    customer_provider: str | None = None
    channel_id: str | None = None


@app.post("/api/live/start")
async def live_start(body: StartBody | None = None):
    try:
        return await engine.start((body.model_dump(exclude_none=True) if body else {}))
    except RuntimeError as e:
        engine.tr_add("sys", f"启动失败: {e}")
        raise err(502, str(e)) from e


@app.get("/api/live/status")
async def live_status(since: int = -1):
    st = engine.status(since)
    st["danmaku_source"] = danmaku_source.status()
    # 指令流水(2026-10-14): 舞台页画面中间偏右的"动作指令"特效用 —— 从弹幕里精简出的
    # 指令滚动展示, 最近真正执行的那条高亮放大 + 小礼花。只读, 不影响任何主链路。
    try:
        st["dm_feed"] = dm_router.cmd_feed()
    except Exception:                                # noqa: BLE001
        st["dm_feed"] = {"last": {}, "recent": []}
    return st


@app.post("/api/live/stop")
async def live_stop():
    return await engine.stop("user_end")


# ───────────────────────── 服务端录播(2026-10-23) ─────────────────────────

class RecordBody(BaseModel):
    enabled: bool


@app.get("/api/record")
async def record_get():
    """开关状态 + 当前场次的录播进度。开关取 config(下次连线生效), 进度取引擎实时状态。"""
    conf = appconf.load().get("rtc") or {}
    return {"enabled": bool(conf.get("managed_recording")),
            "record_dir": conf.get("record_dir") or "",
            "state": dict(engine.record)}


@app.post("/api/record")
async def record_set(body: RecordBody):
    """开/关服务端录播。⚠ 建会话时才生效 —— 直播中切换要下一场才起作用。"""
    appconf.patch({"rtc": {"managed_recording": bool(body.enabled)}})
    conf = appconf.load().get("rtc") or {}
    return {"ok": True, "enabled": bool(conf.get("managed_recording")),
            "note": "已保存；本场已连线的话，下一场生效"}


class RecordFetchBody(BaseModel):
    live_id: str = ""


@app.post("/api/record/fetch")
async def record_fetch(body: RecordFetchBody | None = None):
    """手动再拉一次录播(自动等待超时/漏掉时用)。不传 live_id 就用最近一场。"""
    lid = (body.live_id if body else "") or ""
    return await engine.fetch_recording(lid)


@app.post("/api/interrupt")
async def interrupt():
    return await engine.interrupt()


# ───────────────────────── 对话 ─────────────────────────

class ChatBody(BaseModel):
    content: str


@app.post("/api/chat")
async def chat(body: ChatBody):
    try:
        return await engine.chat(body.content)
    except RuntimeError as e:
        raise err(409, str(e)) from e


class CmdBody(BaseModel):
    text: str
    silent: bool = False      # 随机表演(2026-10-17): 不写字幕流/不亮特效/不计 Top10


@app.post("/api/cmd")
async def vidu_cmd(body: CmdBody):
    """快捷指令直通 Vidu(type9): 不经 LLM/TTS/队列, 她默默做动作。
    ⭐ 手动路径不受任何时间闸限制(用户 2026-10-13 拍板)。
    ⭐ 2026-10-16: 唱歌窗口内由 engine 侧做"最小间隔 + 最新优先合并"(防 Vidu 队列积压
       → 症状"卡在一个动作一直做"); 返回值带 `deferred=True` 表示这条被合并/排队了。
       手动登记(note_manual: 特效高亮 + 日志)已改为 engine 的 on_action_fire 回调,
       在**真正发出**那一刻触发(见文件顶部 `engine.on_action_fire = ...`)。
    ⭐ 2026-10-17: `silent=True`(随机表演) → source="random": 不写字幕流、不回调特效、
       不计 Top10 —— 观众不该在画面上看到一串随机动作名。"""
    try:
        if body.silent:
            return await engine.cmd(body.text, source="random")
        r = await engine.cmd(body.text, source="manual")
        return r
    except RuntimeError as e:
        raise err(409, str(e)) from e


class DmToggleBody(BaseModel):
    enabled: bool | None = None
    mode: str | None = None


@app.post("/api/dmcmd/toggle")
async def dmcmd_toggle(body: DmToggleBody):
    """自动匹配开关/模式(确认|auto)。关=恢复纯手点(2026-10-12 用户拍板)。"""
    return {"ok": True, "state": dm_router.set_config(enabled=body.enabled, mode=body.mode)}


@app.get("/api/dmcmd/state")
async def dmcmd_state():
    """开关状态 + Top10(近1天) + 待确认列表(确认条轮询这个)。"""
    return dm_router.state()


class DmIdBody(BaseModel):
    id: int


@app.post("/api/dmcmd/confirm")
async def dmcmd_confirm(body: DmIdBody):
    ok = dm_router.confirm(body.id)
    if not ok:
        raise err(404, "待确认项不存在或已过期")
    return {"ok": True}


@app.post("/api/dmcmd/dismiss")
async def dmcmd_dismiss(body: DmIdBody):
    return {"ok": dm_router.dismiss(body.id)}


@app.post("/api/llm/reset")
async def llm_reset():
    engine.llm.reset_history()
    return {"ok": True}


# ───────────────────────── 直接开口 / 点播 ─────────────────────────

class SayBody(BaseModel):
    text: str


@app.post("/api/say")
async def say(body: SayBody):
    try:
        return await engine.say_text(body.text)
    except RuntimeError as e:
        raise err(409, str(e)) from e


class SayFileBody(BaseModel):
    file_name: str
    script: str = ""


@app.post("/api/say/file")
async def say_file(body: SayFileBody):
    try:
        return await engine.say_file(body.file_name, body.script)
    except RuntimeError as e:
        raise err(409, str(e)) from e


def _files_dir() -> pathlib.Path:
    d = pathlib.Path((appconf.load().get("tts") or {}).get(
        "files_dir", str(ROOT / "audio")))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _song_dirs() -> list[pathlib.Path]:
    return [pathlib.Path(d) for d in ((appconf.load().get("tts") or {}).get("song_dirs") or [])
            if d]


@app.get("/api/tts/files")
async def tts_files():
    """files_dir + song_dirs 全集; song_dirs 的文件带 lib 标记(只读歌单, 上传/删除仍限 files_dir)。"""
    d = _files_dir()
    items = []
    seen = set()
    for f in sorted(d.iterdir()):
        if f.is_file() and f.suffix.lower() in _AUDIO_EXTS:
            items.append({"name": f.name, "size": f.stat().st_size,
                          "mtime": int(f.stat().st_mtime), "lib": "main"})
            seen.add(f.name)
    for sd in _song_dirs():
        if not sd.is_dir():
            continue
        for f in sorted(sd.iterdir()):
            if f.is_file() and f.suffix.lower() in _AUDIO_EXTS and f.name not in seen:
                items.append({"name": f.name, "size": f.stat().st_size,
                              "mtime": int(f.stat().st_mtime), "lib": sd.name})
                seen.add(f.name)
    return {"dir": str(d), "files": items}


_AUDIO_EXTS = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".opus", ".webm"}


def _clean_upload_name(raw: str) -> str:
    """尽量还原真实文件名; 处理四种形态:
    1. 浏览器 FormData 正常 UTF-8 文件名(直接可用)
    2. RFC 2047 编词(PowerShell 5.1 -Form 实测形态): =?utf-8?B?<base64>?=
    3. RFC5987: filename*=utf-8''%E7%82%B9...
    4. __utf-8_B_<base64>__ 包装(部分客户端变体, base64 里 / 和 = 被换成 _)
    还原不了就退回时间戳名, 保证永远是合法音频文件名。"""
    import base64 as _b64
    import urllib.parse as _up
    name = (raw or "").strip()
    m = re.match(r"=\?([\w-]+)\?[Bb]\?([^?]*)\?=$", name)
    if m:
        try:
            dec = _b64.b64decode(m.group(2)).decode(m.group(1).lower(), "replace")
            if dec and dec.isprintable():
                name = dec
        except Exception:
            pass
    m = re.search(r"utf-8''([^;\s]+)", name, re.I)
    if m:
        name = _up.unquote(m.group(1))
    else:
        m = re.match(r"__utf-8_B_(.+?)_{0,4}$", name)
        if m:
            inner = m.group(1)
            for cand in (inner, inner.replace("_", "/")):
                try:
                    pad = cand + "=" * (-len(cand) % 4)
                    dec = _b64.b64decode(pad).decode("utf-8")
                    if dec and dec.isprintable() and not dec.startswith("__utf-8"):
                        name = dec
                        break
                except Exception:
                    continue
    name = re.sub(r"[^\w.\-\u4e00-\u9fff]", "_", pathlib.Path(name).name).strip("_")
    if not name:
        name = f"clip-{int(time.time())}"
    if pathlib.Path(name).suffix.lower() not in _AUDIO_EXTS:
        name += ".wav"           # 扩展名丢了就补; ffmpeg 按 content 解码, 不看扩展名
    return name


@app.post("/api/tts/upload")
async def tts_upload(files: list[UploadFile] = File(...)):
    """批量上传: 浏览器 <input multiple>/webkitdirectory 全部塞进 files 字段。"""
    results = []
    for uf in files:
        name = _clean_upload_name(uf.filename or "")
        data = await uf.read()
        if len(data) > 200 * 1024 * 1024:
            results.append({"file_name": name, "ok": False, "error": "文件过大(>200MB)"})
            continue
        (_files_dir() / name).write_bytes(data)
        results.append({"file_name": name, "ok": True, "size": len(data)})
    return {"ok": True, "count": sum(1 for r in results if r["ok"]), "results": results}


class ImportFolderBody(BaseModel):
    path: str


@app.post("/api/tts/import-folder")
async def tts_import_folder(body: ImportFolderBody):
    """把本机一个文件夹里的歌批量拷进点播目录(递归找音频, 平铺进来, 重名自动加序号)。"""
    src = pathlib.Path(body.path.strip().strip('"')).expanduser()
    if not src.is_dir():
        raise err(400, f"文件夹不存在: {src}")
    dest_dir = _files_dir()
    found: list[pathlib.Path] = []
    for f in src.rglob("*"):
        if f.is_file() and f.suffix.lower() in _AUDIO_EXTS and not f.name.startswith("."):
            found.append(f)
    if not found:
        raise err(400, "该文件夹(含子文件夹)里没有音频文件")

    def do_copy() -> dict:
        imported = skipped = failed = 0
        errs: list[str] = []
        for f in found:
            try:
                dest = dest_dir / f.name
                n = 2
                stem, ext = f.stem, f.suffix.lower()
                while dest.exists() and dest.stat().st_size != f.stat().st_size:
                    dest = dest_dir / f"{stem} ({n}){ext}"   # 同名不同文件 → 加序号, 同名同大小 → 视为重复跳过
                    n += 1
                    if n > 99:
                        break
                if dest.exists() and dest.stat().st_size == f.stat().st_size:
                    skipped += 1
                    continue
                shutil.copy2(f, dest)
                imported += 1
            except Exception as e:  # noqa: BLE001
                failed += 1
                if len(errs) < 5:
                    errs.append(f"{f.name}: {str(e)[:80]}")
        return {"ok": True, "imported": imported, "skipped": skipped,
                "failed": failed, "errors": errs, "dir": str(dest_dir)}

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, do_copy)


class DanmakuBody(BaseModel):
    text: str
    priority: str = "normal"        # "vip" = 送礼观众的弹幕(优先取件, 守 VIP 间隔)
    answer_only: bool = False       # True = 不复读弹幕, 只把 LLM 回答喂给数字人(托盘关"念弹幕"时)
    nick: str = ""                  # 观众名/内容/平台: 给 LLM 打的标签(2026-10-10)
    content: str = ""
    platform: str = ""
    event_id: str = ""
    user_id: str = ""
    room_id: str = ""
    event_ts: int = 0
    # ---- 礼物合并感谢(2026-10-19): 采集器送礼时带 kind=gift + 结构化字段, 桥侧攒窗口 ----
    kind: str = ""                  # "" 普通弹幕 / "gift" 礼物 / "follow" 新关注
    gift_item: str = ""             # 礼物名(小星星/入团卡/跑车…)
    gift_num: int = 1               # 数量
    gift_value: int = 0             # 折算钻(采集器算好)
    gift_group_id: str = ""
    gift_total: int = 0
    say_nick: str = ""              # 采集器已经缩写好的念法(桥侧兜底再缩一次)


@app.post("/api/danmaku")
async def danmaku(body: DanmakuBody):
    """弹幕采集器转发口: 数字人主播模式。
    · Stage B 弹幕→指令: 入指令队列(最新优先, 默认 5s 一跳, 2026-10-13 用户拍板节流),
      worker 取最新一条跑 T0/T2 → 命中静默 type9 / 疑似进确认条; 其余照旧复读+LLM。
    · 默认: 复读弹幕 → 再把 LLM 完整回答喂给数字人
    · answer_only=true: 不复读, 只喂 LLM 回答(标签带上观众名/平台, 互动感更好)
    永远返回 200, 结果在字段里(采集器可无脑转发):
    ok/queued/reason=idle|empty|filtered|latest|vip。"""
    content = body.content or body.text
    # 2026-10-19 礼物合并感谢: 采集器送礼时带 kind=gift → 进窗口, 不走走弹幕播报那条路
    if (body.kind or "").strip().lower() in ("gift", "gifts"):
        if not engine.note_gift_event({"nick": body.nick, "gift_item": body.gift_item,
                                       "gift_num": body.gift_num, "gift_total": body.gift_total,
                                       "gift_group_id": body.gift_group_id, "event_id": body.event_id,
                                       "user_id": body.user_id, "room_id": body.room_id}):
            return {"ok": True, "queued": False, "reason": "duplicate"}
        return await engine.enqueue_gift({"nick": body.nick, "say_nick": body.say_nick,
                                          "item": body.gift_item, "num": body.gift_num,
                                          "value": body.gift_value, "platform": body.platform})
    if (body.kind or "").strip().lower() == "follow":
        result = engine.enqueue_danmaku(body.text, "vip", answer_only=True,
                                        nick=body.nick, content=body.content, platform=body.platform,
                                        event_id=body.event_id, user_id=body.user_id,
                                        room_id=body.room_id, event_ts=body.event_ts, event_kind="follow")
        if result.get("queued"):
            engine.tr_add("sys", f"【关注】{body.nick or '观众'} 关注了直播间")
        return result
    # 2026-10-19 用户拍板「动作类弹幕不念、其他照旧」: 命中动作指令的弹幕不再复读原话
    # (观众点的动作, 念出来像在念参数), 但仍照常让 LLM 接话 → 复用 answer_only 这条老通道。
    silent_read = False
    try:
        silent_read = dm_router.looks_like_cmd(content)
    except Exception:                            # noqa: BLE001
        silent_read = False
    result = engine.enqueue_danmaku(body.text, body.priority,
                                   answer_only=bool(body.answer_only or silent_read),
                                   nick=body.nick, content=body.content, platform=body.platform,
                                   event_id=body.event_id, user_id=body.user_id,
                                   room_id=body.room_id, event_ts=body.event_ts)
    if result.get("queued"):
        try:
            dm_router.enqueue(body.nick, content)
        except Exception:
            pass
    return result


async def _dispatch_source_danmaku(payload: dict) -> dict:
    # Idle reception must not trigger the action classifier or queue stale events.
    if not engine._ws_ok():
        return {"ok": False, "queued": False, "reason": "idle"}
    return await danmaku(DanmakuBody(**payload))


danmaku_source = DanmakuSource(
    lambda: appconf.load().get("danmaku_source") or {},
    _dispatch_source_danmaku,
)


@app.get("/api/danmaku/source")
async def danmaku_source_status():
    return danmaku_source.status()


class DeleteFileBody(BaseModel):
    file_name: str


@app.post("/api/tts/delete")
async def tts_delete(body: DeleteFileBody):
    if not _FILE_RE.match(body.file_name or ""):
        raise err(400, "file_name 非法")
    p = _files_dir() / body.file_name
    if p.is_file():
        p.unlink()
        return {"ok": True, "deleted": body.file_name}
    raise err(404, f"不存在: {body.file_name}")


# ───────────────────────── 声音 ─────────────────────────

@app.get("/api/tts/voices")
async def tts_voices():
    c = appconf.load().get("tts") or {}
    voices = engine._voices()
    return {"current": c.get("voice", ""),
            "engine": engine._tts_engine,
            "voices": [{"name": k, "label": v.get("label", k), "kind": v.get("kind")}
                       for k, v in sorted(voices.items())],
            "active_base": engine.tts.active_base()}


class VoiceBody(BaseModel):
    name: str


@app.post("/api/tts/voice")
async def tts_set_voice(body: VoiceBody):
    voices = engine._voices()
    if body.name not in voices:
        raise err(400, f"音色不存在: {body.name}")
    appconf.patch({"tts": {"voice": body.name}})
    return {"ok": True, "voice": body.name}


class PreviewBody(BaseModel):
    text: str
    voice: str = ""


@app.post("/api/tts/preview")
async def tts_preview(body: PreviewBody):
    voices = engine._voices()
    preset = voices.get(body.voice) or engine._voice_preset()
    try:
        pcm = await engine.tts.synth_pcm(body.text.strip() or "你好，这是音色试听。", preset)
    except RuntimeError as e:
        raise err(502, str(e)) from e
    return Response(content=audio.wav_bytes(pcm), media_type="audio/wav",
                    headers=NO_CACHE)


# ───────────────────────── 设置 ─────────────────────────

@app.get("/api/settings")
async def settings_get():
    return appconf.public()


@app.post("/api/settings")
async def settings_post(body: dict):
    previous_source = appconf.load().get("danmaku_source") or {}
    merged = appconf.patch(body or {})
    engine.configure_tts()
    if previous_source != (merged.get("danmaku_source") or {}):
        await danmaku_source.stop()
        await danmaku_source.start()
    return {"ok": True, "settings": appconf.public()}


@app.post("/api/settings/reload")
async def settings_reload():
    previous_source = appconf.load().get("danmaku_source") or {}
    appconf.reload()
    engine.configure_tts()
    if previous_source != (appconf.load().get("danmaku_source") or {}):
        await danmaku_source.stop()
        await danmaku_source.start()
    return {"ok": True, "settings": appconf.public()}


# ───────────────────────── 动作库 ─────────────────────────

@app.get("/api/actions/libs")
async def actions_libs():
    _configure_vu()                      # 空闲态也要注入 key(否则 _headers KeyError: api_key → 500)
    try:
        return await vu.list_action_libraries()
    except RuntimeError as e:
        raise err(502, str(e)) from e


class LibBody(BaseModel):
    name: str
    description: str = ""


@app.post("/api/actions/libs")
async def actions_libs_create(body: LibBody):
    _configure_vu()
    try:
        return await vu.create_action_library(body.name.strip(), body.description.strip())
    except RuntimeError as e:
        raise err(502, str(e)) from e


@app.delete("/api/actions/libs/{lib_id}")
async def actions_libs_delete(lib_id: str):
    _configure_vu()
    try:
        return await vu.delete_action_library(lib_id)
    except RuntimeError as e:
        raise err(502, str(e)) from e


@app.get("/api/actions/libs/{lib_id}/actions")
async def actions_list(lib_id: str):
    _configure_vu()
    try:
        return await vu.list_actions(lib_id)
    except RuntimeError as e:
        raise err(502, str(e)) from e


class ActionBody(BaseModel):
    name: str
    description: str


@app.post("/api/actions/libs/{lib_id}/actions")
async def actions_create(lib_id: str, body: ActionBody):
    _configure_vu()
    try:
        return await vu.create_action(lib_id, body.name.strip(), body.description.strip())
    except RuntimeError as e:
        raise err(502, str(e)) from e


@app.delete("/api/actions/libs/{lib_id}/actions/{action_id}")
async def actions_delete(lib_id: str, action_id: str):
    _configure_vu()
    try:
        return await vu.delete_action(lib_id, action_id)
    except RuntimeError as e:
        raise err(502, str(e)) from e


# ───────────────────────── 形象资产库(实时版同款) ─────────────────────────

def _avatar_item(it: dict) -> dict:
    return {"id": str(it.get("id") or ""),
            "name": it.get("name") or ("形象" + str(it.get("id"))[-4:]),
            "image": it.get("image_uri") or "", "status": it.get("status") or "",
            "created_at": it.get("created_at") or ""}


# ───────────────────────── 参考图(type11 prompt_operation) ─────────────────────────
# 2026-10-17 接入(交接笔记): 会话内发 type=11 就能换装 / 拿起指定商品 / 换背景,
# **同一条会话、1 积分/秒不变、信令不额外计费**。三条实测铁律:
#   ① 成功**没有回执**(数秒内画面自己变), 失败才回 type=14; 服务端**不去重** → 别重发
#   ② 换装**不要传 kind**、图里**不能有人脸**; 商品 kind=object; 背景 kind=background
#   ③ 上一张还挂着时新切换常静默不生效 → 先 remove 再等 15~20s(页面已自动这么做)

REFS_DIR = ROOT / "refs"


def _ctype_of(p: pathlib.Path) -> str:
    import mimetypes
    return mimetypes.guess_type(str(p))[0] or "image/jpeg"


def _decode_data_url(uri: str) -> tuple[bytes, str] | None:
    """data:image/png;base64,xxx → (bytes, ctype); 不是 data URL 返回 None。"""
    m = re.match(r"^data:(image/[\w.+-]+)?;base64,(.*)$", uri, re.S | re.I)
    if not m:
        return None
    ctype = (m.group(1) or "image/jpeg").lower()
    try:
        return base64.b64decode(m.group(2), validate=False), ctype
    except Exception:  # noqa: BLE001
        return None


class PromptImage(BaseModel):
    image_uri: str = ""      # ssupload:?id=… 短链 / data:image/*;base64,… / https 公网地址
    ref_name: str = ""       # 或 refs/ 里的文件名(端点内上传拿短链)
    image_id: str = ""       # 留空自动生成; 单次内不可重复
    kind: str = ""           # object / background；**换装请留空**(实测更稳)
    user_text: str = ""      # 自然语言描述, 很关键: 「给数字人换上这件毛衣」


class PromptBody(BaseModel):
    op_type: str = "switch"
    images: list[PromptImage] = []


async def _upload_local(p: pathlib.Path) -> str:
    """本机图片 → 三步上传拿 ssupload 短链(平台才取得到)。"""
    _configure_vu()
    try:
        return await vu.upload_bytes(p.read_bytes(), _ctype_of(p))
    except RuntimeError as e:
        raise err(502, f"参考图上传失败: {str(e)[:160]}") from e


async def _prepare_uri(it: PromptImage, idx: int) -> str:
    """把一张参考图变成**服务端取得到**的地址。

    ⚠️ 2026-10-17 事故: 用户把 Explorer「复制路径」的 `"C:\\...\\扇子.jpg"` 粘进了地址框,
        旧代码**不校验就放行** → Vidu 回 prompt_operation_ack success=false、画面不变。
        现在: 引号自动剥掉; **本机绝对路径直接读盘上传成短链**; 认不出的地址一律 400 说清支持哪些形式。"""
    raw = (it.image_uri or "").strip().strip('"').strip("'").strip()
    if not raw and (it.ref_name or "").strip():
        p = REFS_DIR / pathlib.Path(it.ref_name.strip()).name       # 防目录穿越
        if not p.is_file():
            raise err(404, f"refs/ 里没有 {it.ref_name}")
        return await _upload_local(p)
    if not raw:
        raise err(400, "每张参考图都要有 image_uri 或 ref_name")
    if raw.startswith("data:"):
        dec = _decode_data_url(raw)
        if not dec:
            raise err(400, f"第 {idx + 1} 张 data URL 解析失败")
        data, ctype = dec
        _configure_vu()
        try:
            return await vu.upload_bytes(data, ctype)                # 转短链: 不受 WS 16MB 限制
        except RuntimeError as e:
            raise err(502, f"参考图上传失败: {str(e)[:160]}") from e
    # 本机文件路径(绝对路径 / UNC / file://) → 读盘上传
    if re.match(r"^[A-Za-z]:[\\/]", raw) or raw.startswith("\\\\") or raw.startswith("file://"):
        p = pathlib.Path(raw[len("file://"):] if raw.startswith("file://") else raw)
        if not p.is_absolute():
            p = REFS_DIR / p.name
        if not p.is_file():
            raise err(404, f"本机找不到这个文件: {p}")
        return await _upload_local(p)
    if raw.startswith(("http://127.0.0.1", "http://localhost")):
        raise err(400, "平台取不到本机回环地址, 请用 ssupload 短链 / data URL / 公网地址 / 本机文件路径")
    if raw.startswith("ssupload:") or raw.startswith(("http://", "https://")):
        return raw
    raise err(400, "参考图地址不支持（支持: 本机文件路径 / refs 文件名 / ssupload 短链 / data:base64 / 公网 http(s)）")


@app.get("/api/prompt")
async def prompt_state():
    """当前参考图状态(给页面显示"已挂什么")。"""
    return {"ok": True, **engine.prompt_state()}


@app.post("/api/prompt")
async def prompt_op(body: PromptBody):
    """会话内切换/清除参考图(type=11): 换装 / 拿起指定商品 / 换背景。"""
    op = (body.op_type or "switch").strip().lower()
    if op == "remove":
        try:
            return await engine.prompt_op([], "remove")
        except RuntimeError as e:
            raise err(409, str(e)) from e
    if op != "switch":
        raise err(400, "op_type 只支持 switch / remove")
    if not body.images:
        raise err(400, "switch 至少需要 1 张参考图")
    if len(body.images) > 3:
        raise err(400, "单次最多 3 张参考图")
    used: set[str] = set()
    images: list[dict] = []
    now = int(time.time())
    for i, it in enumerate(body.images):
        uri = await _prepare_uri(it, i)
        iid = (it.image_id or "").strip() or f"ref-{now}-{i + 1}"
        if iid in used:
            raise err(400, f"image_id 单次内不可重复: {iid}")
        used.add(iid)
        item: dict = {"image_uri": uri, "image_id": iid[:128]}
        if (it.kind or "").strip():
            item["kind"] = it.kind.strip().lower()
        if (it.user_text or "").strip():
            item["user_text"] = it.user_text.strip()[:200]
        images.append(item)
    try:
        return await engine.prompt_op(images, "switch")
    except RuntimeError as e:
        raise err(502, str(e)) from e


@app.get("/api/refs")
async def refs_list():
    """refs/ 目录里可用的参考图(商品图/服装图/背景图放这儿, /api/prompt 用 ref_name 引用)。"""
    REFS_DIR.mkdir(parents=True, exist_ok=True)
    items = []
    for p in sorted(REFS_DIR.iterdir()):
        if p.is_file() and p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"):
            items.append({"name": p.name, "size": p.stat().st_size})
    return {"ok": True, "dir": str(REFS_DIR), "items": items}


@app.get("/api/avatars")
async def avatars_list():
    """账号资产清单 + 当前配置用的是哪个。"""
    _configure_vu()
    try:
        out, page = [], 0
        while page <= 4:
            d = await vu.list_avatars(page, 100)
            items = d.get("items") or d.get("avatars") or []
            out += [_avatar_item(it) for it in items]
            if len(items) < 100:
                break
            page += 1
    except RuntimeError as e:
        raise err(502, f"拉取资产失败: {str(e)[:200]}") from e
    cur = appconf.load().get("avatar") or {}
    return {"items": out,
            "current_avatar_id": str(cur.get("avatar_id") or ""),
            "current_image_uri": cur.get("image_uri") or ""}


@app.post("/api/avatars/upload")
async def avatars_upload(file: UploadFile):
    """传图 → 三步上传拿 ssupload 短链 → 平台注册成形象资产。"""
    data = await file.read()
    if not data:
        raise err(400, "空文件")
    if len(data) > 20 * 1024 * 1024:
        raise err(400, "图片超过 20MB")
    ctype = file.content_type or "image/png"
    if not ctype.startswith("image/"):
        raise err(400, f"只收图片, 收到 {ctype}")
    _configure_vu()
    try:
        uri = await vu.upload_bytes(data, ctype)
        name = _clean_upload_name(file.filename or "").rsplit(".", 1)[0][:20] or "我的形象"
        created = await vu.create_avatar(uri, name)
    except RuntimeError as e:
        raise err(502, f"建形象失败: {str(e)[:200]}") from e
    item = created.get("avatar") or created.get("item") or created
    return {"ok": True, "id": str(item.get("id") or item.get("avatar_id") or ""),
            "name": item.get("name") or name, "uri": uri}


class AvatarUseBody(BaseModel):
    avatar_id: str = ""
    image_uri: str = ""


@app.post("/api/avatars/use")
async def avatars_use(body: AvatarUseBody):
    """启用某个资产(avatar_id)或临时图(image_uri); 两者互斥, 引擎 image_uri 优先所以必须清另一个。"""
    if not body.avatar_id and not body.image_uri:
        raise err(400, "avatar_id / image_uri 至少一项")
    patch: dict = {"avatar": {}}
    if body.avatar_id:
        patch["avatar"] = {"avatar_id": body.avatar_id.strip(), "image_uri": None}
    else:
        patch["avatar"] = {"avatar_id": None, "image_uri": body.image_uri.strip()}
    appconf.patch(patch)
    return {"ok": True, "avatar": appconf.load().get("avatar")}


class AvatarDeleteBody(BaseModel):
    avatar_id: str


@app.post("/api/avatars/delete")
async def avatars_delete(body: AvatarDeleteBody):
    _configure_vu()
    try:
        await vu.delete_avatar(body.avatar_id.strip())
    except RuntimeError as e:
        raise err(502, f"删除失败: {str(e)[:200]}") from e
    return await avatars_list()


# ───────────────────────── Vidu API Key 管理(额度用完换号) ─────────────────────────

def _key_name(key: str) -> str:
    return key[-8:] if len(key) > 8 else key


def _vidu_keys() -> list[str]:
    vidu = appconf.load().get("vidu") or {}
    keys = [k for k in (vidu.get("api_keys") or []) if isinstance(k, str) and k]
    active = vidu.get("api_key") or ""
    if active and active not in keys:
        keys.insert(0, active)               # 当前生效的 key 永远在列表里
    return keys


def _keys_view() -> dict:
    active = (appconf.load().get("vidu") or {}).get("api_key") or ""
    masked = (active[:6] + "…" + active[-4:]) if len(active) > 12 else active
    return {"active": _key_name(active), "active_masked": masked,
            "keys": [_key_name(k) for k in _vidu_keys()]}


@app.get("/api/vidu/keys")
async def vidu_keys_get():
    """首次访问把当前 key 落进备用列表(以后切走还能切回来)。"""
    vidu = appconf.load().get("vidu") or {}
    active = vidu.get("api_key") or ""
    stored = vidu.get("api_keys") or []
    if active and active not in stored:
        appconf.patch({"vidu": {"api_keys": [active] + [k for k in stored if k]}})
    return _keys_view()


class ViduKeyBody(BaseModel):
    action: str                      # add | activate | delete
    key: str = ""
    name: str = ""                   # key 尾8位
    activate: bool = True            # add 时是否立即启用


@app.post("/api/vidu/keys")
async def vidu_keys_post(body: ViduKeyBody):
    if body.action == "add":
        key = body.key.strip()
        if not key.startswith("vda_") or len(key) < 16:
            raise err(400, "key 格式不对: 应以 vda_ 开头")
        keys = _vidu_keys()
        if key not in keys:
            keys.append(key)
        updates: dict = {"vidu": {"api_keys": keys}}
        if body.activate:
            updates["vidu"]["api_key"] = key
        appconf.patch(updates)
        return {"ok": True, **_keys_view()}
    if body.action == "activate":
        target = next((k for k in _vidu_keys() if _key_name(k) == body.name), "")
        if not target:
            raise err(404, "备用列表里没有这个 key")
        appconf.patch({"vidu": {"api_key": target}})
        return {"ok": True, **_keys_view()}
    if body.action == "delete":
        active = (appconf.load().get("vidu") or {}).get("api_key") or ""
        if body.name == _key_name(active):
            raise err(400, "不能删除当前生效的 key, 先切到别的")
        appconf.patch({"vidu": {"api_keys": [k for k in _vidu_keys()
                                             if _key_name(k) != body.name]}})
        return {"ok": True, **_keys_view()}
    raise err(400, f"未知 action: {body.action}")


# ───────────────────────── 其他 ─────────────────────────

@app.get("/api/credits")
async def credits():
    _configure_vu()
    try:
        return await vu.credits()
    except RuntimeError as e:
        raise err(502, str(e)) from e


@app.exception_handler(Exception)
async def unhandled(request, exc):  # pragma: no cover
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})
