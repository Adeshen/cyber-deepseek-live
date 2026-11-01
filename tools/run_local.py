#!/usr/bin/env python3
"""Mac foreground launcher; inspect only tts.engine, never key files or values."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
import webbrowser


ROOT = Path(__file__).resolve().parent.parent
LOCAL = ROOT / "assets" / "local"
PYTHON = ROOT / ".venv" / "bin" / "python"
BRIDGE_PORT = 8795
BREEZE_PORT = 17860
STAGE_URL = f"http://127.0.0.1:{BRIDGE_PORT}/stage"
SSH_TARGET = "root@cpod-1x371za4lojo.podtcp.compshare.cn"
SSH_PORT = "26571"
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _configured_tts_engine() -> str:
    """Select dependencies without exposing any other config fields.

    The application shares this JSON file with its Vidu settings. Only the
    engine name is retained; config content and parse exceptions are not logged.
    Key files are never opened by this launcher.
    """
    config_path = Path(os.environ.get("VIDU_CONF") or (ROOT / "config.json"))
    try:
        with config_path.open(encoding="utf-8") as config_file:
            config = json.load(config_file)
        tts = config.get("tts") if isinstance(config, dict) else None
        raw = tts.get("engine", "omnivoice") if isinstance(tts, dict) else "omnivoice"
        del config
        engine = raw.strip().lower() if isinstance(raw, str) else ""
    except OSError:
        raise RuntimeError("无法读取本地配置，请检查 config.json 或 VIDU_CONF 指定的文件。") from None
    except (ValueError, TypeError):
        raise RuntimeError("本地配置不是有效 JSON，请在本机检查配置文件。") from None
    if engine not in {"breezeblue", "breeze", "omnivoice"}:
        raise RuntimeError("tts.engine 须为 breezeblue、breeze 或 omnivoice。")
    return engine


def _json_at(port: int, path: str) -> dict | None:
    try:
        with HTTP.open(f"http://127.0.0.1:{port}{path}", timeout=1.5) as response:
            # Health/OpenAPI responses contain no credentials; never print their bodies.
            raw = response.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            return None
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _project_health() -> bool:
    health = _json_at(BRIDGE_PORT, "/api/health")
    if not health or health.get("ok") is not True:
        return False
    if not {"build", "phase", "tts_engine"}.issubset(health):
        return False
    # The log path identifies this checkout, not another copy of the same app.
    log = health.get("log")
    if not isinstance(log, str) or Path(log).resolve() != ROOT / "logs" / "bridge.log":
        return False
    spec = _json_at(BRIDGE_PORT, "/openapi.json")
    return bool(spec and (spec.get("info") or {}).get("title") == "vidu-dh-s2-component")


def _breeze_health() -> bool:
    health = _json_at(BREEZE_PORT, "/api/health")
    return bool(health and health.get("gateway") == "ok"
                and health.get("family") == "breeze_tts")


def _open_stage() -> None:
    print(f"舞台地址：{STAGE_URL}", flush=True)
    if os.environ.get("CYBER_XIAOYA_NO_BROWSER") == "1":
        return
    try:
        webbrowser.open(STAGE_URL)
    except Exception:
        print("浏览器未自动打开，请复制上方地址。", flush=True)


def _stop_service(service: subprocess.Popen | None) -> None:
    if service is None or service.poll() is not None:
        return
    print("正在停止本次启动的本地服务……", flush=True)
    for sig, timeout in ((signal.SIGINT, 8), (signal.SIGTERM, 3), (signal.SIGKILL, 2)):
        try:
            os.killpg(service.pid, sig)
        except ProcessLookupError:
            return
        try:
            service.wait(timeout=timeout)
            return
        except subprocess.TimeoutExpired:
            pass


def _stop_tunnel(control: Path | None) -> None:
    # A fresh, private control path is assigned only to a tunnel created here.
    # Existing tunnels/listeners are never registered here and are never closed.
    if control is None or not control.exists():
        return
    print("正在关闭本次启动的 Breeze SSH 通道……", flush=True)
    try:
        subprocess.run(
            ["/usr/bin/ssh", "-S", str(control), "-O", "exit", "-p", SSH_PORT, SSH_TARGET],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        print("通道关闭命令未完成；可检查本项目 assets/local/ssh-* 控制 socket。", flush=True)


def _start_tunnel(control: Path) -> None:
    known_hosts = LOCAL / "known_hosts"
    print("连接 Breeze 服务器；若提示 Password，请在此终端输入 SSH 密码（不会显示或保存）。", flush=True)
    result = subprocess.run(
        ["/usr/bin/ssh", "-f", "-N", "-M", "-S", str(control),
         "-o", "ControlMaster=yes", "-o", "ControlPersist=no",
         "-o", "ExitOnForwardFailure=yes",
         "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
         "-o", f'UserKnownHostsFile="{known_hosts}"',
         "-o", "StrictHostKeyChecking=accept-new",
         "-L", "127.0.0.1:17860:127.0.0.1:7860", "-p", SSH_PORT, SSH_TARGET],
        cwd=ROOT, check=False,
    )
    if result.returncode:
        raise RuntimeError("SSH 通道未建立，请检查实例运行状态、网络和 SSH 密码。")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if _breeze_health():
            return
        time.sleep(0.5)
    raise RuntimeError("SSH 已连接，但 Breeze 服务不可达。请在云端恢复实例并等待约 2 分钟后重试。")


def main() -> int:
    os.chdir(ROOT)
    print("大肥鱼 · Mac 本地启动器", flush=True)
    print("本脚本不会创建 Vidu 付费连接，也不会公开直播。", flush=True)
    print("需要数字人画面时，在舞台页手动点“开始连线”；结束后先点“断开”，再按 Ctrl+C。", flush=True)

    if _project_health():
        print("本项目已在运行，直接打开现有舞台；本次不会接管或停止它。", flush=True)
        _open_stage()
        return 0
    if _listening(BRIDGE_PORT):
        raise RuntimeError("127.0.0.1:8795 已被其他服务占用；请先关闭该服务，再重新启动。")
    if not PYTHON.is_file():
        raise RuntimeError("未找到 .venv/bin/python，请先安装本项目的 Python 环境。")
    engine = _configured_tts_engine()
    if engine == "breezeblue":
        print("语音模式：本地桥 + BreezeBlue 官方 API；不需要 SSH，不检查本地 17860 端口。", flush=True)
    elif engine == "omnivoice":
        print("语音模式：本地桥 + OmniVoice；不自动建立 SSH 通道。", flush=True)
    else:
        print("语音模式：本地桥 + 旧版 Breeze SSH 网关。", flush=True)

    LOCAL.mkdir(parents=True, exist_ok=True)
    lock = (LOCAL / "run-local.lock").open("a")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise RuntimeError("另一个本项目启动器正在运行或等待 SSH 密码；请查看已有终端。")

    service: subprocess.Popen | None = None
    control: Path | None = None
    try:
        # Recheck after taking the lock in case another process started meanwhile.
        if _project_health():
            _open_stage()
            return 0
        if _listening(BRIDGE_PORT):
            raise RuntimeError("127.0.0.1:8795 已被其他服务占用，未启动新服务。")

        # Official BreezeBlue and OmniVoice do not depend on the SSH gateway.
        if engine == "breeze":
            if _breeze_health():
                print("复用已运行的 Breeze 接口；退出时会保留该通道。", flush=True)
            elif _listening(BREEZE_PORT):
                raise RuntimeError("127.0.0.1:17860 已有监听，但不是可用的 Breeze 接口。请检查原通道和云端服务；本脚本不会覆盖或关闭它。")
            else:
                candidate = LOCAL / f"ssh-{uuid.uuid4().hex[:8]}"
                if len(os.fsencode(candidate)) >= 100:
                    raise RuntimeError("项目路径过长，SSH 控制 socket 无法创建；请将项目放到更短的目录。")
                if candidate.exists():
                    raise RuntimeError("本次 SSH 控制 socket 已存在，请重新启动。")
                control = candidate
                _start_tunnel(control)

        print("启动本地服务，请保留这个终端窗口。", flush=True)
        service = subprocess.Popen(
            [str(PYTHON), "-u", "-m", "src.main", "--no-browser",
             "--host", "127.0.0.1", "--port", str(BRIDGE_PORT)],
            cwd=ROOT, start_new_session=True,
        )
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if service.poll() is not None:
                raise RuntimeError(f"本地服务提前退出，退出码 {service.returncode}；请查看上方日志。")
            if _project_health():
                _open_stage()
                print("本地服务已就绪。按 Ctrl+C 停止本次启动的本地服务。", flush=True)
                return service.wait()
            time.sleep(0.5)
        raise RuntimeError("本地服务在 60 秒内未就绪，请查看上方日志。")
    finally:
        _stop_service(service)
        _stop_tunnel(control)
        lock.close()


if __name__ == "__main__":
    # Terminal window closure/SIGTERM follows the same owned-resource cleanup path.
    def _interrupt(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _interrupt)
    signal.signal(signal.SIGHUP, _interrupt)
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n本次启动已停止。", flush=True)
        raise SystemExit(130)
    except (OSError, RuntimeError) as exc:
        print(f"\n启动失败：{exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
