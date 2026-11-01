"""LLM 客户端(OpenAI 兼容 /chat/completions, 流式) + 增量分句器。

默认指向 DeepSeek 官方(api.deepseek.com + deepseek-chat); base_url / model / key_file
全部在 config.json 里, 以后换 API 只改配置不改代码。key 解析顺序:
  1. llm.key_file 指向的文本文件(如 C:\\keys\\deepseek-key.txt)
  2. 环境变量 DEEPSEEK_API_KEY
流式输出边到边分句: 每凑齐一句就交出去合成, 首字延迟 ≈ LLM 首句时间 + 一次 TTS。
"""
from __future__ import annotations

import asyncio
import collections
import json
import os
import pathlib
import re

import httpx

try:
    import certifi
    _VERIFY = certifi.where()
except Exception:
    _VERIFY = True

_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_MD_NOISE_RE = re.compile(r"[*_#`>\[\]~|]")

# 强停顿(必须分句)与弱停顿(句子够长时才分)
STRONG_ENDS = "。！？!?；;\n"
SOFT_ENDS = "，,、"


def strip_think(text: str) -> str:
    return _THINK_RE.sub("", text).strip()


def clean_for_speech(text: str) -> str:
    """喂给 TTS 前清掉 markdown 噪音与多余空白。"""
    t = _MD_NOISE_RE.sub("", text)
    t = re.sub(r"[ \t]+", " ", t)
    return t.strip()


class SentenceStream:
    """增量分句: feed(增量文本) → 本轮凑齐的完整句子列表; flush() 收尾。

    规则(2026-10-13 收紧, 用户拍板: 按标点切、≤30字、每轮合成时间最短):
    强停顿立即分句; 弱停顿只在已积累 ≥ soft_min(10) 字时分(太短的逗号切了
    韵律会碎); 缓冲超过 hard_max(30) 字强制放出(无标点长串兜底)。
    """

    def __init__(self, soft_min: int = 10, hard_max: int = 30):
        self.buf = ""
        self.soft_min = soft_min
        self.hard_max = hard_max

    def feed(self, delta: str) -> list[str]:
        self.buf += delta or ""
        out: list[str] = []
        while True:
            cut = -1
            soft = False
            for i, ch in enumerate(self.buf):
                if ch in STRONG_ENDS:
                    cut, soft = i, False
                    break
                if ch in SOFT_ENDS and i + 1 >= self.soft_min:
                    cut, soft = i, True
                    break
            if cut < 0:
                if len(self.buf) > self.hard_max:   # 兜底: 强制放行
                    out.append(self.buf[:self.hard_max].strip())
                    self.buf = self.buf[self.hard_max:]
                    continue
                break
            piece = self.buf[:cut + 1].strip()
            self.buf = self.buf[cut + 1:]
            if piece:
                out.append(piece)
            if soft:
                continue
        return out

    def flush(self) -> str:
        tail = self.buf.strip()
        self.buf = ""
        return tail


def merge_for_tts(pieces: list[str], min_chars: int = 30, first_min: int = 14) -> list[str]:
    """把分句结果**合并成更长的合成单元**(2026-10-17 用户方案B)。

    背景: 每段都要跑一次完整 GPU 合成(实测 1.9~3.5 秒), 而 10 来字的短句只出 ~2 秒音频,
    串行合成时队列补不上 → 每个短分句之间空一小拍, 听感就是"一卡一卡"。
    合并成更大的块: GPU 调用少 2~3 倍、每块音频更长, 句间空档基本消失。

    两级阈值(兼顾开口速度):
      · **第一块**攒到 `first_min`(默认 14 字)就出 —— 别为了攒长度拖慢开口;
      · 之后每块攒到 `min_chars`(默认 30 字)再出。
    """
    out: list[str] = []
    buf = ""
    for s in (pieces or []):
        s = s or ""
        if not s.strip():
            continue
        buf += s
        target = first_min if not out else min_chars
        if len(buf) >= max(6, target):
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out or [""][:0]


def split_for_tts(text: str, soft_min: int = 10, hard_max: int = 30) -> list[str]:
    """批式分段: 整段文本一次性切成 TTS 合成单元(与流式 SentenceStream 同规则)。
    用于 弹幕复读/直接开口 这类"整条到手"的路径 —— 原来整条一坨喂 synth,
    长文本首包慢且断句失控(2026-10-13 用户反馈)。"""
    t = clean_for_speech(text or "")
    if not t:
        return []
    ss = SentenceStream(soft_min, hard_max)
    out = [s for s in ss.feed(t) if s.strip()]
    tail = ss.flush()
    if tail.strip():
        out.append(tail)
    return out or [t]


class LLMClient:
    def __init__(self, conf_get):
        self._conf_get = conf_get          # () -> llm 配置段(热改配置立即生效)
        self.history: collections.deque = collections.deque(maxlen=32)

    # ---- 配置 / key ----

    def conf(self) -> dict:
        return self._conf_get() or {}

    def api_key(self) -> str:
        c = self.conf()
        kf = (c.get("key_file") or "").strip()
        if kf:
            try:
                k = pathlib.Path(kf).read_text(encoding="utf-8").strip()
                if k:
                    return k
            except Exception:
                pass
        return (os.environ.get("DEEPSEEK_API_KEY") or "").strip()

    def system_prompt(self) -> str:
        return self.conf().get("system_prompt") or "你是一个口语化的中文数字人助手。"

    def _messages(self, user_text: str) -> list[dict]:
        msgs = [{"role": "system", "content": self.system_prompt()}]
        msgs.extend(self.history)
        msgs.append({"role": "user", "content": user_text})
        return msgs

    # ---- 流式调用 ----

    async def stream(self, user_text: str, *, commit: bool = True):
        """Yield text; callers sending audio can commit only successfully sent segments."""
        c = self.conf()
        key = self.api_key()
        if not key:
            raise RuntimeError(f"LLM key 未找到(key_file={c.get('key_file')!r} 且无 DEEPSEEK_API_KEY)")
        base = (c.get("base_url") or "https://api.deepseek.com").rstrip("/")
        body = {
            "model": c.get("model") or "deepseek-chat",
            "messages": self._messages(user_text),
            "stream": True,
            "temperature": float(c.get("temperature", 0.7)),
            "max_tokens": int(c.get("max_tokens", 300)),
        }
        # 思考模式开关(2026-10-10): 官方新模型(deepseek-flash)默认开思考且 effort=high,
        # 思维链会把小 max_tokens 吃光返回空 content → 弹幕短回复必须显式 disabled。
        _think = (c.get("thinking") or "").strip().lower()
        if _think in ("enabled", "disabled"):
            body["thinking"] = {"type": _think}
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        url = base + "/chat/completions"
        full: list[str] = []
        timeout = httpx.Timeout(30.0, read=300.0)
        async with httpx.AsyncClient(verify=_VERIFY, timeout=timeout) as client:
            async with client.stream("POST", url, headers=headers, json=body) as r:
                if r.status_code >= 400:
                    text = (await r.aread()).decode("utf-8", "ignore")
                    raise RuntimeError(f"LLM HTTP {r.status_code}: {text[:300]}")
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        obj = json.loads(payload)
                    except Exception:
                        continue
                    delta = ((obj.get("choices") or [{}])[0].get("delta") or {})
                    piece = delta.get("content") or ""
                    if piece:
                        full.append(piece)
                        yield piece
        reply = strip_think("".join(full))
        if commit:
            self.commit_exchange(user_text, reply)

    def commit_exchange(self, user_text: str, assistant_text: str) -> None:
        """Store a completed exchange, or audio segments confirmed sent, not heard."""
        reply = strip_think(assistant_text or "")
        if not reply:
            return
        self.history.append({"role": "user", "content": user_text})
        self.history.append({"role": "assistant", "content": reply})
        self.trim_history()

    def trim_history(self):
        """按 history_turns 截断(2 条/回合)。"""
        turns = int(self.conf().get("history_turns", 8))
        while len(self.history) > turns * 2:
            self.history.popleft()

    def reset_history(self):
        self.history.clear()
