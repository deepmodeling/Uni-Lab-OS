#!/usr/bin/env bash
# GitLab MR ruff 静态检查。由 lint_ruff 调用；不装项目依赖。
set -euo pipefail

source "$(dirname "$0")/ci-package-sources.sh"

use_ci_apt_mirrors
apt-get update -qq
apt-get install -y --no-install-recommends ca-certificates python3 python3-pip
# 国内 Runner 优先使用镜像。
python3 -m pip install --disable-pip-version-check --no-cache-dir \
  --index-url "${PIP_INDEX_URL:-https://mirrors.aliyun.com/pypi/simple}" \
  --only-binary=:all: 'ruff==0.15.19'

# 命令行显式 --select，避免旧 MR 未带 ruff.toml 时落入 ruff 默认整包 Pyflakes。
python3 -m ruff check \
  --target-version py311 \
  --select E9,F821,E722,W605,I \
  --ignore E501 \
  unilabos tests ci scripts
