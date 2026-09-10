"""F05.4-C14 物料来源执行责任的计划与作业合同。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from tests.workflow.test_f05_workflow_spec_compiler import (
    MATERIAL_UUID,
    SOURCE_JOB_UUID,
    SOURCE_NODE_UUID,
    _task_snapshot,
)
from unilabos.workflow.execution_plan import ExecutionPlanBuilder


def _source_plan() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """构造包含一个启用物料来源（MaterialSource）的完整执行计划。

    参数：无。返回：执行计划（ExecutionPlan）与首次工作流节点作业
    （WorkflowNodeJob）集合。异常：夹具图不合法时由计划构建器直接抛出。
    """

    # ``task_snapshot`` 只用于取得生产构建器生成的冻结执行计划。
    task_snapshot, jobs = _task_snapshot()
    return dict(task_snapshot["execution_plan"]), jobs


def _source_only_graph(*, disabled: bool = False) -> dict[str, Any]:
    """从共享夹具裁剪出只含物料来源（MaterialSource）的应用图。

    参数：``disabled`` 决定来源是否禁用。返回：保留来源模板和输出连接点的独立
    图。异常：无；裁剪结果由待测计划构建器校验。
    """

    task_snapshot, _jobs = _task_snapshot()
    graph = deepcopy(task_snapshot["workflow_snapshot"])
    # ``source`` 是唯一保留的非动作供料边界，修改副本不会污染共享夹具。
    source = next(
        node for node in graph["nodes"] if node["uuid"] == SOURCE_NODE_UUID
    )
    source["disabled"] = disabled
    source_template_uuid = source["workflow_node_template_uuid"]
    graph["nodes"] = [source]
    graph["edges"] = []
    graph["node_templates"] = [
        template
        for template in graph["node_templates"]
        if template["uuid"] == source_template_uuid
    ]
    graph["handle_templates"] = [
        handle
        for handle in graph["handle_templates"]
        if handle["workflow_node_template_uuid"] == source_template_uuid
    ]
    return graph


def test_enabled_source_is_a_planned_coordinator_responsibility() -> None:
    """每个启用来源必须形成一个协调器所有的计划节点。

    参数：无。返回：无；断言计划保留物料来源（MaterialSource）身份、种类和
    冻结选择器。异常：来源仍被当成可删除虚拟节点时断言失败。
    """

    plan, _jobs = _source_plan()
    # ``source_nodes`` 精确定位协调器所有的物料来源解析责任。
    source_nodes = [node for node in plan["nodes"] if node["kind"] == "material_source"]

    assert len(source_nodes) == 1
    assert source_nodes[0]["uuid"] == SOURCE_NODE_UUID
    assert source_nodes[0]["param"]["material_uuid"] == MATERIAL_UUID


def test_enabled_source_creates_one_material_source_resolution_job() -> None:
    """每个启用来源必须创建恰好一个物料来源解析作业。

    参数：无。返回：无；断言作业复用来源节点身份且明确使用
    ``executor_kind=material_source``。异常：缺失、重复或伪装普通动作均失败。
    """

    _plan, jobs = _source_plan()
    # ``source_jobs`` 只允许包含协调器执行责任，不能混入设备动作。
    source_jobs = [job for job in jobs if job["workflow_node_uuid"] == SOURCE_NODE_UUID]

    assert len(source_jobs) == 1
    assert source_jobs[0]["uuid"] == SOURCE_JOB_UUID
    assert source_jobs[0]["executor_kind"] == "material_source"


def test_enabled_source_keeps_runtime_handle_and_direct_material_edge() -> None:
    """执行计划必须保留物料来源的运行连接点与直连物料边。

    参数：无。返回：无；断言来源输出连接点、动作输入连接点及它们之间的直连
    物料占位符（ResourceSlot）边都使用运行身份。异常：来源被错误排除在计划图时
    由集合和字段断言失败。
    """

    plan, _jobs = _source_plan()
    # ``handles_by_node`` 证明协调责任虽不派发，仍是冻结运行图中的正式端点。
    handles_by_node = {
        node_uuid: [
            handle for handle in plan["handles"] if handle["node_uuid"] == node_uuid
        ]
        for node_uuid in (SOURCE_NODE_UUID,)
    }
    source_handles = handles_by_node[SOURCE_NODE_UUID]
    direct_edge = next(
        edge
        for edge in plan["edges"]
        if edge["source_node_uuid"] == SOURCE_NODE_UUID
    )

    assert len(source_handles) == 1
    assert source_handles[0]["io_type"] == "source"
    assert direct_edge["target_node_uuid"] != SOURCE_NODE_UUID
    assert direct_edge["source_handle_uuid"] == source_handles[0]["uuid"]
    assert direct_edge["source_type"] == "ResourceSlot"
    assert direct_edge["target_type"] == "ResourceSlot"
    assert direct_edge.get("dependency_only") is not True


def test_source_only_graph_still_creates_one_persistable_job() -> None:
    """只含来源的工作流也必须有可持久执行责任。

    参数：无。返回：无；断言来源图不会退化成零作业任务。异常：若计划构建器
    继续删除物料来源（MaterialSource），数量断言失败。
    """

    plan, jobs = ExecutionPlanBuilder().build(
        _source_only_graph(),
        run_mode="normal",
        target_node_uuid=None,
    )

    assert [node["uuid"] for node in plan["nodes"]] == [SOURCE_NODE_UUID]
    assert [job["workflow_node_uuid"] for job in jobs] == [SOURCE_NODE_UUID]


def test_short_term_reservation_requirement_belongs_to_source_job() -> None:
    """短期预留需求必须归属物料来源解析作业而非设备动作。

    参数：无。返回：无；断言固定既有物料只在来源计划节点保存一次实例需求，
    普通动作不再承担任务物料准入（TaskMaterialAdmission）。异常：需求仍挂在
    首消费动作或被重复时断言失败。
    """

    plan, _jobs = _source_plan()
    # ``requirements_by_node`` 显示短期遗留预留（inventory_reservation）的责任归属。
    requirements_by_node = {
        node["uuid"]: node.get("material_requirements", [])
        for node in plan["nodes"]
    }

    assert requirements_by_node[SOURCE_NODE_UUID] == [
        {"instance_uuid": MATERIAL_UUID}
    ]
    assert all(
        not requirements
        for node_uuid, requirements in requirements_by_node.items()
        if node_uuid != SOURCE_NODE_UUID
    )


def test_disabled_source_creates_no_plan_node_or_job() -> None:
    """禁用来源不能建立准入责任或短期预留。

    参数：无。返回：无；断言禁用物料来源（MaterialSource）保持现有零作业
    行为。异常：若禁用声明仍产生身份或预留入口则断言失败。
    """

    plan, jobs = ExecutionPlanBuilder().build(
        _source_only_graph(disabled=True),
        run_mode="normal",
        target_node_uuid=None,
    )

    assert plan["nodes"] == []
    assert jobs == []
