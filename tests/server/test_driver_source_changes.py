"""源码改动按加载进程提示重启，测试不启动设备或执行真正重启。"""

import io
import os
import threading
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from unilabos.server.services.driver_source_changes import DriverSourceChanges, source_snapshot
from unilabos.server.services import driver_packages as dp, device_processes as dpx
from unilabos.server.api.driver_packages import create_driver_packages_router
from unilabos.server.api.device_processes import create_device_processes_router


def register(monitor, root, target="pump", kind="slave", auto_start=True):
    monitor.register(target, kind=kind, name=target, pid=100, roots=[str(root)],
                     packages=[root.name], auto_start=auto_start)


def settled(monitor):
    monitor.poll()
    monitor.poll()
    return monitor.status()


def test_content_changes_additions_deletions_and_noise(tmp_path):
    root = tmp_path / "unilabos_data" / "driver_packages" / "pump" / "1.0"
    root.mkdir(parents=True)
    code = root / "pump.py"
    code.write_text("version = 1\n")
    definition = root / "device.yaml"
    definition.write_text("class: pump\n")
    monitor = DriverSourceChanges()
    register(monitor, root)
    previous = code.stat()
    code.write_text("version = 2\n")
    os.utime(code, ns=(previous.st_atime_ns, previous.st_mtime_ns))
    definition.unlink()
    (root / "new.py").write_text("pass\n")
    (root / "run.log").write_text("log")
    (root / "database.db").write_bytes(b"sqlite")
    for directory in [".git", "__pycache__", ".staging"]:
        (root / directory).mkdir()
        (root / directory / "ignored.py").write_text("ignored")
    monitor.poll()
    assert not monitor.status()["restart_required"]
    notice = settled(monitor)["notices"][0]
    assert {Path(c["path"]).name: c["change"] for c in notice["changed_files"]} == {
        "pump.py": "modified", "device.yaml": "deleted", "new.py": "added",
    }
    assert notice["target"] == "slave"
    assert notice["process_id"] == "pump"
    assert notice["pid"] == 100
    assert notice["restart_request"]["path"] == "/api/v1/device-processes/pump/restart"
    assert not notice["waits_for_idle"]
    assert monitor.status()["automatic_restart"] is False


def test_only_loaded_targets_and_host_cascade_are_reported(tmp_path):
    shared = tmp_path / "shared"
    shared.mkdir()
    code = shared / "driver.py"
    code.write_text("old")
    other = tmp_path / "other"
    other.mkdir()
    monitor = DriverSourceChanges()
    register(monitor, shared, "host", "host")
    register(monitor, shared, "pump")
    register(monitor, other, "balance", auto_start=False)
    code.write_text("new")
    notices = settled(monitor)["notices"]
    assert {n["process_id"] for n in notices} == {"host", "pump"}
    host = next(n for n in notices if n["target"] == "host")
    assert {s["id"]: s["auto_start"] for s in host["affected_slaves"]} == {"pump": True, "balance": False}
    assert host["restart_request"]["body"] == {"mode": "quiescent", "scope": "auto"}
    monitor.describe("balance", name="天平", auto_start=True)
    host = next(n for n in monitor.status()["notices"] if n["target"] == "host")
    assert next(s for s in host["affected_slaves"] if s["id"] == "balance")["auto_start"]
    # 已检测的变化不会因读提示或代码改回去而假装已经重启。
    code.write_text("old")
    assert len(settled(monitor)["notices"]) == 2
    register(monitor, shared, "pump")
    assert [n["process_id"] for n in monitor.status()["notices"]] == ["host"]
    monitor.remove("host")
    assert not monitor.status()["restart_required"]


def test_saving_and_failed_package_transaction_do_not_publish_transient_files(tmp_path):
    code = tmp_path / "driver.py"
    code.write_text("old")
    monitor = DriverSourceChanges()
    register(monitor, tmp_path)
    code.write_text("partial")
    monitor.poll()
    code.write_text("finished")
    monitor.poll()
    assert not monitor.status()["restart_required"]
    code.write_text("old")
    monitor.mutation(True)
    code.unlink()
    settled(monitor)
    code.write_text("old")
    monitor.mutation(False)
    assert not settled(monitor)["restart_required"]


def test_unreadable_source_is_visible_and_does_not_clear_notices(tmp_path, monkeypatch):
    from unilabos.server.services import driver_source_changes as module
    code = tmp_path / "driver.py"
    code.write_text("old")
    monitor = DriverSourceChanges()
    register(monitor, tmp_path)
    code.write_text("new")
    settled(monitor)

    def denied(roots):
        raise PermissionError("denied")

    monkeypatch.setattr(module, "source_snapshot", denied)
    monitor.poll()
    assert monitor.status()["watching"][0]["scan_error"] == "denied"
    assert monitor.status()["restart_required"]


def test_source_scan_does_not_follow_directory_symlink_loops(tmp_path):
    (tmp_path / "driver.py").write_text("pass")
    (tmp_path / "loop").symlink_to(tmp_path, target_is_directory=True)
    assert len(source_snapshot([str(tmp_path)])) == 1


def test_package_and_shared_dependency_changes_have_explicit_targets(tmp_path):
    monitor = DriverSourceChanges()
    register(monitor, tmp_path / "pump", "pump")
    register(monitor, tmp_path / "balance", "balance")
    register(monitor, tmp_path / "host", "host", "host")
    monitor.package_changed("pump", reason="package_installed")
    assert [n["process_id"] for n in monitor.status()["notices"]] == ["pump"]
    monitor.package_changed("new-package", reason="package_installed", host_mount=True)
    assert {n["process_id"] for n in monitor.status()["notices"]} == {"host", "pump"}
    monitor.package_changed("pump", reason="shared_dependencies_changed", shared_dependencies=True)
    assert {n["process_id"] for n in monitor.status()["notices"]} == {"host", "pump", "balance"}


def test_monitor_runs_without_http_polling_and_can_close(tmp_path):
    monitor = DriverSourceChanges(interval=0.01)
    event = threading.Event()
    monitor.poll = event.set
    try:
        monitor.start()
        assert event.wait(2)
    finally:
        monitor.close()
    assert not monitor._thread.is_alive()


class FakeProcess:
    def __init__(self, *args, **kwargs):
        self.pid = 8765
        self.code = None
        self.stdout = io.BytesIO()

    def poll(self):
        return self.code

    def terminate(self):
        self.code = 0

    kill = terminate

    def wait(self, **kwargs):
        return self.code


@pytest.fixture
def services(tmp_path, monkeypatch):
    packages = dp.DriverPackageService(tmp_path)
    processes = dpx.DeviceProcessService(tmp_path)
    monkeypatch.setattr(dp, "_service", packages)
    monkeypatch.setattr(dpx, "_service", processes)
    monkeypatch.setattr(dpx.subprocess, "Popen", FakeProcess)
    monkeypatch.setattr(processes, "_ensure_monitor", lambda: None)
    monkeypatch.setattr(packages.source_changes, "start", lambda: None)
    yield packages, processes
    processes.shutdown()
    packages.source_changes.close()


def test_slave_restart_clears_only_its_notice_and_api_returns_impact(services, tmp_path):
    packages, processes = services
    root = tmp_path / "driver_packages" / "pump" / "1.0"
    root.mkdir(parents=True)
    code = root / "driver.py"
    code.write_text("old")
    dp.save_ledger(tmp_path, {"pump": dp.DriverPackageRecord(name="pump", package_root=str(root),
                   package_dirs=[str(root)], enabled=False)})
    packages.configure([], external_only=True)
    view = processes.create({"name": "泵站", "graph_nodes": [{"id": "pump", "class": "pump"}],
                             "package_names": ["pump"], "external_only": True,
                             "extra_args": ["--working_dir", str(tmp_path)]})
    processes.start(view["id"])
    code.write_text("new")
    settled(packages.source_changes)
    app = FastAPI()
    app.include_router(create_driver_packages_router())
    app.include_router(create_device_processes_router())
    with TestClient(app) as client:
        payload = client.get("/api/v1/driver-packages/changes").json()
        assert len(payload["notices"]) == 1
        assert payload["notices"][0]["name"] == "泵站"
        assert payload["notices"][0]["target"] == "slave"
        assert client.get("/api/v1/driver-packages").json()["restart_required"]
        assert client.get(f'/api/v1/device-processes/{view["id"]}').json()["restart_required"]
        # 查询没有偷偷替用户重启，执行已有重启命令后才建立新基线。
        assert processes._procs[view["id"]].poll() is None
        processes.restart(view["id"])
        assert client.get("/api/v1/driver-packages/changes").json()["notices"] == []
        code.write_text("again")
        settled(packages.source_changes)
        processes.stop(view["id"])
        assert not packages.source_changes.status()["restart_required"]


def test_slave_baseline_precedes_popen(services, tmp_path, monkeypatch):
    packages, processes = services
    root = tmp_path / "external"
    root.mkdir()
    code = root / "driver.py"
    code.write_text("old")
    view = processes.create({"name": "外部驱动", "graph_nodes": [{"id": "pump", "class": "pump"}],
                             "devices_dirs": [str(root)], "external_only": True})

    def launching(*args, **kwargs):
        code.write_text("changed during child startup")
        return FakeProcess()

    monkeypatch.setattr(dpx.subprocess, "Popen", launching)
    processes.start(view["id"])
    assert settled(packages.source_changes)["notices"][0]["process_id"] == view["id"]


def test_package_root_metadata_outside_python_directory_is_watched(services, tmp_path):
    packages, _ = services
    root = tmp_path / "project"
    code = root / "src" / "pump"
    code.mkdir(parents=True)
    metadata = root / "pyproject.toml"
    metadata.write_text('[project]\nname="pump"\nversion="1"\n')
    dp.save_ledger(tmp_path, {"pump": dp.DriverPackageRecord(name="pump", package_root=str(root),
                   package_dirs=[str(code)], enabled=True)})
    packages.configure([str(code)], external_only=True)
    metadata.write_text('[project]\nname="pump"\nversion="2"\n')
    notice = settled(packages.source_changes)["notices"][0]
    assert notice["changed_files"] == [{"path": str(metadata), "change": "modified"}]


def test_dependency_installer_failure_still_warns_shared_processes(services, tmp_path, monkeypatch):
    packages, _ = services
    source = tmp_path / "new_package"
    code = source / "pump"
    code.mkdir(parents=True)
    (code / "__init__.py").write_text("")
    (source / "pyproject.toml").write_text('[project]\nname="pump"\nversion="1"\ndependencies=["pyserial"]\n')
    packages.configure([], external_only=True)
    register(packages.source_changes, tmp_path / "other", "balance")

    def fail(*args):
        raise RuntimeError("模拟依赖安装中途失败")

    monkeypatch.setattr(packages, "_install_dependencies", fail)
    operation = packages._new_operation("install", str(source))
    with pytest.raises(RuntimeError):
        packages._run_install(operation.operation_id, dp.resolve_source(str(source)), True, False)
    notices = packages.source_changes.status()["notices"]
    assert {n["process_id"] for n in notices} == {"host", "balance"}
    assert all(n["changed_packages"] == ["pump"] for n in notices)
    assert all(n["reasons"] == ["shared_dependencies_changed"] for n in notices)


def test_invalid_slave_arguments_do_not_exit_host(services):
    packages, processes = services
    view = processes.create({"name": "参数错误", "graph_nodes": [{"id": "pump", "class": "pump"}],
                             "external_only": True, "extra_args": ["--devices"]})
    with pytest.raises(dpx.DeviceProcessError, match="启动参数无效"):
        processes.start(view["id"])
    assert view["id"] not in processes._procs
    assert packages.source_changes.status()["watching"] == []
