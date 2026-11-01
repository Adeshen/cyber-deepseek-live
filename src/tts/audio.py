"""音频工具: 任意音频 → PCM s16le / 24kHz / mono(Vidu 组件版要求的精确格式)。

全部走 ffmpeg(机器上已有 C:\\ffmpeg7.1.1)。统一出口 = 裸 PCM 字节流,
上游不用关心源是 OmniVoice 的 wav、用户点播的 mp3 还是别的什么。

Vidu 音频帧参数: 48 字节/毫秒; 建议帧大小 20ms(960B) 或 100ms(4800B)。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import wave

SAMPLE_RATE = 24000
BYTES_PER_MS = SAMPLE_RATE * 2 // 1000          # 48


def find_ffmpeg() -> str:
    p = shutil.which("ffmpeg")
    if p:
        return p
    for cand in (r"C:\ffmpeg7.1.1\bin\ffmpeg.exe", r"C:\ffmpeg\bin\ffmpeg.exe"):
        if os.path.isfile(cand):
            return cand
    return "ffmpeg"


def decode_to_pcm(src_path: str | None = None, src_bytes: bytes | None = None,
                  src_mime: str = "") -> bytes:
    """任意音频(wav/mp3/flac/m4a/ogg/opus...) → s16le / 24kHz / mono 裸 PCM。"""
    ff = find_ffmpeg()
    args = [ff, "-hide_banner", "-loglevel", "error", "-nostdin"]
    tmp = None
    if src_path:
        args += ["-i", src_path]
    else:
        tmp = tempfile.NamedTemporaryFile(suffix=_mime_ext(src_mime), delete=False)
        tmp.write(src_bytes or b"")
        tmp.close()
        args += ["-i", tmp.name]
    args += ["-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"]
    try:
        r = subprocess.run(args, capture_output=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(f"ffmpeg 解码失败: {r.stderr.decode('utf-8', 'ignore')[:300]}")
        return r.stdout
    finally:
        if tmp:
            try:
                os.unlink(tmp.name)
            except Exception:
                pass


def _mime_ext(mime: str) -> str:
    mime = (mime or "").lower()
    for probe, ext in (("wav", ".wav"), ("mpeg", ".mp3"), ("mp3", ".mp3"),
                       ("flac", ".flac"), ("mp4", ".m4a"), ("m4a", ".m4a"),
                       ("ogg", ".ogg"), ("opus", ".opus"), ("webm", ".webm")):
        if probe in mime:
            return ext
    return ".bin"


def pcm_seconds(pcm: bytes) -> float:
    return len(pcm) / (BYTES_PER_MS * 1000.0)


def silence_pcm(ms: int) -> bytes:
    return b"\x00" * (BYTES_PER_MS * max(0, int(ms)))


def chunk_pcm(pcm: bytes, chunk_ms: int = 100):
    """按 chunk_ms 切 PCM, 产出 (bytes, 已播ms)。最后一帧可能不满。"""
    step = BYTES_PER_MS * max(10, int(chunk_ms))
    emitted_ms = 0
    for i in range(0, len(pcm), step):
        piece = pcm[i:i + step]
        yield piece, emitted_ms
        emitted_ms += len(piece) // BYTES_PER_MS


def wav_bytes(pcm: bytes) -> bytes:
    """裸 PCM 包成 24kHz 单声道 WAV(试听/落盘用)。"""
    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()
