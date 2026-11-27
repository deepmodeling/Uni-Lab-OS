"""调度核心的确定性时间合同。"""

from __future__ import annotations

from tests.scheduler_core.conftest import ManualClock
from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.models import WorkflowNode, WorkflowSpec
from unilabos.app.scheduler.service import EdgeScheduler


def test_timeline_uses_injected_clock_without_sleep() -> None:
    """作业耗时只由注入时钟决定，不依赖墙钟或真实等待。"""

    clock = ManualClock()
    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher, clock=clock.time)
    submitted = scheduler.submit_workflow(
        WorkflowSpec(
            workflow_id="clock-task",
            nodes=[
                WorkflowNode(
                    id="clock-job",
                    device_id="clock-device",
                    action_name="run",
                    action_type="UniLabJsonCommand",
                )
            ],
        )
    )

    clock.advance(12.5)
    scheduler.on_job_finished(submitted["dispatched"][0]["job_id"], True)

    completed = scheduler.timeline()["completed"]
    assert completed == [
        {
            "job_id": submitted["dispatched"][0]["job_id"],
            "workflow_id": "clock-task",
            "node_id": "clock-job",
            "device_id": "clock-device",
            "action_name": "run",
            "device_action_key": "/devices/clock-device/run",
            "started_at": 1_800_000_000.0,
            "ended_at": 1_800_000_012.5,
            "actual_s": 12.5,
            "estimated_s": 60.0,
            "estimate_source": "default",
            "state": "success",
            "suc_type": "normal",
        }
    ]
