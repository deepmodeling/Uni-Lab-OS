"""实时观测流跟随：登记表头描述、边写边发布、心跳保活、截断后重读表头。"""

import json
import time

from tests.app.test_inventory_dispatch_admission import station_inventory  # noqa: F401
from unilabos.app.scheduler.device_observations import DeviceObservationService
from unilabos.app.scheduler.device_state import DeviceStateStore
from unilabos.app.scheduler.inventory.runtime_view import ObservationInventoryView
from unilabos.app.scheduler.observation_context import ObservationContext
from unilabos.app.scheduler.observation_sessions import ObservationSessions
from unilabos.runtime.observations import Selector
from unilabos.simulation.observation_feed import ObservationFollowFeed

DESCRIPTOR = {
    "version": "feed-1",
    "properties": {"joint_a": {"semantic": "observed", "unit": "rad", "frame": "joint", "ttl_seconds": 5.0}},
    "joints": {"joint_a": "joint_a"},
}


def _services(station_inventory):  # noqa: F811
    store, _, _ = station_inventory
    state = DeviceStateStore()
    context = ObservationContext(ObservationInventoryView(store), state)
    observations = DeviceObservationService(context)
    return state, observations, ObservationSessions(context, observations)


def _write(stream, row):
    stream.write(json.dumps(row) + "\n")
    stream.flush()


def _latest(observations, uuid):
    node = next(item for item in observations.snapshot()["nodes"] if item["uuid"] == uuid)
    return node, node["properties"].get("joint_a", {}).get("sample")


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_follow_feed_registers_descriptor_and_publishes_live_rows(tmp_path, station_inventory):  # noqa: F811
    state, observations, sources = _services(station_inventory)
    path = tmp_path / "motion.jsonl"
    feed = ObservationFollowFeed(observations, sources, path, group="feed-test", heartbeat_s=0.2,
                                 wait_interval_s=0.01)
    try:
        robot = observations.resolve(Selector(by="name", value="ROBOT"))
        with path.open("w", encoding="utf-8") as stream:
            _write(stream, {"schema": "test", "select_by": "name", "devices": {"ROBOT": ["joint_a"]},
                            "descriptors": {"ROBOT": DESCRIPTOR}})
            feed.start()
            for i in range(3):
                _write(stream, {"t": time.time(), "event": f"E{i}", "values": {"ROBOT": {"joint_a": 0.1 * i}}})
            assert _wait(lambda: feed.rows_published >= 3)
            node, sample = _latest(observations, robot["uuid"])
            assert node["descriptor"]["joints"] == {"joint_a": "joint_a"}
            assert sample["value"] == 0.2 and sample["sample_group"] == "E2"
            assert sample["basis"] == "simulated"  # 纯仿真的样本标为模拟值
            published = feed.rows_published
            assert _wait(lambda: feed.rows_published > published, timeout=2.0)  # heartbeat
            _, sample = _latest(observations, robot["uuid"])
            assert sample["value"] == 0.2 and sample["sample_group"] == "heartbeat"
    finally:
        feed.stop()
        if feed._thread is not None:
            feed._thread.join(timeout=5)
        state.close()


def test_follow_feed_waits_for_file_and_stops_cleanly(tmp_path, station_inventory):  # noqa: F811
    state, observations, sources = _services(station_inventory)
    feed = ObservationFollowFeed(observations, sources, tmp_path / "absent.jsonl", wait_interval_s=0.01)
    try:
        feed.start()
        time.sleep(0.05)
        feed.stop()
        feed._thread.join(timeout=5)
        assert not feed._thread.is_alive() and feed.rows_published == 0
    finally:
        state.close()


def test_feed_group_members_still_dispatch_through_native_chain(tmp_path, station_inventory, monkeypatch):  # noqa: F811
    from unilabos.simulation import observation_feed
    from unilabos.simulation.dispatch import SourceDispatcher

    state, observations, sources = _services(station_inventory)
    path = tmp_path / "motion.jsonl"
    feed = ObservationFollowFeed(observations, sources, path, group="feed-test", wait_interval_s=0.01)
    calls = []

    class Delegate:
        def dispatch(self, payload):
            calls.append(payload)
            return "native"

    try:
        robot = observations.resolve(Selector(by="name", value="ROBOT"))
        with path.open("w", encoding="utf-8") as stream:
            _write(stream, {"schema": "test", "select_by": "name", "devices": {"ROBOT": ["joint_a"]},
                            "descriptors": {"ROBOT": DESCRIPTOR}})
            feed.start()
            _write(stream, {"t": time.time(), "event": "E0", "values": {"ROBOT": {"joint_a": 0.0}}})
            assert _wait(lambda: feed.rows_published >= 1)
        dispatcher = SourceDispatcher(Delegate(), observations, sources)
        payload = {"device_id": robot["id"], "action": "move"}
        monkeypatch.delenv(observation_feed.ENV_FILE, raising=False)
        assert observation_feed.register_native_route(dispatcher) is False
        monkeypatch.setenv(observation_feed.ENV_FILE, str(path))
        assert observation_feed.register_native_route(dispatcher) is True
        # 站点测试库存里设备 id 不唯一，这里直接核对登记的直通路由与组来源/模式。
        assert dispatcher.routes[observation_feed.PROVIDER](payload, {}) == "native" and calls == [payload]
        group = observations.snapshot()["groups"]["feed-test"]
        assert group["provider"] == observation_feed.PROVIDER and group["mode"] == observation_feed.MODE
        _, sample = _latest(observations, robot["uuid"])
        assert sample["provider"] == observation_feed.PROVIDER
    finally:
        feed.stop()
        if feed._thread is not None:
            feed._thread.join(timeout=5)
        state.close()


def test_feed_provider_name_follows_the_environment(monkeypatch):
    from unilabos.simulation.observation_feed import provider_name

    monkeypatch.delenv("UNILABOS_OBSERVATION_FEED_PROVIDER", raising=False)
    assert provider_name() == "simulation-feed"
    monkeypatch.setenv("UNILABOS_OBSERVATION_FEED_PROVIDER", "twin-feed")
    assert provider_name() == "twin-feed"


def test_feed_basis_marks_twin_as_estimated(monkeypatch):
    from unilabos.simulation.observation_feed import basis

    monkeypatch.delenv("UNILABOS_OBSERVATION_FEED_PROVIDER", raising=False)
    monkeypatch.delenv("UNILABOS_OBSERVATION_FEED_BASIS", raising=False)
    assert basis() == "simulated"
    monkeypatch.setenv("UNILABOS_OBSERVATION_FEED_PROVIDER", "twin-feed")
    assert basis() == "estimated"
    monkeypatch.setenv("UNILABOS_OBSERVATION_FEED_BASIS", "observed")
    assert basis() == "observed"


def test_extra_feed_files_are_followed_as_separate_groups(tmp_path, station_inventory, monkeypatch):  # noqa: F811
    """逐台迁移：主观测流和另一路（机构运行时写的）各带不重叠的设备，同时被跟随。"""
    from unilabos.simulation import observation_feed

    state, observations, sources = _services(station_inventory)
    main, extra = tmp_path / "main.jsonl", tmp_path / "mechanism.jsonl"
    for path, name, value in ((main, "ROBOT", 0.25), (extra, "SOURCE-DEVICE", 0.75)):
        with path.open("w", encoding="utf-8") as stream:
            _write(stream, {"schema": "test", "select_by": "name", "devices": {name: ["joint_a"]},
                            "descriptors": {name: DESCRIPTOR}})
            _write(stream, {"t": time.time(), "event": "E", "values": {name: {"joint_a": value}}})
    monkeypatch.setenv(observation_feed.ENV_FILE, str(main))
    monkeypatch.setenv(observation_feed.ENV_EXTRA_FILES, str(extra))
    monkeypatch.setenv(observation_feed.ENV_GROUP, "station")
    try:
        feed = observation_feed.start_from_environment(observations, sources)
        followers = [feed, *observation_feed._extra_feeds]
        assert [f.group for f in followers] == ["station", "station-extra1"]
        assert _wait(lambda: all(f.rows_published >= 1 for f in followers))
        values = {}
        for name in ("ROBOT", "SOURCE-DEVICE"):
            uuid = observations.resolve(Selector(by="name", value=name))["uuid"]
            values[name] = _latest(observations, uuid)[1]["value"]
        assert values == {"ROBOT": 0.25, "SOURCE-DEVICE": 0.75}
        groups = observations.snapshot()["groups"]
        assert set(groups) == {"station", "station-extra1"}
    finally:
        threads = [f._thread for f in [observation_feed._feed, *observation_feed._extra_feeds] if f is not None]
        observation_feed.stop()
        for thread in threads:
            if thread is not None:
                thread.join(timeout=5)
        state.close()
    assert observation_feed._extra_feeds == []
