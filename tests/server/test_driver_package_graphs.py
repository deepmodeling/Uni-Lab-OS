"""驱动包随包设备图：发现（data-files / 源码目录）与一键启动为受管进程。"""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.api_envelope import error_of, ok_data
from unilabos.server.api.driver_package_graphs import create_driver_package_graphs_router
from unilabos.server.services import device_processes as dpx
from unilabos.server.services import driver_package_graphs as dpg
from unilabos.server.services import driver_packages as dp


class FakePopen:
    instances: list["FakePopen"] = []

    def __init__(self, command: list[str], **_: Any) -> None:
        self.command = command
        self.pid = 5000 + len(FakePopen.instances)
        self.exit_code: int | None = None
        self.stdout = io.BytesIO(b"slave booting\n")
        FakePopen.instances.append(self)

    def poll(self) -> int | None:
        return self.exit_code

    def terminate(self) -> None:
        self.exit_code = 0 if self.exit_code is None else self.exit_code

    def kill(self) -> None:
        self.exit_code = -9

    def wait(self, timeout: float | None = None) -> int:
        return self.exit_code if self.exit_code is not None else 0


def _demo_graph(*device_ids: str, extra_node: dict | None = None) -> dict:
    nodes = [
        {
            "id": device_id,
            "uuid": f"0000-{index}",
            "name": device_id,
            "type": "device",
            "class": "lock_probe_demo",
            "template_name": "lock_probe_demo",
            "parent": None,
            "pose": {"position": {"x": 0, "y": 0, "z": 0}},
            "config": {},
            "data": {},
            "sites": [],
            "sites_initialized": True,
        }
        for index, device_id in enumerate(device_ids)
    ]
    if extra_node:
        nodes.append(extra_node)
    return {"nodes": nodes, "links": []}


@pytest.fixture()
def lab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """源码安装形态的 lock_demo：包目录 repo/lock_demo，图在 repo/graph。"""
    FakePopen.instances.clear()
    monkeypatch.setattr(dpx.subprocess, "Popen", FakePopen)
    repo = tmp_path / "LabDeviceLockDemo"
    pkg_dir = repo / "lock_demo"
    pkg_dir.mkdir(parents=True)
    graph_dir = repo / "graph"
    graph_dir.mkdir()
    (graph_dir / "lock_demo.json").write_text(
        json.dumps(_demo_graph("lock_probe_a", "lock_probe_b")), encoding="utf-8"
    )
    (graph_dir / "notes.json").write_text(json.dumps({"hello": "not a graph"}), encoding="utf-8")
    dp.save_ledger(
        tmp_path,
        {"lock_demo": dp.DriverPackageRecord(name="lock_demo", package_dirs=[str(pkg_dir)], device_ids=["lock_probe_demo"])},
    )
    processes = dpx.DeviceProcessService(tmp_path)
    monkeypatch.setattr(dpx, "_service", processes)
    monkeypatch.setattr(dp, "_service", dp.DriverPackageService(tmp_path))
    return SimpleNamespace(working_dir=tmp_path, repo=repo, processes=processes)


def test_lists_graphs_from_source_checkout_and_skips_non_graphs(lab) -> None:
    graphs = dpg.list_bundled_graphs(lab.working_dir, "lock-demo")
    assert [item["name"] for item in graphs] == ["lock_demo"]
    (graph,) = graphs
    assert graph["source"] == "source" and graph["device_only"] is True
    assert [device["id"] for device in graph["devices"]] == ["lock_probe_a", "lock_probe_b"]
    assert graph["devices"][0]["class"] == "lock_probe_demo"

    payload = dpg.bundled_graph_payload(lab.working_dir, "lock_demo", "lock_demo")
    assert len(payload["nodes"]) == 2
    with pytest.raises(dp.DriverPackageError, match="not found"):
        dpg.bundled_graph_payload(lab.working_dir, "lock_demo", "missing")
    with pytest.raises(dp.DriverPackageError, match="not found"):
        dpg.list_bundled_graphs(lab.working_dir, "ghost")


def test_distribution_data_files_take_precedence(lab, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """没有源码身份的旧台账仍兼容 RECORD 里的 data-files。"""
    share = tmp_path / "prefix" / "share" / "lock_demo" / "graph"
    share.mkdir(parents=True)
    (share / "lock_demo.json").write_text(json.dumps(_demo_graph("probe_from_wheel")), encoding="utf-8")
    (share / "bench.json").write_text(json.dumps(_demo_graph("bench")), encoding="utf-8")
    site_packages = tmp_path / "prefix" / "Lib" / "site-packages"
    site_packages.mkdir(parents=True)

    class _Entry(type(Path("x"))):  # PackagePath 替身：相对 site-packages 的 RECORD 路径
        pass

    class _Dist:
        files = [
            _Entry("../../share/lock_demo/graph/lock_demo.json"),
            _Entry("../../share/lock_demo/graph/bench.json"),
            _Entry("lock_demo/__init__.py"),
            _Entry("lock_demo/data/config.json"),
        ]

        @staticmethod
        def locate_file(entry):
            return site_packages / entry

    monkeypatch.setattr("importlib.metadata.distribution", lambda name: _Dist())
    graphs = {item["name"]: item for item in dpg.list_bundled_graphs(lab.working_dir, "lock_demo")}
    assert set(graphs) == {"bench", "lock_demo"}
    assert graphs["lock_demo"]["source"] == "dist"
    assert [device["id"] for device in graphs["lock_demo"]["devices"]] == ["probe_from_wheel"]


@pytest.mark.parametrize("layout", ["flat", "src", "root"])
def test_source_identity_uses_its_graphs_without_old_distribution(lab, monkeypatch, layout):
    package = lab.repo / "lock_demo"
    if layout == "src":
        package = lab.repo / "src" / "lock_demo"
        package.mkdir(parents=True)
    elif layout == "root":
        package = lab.repo
    records = dp.load_ledger(lab.working_dir)
    records["lock_demo"].source_kind = "local"
    records["lock_demo"].package_root = str(lab.repo)
    records["lock_demo"].package_dirs = [str(package)]
    dp.save_ledger(lab.working_dir, records)
    monkeypatch.setattr(dpg, "_distribution_json_files", lambda name: pytest.fail("源码包不应读取同名旧分发"))

    graphs = dpg.list_bundled_graphs(lab.working_dir, "lock_demo")
    assert [entry["name"] for entry in graphs] == ["lock_demo"]
    assert graphs[0]["devices"][0]["id"] == "lock_probe_a"
    assert graphs[0]["source"] == "source"
    # 当前源码删除图后，不能从旧分发重新引入它。
    (lab.repo / "graph" / "lock_demo.json").unlink()
    assert dpg.list_bundled_graphs(lab.working_dir, "lock_demo") == []


def test_launch_creates_process_then_reuses_it(lab) -> None:
    first = dpg.launch_bundled_graph(lab.working_dir, "lock_demo", "lock_demo", processes=lab.processes)
    assert first["created"] is True
    process = first["process"]
    assert process["name"] == "lock_demo/lock_demo"
    assert process["package_names"] == ["lock_demo"] and process["external_only"] is True
    assert process["device_ids"] == ["lock_probe_a", "lock_probe_b"]
    assert process["status"] == "running"
    command = FakePopen.instances[-1].command
    assert "--is_slave" in command and "--external_devices_only" in command
    assert command[command.index("--devices") + 1] == str(lab.repo / "lock_demo")

    # 包更新后图变了：再次启动复用同一条进程，规格更新并重启
    (lab.repo / "graph" / "lock_demo.json").write_text(
        json.dumps(_demo_graph("lock_probe_a", "lock_probe_b", "lock_auditor")), encoding="utf-8"
    )
    second = dpg.launch_bundled_graph(lab.working_dir, "lock_demo", "lock_demo", processes=lab.processes)
    assert second["created"] is False
    assert second["process"]["id"] == process["id"]
    assert second["process"]["device_ids"] == ["lock_probe_a", "lock_probe_b", "lock_auditor"]
    assert len(lab.processes.list()) == 1
    assert len(FakePopen.instances) == 2


def test_launch_rejects_graphs_with_material_nodes(lab) -> None:
    (lab.repo / "graph" / "bench.json").write_text(
        json.dumps(
            _demo_graph(
                "bench",
                extra_node={"id": "plate", "type": "plate", "class": "Plate", "template_name": "Plate", "parent": "bench"},
            )
        ),
        encoding="utf-8",
    )
    assert dpg.list_bundled_graphs(lab.working_dir, "lock_demo")[0]["device_only"] is False
    with pytest.raises(dp.DriverPackageError, match="非设备节点"):
        dpg.launch_bundled_graph(lab.working_dir, "lock_demo", "bench", processes=lab.processes)
    assert lab.processes.list() == []


def test_router_endpoints(lab) -> None:
    app = FastAPI()
    app.include_router(create_driver_package_graphs_router())
    client = TestClient(app)

    listed = client.get("/api/v1/driver-packages/lock_demo/graphs")
    assert listed.status_code == 200 and [item["name"] for item in ok_data(listed)] == ["lock_demo"]
    error_of(client.get("/api/v1/driver-packages/ghost/graphs"), 4001)
    error_of(client.get("/api/v1/driver-packages/lock_demo/graphs/missing"), 4001)
    payload = client.get("/api/v1/driver-packages/lock_demo/graphs/lock_demo")
    assert payload.status_code == 200 and len(ok_data(payload)["nodes"]) == 2

    launched = client.post("/api/v1/driver-packages/lock_demo/graphs/lock_demo/launch")
    assert launched.status_code == 200
    assert ok_data(launched)["created"] is True
    assert ok_data(launched)["process"]["status"] == "running"
