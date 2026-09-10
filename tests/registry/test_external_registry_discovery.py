"""社区设备包目录式注册表发现合同。"""

from pathlib import Path

from unilabos.package_manager.package_distribution.registry_discovery import (
    discover_registry_paths_from_project,
)


def test_discover_registry_paths_from_pyproject_tool_section() -> None:
    """项目显式声明的注册表（Registry）路径必须成为唯一发现结果。

    参数：无；使用仓库固定的外部变体包夹具。
    返回：无；断言项目声明路径按规范绝对路径返回。
    异常：项目元数据解析或注册表路径发现语义漂移时测试失败。
    """

    # ``project_root`` 是显式声明注册表（Registry）路径的固定外部包来源。
    project_root = Path(__file__).parent / "fixtures" / "external_variant_package"

    # ``paths`` 是该包被授权并实际存在的注册表（Registry）目录集合。
    paths = discover_registry_paths_from_project(project_root)

    assert paths == [(project_root / "unilabos_registry").resolve()]


def test_discover_registry_paths_falls_back_to_unilabos_registry_directory(
    tmp_path: Path,
) -> None:
    """未显式声明时只回退到包内默认注册表（Registry）目录。

    参数：``tmp_path`` 提供没有项目路径声明的隔离软件包根。
    返回：无；断言存在的默认目录以规范绝对路径返回。
    异常：回退扫描越过包根或漏掉默认目录时测试失败。
    """

    # ``registry_directory`` 是隔离包根内唯一允许采用的默认注册表目录。
    registry_directory = tmp_path / "unilabos_registry"
    registry_directory.mkdir()

    # ``paths`` 是默认规则发现的完整注册表（Registry）目录集合。
    paths = discover_registry_paths_from_project(tmp_path)

    assert paths == [registry_directory.resolve()]


def test_discover_registry_paths_returns_empty_when_no_registry_exists(
    tmp_path: Path,
) -> None:
    """包内没有注册表（Registry）目录时发现结果必须为空。

    参数：``tmp_path`` 提供没有项目声明和默认目录的隔离软件包根。
    返回：无；断言发现器关闭式返回空集合。
    异常：发现器扫描环境或编造不存在目录时测试失败。
    """

    # ``paths`` 是空包根的完整注册表（Registry）发现结果。
    paths = discover_registry_paths_from_project(tmp_path)

    assert paths == []
