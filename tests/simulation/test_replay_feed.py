"""观测回放线程：等待设备、按记录发布 observed 样本、停止后不再写入。"""

import json

from tests.app.test_inventory_dispatch_admission import station_inventory  # noqa: F401
from unilabos.app.scheduler.device_observations import DeviceObservationService
from unilabos.app.scheduler.device_state import DeviceStateStore
from unilabos.app.scheduler.inventory.runtime_view import ObservationInventoryView
from unilabos.app.scheduler.observation_context import ObservationContext
from unilabos.app.scheduler.observation_sessions import ObservationSessions
from unilabos.runtime.observations import Descriptor, Selector
from unilabos.simulation.replay_feed import ObservationReplayFeed


def _record(path, robot_id):
    rows = [{"schema": "test", "select_by": "name", "devices": {robot_id: ["joint_a"]}}]
    rows += [{"t": 0.01 * i, "event": f"E{i}", "values": {robot_id: {"joint_a": 0.1 * i}}} for i in range(3)]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_replay_feed_publishes_rows_once_devices_exist(tmp_path, station_inventory):  # noqa: F811
    store, _, _ = station_inventory
    state = DeviceStateStore()
    context = ObservationContext(ObservationInventoryView(store), state)
    observations = DeviceObservationService(context)
    sources = ObservationSessions(context, observations)
    try:
        node = observations.resolve(Selector(by="name", value="ROBOT"))
        observations.describe(Selector(by="name", value="ROBOT"), Descriptor(
            version="1",
            properties={"joint_a": {"semantic": "observed", "unit": "rad", "frame": "joint"}},
            joints={"joint_a": "joint_a"},
        ))
        record = tmp_path / "record.jsonl"
        _record(record, "ROBOT")
        feed = ObservationReplayFeed(observations, sources, record, group="replay-test", loop=False,
                                     wait_interval_s=0.01)
        feed.start()
        feed._thread.join(timeout=10)
        assert feed.rows_published == 3
        robot = next(item for item in observations.snapshot()["nodes"] if item["uuid"] == node["uuid"])
        latest = robot["properties"]["joint_a"]["sample"]
        assert latest["value"] == 0.2 and latest["provider"] == "replay" and latest["sample_group"] == "E2"
        assert robot["descriptor"]["joints"] == {"joint_a": "joint_a"}
        # 回放样本只供显示，不进录制表。
        assert observations.records()["records"] == []
    finally:
        state.close()


def test_replay_feed_stop_before_devices_exist_ends_thread(tmp_path, station_inventory):  # noqa: F811
    store, _, _ = station_inventory
    state = DeviceStateStore()
    context = ObservationContext(ObservationInventoryView(store), state)
    observations = DeviceObservationService(context)
    sources = ObservationSessions(context, observations)
    try:
        record = tmp_path / "record.jsonl"
        _record(record, "missing_device")
        feed = ObservationReplayFeed(observations, sources, record, wait_interval_s=0.01)
        feed.start()
        feed.stop()
        feed._thread.join(timeout=5)
        assert not feed._thread.is_alive() and feed.rows_published == 0
    finally:
        state.close()


def test_replay_feed_recovers_group_left_by_previous_runtime(tmp_path, station_inventory):  # noqa: F811
    store, _, _ = station_inventory
    state = DeviceStateStore()
    context = ObservationContext(ObservationInventoryView(store), state)
    observations = DeviceObservationService(context)
    sources = ObservationSessions(context, observations)
    try:
        observations.describe(Selector(by="name", value="ROBOT"), Descriptor(
            version="1", properties={"joint_a": {"semantic": "observed", "unit": "rad", "frame": "joint"}}))
        sources.configure_group("replay-test", [Selector(by="name", value="ROBOT")])
        with context.transaction():
            group = context.group("replay-test")
            group["recovery_required"] = True
            context.save_group("replay-test", group)
        record = tmp_path / "record.jsonl"
        _record(record, "ROBOT")
        feed = ObservationReplayFeed(observations, sources, record, group="replay-test", loop=False,
                                     wait_interval_s=0.01)
        feed.start()
        feed._thread.join(timeout=10)
        assert feed.rows_published == 3
    finally:
        state.close()


def test_replay_feed_registers_header_descriptors_for_model_only_nodes(tmp_path, station_inventory):  # noqa: F811
    """只有模型、模板没有声明观测的节点：记录表头自带 matrix4 位姿属性的描述。"""
    store, _, _ = station_inventory
    state = DeviceStateStore()
    context = ObservationContext(ObservationInventoryView(store), state)
    observations = DeviceObservationService(context)
    sources = ObservationSessions(context, observations)
    try:
        node = observations.resolve(Selector(by="name", value="ROBOT"))
        pose = [1.0, 0.0, 0.0, 0.5, 0.0, 1.0, 0.0, -0.25, 0.0, 0.0, 1.0, 1.2, 0.0, 0.0, 0.0, 1.0]
        rows = [
            {"schema": "test", "select_by": "name", "devices": {"ROBOT": ["world_matrix"]},
             "descriptors": {"ROBOT": {"version": "scene-1", "properties": {"world_matrix": {
                 "semantic": "observed", "dtype": "matrix4", "unit": "m", "frame": "world", "ttl_seconds": 30}}}}},
            {"t": 0.0, "event": "command_86", "values": {"ROBOT": {"world_matrix": pose}}},
        ]
        record = tmp_path / "record.jsonl"
        record.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        feed = ObservationReplayFeed(observations, sources, record, group="replay-test", loop=False,
                                     wait_interval_s=0.01)
        feed.start()
        feed._thread.join(timeout=10)
        assert feed.rows_published == 1
        robot = next(item for item in observations.snapshot()["nodes"] if item["uuid"] == node["uuid"])
        assert robot["descriptor"]["properties"]["world_matrix"]["dtype"] == "matrix4"
        latest = robot["properties"]["world_matrix"]["sample"]
        assert latest["value"] == pose and latest["sample_group"] == "command_86"
    finally:
        state.close()


def _feed_with(tmp_path, observations, sources, rows, **kwargs):
    record = tmp_path / "record.jsonl"
    record.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    return ObservationReplayFeed(observations, sources, record, group="replay-test", wait_interval_s=0.01, **kwargs)


def test_load_record_rejects_undeclared_nodes_and_properties(tmp_path):
    import pytest

    from unilabos.simulation.replay_feed import load_record

    record = tmp_path / "record.jsonl"
    rows = [{"devices": {"ROBOT": ["joint_a"]}},
            {"t": 0.0, "values": {"ROBOT": {"joint_a": 0.0, "joint_b": 1.0}}},
            {"t": 0.1, "values": {"ARM": {"joint_a": 0.0}}}]
    record.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    with pytest.raises(ValueError) as error:
        load_record(record)
    assert "joint_b" in str(error.value) and "ARM" in str(error.value)


def test_command_index_from_events_and_header():
    from unilabos.simulation.replay_feed import command_index

    rows = [{"t": 0.0, "event": "idle"}, {"t": 0.5, "event": "command_7"},
            {"t": 1.0, "event": "command_7+8"}, {"t": 1.5, "event": "command_8"}]
    assert command_index({}, rows) == [{"id": "7", "t_start": 0.5, "t_end": 1.0},
                                       {"id": "8", "t_start": 1.0, "t_end": 1.5}]
    header = {"commands": [{"id": 3, "t_start": 0, "t_end": 2, "label": "转移"}]}
    assert command_index(header, rows) == [{"id": "3", "t_start": 0.0, "t_end": 2.0, "label": "转移"}]


def test_seek_publishes_joint_and_material_state_at_target_and_keeps_pause(tmp_path, station_inventory):  # noqa: F811
    """跳到某条命令：该时刻前每个属性的最后值（关节 + 物料父节点/显隐）一次发出，暂停保持。"""
    import time

    store, _, _ = station_inventory
    state = DeviceStateStore()
    context = ObservationContext(ObservationInventoryView(store), state)
    observations = DeviceObservationService(context)
    sources = ObservationSessions(context, observations)
    try:
        node = observations.resolve(Selector(by="name", value="ROBOT"))
        properties = {"joint_a": {"semantic": "observed", "unit": "rad", "frame": "joint"},
                      "parent": {"semantic": "observed", "dtype": "string", "unit": "-", "frame": "material"},
                      "visible": {"semantic": "observed", "dtype": "boolean", "unit": "-", "frame": "material"}}
        header = {"schema": "unilab.replay-bundle/1", "select_by": "name", "tip_order": {"ROBOT": ["tip_000"]},
                  "devices": {"ROBOT": list(properties)},
                  "descriptors": {"ROBOT": {"version": "bundle-1", "properties": properties}}}
        rows = [header,
                {"t": 0.0, "event": "idle", "values": {"ROBOT": {"joint_a": 0.0, "parent": "deck", "visible": True}}},
                {"t": 30.0, "event": "command_5", "values": {"ROBOT": {"joint_a": 0.5}}},
                {"t": 31.0, "event": "command_5", "values": {"ROBOT": {"parent": "gripper"}}},
                {"t": 60.0, "event": "command_6", "values": {"ROBOT": {"joint_a": 1.0, "visible": False}}}]
        feed = _feed_with(tmp_path, observations, sources, rows, loop=False)
        feed.pause()
        feed.start()
        deadline = time.monotonic() + 10
        while feed.status()["rows_published"] == 0 and time.monotonic() < deadline:
            feed.seek(command="5")
            time.sleep(0.05)
            robot = next(item for item in observations.snapshot()["nodes"] if item["uuid"] == node["uuid"])
            if "joint_a" in robot["properties"]:
                break
        feed.seek(t=31.0)
        time.sleep(0.3)
        robot = next(item for item in observations.snapshot()["nodes"] if item["uuid"] == node["uuid"])
        values = {name: robot["properties"][name]["sample"]["value"] for name in properties}
        assert values == {"joint_a": 0.5, "parent": "gripper", "visible": True}
        status = feed.status()
        assert status["paused"] and status["position_s"] == 31.0 and status["event"] == "command_5"
        assert [item["id"] for item in status["commands"]] == ["5", "6"]
        assert status["tip_order"] == {"ROBOT": ["tip_000"]}
        feed.stop()
        feed._thread.join(timeout=5)
    finally:
        state.close()


def test_replay_router_controls_and_reports_unconfigured():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from unilabos.simulation.replay_api import create_replay_router

    class Fake:
        def __init__(self):
            self.calls = []

        def status(self):
            return {"paused": False}

        def pause(self):
            self.calls.append("pause")
            return {"paused": True}

        def resume(self):
            return {"paused": False}

        def set_speed(self, speed):
            self.calls.append(("speed", speed))
            return {"speed": speed}

        def seek(self, t=None, command=None):
            if command == "404":
                raise KeyError("记录里没有命令 404")
            self.calls.append(("seek", t, command))
            return {"position_s": t}

    fake = Fake()
    holder = {"feed": None}
    app = FastAPI()
    app.include_router(create_replay_router(lambda: holder["feed"]))
    client = TestClient(app)
    assert client.get("/device-state/replay").status_code == 503
    holder["feed"] = fake
    assert client.post("/device-state/replay/pause").json()["data"] == {"paused": True}
    assert client.post("/device-state/replay/speed", json={"speed": 2}).json()["data"] == {"speed": 2.0}
    assert client.post("/device-state/replay/speed", json={"speed": 0}).status_code == 422
    assert client.post("/device-state/replay/seek", json={"command": "88"}).status_code == 200
    assert client.post("/device-state/replay/seek", json={"command": "404"}).status_code == 404
    assert fake.calls == ["pause", ("speed", 2.0), ("seek", None, "88")]
