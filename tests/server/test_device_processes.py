"""受管设备进程：规格 CRUD、命令拼装、启动 / 停止 / 崩溃看护（Popen 用桩替代）。"""

from __future__ import annotations

import io
import json
import sys
import threading
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
    from unilabos.server import execution_composition

    class _Record:
        class material:  # noqa: N801 - 模拟 MaterialAggregateRead.material
            material_uuid = "11111111-2222-3333-4444-555555555555"

    class _Materials:
        def get_material_by_resource_id(self, resource_id: str) -> _Record:
            if resource_id != "pump_1":
                raise LookupError(resource_id)
            return _Record()

    monkeypatch.setattr(execution_composition, "get_materials_service", lambda: _Materials())
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


def test_managed_slave_follows_its_host_and_shutdown_stops_all_at_once(
    service: dpx.DeviceProcessService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """受管 Slave 跟随 Host（Host 被强制结束时自行退出）；Host 退出时先一起请全部 Slave 退出再限时等待。"""

    import os

    from unilabos.app import lifetime

    events: list[str] = []
    envs: list[dict[str, str]] = []

    class RecordingPopen(FakePopen):
        def __init__(self, command: list[str], **kwargs: Any) -> None:
            super().__init__(command, **kwargs)
            envs.append(dict(kwargs["env"]))

        def terminate(self) -> None:
            events.append(f"terminate {self.pid}")
            super().terminate()

        def wait(self, timeout: float | None = None) -> int:
            events.append(f"wait {self.pid}")
            return super().wait(timeout)

    monkeypatch.setattr(dpx.subprocess, "Popen", RecordingPopen)
    first = service.create(_payload(name="泵站 1"))["id"]
    second = service.create(_payload(name="泵站 2", graph_nodes=[dpx.build_device_node("pump_2", "acme_pump")]))["id"]
    service.start(first)
    service.start(second)

    # 拉起线程可能是 API 工作线程 / 定时器线程：只写父进程 pid，不启用跟随拉起线程的 Linux 死亡信号
    assert [env[lifetime.PARENT_PID_ENV] for env in envs] == [str(os.getpid())] * 2
    assert all(lifetime.PARENT_DEATH_SIGNAL_ENV not in env for env in envs)

    service.shutdown()
    pids = [proc.pid for proc in RecordingPopen.instances[-2:]]
    assert events == [f"terminate {pids[0]}", f"terminate {pids[1]}", f"wait {pids[0]}", f"wait {pids[1]}"]


# ── --root_devices_as_slaves 生成的规格 ─────────────────────────────────────


def _root_device_spec(tmp_path: Path, **overrides: Any) -> dpx.DeviceProcessSpec:
    """Host 按 --root_devices_as_slaves 为一个根工作站生成的规格（节点取自 Graph Authority payload）。"""

    nodes = [
        {"id": "station", "uuid": "00000000-0000-4000-8000-000000000002", "parent": None, "type": "device",
         "template_name": "acme_station", "config": {"protocol_type": []}, "data": {}},
        {"id": "pump", "uuid": "00000000-0000-4000-8000-000000000003", "parent": "station", "type": "device",
         "template_name": "acme_pump", "config": {"port": "COM3"}, "data": {}},
        {"id": "vial", "uuid": "00000000-0000-4000-8000-000000000004", "parent": "station", "type": "container",
         "config": {"type": "RegularContainer"}, "data": {}},
    ]
    links = [{"id": "inner", "source": "pump", "target": "station", "type": "fluid",
              "sourceHandle": "out", "targetHandle": "in"}]
    spec = dpx.DeviceProcessSpec(
        id="root-station",
        name="station",
        graph_nodes=nodes,
        graph_links=links,
        devices_dirs=[str(tmp_path / "acme_devices")],
        external_only=True,
        extra_args=["--graph_is_authority_payload", "--working_dir", str(tmp_path), "--test_mode"],
        generated_by="root_devices_as_slaves",
    )
    for key, value in overrides.items():
        setattr(spec, key, value)
    return spec


def test_generated_slave_command_line(service: dpx.DeviceProcessService, tmp_path: Path, monkeypatch) -> None:
    from unilabos.app.cli.parser import build_parser
    from unilabos.config.config import BasicConfig, HostLinkConfig

    monkeypatch.setattr(BasicConfig, "backend", "ros2")
    monkeypatch.setattr(BasicConfig, "machine_name", "lab_host")
    monkeypatch.setattr(HostLinkConfig, "bind", "0.0.0.0")
    monkeypatch.setattr(HostLinkConfig, "port", 18511)
    spec = _root_device_spec(tmp_path)
    dpx.sync_generated_specs(tmp_path, "root_devices_as_slaves", [spec])

    command = service.build_command(dpx.load_specs(tmp_path)["root-station"])
    assert command[:3] == [sys.executable, "-m", "unilabos"]
    args = build_parser().parse_args(command[3:])
    # 与 Host 同一 backend，接回本机 HostLink；Slave 自己不再拆分
    assert args.backend == "ros2" and args.is_slave and not args.root_devices_as_slaves
    assert (args.host_node_ip, args.hostlink_port) == ("127.0.0.1", 18511)
    assert args.machine_name == "lab_host_managed_root_station"
    assert args.server_database_root == str(dpx.process_dir(tmp_path, "root-station") / "database")
    assert args.devices == [str(tmp_path / "acme_devices")] and args.external_devices_only
    # 图按 Host 权威 payload 原样启动；工作目录与运行开关继承自 Host
    assert args.graph_is_authority_payload and args.working_dir == str(tmp_path) and args.test_mode
    assert args.visual == "disable" and args.skip_env_check and args.disable_browser
    graph = json.loads(Path(args.graph).read_text(encoding="utf-8"))
    assert graph == {"nodes": spec.graph_nodes, "links": spec.graph_links}


def test_generated_specs_are_listed_and_controllable_but_not_editable(
    service: dpx.DeviceProcessService, tmp_path: Path
) -> None:
    dpx.sync_generated_specs(tmp_path, "root_devices_as_slaves", [_root_device_spec(tmp_path)])
    app = FastAPI()
    app.include_router(create_device_processes_router())
    client = TestClient(app)

    [item] = ok_data(client.get("/api/v1/device-processes"))["processes"]
    assert item["id"] == "root-station" and item["generated_by"] == "root_devices_as_slaves"
    # 子树里的物料节点不算设备
    assert item["device_ids"] == ["station", "pump"]
    assert item["graph_links"][0]["id"] == "inner"

    body = {"name": "改名", "devices": [{"id": "pump", "class": "acme_pump"}]}
    message = error_of(client.put("/api/v1/device-processes/root-station", json=body), 3003)
    assert "--root_devices_as_slaves" in str(message)
    error_of(client.delete("/api/v1/device-processes/root-station"), 3003)
    assert ok_data(client.post("/api/v1/device-processes/root-station/start"))["status"] == "running"
    assert ok_data(client.post("/api/v1/device-processes/root-station/restart"))["status"] == "running"
    assert ok_data(client.post("/api/v1/device-processes/root-station/stop"))["status"] == "stopped"
    assert dpx.load_specs(tmp_path)["root-station"].name == "station"


def test_unavailable_devices_explain_why_a_managed_process_is_down(
    service: dpx.DeviceProcessService, tmp_path: Path, monkeypatch
) -> None:
    class FailingPopen(FakePopen):
        def __init__(self, command: list[str], **kwargs: Any) -> None:
            super().__init__(command, **kwargs)
            self.stdout = io.BytesIO(
                b"[INFO] HostLink connected\n\x1b[31mRuntimeError: acme_station \xe6\x9e\x84\xe9\x80\xa0\xe5\xa4\xb1\xe8\xb4\xa5\x1b[0m\n"
                b"[INFO] bye\n"
            )

    monkeypatch.setattr(dpx.subprocess, "Popen", FailingPopen)
    # 「重启中」只持续一个退避（夹具里 50 ms），轮询在高负载 CI 上可能整段错过它。退避照常计时，
    # 真正的重启等用例读完这个状态再放行：窗口不再取决于线程调度。
    restart_allowed = threading.Event()
    restart_later = service._restart_later

    def restart_when_allowed(process_id: str) -> None:
        restart_allowed.wait(10)
        restart_later(process_id)

    monkeypatch.setattr(service, "_restart_later", restart_when_allowed)
    dpx.sync_generated_specs(tmp_path, "root_devices_as_slaves", [_root_device_spec(tmp_path, max_restarts=1)])
    # 从未拉起过的进程不登记原因（设备照旧只是还没上线）
    assert service.unavailable_devices() == {}

    service.start("root-station")
    assert service.unavailable_devices() == {}

    def wait_status(status: str) -> dict:
        deadline = time.time() + 5
        while time.time() < deadline:
            records = service.unavailable_devices()
            if records and records["station"]["managed_process"]["status"] == status:
                return records
            time.sleep(0.02)
        raise AssertionError(f"没有等到 {status}: {service.unavailable_devices()}")

    FakePopen.instances[-1].exit_code = 1
    records = wait_status("restarting")
    assert set(records) == {"station", "pump"}  # 物料节点 vial 不是设备
    station, pump = records["station"], records["pump"]
    assert pump["parent_device_id"] == "station" and "parent_device_id" not in station
    assert station["registry_name"] == "acme_station" and station["backend"]
    managed = station["managed_process"]
    log_path = str(dpx.process_dir(tmp_path, "root-station") / "process.log")
    assert managed["id"] == "root-station" and managed["log_path"] == log_path
    assert "RuntimeError: acme_station 构造失败" in managed["log_tail"]  # 着色已剥掉
    assert "意外退出（退出码 1）" in station["reason"] and "重启中" in station["reason"] and log_path in station["reason"]
    assert "日志最后的错误：RuntimeError: acme_station 构造失败" in station["reason"]

    # 拉起后：进程在跑就不再登记
    restart_allowed.set()
    deadline = time.time() + 5
    while time.time() < deadline and (len(FakePopen.instances) < 2 or service.unavailable_devices()):
        time.sleep(0.02)
    assert service.unavailable_devices() == {}

    FakePopen.instances[-1].exit_code = 1
    records = wait_status("crashed")
    assert "已自动重启 1 次仍退出，停止看护" in records["station"]["reason"]
    assert "修复后在设备进程页重启它" in records["station"]["reason"]

    service.stop("root-station")
    assert "已停止" in service.unavailable_devices()["station"]["reason"]
    assert dpx.managed_device_unavailability() == service.unavailable_devices()
