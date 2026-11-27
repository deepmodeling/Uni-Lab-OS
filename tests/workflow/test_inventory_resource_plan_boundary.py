"""原子搬运释放默认锁，同时保留作者显式声明的作用域。"""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from unilabos.workflow.inventory_resource_plan import (
    _validate_execution_plan,
    bind_inventory_resource_plan,
)
from unilabos.workflow.resource_lock_plan import (
    bind_station_resource_plan,
    compile_template_resource_plan,
    serialize_resource_plan,
    with_hashed_resource_plan_metadata,
)
from unilabos.workflow.store import StoreConflict


@pytest.mark.parametrize("explicit_scope", [False, True])
def test_atomic_transfer_boundary_preserves_explicit_scope(explicit_scope: bool):
    graph = {
        "nodes": [
            {"uuid": "move", "resource_defaults": ["robot"], "resource_default_boundary": True},
            {"uuid": "process", "resource_defaults": ["robot"]},
        ],
        "edges": [{"source_node_uuid": "move", "target_node_uuid": "process"}],
    }
    if explicit_scope:
        graph["resource_scopes"] = [{
            "kind": "with", "scope_id": "region", "resources": ["robot"],
            "node_uuids": ["move", "process"],
        }]
    plan = compile_template_resource_plan(graph)
    assert {interval.node_uuids for interval in plan.intervals} == (
        {("move", "process")} if explicit_scope else {("move",), ("process",)}
    )


def _pending_execution_plan() -> dict[str, object]:
    resource_plan = compile_template_resource_plan(
        {"workflow_uuid": "wire-boundary", "nodes": [{"uuid": "source"}]}
    )
    return {
        "version": 1,
        "run_mode": "normal",
        "nodes": [
            {
                "uuid": "source",
                "parent_uuid": None,
                "topological_index": 0,
                "kind": "material_source",
                "param": {},
                "execution_policy": {},
                "action_resource_contract": {},
                "inputs": [],
                "source_handle_uuids": [],
            }
        ],
        "edges": [],
        "handles": [],
        "resource_plan": serialize_resource_plan(resource_plan),
        "inventory_resource_binding": "pending",
        "capabilities": list(resource_plan.capabilities),
    }


class _UnexpectedInventoryCall:
    def plan_transfer_resources(self, **_kwargs: object) -> list[str]:
        raise AssertionError("非法持久计划不能读取库存或产生任何绑定副作用")


class _EmptyInventoryResources:
    def __init__(self) -> None:
        self.calls = 0

    def plan_transfer_resources(self, **_kwargs: object) -> list[str]:
        self.calls += 1
        return []


class _FixedInventoryResources:
    def __init__(self, *keys: str) -> None:
        self.keys = keys
        self.calls = 0

    def plan_transfer_resources(self, **_kwargs: object) -> list[str]:
        self.calls += 1
        return list(self.keys)


    def resolve_target_site(self, request):
        from types import SimpleNamespace

        assert request.site_uuid
        return SimpleNamespace(uuid=request.site_uuid)


@pytest.mark.parametrize("source_kind", ["empty", "unseen", "retained"])
def test_inventory_binding_preserves_frozen_center_source_context(source_kind):
    """真实 pending→bound 及重复读取不得拒绝或改写中心来源上下文。"""
    from unilabos.center_protocol.digest import canonical_json

    packages = json.loads((Path(__file__).parents[1] / "app/center_client/fixtures/static-source-wire.json").read_text())
    payload = packages["legacy" if source_kind == "empty" else source_kind]["payload"]
    context = {"bindings": payload.get("material_source_bindings", []), "material_refs": payload["material_refs"]}
    plan, snapshot = _split_transfer_execution_plan()
    plan["center_material_source_context"] = deepcopy(context)
    original = deepcopy(plan)
    inventory = _FixedInventoryResources("/devices/00000000-0000-4000-8000-000000000307")
    jobs = [{"workflow_node_uuid": node, "param": {}, "return_info": {}} for node in ["pick", "place"]]

    bound = bind_inventory_resource_plan({"execution_plan": plan, "workflow_snapshot": snapshot}, jobs, inventory)
    assert bound["inventory_resource_binding"] == "bound"
    assert canonical_json(bound["center_material_source_context"]) == canonical_json(context)
    assert plan == original
    calls = inventory.calls
    restored = json.loads(json.dumps(bound))
    assert bind_inventory_resource_plan({"execution_plan": restored}, jobs, inventory) == bound
    assert inventory.calls == calls


@pytest.mark.parametrize("context", [None, {}, {"bindings": []}, {"bindings": [], "material_refs": None},
    {"bindings": [], "material_refs": [], "unexpected": True}])
def test_inventory_binding_rejects_malformed_center_source_context(context):
    plan = _pending_execution_plan()
    plan["center_material_source_context"] = context
    with pytest.raises(StoreConflict, match="持久化执行计划"):
        bind_inventory_resource_plan({"execution_plan": plan}, [], _UnexpectedInventoryCall())


def test_inventory_binding_preserves_runtime_material_source_mount() -> None:
    """动态 mount 的冻结来源边必须通过库存资源计划关闭式校验。"""

    plan = _pending_execution_plan()
    nodes = plan["nodes"]
    assert isinstance(nodes, list)
    nodes[0]["runtime_material_source"] = True
    nodes[0]["runtime_mount_binding"] = {
        "source_node_uuid": "load-tip-box",
        "source_data_key": "material",
    }

    wire, _resource_plan = _validate_execution_plan(plan)

    binding = wire.nodes[0].runtime_mount_binding
    assert binding is not None
    assert binding.model_dump() == {
        "source_node_uuid": "load-tip-box",
        "source_data_key": "material",
    }


def _split_transfer_execution_plan() -> tuple[dict[str, object], dict[str, object]]:
    plan = _pending_execution_plan()
    material_uuid = "00000000-0000-4000-8000-000000000301"
    executor_uuid = "00000000-0000-4000-8000-000000000302"
    source_uuid = "00000000-0000-4000-8000-000000000303"
    source_site_uuid = "00000000-0000-4000-8000-000000000304"
    target_uuid = "00000000-0000-4000-8000-000000000305"
    target_site_uuid = "00000000-0000-4000-8000-000000000306"

    def transfer_node(
        node_uuid: str, operation: str, owner_param: str, site_param: str,
    ) -> dict[str, object]:
        return {
            "uuid": node_uuid,
            "parent_uuid": None,
            "topological_index": 0 if operation == "pick" else 1,
            "kind": "material_transfer",
            "param": {
                "material": {"uuid": material_uuid},
                "executor": {"uuid": executor_uuid},
                "source": {"uuid": source_uuid},
                "source_site": {"uuid": source_site_uuid},
                "target": {"uuid": target_uuid},
                "target_site": {"uuid": target_site_uuid},
            },
            "execution_policy": {},
            "action_resource_contract": {
                "version": 2,
                "required_device_params": ["executor"],
                "transfer": {
                    "material_param": "material",
                    "source_owner_param": "source",
                    "source_site_uuid_param": "",
                    "source_site_name_param": "source_site",
                    "target_owner_param": "target",
                    "target_site_uuid_param": "",
                    "target_site_name_param": "target_site",
                    "gripper_site_role": "robot.gripper",
                },
                "transfer_step": {
                    "operation": operation,
                    "material_param": "material",
                    "owner_param": owner_param,
                    "site_param": site_param,
                    "carrier_params": ["executor"],
                },
            },
            "inputs": [],
            "source_handle_uuids": [],
            "material_uuid": executor_uuid,
        }

    plan["nodes"] = [
        transfer_node("pick", "pick", "source", "source_site"),
        transfer_node("place", "place", "target", "target_site"),
    ]
    plan["edges"] = [
        {
            "uuid": "pick-place",
            "source_node_uuid": "pick",
            "target_node_uuid": "place",
            "dependency_only": True,
        }
    ]
    bindings = {
        executor_uuid: {
            "canonical_key": f"/devices/{executor_uuid}",
            "kind": "device",
        },
        source_uuid: {
            "canonical_key": f"/devices/{source_uuid}",
            "kind": "device",
        },
        source_site_uuid: {
            "canonical_key": (
                f"material/{source_uuid}/site/{source_site_uuid}/exclusive"
            ),
            "kind": "material_site",
        },
        target_uuid: {
            "canonical_key": f"/devices/{target_uuid}",
            "kind": "device",
        },
        target_site_uuid: {
            "canonical_key": (
                f"material/{target_uuid}/site/{target_site_uuid}/exclusive"
            ),
            "kind": "material_site",
        },
    }
    return plan, {"resource_bindings": bindings}


def test_inventory_binding_rejects_material_transfer_without_transfer_contract(
) -> None:
    """material_transfer 不能以空合同跳过库存规划后标记为 bound。"""

    plan = _pending_execution_plan()
    nodes = plan["nodes"]
    assert isinstance(nodes, list)
    nodes[0]["kind"] = "material_transfer"

    with pytest.raises(StoreConflict, match="持久化执行计划"):
        bind_inventory_resource_plan(
            {"execution_plan": plan, "workflow_snapshot": {}},
            [],
            _UnexpectedInventoryCall(),
        )


def test_inventory_binding_rejects_misspelled_transfer_before_inventory_side_effects(
) -> None:
    """动作资源合同拼写错误必须在任何库存查询之前关闭失败。"""

    plan = _pending_execution_plan()
    nodes = plan["nodes"]
    assert isinstance(nodes, list)
    nodes[0]["kind"] = "material_transfer"
    nodes[0]["action_resource_contract"] = {
        "version": 1,
        "tranfer": {
            "material_param": "material",
            "target_owner_param": "target",
            "target_site_name_param": "target_site",
            "gripper_site_role": "robot.gripper",
        },
    }

    with pytest.raises(StoreConflict, match="持久化执行计划"):
        bind_inventory_resource_plan(
            {"execution_plan": plan, "workflow_snapshot": {}},
            [],
            _UnexpectedInventoryCall(),
        )

    assert plan["inventory_resource_binding"] == "pending"


@pytest.mark.parametrize("binding", [None, "boudn", "future"])
def test_inventory_binding_rejects_unrecognized_binding_state_for_transfer_plan(
    binding: str | None,
) -> None:
    """需要库存绑定的计划不能通过缺失或未知判别字段绕过验证。"""

    plan = _pending_execution_plan()
    nodes = plan["nodes"]
    assert isinstance(nodes, list)
    nodes[0]["kind"] = "material_transfer"
    nodes[0]["action_resource_contract"] = {
        "version": 1,
        "transfer": {
            "material_param": "material",
            "source_owner_param": "",
            "source_site_uuid_param": "",
            "source_site_name_param": "",
            "target_owner_param": "target",
            "target_site_uuid_param": "",
            "target_site_name_param": "target_site",
            "gripper_site_role": "robot.gripper",
        },
    }
    if binding is None:
        plan.pop("inventory_resource_binding")
    else:
        plan["inventory_resource_binding"] = binding

    with pytest.raises(StoreConflict, match="持久化执行计划"):
        bind_inventory_resource_plan(
            {"execution_plan": plan, "workflow_snapshot": {}},
            [],
            _UnexpectedInventoryCall(),
        )


def test_inventory_binding_rejects_false_bound_transfer_plan() -> None:
    """仅改判别字段不能把仍为 template 的转运资源计划伪装成已绑定。"""

    plan = _pending_execution_plan()
    nodes = plan["nodes"]
    assert isinstance(nodes, list)
    nodes[0]["kind"] = "material_transfer"
    nodes[0]["action_resource_contract"] = {
        "version": 1,
        "transfer": {
            "material_param": "material",
            "source_owner_param": "",
            "source_site_uuid_param": "",
            "source_site_name_param": "",
            "target_owner_param": "target",
            "target_site_uuid_param": "",
            "target_site_name_param": "target_site",
            "gripper_site_role": "robot.gripper",
        },
    }
    plan["inventory_resource_binding"] = "bound"

    with pytest.raises(StoreConflict, match="持久化执行计划"):
        bind_inventory_resource_plan(
            {"execution_plan": plan, "workflow_snapshot": {}},
            [],
            _UnexpectedInventoryCall(),
        )


def test_inventory_binding_rejects_bound_transfer_without_inventory_projection() -> None:
    """bound 资源计划也必须含覆盖转运节点的库存资源区间。"""

    plan = _pending_execution_plan()
    nodes = plan["nodes"]
    assert isinstance(nodes, list)
    nodes[0]["kind"] = "material_transfer"
    nodes[0]["action_resource_contract"] = {
        "version": 1,
        "transfer": {
            "material_param": "material",
            "source_owner_param": "",
            "source_site_uuid_param": "",
            "source_site_name_param": "",
            "target_owner_param": "target",
            "target_site_uuid_param": "",
            "target_site_name_param": "target_site",
            "gripper_site_role": "robot.gripper",
        },
    }
    empty_bound = bind_station_resource_plan(
        compile_template_resource_plan({"nodes": [{"uuid": "source"}]}),
        {},
    )
    plan["resource_plan"] = serialize_resource_plan(empty_bound)
    plan["inventory_resource_binding"] = "bound"

    with pytest.raises(StoreConflict, match="持久化执行计划"):
        bind_inventory_resource_plan(
            {"execution_plan": plan, "workflow_snapshot": {}},
            [],
            _UnexpectedInventoryCall(),
        )


def test_inventory_binding_revalidates_generated_bound_transfer_projection() -> None:
    """库存未返回资源时，不得把仅绑定普通资源的输出发布为 bound。"""

    plan = _pending_execution_plan()
    nodes = plan["nodes"]
    assert isinstance(nodes, list)
    nodes[0]["kind"] = "material_transfer"
    nodes[0]["resource_defaults"] = ["robot"]
    nodes[0]["action_resource_contract"] = {
        "version": 1,
        "transfer": {
            "material_param": "material",
            "source_owner_param": "",
            "source_site_uuid_param": "",
            "source_site_name_param": "",
            "target_owner_param": "target",
            "target_site_uuid_param": "",
            "target_site_name_param": "target_site",
            "gripper_site_role": "robot.gripper",
        },
    }
    inventory = _EmptyInventoryResources()

    with pytest.raises(StoreConflict, match="持久化执行计划"):
        bind_inventory_resource_plan(
            {
                "execution_plan": plan,
                "workflow_snapshot": {
                    "resource_bindings": {
                        "robot": "00000000-0000-4000-8000-000000000211"
                    }
                },
            },
            [],
            inventory,
        )

    assert inventory.calls == 1


def test_inventory_binding_accepts_legacy_plan_without_inventory_semantics() -> None:
    """没有来源或转运语义的旧计划可明确识别并保持原状。"""

    plan = _pending_execution_plan()
    nodes = plan["nodes"]
    assert isinstance(nodes, list)
    nodes[0]["kind"] = "workflow_input"
    plan.pop("inventory_resource_binding")

    assert bind_inventory_resource_plan(
        {"execution_plan": plan, "workflow_snapshot": {}},
        [],
        _UnexpectedInventoryCall(),
    ) == plan


def test_inventory_binding_rejects_explicit_null_binding_state() -> None:
    """只有字段缺失才是旧计划；显式 null 不能伪装成 legacy。"""

    plan = _pending_execution_plan()
    nodes = plan["nodes"]
    assert isinstance(nodes, list)
    nodes[0]["kind"] = "workflow_input"
    plan["inventory_resource_binding"] = None

    with pytest.raises(StoreConflict, match="持久化执行计划"):
        bind_inventory_resource_plan(
            {"execution_plan": plan, "workflow_snapshot": {}},
            [],
            _UnexpectedInventoryCall(),
        )


def test_inventory_binding_rejects_incomplete_bound_inventory_projection() -> None:
    """一个碰巧相交的库存资源不能代替清单内其余资源的投影。"""

    plan = _pending_execution_plan()
    nodes = plan["nodes"]
    assert isinstance(nodes, list)
    node = nodes[0]
    node["kind"] = "material_transfer"
    node["action_resource_contract"] = {
        "version": 2,
        "required_device_params": ["executor"],
        "resource_params": [{"param": "rail", "role": "motion"}],
        "device_tenancy": {
            "mode": "task_while_loaded",
            "material_param": "material",
            "acquire_device_param": "executor",
            "release_device_param": "",
        },
        "transfer": {
            "material_param": "material",
            "source_owner_param": "source",
            "source_site_uuid_param": "",
            "source_site_name_param": "source_site",
            "target_owner_param": "target",
            "target_site_uuid_param": "",
            "target_site_name_param": "target_site",
            "gripper_site_role": "robot.gripper",
            "motion_resource_roles": ["rail"],
            "tool_resource_roles": ["gripper"],
        },
        "operate_in_place": {"material_param": "material"},
        "transfer_step": {
            "operation": "pick",
            "material_param": "material",
            "owner_param": "source",
            "site_param": "source_site",
            "carrier_params": ["executor", "rail"],
        },
        "order_sensitive": True,
        "aliquot": {
            "source_material_param": "material",
            "target_material_params": ["target_material"],
        },
    }
    inventory_key = "/devices/00000000-0000-4000-8000-000000000201"
    missing_key = "/devices/00000000-0000-4000-8000-000000000202"
    node["inventory_resource_lock_keys"] = [inventory_key, missing_key]
    alias = f"inventory:{inventory_key}"
    resource_plan = bind_station_resource_plan(
        compile_template_resource_plan(
            {"nodes": [{"uuid": "source", "resource_defaults": [alias]}]}
        ),
        {alias: {"canonical_key": inventory_key, "kind": "device"}},
    )
    resource_plan = with_hashed_resource_plan_metadata(
        resource_plan,
        {
            "inventory_resource_lock_keys_by_node": {
                "source": [inventory_key, missing_key]
            }
        },
    )
    plan["resource_plan"] = serialize_resource_plan(resource_plan)
    plan["inventory_resource_binding"] = "bound"

    with pytest.raises(StoreConflict, match="库存资源投影"):
        bind_inventory_resource_plan(
            {"execution_plan": plan, "workflow_snapshot": {}},
            [],
            _UnexpectedInventoryCall(),
        )


def test_single_material_in_place_binds_site_of_preceding_placement() -> None:
    plan, snapshot = _split_transfer_execution_plan()
    place = plan["nodes"][-1]
    place["action_resource_contract"]["transfer"].update(
        target_site_uuid_param="target_site", target_site_name_param=""
    )
    args = place["param"]
    sample = args["material"]["uuid"]
    owner = args["target"]["uuid"]
    site = args["target_site"]["uuid"]
    site_key = f"material/{owner}/site/{site}/exclusive"
    action = {
        **place,
        "uuid": "inspect",
        "kind": "device_action",
        "topological_index": 2,
        "param": {"sample": {"uuid": sample}},
        "material_uuid": owner,
        "action_resource_contract": {"version": 2, "operate_in_place": {"material_param": "sample"}},
    }
    plan["nodes"].append(action)
    plan["edges"].append({
        "uuid": "place-inspect", "source_node_uuid": "place",
        "target_node_uuid": "inspect", "dependency_only": True,
    })

    class Inventory(_FixedInventoryResources):
        def plan_operate_in_place_resources(
            self, *, material_uuid, device_material_uuid, planned_location, planned_parent
        ):
            assert (material_uuid, device_material_uuid) == (sample, owner)
            assert planned_location == {"owner_material_uuid": owner, "site_uuid": site, "site_name": ""}
            return [f"material/{sample}/exclusive", site_key, f"/devices/{owner}"]

    bound = bind_inventory_resource_plan(
        {"execution_plan": plan, "workflow_snapshot": snapshot}, [], Inventory(f"/devices/{owner}")
    )
    assert site_key in bound["nodes"][-1]["inventory_resource_lock_keys"]
    assert site_key in bound["resource_plan"]["metadata"]["inventory_resource_lock_keys_by_node"]["inspect"]


def test_inventory_binding_keeps_split_pick_place_in_one_transfer_scope() -> None:
    """transfer_step 的 pick/place 不是原子 transfer 的默认释放边界。"""

    plan, snapshot = _split_transfer_execution_plan()
    inventory_key = "/devices/00000000-0000-4000-8000-000000000307"
    inventory = _FixedInventoryResources(inventory_key)
    jobs = [
        {"workflow_node_uuid": "pick", "param": {}, "return_info": {}},
        {"workflow_node_uuid": "place", "param": {}, "return_info": {}},
    ]

    bound = bind_inventory_resource_plan(
        {"execution_plan": plan, "workflow_snapshot": snapshot},
        jobs,
        inventory,
    )

    assert inventory.calls == 2
    # 设备锁、端点库位锁、搬运器锁；端点所属物件只锁库位、不整件独占。
    assert all(
        inventory_key in node["inventory_resource_lock_keys"]
        and len(node["inventory_resource_lock_keys"]) == 3
        and not any(key.startswith("material/") and key.endswith("/exclusive") and "/site/" not in key
                    for key in node["inventory_resource_lock_keys"])
        for node in bound["nodes"]
    )
    assert all(not node.get("resource_default_boundary") for node in bound["nodes"])
    assert bind_inventory_resource_plan(
        {"execution_plan": bound, "workflow_snapshot": snapshot},
        jobs,
        _UnexpectedInventoryCall(),
    ) == bound


def test_admitted_child_identity_is_shared_by_frozen_locks_and_runtime_guard() -> None:
    """锁规划使用子来源的真实准入回执，并保存同一身份供重试校验。"""
    plan, snapshot = _split_transfer_execution_plan()
    selected = plan["nodes"][0]["param"]["material"]["uuid"]
    source = _pending_execution_plan()["nodes"][0]
    source.update(
        uuid="child", runtime_material_source=True,
        runtime_mount_binding={"source_node_uuid": "parent", "source_data_key": "material"},
        source_handle_uuids=["child-material"],
    )
    handle = {
        "uuid": "child-material", "node_uuid": "child", "template_handle_uuid": "child-template",
        "io_type": "source", "type": "ResourceSlot", "data_key": "material",
        "handle_key": "material", "data_source": "executor", "required": False,
    }
    plan["handles"] = [handle]
    for node in plan["nodes"]:
        node["param"].pop("material")
        target_handle = f"{node['uuid']}-material"
        plan["handles"].append({**handle, "uuid": target_handle, "node_uuid": node["uuid"], "io_type": "target"})
        plan["edges"].append({
            "uuid": f"child-{node['uuid']}", "source_node_uuid": "child", "target_node_uuid": node["uuid"],
            "source_handle_uuid": "child-material", "target_handle_uuid": target_handle,
            "source_type": "ResourceSlot", "target_type": "ResourceSlot",
            "source_data_key": "material", "target_data_key": "material",
        })
    plan["nodes"].insert(0, source)
    jobs = [{"workflow_node_uuid": "child", "status": "succeeded", "param": {},
             "return_info": {"material": {"uuid": selected}}}]
    resolved_materials = []

    class Inventory(_FixedInventoryResources):
        def resolve_target_site(self, request):
            resolved_materials.append(request.occupant_material_uuid)
            return super().resolve_target_site(request)

    bound = bind_inventory_resource_plan(
        {"execution_plan": plan, "workflow_snapshot": snapshot}, jobs, Inventory(),
    )
    assert bound["nodes"][0]["planned_material_uuid"] == selected
    assert resolved_materials == [selected, selected]
    assert all(node["inventory_resource_lock_keys"] for node in bound["nodes"][1:])
    assert bind_inventory_resource_plan(
        {"execution_plan": json.loads(json.dumps(bound)), "workflow_snapshot": snapshot},
        jobs, _UnexpectedInventoryCall(),
    ) == bound


def test_inventory_binding_rejects_shortened_node_inventory_manifest() -> None:
    """节点清单不能脱离资源计划内容身份被缩短后继续冒充完整绑定。"""

    plan, snapshot = _split_transfer_execution_plan()
    inventory_keys = (
        "/devices/00000000-0000-4000-8000-000000000307",
        "/devices/00000000-0000-4000-8000-000000000308",
    )
    jobs = [
        {"workflow_node_uuid": "pick", "param": {}, "return_info": {}},
        {"workflow_node_uuid": "place", "param": {}, "return_info": {}},
    ]
    bound = bind_inventory_resource_plan(
        {"execution_plan": plan, "workflow_snapshot": snapshot},
        jobs,
        _FixedInventoryResources(*inventory_keys),
    )
    tampered = deepcopy(bound)
    tampered["nodes"][0]["inventory_resource_lock_keys"] = [inventory_keys[0]]

    with pytest.raises(StoreConflict, match="库存资源清单"):
        bind_inventory_resource_plan(
            {"execution_plan": tampered, "workflow_snapshot": snapshot},
            jobs,
            _UnexpectedInventoryCall(),
        )


def test_inventory_binding_rejects_tampered_hashed_inventory_manifest() -> None:
    """直接缩短资源计划内冻结清单必须破坏 plan_id 并关闭失败。"""

    plan, snapshot = _split_transfer_execution_plan()
    inventory_keys = (
        "/devices/00000000-0000-4000-8000-000000000307",
        "/devices/00000000-0000-4000-8000-000000000308",
    )
    jobs = [
        {"workflow_node_uuid": "pick", "param": {}, "return_info": {}},
        {"workflow_node_uuid": "place", "param": {}, "return_info": {}},
    ]
    bound = bind_inventory_resource_plan(
        {"execution_plan": plan, "workflow_snapshot": snapshot},
        jobs,
        _FixedInventoryResources(*inventory_keys),
    )
    tampered = deepcopy(bound)
    metadata = tampered["resource_plan"]["metadata"]
    metadata["inventory_resource_lock_keys_by_node"]["pick"] = [
        inventory_keys[0]
    ]

    with pytest.raises(StoreConflict, match="持久化执行计划字段无效"):
        bind_inventory_resource_plan(
            {"execution_plan": tampered, "workflow_snapshot": snapshot},
            jobs,
            _UnexpectedInventoryCall(),
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown_node_field",
        "wrong_contract_version_type",
        "duplicate_node",
        "dangling_edge",
    ],
)
def test_inventory_binding_rejects_malformed_persisted_execution_plan(
    mutation: str,
) -> None:
    """首次派发前关闭式验证完整图形，不能用字典覆盖或跳过损坏成员。"""

    plan = _pending_execution_plan()
    nodes = plan["nodes"]
    assert isinstance(nodes, list)
    if mutation == "unknown_node_field":
        nodes[0]["resource_defualts"] = ["typo"]
    elif mutation == "wrong_contract_version_type":
        nodes[0]["action_resource_contract"] = {
            "version": True,
            "required_device_params": ["executor"],
        }
    elif mutation == "duplicate_node":
        nodes.append(deepcopy(nodes[0]))
    else:
        plan["edges"] = [
            {
                "uuid": "dangling",
                "source_node_uuid": "source",
                "target_node_uuid": "missing",
                "source_handle_uuid": "",
                "target_handle_uuid": "",
                "dependency_only": True,
            }
        ]

    with pytest.raises(StoreConflict, match="持久化执行计划"):
        bind_inventory_resource_plan(
            {"execution_plan": plan, "workflow_snapshot": {}},
            [],
            _UnexpectedInventoryCall(),
        )


@pytest.mark.parametrize("explicit_scope", [False, True])
def test_bound_material_releases_after_each_action_unless_scope_is_explicit(
    explicit_scope: bool,
) -> None:
    """共享物料默认逐 Job 释放；作者显式连续范围仍保留整段 reservation。"""

    graph: dict[str, object] = {
        "workflow_uuid": "shared-material-release",
        "nodes": [
            {"uuid": "first", "resource_defaults": ["sample"]},
            {"uuid": "second", "resource_defaults": ["sample"]},
        ],
        "edges": [{"source_node_uuid": "first", "target_node_uuid": "second"}],
    }
    if explicit_scope:
        graph["resource_scopes"] = [
            {
                "scope_id": "continuous",
                "kind": "with",
                "resources": ["sample"],
                "node_uuids": ["first", "second"],
            }
        ]
    material_uuid = "00000000-0000-4000-8000-000000000101"
    plan = bind_station_resource_plan(
        compile_template_resource_plan(graph),
        {
            "sample": {
                "instance_uuid": material_uuid,
                "canonical_key": f"material/{material_uuid}/exclusive",
                "kind": "material",
            }
        },
    )
    material_intervals = [
        interval
        for interval in plan.intervals
        if next(
            resource for resource in plan.resources if resource.resource_id == interval.resource_id
        ).alias
        == "sample"
    ]

    assert {interval.node_uuids for interval in material_intervals} == (
        {("first", "second")} if explicit_scope else {("first",), ("second",)}
    )
