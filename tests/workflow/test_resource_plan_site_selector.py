"""库位引用延迟解析时，资源计划仍须严格冻结物料身份。"""

import pytest

from unilabos.registry.material_lock_schema import MaterialLockSchemaError
from unilabos.workflow.execution_plan import ExecutionPlanBuilder


def _build_plan(material_uuid: str, *, selector: bool = True):
    return ExecutionPlanBuilder._resource_plan(
        graph={},
        planned_edges=[],
        planned_nodes=[{
            "uuid": "transfer",
            "param": {"resource": {"uuid": material_uuid}, "target_site": "S061"},
            "site_selectors": ([{
                "parameter": "target_site", "owner_parameter": "target_warehouse",
            }] if selector else []),
            "param_schema": {"properties": {"goal": {
                "type": "object",
                "properties": {
                    "resource": {
                        "type": "object",
                        "properties": {"uuid": {"type": "string", "format": "uuid"}},
                        "required": ["uuid"],
                        "x-unilabos-material-lock": True,
                    },
                    "target_site": {"type": "string", "format": "uuid"},
                },
            }}},
        }],
    )


def test_named_site_does_not_prevent_freezing_material_lock():
    material_uuid = "00000000-0000-4000-8000-000000000111"
    plan = _build_plan(material_uuid)
    assert {resource.canonical_key for resource in plan.resources} == {
        f"material/{material_uuid}/exclusive",
    }


def test_deferred_site_does_not_bypass_material_uuid_validation():
    with pytest.raises(MaterialLockSchemaError) as error:
        _build_plan("invalid-material")
    assert error.value.path == "/resource/uuid"


def test_uuid_field_without_site_selector_remains_strict():
    with pytest.raises(MaterialLockSchemaError) as error:
        _build_plan("00000000-0000-4000-8000-000000000111", selector=False)
    assert error.value.path == "/target_site"
