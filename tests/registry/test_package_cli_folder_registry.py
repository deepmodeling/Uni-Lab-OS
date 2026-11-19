"""Package inspection for folder-layout external registries.

The fixture defines two device variants that share a Python class and a
contract referenced through ``$ref``.
"""

from pathlib import Path

from unilabos.app.cli.package import (
    inspect_package,
    read_external_registry_devices,
    read_registry_yaml_devices,
)

PKG = Path(__file__).parent / "fixtures" / "external_variant_pkg"


def test_read_external_registry_devices_discovers_folder_layout():
    # The fixture intentionally has no root registry.yaml.
    assert read_registry_yaml_devices(PKG) == {}

    # The folder reader discovers both entries under devices/.
    entries = read_external_registry_devices(PKG)
    assert set(entries) == {"vendor.lh.model_a", "vendor.lh.model_b"}

    a, b = entries["vendor.lh.model_a"], entries["vendor.lh.model_b"]
    # Variants share a class but retain distinct initialization parameters.
    assert a["class"]["module"] == b["class"]["module"]
    assert a["class"]["init"]["kwargs"]["channels"] == 8
    assert b["class"]["init"]["kwargs"]["channels"] == 96
    # Shared contract actions and status fields are expanded from $ref.
    assert "setup" in a["class"]["action_value_mappings"]
    assert "initialized" in b["class"]["status_types"]


def test_inspect_package_uses_folder_registry_source(tmp_path):
    info = inspect_package(str(PKG), namespace=None, out_dir=str(tmp_path))

    assert sorted(info["devices"]) == ["vendor.lh.model_a", "vendor.lh.model_b"]
    assert info["class_namespace"] == "community.example_variant_pkg"

    by_id = {r["id"]: r for r in info["resources"]}
    # Package metadata retains each variant's class.init values.
    init_a = by_id["vendor.lh.model_a"]["source_registry"]["class"]["init"]["kwargs"]
    init_b = by_id["vendor.lh.model_b"]["source_registry"]["class"]["init"]["kwargs"]
    assert init_a["channels"] == 8
    assert init_b["channels"] == 96
