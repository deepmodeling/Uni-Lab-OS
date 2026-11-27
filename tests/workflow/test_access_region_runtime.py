"""PLC 访问区域不得进入工站 Scheduler 软件占用集合的合同。"""

from __future__ import annotations

from typing import Any

from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.models import WorkflowEdge, WorkflowNode, WorkflowSpec
from unilabos.app.scheduler.service import EdgeScheduler

TASK_UUID = "81000000-0000-4000-8000-000000000001"
SOURCE_NODE_UUID = "82000000-0000-4000-8000-000000000001"
RELEASE_NODE_UUID = "82000000-0000-4000-8000-000000000002"
SOURCE_JOB_UUID = "83000000-0000-4000-8000-000000000001"
RELEASE_JOB_UUID = "83000000-0000-4000-8000-000000000002"
MATERIAL_UUID = "84000000-0000-4000-8000-000000000001"


def _node(
    *,
    node_uuid: str,
    job_uuid: str,
    action_name: str,
    executor_kind: str,
    execution_policy: dict[str, Any] | None = None,
) -> WorkflowNode:
    """构造带稳定作业身份和冻结策略的可派发节点。

    参数：节点/作业身份、动作名、执行责任和可选策略决定测试计划。返回：不依赖
    注册表回读的工作流节点。异常：无；固定值全部由调用方控制。
    """

    return WorkflowNode(
        id=node_uuid,
        job_id=job_uuid,
        device_id="reactor-a",
        action_name=action_name,
        action_type="UniLabJsonCommand",
        param={},
        param_schema={
            "type": "object",
            "properties": {
                "goal": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": True,
                }
            },
            "required": ["goal"],
        },
        executor_kind=executor_kind,
        execution_policy=dict(execution_policy or {}),
    )


def test_scheduler_rejects_legacy_access_region_without_compatibility_path() -> None:
    """直接装配的旧访问区域策略必须关闭式拒绝。

    参数：无。返回无。异常：旧字段被静默忽略、生成软件锁或越过设备派发边界时
    由断言暴露。新项目不提供升级兼容路径，正式发布边界会更早拒绝该字段。
    """

    policy = {
        "access_region": {
            "key": "s08-s09-reagent-corridor",
            "lock_material_uuid": MATERIAL_UUID,
            "release_node_uuid": RELEASE_NODE_UUID,
        }
    }
    spec = WorkflowSpec(
        workflow_id=TASK_UUID,
        task_id=TASK_UUID,
        nodes=[
            _node(
                node_uuid=SOURCE_NODE_UUID,
                job_uuid=SOURCE_JOB_UUID,
                action_name="inspect",
                executor_kind="device_action",
                execution_policy=policy,
            ),
            _node(
                node_uuid=RELEASE_NODE_UUID,
                job_uuid=RELEASE_JOB_UUID,
                action_name="transfer_resource",
                executor_kind="material_transfer",
            ),
        ],
        edges=[
            WorkflowEdge(
                uuid="85000000-0000-4000-8000-000000000001",
                source_node_id=SOURCE_NODE_UUID,
                target_node_id=RELEASE_NODE_UUID,
            )
        ],
    )
    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    dispatching: list[dict[str, Any]] = []
    scheduler.bind_dispatch_admission_authority(
        lambda payload: dispatching.append(payload) or True
    )

    submitted = scheduler.submit_workflow(spec)

    assert submitted["dispatched"] == []
    assert dispatching == []
    assert scheduler.snapshot()["workflows"][TASK_UUID]["state"] == "failed"
