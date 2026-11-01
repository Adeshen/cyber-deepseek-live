#!/bin/zsh
set -e
cd -- "${0:A:h}"
if [[ -x .venv/bin/python ]]; then
  .venv/bin/python -m pip --version >/dev/null 2>&1 || true
elif command -v uv >/dev/null 2>&1; then
  uv venv .venv --python 3.12
elif command -v python3.12 >/dev/null 2>&1; then
  python3.12 -m venv .venv
else
  print -u2 -- "请先安装 Python 3.12 或 uv，再运行本脚本。"
  exit 1
fi
if command -v uv >/dev/null 2>&1; then
  uv pip install --python .venv/bin/python -r requirements.mac.lock.txt
else
  .venv/bin/python -m pip install -r requirements.mac.lock.txt
fi
if ! command -v ffmpeg >/dev/null 2>&1; then
  print -- "尚未找到 ffmpeg；音频试听和点歌前请安装并加入 PATH。"
fi
print -- "依赖已安装。请按打包使用说明补齐三个 API Key，然后双击启动-大肥鱼.command。"
read -k 1 "?按任意键退出。"
