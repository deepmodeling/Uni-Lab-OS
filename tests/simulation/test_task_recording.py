"""任务录制：只用作业、属性变化点和物料台账拼回放记录。"""

from unilabos.app.scheduler.device_state import DeviceStateStore
from unilabos.simulation.replay_feed import command_index, state_at
from unilabos.simulation.task_recording import build_bundle, to_ms


def test_bundle_from_jobs_property_history_and_material_events():
    store = DeviceStateStore(max_history_per_key=0)
    try:
        base = to_ms("2026-11-20T10:00:00Z")
        store.set("arm", "lift_joint", 0.0, now_ms=base - 5000)          # 窗口前的最后值 → 初值
        for i in range(1, 1500):                                          # 超过旧的 1000 条上限也不裁
            store.set("arm", "lift_joint", i * 0.001, now_ms=base + i)
        store.set("arm", "lift_joint", 9.0, now_ms=base + 60_000)        # 窗口外，不进录制
        jobs = [{"uuid": "job-1", "material_uuid": "u-arm", "workflow_node_uuid": "pick",
                 "started_at": "2026-11-20T10:00:00Z", "finished_at": "2026-11-20T10:00:02Z"}]
        events = [{"kind": "moved", "material_uuid": "u-plate", "occurred_at": "2026-11-20T10:00:01.500Z",
                   "to_owner_uuid": "u-arm", "to_site": "gripper", "workflow_node_job_uuid": "job-1"}]
        ids = {"u-arm": "arm", "u-plate": "plate_1"}
        header, rows = build_bundle(
            jobs, events, node_id=ids.get, properties_of=lambda device: ["lift_joint"],
            history=lambda device, name, since: store.history(device, name, since, 5000), task_uuid="task-1")
        assert header["devices"] == {"arm": ["lift_joint"], "plate_1": ["parent", "site"]}
        assert command_index(header, rows) == [{"id": "job-1", "t_start": 0.0, "t_end": 2.0, "label": "arm"}]
        assert rows[0]["values"]["arm"]["lift_joint"] == 0.0
        final = state_at(rows, len(rows))
        assert final["arm"]["lift_joint"] == 1.499 and final["plate_1"] == {"parent": "arm", "site": "gripper"}
        assert len(store.history("arm", "lift_joint", 0, 5000)) == 1501
    finally:
        store.close()
