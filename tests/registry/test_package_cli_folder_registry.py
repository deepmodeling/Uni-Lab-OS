"""Plan 09 / Plan 04: package-management 必须支持"文件夹式"外部注册表
(unilabos_registry/devices/*.yaml)，而不仅是 @device 装饰器与根目录 registry.yaml。

复用 external_variant_pkg fixture（两个变体共享一个 Python class，contracts 经 $ref 复用）。
"""

from pathlib import Path

from unilabos.package_manager import inspect_package
from unilabos.package_manager.package_distribution.registry_discovery import (
    read_external_registry_devices,
    read_registry_yaml_devices,
)

# ``PKG`` 是包含两个设备定义变体和共享 ``$ref`` 合同的固定外部包夹具。
PKG = Path(__file__).parent / "fixtures" / "external_variant_pkg"


def test_read_external_registry_devices_discovers_folder_layout() -> None:
    """目录式注册表（Registry）必须发现并展开两个设备定义变体。

    参数：无；使用 ``PKG`` 固定外部包夹具。
    返回：无；断言根 YAML 不产生结果、目录读取保留共享驱动和各变体强制参数。
    异常：目录发现、``$ref`` 展开或设备定义投影漂移时测试失败。
    """

    # 包根没有 registry.yaml —— 旧的根目录读取器应为空
    assert read_registry_yaml_devices(PKG) == {}

    # 新增的文件夹式读取器应发现 devices/ 下的两个变体
    registry_entries = read_external_registry_devices(PKG)
    assert set(registry_entries) == {"vendor.lh.model_a", "vendor.lh.model_b"}

    # 两项分别是同一驱动类下的八通道与九十六通道设备定义变体。
    model_a_entry = registry_entries["vendor.lh.model_a"]
    model_b_entry = registry_entries["vendor.lh.model_b"]
    # 同一个 class，不同注册表强制参数
    assert model_a_entry["class"]["module"] == model_b_entry["class"]["module"]
    assert model_a_entry["init_param_enforce"]["channels"] == 8
    assert model_b_entry["init_param_enforce"]["channels"] == 96
    # $ref 已展开：contracts/liquid_handler.yaml 的 action/status 已并入条目
    assert "setup" in model_a_entry["class"]["action_value_mappings"]
    assert "initialized" in model_b_entry["class"]["status_types"]


def test_inspect_package_uses_folder_registry_source(tmp_path: Path) -> None:
    """软件包检查（Package Inspect）必须消费目录式注册表定义来源。

    参数：``tmp_path`` 提供隔离检查产物目录。
    返回：无；断言检查结果保留两个设备定义身份、社区命名空间和各自强制参数。
    异常：检查编排回退旧扫描、丢失来源投影或合并设备变体时测试失败。
    """

    # ``inspection`` 是根公开 Interface 对固定外部包生成的完整检查产物。
    inspection = inspect_package(str(PKG), namespace=None, out_dir=str(tmp_path))

    assert sorted(inspection["devices"]) == [
        "vendor.lh.model_a",
        "vendor.lh.model_b",
    ]
    assert inspection["class_namespace"] == "community.example_variant_pkg"

    # ``resources_by_definition_id`` 按稳定设备定义身份索引遗留发布资源投影。
    resources_by_definition_id = {
        resource["id"]: resource for resource in inspection["resources"]
    }
    # source_registry 保留各自不同的 init_param_enforce
    # 两项强制参数分别证明设备定义变体未被同一驱动类错误合并。
    model_a_enforced_params = resources_by_definition_id["vendor.lh.model_a"][
        "source_registry"
    ]["init_param_enforce"]
    model_b_enforced_params = resources_by_definition_id["vendor.lh.model_b"][
        "source_registry"
    ]["init_param_enforce"]
    assert model_a_enforced_params["channels"] == 8
    assert model_b_enforced_params["channels"] == 96
