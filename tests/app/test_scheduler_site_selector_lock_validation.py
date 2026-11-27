"""调度器在库位名称已经由库存权威解析后的物料锁校验测试。"""

from types import SimpleNamespace

from unilabos.app.scheduler.resource_lock import material_lock_key, site_lock_key
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.app.scheduler.site_target import ResolvedSiteTarget

_MATERIAL_UUID = "11111111-1111-4111-8111-111111111111"
_OWNER_UUID = "22222222-2222-4222-8222-222222222222"
_SITE_UUID = "33333333-3333-4333-8333-333333333333"


def test_resolved_site_name_uses_stable_uuid_only_for_lock_validation() -> None:
    """设备参数保留库位名时，锁校验应使用已解析的稳定库位 UUID。"""

    node = SimpleNamespace(
        param_schema={
            "type": "object",
            "properties": {
                "goal": {
                    "type": "object",
                    "properties": {
                        "resource": {
                            "type": "object",
                            "x-unilabos-material-lock": True,
                            "properties": {
                                "uuid": {"type": "string", "format": "uuid"},
                            },
                            "required": ["uuid"],
                            "additionalProperties": False,
                        },
                        "target_site": {
                            "type": "string",
                            "format": "uuid",
                            "x-unilabos-editor-control": "site_selector",
                            "x-unilabos-site-selector": {
                                "version": 1,
                                "owner": "target_warehouse",
                                "occupant": "resource",
                                "show_occupied": True,
                                "allow_occupied": False,
                            },
                        },
                    },
                    "required": ["resource", "target_site"],
                    "additionalProperties": False,
                },
                "feedback": {},
                "result": {},
            },
            "required": ["goal"],
        },
        action_resource_contract={
            "version": 1,
            "transfer": {
                "material_param": "resource",
                "target_owner_param": "target_warehouse",
                "target_site_uuid_param": "",
                "target_site_name_param": "target_site",
                "gripper_site_role": "robot.gripper",
            },
        },
        executor_kind="material_transfer",
        material_requirements=[],
    )
    resolved_args = {
        "resource": {"uuid": _MATERIAL_UUID},
        "target_site": "S0722",
    }
    resolved_site = ResolvedSiteTarget(
        uuid=_SITE_UUID,
        name="S0722",
        owner_material_uuid=_OWNER_UUID,
    )

    keys = EdgeScheduler()._resource_lock_keys(
        node,
        resolved_args,
        resolved_site=resolved_site,
    )

    assert keys == {
        material_lock_key(_MATERIAL_UUID),
        site_lock_key(_OWNER_UUID, _SITE_UUID),
    }
    assert resolved_args["target_site"] == "S0722"
