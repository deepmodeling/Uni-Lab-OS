"""同一任务共享资源区间跨 Claim 复用 Fence 的执行镜像回归。"""

import uuid

import pytest

from unilabos.app.edge_control.store import EdgeControlStore


def test_scope_jobs_share_fence_but_other_task_and_stale_epoch_are_rejected(tmp_path):
    """同一 Task 共享区间可跨 Claim 和重启复用，别的 Task 不能复用或倒退凭证。"""
    path = str(tmp_path / "scope.db")
    claim = str(uuid.uuid4())
    task = str(uuid.uuid4())

    def payload(owner=claim, token=7, task_uuid=task):
        return {
            "job_uuid": str(uuid.uuid4()),
            "node_uuid": str(uuid.uuid4()),
            "task_uuid": task_uuid,
            "claim_uuid": owner,
            "attempt": 1,
            "job_access_token": "test-only",
            "fences": [{"lock_key": "/devices/robot", "fencing_token": token}],
        }

    store = EdgeControlStore(path)
    assert store.save_job_start(payload(), str(uuid.uuid4()))
    store.close()
    store = EdgeControlStore(path)
    second = payload(str(uuid.uuid4()))
    command = str(uuid.uuid4())
    assert store.save_job_start(second, command)
    assert not store.save_job_start(second, command)
    for rejected in (payload(str(uuid.uuid4()), task_uuid=str(uuid.uuid4())), payload(token=6)):
        with pytest.raises(ValueError, match="stale resource Fence"):
            store.save_job_start(rejected, str(uuid.uuid4()))
        assert store.get_job(rejected["job_uuid"]) is None
    assert store.save_job_start(payload(str(uuid.uuid4()), 8), str(uuid.uuid4()))
    with pytest.raises(ValueError, match="stale resource Fence"):
        store.save_job_start(payload(), str(uuid.uuid4()))
    store.close()
