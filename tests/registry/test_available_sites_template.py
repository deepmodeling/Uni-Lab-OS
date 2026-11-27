"""资源模板（ResourceTemplate）库位（Site）声明的 Registry 契约测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from unilabos.registry.ast_registry_scanner import _parse_file
from unilabos.registry.decorators import (
    device,
    get_device_meta,
    get_resource_meta,
    resource,
)
from unilabos.registry.template_snapshot import RegistryTemplateSnapshot
from unilabos.resources.site_definition import normalize_available_sites

INITIAL_TEMPLATE_UUID = "aaaaaaaa-0000-4000-8000-000000000001"
OTHER_TEMPLATE_UUID = "10000000-0000-4000-8000-000000000002"


NESTED_SITE_DEFINITIONS = [
    {
        "index": "A1",
        "label": "反应瓶位",
        "position": {"x": 1, "y": 2, "z": 3},
        "rotation": {"x": 4, "y": 5, "z": 6},
        "size": {"width": 7, "height": 8, "depth": 9},
        "content_type": ["bottle", "bottle"],
        "parent_link": "deck",
    }
]


def test_public_registry_does_not_publish_uninstalled_bundled_drivers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys
    from importlib.machinery import PathFinder

    from unilabos.config.config import BasicConfig
    from unilabos.registry.registry import lab_registry

    original = PathFinder.find_spec
    modules = tmp_path / "unilabos"
    (modules / "devices" / "virtual").mkdir(parents=True)

    def find_spec(name, path=None, target=None):
        if name == "unilabos" and path is None:
            return original(name, [str(tmp_path)], target)
        return original(name, path, target)

    monkeypatch.setattr(PathFinder, "find_spec", staticmethod(find_spec))
    monkeypatch.setattr(BasicConfig, "working_dir", str(tmp_path))
    monkeypatch.setattr(BasicConfig, "enable_resource_load", True)
    monkeypatch.setattr(sys, "path", sys.path.copy())
    for _ in range(2):
        registry = type(lab_registry)()
        registry.setup()
        assert "host_node" in registry.device_type_registry
        assert "cameracontroller_device" not in registry.device_type_registry
        assert "virtual_sample_demo" not in registry.device_type_registry
        assert "hplc_plate" not in registry.resource_type_registry
        assert registry.resource_type_registry
        assert all(
            not str(entry.get("class", {}).get("module", "")).startswith("unilabos.devices.")
            for entries in (registry.device_type_registry, registry.resource_type_registry)
            for entry in entries.values()
        )
        assert (tmp_path / "registry_cache.pkl").is_file()


class _RegistryFixture:
    """向模板快照提供已经规范化的设备与器材模板定义。"""

    def obtain_registry_device_info(self) -> list[dict[str, Any]]:
        """返回包含一个库位定义的设备模板。

        参数：无。返回：用于资源模板（ResourceTemplate）快照编译的设备定义列表。
        """

        return [
            {
                "id": "site_device",
                "displayname": "库位设备",
                "class": {},
                "available_sites": NESTED_SITE_DEFINITIONS,
            }
        ]

    def obtain_registry_resource_info(self) -> list[dict[str, Any]]:
        """返回不拥有库位的器材模板。

        参数：无。返回：用于确认空库位数组稳定输出的器材模板定义列表。
        """

        return [{"id": "plain_resource", "class": {}}]


def test_normalize_available_sites_flattens_template_geometry() -> None:
    """嵌套几何应规范化为 Backend 接受的扁平库位模板字段。

    参数：无。返回：无；断言规范化结果不包含任何实例身份或库位占用
    （SiteOccupancy）字段，并按大小写不敏感规则去重允许物料类型。
    """

    sites = normalize_available_sites(NESTED_SITE_DEFINITIONS)

    assert sites == [
        {
            "schema_version": 1,
            "index": "A1",
            "label": "反应瓶位",
            "visible": True,
            "position_x": 1.0,
            "position_y": 2.0,
            "position_z": 3.0,
            "width": 7.0,
            "length": 8.0,
            "depth": 9.0,
            "rotation_x": 4.0,
            "rotation_y": 5.0,
            "rotation_z": 6.0,
            "content_type": ["bottle"],
            "allowed_resource_template_uuids": [],
            "initial_material_template_uuid": None,
            "parent_link": "deck",
            "description": "",
            "meta_data": {},
        }
    ]
    assert {
        "uuid",
        "material_uuid",
        "occupied_material_uuid",
        "template_name",
    }.isdisjoint(sites[0])


def test_normalize_available_sites_validates_initial_material_template() -> None:
    """默认内容模板必须是规范 UUID，并满足非空准入白名单。"""

    site = normalize_available_sites(
        [
            {
                "index": "A1",
                "label": "默认试剂位",
                "allowed_resource_template_uuids": [INITIAL_TEMPLATE_UUID],
                "initial_material_template_uuid": INITIAL_TEMPLATE_UUID,
            }
        ]
    )[0]

    assert site["initial_material_template_uuid"] == INITIAL_TEMPLATE_UUID

    with pytest.raises(ValueError, match="必须是规范 UUID"):
        normalize_available_sites(
            [
                {
                    "index": "A1",
                    "label": "默认试剂位",
                    "initial_material_template_uuid": INITIAL_TEMPLATE_UUID.upper(),
                }
            ]
        )
    with pytest.raises(ValueError, match="必须包含在"):
        normalize_available_sites(
            [
                {
                    "index": "A1",
                    "label": "默认试剂位",
                    "allowed_resource_template_uuids": [OTHER_TEMPLATE_UUID],
                    "initial_material_template_uuid": INITIAL_TEMPLATE_UUID,
                }
            ]
        )


def test_normalize_available_sites_preserves_initial_template_name_hint() -> None:
    """领域资源可在元数据中保留待模板同步解析的稳定短名。"""

    site = normalize_available_sites(
        [
            {
                "index": "A1",
                "label": "默认吸头位",
                "allowed_resource_template_uuids": [INITIAL_TEMPLATE_UUID],
                "meta_data": {
                    "unilab": {
                        "initial_material_template": "szlab_pipette_tip",
                    }
                },
            }
        ]
    )[0]

    assert site["initial_material_template_uuid"] is None
    assert site["meta_data"]["unilab"]["initial_material_template"] == (
        "szlab_pipette_tip"
    )


def test_normalize_available_sites_rejects_duplicate_business_identity() -> None:
    """同一模板内重复的库位索引或名称应在 HTTP 上报前关闭式失败。

    参数：无。返回：无；断言重复 ``label`` 不会形成含糊的库位模板定义。
    """

    with pytest.raises(ValueError, match="重复 label"):
        normalize_available_sites(
            [
                {"index": 1, "label": "A1"},
                {"index": 2, "label": "a1"},
            ]
        )


def test_device_and_resource_decorators_expose_available_sites() -> None:
    """设备和器材装饰器都应公开相同的库位模板合同。

    参数：无。返回：无；断言运行时 Registry 元数据使用同一个规范化函数，且
    调用方后续修改输入数组不会改变已经冻结的模板定义。
    """

    definitions = [dict(NESTED_SITE_DEFINITIONS[0])]

    @device(
        id="decorated_site_device",
        category=["test"],
        available_sites=definitions,
    )
    class DecoratedSiteDevice:
        """测试设备模板。"""

    @resource(
        id="decorated_site_resource",
        category=["test"],
        available_sites=definitions,
    )
    def decorated_site_resource(name: str) -> str:
        """返回测试器材名称。

        参数：``name`` 是测试器材名称。返回：原始名称。
        """

        return name

    definitions.clear()
    assert get_device_meta(DecoratedSiteDevice)["available_sites"][0][
        "label"
    ] == "反应瓶位"
    assert get_resource_meta(decorated_site_resource)["available_sites"][0][
        "label"
    ] == "反应瓶位"


def test_ast_scanner_reads_literal_available_sites_for_device_and_resource(
    tmp_path,
) -> None:
    """AST 扫描应在不执行作者代码时读取模块常量中的库位定义。

    参数：``tmp_path`` 是隔离的设备包源码目录。返回：无；断言设备和器材模板
    都得到规范化的 ``available_sites``，且常量本身不是运行时导入依赖。
    """

    source_file = tmp_path / "site_templates.py"
    source_file.write_text(
        """
from unilabos.registry.decorators import device, resource

AVAILABLE_SITES = [{
    "label": "A1",
    "position": {"x": 1, "y": 2, "z": 3},
    "size": {"width": 4, "height": 5, "depth": 6},
}]

@device(id="ast_site_device", category=["test"], available_sites=AVAILABLE_SITES)
class AstSiteDevice:
    pass

@resource(id="ast_site_resource", category=["test"], available_sites=AVAILABLE_SITES)
def ast_site_resource(name: str):
    return name
""",
        encoding="utf-8",
    )

    devices, resources = _parse_file(source_file, tmp_path)

    assert devices[0]["available_sites"][0]["label"] == "A1"
    assert devices[0]["available_sites"][0]["index"] == 0
    assert resources[0]["available_sites"][0]["length"] == 5.0


def test_registry_template_snapshot_preserves_available_sites() -> None:
    """不可变模板快照应把库位定义纳入指纹和 Backend DTO。

    参数：无。返回：无；断言有库位模板完整输出，未声明库位的模板稳定输出空数组。
    """

    snapshot = RegistryTemplateSnapshot.from_registry(_RegistryFixture())

    assert snapshot.detached_devices()[0]["available_sites"][0][
        "label"
    ] == "反应瓶位"
    assert snapshot.detached_resources()[0]["available_sites"] == []


def test_virtual_workbench_declares_three_heating_sites() -> None:
    """虚拟工作台运行时与 AST 模板都应声明三个可上报的加热库位。

    参数：无。返回：无；断言 Registry 两条发现路径中的名称、顺序和允许物料
    类型稳定，保证正常启动不会因静态扫描而丢失库位（Site）。
    """

    module = pytest.importorskip("unilabos.devices.virtual.workbench", exc_type=ModuleNotFoundError)
    VirtualWorkbench = module.VirtualWorkbench

    metadata = get_device_meta(VirtualWorkbench, "virtual_workbench")

    assert metadata is not None
    assert [site["label"] for site in metadata["available_sites"]] == [
        "heating_station_1",
        "heating_station_2",
        "heating_station_3",
    ]
    assert all(
        site["content_type"] == ["workbench_material"]
        for site in metadata["available_sites"]
    )

    repository_root = Path(__file__).resolve().parents[2]
    source_file = repository_root / (
        VirtualWorkbench.__module__.replace(".", "/") + ".py"
    )
    devices, _ = _parse_file(source_file, repository_root)
    ast_metadata = next(
        device
        for device in devices
        if device["device_id"] == "virtual_workbench"
    )
    assert ast_metadata["available_sites"] == metadata["available_sites"]
