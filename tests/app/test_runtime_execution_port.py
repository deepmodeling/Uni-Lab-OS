"""执行端不能用数据库旁路；后端接口鉴权和本地命令日志共同保护执行。"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.app.test_observation_services import world
from unilabos.app.scheduler.runtime_execution_api import create_runtime_execution_router
from unilabos.runtime.execution import JournaledExecution, RuntimeMaterials


def test_execution_interface_authenticates_before_touching_authority():
    calls = []

    class Port:
        def call(self, operation, payload):
            calls.append((operation, payload))
            return {"attempt": 2}

    app = FastAPI()
    app.include_router(
        create_runtime_execution_router(lambda: Port(), lambda: "workspace-key")
    )
    with TestClient(app) as client:
        assert client.post("/runtime/execution/authorize", json={}).status_code == 401
        assert not calls
        response = client.post(
            "/runtime/execution/authorize",
            json={"request": {}},
            headers={"Authorization": "Bearer workspace-key"},
        )
        assert response.json()["data"] == {"attempt": 2}


def test_command_result_replays_after_restart_without_physical_repeat(tmp_path):
    path = tmp_path / "commands.db"
    request, grant = {"identity": {"node_job_uuid": "job"}}, {"attempt": 1}
    calls = []
    run = JournaledExecution(path, namespace="device")
    expected = {"success": True, "inventory_receipt": {"receipt_id": "receipt"}}
    assert run.run(request, grant, lambda: (calls.append(1), expected)[1]) == expected
    restarted = JournaledExecution(path, namespace="device")
    assert restarted.run(request, grant, lambda: calls.append(2)) == expected
    assert calls == [1]


def test_uncertain_command_blocks_reexecution_across_restart(tmp_path):
    path = tmp_path / "commands.db"

    def fail():
        raise TimeoutError("engine ACK missing")

    request, grant = {"identity": {"node_job_uuid": "job"}}, {"attempt": 1}
    with pytest.raises(TimeoutError):
        JournaledExecution(path, namespace="device").run(request, grant, fail)
    with pytest.raises(RuntimeError, match="reconciliation_required"):
        JournaledExecution(path, namespace="device").run(
            request, grant, lambda: pytest.fail("重复物理动作")
        )


def test_execution_service_refuses_legacy_database_configuration(tmp_path):
    from unilabos.simulation.session import SimulationSession

    with pytest.raises(ValueError, match="数据库路径"):
        SimulationSession(
            {"inventory_db": str(tmp_path / "inventory.db")}, lambda *a: None
        )
    assert not list(tmp_path.iterdir())


def test_unknown_blocks_model_construction_before_world_can_reset(tmp_path):
    from unilabos.simulation.session import SimulationSession

    path = tmp_path / "commands.db"
    run = JournaledExecution(path, namespace="simulation-execution")
    with pytest.raises(TimeoutError):
        run.run(
            {"identity": {"node_job_uuid": "job"}},
            {"attempt": 1},
            lambda: (_ for _ in ()).throw(TimeoutError()),
        )
    with pytest.raises(RuntimeError, match="reconciliation_required"):
        SimulationSession(
            {"command_journal": path, "journal": tmp_path / "new-run.jsonl"},
            lambda *a: pytest.fail("不能在对账前重建世界"),
            execution_port=object(),
        )
    assert not (tmp_path / "new-run.jsonl").exists()


def test_command_journal_requires_a_stable_explicit_path():
    from unilabos.simulation.session import SimulationSession

    with pytest.raises(ValueError, match="command_journal"):
        SimulationSession({}, lambda *a: None, execution_port=object())


def test_same_job_changed_request_is_not_reexecuted(tmp_path):
    run = JournaledExecution(tmp_path / "commands.db", namespace="device")
    request = {"identity": {"node_job_uuid": "job"}, "value": 1}
    run.run(request, {"attempt": 1}, lambda: {"success": True})
    from unilabos.runtime.commands import CommandConflictError

    with pytest.raises(CommandConflictError):
        run.run(
            {**request, "value": 2}, {"attempt": 1}, lambda: pytest.fail("重复执行")
        )


def test_real_http_client_uses_backend_inventory(world):
    import socket
    import threading
    import time

    import uvicorn

    from unilabos.app.scheduler.runtime_execution_api import BackendExecutionPort
    from unilabos.runtime.execution import BackendExecutionClient

    runtime = world[0]
    # 与后端共用同一个库存，不向客户端提供路径或连接。
    from types import SimpleNamespace
    port = BackendExecutionPort(None, SimpleNamespace(store=runtime.inventory))
    app = FastAPI()
    app.include_router(create_runtime_execution_router(lambda: port, lambda: 'key'))
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    url = f'http://127.0.0.1:{sock.getsockname()[1]}'
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', lifespan='off'))
    thread = threading.Thread(target=server.run, kwargs={'sockets': [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started:
            assert time.monotonic() < deadline
            time.sleep(.01)
        materials = RuntimeMaterials(BackendExecutionClient(url, 'key'))
        assert materials.by_id['axis']['uuid'] == world[4]['uuid']
        assert materials.relations([world[4]['uuid']]) == {world[4]['uuid']: None}
        from urllib.error import HTTPError
        with pytest.raises(HTTPError) as error:
            RuntimeMaterials(BackendExecutionClient(url, 'wrong'))
        assert error.value.code == 401
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
