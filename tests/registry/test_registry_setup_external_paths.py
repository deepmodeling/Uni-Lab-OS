"""外部变体 fixture 可由包管理公共入口发现。"""

from pathlib import Path

from unilabos.package_manager.package_distribution.registry_discovery import (
    discover_registry_paths_from_project,
)


def test_external_variant_fixture_registry_path_is_discoverable() -> None:
    """产品外部变体夹具的注册表（Registry）路径必须可由公开发现器读取。

    参数：无；使用仓库固定的外部变体包夹具。
    返回：无；断言发现结果只包含包内声明目录的规范绝对路径。
    异常：夹具项目声明或公开路径发现合同漂移时测试失败。
    """

    # ``project_root`` 是产品注册表设置测试使用的固定外部包来源。
    project_root = Path(__file__).parent / "fixtures" / "external_variant_package"

    # ``paths`` 是该来源完成安全解析后的全部注册表（Registry）目录。
    paths = discover_registry_paths_from_project(project_root)

    assert paths == [(project_root / "unilabos_registry").resolve()]
