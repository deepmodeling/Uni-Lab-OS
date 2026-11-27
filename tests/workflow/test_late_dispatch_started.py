"""完成回执先于 WebSocket 开始通知时，不得把终态倒退或断开会话。"""
import pytest

from tests.workflow.test_f05_task_scheduler_bridge import JOB_UUID, TASK_UUID, _seed_task
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.task_runtime_projection import TaskRuntimeProjection


@pytest.mark.parametrize('terminal', ['success', 'failed'])
def test_late_started_after_outcome_is_idempotent(tmp_path, terminal):
    store = WorkflowStore(tmp_path / 'workflow.db')
    try:
        _seed_task(store, with_material=False)
        projection = TaskRuntimeProjection(store)
        projection.project_pre_dispatch(task_uuid=TASK_UUID, job_uuid=JOB_UUID, execution_locks=[{'lock_key':'/devices/test-robot','scope':'device'}])
        projection.project_job_finished(job_uuid=JOB_UUID, scheduler_state=terminal)
        before = store.get_job(JOB_UUID)
        projection.project_dispatch_accepted(JOB_UUID)
        after = store.get_job(JOB_UUID)
        assert after == before
    finally:
        store.close()
