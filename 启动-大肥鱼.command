#!/bin/zsh
# Finder 双击会在“终端”里启动；密码只由 ssh 在终端中交互读取。
cd -- "${0:A:h}" || exit 1
if [[ ! -x .venv/bin/python ]]; then
  print -u2 -- "缺少本项目的 .venv/bin/python，请先完成 Python 环境安装。"
  exit 1
fi
exec .venv/bin/python -u tools/run_local.py
