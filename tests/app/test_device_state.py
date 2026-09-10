"""设备状态存储（(device, property, value) 三元组，独立 SQLite）测试。

- 类型标记：str / int / float / bool 无损还原，复杂类型拒绝
- latest upsert：同 key 只有一行，时间戳推进
- history：只记变化点 + 每 key 环形裁剪
- 独立文件：与 inventory 库分开，重开持久
- 微后端桥：publish_device_status（ROS 形状）→ worker 串行写 + monitor 事件
- REST：GET 全量/单设备/历史 + POST report（422 拒绝复杂类型）
"""

import pytest

from unilabos.app.scheduler.device_state import DeviceStateStore
from unilabos.app.scheduler.monitor import MonitorBus


class TestStoreBasics:
    def test_scalar_roundtrip_with_types(self):
        store = DeviceStateStore()
        store.set("dev1", "temperature", 25.5)
        store.set("dev1", "count", 3)
        store.set("dev1", "mode", "heating")
        store.set("dev1", "door_open", True)
        props = store.latest_for("dev1")
        assert props["temperature"]["value"] == 25.5
        assert props["temperature"]["value_type"] == "float"
        assert props["count"]["value"] == 3
        assert props["count"]["value_type"] == "int"
        assert props["mode"]["value"] == "heating"
        assert props["door_open"]["value"] is True
        assert props["door_open"]["value_type"] == "bool"

    def test_complex_types_rejected(self):
        store = DeviceStateStore()
        with pytest.raises(TypeError):
            store.set("dev1", "config", {"a": 1})
        with pytest.raises(TypeError):
            store.set("dev1", "list", [1, 2])
        with pytest.raises(ValueError):
            store.set("", "p", 1)

    def test_latest_upsert_single_row(self):
        store = DeviceStateStore()
        assert store.set("dev1", "temp", 20.0) is True    # 新建 = 变化
        assert store.set("dev1", "temp", 20.0) is False   # 同值 = 无变化
        assert store.set("dev1", "temp", 21.0) is True
        assert store.stats()["properties"] == 1  # 始终一行

    def test_type_change_counts_as_change(self):
        store = DeviceStateStore()
        store.set("dev1", "v", 1)
        assert store.set("dev1", "v", "1") is True  # int→str 是变化

    def test_history_only_records_changes(self):
        store = DeviceStateStore()
        store.set("dev1", "temp", 20.0)
        store.set("dev1", "temp", 20.0)  # 无变化不记
        store.set("dev1", "temp", 21.0)
        entries = store.history("dev1", "temp")
        assert [e["value"] for e in entries] == [21.0, 20.0]  # 新→旧

    def test_history_ring_trim(self):
        store = DeviceStateStore(max_history_per_key=5)
        for i in range(12):
            store.set("dev1", "temp", float(i))
        entries = store.history("dev1", "temp", limit=100)
        assert len(entries) == 5
        assert entries[0]["value"] == 11.0  # 保留最新

    def test_separate_file_persists(self, tmp_path):
        db = str(tmp_path / "device_state.db")
        store = DeviceStateStore(db)
        store.set("dev1", "temp", 42.0)
        store.close()
        reopened = DeviceStateStore(db)
        assert reopened.latest_for("dev1")["temp"]["value"] == 42.0
        # 与 inventory 库完全分开（不同文件）
        assert not (tmp_path / "inventory.db").exists()

    def test_latest_all_grouping(self):
        store = DeviceStateStore()
        store.set("dev1", "temp", 20.0)
        store.set("dev2", "rpm", 300)
        snapshot = store.latest_all()
        assert set(snapshot) == {"dev1", "dev2"}
        assert snapshot["dev2"]["rpm"]["value"] == 300


class TestBackendBridge:
    def _make(self):
        from unilabos.app.scheduler.backend import JobExecutionBackend

        bus = MonitorBus()
        store = DeviceStateStore()
        backend = JobExecutionBackend(device_state_store=store, monitor=bus)
        backend.start()
        return backend, store, bus

    def test_publish_device_status_persists_via_worker(self):
        backend, store, bus = self._make()
        device_status = {"stirrer-1": {"speed": 250.0}}
        backend.publish_device_status(device_status, "stirrer-1", "speed")
        assert backend.wait_idle()
        assert store.latest_for("stirrer-1")["speed"]["value"] == 250.0
        events = bus.recent("device", 10)
        assert events[-1]["type"] == "device_property"
        assert events[-1]["data"] == {
            "device_id": "stirrer-1", "property": "speed", "value": 250.0,
        }
        backend.stop()

    def test_non_scalar_dropped(self):
        backend, store, _ = self._make()
        backend.publish_device_status({"d": {"cfg": {"x": 1}}}, "d", "cfg")
        assert backend.wait_idle()
        assert store.latest_for("d") == {}
        backend.stop()

    def test_report_device_properties_sync(self):
        backend, store, bus = self._make()
        changed = backend.report_device_properties("pump-1", {"flow": 1.5, "on": True})
        assert changed == {"flow": True, "on": True}
        assert store.latest_for("pump-1")["on"]["value"] is True
        # 同值再报不算变化、不发事件
        before = len(bus.recent("device", 50))
        assert backend.report_device_properties("pump-1", {"flow": 1.5}) == {"flow": False}
        assert len(bus.recent("device", 50)) == before
        backend.stop()

    def test_store_disabled_is_noop(self):
        from unilabos.app.scheduler.backend import JobExecutionBackend

        backend = JobExecutionBackend()  # 无 store
        backend.publish_device_status({"d": {"p": 1}}, "d", "p")  # 不抛
        with pytest.raises(RuntimeError):
            backend.report_device_properties("d", {"p": 1})


class TestDeviceStateApi:
    @pytest.fixture()
    def client(self):
        fastapi = pytest.importorskip("fastapi")  # noqa: F841
        from fastapi.testclient import TestClient

        from unilabos.app.scheduler.api import create_app

        return TestClient(create_app(device_state=DeviceStateStore()))

    def test_report_and_query(self, client):
        r = client.post(
            "/api/v1/device-state/report",
            json={"device_id": "heater-1", "properties": {"temp": 85.5, "mode": "pid"}},
        )
        assert r.status_code == 200
        assert r.json()["changed"] == {"temp": True, "mode": True}

        r_all = client.get("/api/v1/device-state").json()
        assert r_all["devices"]["heater-1"]["temp"]["value"] == 85.5
        assert r_all["stats"]["devices"] == 1

        r_one = client.get("/api/v1/device-state/heater-1").json()
        assert r_one["properties"]["mode"]["value"] == "pid"

    def test_history_endpoint(self, client):
        for temp in (20.0, 21.0, 21.0, 22.0):
            client.post(
                "/api/v1/device-state/report",
                json={"device_id": "heater-1", "properties": {"temp": temp}},
            )
        r = client.get("/api/v1/device-state/heater-1/history?property=temp").json()
        assert [e["value"] for e in r["entries"]] == [22.0, 21.0, 20.0]
        assert all(e["device_id"] == "heater-1" for e in r["entries"])
        assert all(e["property"] == "temp" for e in r["entries"])
        assert all(isinstance(e["id"], int) for e in r["entries"])

        all_history = client.get("/api/v1/device-state/history?limit=2").json()
        assert [e["value"] for e in all_history["entries"]] == [22.0, 21.0]

    def test_device_id_with_slash(self, client):
        """WorkstationNode 场景：device_id 本身含斜杠。"""
        client.post(
            "/api/v1/device-state/report",
            json={"device_id": "ws1/pump-2", "properties": {"flow": 0.8}},
        )
        r = client.get("/api/v1/device-state/ws1/pump-2").json()
        assert r["properties"]["flow"]["value"] == 0.8

    def test_complex_value_422(self, client):
        r = client.post(
            "/api/v1/device-state/report",
            json={"device_id": "d", "properties": {"cfg": {"a": 1}}},
        )
        assert r.status_code == 422

    def test_unknown_device_404(self, client):
        assert client.get("/api/v1/device-state/nope").status_code == 404

    def test_disabled_store_503(self):
        fastapi = pytest.importorskip("fastapi")  # noqa: F841
        from fastapi.testclient import TestClient

        from unilabos.app.scheduler.api import create_app

        client = TestClient(create_app())  # device_state=None
        assert client.get("/api/v1/device-state").status_code == 503
