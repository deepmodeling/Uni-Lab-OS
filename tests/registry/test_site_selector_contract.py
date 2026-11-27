"""库位参数（``SiteSlot``）与物料参数的关系：``@action(material_contract=MaterialRole.site_picker(...))``。

``SiteSelector`` 已与 ``SiteSlot`` 合一：库位参数只有一个类型 ``SiteSlot``，库位属于哪个物料参数、放进去的是
哪个物料参数、已占用库位的显示 / 可选策略写在 ``MaterialRole.site_picker``。生成的 ``x-unilabos-site-selector`` 扩展与
原 ``SiteSelector`` 相同，连接点投影、运行预检与派发前的库位门禁照旧按它工作。
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from unilabos.registry.action_schema import ActionSchemaError
from unilabos.registry.action_template_projection import (
    ActionTemplateProjectionError,
    compile_action_template_handles,
)
from unilabos.registry.ast_registry_scanner import _parse_file
from unilabos.registry.decorators import MaterialRole, action
from unilabos.registry.material_roles import MaterialRoleError, material_contract, material_site_selectors
from unilabos.registry.registry import Registry

#: 原 ``Annotated[str | None, SiteSelector(owner="warehouse", occupant="resource")] = None`` 生成的字段扩展
#: （不再限定 uuid 格式：值也可以是所属方下的库位名）。
_SITE_SELECTOR_FIELD = {
    "type": ["string", "null"],
    "default": None,
    "x-unilabos-editor-control": "site_selector",
    "x-unilabos-site-selector": {
        "version": 1,
        "owner": "warehouse",
        "occupant": "resource",
        "show_occupied": True,
        "allow_occupied": False,
    },
}


def _source(material: str, signature: str, *, header: str = "") -> str:
    return textwrap.dedent(
        f'''
        from typing import Optional
        from unilabos.registry.decorators import MaterialRole, action, device
        from unilabos.registry.placeholder_type import ResourceSlot, SiteSlot
        {header}

        @device(id="site_driver", category=["test"])
        class Driver:
            @action(material_contract={material})
            def action(self, {signature}) -> None:
                pass
        '''
    )


def _scan(tmp_path: Path, material: str, signature: str, *, header: str = "") -> dict[str, Any]:
    """真实 AST 扫描（不导入作者模块）并按注册表规则生成动作条目。"""

    path = tmp_path / "site_driver.py"
    path.write_text(_source(material, signature, header=header), encoding="utf-8")
    devices, _resources, _workflows = _parse_file(path, tmp_path)
    entry = Registry()._build_device_entry_from_ast("site_driver", devices[0])
    return entry["class"]["action_value_mappings"]["action"]


_FULL = 'MaterialRole.site_picker("site", of="warehouse", for_material="resource", occupied="show")'
_FULL_SIGNATURE = "warehouse: ResourceSlot, resource: ResourceSlot, site: SiteSlot = None"


def _without_description(field: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in field.items() if key != "description"}


def test_material_site_builds_the_same_extension_as_the_removed_site_selector() -> None:
    role = MaterialRole.site_picker("site", of="warehouse", for_material="resource", occupied="show")
    assert material_site_selectors(role) == {"site": _SITE_SELECTOR_FIELD["x-unilabos-site-selector"]}
    # 库位关系不进资源合同：只写库位关系的动作没有资源合同，与其他语义写在一起时互不影响
    assert material_contract(role) == {}
    assert material_contract([MaterialRole.in_place("resource"), role]) == {
        "version": 2,
        "operate_in_place": {"material_param": "resource"},
    }
    with pytest.raises(MaterialRoleError):
        MaterialRole.site_picker("site", of="warehouse", occupied="yes")
    with pytest.raises(MaterialRoleError, match="自身"):
        MaterialRole.site_picker("site", of="site")
    with pytest.raises(MaterialRoleError, match="多条"):
        material_site_selectors([MaterialRole.site_picker("site", of="a"), MaterialRole.site_picker("site", of="b")])


def test_action_site_parameter_compiles_complete_relation_metadata(tmp_path: Path) -> None:
    action_entry = _scan(tmp_path, _FULL, _FULL_SIGNATURE)
    site = action_entry["schema"]["properties"]["goal"]["properties"]["site"]
    # 选择器扩展与原 SiteSelector 相同；另外标出槽位类型（调度器按它加库位锁）
    assert _without_description(site) == {**_SITE_SELECTOR_FIELD, "x-unilabos-slot": "site"}
    assert action_entry["placeholder_keys"]["site"] == "unilabos_sites"
    assert action_entry["materials_need_lock"] == ["warehouse", "resource"]
    assert "resource_contract" not in action_entry["schema"]["x-unilabos-action-contract"]


def test_decorator_records_the_same_relation(tmp_path: Path) -> None:
    from unilabos.registry.placeholder_type import ResourceSlot, SiteSlot

    @action(material_contract=MaterialRole.site_picker("site", of="warehouse"))
    def place(self, warehouse: ResourceSlot, site: SiteSlot) -> None:
        pass

    assert place._action_registry_meta["site_selectors"] == {
        "site": {"version": 1, "owner": "warehouse", "occupant": None, "show_occupied": True, "allow_occupied": False}
    }
    with pytest.raises(MaterialRoleError, match="missing"):

        @action(material_contract=MaterialRole.site_picker("site", of="missing"))
        def bad(self, warehouse: ResourceSlot, site: SiteSlot) -> None:
            pass


@pytest.mark.parametrize(
    ("signature", "material", "message"),
    [
        pytest.param(
            "warehouse: ResourceSlot, resource: ResourceSlot, site: SiteSlot",
            'MaterialRole.site_picker("site", of="missing", for_material="resource")',
            "of='missing'",
            id="missing-owner",
        ),
        pytest.param(
            "warehouse: str, resource: ResourceSlot, site: SiteSlot",
            'MaterialRole.site_picker("site", of="warehouse", for_material="resource")',
            "of='warehouse'",
            id="scalar-owner",
        ),
        pytest.param(
            "warehouse: ResourceSlot, resource: str, site: SiteSlot",
            'MaterialRole.site_picker("site", of="warehouse", for_material="resource")',
            "for_material='resource'",
            id="scalar-occupant",
        ),
        pytest.param(
            "warehouse: list[ResourceSlot], site: SiteSlot",
            'MaterialRole.site_picker("site", of="warehouse")',
            "单个 ResourceSlot",
            id="list-owner",
        ),
        pytest.param(
            "warehouse: ResourceSlot, site: str",
            'MaterialRole.site_picker("site", of="warehouse")',
            "必须注解为 SiteSlot",
            id="plain-string-site",
        ),
        pytest.param(
            "warehouse: ResourceSlot, site: list[SiteSlot]",
            'MaterialRole.site_picker("site", of="warehouse")',
            "必须注解为 SiteSlot",
            id="site-list",
        ),
        pytest.param(
            "warehouse: ResourceSlot",
            'MaterialRole.site_picker("site", of="warehouse")',
            "签名里没有",
            id="missing-site",
        ),
    ],
)
def test_material_site_rejects_unknown_or_non_material_relations(
    tmp_path: Path, signature: str, material: str, message: str
) -> None:
    """库位参数必须是 SiteSlot，owner / occupant 必须显式指向单个 ResourceSlot 参数，不按名字猜。"""

    with pytest.raises(ActionSchemaError, match=message):
        _scan(tmp_path, material, signature)


def test_material_site_rejects_forged_material_import(tmp_path: Path) -> None:
    path = tmp_path / "site_driver.py"
    path.write_text(
        _source(_FULL, _FULL_SIGNATURE).replace(
            "from unilabos.registry.decorators import MaterialRole, action, device",
            "from unilabos.registry.decorators import action, device\nfrom malicious.roles import MaterialRole",
        ),
        encoding="utf-8",
    )
    with pytest.raises(MaterialRoleError):
        _parse_file(path, tmp_path)


def test_removed_site_selector_points_to_material_site(tmp_path: Path) -> None:
    with pytest.raises(ImportError, match=r"MaterialRole\.site"):
        from unilabos.registry.annotations import SiteSelector  # noqa: F401
    path = tmp_path / "old_site_driver.py"
    path.write_text(
        textwrap.dedent(
            '''
            from typing import Annotated
            from unilabos.registry.annotations import SiteSelector
            from unilabos.registry.decorators import action, device
            from unilabos.registry.placeholder_type import ResourceSlot

            @device(id="old_site_driver", category=["test"])
            class Driver:
                @action()
                def place(self, warehouse: ResourceSlot, site: Annotated[str, SiteSelector(owner="warehouse")]) -> None:
                    pass
            '''
        ),
        encoding="utf-8",
    )
    with pytest.raises(ActionSchemaError, match=r"SiteSelector 已删除.*SiteSlot.*MaterialRole\.site"):
        _parse_file(path, tmp_path)


def test_material_site_allows_omitting_the_optional_occupant(tmp_path: Path) -> None:
    action_entry = _scan(
        tmp_path, 'MaterialRole.site_picker("site", of="warehouse")', "warehouse: ResourceSlot, site: SiteSlot"
    )
    site = action_entry["schema"]["properties"]["goal"]["properties"]["site"]
    assert site["type"] == "string"
    assert site["x-unilabos-site-selector"] == {
        "version": 1,
        "owner": "warehouse",
        "occupant": None,
        "show_occupied": True,
        "allow_occupied": False,
    }


def test_action_template_handle_projects_complete_site_selector_metadata(tmp_path: Path) -> None:
    """库位输入连接点直接投影完整库位关系；连接点值模式保留相同扩展。"""

    schema = _scan(
        tmp_path, _FULL, "warehouse: ResourceSlot, resource: ResourceSlot, site: SiteSlot"
    )["schema"]
    handles = compile_action_template_handles(
        schema,
        node_business_key=("owner-template", "action"),
        resource_template_identity_resolver=None,
    )
    site_handle = next(
        handle for handle in handles if handle["io_type"] == "target" and handle["handle_key"] == "site"
    )
    assert site_handle["meta_data"]["unilab"]["site_selector"] == _SITE_SELECTOR_FIELD["x-unilabos-site-selector"]
    assert (
        site_handle["meta_data"]["unilab"]["value_schema"]["x-unilabos-site-selector"]
        == site_handle["meta_data"]["unilab"]["site_selector"]
    )


_RELATION = {"version": 1, "owner": "warehouse", "occupant": None, "show_occupied": True, "allow_occupied": False}


@pytest.mark.parametrize(
    ("field_name", "schema_change", "message"),
    [
        pytest.param("site", {"x-unilabos-site-selector": None}, "库位选择控件缺少完整库位选择合同", id="missing-contract"),
        pytest.param(
            "site", {"x-unilabos-site-selector": "warehouse"}, "库位选择控件缺少完整库位选择合同", id="non-object-contract"
        ),
        pytest.param(
            "site",
            {"x-unilabos-editor-control": "variable_selector", "x-unilabos-site-selector": _RELATION},
            "非库位选择控件不能携带库位选择合同",
            id="extension-on-non-site-control",
        ),
        pytest.param(
            "warehouse",
            {"x-unilabos-editor-control": "site_selector", "x-unilabos-site-selector": _RELATION},
            "非库位选择控件不能携带库位选择合同",
            id="extension-on-material-port",
        ),
    ],
)
def test_action_template_projection_rejects_invalid_site_selector_extensions(
    tmp_path: Path, field_name: str, schema_change: dict[str, object], message: str
) -> None:
    schema = _scan(tmp_path, 'MaterialRole.site_picker("site", of="warehouse")', "warehouse: ResourceSlot, site: SiteSlot")[
        "schema"
    ]
    field_schema = schema["properties"]["goal"]["properties"][field_name]
    for key, value in schema_change.items():
        if value is None:
            field_schema.pop(key, None)
        else:
            field_schema[key] = value
    with pytest.raises(ActionTemplateProjectionError, match=message):
        compile_action_template_handles(
            schema,
            node_business_key=("owner-template", "action"),
            resource_template_identity_resolver=None,
        )


class _Materials:
    """物料权威替身：一块板（warehouse）有 A / B 两个库位，A 上已有别的物料。"""

    def __init__(self) -> None:
        self.sites = [
            SimpleNamespace(site_uuid="site-a", occupied_material_uuid="blocker"),
            SimpleNamespace(site_uuid="site-b", occupied_material_uuid=None),
        ]

    def get_material(self, identity: str) -> Any:
        material = SimpleNamespace(lifecycle_status="active", template_uuid="", resource_type="container")
        return SimpleNamespace(material=material, sites=self.sites if identity == "plate" else [])


@pytest.mark.parametrize("allow_occupied", [False, True])
def test_preflight_and_dispatch_gate_follow_the_material_site_relation(tmp_path: Path, allow_occupied: bool) -> None:
    """运行预检与派发前共用的库位门禁按 MaterialRole.site_picker 生成的扩展核对归属与占用，行为与原 SiteSelector 相同。"""

    from unilabos.server.services.runtime.workflow.run_preflight import (
        ResourceUnavailableError,
        validate_resource_values,
    )

    material = (
        f'MaterialRole.site_picker("site", of="warehouse", for_material="resource", occupied={'"allow"' if allow_occupied else '"show"'})'
    )
    schema = _scan(tmp_path, material, "warehouse: ResourceSlot, resource: ResourceSlot, site: SiteSlot")["schema"]
    materials = _Materials()
    values = {"warehouse": {"uuid": "plate"}, "resource": {"uuid": "sample"}}
    validate_resource_values(schema, {**values, "site": "site-b"}, materials)
    with pytest.raises(ValueError, match="不属于所选容器"):
        validate_resource_values(schema, {**values, "site": "elsewhere"}, materials)
    if allow_occupied:
        validate_resource_values(schema, {**values, "site": "site-a"}, materials)
    else:
        with pytest.raises(ResourceUnavailableError, match="已被其他物料占用"):
            validate_resource_values(schema, {**values, "site": "site-a"}, materials)
    # 占着库位的正是要放进去的物料：不算被别的物料占用
    validate_resource_values(schema, {"warehouse": {"uuid": "plate"}, "resource": {"uuid": "blocker"}, "site": "site-a"}, materials)
