"""整站持久执行等待图测试。"""

from __future__ import annotations

from tests.workflow.test_f05_task_scheduler_bridge import (
    JOB_UUID,
    TASK_UUID,
    _seed_task,
)
from tests.workflow.test_local_execution_lock_lease import (
    SECOND_JOB_UUID,
    SECOND_TASK_UUID,
    _seed_second_task,
)
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.task_runtime_projection import TaskRuntimeProjection


def test_wait_graph_reports_cross_task_cycle(tmp_path) -> None:
    """两个 Task 互相等待时，整站快照返回完整边和稳定循环成员。"""

    store = WorkflowStore(tmp_path / "wait-graph.db")
    try:
        _seed_task(store, with_material=False)
        _seed_second_task(store, device_id="reactor-b")
        projection = TaskRuntimeProjection(store)
        first_lock = [{"lock_key": "/devices/reactor-a", "scope": "device"}]
        second_lock = [{"lock_key": "/devices/reactor-b", "scope": "device"}]
        projection.project_execution_lock_wait(
            task_uuid=TASK_UUID,
            job_uuid=JOB_UUID,
            execution_locks=first_lock,
            blocking_task_uuid=SECOND_TASK_UUID,
            blocking_job_uuid=SECOND_JOB_UUID,
        )
        projection.project_execution_lock_wait(
            task_uuid=SECOND_TASK_UUID,
            job_uuid=SECOND_JOB_UUID,
            execution_locks=second_lock,
            blocking_task_uuid=TASK_UUID,
            blocking_job_uuid=JOB_UUID,
        )

        graph = projection.get_execution_wait_graph()

        assert graph["deadlock_detected"] is True
        assert graph["cycles"] == [[JOB_UUID, SECOND_JOB_UUID]]
        assert {node["job_uuid"] for node in graph["nodes"]} == {
            JOB_UUID,
            SECOND_JOB_UUID,
        }
        assert all(node["deadlock_cycle"] for node in graph["nodes"])
    finally:
        store.close()
