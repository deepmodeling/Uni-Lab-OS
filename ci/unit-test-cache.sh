#!/usr/bin/env bash
# 优先复用工作目录之外的 Python 测试环境。

source "$(dirname "${BASH_SOURCE[0]}")/ci-package-sources.sh"

CONDA_FORGE_CHANNEL="${CONDA_FORGE_CHANNEL:-https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud/conda-forge}"

unit_test_cache_root() {
  if [[ -n "${UNIT_TEST_CACHE_ROOT:-}" ]]; then
    printf '%s\n' "$UNIT_TEST_CACHE_ROOT"
    return
  fi
  local parent candidate
  for candidate in /builds/lab-os/.ci-cache /cache/uni-lab-os; do
    parent="$(dirname "$candidate")"
    if [[ -d "$parent" && -w "$parent" ]]; then
      printf '%s\n' "$candidate"
      return
    fi
  done
  printf '%s\n' "${CI_PROJECT_DIR:-$PWD}/.ci-local-cache"
}

unit_test_sha256() {
  sha256sum "$1" | awk '{print $1}'
}

unit_test_curl() {
  curl --fail --location --retry 5 --retry-delay 2 \
    --connect-timeout 20 --speed-time 30 --speed-limit 10000 \
    "$@"
}

unit_test_ensure_os_packages() {
  if command -v curl >/dev/null 2>&1 \
    && command -v gcc >/dev/null 2>&1 \
    && command -v sha256sum >/dev/null 2>&1; then
    return
  fi
  use_ci_apt_mirrors || return
  apt-get update -qq >&2 || return
  apt-get install -y --no-install-recommends \
    ca-certificates curl bzip2 tar xz-utils build-essential git >&2
}

unit_test_micromamba() {
  local root="$1"
  local bin="$root/bin/micromamba"
  if [[ ! -x "$bin" ]]; then
    local archive="$root/micromamba.tar.bz2"
    local sha256="8528263837623551a44464a372e5bd6b0b856479a83d2a77490a19dd98da3b06"
    mkdir -p "$root/bin" || return
    unit_test_curl --show-error --output "$archive" \
      "${CONDA_FORGE_CHANNEL%/}/linux-64/micromamba-2.8.1-1.tar.bz2" || return
    printf '%s  %s\n' "$sha256" "$archive" | sha256sum -c - >&2 || return
    tar -xjf "$archive" -C "$root" bin/micromamba || return
  fi
  printf '%s\n' "$bin"
}

# 标准输出只有 "hit <prefix>" 或 "miss <prefix>"。依赖安装由调用方完成后再提交 key。
unit_test_prepare_env() {
  local name="$1"
  local key="$2"
  local root prefix recorded
  root="$(unit_test_cache_root)"
  prefix="$root/envs/$name"
  recorded=""
  if [[ -f "$prefix/.cache-key" ]]; then
    recorded="$(<"$prefix/.cache-key")"
  fi
  if [[ -x "$prefix/bin/python" && "$recorded" == "$key" ]]; then
    printf 'hit %s\n' "$prefix"
    return
  fi
  unit_test_ensure_os_packages || return
  local mamba
  mamba="$(unit_test_micromamba "$root")" || return
  rm -rf "$prefix"
  "$mamba" create -y -p "$prefix" --override-channels -c "$CONDA_FORGE_CHANNEL" python=3.11 pip >&2 || return
  printf 'miss %s\n' "$prefix"
}

unit_test_commit_env() {
  local prefix="$1"
  local key="$2"
  printf '%s\n' "$key" >"$prefix/.cache-key"
}
