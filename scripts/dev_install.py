#!/usr/bin/env python3
"""开发安装入口：默认用 uv 准备 Python 3.14 虚拟环境并可编辑安装，无 uv 时回退 pip；默认不安装 ROS。"""

from __future__ import annotations

import argparse
import locale
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

if __package__:
    from .install_wheel_release import environment_python
else:
    from install_wheel_release import environment_python

TSINGHUA_MIRROR = "https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple"
PYTHON_VERSION = "3.14"
# 在目标解释器里判断：只接受普通 CPython 3.14（cp314，非 free-threaded）。
_SUPPORTED_PROBE = ("import sys, sysconfig; print(sys.implementation.name == 'cpython' "
                    "and sys.version_info[:2] == (3, 14) and not sysconfig.get_config_var('Py_GIL_DISABLED'))")


def uv_install_hint() -> str:
    """当前系统安装 uv 的建议命令（官方安装脚本优先，pip 兜底）。"""
    if sys.platform == "win32":
        official = 'powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"'
    else:
        official = "curl -LsSf https://astral.sh/uv/install.sh | sh"
    return (
        "建议先安装 uv（更快，并会自动准备 CPython 3.14）：\n"
        f"    {official}\n"
        f"    或：{Path(sys.executable).name} -m pip install uv\n"
        "  装好后重新打开终端再运行本脚本，即默认使用 uv。详见 https://docs.astral.sh/uv/"
    )


def resolve_installer(requested: str) -> str:
    """auto：有 uv 用 uv，否则给出 uv 的安装命令后回退 pip；显式 uv 但未安装时报错并给出安装命令。"""
    if requested == "pip":
        return "pip"
    if shutil.which("uv"):
        return "uv"
    if requested == "uv":
        raise RuntimeError("未找到 uv；或改用 --installer pip。" + uv_install_hint())
    print("[INFO] 未找到 uv，改用 pip 安装（较慢，且需要已有 CPython 3.14）。" + uv_install_hint())
    return "pip"


def in_managed_environment() -> bool:
    """当前解释器是否在 venv 或已激活的 Conda 环境里；全局解释器不直接安装。"""
    if sys.prefix != sys.base_prefix:
        return True
    conda_prefix = os.environ.get("CONDA_PREFIX")
    return bool(conda_prefix) and Path(conda_prefix).resolve() == Path(sys.prefix).resolve()


def is_supported_python(python: str) -> bool:
    try:
        result = subprocess.run([python, "-c", _SUPPORTED_PROBE], capture_output=True, text=True)
    except OSError:
        return False
    return result.returncode == 0 and result.stdout.strip() == "True"


def prepare_environment(project_root: Path, installer: str, venv: Path | None, *, mirror: bool) -> str:
    """返回安装目标解释器：显式 --venv > 已激活的 venv / Conda 环境 > 项目根目录 .venv（不存在则新建）。"""
    if venv is None and in_managed_environment():
        if not is_supported_python(sys.executable):
            raise RuntimeError(f"当前环境不是 CPython {PYTHON_VERSION}：{sys.executable}；"
                               f"请换用 {PYTHON_VERSION} 环境，或用 --venv 指定一个新目录")
        return sys.executable
    prefix = (venv or project_root / ".venv").absolute()
    python = str(environment_python(prefix))
    if prefix.exists():
        if not is_supported_python(python):
            raise RuntimeError(f"{prefix} 已存在但不是 CPython {PYTHON_VERSION} 虚拟环境；请删除后重试，或用 --venv 指定新目录")
        print(f"[INFO] 复用已有环境 {prefix}")
        return python
    if installer == "uv":
        # uv 按需下载 CPython 3.14；--seed 带上 pip，供配套 wheel 构建及之后的 pip 回退使用。
        command = ["uv", "venv", "--seed", "--python", f"cpython{PYTHON_VERSION}", str(prefix)]
        if mirror:
            command.extend(["--index-url", TSINGHUA_MIRROR])
    elif is_supported_python(sys.executable):
        command = [sys.executable, "-m", "venv", str(prefix)]
    else:
        raise RuntimeError(f"pip 只能用当前解释器创建环境，而它不是 CPython {PYTHON_VERSION}：{sys.executable}；"
                           f"请用 Python {PYTHON_VERSION} 运行本脚本，或改用 uv（已安装时默认使用）")
    print(f"[INFO] 创建 Python {PYTHON_VERSION} 虚拟环境 {prefix}")
    subprocess.run(command, check=True)
    if not is_supported_python(python):
        raise RuntimeError(f"新建的 {prefix} 不是 CPython {PYTHON_VERSION} 环境")
    return python


def ensure_pip(python: str, installer: str) -> None:
    """配套 wheel 由目标解释器的 pip wheel 构建；uv 未带 --seed 创建的环境没有 pip。"""
    if subprocess.run([python, "-m", "pip", "--version"], capture_output=True).returncode == 0:
        return
    print("[INFO] 目标环境没有 pip，先安装 pip 以构建配套 wheel")
    if installer == "uv":
        subprocess.run(["uv", "pip", "install", "--python", python, "pip"], check=True)
    else:
        subprocess.run([python, "-m", "ensurepip"], check=True)


def build_command(project_root: Path, *, installer: str, python: str, skip_deps: bool, mirror: bool,
                  wheelhouse: Path | None = None, extras: str = "") -> list[str]:
    if installer == "uv":
        command = ["uv", "pip", "install", "--python", python]
    else:
        command = [python, "-m", "pip", "install"]
    requested = list(dict.fromkeys(value.strip() for value in extras.split(",") if value.strip()))
    if set(requested) - {"ros2", "full"}:
        raise ValueError(f"未知的可选依赖组：{extras}")
    target = str(project_root) + (f"[{','.join(requested)}]" if requested else "")
    command.extend(["-e", target])
    if skip_deps:
        command.append("--no-deps")
    if wheelhouse:
        command.extend(["--find-links", str(wheelhouse.resolve())])
    if mirror:
        command.extend(["--index-url", TSINGHUA_MIRROR])
    return command


def check_command(installer: str, python: str) -> list[str]:
    return ["uv", "pip", "check", "--python", python] if installer == "uv" else [python, "-m", "pip", "check"]


def print_next_steps(python: str) -> None:
    if python == sys.executable:
        print("[OK] 安装完成。可运行 unilab --disable-browser")
        return
    prefix = Path(python).parent.parent
    activate = (f"{prefix}\\Scripts\\Activate.ps1" if sys.platform == "win32"
                else f"source {prefix}/bin/activate")
    print(f"[OK] 安装完成，环境：{prefix}")
    print(f"     激活：{activate}")
    print("     启动：unilab --disable-browser")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mirrors = parser.add_mutually_exclusive_group()
    mirrors.add_argument("--china", action="store_true", help="使用清华 Python 包镜像")
    mirrors.add_argument("--no-mirror", action="store_true", help="使用默认 Python 包源")
    parser.add_argument("--skip-deps", action="store_true", help="只安装源码；依赖须已由 Conda 等方式安装")
    parser.add_argument("--installer", choices=("auto", "uv", "pip"), default="auto",
                        help="安装工具；默认 auto：有 uv 用 uv，否则 pip")
    parser.add_argument("--use-pip", action="store_true", help="等同 --installer pip（兼容旧参数）")
    parser.add_argument("--venv", type=Path,
                        help="目标虚拟环境目录，不存在时以 Python 3.14 新建；"
                             "省略时复用已激活的 venv / Conda 环境，否则使用项目根目录 .venv")
    parser.add_argument("--wheelhouse", type=Path, help="已有配套 wheel 目录；省略时从固定源码构建，不上传 PyPI")
    parser.add_argument("--extras", default="", choices=("ros2", "full"),
                        help="可选 ros2 或 full（含文档/测试/开发）；原生 ROS 须另由 Conda 安装")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        # 输出被重定向时也与 uv / pip 子进程的输出保持先后顺序。
        sys.stdout.reconfigure(line_buffering=True)
    if args.use_pip:
        if args.installer == "uv":
            parser.error("--use-pip 与 --installer uv 不能同时使用")
        args.installer = "pip"
    project_root = Path(__file__).resolve().parent.parent
    language = locale.getlocale()[0] or ""
    mirror = not args.no_mirror and (args.china or language.lower().startswith(("zh", "chinese")))
    print("[INFO] 安装默认 HostLink 开发环境；PLR/Opentrons 使用配套 wheel，不在启动时切换分支。")
    try:
        installer = resolve_installer(args.installer)
        python = prepare_environment(project_root, installer, args.venv, mirror=mirror)
        with tempfile.TemporaryDirectory(prefix="unilab-dev-wheels-") as temporary:
            wheelhouse = args.wheelhouse
            if not args.skip_deps and wheelhouse is None:
                ensure_pip(python, installer)
                # 构建脚本要求 CPython 3.14，用目标解释器运行，而不是启动本脚本的解释器。
                subprocess.run([python, str(project_root / "scripts/build_wheel_release.py"),
                                "--dependencies-only", "--output-dir", temporary], check=True)
                wheelhouse = Path(temporary) / "wheelhouse"
            command = build_command(project_root, installer=installer, python=python, skip_deps=args.skip_deps,
                                    mirror=mirror, wheelhouse=wheelhouse, extras=args.extras)
            subprocess.run(command, check=True)
        subprocess.run(check_command(installer, python), check=True)
    except RuntimeError as exc:
        print(f"[ERROR] {exc}")
        return 1
    except subprocess.CalledProcessError as exc:
        print(f"[ERROR] 安装或依赖验证失败，退出码 {exc.returncode}。请检查上述输出。")
        return exc.returncode
    print_next_steps(python)
    return 0


if __name__ == "__main__":
    sys.exit(main())
