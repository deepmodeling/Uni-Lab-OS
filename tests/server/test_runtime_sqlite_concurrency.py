"""执行回调、控制面与 HTTP 共享 runtime.db 时的并发读回归。"""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest

from unilabos.protocol.runtime import BackendSessionUpsert, CommandEnvelope, ExecutionJobCreate
from unilabos.server.services.runtime import RuntimeService


@pytest.fixture
def runtime(tmp_path):
    with RuntimeService(tmp_path / "runtime.db") as service:
        service.upsert_backend_session(
            BackendSessionUpsert(
                session_uuid="session", edge_uuid="edge", backend_uri="http://backend",
                authority_epoch="authority", connection_epoch="connection", state="active",
            )
        )
        service.receive_command(
            CommandEnvelope(
                command_uuid="execute", session_uuid="session", backend_sequence=1,
                command_type="execute_job", job_uuid="job", payload_sha256="hash",
            )
        )
        service.create_execution_job(
            ExecutionJobCreate(
                job_uuid="job", task_uuid="task", node_uuid="node", attempt_group_uuid="attempt",
                execute_command_uuid="execute", device_uuid="device", action_name="probe",
                action_payload_uuid="payload", scheduler_revision=1,
            )
        )
        yield service


def test_execution_callback_reads_wait_for_transaction_commit(runtime):
    """其他线程不能读到写事务里的暂态，也不能与 SQLite 惰性取数交错。"""
    started = Event()
    finished = Event()

    def read_job():
        started.set()
        try:
            return runtime.get_execution_job("job")
        finally:
            finished.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with runtime.write():
            runtime.connection.execute(
                "UPDATE execution_job SET error_summary='not committed' WHERE job_uuid='job'"
            )
            future = pool.submit(read_job)
            assert started.wait(2)
            observed_uncommitted = finished.wait(0.15)
            runtime.connection.execute(
                "UPDATE execution_job SET error_summary='committed' WHERE job_uuid='job'"
            )
        record = future.result(timeout=3)
    assert not observed_uncommitted, "执行结果回调绕过了数据库事务锁"
    assert record.error_summary == "committed"


def test_parallel_execution_job_queries_never_lose_or_corrupt_rows(runtime):
    """模拟极快动作终态回调与调度查询同时复用同一 SELECT 的语句缓存。"""
    workers = 8
    start = Barrier(workers)

    def read_repeatedly():
        start.wait(timeout=5)
        for _ in range(400):
            record = runtime.get_execution_job("job")
            assert (record.job_uuid, record.device_uuid, record.action_name) == ("job", "device", "probe")
            assert record.status == "accepted"
            assert len(runtime.list_execution_jobs(device_uuid="device")) == 1

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(read_repeatedly) for _ in range(workers)]
        for future in futures:
            future.result(timeout=20)
