"""后端控制命令只能经 ws_client 进入执行微后端。"""

from __future__ import annotations

import asyncio
from queue import Queue

from unilabos.app import ws_client as ws_module
from unilabos.app.ws_client import DeviceActionManager, MessageProcessor


class _Microbackend:
    def __init__(self) -> None:
        self.dispatched: list[dict] = []
        self.canceled_jobs: list[str] = []
        self.canceled_tasks: list[str] = []
        self.decisions: list[tuple[str, str, dict]] = []

    def dispatch(self, payload: dict) -> None:
        self.dispatched.append(payload)

    def cancel_job(self, job_id: str) -> bool:
        self.canceled_jobs.append(job_id)
        return True

    def cancel_task(self, task_id: str) -> list[str]:
        self.canceled_tasks.append(task_id)
        return ["job-by-task"]

    def handle_action_error_decision(
        self,
        decision_id: str,
        job_id: str,
        decision: dict,
    ) -> bool:
        self.decisions.append((decision_id, job_id, decision))
        return True


def test_ws_commands_are_delegated_to_the_single_microbackend(monkeypatch) -> None:
    backend = _Microbackend()
    monkeypatch.setattr(
        ws_module,
        "_get_job_execution_backend",
        lambda: backend,
    )
    processor = MessageProcessor("", Queue(), DeviceActionManager())
    monkeypatch.setattr(
        processor,
        "_check_action_always_free",
        lambda _device_id, _action: False,
    )

    async def exercise() -> None:
        await processor._handle_job_start(
            {
                "job_id": "job-1",
                "task_id": "task-1",
                "node_id": "node-1",
                "device_id": "pump-1",
                "action": "transfer",
                "action_type": "TransferLiquid",
                "action_args": {"volume": 5},
                "sample_material": {},
                "retry_count": 0,
            }
        )
        await processor._handle_cancel_action({"job_id": "job-1"})
        await processor._handle_cancel_action({"task_id": "task-2"})
        await processor._handle_job_error_decision(
            {
                "decision_id": "decision-1",
                "job_id": "job-1",
                "device_id": "pump-1",
                "action": "retry",
                "scheduler_updated": True,
            }
        )

    asyncio.run(exercise())

    assert backend.dispatched == [
        {
            "job_id": "job-1",
            "task_id": "task-1",
            "node_id": "node-1",
            "device_id": "pump-1",
            "action": "transfer",
            "action_type": "TransferLiquid",
            "action_args": {"volume": 5},
            "sample_material": {},
            "server_info": {},
            "notebook_id": "",
            "retry_count": 0,
            "always_free": False,
        }
    ]
    assert backend.canceled_jobs == ["job-1"]
    assert backend.canceled_tasks == ["task-2"]
    assert backend.decisions == [
        (
            "decision-1",
            "job-1",
            {
                "decision_id": "decision-1",
                "job_id": "job-1",
                "device_id": "pump-1",
                "action": "retry",
                "scheduler_updated": True,
            },
        )
    ]
