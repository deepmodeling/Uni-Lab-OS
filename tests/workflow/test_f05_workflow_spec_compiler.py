"""F05.3-A 工作流规格编译器（WorkflowSpecCompiler）的失败关闭合同。"""

from __future__ import annotations

from importlib import import_module
from typing import Any

import pytest

from unilabos.workflow.execution_plan import (
    ExecutionPlanBuilder,
    ExecutionPlanBuildError,
)
from unilabos.workflow.inventory_resource_plan import bind_inventory_resource_plan
from unilabos.workflow.resource_lock_plan import (
    bind_station_resource_plan,
    compile_template_resource_plan,
    deserialize_resource_plan,
    serialize_resource_plan,
)

WORKFLOW_UUID = "10000000-0000-4000-8000-000000000001"
TASK_UUID = "20000000-0000-4000-8000-000000000001"
SOURCE_NODE_UUID = "30000000-0000-4000-8000-000000000001"
FIRST_NODE_UUID = "30000000-0000-4000-8000-000000000002"
SECOND_NODE_UUID = "30000000-0000-4000-8000-000000000003"
OUTPUT_NODE_UUID = "30000000-0000-4000-8000-000000000004"
SOURCE_JOB_UUID = "40000000-0000-4000-8000-000000000001"
FIRST_JOB_UUID = "40000000-0000-4000-8000-000000000002"
SECOND_JOB_UUID = "40000000-0000-4000-8000-000000000003"
OUTPUT_JOB_UUID = "40000000-0000-4000-8000-000000000004"
MATERIAL_UUID = "50000000-0000-4000-8000-000000000001"
DEVICE_UUID = "60000000-0000-4000-8000-000000000001"


def _action_contract_schema(*, material_role: bool = False) -> dict[str, Any]:
    """构造工作流规格编译测试使用的完整动作合同（Action Contract）。

    参数：无。返回：允许任意 Goal 参数的动作 Schema envelope；本夹具不声明
    额外物料锁标记，短期物料需求仍由物料来源链产生。异常：无。
    """

    contract = {
        "type": "object",
        "properties": {
            "goal": {"type": "object", "additionalProperties": True},
        },
        "required": ["goal"],
    }
    if material_role:
        contract["properties"]["goal"] = {
            "type": "object",
            "properties": {
                "plate": {
                    "type": "object",
                    "properties": {"uuid": {"type": "string"}},
                    "required": ["uuid"],
                    "x-unilabos-material-lock": True,
                }
            },
            "required": ["plate"],
        }
        contract["x-unilabos-action-contract"] = {
            "version": 2,
            "resource_contract": {
                "version": 2,
                "resource_params": [{"param": "plate", "role": "material"}],
            },
        }
    return contract


def _compiler_contract() -> tuple[type[Any], type[BaseException]]:
    """取得待实现编译器及其稳定错误类型。

    参数：无。返回：工作流规格编译器（WorkflowSpecCompiler）类型与编译错误
    类型。异常：生产模块尚未实现时保留 ``ModuleNotFoundError``，使每个行为
    测试独立呈现 RED，而不是在测试收集阶段中止。
    """

    module = import_module("unilabos.workflow.workflow_spec_compiler")
    return module.WorkflowSpecCompiler, module.WorkflowSpecCompilationError


def _task_snapshot(
    *,
    mode: str = "existing",
    material_uuid: str | None = MATERIAL_UUID,
    disable_first_consumer: bool = False,
    implicit_passthrough: bool = False,
    custody_policy: str | None = "task_exclusive",
    material_role: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """构造冻结任务快照与工作流节点作业（WorkflowNodeJob）列表。

    参数：``mode`` 是物料来源模式（MaterialSourceMode）；``material_uuid`` 是
    任务提交前已固定的具体物料身份；``disable_first_consumer`` 控制物料链首个
    物理消费者是否禁用。返回：纯字典任务快照与按节点稳定绑定的作业列表。
    异常：无；非法组合由待测编译边界失败关闭。
    """

    # ``source_selector`` 是冻结的物料来源（MaterialSource）选择器，不允许
    # 编译器在运行时查询库存（Inventory）或补写具体物料身份。
    source_selector = {
        "mode": mode,
        "resource_template_uuid": "70000000-0000-4000-8000-000000000001",
        "mount": {"uuid": "70000000-0000-4000-8000-000000000002"},
        "material_uuid": material_uuid,
        "site": None,
        "slot_range": None,
        "flow_role": "primary_sample",
        "custody_policy": "task_exclusive",
    }
    if custody_policy is not None:
        source_selector["custody_policy"] = custody_policy
    # ``snapshot_nodes`` 是已应用工作流图的不可变节点顺序；物料来源节点只提供
    # 任务物料绑定（TaskMaterialBinding），两个 ILab 节点才是可派发动作。
    snapshot_nodes = [
        {
            "uuid": SOURCE_NODE_UUID,
            "workflow_node_template_uuid": "71000000-0000-4000-8000-000000000001",
            "name": "固定孔板来源",
            "type": "material_source",
            "param": source_selector,
            "disabled": False,
        },
        {
            "uuid": FIRST_NODE_UUID,
            "workflow_node_template_uuid": "71000000-0000-4000-8000-000000000002",
            "material_uuid": DEVICE_UUID,
            "name": "加入预混液",
            "type": "ILab",
            "action_name": "distribute",
            "action_type": "UniLabJsonCommand",
            "param": (
                {"plate": {"uuid": material_uuid}} if material_uuid is not None else {}
            ),
            "disabled": disable_first_consumer,
            "meta_data": {
                "unilab": {
                    "executor_binding": {"mode": "fixed", "device_id": DEVICE_UUID}
                }
            },
        },
        {
            "uuid": SECOND_NODE_UUID,
            "workflow_node_template_uuid": "71000000-0000-4000-8000-000000000003",
            "material_uuid": DEVICE_UUID,
            "name": "读取孔板",
            "type": "ILab",
            "action_name": "read_plate",
            "action_type": "UniLabJsonCommand",
            "param": (
                {"plate": {"uuid": material_uuid}} if material_uuid is not None else {}
            ),
            "disabled": False,
            "meta_data": {
                "unilab": {
                    "executor_binding": {"mode": "fixed", "device_id": DEVICE_UUID}
                }
            },
        },
    ]
    # ``snapshot_edges`` 冻结同一物料占位符（ResourceSlot）的线性消费顺序。
    snapshot_edges = [
        {
            "uuid": "72000000-0000-4000-8000-000000000001",
            "source_node_uuid": SOURCE_NODE_UUID,
            "source_handle_uuid": "73000000-0000-4000-8000-000000000001",
            "target_node_uuid": FIRST_NODE_UUID,
            "target_handle_uuid": "73000000-0000-4000-8000-000000000002",
        },
        {
            "uuid": "72000000-0000-4000-8000-000000000002",
            "source_node_uuid": (
                SOURCE_NODE_UUID if implicit_passthrough else FIRST_NODE_UUID
            ),
            "source_handle_uuid": (
                "73000000-0000-4000-8000-000000000001"
                if implicit_passthrough
                else "73000000-0000-4000-8000-000000000003"
            ),
            "target_node_uuid": SECOND_NODE_UUID,
            "target_handle_uuid": "73000000-0000-4000-8000-000000000004",
        },
    ]
    # ``handle_templates`` 冻结每条边是否传递物料占位符，而不让编译器从参数
    # 名称猜测物料语义。
    handle_templates = [
        {
            "uuid": "73000000-0000-4000-8000-000000000001",
            "workflow_node_template_uuid": "71000000-0000-4000-8000-000000000001",
            "handle_key": "material",
            "io_type": "source",
            "type": "ResourceSlot",
        },
        {
            "uuid": "73000000-0000-4000-8000-000000000002",
            "workflow_node_template_uuid": "71000000-0000-4000-8000-000000000002",
            "handle_key": "plate",
            "io_type": "target",
            "type": "ResourceSlot",
        },
        {
            "uuid": "73000000-0000-4000-8000-000000000003",
            "workflow_node_template_uuid": "71000000-0000-4000-8000-000000000002",
            "handle_key": "plate",
            "io_type": "source",
            "type": "ResourceSlot",
        },
        {
            "uuid": "73000000-0000-4000-8000-000000000004",
            "workflow_node_template_uuid": "71000000-0000-4000-8000-000000000003",
            "handle_key": "plate",
            "io_type": "target",
            "type": "ResourceSlot",
        },
    ]
    # ``first_action_contract``/``second_action_contract`` 分别模拟真实模板投影中
    # Goal 子模式和保留完整合同的双层表示。
    first_action_contract = _action_contract_schema(material_role=material_role)
    second_action_contract = _action_contract_schema(material_role=material_role)
    graph = {
        "nodes": snapshot_nodes,
        "edges": snapshot_edges,
        "node_templates": [
            {
                "uuid": "71000000-0000-4000-8000-000000000001",
                "node_type": "material_source",
            },
            {
                "uuid": "71000000-0000-4000-8000-000000000002",
                "node_type": "ILab",
                "schema": first_action_contract["properties"]["goal"],
                "meta_data": {
                    "unilab": {
                        "contract_kind": "typed",
                        "action_contract_schema": first_action_contract,
                    }
                },
            },
            {
                "uuid": "71000000-0000-4000-8000-000000000003",
                "node_type": "ILab",
                "schema": second_action_contract["properties"]["goal"],
                "meta_data": {
                    "unilab": {
                        "contract_kind": "typed",
                        "action_contract_schema": second_action_contract,
                    }
                },
            },
        ],
        "handle_templates": handle_templates,
    }
    # ``execution_plan`` 是从冻结应用图一次性产生的唯一运行静态输入；测试不再
    # 允许编译器回退读取 ``workflow_snapshot``。
    execution_plan, jobs = ExecutionPlanBuilder().build(
        graph,
        run_mode="normal",
        target_node_uuid=None,
    )
    job_identities = {
        SOURCE_NODE_UUID: SOURCE_JOB_UUID,
        FIRST_NODE_UUID: FIRST_JOB_UUID,
        SECOND_NODE_UUID: SECOND_JOB_UUID,
    }
    for job in jobs:
        # ``job`` 是构建器首次作业；替换随机 UUID 只为断言持久身份保持不变。
        job["uuid"] = job_identities[job["workflow_node_uuid"]]
    task_snapshot = {
        "uuid": TASK_UUID,
        "workflow_uuid": WORKFLOW_UUID,
        "workflow_snapshot": graph,
        "execution_plan": execution_plan,
    }
    return task_snapshot, jobs


def test_fixed_existing_source_compiles_only_physical_consumers() -> None:
    """固定既有物料来源只把普通动作交给本地调度器。

    参数：无。返回：无；断言物料来源解析作业
    （MaterialSourceResolutionJob）不派发，且两个普通动作都不重复承担任务物料
    准入（TaskMaterialAdmission）。异常：编译失败即测试失败。
    """

    compiler_type, _error_type = _compiler_contract()
    task_snapshot, jobs = _task_snapshot()

    spec = compiler_type().compile(task_snapshot, jobs)

    assert [node.id for node in spec.nodes] == [FIRST_NODE_UUID, SECOND_NODE_UUID]
    assert SOURCE_NODE_UUID not in {node.id for node in spec.nodes}
    assert spec.resource_coordinator_node_ids == [SOURCE_NODE_UUID]
    assert spec.material_requirements_by_node() == {}


def test_compiler_maps_template_passthrough_handles_to_runtime_handles() -> None:
    """发布合同中的模板句柄必须映射到执行计划物化的运行时句柄。

    复合动作的 ``material_passthrough_handles`` 保存在工作流模板元数据中，
    而执行计划会为每个节点生成新的 Handle UUID。若编译边界直接透传模板 UUID，
    运行态 DAG 无法找到对应端口，后继作业会被错误地标成参数待定。
    """

    compiler_type, _error_type = _compiler_contract()
    task_snapshot, jobs = _task_snapshot()
    node = next(
        item
        for item in task_snapshot["execution_plan"]["nodes"]
        if item["uuid"] == FIRST_NODE_UUID
    )
    node["meta_data"] = {
        "unilab": {
            "material_passthrough_handles": {
                "73000000-0000-4000-8000-000000000003":
                "73000000-0000-4000-8000-000000000002"
            }
        }
    }
    runtime_handles = {
        item["template_handle_uuid"]: item["uuid"]
        for item in task_snapshot["execution_plan"]["handles"]
        if item["node_uuid"] == FIRST_NODE_UUID
    }

    spec = compiler_type().compile(task_snapshot, jobs)
    compiled = next(item for item in spec.nodes if item.id == FIRST_NODE_UUID)

    assert compiled.material_passthrough_handles == {
        runtime_handles["73000000-0000-4000-8000-000000000003"]:
        runtime_handles["73000000-0000-4000-8000-000000000002"]
    }


def test_material_resource_role_binds_fixed_resource_slot_to_material_mutex() -> None:
    """物料角色参数应直接冻结为该 Material UUID 的动作互斥资源。

    参数：无。返回：无；断言合法的 v2 ``material`` 角色不要求调用方再提供重复
    ``resource_bindings``，且最终资源计划只用真实物料锁键表示该参数。异常：构建
    失败或物料别名仍未绑定时由测试失败。
    """

    action_contract = {
        "type": "object",
        "properties": {
            "goal": {
                "type": "object",
                "properties": {
                    "plate": {
                        "type": "object",
                        "properties": {"uuid": {"type": "string"}},
                        "required": ["uuid"],
                        "x-unilabos-material-lock": True,
                    }
                },
                "required": ["plate"],
            }
        },
        "required": ["goal"],
        "x-unilabos-action-contract": {
            "version": 2,
            "resource_contract": {
                "version": 2,
                "resource_params": [{"param": "plate", "role": "material"}],
            },
        },
    }
    execution_plan, _jobs = ExecutionPlanBuilder().build(
        {
            "nodes": [
                {
                    "uuid": FIRST_NODE_UUID,
                    "workflow_node_template_uuid": (
                        "71000000-0000-4000-8000-000000000002"
                    ),
                    "material_uuid": DEVICE_UUID,
                    "name": "读取孔板",
                    "type": "ILab",
                    "action_name": "read_plate",
                    "action_type": "UniLabJsonCommand",
                    "param": {"plate": {"uuid": MATERIAL_UUID}},
                    "meta_data": {
                        "unilab": {
                            "executor_binding": {
                                "mode": "fixed",
                                "device_id": DEVICE_UUID,
                            }
                        }
                    },
                }
            ],
            "edges": [],
            "node_templates": [
                {
                    "uuid": "71000000-0000-4000-8000-000000000002",
                    "node_type": "ILab",
                    "schema": action_contract["properties"]["goal"],
                    "meta_data": {
                        "unilab": {
                            "contract_kind": "typed",
                            "action_contract_schema": action_contract,
                        }
                    },
                }
            ],
            "handle_templates": [],
        },
        run_mode="normal",
        target_node_uuid=None,
    )

    resources = execution_plan["resource_plan"]["resources"]
    material_resources = [
        resource
        for resource in resources
        if resource["canonical_key"] == f"material/{MATERIAL_UUID}/exclusive"
    ]
    assert len(material_resources) == 1
    assert material_resources[0]["alias"] == f"node:{FIRST_NODE_UUID}:param:plate"


@pytest.mark.parametrize("custody_policy", ["shared_source", "task_exclusive"])
def test_automatic_material_source_binds_material_role_after_admission(
    custody_policy: str,
) -> None:
    """自动来源可在准入后把 material 角色绑定为真实物料互斥锁。"""

    task, jobs = _task_snapshot(
        material_uuid=None,
        implicit_passthrough=True,
        custody_policy=custody_policy,
        material_role=True,
    )
    pending = deserialize_resource_plan(task["execution_plan"]["resource_plan"])
    assert task["execution_plan"]["inventory_resource_binding"] == "pending"
    assert pending.binding_state == "template"

    compiler_type, error_type = _compiler_contract()
    compiler = compiler_type()
    with pytest.raises(error_type) as strict:
        compiler.compile(task, jobs)
    assert strict.value.code == "resource_plan_unbound"
    # 库存来源由协调器准入，普通动作没有单独声明数量或实例需求。
    assert compiler.validate_inventory_admission(task, jobs) is False
    with pytest.raises(error_type):
        compiler.validate_inventory_admission(task, [])
    unmarked = {**task, "execution_plan": dict(task["execution_plan"])}
    del unmarked["execution_plan"]["inventory_resource_binding"]
    with pytest.raises(error_type) as missing:
        compiler.validate_inventory_admission(unmarked, jobs)
    assert missing.value.code == "resource_plan_unbound"

    source_job = next(
        job for job in jobs if job["workflow_node_uuid"] == SOURCE_NODE_UUID
    )
    source_job["return_info"] = {"material": {"uuid": MATERIAL_UUID}}
    bound = bind_inventory_resource_plan(task, jobs, inventory=None)

    resource_plan = deserialize_resource_plan(bound["resource_plan"])
    assert bound["inventory_resource_binding"] == "bound"
    assert resource_plan.binding_state == "bound"
    compiler.compile({**task, "execution_plan": bound}, jobs)
    material_resources = [
        resource
        for resource in resource_plan.resources
        if resource.canonical_key == f"material/{MATERIAL_UUID}/exclusive"
    ]
    assert len(material_resources) == 1
    assert material_resources[0].alias == f"node:{FIRST_NODE_UUID}:param:plate"


def test_task_creation_keeps_template_plan_pending_until_inventory_admission(tmp_path) -> None:
    from unilabos.app.scheduler.service import EdgeScheduler
    from unilabos.workflow.store import WorkflowStore
    from unilabos.workflow.task_input import PreparedTaskInput
    from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge

    task, jobs = _task_snapshot(material_uuid=None, implicit_passthrough=True, material_role=True)
    graph = task["workflow_snapshot"]
    plan = task["execution_plan"]
    store = WorkflowStore(tmp_path / "workflow.db", persist_workflow_definitions=False)
    try:
        created = store.create_task_with_jobs(
            workflow_uuid=WORKFLOW_UUID, task_uuid=TASK_UUID, run_mode="normal",
            target_node_uuid=None, description=None, meta_data={}, applied_graph=graph,
            plan_builder=lambda snapshot: PreparedTaskInput(
                workflow_snapshot=snapshot, resolved_input={}, execution_plan=plan, jobs=jobs,
            ),
            validate_scheduler_compilation=True,
        )
        assert created["execution_plan"]["inventory_resource_binding"] == "pending"
        assert len(store.list_jobs(TASK_UUID)) == len(jobs)

        scheduler = EdgeScheduler(serial_queue=True)
        bridge = TaskSchedulerBridge(store, scheduler=scheduler)
        try:
            scheduler.begin_drain()
            assert bridge.submit(created)["task"]["status"] == "pending"
        finally:
            bridge.close()
            scheduler.close()

        scheduler = EdgeScheduler(serial_queue=True)
        bridge = TaskSchedulerBridge(store, scheduler=scheduler)
        try:
            scheduler.begin_drain()
            recovered, = bridge.recover_active_tasks()
            assert recovered["task"]["uuid"] == TASK_UUID
            assert recovered["task"]["execution_plan"]["inventory_resource_binding"] == "pending"
            assert scheduler.workflow_snapshot(TASK_UUID) is None
        finally:
            bridge.close()
            scheduler.close()
    finally:
        store.close()


def test_compiler_does_not_classify_completed_physical_node_as_coordinator() -> None:
    """已成功的物理节点不能因状态终结而被误归类成省略协调器。"""

    compiler_type, _error_type = _compiler_contract()
    task_snapshot, jobs = _task_snapshot()
    jobs[0]["status"] = "succeeded"
    jobs[1]["status"] = "succeeded"

    spec = compiler_type().compile(task_snapshot, jobs)

    assert spec.resource_coordinator_node_ids == [SOURCE_NODE_UUID]


def test_compiler_projects_pending_workflow_output_as_resource_coordinator() -> None:
    """尚待物理动作产出数据的 workflow_output 仍是非物理资源边界成员。"""

    compiler_type, _error_type = _compiler_contract()
    task_snapshot, jobs = _task_snapshot()
    execution_plan = task_snapshot["execution_plan"]
    execution_plan["nodes"].append(
        {
            "uuid": OUTPUT_NODE_UUID,
            "topological_index": 3,
            "kind": "workflow_output",
            "param": {},
        }
    )
    execution_plan["edges"].append(
        {
            "uuid": "72000000-0000-4000-8000-000000000003",
            "source_node_uuid": SECOND_NODE_UUID,
            "target_node_uuid": OUTPUT_NODE_UUID,
        }
    )
    jobs.append(
        {
            "uuid": OUTPUT_JOB_UUID,
            "workflow_node_uuid": OUTPUT_NODE_UUID,
            "executor_kind": "workflow_output",
            "status": "pending",
            "param": {},
        }
    )
    resource_plan = serialize_resource_plan(
        bind_station_resource_plan(
            compile_template_resource_plan(
                {
                    "nodes": [
                        {"uuid": SECOND_NODE_UUID},
                        {"uuid": OUTPUT_NODE_UUID},
                    ],
                    "edges": [
                        {
                            "source_node_uuid": SECOND_NODE_UUID,
                            "target_node_uuid": OUTPUT_NODE_UUID,
                        }
                    ],
                    "resource_scopes": [
                        {
                            "scope_id": "action-output",
                            "kind": "with",
                            "resources": ["common"],
                            "node_uuids": [SECOND_NODE_UUID, OUTPUT_NODE_UUID],
                        }
                    ],
                }
            ),
            {"common": {"canonical_key": "/devices/common", "kind": "device"}},
        )
    )
    execution_plan["resource_plan"] = resource_plan
    execution_plan["capabilities"] = list(resource_plan["capabilities"])
    for node in execution_plan["nodes"]:
        node_uuid = node["uuid"]
        node["resource_plan_id"] = resource_plan["plan_id"]
        node["resource_interval_ids"] = [
            interval["interval_id"]
            for interval in resource_plan["intervals"]
            if node_uuid in interval["node_uuids"]
        ]
        node["resource_acquire_set_id"] = next(
            (
                acquire_set["acquire_set_id"]
                for acquire_set in resource_plan["acquire_sets"]
                if acquire_set["node_uuid"] == node_uuid
            ),
            "",
        )

    spec = compiler_type().compile(task_snapshot, jobs)

    assert spec.resource_coordinator_node_ids == [OUTPUT_NODE_UUID]


def test_material_requirement_is_not_duplicated_on_downstream_consumer() -> None:
    """同一线性物料链的普通消费者不得重复申请任务物料预留。

    参数：无。返回：无；断言短期整图物料预留输入只由物料来源解析作业
    （MaterialSourceResolutionJob）承担，两个普通动作都不重复；该兼容输入将在
    K11 被正式任务物料预留（TaskMaterialReservation）替换。异常：编译失败即
    测试失败。
    """

    compiler_type, _error_type = _compiler_contract()
    task_snapshot, jobs = _task_snapshot()

    spec = compiler_type().compile(task_snapshot, jobs)

    requirements_by_node = spec.material_requirements_by_node()
    assert requirements_by_node == {}
    assert spec.nodes[1].material_requirements == []


def test_disabled_consumer_moves_requirement_to_first_enabled_node() -> None:
    """禁用消费者不派发，物料需求应归属首个启用物理消费者。

    参数：无。返回：无；断言禁用节点及其作业身份都不会进入调度规格，后续
    启用动作也不获得新的物料需求。异常：编译失败即测试失败。
    """

    compiler_type, _error_type = _compiler_contract()
    task_snapshot, jobs = _task_snapshot(disable_first_consumer=True)

    spec = compiler_type().compile(task_snapshot, jobs)

    assert [node.id for node in spec.nodes] == [SECOND_NODE_UUID]
    assert spec.nodes[0].job_id == SECOND_JOB_UUID
    assert spec.nodes[0].material_requirements == []


def test_compiler_preserves_task_node_and_job_stable_identities() -> None:
    """编译必须保留任务、节点与作业的稳定 UUID，禁止创建第二套身份。

    参数：无。返回：无；断言工作流规格（WorkflowSpec）的任务身份以及每个
    工作流节点（WorkflowNode）/作业身份与持久快照完全一致。异常：编译失败即
    测试失败。
    """

    compiler_type, _error_type = _compiler_contract()
    task_snapshot, jobs = _task_snapshot()

    spec = compiler_type().compile(task_snapshot, jobs)

    assert spec.workflow_id == TASK_UUID
    assert spec.task_id == TASK_UUID
    assert [(node.id, node.job_id) for node in spec.nodes] == [
        (FIRST_NODE_UUID, FIRST_JOB_UUID),
        (SECOND_NODE_UUID, SECOND_JOB_UUID),
    ]


def test_create_new_material_source_fails_closed_before_scheduling() -> None:
    """短期桥不支持新建物料来源时必须在调度前失败关闭。

    参数：无。返回：无；断言 ``create_new`` 不会绕过库存权威（Inventory
    Authority）或生成可派发节点。异常：预期稳定编译错误码。
    """

    with pytest.raises(ExecutionPlanBuildError) as caught:
        _task_snapshot(mode="create_new", material_uuid=None)

    assert caught.value.code == "unsupported_material_source_mode"


def test_legacy_material_source_defaults_to_task_exclusive_in_execution_plan() -> None:
    """升级前物料来源必须在冻结执行计划中恢复原独占语义。

    参数：无。返回：无；断言缺少 ``custody_policy`` 的既有工作流图不会被解释
    为共享来源，而是显式写入 ``task_exclusive``。异常：计划构建失败或默认值
    漂移均由测试失败暴露。
    """

    task_snapshot, _jobs = _task_snapshot(custody_policy=None)

    source = task_snapshot["execution_plan"]["nodes"][0]
    assert source["param"]["custody_policy"] == "task_exclusive"


def test_automatic_existing_source_freezes_selector_without_inventory_lookup() -> None:
    """自动既有来源只冻结选择条件和运行时写入目标。

    参数：无。返回：无；断言执行计划（ExecutionPlan）不查询库存权威
    （Inventory Authority）或伪造具体物料（Material）身份，但保留挂载点、资源
    模板和首个物理消费者参数目标，供边缘调度器（EdgeScheduler）准入时解析。
    异常：计划构建或规格编译失败即测试失败。
    """

    task_snapshot, jobs = _task_snapshot(material_uuid=None)

    source = task_snapshot["execution_plan"]["nodes"][0]
    assert source["material_requirements"] == [
        {
            "template_id": "70000000-0000-4000-8000-000000000001",
            "mount_uuid": "70000000-0000-4000-8000-000000000002",
            "site_uuid": "",
            "slot_uuids": [],
        }
    ]
    assert source["material_binding_targets"] == [
        {"workflow_node_uuid": FIRST_NODE_UUID, "param_key": "plate"}
    ]
    assert "plate" not in task_snapshot["execution_plan"]["nodes"][1]["param"]
    assert "plate" not in jobs[1]["param"]


def test_automatic_source_binds_every_ordered_implicit_passthrough_consumer() -> None:
    """自动来源必须冻结所有隐式透传消费者的运行绑定目标。

    参数：无。返回无；断言复合工作流（CompositeWorkflow）展开后同一
    物料占位符（ResourceSlot）直接连接多个严格有序动作时，每个动作都能从
    调度边缘（EdgeScheduler）的单次预分配取得同一个物料（Material）身份。
    """

    task_snapshot, jobs = _task_snapshot(
        material_uuid=None,
        implicit_passthrough=True,
    )

    source = task_snapshot["execution_plan"]["nodes"][0]
    assert source["material_binding_targets"] == [
        {"workflow_node_uuid": FIRST_NODE_UUID, "param_key": "plate"},
        {"workflow_node_uuid": SECOND_NODE_UUID, "param_key": "plate"},
    ]
    assert "plate" not in jobs[1]["param"]
    assert "plate" not in jobs[2]["param"]


@pytest.mark.parametrize("frozen", [True, False])
def test_runtime_input_does_not_restore_frozen_site_selector(frozen: bool) -> None:
    """启动库位冻结后，规格编译不能重新注入显式库位；普通输入仍需绑定。"""
    compiler, _ = _compiler_contract()
    task, jobs = _task_snapshot()
    task["input"] = {"return_site": "R1C2", "sample_id": "sample-test"}
    node = next(n for n in task["execution_plan"]["nodes"] if n["uuid"] == FIRST_NODE_UUID)
    node["inputs"] = [
        {"handle_uuid": "site-input", "data_key": "target_site"},
        {"handle_uuid": "sample-input", "data_key": "sample_id"},
    ]
    node["input_bindings"] = {
        "site-input": {"parameter": "return_site"},
        "sample-input": {"parameter": "sample_id"},
    }
    node["site_selectors"] = [{"parameter": "target_site", "handle_uuid": "site-input"}]
    if frozen:
        node["execution_policy"] = {
            "target_site_group": [MATERIAL_UUID],
            "target_site_selection": {"site_uuids": [MATERIAL_UUID], "requested_reference": "R1C2"},
        }
    compiled = next(n for n in compiler().compile(task, jobs).nodes if n.id == FIRST_NODE_UUID)
    assert compiled.param.get("sample_id") == "sample-test"
    if frozen:
        assert "target_site" not in compiled.param
        assert compiled.execution_policy["target_site_group"] == [MATERIAL_UUID]
    else:
        assert compiled.param["target_site"] == "R1C2"
