"""受管设备进程：规格 CRUD、命令拼装、启动 / 停止 / 崩溃看护（Popen 用桩替代）。"""

from __future__ import annotations

import io
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.api_envelope import error_of, ok_data
from unilabos.server.api.device_processes import create_device_processes_router
from unilabos.server.services import device_processes as dpx
from unilabos.server.services import driver_packages as dp


class FakePopen:
    """可控的子进程桩：poll() 返回 exit_code，None 表示仍在运行。"""

    instances: list["FakePopen"] = []

    def __init__(self, command: list[str], **_: Any) -> None:
        self.command = command
        self.pid = 4000 + len(FakePopen.instances)
        self.exit_code: int | None = None
        self.stdout = io.BytesIO(b"slave booting\n\x1b[37m[INFO]\x1b[0m HostLink connected\n")
        self.terminated = False
        FakePopen.instances.append(self)

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True
        self.exit_code = 0 if self.exit_code is None else self.exit_code

    def kill(self) -> None:
        self.exit_code = -9

    def wait(self, timeout: float | None = None) -> int:
        return self.exit_code if self.exit_code is not None else 0


@pytest.fixture()
def service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dpx.DeviceProcessService:
    FakePopen.instances.clear()
    monkeypatch.setattr(dpx.subprocess, "Popen", FakePopen)
    monkeypatch.setattr(dpx, "RESTART_BACKOFF_S", (0.05, 0.05))
    pkg_dir = tmp_path / "acme_devices"
    pkg_dir.mkdir()
    dp.save_ledger(
        tmp_path,
        {"acme_devices": dp.DriverPackageRecord(name="acme-devices", package_dirs=[str(pkg_dir)], device_ids=["acme_pump"])},
    )
    svc = dpx.DeviceProcessService(tmp_path)
    monkeypatch.setattr(dpx, "_service", svc)
    # device-classes 端点会查驱动包台账，让它也指向同一个 working_dir
    monkeypatch.setattr(dp, "_service", dp.DriverPackageService(tmp_path))
    return svc


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "name": "泵站",
        "graph_nodes": [dpx.build_device_node("pump_1", "acme_pump", config={"port": "COM3"})],
        "package_names": ["acme-devices"],
        "external_only": True,
        "restart_policy": "on-failure",
        "max_restarts": 2,
    }
    payload.update(overrides)
    return payload


def test_create_writes_graph_and_command(service: dpx.DeviceProcessService, tmp_path: Path) -> None:
    view = service.create(_payload())
    graph = Path(view["graph_path"])
    assert graph.is_file()
    assert view["device_ids"] == ["pump_1"]
    spec = dpx.load_specs(tmp_path)[view["id"]]
    command = service.build_command(spec)
    assert command[1:3] == ["-m", "unilabos"]
    assert "--is_slave" in command and "--external_devices_only" in command
    assert command[command.index("--devices") + 1] == str(tmp_path / "acme_devices")
    assert command[command.index("-g") + 1] == str(graph)
    assert command[command.index("--host_node_ip") + 1] == "127.0.0.1"
    # 一个数据库根目录只归一个进程：受管 Slave 不与 Host / 权威共用 <working_dir>/database
    assert command[command.index("--server_database_root") + 1] == str(
        dpx.process_dir(tmp_path, view["id"]) / "database"
    )

    with pytest.raises(dpx.DeviceProcessError):
        service.create(_payload(package_names=["ghost"]))
    with pytest.raises(dpx.DeviceProcessError):
        service.create(_payload(restart_policy="sometimes"))


def test_device_node_reuses_authority_uuid(service: dpx.DeviceProcessService, monkeypatch: pytest.MonkeyPatch) -> None:
    """同 id 设备再次登记时沿用权威 uuid，否则 Graph Authority 会以 identity_conflict 拒绝启动图。"""
    from unilabos.server.backend import composition

    class _Record:
        class material:  # noqa: N801 - 模拟 MaterialAggregateRead.material
            material_uuid = "11111111-2222-3333-4444-555555555555"

    class _Materials:
        def get_material_by_resource_id(self, resource_id: str) -> _Record:
            if resource_id != "pump_1":
                raise LookupError(resource_id)
            return _Record()

    monkeypatch.setattr(composition, "get_materials_service", lambda: _Materials())
    assert dpx.build_device_node("pump_1", "acme_pump")["uuid"] == _Record.material.material_uuid
    fresh = dpx.build_device_node("pump_2", "acme_pump")["uuid"]
    assert fresh != _Record.material.material_uuid and len(fresh) == 36
    assert "children" not in dpx.build_device_node("pump_2", "acme_pump")

    view = service.create(_payload(graph_nodes=[dpx.build_device_node("pump_1", "acme_pump")]))
    assert view["graph_nodes"][0]["uuid"] == _Record.material.material_uuid
    assert Path(view["graph_path"]).name.startswith("managed_")


def test_start_stop_and_crash_restart(service: dpx.DeviceProcessService) -> None:
    view = service.create(_payload())
    process_id = view["id"]

    started = service.start(process_id)
    assert started["status"] == "running"
    assert started["pid"] == FakePopen.instances[0].pid
    with pytest.raises(dpx.DeviceProcessError):
        service.start(process_id)  # 已在运行

    time.sleep(0.1)
    from unilabos.server.services.runtime.logs import RuntimeLogService

    logs = RuntimeLogService(service, None, machine_name="host").read(f"managed:{process_id}")
    assert "[INFO] HostLink connected" in [line.text for line in logs.lines]  # ANSI 着色已剥掉

    # 模拟崩溃：看护线程应在退避后拉起第二个进程
    FakePopen.instances[0].exit_code = 1
    deadline = time.time() + 5
    while time.time() < deadline and len(FakePopen.instances) < 2:
        time.sleep(0.05)
    assert len(FakePopen.instances) == 2
    time.sleep(0.05)
    view = service.get(process_id)
    assert view["status"] == "running"
    assert view["restart_count"] == 1
    assert view["last_exit_code"] == 1

    # 手动停止：不应再拉起
    stopped = service.stop(process_id)
    assert stopped["status"] == "stopped"
    assert FakePopen.instances[-1].terminated is True
    time.sleep(0.3)
    assert len(FakePopen.instances) == 2

    # 超过 max_restarts 后放弃看护
    service.start(process_id)
    for _ in range(3):
        current = FakePopen.instances[-1]
        current.exit_code = 2
        deadline = time.time() + 2
        while time.time() < deadline and FakePopen.instances[-1] is current:
            time.sleep(0.05)
    time.sleep(0.3)
    final = service.get(process_id)
    assert final["status"] == "crashed"
    assert final["restart_count"] == 2
    assert "停止看护" in (final["last_error"] or "")


def test_router_roundtrip(service: dpx.DeviceProcessService) -> None:
    app = FastAPI()
    app.include_router(create_device_processes_router())
    client = TestClient(app)

    listing = ok_data(client.get("/api/v1/device-processes"))
    assert listing["hostlink"]["host"] == "127.0.0.1"
    assert listing["processes"] == []

    classes = ok_data(client.get("/api/v1/device-processes/device-classes"))
    assert any(item["id"] == "acme_pump" and item["package"] == "acme-devices" for item in classes)

    created = client.post(
        "/api/v1/device-processes",
        json={
            "name": "泵站",
            "devices": [{"id": "pump_1", "class": "acme_pump", "config": {"port": "COM3"}}],
            "package_names": ["acme-devices"],
            "external_only": True,
        },
    )
    created = ok_data(created, status=201)
    process_id = created["id"]
    assert created["graph_nodes"][0]["template_name"] == "acme_pump"

    assert ok_data(client.post(f"/api/v1/device-processes/{process_id}/start"))["status"] == "running"
    error_of(client.post(f"/api/v1/device-processes/{process_id}/start"), 3003)
    # 日志路由不存在：框架默认 404（没有匹配的合同，不是业务信封）
    assert client.get(f"/api/v1/device-processes/{process_id}/logs?tail=10").status_code == 404
    assert ok_data(client.post(f"/api/v1/device-processes/{process_id}/stop"))["status"] == "stopped"
    assert ok_data(client.delete(f"/api/v1/device-processes/{process_id}")) is None
    error_of(client.get(f"/api/v1/device-processes/{process_id}"), 4001)
