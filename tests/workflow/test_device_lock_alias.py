"""同一台设备只用一个锁名：作者写的设备本地 ID 归一到动作默认持有的 ``device:<UUID>``。"""

from unilabos.workflow.resource_lock_plan import compile_template_resource_plan

DEVICE = "f913f430-1ef8-5a87-81de-7f523240f475"


def _node(uuid, device_id="tecan", material_uuid=DEVICE):
    return {"uuid": uuid, "kind": "device_action", "device_id": device_id, "material_uuid": material_uuid,
            "resource_defaults": [f"device:{material_uuid}"]}


def test_device_local_id_in_a_scope_is_the_same_resource_as_the_action_device_alias():
    graph = {
        "nodes": [_node("a"), _node("b")],
        "edges": [{"source_node_uuid": "a", "target_node_uuid": "b"}],
        "resource_scopes": [{"scope_id": "hold", "kind": "with", "resources": ["tecan"], "node_uuids": ["a", "b"]}],
    }
    plan = compile_template_resource_plan(graph)
    assert sorted(resource.canonical_key for resource in plan.resources) == [f"symbol:device:{DEVICE}"]


def test_ambiguous_local_id_is_left_alone():
    other = "11111111-2222-3333-4444-555555555555"
    graph = {"nodes": [_node("a"), _node("b", material_uuid=other)],
             "edges": [{"source_node_uuid": "a", "target_node_uuid": "b"}]}
    plan = compile_template_resource_plan(graph)
    assert sorted(resource.canonical_key for resource in plan.resources) == sorted(
        [f"symbol:device:{DEVICE}", f"symbol:device:{other}"])
