"""Plan 09 / Plan 04: package-management 必须支持"文件夹式"外部注册表
(unilabos_registry/devices/*.yaml)，而不仅是 @device 装饰器与根目录 registry.yaml。

复用 external_variant_pkg fixture（两个变体共享一个 Python class，contracts 经 $ref 复用）。
"""

from pathlib import Path

from unilabos.app.cli.package import (
    inspect_package,
    read_external_registry_devices,
    read_registry_yaml_devices,
)

PKG = Path(__file__).parent / "fixtures" / "external_variant_pkg"


def test_read_external_registry_devices_discovers_folder_layout():
    # 包根没有 registry.yaml —— 旧的根目录读取器应为空
    assert read_registry_yaml_devices(PKG) == {}

    # 新增的文件夹式读取器应发现 devices/ 下的两个变体
    entries = read_external_registry_devices(PKG)
    assert set(entries) == {"vendor.lh.model_a", "vendor.lh.model_b"}

    a, b = entries["vendor.lh.model_a"], entries["vendor.lh.model_b"]
    # 同一个 class，不同 init 参数
    assert a["class"]["module"] == b["class"]["module"]
    assert a["class"]["init"]["kwargs"]["channels"] == 8
    assert b["class"]["init"]["kwargs"]["channels"] == 96
    # $ref 已展开：contracts/liquid_handler.yaml 的 action/status 已并入条目
    assert "setup" in a["class"]["action_value_mappings"]
    assert "initialized" in b["class"]["status_types"]


def test_inspect_package_uses_folder_registry_source(tmp_path):
    info = inspect_package(str(PKG), namespace=None, out_dir=str(tmp_path))

    assert sorted(info["devices"]) == ["vendor.lh.model_a", "vendor.lh.model_b"]
    assert info["class_namespace"] == "community.example_variant_pkg"

    by_id = {r["id"]: r for r in info["resources"]}
    # source_registry 保留各自不同的 class.init（同 class 不同初始化参数）
    init_a = by_id["vendor.lh.model_a"]["source_registry"]["class"]["init"]["kwargs"]
    init_b = by_id["vendor.lh.model_b"]["source_registry"]["class"]["init"]["kwargs"]
    assert init_a["channels"] == 8
    assert init_b["channels"] == 96
