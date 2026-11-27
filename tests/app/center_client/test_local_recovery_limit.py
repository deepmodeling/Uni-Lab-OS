"""单调用的本地恢复必须有持久上限，不阻塞通信或重建真实 Task。"""
import time

from tests.app.center_client.test_runtime import Center, Service, runtime
from unilabos.workflow.service import WorkflowConflict


def test_unknown_creation_conflict_stops_after_three_attempts_without_blocking_poll(tmp_path, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    center, service = Center(), Service()
    command = center.add()
    center.add(canceled=True)
    attempts, catalog = [], []
    def reject(**kwargs):
        attempts.append(kwargs)
        raise WorkflowConflict("conflict", message="原始错误可能含敏感输入")
    service.submit_station_workflow = reject
    run = runtime(tmp_path, center, service)
    run._sync_catalog = lambda **kwargs: catalog.append(kwargs)
    try:
        run.poll_once()
        assert len(attempts) == 1 and len(catalog) == 1
        for _ in range(5):
            run.poll_once()
        assert len(attempts) == 1, "等待只作用于此调用，不能每次轮询都重复创建"
        clock[0] += 3
        run.poll_once()
        assert len(attempts) == 2
        clock[0] += 5
        run.poll_once()
        for _ in range(4):
            clock[0] += 60
            run.poll_once()
        assert len(attempts) == center.claims == 3
        assert len(center.reports) == 1
        assert center.reports[0]["event_type"] == "admission_failed"
        assert center.reports[0]["payload"]["output"]["error_info"][0]["code"] == "admission_retry_exhausted"
        assert "敏感输入" not in str(center.reports)
        assert run.store.commands() == []
    finally:
        run.close()


def test_recovery_count_survives_restart_and_duplicate_notification(tmp_path, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    center, service = Center(), Service()
    center.add()
    attempts = []
    def reject(**kwargs):
        attempts.append(kwargs)
        raise WorkflowConflict("conflict")
    service.submit_station_workflow = reject
    run = runtime(tmp_path, center, service)
    run.poll_once()
    run.close()
    run = runtime(tmp_path, center, service)
    try:
        center.add()
        run.poll_once()
        assert len(attempts) == 1
        for _ in range(5):
            clock[0] += 60
            run.poll_once()
        assert len(attempts) == 3 and center.claims == 3
        assert len(center.reports) == 1
    finally:
        run.close()


def test_existing_task_recovery_is_held_without_failing_or_recreating_task(tmp_path, monkeypatch):
    from tests.app.center_client.test_atomic_workflow_events import _submit, _workflow
    from tests.app.center_client.test_center_client import config
    from unilabos.app.center_client.client import CenterClient
    from unilabos.app.center_client.runtime import CenterRuntime
    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    with _workflow(tmp_path) as (store, service, durable, package, _):
        task = _submit(service, package)
        center = Center()
        center.package = {"payload": dict(package.raw), "payload_digest": package.payload_digest}
        envelope = center.add()
        durable.receive(envelope)
        durable.acknowledge_command(envelope["command"]["command_uuid"])
        cfg = config(tmp_path)
        run = CenterRuntime(cfg, CenterClient(cfg, center), durable, service)
        attempts = []
        def conflict(*args, **kwargs):
            attempts.append(True)
            raise WorkflowConflict("conflict")
        monkeypatch.setattr(service, "recover_center_workflow_invocation", conflict)
        for _ in range(6):
            clock[0] += 60
            run.poll_once()
        assert len(attempts) == 3
        assert store.get_task(task["uuid"])["status"] == task["status"]
        assert all(x["event_type"] != "admission_failed" for x in center.reports)
        state = durable.local_recovery_state(package.station_invocation_uuid)
        assert state["status"] == "needs_review" and state["attempts"] == 3
        assert store.db.execute("SELECT COUNT(*) FROM workflow_task").fetchone()[0] == 1


def test_local_recovery_limit_can_disable_automatic_retry(tmp_path):
    from dataclasses import replace
    center, service = Center(), Service()
    center.add()
    attempts = []
    def conflict(**kwargs):
        attempts.append(True)
        raise WorkflowConflict("conflict")
    service.submit_station_workflow = conflict
    run = runtime(tmp_path, center, service)
    run.config = replace(run.config, local_recovery_max_attempts=1)
    try:
        for _ in range(3):
            run.poll_once()
        assert len(attempts) == len(center.reports) == 1
    finally:
        run.close()


def test_temporary_local_conflict_can_recover_before_limit(tmp_path, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    center, service = Center(), Service()
    center.add()
    submit = service.submit_station_workflow
    attempts = []
    def once(**kwargs):
        attempts.append(True)
        if len(attempts) == 1:
            raise WorkflowConflict("conflict")
        return submit(**kwargs)
    service.submit_station_workflow = once
    run = runtime(tmp_path, center, service)
    try:
        run.poll_once()
        clock[0] += 3
        run.poll_once()
        assert len(attempts) == 2 and service.task is not None
        assert [x["event_type"] for x in center.reports] == ["accepted"]
        assert run.store.local_recovery_states() == []
    finally:
        run.close()


def test_network_error_does_not_consume_local_recovery_budget(tmp_path):
    import pytest

    from unilabos.app.center_client.transport import CenterTransportError
    center, service = Center(), Service()
    center.add()
    request = center.request
    def disconnected(method, path, **kwargs):
        if path.endswith("/claim"):
            raise CenterTransportError("模拟网络断开", retryable=True)
        return request(method, path, **kwargs)
    center.request = disconnected
    run = runtime(tmp_path, center, service)
    try:
        with pytest.raises(CenterTransportError):
            run.poll_once()
        assert run.store.local_recovery_states() == []
        assert service.calls == [] and center.reports == []
    finally:
        run.close()


def test_local_recovery_limit_configuration_is_positive_integer(tmp_path):
    from dataclasses import replace

    import pytest

    from tests.app.center_client.test_center_client import config
    from unilabos.app.center_client.config import CenterClientConfig, CenterConfigError
    cfg = config(tmp_path)
    assert cfg.local_recovery_max_attempts == 3
    for value in [0, -1, 101, True, 1.5]:
        with pytest.raises(CenterConfigError):
            replace(cfg, local_recovery_max_attempts=value)
    environment = {"CENTER_ENDPOINT": cfg.endpoint, "CENTER_CLIENT_CERT": str(cfg.client_cert),
                   "CENTER_CLIENT_KEY": str(cfg.client_key), "CENTER_SERVER_CA": str(cfg.server_ca),
                   "CENTER_MACHINE_SECRET": cfg.machine_secret, "CENTER_STATION_UUID": cfg.station_uuid,
                   "CENTER_INSTANCE_UUID": cfg.instance_uuid, "CENTER_LOCAL_RECOVERY_MAX_ATTEMPTS": "5"}
    assert CenterClientConfig.from_env(environment).local_recovery_max_attempts == 5
