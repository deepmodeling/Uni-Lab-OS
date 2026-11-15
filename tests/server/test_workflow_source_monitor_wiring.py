"""本机 Workflow Authority 拥有已注册源码：启动时重试待写回的 Apply，运行期监视外部编辑。"""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path
from typing import Any

from unilabos.config.config import BasicConfig
from unilabos.registry.workflows import DeviceCatalog
from unilabos.server.backend.composition import (
    get_workflow_service,
    reset_for_test,
    setup_execution_backend,
    setup_local_scheduler,
)
from unilabos.server.database import ServerDatabasePaths
from unilabos.server.services.runtime.workflow.authoring_compiler import WorkflowAuthoringCompiler
from unilabos.server.services.runtime.workflow.service import WorkflowError
from unilabos.server.services.runtime.workflow.source_monitor import WorkflowSourceMonitor

PUMP_UUID = "11111111-0000-4000-8000-000000000001"
SOURCE = '''from unilabos.registry.workflows import workflow


@workflow("加样流程", tags=["demo"])
def flow(ctx, *, amount: float = 2.0):
    ctx.run("pump-1/dose", {"volume": amount}, name="加样")
'''
MONITOR_THREAD = "workflow-source-monitor"


def _hash(content: str) -> str:
    return "sha256:" + hashlib.sha256(content.encode()).hexdigest()


def _compiler() -> WorkflowAuthoringCompiler:
    catalog = DeviceCatalog()
    catalog.add("pump-1", "pump", PUMP_UUID)
    return WorkflowAuthoringCompiler(catalog_provider=lambda: catalog, registry_fingerprint=lambda: "r1")


def _start_authority(paths: ServerDatabasePaths) -> Any:
    setup_local_scheduler(backend=setup_execution_backend(database_paths=paths))
    service = get_workflow_service()
    # 测试目录里没有真实设备：换上带 pump-1 的编译目录，候选才能签发
    service.compiler = _compiler()
    return service


def _monitor_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == MONITOR_THREAD and thread.is_alive()]


def _wait_for(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    assert predicate()


def test_authority_retries_pending_writeback_on_restart_and_watches_external_edits(tmp_path, monkeypatch) -> None:
    paths = ServerDatabasePaths.resolve(tmp_path)
    monkeypatch.setattr(BasicConfig, "backend", "hostlink")
    monkeypatch.setattr(BasicConfig, "machine_name", "test-host")
    monkeypatch.setattr(BasicConfig, "server_database_paths", paths)
    try:
        service = _start_authority(paths)
        assert len(_monitor_threads()) == 1
        workflow = service.create_workflow(name="源码写回", tags=[], description=None, meta_data={})
        saved = service.save_draft(workflow["uuid"], python_source=SOURCE, expected_draft_hash=None,
                                   expected_workflow_revision=workflow["revision"])
        registration = service._registration(workflow["uuid"])
        target = Path(registration["package_root"]) / registration["relative_path"]
        assert target.read_text() == SOURCE

        # Apply 已提交，但这一次（以及进程退出前的每次重试）写回源码都失败
        def failing_write(*_args, **_kwargs):
            raise OSError(5, "I/O error")

        monkeypatch.setattr(service, "_atomic_write", failing_write)
        applied = service.apply_authoring(
            workflow["uuid"], expected_draft_hash=saved["draft"]["draft_hash"],
            expected_workflow_revision=workflow["revision"],
            expected_candidate_hash=saved["candidate"]["candidate_hash"],
        )
        assert [item["code"] for item in applied["apply_result"]["warnings"]] == ["draft_writeback_pending"]
        assert service.get_authoring_record(workflow["uuid"])["writeback_status"] == "pending"
        target.unlink()  # 写回失败后源码也丢了：只剩 OS 保留的应用记录
        time.sleep(1.5)  # 运行中的监视线程会重试，但写入仍然失败
        assert service.get_authoring_record(workflow["uuid"])["writeback_status"] == "pending"
    finally:
        reset_for_test()
    assert _monitor_threads() == []
    assert not target.exists()

    try:
        # 重启：Authority 启动时同步恢复，先把应用记录写回源码文件再对外服务
        service = _start_authority(paths)
        record = service.get_authoring_record(workflow["uuid"])
        assert record["writeback_status"] == "settled"
        assert target.read_text() == SOURCE

        # 外部编辑器改了源码：监视线程重新编译出对应的新候选
        edited = SOURCE.replace("amount: float = 2.0", "amount: float = 3.0")
        target.write_text(edited)
        _wait_for(lambda: (service.get_authoring_record(workflow["uuid"]).get("candidate") or {})
                  .get("draft_hash") == _hash(edited))
        assert service.get_authoring_record(workflow["uuid"])["observed_draft_hash"] == _hash(edited)
    finally:
        reset_for_test()
    assert _monitor_threads() == []


def test_running_monitor_settles_a_pending_writeback(tmp_path, monkeypatch) -> None:
    paths = ServerDatabasePaths.resolve(tmp_path)
    monkeypatch.setattr(BasicConfig, "backend", "hostlink")
    monkeypatch.setattr(BasicConfig, "machine_name", "test-host")
    monkeypatch.setattr(BasicConfig, "server_database_paths", paths)
    try:
        service = _start_authority(paths)
        workflow = service.create_workflow(name="运行期重试", tags=[], description=None, meta_data={})
        saved = service.save_draft(workflow["uuid"], python_source=SOURCE, expected_draft_hash=None,
                                   expected_workflow_revision=workflow["revision"])
        registration = service._registration(workflow["uuid"])
        target = Path(registration["package_root"]) / registration["relative_path"]
        real_write = service._atomic_write
        failures = {"left": 1}

        def flaky_write(*args, **kwargs):
            if failures["left"]:
                failures["left"] -= 1
                raise OSError(5, "I/O error")
            return real_write(*args, **kwargs)

        monkeypatch.setattr(service, "_atomic_write", flaky_write)
        applied = service.apply_authoring(
            workflow["uuid"], expected_draft_hash=saved["draft"]["draft_hash"],
            expected_workflow_revision=workflow["revision"],
            expected_candidate_hash=saved["candidate"]["candidate_hash"],
        )
        assert [item["code"] for item in applied["apply_result"]["warnings"]] == ["draft_writeback_pending"]
        target.unlink()
        _wait_for(lambda: service.get_authoring_record(workflow["uuid"])["writeback_status"] == "settled")
        assert target.read_text() == SOURCE
    finally:
        reset_for_test()


def test_startup_recovery_skips_a_broken_source_and_recovers_the_rest() -> None:
    reconciled: list[str] = []

    class _Service:
        def list_registered_sources(self):
            return [{"workflow_uuid": "bad"}, {"workflow_uuid": "good"}]

        def reconcile_registered_source(self, workflow_uuid, **_kwargs):
            if workflow_uuid == "bad":
                raise WorkflowError("invalid_input")
            reconciled.append(workflow_uuid)

    from unilabos.server.services.runtime.workflow.service import WorkflowService

    errors: list[tuple[str, str]] = []
    WorkflowService.recover_registered_sources(_Service(), on_error=lambda key, exc: errors.append((key, exc.code)))
    assert reconciled == ["good"] and errors == [("bad", "invalid_input")]


def test_monitor_survives_an_unexpected_error_and_stops_promptly() -> None:
    calls = {"count": 0}

    class _Service:
        def list_registered_sources(self):
            calls["count"] += 1
            if calls["count"] == 1:
                raise ValueError("数据库暂时不可用")
            return []

    monitor = WorkflowSourceMonitor(_Service(), interval_seconds=0.01, settle_seconds=0.0)  # type: ignore[arg-type]
    monitor.start()
    try:
        _wait_for(lambda: calls["count"] >= 3, timeout=5.0)
    finally:
        started = time.monotonic()
        monitor.stop()
    assert time.monotonic() - started < 1.0
    assert _monitor_threads() == []
