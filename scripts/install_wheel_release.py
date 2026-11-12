"""安装配套 wheel 发行包：只用标准库，校验后创建新环境，全程不访问包源。"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
import sysconfig
import venv


def platform_id() -> str:
    machine = platform.machine().lower()
    targets = {
        ("win32", "amd64"): "win-64", ("win32", "x86_64"): "win-64",
        ("linux", "x86_64"): "linux-64",
        ("darwin", "x86_64"): "osx-64", ("darwin", "arm64"): "osx-arm64",
    }
    try:
        return targets[sys.platform, machine]
    except KeyError:
        raise RuntimeError(f"此发行包暂不支持 {sys.platform}/{machine}") from None


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def environment_python(prefix: Path) -> Path:
    return prefix / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def verify_bundle(bundle: Path) -> dict:
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or manifest.get("kind") != "offline-wheelhouse":
        raise RuntimeError("不是完整的 wheel 发行包；不能用依赖构建目录代替")
    if (manifest["python"] != "3.14" or sys.version_info[:2] != (3, 14)
            or sysconfig.get_config_var("Py_GIL_DISABLED")):
        raise RuntimeError("请使用普通 CPython 3.14（cp314，非 cp314t）执行安装入口")
    if manifest["platform"] != platform_id():
        raise RuntimeError(f"平台不匹配：此包用于 {manifest['platform']}，当前为 {platform_id()}")
    files = manifest["files"]
    if not {"requirements.lock", "verify_installation.py"} <= files.keys():
        raise RuntimeError("发行包缺少安装清单或验证脚本的校验信息")
    for relative, expected in files.items():
        path = (bundle / relative).resolve()
        if not path.is_relative_to(bundle.resolve()) or not path.is_file():
            raise RuntimeError(f"发行包文件缺失或路径越界：{relative}")
        if sha256(path) != expected:
            raise RuntimeError(f"SHA256 校验失败：{relative}")
    recorded = {name for name in files if name.startswith("wheelhouse/") and name.endswith(".whl")}
    actual = {f"wheelhouse/{path.name}" for path in (bundle / "wheelhouse").iterdir()}
    if not recorded or recorded != actual:
        raise RuntimeError("wheelhouse 含缺失或清单外文件，拒绝安装")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, help="新环境目录，默认是发行包内的 .venv；已有目录不会覆盖")
    args = parser.parse_args()
    bundle = Path(__file__).resolve().parent
    manifest = verify_bundle(bundle)
    prefix = (args.prefix or bundle / ".venv").resolve()
    if prefix.exists():
        raise RuntimeError(f"环境目录已存在，不会覆盖：{prefix}；请指定新的 --prefix")
    venv.EnvBuilder(with_pip=True).create(prefix)
    python = str(environment_python(prefix))
    # -I 与 --isolated 禁止用户 PYTHONPATH / pip 环境配置把安装导向其他环境。
    subprocess.run([python, "-I", "-m", "pip", "--isolated", "install",
                    "--disable-pip-version-check", "--no-index", "--only-binary=:all:",
                    "--find-links", str(bundle / "wheelhouse"), "--require-hashes",
                    "-r", str(bundle / "requirements.lock")], check=True, cwd=bundle)
    subprocess.run([python, "-I", "-m", "pip", "check"], check=True, cwd=bundle)
    subprocess.run([python, "-I", str(bundle / "verify_installation.py"), "--assert-no-ros"],
                   check=True, cwd=bundle)
    subprocess.run([python, "-I", "-m", "unilabos.app.main", "--help"], check=True, cwd=bundle)
    print(f"安装完成：UniLabOS {manifest['version']}，无 ROS，环境 {prefix}")
    print(f'启动："{python}" -m unilabos.app.main --disable_browser')
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        sys.exit(main())
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"安装失败：{exc}", file=sys.stderr)
        sys.exit(1)
