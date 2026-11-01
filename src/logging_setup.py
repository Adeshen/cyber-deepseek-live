"""桥的日志落盘: 把 stdout/stderr 同时写进 logs/bridge.log(轮转)。

## 为什么要有这个文件(2026-10-10)
一键启动脚本用 `Start-Process -WindowStyle Minimized` 拉起桥 —— 输出全在那个**最小化的
控制台窗口**里。一旦出现"唱歌时聊几句就断流"这类事故, 事后没有任何东西可查: 用户只能
看到面板上一行短消息, 我看不到完整堆栈、也看不到"到底是谁关的连接"。
于是: 所有 print / traceback 同时落盘, 用 `tools/watch-bridge-log.ps1` 直接跟屏幕看,
事故现场可以整段贴回来。

## 用法
    from . import logging_setup
    logging_setup.attach()          # 幂等, 可重复调用
日志: <项目根>/logs/bridge.log, 超过 4MB 轮转, 保留 4 份(bridge.log.1 … .4)。

## 2026-10-13 修复: stdout/stderr 共享一个 Sink
原来 stdout/stderr 两个 _Tee **各自**开句柄、各自计数、各自轮转同一个文件:
① 两份计数叠加, 实测 bridge.log 长到 10MB 才轮(上限 4MB);
② 一方轮转(改名)后, 另一方还攥着旧句柄继续往已改名文件里写。
现在 _Sink 唯一持有句柄/计数/轮转, 两个 _Tee 共享它。
"""
from __future__ import annotations

import io
import os
import pathlib
import sys
import time

LOG_DIR = pathlib.Path(__file__).resolve().parent.parent / "logs"
LOG_NAME = "bridge.log"
MAX_BYTES = 4 * 1024 * 1024
KEEP = 4

_attached = False


class _Sink:
    """唯一持有日志文件句柄 + 大小计数 + 轮转(stdout/stderr 共享)。"""

    def __init__(self, path: pathlib.Path):
        self._path = path
        self._fh = None
        self._size = 0
        self._open()

    def _open(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = open(self._path, "a", encoding="utf-8", errors="replace")
            try:
                self._size = self._path.stat().st_size
            except OSError:
                self._size = 0
        except Exception:
            self._fh = None

    def _rotate(self) -> None:
        try:
            if self._fh:
                self._fh.close()
        except Exception:
            pass
        self._fh = None
        try:
            for i in range(KEEP - 1, 0, -1):
                src = self._path.with_name(f"{self._path.name}.{i}")
                dst = self._path.with_name(f"{self._path.name}.{i + 1}")
                if src.exists():
                    if dst.exists():
                        dst.unlink()
                    os.replace(src, dst)
            if self._path.exists():
                os.replace(self._path, self._path.with_name(f"{self._path.name}.1"))
        except Exception:
            pass
        self._open()

    def write(self, s: str) -> None:
        if self._fh is None:
            return
        try:
            self._fh.write(s)
            self._size += len(s.encode("utf-8", errors="replace"))
            if self._size >= MAX_BYTES:
                self._rotate()
        except Exception:
            pass

    def flush(self) -> None:
        try:
            if self._fh is not None:
                self._fh.flush()
        except Exception:
            pass


class _Tee(io.TextIOBase):
    """把写入同时送到原控制台 + 共享日志 Sink(带轮转)。"""

    def __init__(self, original, sink: _Sink):
        self._original = original
        self._sink = sink

    # ---- io 接口 ----
    def write(self, s: str) -> int:  # type: ignore[override]
        try:
            self._original.write(s)
        except Exception:
            pass
        self._sink.write(s)
        return len(s)

    def flush(self) -> None:
        for f in (self._original, self._sink):
            try:
                if f is not None:
                    f.flush()
            except Exception:
                pass

    def isatty(self) -> bool:
        try:
            return bool(self._original.isatty())
        except Exception:
            return False

    @property
    def encoding(self):
        return getattr(self._original, "encoding", "utf-8")

    @property
    def errors(self):
        return getattr(self._original, "errors", "replace")


def attach(path: pathlib.Path | None = None) -> pathlib.Path | None:
    """挂上日志落盘(幂等)。返回日志文件路径; 失败返回 None(不影响主流程)。"""
    global _attached
    if _attached:
        return LOG_DIR / LOG_NAME
    p = path or (LOG_DIR / LOG_NAME)
    try:
        banner = (f"\n{'=' * 70}\n[log] 进程启动 {time.strftime('%Y-%m-%d %H:%M:%S')} "
                  f"pid={os.getpid()} cwd={os.getcwd()}\n{'=' * 70}\n")
        sink = _Sink(p)                       # stdout/stderr 共享同一份句柄/计数/轮转
        sys.stdout = _Tee(sys.stdout, sink)   # type: ignore[assignment]
        sys.stderr = _Tee(sys.stderr, sink)   # type: ignore[assignment]
        sys.stdout.write(banner)
        sys.stdout.flush()
        _attached = True
        return p
    except Exception:
        return None
