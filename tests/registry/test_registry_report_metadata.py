"""Host 侧注册表上报带给调度权威的接受规则依据：条目所属驱动包与版本（规则 A）、本机机器标识
与连着的 Slave（规则 B），以及 Slave 接入后的补报。"""

from __future__ import annotations

import gzip
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import unilabos
from unilabos.client.host.hostlink.client import HostLinkClient
from unilabos.client.host.hostlink.protocol import ActionType
from unilabos.client.host.hostlink.server import HostLinkServer, add_peer_listener
from unilabos.config.config import BasicConfig
from unilabos.registry.package_provenance import PackageResolver, registry_entry_packages
from unilabos.server.api.runtime.registry import install_registry_api
from unilabos.client.host.legacy_adaptor.sync.slaves import SlaveAnnouncer
from unilabos.client.host.legacy_adaptor.sync.templates import (
    TemplateSynchronizer,
    collect_registry_templates,
)
from unilabos.server.services.driver_packages import DriverPackageRecord, save_ledger
from unilabos.server.services.runtime.registry import RegistryService, set_registry_service
from unilabos.utils import machine_identity

AUTHORITY_MACHINE = "authority-machine"


def _project(root: Path, name: str, version: str) -> Path:
    root.mkdir(parents=True)
    (root / "pyproject.toml").write_text(f'[project]\nname = "{name}"\nversion = "{version}"\n', encoding="utf-8")
    package = root / name.replace("-", "_")
    (package / "devices").mkdir(parents=True)
    return package


def _device(name: str, file_path: str, *, goal: Any = None) -> dict[str, Any]:
    return {
        "id": name,
        "file_path": file_path,
        "class": {
            "module": f"{name}_pkg.devices:Device",
            "type": "python",
            "action_value_mappings": {"run": {"type": "UniLabJsonCommand", "goal": goal or {}}},
        },
        "available_sites": [{"id": "slot", "name": "slot"}],
    }


class _Registry:
    def __init__(self, devices: dict[str, Any]) -> None:
        self.device_type_registry = devices
        self.resource_type_registry: dict[str, Any] = {}
        self.workflow_registry: dict[str, Any] = {}

    def obtain_registry_device_info(self):
        return [{"id": key, **value} for key, value in self.device_type_registry.items()]

    def obtain_registry_resource_info(self):
        return []


class TestPackageProvenance:
    def test_ledger_unilabos_pyproject_and_unknown_sources(self, tmp_path) -> None:
        installed = _project(tmp_path / "driver_packages" / "lab-drivers" / "1.4.0", "lab-drivers", "9.9.9")
        save_ledger(tmp_path, {
            "lab_drivers": DriverPackageRecord(
                name="lab-drivers", version="1.4.0", package_root=str(installed.parent),
                package_dirs=[str(installed)],
            ),
        })
        mounted = _project(tmp_path / "checkout", "bench-drivers", "0.2.0")
        loose = tmp_path / "loose" / "devices"
        loose.mkdir(parents=True)
        resolver = PackageResolver(str(tmp_path))

        # 台账优先于源码树里的 pyproject（台账记的是安装时的版本）
        assert resolver.for_file(installed / "devices" / "pump.py") == ("lab-drivers", "1.4.0")
        assert resolver.for_file(Path(unilabos.__file__).parent / "registry" / "x.yaml") == (
            "unilabos", unilabos.__version__)
        assert resolver.for_file(mounted / "devices" / "arm.py") == ("bench-drivers", "0.2.0")
        assert resolver.for_file(loose / "thing.py") == ("", "")
        assert resolver.for_module("unilabos.client.host.host_services:HostServices") == (
            "unilabos", unilabos.__version__)
        assert resolver.for_module("lab_drivers.devices.pump:Pump") == ("lab-drivers", "1.4.0")

    def test_registry_entry_packages_covers_devices_and_workflow_templates(self, tmp_path) -> None:
        package = _project(tmp_path / "checkout", "bench-drivers", "0.2.0")
        registry = _Registry({"arm": _device("arm", str(package / "devices" / "arm.py"))})
        packages = registry_entry_packages(
            registry, [{"id": "unilabos.registry.workflows:demo", "module": "unilabos.registry.workflows"}],
            working_dir=str(tmp_path),
        )
        assert packages == {
            "arm": ("bench-drivers", "0.2.0"),
            "unilabos.registry.workflows:demo": ("unilabos", unilabos.__version__),
        }


class _Session:
    """把 TemplateSynchronizer 的 requests.Session 调用落到 TestClient 上。"""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.posted: list[dict[str, Any]] = []

    def get(self, url, headers=None, timeout=None):
        from urllib.parse import urlsplit

        return self.client.get(urlsplit(url).path, headers=headers or {})

    def post(self, url, data=None, headers=None, timeout=None):
        from urllib.parse import urlsplit

        self.posted.append(json.loads(gzip.decompress(data)))
        return self.client.post(urlsplit(url).path, content=data, headers=headers or {})


class TestReportCarriesAcceptanceInputs:
    @pytest.fixture()
    def stack(self, tmp_path, monkeypatch):
        service = RegistryService(tmp_path / "runtime.db", machine_id=AUTHORITY_MACHINE)
        app = FastAPI()
        install_registry_api(app)
        set_registry_service(service)
        monkeypatch.setattr(BasicConfig, "working_dir", str(tmp_path))
        try:
            yield service, _Session(TestClient(app))
        finally:
            set_registry_service(None)
            service.close()

    def test_device_projection_keeps_available_sites(self, tmp_path) -> None:
        registry = _Registry({"arm": _device("arm", str(tmp_path / "arm.py"))})
        devices, _resources, _workflows = collect_registry_templates(registry)
        assert devices[0]["available_sites"] == [{"id": "slot", "name": "slot"}]
        assert "file_path" not in devices[0]

    def test_local_report_accepts_plr_float_keys(self, tmp_path) -> None:
        """PyLabRobot 1.0 孔的 ``height_volume_data`` 是 ``{float: float}``：本机注册表上报照常完成。

        上报失败只记一条警告（fail-open），但结果是注册表没有上报、工作流作者编译报
        ``registry_not_reported``；键按 JSON 规则转成字符串。
        """

        from pylabrobot.resources import cor_96_wellplate_360uL_Fb

        from unilabos.client.host.legacy_adaptor.sync.templates import report_registry_snapshot_local
        from unilabos.resources.resource_tracker import ResourceTreeSet

        nodes = ResourceTreeSet.from_plr_resources(
            [cor_96_wellplate_360uL_Fb(name="corning_plate")], known_random_uuid=True, old_size=True
        ).dump_templates()[0]
        # 上一版写下的 config_info 缓存（按资源模块源码哈希复用）：键还是 PLR 原样的 float
        for node in nodes:
            volumes = node["config"].get("height_volume_data")
            if volumes:
                node["config"]["height_volume_data"] = {float(key): value for key, value in volumes.items()}
        registry = _Registry({"arm": _device("arm", str(tmp_path / "arm.py"))})
        registry.obtain_registry_resource_info = lambda: [{
            "id": "corning_plate",
            "class": {"module": "pylabrobot.resources:cor_96_wellplate_360uL_Fb", "type": "pylabrobot"},
            "config_info": nodes,
        }]
        service = RegistryService(tmp_path / "runtime.db", machine_id=AUTHORITY_MACHINE)
        try:
            report = report_registry_snapshot_local(registry, service)
            assert report is not None
            assert report.resource_count == 1
            payload = service.entry_detail("corning_plate")["active_payload"]
        finally:
            service.close()
        volumes = payload["config_info"][1]["config"]["height_volume_data"]
        assert volumes and all(isinstance(key, str) for key in volumes)

    def test_package_upgrade_on_a_remote_host_is_accepted_and_an_edit_is_not(self, stack, tmp_path, monkeypatch) -> None:
        """规则 A 端到端：另一台机器上的 Host 升级驱动包 → 自动接受；同版本里改动 → 挂起。"""

        service, session = stack
        root = tmp_path / "checkout"
        package = _project(root, "bench-drivers", "1.0.0")
        arm_file = str(package / "devices" / "arm.py")
        registry = _Registry({"arm": _device("arm", arm_file, goal={"speed": 1})})

        monkeypatch.setattr(BasicConfig, "machine_id", "lab-host-machine")  # Host 不在权威这台机器
        synchronizer = TemplateSynchronizer("http://authority", session=session)
        first = synchronizer.sync(registry)
        posted = session.posted[-1]
        assert posted["machine_id"] == "lab-host-machine" and posted["slaves"] == []
        assert posted["entries"][0]["package"] == "bench-drivers"
        assert posted["entries"][0]["package_version"] == "1.0.0"
        assert first.summary["counts"]["pending"] == 1  # 新包：跨机器上报要确认
        service.apply_pending("arm")

        (root / "pyproject.toml").write_text('[project]\nname = "bench-drivers"\nversion = "1.1.0"\n', encoding="utf-8")
        registry.device_type_registry["arm"] = _device("arm", arm_file, goal={"speed": 2})
        upgraded = TemplateSynchronizer("http://authority", session=session).sync(registry)
        assert upgraded.summary["auto_accepted"] == [{"name": "arm", "rule": "package-upgrade", "conflicts": []}]
        assert service.entry_detail("arm")["package_version"] == "1.1.0"

        registry.device_type_registry["arm"] = _device("arm", arm_file, goal={"speed": 3})
        edited = TemplateSynchronizer("http://authority", session=session).sync(registry)
        assert [item["name"] for item in edited.summary["pending"]] == ["arm"]
        assert edited.summary["pending"][0]["reason"] == "cross-machine-reporter"

    def test_same_machine_report_with_a_remote_slave_needs_acceptance(self, stack, tmp_path, monkeypatch) -> None:
        service, session = stack
        monkeypatch.setattr(BasicConfig, "machine_id", AUTHORITY_MACHINE)
        monkeypatch.setattr(
            "unilabos.client.host.legacy_adaptor.sync.templates.current_hostlink_slaves",
            lambda: [{"machine_name": "lab-b", "machine_id": "machine-b"}],
        )
        registry = _Registry({"arm": _device("arm", str(tmp_path / "arm.py"))})

        report = TemplateSynchronizer("http://authority", session=session).sync(registry)

        assert session.posted[-1]["slaves"] == [{"machine_name": "lab-b", "machine_id": "machine-b"}]
        assert report.summary["acceptance"]["reason"] == "cross-machine-slave"
        assert [row["machine_name"] for row in service.list_remote_slaves()] == ["lab-b"]


class TestSlaveIdentity:
    def test_machine_id_is_stable_and_overridable(self, monkeypatch) -> None:
        monkeypatch.setattr(BasicConfig, "machine_id", "")
        first = machine_identity.machine_id()
        assert len(first) == 32 and first == machine_identity.machine_id()
        monkeypatch.setattr(BasicConfig, "machine_id", "lab-box")
        assert machine_identity.machine_id() == "lab-box"

    def test_hello_records_the_slave_machine_and_notifies_listeners(self, monkeypatch) -> None:
        monkeypatch.setattr(BasicConfig, "machine_id", "slave-box")
        server = HostLinkServer("127.0.0.1", 0).start()
        seen: list[dict[str, Any]] = []
        remove = add_peer_listener(seen.append)
        client = HostLinkClient("127.0.0.1", server.port, machine_name="lab-b", heartbeat_interval=60)
        try:
            assert client.connect_blocking(timeout=5)
            peer = next(item for item in server.peers() if item["machine_name"] == "lab-b")
            assert peer["machine_id"] == "slave-box"
            assert [item["machine_name"] for item in seen] == ["lab-b"]
        finally:
            remove()
            client.close()
            server.stop()

    def test_old_slave_without_machine_id_is_kept_empty(self) -> None:
        server = HostLinkServer("127.0.0.1", 0)
        peer = server._touch_peer("conn", ActionType.HELLO, {"machine_name": "legacy", "device_ids": []})
        assert peer["machine_id"] == ""


class TestSlaveAnnouncer:
    def test_announces_changed_slave_lists_once(self) -> None:
        current: list[dict[str, str]] = []
        sent: list[list[dict[str, str]]] = []
        done = threading.Event()

        def send(slaves):
            sent.append(list(slaves))
            done.set()

        announcer = SlaveAnnouncer(send, slaves=lambda: list(current), debounce_s=0.01).start()
        try:
            time.sleep(0.1)
            assert sent == []  # 没有 Slave：与上报里带过的相同，不发
            current.append({"machine_name": "lab-b", "machine_id": "machine-b"})
            for _ in range(3):  # 接连几次接入合并成一次补报
                announcer._on_peer({"role": "slave", "machine_name": "lab-b"})
            assert done.wait(2)
            time.sleep(0.1)
            assert sent == [[{"machine_name": "lab-b", "machine_id": "machine-b"}]]
            announcer._on_peer({"role": "slave", "machine_name": "lab-b"})  # 重连：列表没变
            time.sleep(0.1)
            assert len(sent) == 1
            announcer._on_peer({"role": "doctor", "machine_name": "doctor"})
            time.sleep(0.1)
            assert len(sent) == 1
        finally:
            announcer.stop()

    def test_failed_send_is_retried_on_the_next_change(self) -> None:
        current = [{"machine_name": "lab-b", "machine_id": "machine-b"}]
        attempts: list[int] = []
        delivered = threading.Event()

        def send(slaves):
            attempts.append(len(slaves))
            if len(attempts) == 1:
                raise ConnectionError("权威暂时不可达")
            delivered.set()

        announcer = SlaveAnnouncer(send, slaves=lambda: list(current), debounce_s=0.01).start()
        try:
            time.sleep(0.1)
            assert attempts == [1]
            announcer._on_peer({"role": "slave"})
            assert delivered.wait(2)
        finally:
            announcer.stop()


def test_http_slave_sender_posts_without_a_token_and_reads_the_envelope(monkeypatch) -> None:
    """控制面 token 已去掉：补报不带令牌头；权威回失败信封（HTTP 200 + 业务码）时要报错，不能当成功。"""

    import requests

    from unilabos.client.host.legacy_adaptor.sync.slaves import http_slave_sender

    calls: list[dict[str, Any]] = []
    replies = iter([{"code": 0, "data": {"recorded": 1}}, {"code": 1001, "error": {"msg": "参数不合法"}}])

    class Reply:
        status_code = 200

        def __init__(self, body: dict[str, Any]) -> None:
            self._body = body

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return self._body

    def post(url: str, **kwargs: Any) -> Reply:
        calls.append({"url": url, **kwargs})
        return Reply(next(replies))

    monkeypatch.setattr(requests, "post", post)
    send = http_slave_sender("http://127.0.0.1:8002", edge_uuid="edge-1")
    send([{"machine_name": "slave-a", "machine_id": "m-a"}])
    assert calls[0]["url"] == "http://127.0.0.1:8002/api/v1/registry/slaves"
    assert "headers" not in calls[0]
    with pytest.raises(RuntimeError, match="拒绝"):
        send([{"machine_name": "slave-a", "machine_id": "m-a"}])
