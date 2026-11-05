#!/usr/bin/env python3
"""编译原生锁授予策略库（native/grant-policy，Rust → C ABI 动态库），并重新生成接口文件。

每次编译：cargo 的 build.rs 经 cbindgen 从 Rust 源码生成 C 头文件
``native/grant-policy/include/unilab_grant_policy.h``，本脚本再调用 ``scripts/gen_grant_bindings.py``
从头文件生成 Python ctypes 绑定 ``unilabos/server/backend/scheduler/grant/_abi.py``。

    python scripts/build_grant_policy.py                  # 编译 + 重新生成，打印产物路径
    python scripts/build_grant_policy.py --check          # CI：生成物与已提交的不一致时失败
    python scripts/build_grant_policy.py --install ~/.unilabos/native
    python scripts/build_grant_policy.py --bundle dist --platform linux-64   # 发布附件 zip + sha256

缺少 cargo 时退出码 2；调度器在找不到原生库时自动使用 Python 参考实现。
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CRATE = ROOT / "native" / "grant-policy"
HEADER = CRATE / "include" / "unilab_grant_policy.h"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gen_grant_bindings  # noqa: E402


def library_filename() -> str:
    if sys.platform == "win32":
        return "unilab_grant_policy.dll"
    if sys.platform == "darwin":
        return "libunilab_grant_policy.dylib"
    return "libunilab_grant_policy.so"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="编译后头文件或 Python 绑定有变化即失败（CI 用）")
    parser.add_argument("--install", type=Path, help="把编译好的库复制到该目录（例如 <working_dir>/native）")
    parser.add_argument("--bundle", type=Path, help="在该目录生成发布附件 unilab-grant-policy-<platform>.zip 与 .sha256")
    parser.add_argument("--platform", default="", help="发布附件的平台名（linux-64 / win-64 / osx-64 / osx-arm64）")
    parser.add_argument("--cargo", default="cargo")
    args = parser.parse_args()
    if shutil.which(args.cargo) is None:
        print("未找到 cargo；跳过原生库编译（调度器会使用 Python 参考实现）", file=sys.stderr)
        return 2
    header_before = HEADER.read_bytes() if HEADER.is_file() else b""
    subprocess.run(
        [args.cargo, "build", "--release", "--locked", "--manifest-path", str(CRATE / "Cargo.toml")],
        check=True,
    )
    if args.check:
        if HEADER.read_bytes() != header_before:
            print(f"{HEADER} 与 Rust 源码不一致：本地编译后提交更新的头文件", file=sys.stderr)
            return 1
        if gen_grant_bindings.main_check() != 0:
            return 1
    else:
        gen_grant_bindings.write()
    built = CRATE / "target" / "release" / library_filename()
    if not built.is_file():
        print(f"编译完成但未找到产物 {built}", file=sys.stderr)
        return 1
    artifact = built
    if args.install:
        target_dir = args.install.expanduser()
        target_dir.mkdir(parents=True, exist_ok=True)
        artifact = target_dir / library_filename()
        shutil.copy2(built, artifact)
    if args.bundle:
        if not args.platform:
            print("--bundle 需要同时给出 --platform", file=sys.stderr)
            return 1
        args.bundle.mkdir(parents=True, exist_ok=True)
        artifact = args.bundle / f"unilab-grant-policy-{args.platform}.zip"
        with zipfile.ZipFile(artifact, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(built, arcname=library_filename())
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        artifact.with_name(artifact.name + ".sha256").write_text(f"{digest}  {artifact.name}\n", encoding="utf-8")
    print(artifact)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
