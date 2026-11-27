"""已发布子工作流合同自动替换的纯领域回归测试。"""

from __future__ import annotations

from copy import deepcopy
from uuid import UUID, uuid5

import pytest

from unilabos.workflow.authoring_identity import expanded_node_uuid
from unilabos.workflow.composite_contract_refresh import (
    CompositeContractRefreshPending,
    refresh_published_composite_invocations,
)
from unilabos.workflow.composite_invocation import (
    CompositeInvocationInvalid,
    compile_composite_invocation,
    expand_composite_invocation,
)
from unilabos.workflow.resource_lock_plan import compile_template_resource_plan

# 下面的固定 UUID 分别代表子定义、父定义、调用根、上游节点、发布合同和边界连线；
# 固定身份使测试能够直接证明自动替换前后的节点、连接点（Handle）和边 UUID
# 是否按规则保持。
CHILD_UUID = "10000000-0000-4000-8000-000000000001"
PARENT_UUID = "10000000-0000-4000-8000-000000000002"
INVOCATION_UUID = "10000000-0000-4000-8000-000000000003"
PROVIDER_UUID = "10000000-0000-4000-8000-000000000004"
CHILD_NODE_UUID = "10000000-0000-4000-8000-000000000005"
OLD_CONTRACT_UUID = "10000000-0000-4000-8000-000000000006"
NEW_CONTRACT_UUID = "10000000-0000-4000-8000-000000000007"
OLD_TEMPLATE_UUID = "10000000-0000-4000-8000-000000000008"
NEW_TEMPLATE_UUID = "10000000-0000-4000-8000-000000000009"
PROVIDER_HANDLE_UUID = "10000000-0000-4000-8000-000000000010"
EDGE_UUID = "10000000-0000-4000-8000-000000000011"


def _input(name: str, *, required: bool) -> dict:
    """构造一个发布合同输入描述符。

    参数：``name`` 是参数名，``required`` 决定是否必填。返回：规范测试描述符；
    可选参数附带默认值。异常：无。
    """

    descriptor = {"name": name, "schema": {"type": "string"}, "required": required}
    if not required:
        descriptor["default"] = ""
    return descriptor


def _contract(
    *,
    identity: str,
    template_uuid: str,
    revision: int,
    inputs: list[dict],
) -> dict:
    """构造足以完成确定性展开的不可变发布合同。

    参数：身份、模板 UUID、修订和输入描述符共同描述一个版本。返回：含冻结图、
    边界映射和空设备要求的合同。异常：无；测试调用方负责传入规范 UUID。
    """

    return {
        "uuid": identity,
        "workflow_uuid": CHILD_UUID,
        "workflow_revision": revision,
        "node_template_uuid": template_uuid,
        "name": "子工作流",
        "source_hash": f"sha256:{revision:064x}",
        "contract_digest": f"sha256:{revision + 10:064x}",
        "input_contract": {"version": 1, "parameters": inputs},
        "output_contract": {"version": 1, "outputs": []},
        "executor_requirements": [],
        "executor_binding_mapping": {},
        "boundary_mapping": {
            "target_mappings": {},
            "source_mappings": {},
            "structural_mappings": {
                "entry_targets": [],
                "completion_sources": [],
            },
        },
        "graph_snapshot": {
            "workflow": {
                "uuid": CHILD_UUID,
                "revision": revision,
                "workflow_type": "experiment_operation",
            },
            "nodes": [
                {
                    "uuid": CHILD_NODE_UUID,
                    "name": f"动作 {revision}",
                    "type": "compute",
                    "pose": {"x": 0, "y": 0},
                    "param": {},
                    "execution_policy": {},
                    "disabled": False,
                    "minimized": False,
                    "meta_data": {},
                }
            ],
            "edges": [],
        },
    }


def _handle_uuid(template_uuid: str, name: str) -> str:
    """按生产发布规则计算一个输入连接点 UUID。

    参数：``template_uuid`` 是发布模板身份，``name`` 是输入参数名。返回：确定性
    连接点（Handle）UUID。异常：模板身份非法时由 ``UUID`` 构造器抛出
    ``ValueError``。
    """

    return str(uuid5(UUID(template_uuid), f"published-handle:target:{name}"))


def _parent_graph(contract: dict, *, param: dict | None = None) -> dict:
    """构造含一个上游节点和一个已展开子工作流调用的父图。

    参数：``contract`` 是待插入的旧发布合同，``param`` 是本次调用的固定参数，
    省略时使用测试默认值。返回：带一条外部参数连线的完整父图。异常：合同无法
    展开时传播 ``CompositeInvocationInvalid``。
    """

    base = {
        "workflow": {"uuid": PARENT_UUID, "revision": 2},
        "nodes": [
            {
                "uuid": PROVIDER_UUID,
                "name": "上游",
                "type": "compute",
                "pose": {"x": 0, "y": 0},
                "param": {},
                "execution_policy": {},
                "disabled": False,
                "minimized": False,
                "meta_data": {},
            }
        ],
        "edges": [],
    }
    expansion = compile_composite_invocation(
        parent_graph=base,
        contract=contract,
        invocation_uuid=INVOCATION_UUID,
        pose={"x": 300, "y": 100},
        param={"sample": "manual"} if param is None else param,
        device_bindings={},
    )
    return {
        **base,
        "workflow": {
            **base["workflow"],
            "meta_data": expansion.workflow_meta_data,
        },
        "nodes": [*base["nodes"], *expansion.nodes],
        "edges": [
            *expansion.edges,
            {
                "uuid": EDGE_UUID,
                "source_node_uuid": PROVIDER_UUID,
                "source_handle_uuid": PROVIDER_HANDLE_UUID,
                "target_node_uuid": INVOCATION_UUID,
                "target_handle_uuid": _handle_uuid(OLD_TEMPLATE_UUID, "sample"),
                "meta_data": {},
            },
        ],
    }


def test_composite_invocation_rejects_missing_required_input_before_expansion() -> None:
    """组合调用缺少必填参数时须在生成任何节点前关闭式拒绝。

    参数：无。返回：无。异常：若展开函数继续生成节点或没有返回稳定的参数错误，
    由断言暴露。
    """

    contract = _contract(
        identity=OLD_CONTRACT_UUID,
        template_uuid=OLD_TEMPLATE_UUID,
        revision=1,
        inputs=[_input("sample", required=True)],
    )

    with pytest.raises(CompositeInvocationInvalid, match="缺少必填输入参数"):
        _parent_graph(contract, param={})


def test_composite_invocation_rejects_unknown_input_before_expansion() -> None:
    """组合调用包含发布合同未声明的参数时须拒绝而不能静默丢弃。

    参数：无。返回：无。异常：若未知参数被静默丢弃并继续展开，或错误类型偏离
    ``CompositeInvocationInvalid``，由断言暴露。
    """

    contract = _contract(
        identity=OLD_CONTRACT_UUID,
        template_uuid=OLD_TEMPLATE_UUID,
        revision=1,
        inputs=[_input("sample", required=True)],
    )

    with pytest.raises(CompositeInvocationInvalid, match="未知输入参数"):
        _parent_graph(contract, param={"sample": "manual", "extra": "ignored"})


def _three_step_resource_scope_contract() -> tuple[dict, tuple[str, str]]:
    """构造来源旋转、目标旋转、机械臂搬运的连续预留合同。"""

    contract = _contract(
        identity=OLD_CONTRACT_UUID,
        template_uuid=OLD_TEMPLATE_UUID,
        revision=1,
        inputs=[_input("sample", required=True)],
    )
    target_rotation_uuid = "10000000-0000-4000-8000-000000000015"
    robot_transfer_uuid = "10000000-0000-4000-8000-000000000016"
    for node_uuid, name in (
        (target_rotation_uuid, "目标位旋转"),
        (robot_transfer_uuid, "机械臂搬运"),
    ):
        node = deepcopy(contract["graph_snapshot"]["nodes"][0])
        node.update({"uuid": node_uuid, "name": name})
        contract["graph_snapshot"]["nodes"].append(node)
    contract["graph_snapshot"]["edges"] = [
        {
            "uuid": "10000000-0000-4000-8000-000000000017",
            "source_node_uuid": CHILD_NODE_UUID,
            "source_handle_uuid": "10000000-0000-4000-8000-000000000018",
            "target_node_uuid": target_rotation_uuid,
            "target_handle_uuid": "10000000-0000-4000-8000-000000000019",
        },
        {
            "uuid": "10000000-0000-4000-8000-000000000020",
            "source_node_uuid": target_rotation_uuid,
            "source_handle_uuid": "10000000-0000-4000-8000-000000000021",
            "target_node_uuid": robot_transfer_uuid,
            "target_handle_uuid": "10000000-0000-4000-8000-000000000022",
        },
    ]
    contract["graph_snapshot"]["workflow"]["meta_data"] = {
        "unilab": {
            "resource_scopes": [
                {
                    "scope_id": "child-operation",
                    "kind": "with",
                    "resources": ["robot", "source-turntable", "target-turntable"],
                    "parent_scope_id": None,
                    "entry_node_uuid": CHILD_NODE_UUID,
                    "exit_node_uuid": robot_transfer_uuid,
                    "node_uuids": [
                        CHILD_NODE_UUID,
                        target_rotation_uuid,
                        robot_transfer_uuid,
                    ],
                    "hard_boundary": True,
                    "source": "authoring.with.resources",
                }
            ]
        }
    }
    return contract, (target_rotation_uuid, robot_transfer_uuid)


def test_composite_invocation_compiles_resource_scope_graph_patch() -> None:
    """冻结合同展开必须返回可与父图一起原子提交的资源作用域补丁。"""

    contract, (target_rotation_uuid, robot_transfer_uuid) = (
        _three_step_resource_scope_contract()
    )
    parent_graph = {
        "workflow": {
            "uuid": PARENT_UUID,
            "revision": 2,
            "meta_data": {},
        },
        "nodes": [],
        "edges": [],
    }

    expansion = compile_composite_invocation(
        parent_graph=parent_graph,
        contract=contract,
        invocation_uuid=INVOCATION_UUID,
        pose={"x": 300, "y": 100},
        param={"sample": "manual"},
        device_bindings={},
    )

    expanded_child_uuids = [
        expanded_node_uuid(INVOCATION_UUID, source_uuid)
        for source_uuid in (
            CHILD_NODE_UUID,
            target_rotation_uuid,
            robot_transfer_uuid,
        )
    ]
    (child_scope,) = expansion.resource_scopes
    assert child_scope["node_uuids"] == expanded_child_uuids
    assert child_scope["parent_scope_id"] is None
    assert child_scope["composite_invocation_uuid"] == INVOCATION_UUID
    assert child_scope["source_scope_id"] == "child-operation"
    assert child_scope["source_workflow_uuid"] == CHILD_UUID
    assert expansion.workflow_meta_data["unilab"]["resource_scopes"] == list(
        expansion.resource_scopes
    )

    graph = {
        **parent_graph,
        "workflow": {
            **parent_graph["workflow"],
            "meta_data": expansion.workflow_meta_data,
        },
        "nodes": list(expansion.nodes),
        "edges": list(expansion.edges),
    }
    plan = compile_template_resource_plan(graph)
    for alias in ("robot", "source-turntable", "target-turntable"):
        resource_id = next(
            resource.resource_id for resource in plan.resources if resource.alias == alias
        )
        intervals = [
            interval
            for interval in plan.intervals
            if interval.resource_id == resource_id
            and interval.scope_id == child_scope["scope_id"]
        ]
        assert len(intervals) == 1
        assert intervals[0].node_uuids == tuple(expanded_child_uuids)
        assert intervals[0].explicit_boundary is True


def test_covering_parent_scope_does_not_create_resource_cycle() -> None:
    """覆盖组合调用的父作用域不应与三设备子预留形成取得环。"""

    contract, _node_uuids = _three_step_resource_scope_contract()
    parent_graph = {
        "workflow": {
            "uuid": PARENT_UUID,
            "revision": 2,
            "meta_data": {
                "unilab": {
                    "resource_scopes": [
                        {
                            "scope_id": "parent-operation",
                            "kind": "with",
                            "resources": ["parent-lock"],
                            "parent_scope_id": None,
                            "entry_node_uuid": INVOCATION_UUID,
                            "exit_node_uuid": INVOCATION_UUID,
                            "node_uuids": [INVOCATION_UUID],
                            "hard_boundary": True,
                            "source": "authoring.with.resources",
                        }
                    ]
                }
            },
        },
        "nodes": [],
        "edges": [],
    }
    expansion = compile_composite_invocation(
        parent_graph=parent_graph,
        contract=contract,
        invocation_uuid=INVOCATION_UUID,
        pose={"x": 300, "y": 100},
        param={"sample": "manual"},
        device_bindings={},
    )
    graph = {
        **parent_graph,
        "workflow": {
            **parent_graph["workflow"],
            "meta_data": expansion.workflow_meta_data,
        },
        "nodes": list(expansion.nodes),
        "edges": list(expansion.edges),
    }

    plan = compile_template_resource_plan(graph)

    child_scope = next(
        scope
        for scope in plan.scopes
        if scope.parent_scope_id == "parent-operation"
    )
    assert child_scope.parent_scope_id == "parent-operation"


def test_composite_invocation_remaps_control_region_references() -> None:
    """展开后控制节点参数中的内部 UUID 必须与复制节点保持一致。"""

    contract = _contract(
        identity=OLD_CONTRACT_UUID,
        template_uuid=OLD_TEMPLATE_UUID,
        revision=1,
        inputs=[_input("sample", required=True)],
    )
    control_uuid = "10000000-0000-0000-0000-000000000012"
    body_uuid = CHILD_NODE_UUID
    control = {
        "uuid": control_uuid,
        "name": "重复直到",
        "type": "repeat_until",
        "pose": {"x": 0, "y": 0},
        "param": {
            "node_uuids": [body_uuid],
            "entry_node_uuids": [body_uuid],
            "exit_node_uuids": [body_uuid],
            "predecessor_node_uuids": [],
            "successor_node_uuids": [],
            "initial_carry": {
                "x": {"kind": "node_result", "node_uuid": body_uuid}
            },
        },
        "execution_policy": {},
        "disabled": False,
        "minimized": False,
        "meta_data": {
            "unilab": {
                "carry_bindings": {
                    "slot": {"control_region_uuid": control_uuid, "key": "x"}
                }
            }
        },
    }
    contract["graph_snapshot"]["nodes"].extend([control])
    expanded_nodes, _ = expand_composite_invocation(
        parent_graph={"workflow": {"uuid": PARENT_UUID, "revision": 2}, "nodes": [], "edges": []},
        contract=contract,
        invocation_uuid=INVOCATION_UUID,
        pose={"x": 300, "y": 100},
        param={"sample": "manual"},
        device_bindings={},
    )

    expanded_control = next(
        node for node in expanded_nodes if node["type"] == "repeat_until"
    )
    expanded_body_uuid = str(
        next(node for node in expanded_nodes if node["uuid"] != INVOCATION_UUID)["uuid"]
    )
    assert expanded_control["param"]["node_uuids"] == [expanded_body_uuid]
    assert expanded_control["param"]["initial_carry"]["x"]["node_uuid"] == expanded_body_uuid
    assert expanded_control["meta_data"]["unilab"]["carry_bindings"]["slot"]["control_region_uuid"] == expanded_control["uuid"]


def test_published_invocation_materializes_repeat_inputs_before_target_mapping() -> None:
    """发布合同展开时也必须固化无动作目标的 RepeatUntil 输入。

    参数：无。返回：无。异常：如果真实组合展开路径在目标映射处理前直接返回，
    控制节点仍会携带子工作流输入绑定，父图保存时会被严格 I/O 校验拒绝。
    """

    node_uuid = CHILD_NODE_UUID
    template_uuid = OLD_TEMPLATE_UUID
    boundary_uuid = _handle_uuid(template_uuid, "repeat_count")
    source_node = {
        "uuid": node_uuid,
        "name": "循环控制",
        # 旧冻结快照可能只保存 node_type；真实展开不能因此退回普通动作路径。
        "node_type": "repeat_until",
        "pose": {"x": 0, "y": 0},
        "param": {
            "bindings": {
                "repeat_count": {
                    "kind": "workflow_input",
                    "parameter": "repeat_count",
                }
            },
            "until": {"var": "repeat_count"},
            "initial_carry": {"count": {"kind": "literal", "value": 0}},
            "next_carry": {"count": {"kind": "literal", "value": 1}},
        },
        "meta_data": {},
    }
    contract = _contract(
        identity=OLD_CONTRACT_UUID,
        template_uuid=template_uuid,
        revision=1,
        inputs=[
            {
                "name": "repeat_count",
                "schema": {"type": "integer"},
                "required": True,
            }
        ],
    )
    contract["boundary_mapping"]["target_mappings"] = {boundary_uuid: []}
    contract["graph_snapshot"]["nodes"] = [source_node]

    expanded_nodes, _ = expand_composite_invocation(
        parent_graph={
            "workflow": {"uuid": PARENT_UUID, "revision": 1},
            "nodes": [],
            "edges": [],
        },
        contract=contract,
        invocation_uuid=INVOCATION_UUID,
        pose={"x": 100, "y": 100},
        param={"repeat_count": 3},
        device_bindings={},
    )
    expanded_node = next(
        node for node in expanded_nodes if node["uuid"] != INVOCATION_UUID
    )

    assert expanded_node["param"]["bindings"] == {}
    assert expanded_node["param"]["until"] == {"lit": 3}


def test_refresh_preserves_invocation_and_remaps_boundary_by_parameter_name() -> None:
    """兼容更新须保留调用身份和填写值，并按参数名迁移外部连线。

    参数：无。返回：无。异常：稳定身份、填写值、位置、合同 pin 或连线连接点
    任一未正确迁移时由断言暴露。
    """

    previous = _contract(
        identity=OLD_CONTRACT_UUID,
        template_uuid=OLD_TEMPLATE_UUID,
        revision=1,
        inputs=[_input("sample", required=True)],
    )
    current = _contract(
        identity=NEW_CONTRACT_UUID,
        template_uuid=NEW_TEMPLATE_UUID,
        revision=2,
        inputs=[_input("sample", required=True), _input("note", required=False)],
    )
    refreshed = refresh_published_composite_invocations(
        parent_graph=_parent_graph(previous),
        current_contract=current,
        load_contract=lambda _identity: previous,
        validate_bindings=lambda _requirements, _bindings: True,
    )

    root = next(
        node for node in refreshed.graph["nodes"] if node["uuid"] == INVOCATION_UUID
    )
    boundary = next(
        edge
        for edge in refreshed.graph["edges"]
        if edge["target_node_uuid"] == INVOCATION_UUID
    )
    assert refreshed.invocation_uuids == (INVOCATION_UUID,)
    assert root["param"] == {"sample": "manual", "note": ""}
    assert root["pose"] == {"x": 300, "y": 100}
    assert root["meta_data"]["unilab"]["composite"]["contract_uuid"] == (
        NEW_CONTRACT_UUID
    )
    assert boundary["target_handle_uuid"] == _handle_uuid(NEW_TEMPLATE_UUID, "sample")
    assert boundary["uuid"] != EDGE_UUID


def test_refresh_replaces_persisted_composite_resource_scopes() -> None:
    """合同刷新必须删除旧子作用域，并把新作用域与图一起返回。"""

    previous = _contract(
        identity=OLD_CONTRACT_UUID,
        template_uuid=OLD_TEMPLATE_UUID,
        revision=1,
        inputs=[_input("sample", required=True)],
    )
    current = _contract(
        identity=NEW_CONTRACT_UUID,
        template_uuid=NEW_TEMPLATE_UUID,
        revision=2,
        inputs=[_input("sample", required=True)],
    )
    for contract, resource in ((previous, "old-device"), (current, "new-device")):
        contract["graph_snapshot"]["workflow"]["meta_data"] = {
            "unilab": {
                "resource_scopes": [
                    {
                        "scope_id": "child-operation",
                        "kind": "with",
                        "resources": [resource],
                        "parent_scope_id": None,
                        "entry_node_uuid": CHILD_NODE_UUID,
                        "exit_node_uuid": CHILD_NODE_UUID,
                        "node_uuids": [CHILD_NODE_UUID],
                        "hard_boundary": True,
                        "source": "authoring.with.resources",
                    }
                ]
            }
        }

    parent_graph = _parent_graph(previous)
    stale_nested_scope = deepcopy(
        parent_graph["workflow"]["meta_data"]["unilab"]["resource_scopes"][0]
    )
    stale_nested_scope.update(
        {
            "scope_id": "stale-nested-scope",
            "parent_scope_id": None,
            "composite_invocation_uuid": expanded_node_uuid(
                INVOCATION_UUID,
                CHILD_NODE_UUID,
            ),
        }
    )
    parent_graph["workflow"]["meta_data"]["unilab"]["resource_scopes"].append(
        stale_nested_scope
    )
    refreshed = refresh_published_composite_invocations(
        parent_graph=parent_graph,
        current_contract=current,
        load_contract=lambda _identity: previous,
        validate_bindings=lambda _requirements, _bindings: True,
    )

    scopes = refreshed.graph["workflow"]["meta_data"]["unilab"][
        "resource_scopes"
    ]
    assert len(scopes) == 1
    assert scopes[0]["resources"] == ["new-device"]
    assert scopes[0]["composite_invocation_uuid"] == INVOCATION_UUID


def test_refresh_rejects_new_required_input_without_mutating_parent() -> None:
    """新增必填参数没有提供者时须保留父图原样并给出稳定诊断。

    参数：无。返回：无。异常：没有关闭式拒绝、诊断码变化或输入父图被就地修改
    时由断言暴露。
    """

    previous = _contract(
        identity=OLD_CONTRACT_UUID,
        template_uuid=OLD_TEMPLATE_UUID,
        revision=1,
        inputs=[_input("sample", required=True)],
    )
    current = _contract(
        identity=NEW_CONTRACT_UUID,
        template_uuid=NEW_TEMPLATE_UUID,
        revision=2,
        inputs=[_input("sample", required=True), _input("operator", required=True)],
    )
    parent = _parent_graph(previous)
    original = deepcopy(parent)

    with pytest.raises(CompositeContractRefreshPending) as raised:
        refresh_published_composite_invocations(
            parent_graph=parent,
            current_contract=current,
            load_contract=lambda _identity: previous,
            validate_bindings=lambda _requirements, _bindings: True,
        )

    assert raised.value.code == "composite_input_required"
    assert parent == original


def test_refresh_remaps_workflow_input_binding_handles() -> None:
    """发布合同更新时，父参数绑定随调用模板一起迁移，不能留下旧 Handle。"""
    previous = _contract(identity=OLD_CONTRACT_UUID, template_uuid=OLD_TEMPLATE_UUID,
                         revision=1, inputs=[_input("sample", required=True)])
    current = _contract(identity=NEW_CONTRACT_UUID, template_uuid=NEW_TEMPLATE_UUID,
                        revision=2, inputs=[_input("sample", required=True)])
    graph = _parent_graph(previous)
    root = next(n for n in graph['nodes'] if n['uuid'] == INVOCATION_UUID)
    root['meta_data']['unilab']['input_bindings'] = {
        _handle_uuid(OLD_TEMPLATE_UUID, 'sample'): {'parameter': 'parent_sample'}
    }
    refreshed = refresh_published_composite_invocations(
        parent_graph=graph, current_contract=current,
        load_contract=lambda _: previous,
        validate_bindings=lambda _requirements, _bindings: True,
    )
    root = next(n for n in refreshed.graph['nodes'] if n['uuid'] == INVOCATION_UUID)
    assert root['meta_data']['unilab']['input_bindings'] == {
        _handle_uuid(NEW_TEMPLATE_UUID, 'sample'): {'parameter': 'parent_sample'}
    }
