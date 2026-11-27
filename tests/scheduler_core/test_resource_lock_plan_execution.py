"""资源计划进入执行计划和旧调度模型的模块测试。"""

from __future__ import annotations

import subprocess
import sys
from copy import deepcopy

import pytest

from unilabos.app.scheduler.models import node_from_dict, spec_from_dict
from unilabos.workflow.execution_plan import ExecutionPlanBuilder, ExecutionPlanBuildError
from unilabos.workflow.resource_lock_key import canonical_resource_lock_scope, named_resource_lock_key
from unilabos.workflow.resource_lock_plan import (
    RESOURCE_PLAN_CAPABILITY,
    ResourcePlan,
    ResourcePlanError,
    bind_station_resource_plan,
    compile_template_resource_plan,
    deserialize_resource_plan,
    serialize_resource_plan,
)
from unilabos.workflow.workflow_spec_compiler import (
    WorkflowSpecCompilationError,
    WorkflowSpecCompiler,
)


def _bound_plan() -> tuple[ResourcePlan, dict[str, object]]:
    """构造一个最小 bound 资源计划与序列化字典。"""

    template = compile_template_resource_plan(
        {
            "workflow_uuid": "workflow-spec-1",
            "nodes": [{"uuid": "action", "resource_defaults": ["robot"]}],
            "resource_scopes": [
                {
                    "scope_id": "operation",
                    "kind": "with",
                    "resources": ["robot"],
                    "node_uuids": ["action"],
                }
            ],
        }
    )
    bound = bind_station_resource_plan(
        template,
        {"robot": "00000000-0000-4000-8000-000000000001"},
    )
    return bound, serialize_resource_plan(bound)


def test_execution_plan_imports_in_clean_process_without_scheduler_cycle() -> None:
    """纯 Workflow 消费方导入 ExecutionPlan 时不能触发 Scheduler 回导。"""

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from unilabos.workflow.execution_plan import ExecutionPlanBuilder",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_execution_plan_builder_projects_bound_resource_plan() -> None:
    """执行计划 seam 应把 bound 计划和节点区间身份一起投影。"""

    plan = ExecutionPlanBuilder._resource_plan(
        graph={
            "resources": ["robot"],
            "resource_bindings": {
                "robot": "00000000-0000-4000-8000-000000000001",
            },
        },
        planned_nodes=[{"uuid": "action", "resource_defaults": ["robot"]}],
        planned_edges=[],
    )

    assert plan is not None
    assert plan.binding_state == "bound"
    assert plan.plan_id
    assert plan.resources[0].canonical_key.startswith("resource:")


@pytest.mark.parametrize(
    ("canonical_key", "expected_kind"),
    [
        ("/devices/legacy-reactor", "device"),
        ("material/legacy-vessel/exclusive", "material"),
        (
            "material/legacy-rack/site/legacy-position/exclusive",
            "material_site",
        ),
    ],
)
def test_deserialize_normalizes_only_legacy_generic_kind_on_strict_physical_key(
    canonical_key: str,
    expected_kind: str,
) -> None:
    """历史 kind=resource 物理键可恢复，但只按严格键语法规范角色。"""

    template = compile_template_resource_plan(
        {
            "workflow_uuid": "workflow-legacy-kind",
            "nodes": [{"uuid": "action", "resource_defaults": ["robot"]}],
        }
    )
    plan = bind_station_resource_plan(
        template,
        {
            "robot": {
                "canonical_key": canonical_key,
                "kind": expected_kind,
            }
        },
    )
    serialized = serialize_resource_plan(plan)
    serialized["version"] = 1
    serialized["plan_id"] = "f5121aea-174d-5a60-96fe-7a13219319d9"
    serialized["metadata"] = {"workflow_instance_id": "workflow-legacy-kind"}
    serialized["acquire_sets"][0]["acquire_set_id"] = (
        "3cc72e31-4b1a-5d00-ab30-d60c1751a494"
    )
    serialized["resources"][0]["kind"] = "resource"

    restored = deserialize_resource_plan(serialized)

    assert restored.plan_id == "f5121aea-174d-5a60-96fe-7a13219319d9"
    assert restored.version == 1
    assert restored.resources[0].kind == expected_kind
    assert restored.resources[0].canonical_key == canonical_key


def test_deserialize_rejects_valid_resource_identity_tamper_under_old_plan_id() -> None:
    """合法命名键也不能在保留冻结 plan_id 时替换原资源身份。"""

    plan = ExecutionPlanBuilder._resource_plan(
        graph={"resources": ["station_mutex"]},
        planned_nodes=[{"uuid": "action"}],
        planned_edges=[],
    )
    assert plan is not None
    serialized = serialize_resource_plan(plan)
    tampered_key = named_resource_lock_key("different_station_mutex")
    serialized["resources"][0]["canonical_key"] = tampered_key
    serialized["resources"][0]["instance_uuid"] = tampered_key.removeprefix(
        "resource:"
    )

    with pytest.raises(ResourcePlanError, match="内容与 plan_id 不一致"):
        deserialize_resource_plan(serialized)


@pytest.mark.parametrize("section", ["scopes", "intervals", "acquire_sets"])
def test_deserialize_rejects_valid_boundary_tamper_under_old_plan_id(
    section: str,
) -> None:
    """合法作用域、区间或取得集合字段变化必须产生新的冻结计划身份。"""

    _plan, serialized = _bound_plan()
    tampered = deepcopy(serialized)
    assert tampered[section]
    tampered[section][0]["source"] = "tampered-but-structurally-valid"

    with pytest.raises(ResourcePlanError, match="内容与 plan_id 不一致"):
        deserialize_resource_plan(tampered)


def test_deserialize_rejects_generic_resource_key_in_legacy_v1_plan() -> None:
    """v1 没有通用命名键语义，不能借兼容路径绕过 v2 内容身份校验。"""

    plan = ExecutionPlanBuilder._resource_plan(
        graph={"resources": ["station_mutex"]},
        planned_nodes=[{"uuid": "action"}],
        planned_edges=[],
    )
    assert plan is not None
    serialized = serialize_resource_plan(plan)
    serialized["version"] = 1
    serialized["plan_id"] = "legacy-plan-with-generic-key"

    with pytest.raises(ResourcePlanError) as caught:
        deserialize_resource_plan(serialized)

    assert caught.value.code == "unsupported_legacy_resource"


def test_deserialize_rejects_content_addressed_v2_plan_downgraded_to_v1() -> None:
    """只改版本号不能把新计划降级到不校验内容身份的兼容路径。"""

    template = compile_template_resource_plan(
        {
            "workflow_uuid": "workflow-version-downgrade",
            "nodes": [{"uuid": "action", "resource_defaults": ["robot"]}],
        }
    )
    plan = bind_station_resource_plan(
        template,
        {
            "robot": {
                "canonical_key": "/devices/legacy-reactor",
                "kind": "device",
            }
        },
    )
    serialized = serialize_resource_plan(plan)
    serialized["version"] = 1

    with pytest.raises(ResourcePlanError) as caught:
        deserialize_resource_plan(serialized)

    assert caught.value.code == "plan_version_downgrade"


def test_deserialize_rejects_tampered_v2_plan_disguised_as_legacy_v1() -> None:
    """篡改内容后再降级版本也不能进入旧计划兼容路径。"""

    template = compile_template_resource_plan(
        {
            "workflow_uuid": "workflow-tampered-version-downgrade",
            "nodes": [{"uuid": "action", "resource_defaults": ["robot"]}],
        }
    )
    plan = bind_station_resource_plan(
        template,
        {
            "robot": {
                "canonical_key": "/devices/legacy-reactor",
                "kind": "device",
            }
        },
    )
    serialized = serialize_resource_plan(plan)
    serialized["version"] = 1
    serialized["metadata"] = {
        "workflow_instance_id": "workflow-tampered-version-downgrade"
    }
    serialized["intervals"][0]["source"] = "tampered-after-downgrade"

    with pytest.raises(ResourcePlanError) as caught:
        deserialize_resource_plan(serialized)

    assert caught.value.code == "plan_version_downgrade"


def test_deserialize_v2_does_not_apply_legacy_resource_kind_normalization() -> None:
    """v2 的 kind 变化属于内容篡改，不能借历史归一化恢复原身份。"""

    template = compile_template_resource_plan(
        {
            "workflow_uuid": "workflow-v2-kind-tamper",
            "nodes": [{"uuid": "action", "resource_defaults": ["robot"]}],
        }
    )
    plan = bind_station_resource_plan(
        template,
        {
            "robot": {
                "canonical_key": "/devices/legacy-reactor",
                "kind": "device",
            }
        },
    )
    serialized = serialize_resource_plan(plan)
    serialized["resources"][0]["kind"] = "resource"

    with pytest.raises(ResourcePlanError) as caught:
        deserialize_resource_plan(serialized)

    assert caught.value.code == "plan_identity_mismatch"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("version",), "2"),
        (("acquire_sets", 0, "atomic"), "false"),
        (("resources",), "not-an-array"),
        (("resources", 0), "not-an-object"),
        (("diagnostics", 0), "not-an-object"),
    ],
)
def test_deserialize_rejects_type_coercion_and_malformed_members(
    path: tuple[str | int, ...],
    value: object,
) -> None:
    """持久化计划必须严格匹配 wire 类型，不能强转或跳过坏成员。"""

    _plan, serialized = _bound_plan()
    if path[0] == "diagnostics":
        serialized["diagnostics"].append({})
    target: object = serialized
    for part in path[:-1]:
        target = target[part]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]

    with pytest.raises(ResourcePlanError) as caught:
        deserialize_resource_plan(serialized)

    assert caught.value.code == "invalid_plan"


@pytest.mark.parametrize(
    ("section", "extra_field"),
    [
        (None, "unexpected_root"),
        ("resources", "unexpected_resource"),
        ("scopes", "unexpected_scope"),
        ("intervals", "unexpected_interval"),
        ("acquire_sets", "unexpected_acquire_set"),
        ("relations", "unexpected_relation"),
    ],
)
def test_deserialize_rejects_unknown_wire_fields(
    section: str | None,
    extra_field: str,
) -> None:
    """版本化 wire 合同关闭式拒绝未知字段，避免字段拼错后静默失效。"""

    _plan, serialized = _bound_plan()
    if section is None:
        serialized[extra_field] = True
    else:
        resource_id = serialized["resources"][0]["resource_id"]
        if section == "scopes" and not serialized[section]:
            serialized[section].append(
                {
                    "scope_id": "wire-scope",
                    "kind": "with",
                    "resource_ids": [resource_id],
                    "parent_scope_id": None,
                    "entry_node_uuid": "action",
                    "exit_node_uuid": "action",
                    "node_uuids": ["action"],
                    "hard_boundary": True,
                    "branch_id": "",
                    "source": "test",
                }
            )
        if section == "relations" and not serialized[section]:
            serialized[section].append(
                {
                    "relation_id": "wire-relation",
                    "from_resource_id": resource_id,
                    "to_resource_id": resource_id,
                    "source_interval_id": "",
                    "source_node_uuid": "action",
                    "branch_id": "",
                    "possible_concurrency": True,
                    "reason": "test",
                }
            )
        assert serialized[section]
        serialized[section][0][extra_field] = True

    with pytest.raises(ResourcePlanError) as caught:
        deserialize_resource_plan(serialized)

    assert caught.value.code == "invalid_plan"


def test_execution_plan_builder_reads_v2_action_resource_params() -> None:
    """v2 Action 参数角色可通过任务绑定映射进入资源计划。"""

    plan = ExecutionPlanBuilder._resource_plan(
        graph={
            "resource_bindings": {
                "executor": "00000000-0000-4000-8000-000000000001"
            }
        },
        planned_nodes=[
            {
                "uuid": "action",
                "action_resource_contract": {
                    "version": 2,
                    "resource_params": [
                        {"param": "executor", "role": "device"}
                    ],
                },
            }
        ],
        planned_edges=[],
    )

    assert plan is not None
    assert plan.binding_state == "bound"
    assert plan.resources[0].alias == "executor"


@pytest.mark.parametrize("role", ["material", "device", "motion", "tool"])
@pytest.mark.parametrize("same_instance", [False, True])
def test_action_parameter_bindings_are_local_to_each_invocation(
    role: str, same_instance: bool,
) -> None:
    """同名动作参数可指向不同实例，同实例仍合并为同一物理互斥键。"""

    identities = ["00000000-0000-4000-8000-000000000001",
                  "00000000-0000-4000-8000-000000000002"]
    if same_instance:
        identities[1] = identities[0]
    nodes = [{"uuid": f"action-{index}", "kind": "device_action",
              "param": {"plate": {"uuid": identity}},
              "action_resource_contract": {"version": 2,
                  "resource_params": [{"param": "plate", "role": role}]}}
             for index, identity in enumerate(identities)]
    original = deepcopy(nodes)
    plan = ExecutionPlanBuilder._resource_plan(
        graph={}, planned_nodes=nodes, planned_edges=[],
    )
    keys = [(f"material/{identity}/exclusive" if role == "material"
             else f"/devices/{identity}") for identity in identities]
    assert {item.canonical_key for item in plan.resources} == set(keys)
    by_id = {item.resource_id: item.canonical_key for item in plan.resources}
    for index, identity in enumerate(identities):
        assert {by_id[item.resource_id] for item in plan.intervals
                if f"action-{index}" in item.node_uuids} == {
            keys[index],
        }
    assert nodes == original


@pytest.mark.parametrize("role", ["material", "device"])
@pytest.mark.parametrize("alias", ["plate", "node:action:param:plate"])
def test_action_parameter_rejects_conflicting_explicit_global_binding(
    role: str, alias: str,
) -> None:
    """显式全局资源绑定不得被动作参数自动投影覆盖。"""

    nodes = [{"uuid": "action", "param": {"plate": {
        "uuid": "00000000-0000-4000-8000-000000000001"}},
        "action_resource_contract": {"version": 2,
            "resource_params": [{"param": "plate", "role": role}]}}]
    with pytest.raises(ExecutionPlanBuildError, match="冻结动作参数实例不一致"):
        ExecutionPlanBuilder._resource_plan(
            graph={"resource_bindings": {alias: {
                "instance_uuid": "00000000-0000-4000-8000-000000000002", "kind": role}}},
            planned_nodes=nodes, planned_edges=[],
        )


def test_execution_plan_builder_accepts_root_and_lexical_resource_declarations() -> None:
    """执行计划 seam 同时接受工作流根资源与词法作用域。"""

    plan = ExecutionPlanBuilder._resource_plan(
        graph={
            "resources": ["batch"],
            "resource_scopes": [
                {
                    "scope_id": "lexical",
                    "kind": "with",
                    "resources": ["robot"],
                    "node_uuids": ["inside"],
                }
            ],
        },
        planned_nodes=[
            {"uuid": "before", "resource_defaults": ["batch"]},
            {"uuid": "inside", "resource_defaults": ["batch", "robot"]},
        ],
        planned_edges=[
            {"source_node_uuid": "before", "target_node_uuid": "inside"}
        ],
    )

    assert plan is not None
    assert {scope.scope_id for scope in plan.scopes} == {"root", "lexical"}


def test_explicit_root_resource_becomes_stable_named_mutex_without_inventory_binding() -> None:
    """作者显式声明的通用资源可直接冻结为跨 Workflow 稳定的互斥键。"""

    def build(workflow_uuid: str):
        return ExecutionPlanBuilder._resource_plan(
            graph={"workflow_uuid": workflow_uuid, "resources": ["station_mutex"]},
            planned_nodes=[{"uuid": "action"}],
            planned_edges=[],
        )

    first = build("workflow-named-mutex-a")
    second = build("workflow-named-mutex-b")

    assert first is not None and second is not None
    assert first.binding_state == second.binding_state == "bound"
    assert first.resources[0].kind == second.resources[0].kind == "resource"
    assert first.resources[0].canonical_key == second.resources[0].canonical_key
    assert first.resources[0].canonical_key.startswith("resource:")
    assert len(first.resources[0].canonical_key) == len("resource:") + 36


def test_explicit_lexical_resource_becomes_stable_named_mutex_without_binding() -> None:
    """词法 resources 作用域也把同名别名冻结为跨 Workflow 稳定互斥键。"""

    def build(workflow_uuid: str):
        return ExecutionPlanBuilder._resource_plan(
            graph={
                "workflow_uuid": workflow_uuid,
                "resource_scopes": [
                    {
                        "scope_id": "critical-section",
                        "kind": "with",
                        "resources": ["station_mutex"],
                        "node_uuids": ["inside"],
                    }
                ],
            },
            planned_nodes=[{"uuid": "inside"}],
            planned_edges=[],
        )

    first = build("workflow-lexical-mutex-a")
    second = build("workflow-lexical-mutex-b")

    assert first is not None and second is not None
    assert first.binding_state == second.binding_state == "bound"
    assert first.resources[0].kind == second.resources[0].kind == "resource"
    assert first.resources[0].canonical_key == second.resources[0].canonical_key
    assert first.scopes[0].resource_ids == (first.resources[0].resource_id,)


def test_explicit_binding_wins_over_named_mutex_fallback() -> None:
    """同一作用域别名有真实设备绑定时必须继续使用真实实例身份。"""

    device_uuid = "00000000-0000-4000-8000-000000000011"
    plan = ExecutionPlanBuilder._resource_plan(
        graph={
            "resources": ["station_mutex"],
            "resource_bindings": {
                "station_mutex": {"instance_uuid": device_uuid, "kind": "device"}
            },
        },
        planned_nodes=[{"uuid": "action"}],
        planned_edges=[],
    )

    assert plan is not None
    assert plan.resources[0].kind == "device"
    assert plan.resources[0].canonical_key == f"/devices/{device_uuid}"


def test_persisted_workflow_metadata_resource_binding_wins_named_mutex_fallback() -> None:
    """作者图可持久化的 Workflow 元数据绑定必须进入 ExecutionPlan。"""

    device_uuid = "00000000-0000-4000-8000-000000000012"
    plan = ExecutionPlanBuilder._resource_plan(
        graph={
            "workflow": {
                "meta_data": {
                    "unilab": {
                        "resources": ["station_mutex"],
                        "resource_bindings": {
                            "station_mutex": {
                                "instance_uuid": device_uuid,
                                "kind": "device",
                            }
                        },
                    }
                }
            }
        },
        planned_nodes=[{"uuid": "action"}],
        planned_edges=[],
    )

    assert plan is not None
    assert plan.resources[0].canonical_key == f"/devices/{device_uuid}"


def test_unbound_action_resource_is_not_downgraded_to_generic_mutex() -> None:
    """动作合同缺少库存绑定时不能用命名锁掩盖配置错误。"""

    plan = ExecutionPlanBuilder._resource_plan(
        graph={},
        planned_nodes=[{"uuid": "action", "resource_defaults": ["executor"]}],
        planned_edges=[],
    )

    assert plan is not None
    assert plan.binding_state == "template"
    assert plan.resources[0].canonical_key == "symbol:executor"


def test_malformed_explicit_generic_key_is_rejected() -> None:
    """调用方不能把任意非空字符串伪装成通用资源规范键。"""

    with pytest.raises(ExecutionPlanBuildError, match="通用资源锁键"):
        ExecutionPlanBuilder._resource_plan(
            graph={
                "resources": ["station_mutex"],
                "resource_bindings": {
                    "station_mutex": {
                        "kind": "resource",
                        "canonical_key": "resource:opaque",
                    }
                },
            },
            planned_nodes=[{"uuid": "action"}],
            planned_edges=[],
        )


@pytest.mark.parametrize(
    ("kind", "canonical_key"),
    [
        ("device", "material/00000000-0000-4000-8000-000000000013/exclusive"),
        ("material", "/foo/00000000-0000-4000-8000-000000000013"),
        ("resource", "/devices/00000000-0000-4000-8000-000000000013"),
    ],
)
def test_binding_kind_must_match_canonical_key_shape(
    kind: str,
    canonical_key: str,
) -> None:
    """绑定角色与物理/通用锁键语法不能漂移。"""

    with pytest.raises(ExecutionPlanBuildError, match="资源绑定.*不匹配|锁键格式"):
        ExecutionPlanBuilder._resource_plan(
            graph={
                "resources": ["station_mutex"],
                "resource_bindings": {
                    "station_mutex": {
                        "kind": kind,
                        "canonical_key": canonical_key,
                    }
                },
            },
            planned_nodes=[{"uuid": "action"}],
            planned_edges=[],
        )


@pytest.mark.parametrize(
    "lock_key",
    [
        "material//exclusive",
        "material/ /exclusive",
        "material/owner/site//exclusive",
        "material/owner/site/ /exclusive",
        "/devices/",
        "/devices/ executor",
    ],
)
def test_canonical_lock_grammar_rejects_empty_or_whitespace_identity(
    lock_key: str,
) -> None:
    """规范物理锁键的每个身份段都必须非空且无首尾空白。"""

    assert canonical_resource_lock_scope(lock_key) is None


def test_execution_plan_scope_projection_ignores_authoring_group_nodes() -> None:
    """作者作用域包住展示 Group 时，计划只保留其真实执行子节点。"""

    plan = ExecutionPlanBuilder._resource_plan(
        graph={
            "workflow": {
                "meta_data": {
                    "unilab": {
                        "resource_scopes": [
                            {
                                "scope_id": "lexical",
                                "kind": "with",
                                "resources": ["robot"],
                                "entry_node_uuid": "group",
                                "exit_node_uuid": "inside",
                                "node_uuids": ["group", "inside"],
                            }
                        ]
                    }
                }
            }
        },
        planned_nodes=[{"uuid": "inside", "resource_defaults": ["robot"]}],
        planned_edges=[],
    )

    assert plan is not None
    scope = next(item for item in plan.scopes if item.scope_id == "lexical")
    assert scope.node_uuids == ("inside",)
    assert scope.entry_node_uuid == scope.exit_node_uuid == "inside"


def test_workflow_spec_compiler_rejects_template_resource_plan() -> None:
    """任务运行时不得绕过工站绑定直接消费 template 计划。"""

    template = compile_template_resource_plan(
        {
            "workflow_uuid": "workflow-spec-unbound",
            "nodes": [{"uuid": "action", "resource_defaults": ["robot"]}],
        }
    )
    task_snapshot = {
        "uuid": "00000000-0000-4000-8000-000000000099",
        "execution_plan": {
            "version": 1,
            "nodes": [],
            "edges": [],
            "handles": [],
            "resource_plan": serialize_resource_plan(template),
        },
    }

    with pytest.raises(WorkflowSpecCompilationError) as caught:
        WorkflowSpecCompiler().compile(task_snapshot, [])

    assert caught.value.code == "resource_plan_unbound"


def test_workflow_spec_compiler_rejects_dag_capability_without_plan() -> None:
    """静态无环能力不能脱离资源计划单独声明。"""

    task_snapshot = {
        "uuid": "00000000-0000-4000-8000-000000000097",
        "execution_plan": {
            "version": 1,
            "nodes": [],
            "edges": [],
            "handles": [],
            "capabilities": ["static_resource_dag_v1"],
        },
    }

    with pytest.raises(WorkflowSpecCompilationError) as caught:
        WorkflowSpecCompiler().compile(task_snapshot, [])

    assert caught.value.code == "invalid_resource_plan"


def test_workflow_spec_compiler_accepts_bound_resource_plan() -> None:
    """已绑定且含静态 DAG 能力的计划可进入 WorkflowSpec。"""

    _bound, serialized = _bound_plan()
    task_snapshot = {
        "uuid": "00000000-0000-4000-8000-000000000098",
        "execution_plan": {
            "version": 1,
            "nodes": [],
            "edges": [],
            "handles": [],
            "capabilities": [RESOURCE_PLAN_CAPABILITY],
            "resource_plan": serialized,
        },
    }

    spec = WorkflowSpecCompiler().compile(task_snapshot, [])

    assert spec.resource_plan == serialized


def test_scheduler_models_preserve_resource_plan_projection() -> None:
    """旧调度模型反序列化时保留节点与 WorkflowSpec 的计划身份。"""

    node = node_from_dict(
        {
            "id": "action",
            "resource_plan_id": "plan-1",
            "resource_interval_ids": ["interval-1"],
            "resource_acquire_set_id": "acquire-1",
        }
    )
    spec = spec_from_dict(
        {
            "workflow_id": "workflow-1",
            "nodes": [
                {
                    "id": "action",
                    "resource_plan_id": "plan-1",
                    "resource_interval_ids": ["interval-1"],
                    "resource_acquire_set_id": "acquire-1",
                }
            ],
            "resource_plan": {"plan_id": "plan-1", "binding_state": "bound"},
        }
    )

    assert node.resource_plan_id == "plan-1"
    assert node.resource_interval_ids == ["interval-1"]
    assert node.resource_acquire_set_id == "acquire-1"
    assert spec.resource_plan == {"plan_id": "plan-1", "binding_state": "bound"}
