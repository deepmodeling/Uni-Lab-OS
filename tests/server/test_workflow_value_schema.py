from uuid import uuid4

import pytest

from unilabos.server.services.runtime.workflow.value_schema import (
    WorkflowValueSchemaError,
    validate_value,
)


def test_resource_slot_value_requires_a_canonical_uuid() -> None:
    material_uuid = str(uuid4())

    assert validate_value(
        {"$slot": "ResourceSlot"},
        {"uuid": material_uuid},
    ) == {"uuid": material_uuid}

    with pytest.raises(WorkflowValueSchemaError) as exc_info:
        validate_value(
            {"$slot": "ResourceSlot"},
            {"uuid": "local-material-name"},
        )
    assert exc_info.value.code == "workflow_value_invalid"


@pytest.mark.parametrize("schema,value", [
    ({"type": "integer", "minimum": 2}, 1),
    ({"type": "number", "maximum": 2}, 3),
    ({"type": "string", "minLength": 2}, "a"),
    ({"type": "array", "items": {"type": "integer"}, "maxItems": 1}, [1, 2]),
])
def test_input_and_output_bounds_are_enforced(schema, value):
    with pytest.raises(WorkflowValueSchemaError):
        validate_value(schema, value)


def test_composite_cannot_narrow_parent_bounds_without_validation():
    from unilabos.server.services.runtime.workflow.value_schema import schema_is_assignable
    target = {"type": "number", "minimum": 1, "maximum": 10}
    assert not schema_is_assignable({"type": "integer"}, target)
    assert not schema_is_assignable({"type": "integer", "enum": [0, 2]}, target)
    assert schema_is_assignable({"type": "integer", "enum": [1, 2]}, target)
    assert schema_is_assignable({"type": "integer", "minimum": 2, "maximum": 8}, target)


def test_device_and_site_slots_validate_like_node_parameters():
    """设备槽位是设备 id 字符串，库位槽位是 ResourceSite uuid 字符串（与节点参数同一形状）。"""
    site_uuid = str(uuid4())
    assert validate_value({"$slot": "DeviceSlot"}, " pump-1 ") == "pump-1"
    assert validate_value({"$slot": "SiteSlot"}, site_uuid) == site_uuid
    for schema, value in [
        ({"$slot": "DeviceSlot"}, ""),
        ({"$slot": "DeviceSlot"}, {"uuid": site_uuid}),
        ({"$slot": "SiteSlot"}, "A1"),
        ({"$slot": "SiteSlot"}, {"uuid": site_uuid}),
    ]:
        with pytest.raises(WorkflowValueSchemaError) as exc_info:
            validate_value(schema, value)
        assert exc_info.value.code == "workflow_value_invalid"


def test_string_slots_are_assignable_to_strings_but_not_to_each_other():
    from unilabos.server.services.runtime.workflow.value_schema import schema_is_assignable
    device, site, material = {"$slot": "DeviceSlot"}, {"$slot": "SiteSlot"}, {"$slot": "ResourceSlot"}
    assert schema_is_assignable(device, device)
    assert schema_is_assignable(site, site)
    # 节点上的 DeviceSlot / SiteSlot 参数渲染为字符串：工作流槽位输入可以接上去，字符串输出也能喂给槽位
    assert schema_is_assignable(device, {"type": "string"})
    assert schema_is_assignable({"type": "string"}, site)
    assert not schema_is_assignable(device, site)
    assert not schema_is_assignable(site, material)
    assert not schema_is_assignable(material, device)
    assert not schema_is_assignable(device, {"type": "integer"})
