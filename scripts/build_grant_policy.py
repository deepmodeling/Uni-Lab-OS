#!/usr/bin/env python3
"""编译原生锁授予策略库（native/grant-policy，Rust → C ABI 动态库）并可选复制到目标目录。

    python scripts/build_grant_policy.py                  # 只编译，打印产物路径
    python scripts/build_grant_policy.py --install ~/.unilabos/native

缺少 cargo 时退出码 2；调度器在找不到原生库时自动使用 Python 参考实现。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CRATE = ROOT / "native" / "grant-policy"


def library_filename() -> str:
    if sys.platform == "win32":
        return "unilab_grant_policy.dll"
    if sys.platform == "darwin":
        return "libunilab_grant_policy.dylib"
    return "libunilab_grant_policy.so"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", type=Path, help="把编译好的库复制到该目录（例如 <working_dir>/native）")
    parser.add_argument("--cargo", default="cargo")
    args = parser.parse_args()
    if shutil.which(args.cargo) is None:
        print("未找到 cargo；跳过原生库编译（调度器会使用 Python 参考实现）", file=sys.stderr)
        return 2
    subprocess.run(
        [args.cargo, "build", "--release", "--locked", "--manifest-path", str(CRATE / "Cargo.toml")],
        check=True,
    )
    artifact = CRATE / "target" / "release" / library_filename()
    if not artifact.is_file():
        print(f"编译完成但未找到产物 {artifact}", file=sys.stderr)
        return 1
    if args.install:
        args.install.expanduser().mkdir(parents=True, exist_ok=True)
        target = args.install.expanduser() / library_filename()
        shutil.copy2(artifact, target)
        artifact = target
    print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
