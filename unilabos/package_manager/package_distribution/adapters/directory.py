"""Python 包来源安装与安装后设备定义发现 Adapter。"""

from __future__ import annotations

import importlib.util
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

from unilabos.registry.ast_registry_scanner import scan_directory
from unilabos.utils import logger
from unilabos.utils.banner_print import print_status

from ..archive import ARCHIVE_EXCLUDE_DIRS
from ..errors import PackageCLIError
from ..registry_discovery import read_pyproject


def install_package(spec: str, run_inspect: bool = True) -> dict[str, object]:
    """安装一个本地或远端 Python 软件包。

    参数：``spec`` 是 pip 规格、Git 地址或本地路径；``run_inspect``
    决定安装后是否静态列出设备定义。
    返回：安装规格、安装器、可识别分发名和设备身份列表。
    异常：规格缺失或所有安装器失败时抛出 ``PackageCLIError``。
    """

    # ``selected_spec`` 是去除外围空白后的唯一安装来源声明。
    selected_spec = (spec or "").strip()
    if not selected_spec:
        raise PackageCLIError(
            "缺少安装目标，用法：unilab package install <pip-spec 或 git-url>"
        )

    # ``installer`` 标识实际成功完成安装的环境工具。
    installer = _run_pip_install(selected_spec)
    print_status(f"package install 完成：{selected_spec}（{installer}）", "info")
    # ``distribution_name`` 是安装后可寻址的 Python 分发身份。
    distribution_name = _spec_dist_name(selected_spec) or _local_dist_name(
        selected_spec
    )
    # ``device_ids`` 是安装后静态发现的设备定义身份集合，不包含运行实例。
    device_ids: list[str] = []
    if run_inspect and distribution_name:
        device_ids = _installed_device_ids(distribution_name)

    if device_ids:
        print_status(f"  包内可用设备    : {', '.join(device_ids)}", "info")
    elif distribution_name:
        print_status(
            f"  已安装分发      : {distribution_name}（未扫描到 @device）",
            "info",
        )
    else:
        print_status("  已安装（无法确定分发名，跳过设备扫描）", "info")
    return {
        "spec": selected_spec,
        "installer": installer,
        "dist_name": distribution_name,
        "device_ids": device_ids,
    }


def _run_pip_install(spec: str) -> str:
    """用产品统一安装器候选链安装 Python 规格。

    参数：``spec`` 是非空 pip 安装规格。
    返回：实际成功使用的安装器名。
    异常：全部候选安装器缺失、超时或返回失败时抛出
    ``PackageCLIError``。
    """

    from unilabos.utils.environment_check import (
        _install_command,
        _installer_candidates,
        _is_chinese_locale,
    )

    # ``chinese_locale`` 决定现有环境工具生成安装命令时采用的镜像策略。
    chinese_locale = _is_chinese_locale()
    # ``last_error`` 保存候选安装器链最后一项可诊断失败信息。
    last_error = ""
    for installer_kind in _installer_candidates():
        # ``installer_name`` 是返回给调用者和状态输出的稳定工具名称。
        installer_name = "uv pip install" if installer_kind == "uv" else "pip install"
        # ``command`` 是环境工具为当前来源和区域策略生成的实际子进程参数。
        command = _install_command(
            installer_kind,
            spec,
            False,
            chinese_locale,
        )
        print_status(f"尝试安装：{installer_name} {spec}", "info")
        try:
            process = subprocess.run(
                command,
                capture_output=True,
                check=False,
                text=True,
                timeout=600,
            )
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            last_error = "timeout after 600s"
            continue
        if process.returncode == 0:
            return installer_name
        last_error = (process.stderr or process.stdout or "").strip()
    raise PackageCLIError(f"安装失败：{spec}\n{last_error}")


def _spec_dist_name(spec: str) -> str:
    """从可静态识别的 pip 规格中提取分发名。

    参数：``spec`` 是安装规格。
    返回：分发名；Git、URL 和本地路径返回空字符串。
    异常：无。
    """

    # ``normalized_spec`` 是用于识别来源种类的无外围空白规格。
    normalized_spec = spec.strip()
    if normalized_spec.startswith(("git+", "http://", "https://", "file:", ".", "/")):
        return ""
    # ``match`` 只接受 pip 发行规格开头可移植的发行名字符集。
    match = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", normalized_spec)
    return match.group(1) if match else ""


def _local_dist_name(spec: str) -> str:
    """从本地安装规格的项目元数据提取分发名。

    参数：``spec`` 是安装规格。
    返回：本地项目分发名；非本地或不可读来源返回空字符串。
    异常：项目解析错误被兼容为空结果，不向外传播。
    """

    # ``normalized_spec`` 是完成空白归一化、尚未解释来源种类的安装声明。
    normalized_spec = spec.strip()
    if normalized_spec.startswith(("git+", "http://", "https://")):
        return ""
    # 去掉 file 协议后，该值只用于解析本地文件系统来源，不再作为 pip 身份。
    normalized_spec = normalized_spec.removeprefix("file:")
    # ``selected_path`` 是本地安装规格解析后的文件系统候选。
    selected_path = Path(normalized_spec).expanduser()
    if not selected_path.exists():
        return ""
    # ``package_root`` 是读取 pyproject.toml 所需的软件包目录。
    package_root = selected_path if selected_path.is_dir() else selected_path.parent
    try:
        return str(read_pyproject(package_root).get("name") or "").strip()
    except PackageCLIError:
        return ""


def _installed_device_ids(distribution_name: str) -> list[str]:
    """静态扫描已安装 Python 分发的设备定义身份。

    参数：``distribution_name`` 是已安装 Python 分发身份。
    返回：按身份排序的设备定义列表；分发不可读时返回空列表。
    异常：发现阶段的环境错误被记录并兼容为空结果。
    """

    try:
        # ``installed_distribution`` 是环境元数据中匹配发行身份的只读记录。
        installed_distribution = distribution(distribution_name)
    except PackageNotFoundError:
        return []
    except (OSError, ValueError) as error:
        logger.warning(f"[package] 读取已安装分发失败: {distribution_name}, {error}")
        return []

    # ``top_modules`` 是发行声明或文件表推导出的候选顶层 Python 包集合。
    top_modules: list[str] = []
    try:
        top_text = installed_distribution.read_text("top_level.txt") or ""
        top_modules = [line.strip() for line in top_text.splitlines() if line.strip()]
    except (OSError, UnicodeError):
        top_modules = []
    if not top_modules:
        # ``inferred_modules`` 从发行文件表收集不依赖导入执行的顶层包候选。
        inferred_modules: set[str] = set()
        for entry in installed_distribution.files or []:
            parts = entry.parts
            if not parts:
                continue
            head = parts[0]
            if head in {"..", "__pycache__"} or head.endswith((".dist-info", ".data")):
                continue
            if len(parts) == 1 and head.endswith(".py"):
                inferred_modules.add(head[:-3])
            elif len(parts) > 1 and "." not in head:
                inferred_modules.add(head)
        top_modules = sorted(inferred_modules) or [distribution_name.replace("-", "_")]

    # ``scan_files`` 是已安装分发允许静态观察的 Python 文件。
    scan_files: list[Path] = []
    for module_name in top_modules:
        try:
            module_spec = importlib.util.find_spec(module_name)
        except (ImportError, ValueError):
            continue
        if module_spec is None:
            continue
        if module_spec.submodule_search_locations:
            for location in module_spec.submodule_search_locations:
                location_path = Path(location)
                if not location_path.is_dir():
                    continue
                scan_files.extend(
                    source_file
                    for source_file in location_path.rglob("*.py")
                    if not source_file.name.startswith("__")
                    and not (
                        set(source_file.relative_to(location_path).parts)
                        & ARCHIVE_EXCLUDE_DIRS
                    )
                )
        elif module_spec.origin and module_spec.origin.endswith(".py"):
            scan_files.append(Path(module_spec.origin))

    if not scan_files:
        return []
    # ``executor`` 只并行静态 AST 扫描，不执行作者模块或设备代码。
    executor = ThreadPoolExecutor(
        max_workers=8,
        thread_name_prefix="PackageInstallScan",
    )
    try:
        # ``result`` 是扫描器返回的设备与资源定义只读索引。
        result = scan_directory(
            scan_files[0].parent,
            executor=executor,
            include_files=scan_files,
        )
    finally:
        executor.shutdown(wait=True)
    # ``devices`` 是扫描结果中的设备定义映射，不代表设备运行实例。
    devices = result.get("devices", {})
    return sorted(
        device_id
        for device_id, metadata in devices.items()
        if isinstance(metadata, dict)
    )


__all__ = ["install_package"]
