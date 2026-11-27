"""人工确认在 Workflow 持久层的终态与重启策略。"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.scheduler_core.conftest import build_core_runtime, stable_uuid
from unilabos.app.scheduler.dispatch import CommittedJobOutcome
from unilabos.workflow.execution_plan import ExecutionPlanBuilder
from unilabos.workflow.manual_confirmation import (
    normalize_manual_confirmation_config,
)
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import StoreConflict
from unilabos.workflow.task_runtime_projection import TaskRuntimeProjection


def test_manual_wrapper_overrides_underlying_device_template_executor_kind() -> None:
    """设备动作模板被标成 manual_confirm 后必须冻结为人工确认作业。"""

    workflow_uuid = stable_uuid("workflow:manual-wrapper")
    node_uuid = stable_uuid("node:manual-wrapper")
    template_uuid = stable_uuid("template:manual-wrapper")
    device_uuid = stable_uuid("device:manual-wrapper")
    action_contract = {
        "type": "object",
        "properties": {
            "goal": {"type": "object", "additionalProperties": True}
        },
        "required": ["goal"],
    }
    plan, jobs = ExecutionPlanBuilder().build(
        {
            "workflow": {"uuid": workflow_uuid, "revision": 1},
            "nodes": [
                {
                    "uuid": node_uuid,
                    "workflow_node_template_uuid": template_uuid,
                    "type": "manual_confirm",
                    "action_name": "heat",
                    "action_type": "UniLabJsonCommand",
                    "param": {"temperature": 42},
                    "manual_confirmation": {"timeout_seconds": 15},
                    "execution_policy": {},
                    "meta_data": {
                        "unilab": {
                            "executor_binding": {
                                "mode": "fixed",
                                "device_id": device_uuid,
                            }
                        }
                    },
                }
            ],
            "edges": [],
            "node_templates": [
                {
                    "uuid": template_uuid,
                    "node_type": "device_action",
                    "type": "UniLabJsonCommand",
                    "meta_data": {
                        "unilab": {"action_contract_schema": action_contract}
                    },
                }
            ],
            "handle_templates": [],
        },
        run_mode="normal",
        target_node_uuid=None,
    )

    assert plan["nodes"][0]["kind"] == "manual_confirm"
    assert plan["nodes"][0]["manual_confirmation"] == {"timeout_seconds": 15}
    assert jobs[0]["executor_kind"] == "manual_confirm"


def test_manual_wrapper_accepts_real_ilab_template_type() -> None:
    """真实注册表把设备动作标为 ``ILab``，仍应允许包装人工确认。"""

    from unilabos.workflow.definition_edit import create_node

    node = create_node(
        payload={
            "workflow_node_template_uuid": stable_uuid("template:ilab-wrapper"),
            "type": "manual_confirm",
            "name": "人工复核加热",
            "manual_confirmation": {"timeout_seconds": 30},
            "meta_data": {
                "unilab": {
                    "executor_binding": {
                        "mode": "fixed",
                        "device_id": stable_uuid("device:ilab-wrapper"),
                    }
                }
            },
        },
        template={
            "uuid": stable_uuid("template:ilab-wrapper"),
            "node_type": "ILab",
            "name": "heat",
            "type": "UniLabJsonCommand",
        },
    )

    assert node["type"] == "manual_confirm"
    assert node["action_name"] == "heat"


def test_approved_confirmation_remains_approved_after_device_outcome(
    tmp_path: Path,
) -> None:
    """设备成功/失败不改写已经批准的人工决定。"""

    runtime = build_core_runtime(tmp_path / "approved")
    try:
        _pending, job_uuid = runtime.submit_manual(task_name="approved-outcome")
        service = WorkflowService(
            runtime.workflow_store,
            task_scheduler_bridge=runtime.bridge,
        )
        service.decide_manual_confirmation(job_uuid, action="approve")

        runtime.scheduler.on_job_outcome(
            job_uuid,
            CommittedJobOutcome(
                outcome="succeeded",
                return_info={"completed": True},
                error_info=[],
                unknown_command_ids=[],
            ),
        )

        job = service.get_workflow_node_job(job_uuid)
        assert job["status"] == "succeeded"
        assert job["manual_confirmation"]["status"] == "approved"
        replay = service.decide_manual_confirmation(job_uuid, action="approve")
        replay_job = next(item for item in replay["jobs"] if item["uuid"] == job_uuid)
        assert replay_job["status"] == "succeeded"
    finally:
        runtime.close()


def test_manual_confirmation_timeout_config_defaults_and_is_strict() -> None:
    """确认超时使用独立配置，默认一小时，并拒绝越界或扩展字段。"""

    assert normalize_manual_confirmation_config(None) == {"timeout_seconds": 3600}
    assert normalize_manual_confirmation_config({"timeout_seconds": 86400}) == {
        "timeout_seconds": 86400
    }
    for invalid in (
        {"timeout_seconds": 0},
        {"timeout_seconds": 86401},
        {"timeout_seconds": 1.5},
        {"timeout_seconds": True},
        {"timeout_seconds": 60, "unknown": True},
    ):
        with pytest.raises(StoreConflict):
            normalize_manual_confirmation_config(invalid)


def test_runtime_restart_fails_manual_wait_but_releases_no_send_resources(
    tmp_path: Path,
) -> None:
    """人工等待不恢复；Job 失败、确认关闭，未下发资源可证明安全释放。"""

    runtime = build_core_runtime(tmp_path / "restart")
    try:
        _pending, job_uuid = runtime.submit_manual(task_name="restart-manual")
        task_uuid = stable_uuid("task:restart-manual")

        aggregate = TaskRuntimeProjection(
            runtime.workflow_store
        ).project_execution_process_restarted(task_uuid)

        assert aggregate is not None
        assert aggregate["task"]["status"] == "failed"
        assert aggregate["task"]["cleanup_status"] == "pending"
        job = next(item for item in aggregate["jobs"] if item["uuid"] == job_uuid)
        assert job["status"] == "canceled"
        assert not job.get("uncertainty_reason")
        confirmation = WorkflowService(
            runtime.workflow_store
        ).get_manual_confirmation(job_uuid)
        assert confirmation["status"] == "canceled"
        assert confirmation["resolution_reason"] == "runtime_restarted"
        assert {
            lock["state"]
            for lock in TaskRuntimeProjection(
                runtime.workflow_store
            ).list_execution_locks(job_uuid)
        } == {"released"}
    finally:
        runtime.close()
