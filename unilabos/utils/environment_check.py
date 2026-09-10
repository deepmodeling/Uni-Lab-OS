"""
环境检查模块
用于检查并自动安装 UniLabOS 运行所需的 Python 包
"""

import argparse
import importlib
import locale
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from unilabos.utils.banner_print import print_status
from unilabos.utils.log import logger


# ---------------------------------------------------------------------------
# 底层安装工具
# ---------------------------------------------------------------------------

def _is_chinese_locale() -> bool:
    try:
        lang = locale.getdefaultlocale()[0]
        return bool(lang and ("zh" in lang.lower() or "chinese" in lang.lower()))
    except Exception:
        return False


_USE_UV: Optional[bool] = None


def _has_uv() -> bool:
    global _USE_UV
    if _USE_UV is None:
        uv_path = shutil.which("uv")
        if not uv_path:
            _USE_UV = False
        else:
            try:
                result = subprocess.run([uv_path, "--version"], capture_output=True, text=True, timeout=10)
                _USE_UV = result.returncode == 0
            except Exception:
                _USE_UV = False
    return _USE_UV


def _install_command(installer: str, package: str, upgrade: bool, is_chinese: bool) -> List[str]:
    if installer == "uv":
        # uv >= 0.5 默认要求虚拟环境，对 conda env 会报 "No virtual environment found"。
        # 显式 --python sys.executable 让 uv 把当前解释器（conda/venv/system 都行）
        # 视为目标环境，绕开 venv 检测。
        cmd = ["uv", "pip", "install", "--python", sys.executable]
        if upgrade:
            cmd.append("--upgrade")
        cmd.append(package)
        if is_chinese:
            cmd.extend(["--index-url", "https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple"])
        return cmd

    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check"]
    if upgrade:
        cmd.append("--upgrade")
    cmd.append(package)
    if is_chinese:
        cmd.extend(["-i", "https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple"])
    return cmd


def _installer_candidates() -> List[str]:
    installers: List[str] = []
    if _has_uv():
        installers.append("uv")
    installers.append("pip")
    return installers


def _git_url_from_requirement(requirement: str) -> Optional[str]:
    if not requirement.startswith("git+"):
        return None
    return requirement[4:].split("#", 1)[0]


def _repo_dir_name(git_url: str) -> str:
    repo_name = git_url.rstrip("/").rsplit("/", 1)[-1]
    return repo_name[:-4] if repo_name.endswith(".git") else repo_name


def _print_manual_git_install_hint(requirement: str) -> None:
    git_url = _git_url_from_requirement(requirement)
    if not git_url:
        return

    repo_dir = _repo_dir_name(git_url)
    install_cmd = (
        f'uv pip install --python "{sys.executable}" -e .'
        if _has_uv()
        else f"{sys.executable} -m pip install -e ."
    )
    if _is_chinese_locale() and not _has_uv():
        install_cmd += " -i https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple"

    print_status("Git 依赖自动安装失败，通常是网络连接被重置或代码托管站点暂时不可达。", "warning")
    print_status("可以手动拉取代码后在本地安装:", "warning")
    print_status(f"  git clone {git_url}", "warning")
    print_status(f"  cd {repo_dir}", "warning")
    print_status("  git pull", "warning")
    print_status(f"  {install_cmd}", "warning")
    print_status(f"如果目录 {repo_dir} 已存在，直接进入该目录执行 git pull 后再安装。", "warning")
    print_status("如果 git clone 仍失败，请切换网络/代理，或从浏览器下载源码后进入源码目录执行本地安装命令。", "warning")


def _install_packages(
    packages: List[str],
    upgrade: bool = False,
    label: str = "",
) -> bool:
    """
    安装/升级一组包。优先 uv pip install，回退 sys pip。
    逐个安装，任意一个失败不影响后续包。

    Returns:
        True if all succeeded, False otherwise.
    """
    if not packages:
        return True

    is_chinese = _is_chinese_locale()
    installers = _installer_candidates()
    failed: List[str] = []

    for pkg in packages:
        action_word = "升级" if upgrade else "安装"
        if label:
            print_status(f"[{label}] 正在{action_word} {pkg}...", "info")
        else:
            print_status(f"正在{action_word} {pkg}...", "info")

        pkg_installed = False
        last_error = "unknown error"

        for installer in installers:
            cmd = _install_command(installer, pkg, upgrade, is_chinese)
            logger.trace(f"[install]{(' [' + label + ']') if label else ''} exec: {' '.join(cmd)}")
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
                if result.returncode == 0:
                    print_status(f"✓ {pkg} {action_word}成功 (via {installer})", "success")
                    logger.trace(
                        f"[install] ok: {pkg} via {installer} (rc=0)\n"
                        f"{(result.stdout or '').strip()[-2000:]}"
                    )
                    pkg_installed = True
                    break

                last_error = result.stderr.strip().split("\n")[-1] if result.stderr else "unknown error"
                print_status(f"× {pkg} {action_word}失败 (via {installer}): {last_error}", "warning")
                logger.trace(
                    f"[install] fail: {pkg} via {installer} (rc={result.returncode})\n"
                    f"STDOUT:\n{(result.stdout or '').strip()[-2000:]}\n"
                    f"STDERR:\n{(result.stderr or '').strip()[-2000:]}"
                )
            except subprocess.TimeoutExpired:
                last_error = "timeout after 300s"
                print_status(f"× {pkg} {action_word}超时 (via {installer}, 300s)", "warning")
                logger.trace(f"[install] timeout: {pkg} via {installer} (300s) cmd={' '.join(cmd)}")
            except Exception as e:
                last_error = str(e)
                print_status(f"× {pkg} {action_word}异常 (via {installer}): {e}", "warning")
                logger.trace(f"[install] error: {pkg} via {installer}: {e!r} cmd={' '.join(cmd)}")

        if not pkg_installed:
            print_status(f"× {pkg} {action_word}失败: {last_error}", "error")
            _print_manual_git_install_hint(pkg)
            failed.append(pkg)

    if failed:
        print_status(f"有 {len(failed)} 个包操作失败: {', '.join(failed)}", "error")
        return False
    return True


# ---------------------------------------------------------------------------
# requirements.txt 安装（可多次调用）
# ---------------------------------------------------------------------------

def _requirement_import_name(requirement: str) -> str:
    """从 PEP 508 依赖声明取用于 import 探测的模块名（hyphen→underscore）。

    git+/URL/本地路径无法可靠探测，返回空串，由调用方当作"必须交给安装器"处理。
    正则只取首段分发名，天然覆盖 ~= != === >= 等各种约束、extras 与环境标记。
    """
    s = requirement.strip()
    if s.startswith(("git+", "http://", "https://", "file:", "-", ".", "/")):
        return ""
    match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", s)
    return (match.group(1) if match else "").replace("-", "_")


def install_requirements_list(reqs: List[str], label: str = "") -> bool:
    """
    检查一组 PEP 508 依赖声明（按 import 名探测缺失）并安装缺失项。

    requirements.txt 与社区包 pyproject [project].dependencies 共用这套缺失检测 + 安装逻辑
    （uv 优先回退 pip，中文 locale 自动走清华源）。

    Returns:
        True if all ok, False if any install failed.
    """
    reqs = [str(r).strip() for r in (reqs or []) if str(r).strip()]
    if not reqs:
        return True

    tag = label or "deps"
    logger.trace(f"[{tag}] 依赖解析输入 ({len(reqs)}): {reqs}")
    missing: List[str] = []
    for req in reqs:
        pkg_import = _requirement_import_name(req)
        if not pkg_import:
            missing.append(req)  # git/URL/本地路径无法探测，始终交给安装器
            continue
        try:
            importlib.import_module(pkg_import)
        except ImportError:
            missing.append(req)

    if not missing:
        print_status(f"[{tag}] ✓ 依赖检查通过 ({len(reqs)} 个包)", "success")
        logger.trace(f"[{tag}] 全部依赖已满足，无需安装: {reqs}")
        return True

    print_status(f"[{tag}] 缺失 {len(missing)} 个依赖: {', '.join(missing)}", "warning")
    logger.trace(f"[{tag}] 待安装缺失依赖 ({len(missing)}): {missing}")
    ok = _install_packages(missing, label=tag)
    # 运行时新装的包必须刷新 import 缓存：进程启动时 FileFinder 已缓存各 site-packages
    importlib.invalidate_caches()
    return ok


def install_requirements_txt(req_path: str | Path, label: str = "") -> bool:
    """
    读取一个 requirements.txt 文件，检查缺失的包并安装。

    Args:
        req_path: requirements.txt 文件路径
        label: 日志前缀标签（如 "device_package_sim"）

    Returns:
        True if all ok, False if any install failed.
    """
    req_path = Path(req_path)
    if not req_path.exists():
        return True

    tag = label or req_path.parent.name
    print_status(f"[{tag}] 检查依赖: {req_path}", "info")

    reqs: List[str] = []
    with open(req_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and not line.startswith("-"):
                reqs.append(line)

    return install_requirements_list(reqs, label=tag)


def check_device_package_requirements(devices_dirs: list[str]) -> bool:
    """
    检查 --devices 指定的所有外部设备包目录中的 requirements.txt。
    对每个目录查找 requirements.txt（先在目录内找，再在父目录找）。
    """
    if not devices_dirs:
        return True

    all_ok = True
    for d in devices_dirs:
        d_path = Path(d).resolve()
        req_file = d_path / "requirements.txt"
        if not req_file.exists():
            req_file = d_path.parent / "requirements.txt"
        if not req_file.exists():
            continue
        if not install_requirements_txt(req_file, label=d_path.name):
            all_ok = False

    return all_ok


# ---------------------------------------------------------------------------
# UniLabOS 核心环境检查
# ---------------------------------------------------------------------------

class EnvironmentChecker:
    """环境检查器"""

    def __init__(self):
        self.required_packages = {
            "websockets": "websockets",
            "msgcenterpy": "msgcenterpy",
            "orjson": "orjson",
            "opentrons_shared_data": "opentrons_shared_data",
            "typing_extensions": "typing_extensions",
            "crcmod": "crcmod-plus",
        }

        # 中文 locale 下走 Gitee 镜像，规避 GitHub 拉取失败
        pylabrobot_url = (
            "git+https://gitee.com/xuwznln/pylabrobot.git"
            if _is_chinese_locale()
            else "git+https://github.com/Xuwznln/pylabrobot.git"
        )
        self.special_packages = {"pylabrobot": pylabrobot_url}

        self.version_requirements = {
            "msgcenterpy": "0.1.8",
        }

        self.missing_packages: List[tuple] = []
        self.failed_installs: List[tuple] = []
        self.packages_need_upgrade: List[tuple] = []

    def check_package_installed(self, package_name: str) -> bool:
        try:
            importlib.import_module(package_name)
            return True
        except ImportError:
            return False

    def get_package_version(self, package_name: str) -> str | None:
        try:
            module = importlib.import_module(package_name)
            return getattr(module, "__version__", None)
        except (ImportError, AttributeError):
            return None

    def compare_version(self, current: str, required: str) -> bool:
        try:
            current_parts = [int(x) for x in current.split(".")]
            required_parts = [int(x) for x in required.split(".")]
            max_len = max(len(current_parts), len(required_parts))
            current_parts.extend([0] * (max_len - len(current_parts)))
            required_parts.extend([0] * (max_len - len(required_parts)))
            return current_parts >= required_parts
        except Exception:
            return True

    def check_all_packages(self) -> bool:
        print_status("开始检查环境依赖...", "info")

        for import_name, pip_name in self.required_packages.items():
            if not self.check_package_installed(import_name):
                self.missing_packages.append((import_name, pip_name))
            elif import_name in self.version_requirements:
                current_version = self.get_package_version(import_name)
                required_version = self.version_requirements[import_name]
                if current_version and not self.compare_version(current_version, required_version):
                    print_status(
                        f"{import_name} 版本过低 (当前: {current_version}, 需要: >={required_version})",
                        "warning",
                    )
                    self.packages_need_upgrade.append((import_name, pip_name))

        for package_name, install_url in self.special_packages.items():
            if not self.check_package_installed(package_name):
                self.missing_packages.append((package_name, install_url))

        all_ok = not self.missing_packages and not self.packages_need_upgrade

        if all_ok:
            print_status("✓ 所有依赖包检查完成，环境正常", "success")
            return True

        if self.missing_packages:
            print_status(f"发现 {len(self.missing_packages)} 个缺失的包", "warning")
        if self.packages_need_upgrade:
            print_status(f"发现 {len(self.packages_need_upgrade)} 个需要升级的包", "warning")

        return False

    def install_missing_packages(self, auto_install: bool = True) -> bool:
        if not self.missing_packages and not self.packages_need_upgrade:
            return True

        if not auto_install:
            if self.missing_packages:
                print_status("缺失以下包:", "warning")
                for import_name, pip_name in self.missing_packages:
                    print_status(f"  - {import_name} ({pip_name})", "warning")
            if self.packages_need_upgrade:
                print_status("需要升级以下包:", "warning")
                for import_name, pip_name in self.packages_need_upgrade:
                    print_status(f"  - {import_name} ({pip_name})", "warning")
            return False

        if self.missing_packages:
            pkgs = [pip_name for _, pip_name in self.missing_packages]
            if not _install_packages(pkgs, label="unilabos"):
                self.failed_installs.extend(self.missing_packages)

        if self.packages_need_upgrade:
            pkgs = [pip_name for _, pip_name in self.packages_need_upgrade]
            if not _install_packages(pkgs, upgrade=True, label="unilabos"):
                self.failed_installs.extend(self.packages_need_upgrade)

        return not self.failed_installs

    def verify_installation(self) -> bool:
        if not self.missing_packages and not self.packages_need_upgrade:
            return True

        print_status("验证安装结果...", "info")
        failed_verification = []

        for import_name, pip_name in self.missing_packages:
            if not self.check_package_installed(import_name):
                failed_verification.append((import_name, pip_name))

        for import_name, pip_name in self.packages_need_upgrade:
            if not self.check_package_installed(import_name):
                failed_verification.append((import_name, pip_name))
            elif import_name in self.version_requirements:
                current_version = self.get_package_version(import_name)
                required_version = self.version_requirements[import_name]
                if current_version and not self.compare_version(current_version, required_version):
                    failed_verification.append((import_name, pip_name))
                    print_status(
                        f"  {import_name} 版本仍然过低 (当前: {current_version}, 需要: >={required_version})",
                        "error",
                    )

        if failed_verification:
            print_status(f"有 {len(failed_verification)} 个包验证失败:", "error")
            for import_name, pip_name in failed_verification:
                print_status(f"  - {import_name}", "error")
            return False

        print_status("✓ 所有包验证通过", "success")
        return True


def check_environment(auto_install: bool = True, show_details: bool = True) -> bool:
    """
    检查环境并自动安装缺失的包

    Args:
        auto_install: 是否自动安装缺失的包
        show_details: 是否显示详细信息

    Returns:
        bool: 环境检查是否通过
    """
    checker = EnvironmentChecker()

    if checker.check_all_packages():
        return True

    if not checker.install_missing_packages(auto_install):
        if show_details:
            print_status("请手动安装缺失的包后重新启动程序", "error")
        return False

    if not checker.verify_installation():
        if show_details:
            print_status("安装验证失败，请检查网络连接或手动安装", "error")
        return False

    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="UniLabOS 环境依赖检查工具")
    parser.add_argument("--no-auto-install", action="store_true", help="仅检查环境，不自动安装缺失的包")
    parser.add_argument("--silent", action="store_true", help="静默模式，不显示详细信息")

    args = parser.parse_args()

    auto_install = not args.no_auto_install
    show_details = not args.silent

    success = check_environment(auto_install=auto_install, show_details=show_details)

    if not success:
        if show_details:
            print_status("环境检查失败", "error")
        sys.exit(1)
    else:
        if show_details:
            print_status("环境检查完成", "success")
