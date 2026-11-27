"""实时录制：接受的观测样本写进 MCAP，回放样本不录，停止后可直接作回放源。"""

import pytest

pytest.importorskip("mcap")

from tests.app.test_inventory_dispatch_admission import station_inventory  # noqa: F401,E402
from unilabos.app.scheduler.device_observations import DeviceObservationService  # noqa: E402
from unilabos.app.scheduler.device_state import DeviceStateStore  # noqa: E402
from unilabos.app.scheduler.inventory.runtime_view import ObservationInventoryView  # noqa: E402
from unilabos.app.scheduler.observation_context import ObservationContext  # noqa: E402
from unilabos.app.scheduler.observation_sessions import ObservationSessions  # noqa: E402
from unilabos.runtime.observations import Descriptor, Selector  # noqa: E402
from unilabos.simulation import mcap_recorder  # noqa: E402
from unilabos.simulation.mcap_record import read_mcap  # noqa: E402
from unilabos.simulation.replay_feed import state_at  # noqa: E402


def _provider(observations, sources, group):
    from unilabos.simulation.observation_feed import FeedProvider

    feed = FeedProvider(observations, sources, group, [Selector(by="name", value="ROBOT")])
    feed.start(operation_id=f"op-{group}", source_session=f"s-{group}", quiescent=lambda: True)
    return feed


def test_recorder_writes_accepted_non_replay_samples(tmp_path, station_inventory):  # noqa: F811
    store, _, _ = station_inventory
    state = DeviceStateStore()
    context = ObservationContext(ObservationInventoryView(store), state)
    observations = DeviceObservationService(context)
    sources = ObservationSessions(context, observations)
    try:
        node = observations.resolve(Selector(by="name", value="ROBOT"))
        observations.describe(Selector(by="name", value="ROBOT"), Descriptor(
            version="1", properties={"joint_a": {"semantic": "observed", "unit": "rad", "frame": "joint"}}))
        sim = _provider(observations, sources, "sim-group")
        mcap_recorder.start(tmp_path / "run.mcap", task_uuid="task-1")
        for i in range(5):
            sim.feed(100.0 + i * 0.1, {"ROBOT": {"joint_a": 0.1 * i}}, sample_group=f"job-{i // 3}")
        result = mcap_recorder.stop()
        assert result["messages"] == 5 and result["task_uuid"] == "task-1" and len(result["sha256"]) == 64
        header, rows = read_mcap(tmp_path / "run.mcap")
        key = node["id"] or node["name"]
        assert [row["t"] for row in rows] == [0.0, 0.1, 0.2, 0.3, 0.4]
        assert state_at(rows, len(rows))[key]["joint_a"] == pytest.approx(0.4)
        assert header["source"]["workflow_task_uuid"] == "task-1"
        assert mcap_recorder.current() is None
    finally:
        mcap_recorder.stop()
        state.close()


def test_task_watcher_records_while_tasks_run(tmp_path):
    import json

    running = []
    now = [0.0]
    watcher = mcap_recorder.TaskRecordingWatcher(lambda: list(running), tmp_path, grace_s=3.0, clock=lambda: now[0])
    watcher.poll()
    assert mcap_recorder.current() is None
    running[:] = ["task-b", "task-a"]
    watcher.poll()
    recorder = mcap_recorder.current()
    assert recorder is not None and recorder.path == tmp_path / "task-a.mcap"
    recorder.record("arm", None, 1.0, "job-1", {"joint_a": 0.1})
    running[:] = ["task-b"]
    watcher.poll()
    assert mcap_recorder.current() is recorder
    running[:] = []
    watcher.poll()                      # 任务刚结束：仍在录（宽限期）
    now[0] = 2.0
    watcher.poll()
    assert mcap_recorder.current() is recorder
    now[0] = 3.5
    watcher.poll()
    assert mcap_recorder.current() is None
    sidecar = json.loads((tmp_path / "task-a.json").read_text(encoding="utf-8"))
    assert sidecar["tasks"] == ["task-a", "task-b"] and sidecar["messages"] == 1
