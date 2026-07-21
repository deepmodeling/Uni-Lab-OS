#!/usr/bin/env bash
# 确定性修复 macOS + conda 下 dora_cli / pyarrow 的 libiconv `_iconv` 缺失问题。
#
# 原因：dora_cli 与 pip 版 pyarrow 的原生库链接系统 /usr/lib/libiconv.2.dylib（需 `_iconv`），
# 但 conda 的 GNU libiconv 只导出 `_libiconv` 且被 dyld 按叶名 coalesce，导致
# `Symbol not found: _iconv`。本脚本生成兼容垫片 libiconv_compat.dylib（把 `_iconv*`
# 转发到 `_libiconv*`），并把两个原生库的 libiconv 依赖改写到该垫片。幂等、可重复执行。
#
# 用法：conda activate unilab && bash scripts/fix_macos_libiconv.sh
set -euo pipefail

if [[ "$(uname)" != "Darwin" ]]; then
  echo "[skip] 仅 macOS 需要此修复"; exit 0
fi

PY=$(python -c 'import sys; print(sys.prefix)')
CONDA_ICONV="$PY/lib/libiconv.2.dylib"
if [[ ! -f "$CONDA_ICONV" ]]; then echo "[error] 未找到 $CONDA_ICONV"; exit 1; fi

SHIM_SRC=$(mktemp /tmp/iconv_shim.XXXX.c)
cat > "$SHIM_SRC" <<'C'
#include <stddef.h>
typedef void* iconv_t;
extern iconv_t libiconv_open(const char*, const char*);
extern size_t  libiconv(iconv_t, char**, size_t*, char**, size_t*);
extern int     libiconv_close(iconv_t);
iconv_t iconv_open(const char* to, const char* from){ return libiconv_open(to, from); }
size_t  iconv(iconv_t c, char** ib, size_t* il, char** ob, size_t* ol){ return libiconv(c, ib, il, ob, ol); }
int     iconv_close(iconv_t c){ return libiconv_close(c); }
C

patch_lib () {
  local target="$1"          # 待修复的 .so/.dylib
  local dir; dir=$(dirname "$target")
  local shim="$dir/libiconv_compat.dylib"
  local old; old=$(otool -L "$target" | grep -i 'iconv' | awk '{print $1}' | grep -v 'libiconv_compat' | head -1 || true)
  if [[ -z "$old" ]]; then echo "[ok] $target 已无系统 libiconv 依赖，跳过"; return; fi
  clang -dynamiclib -o "$shim" "$SHIM_SRC" "$CONDA_ICONV" -install_name @loader_path/libiconv_compat.dylib
  install_name_tool -change "$old" @loader_path/libiconv_compat.dylib "$target"
  codesign -f -s - "$target" >/dev/null 2>&1 || true
  codesign -f -s - "$shim" >/dev/null 2>&1 || true
  echo "[fixed] $target ($old -> @loader_path/libiconv_compat.dylib)"
}

# dora_cli（CLI/daemon）
DORA_SO=$(python -c 'import dora_cli, os; print(os.path.join(os.path.dirname(dora_cli.__file__), "dora_cli.abi3.so"))')
patch_lib "$DORA_SO"

# dora python binding（节点进程 `from dora import Node`）
DORA_PY_SO=$(python -c 'import dora, os; print(os.path.join(os.path.dirname(dora.__file__), "dora.abi3.so"))')
patch_lib "$DORA_PY_SO"

# pyarrow libarrow
LIBARROW=$(python -c 'import pyarrow, glob, os; print(glob.glob(os.path.join(os.path.dirname(pyarrow.__file__), "libarrow.*.dylib"))[0])')
patch_lib "$LIBARROW"

rm -f "$SHIM_SRC"

echo "=== 自检 ==="
dora --version | head -1
python -c "import pyarrow; from unilabos.devices.virtual.virtual_stirrer import VirtualStirrer; print('pyarrow+driver coexist OK', pyarrow.__version__)"
