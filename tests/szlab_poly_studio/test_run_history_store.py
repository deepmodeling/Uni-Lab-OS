import csv
import json
import sqlite3

import pytest
import scripts.workflow_ui as workflow_ui
from scripts.run_history_store import RunHistoryStore, infer_station
from scripts.run_workflow_local import (
    RuntimeConfig,
    RuntimeDeviceFactoryConfig,
    RuntimeOpcSnapshotConfig,
    WorkflowNode,
)
from scripts.workflow_ui import WorkflowRunManager, _load_preset_runtime_config, load_preset


class MutableClock:
    def __init__(self, value: int) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


def _start_action(
    store: RunHistoryStore,
    *,
    execution_id: str,
    device_id: str,
    action_name: str,
    sample_id: str = "sample-1",
    params: dict | None = None,
    started_at: int,
) -> None:
    store.record_action_start(
        execution_id=execution_id,
        workflow_path="main-process.json",
        instance_id=f"instance-{sample_id}",
        sample_id=sample_id,
        node_id=f"node-{execution_id}",
        device_id=device_id,
        action_name=action_name,
        params=params or {},
        started_at=started_at,
    )


def test_initialize_eagerly_creates_database_and_directories(tmp_path):
    store = RunHistoryStore(tmp_path, session_id="run-initialize")

    paths = store.initialize()

    assert (tmp_path / "history.db").is_file()
    assert (tmp_path / "exports").is_dir()
    assert (tmp_path / "artifacts").is_dir()
    assert (tmp_path / "backups").is_dir()
    assert paths["database_path"] == str((tmp_path / "history.db").resolve())


def test_scheduler_timings_are_persisted_in_history_database(tmp_path):
    store = RunHistoryStore(tmp_path, session_id="run-timing")

    written = store.append_scheduler_timings(
        workflow_path="task-flow.json",
        entries=[
            {
                "cycle_id": 7,
                "step": "opc_poll",
                "phase": "finish",
                "timestamp_ms": 12_345,
                "duration_ms": 7344,
            },
            {
                "generation_id": 3,
                "step": "controller_run",
                "phase": "skipped",
                "reason": "in_flight",
                "timestamp_ms": 12_346,
            },
        ],
    )

    assert written == 2
    connection = sqlite3.connect(tmp_path / "history.db")
    rows = connection.execute(
        """
        SELECT workflow_path, cycle_id, generation_id, step, phase,
               timestamp, duration_ms, reason
        FROM scheduler_timings ORDER BY id
        """
    ).fetchall()
    assert rows == [
        ("task-flow.json", "7", "", "opc_poll", "finish", 12_345, 7344, ""),
        ("task-flow.json", "", "3", "controller_run", "skipped", 12_346, None, "in_flight"),
    ]


def test_timing_ledger_records_sample_device_station_and_robot_idle(tmp_path):
    clock = MutableClock(10_000)
    store = RunHistoryStore(tmp_path, session_id="run-a", clock=clock)

    _start_action(
        store,
        execution_id="robot-1",
        device_id="AI4C_robot_arm",
        action_name="submit_pick_from_s03",
        started_at=1_000,
    )
    store.record_action_finish(
        execution_id="robot-1",
        status="completed",
        result={"success": True, "station": "S03"},
        finished_at=2_000,
    )
    _start_action(
        store,
        execution_id="s07-1",
        device_id="szlab_s07_solid_addition",
        action_name="dose_powder",
        started_at=1_100,
    )
    store.record_action_finish(
        execution_id="s07-1",
        status="completed",
        result={"success": True},
        finished_at=3_100,
    )
    _start_action(
        store,
        execution_id="robot-2",
        device_id="AI4C_robot_arm",
        action_name="submit_place_to_s06",
        started_at=2_500,
    )
    store.record_action_finish(
        execution_id="robot-2",
        status="completed",
        result={"success": True, "station": "S06"},
        finished_at=3_000,
    )

    ledger = store.timing_ledger("run-a")

    assert ledger["total_time_ms"] == 2_100
    assert ledger["robot"] == {
        "action_count": 2,
        "busy_time_ms": 1_500,
        "idle_time_ms": 600,
        "utilization_percent": 71.43,
        "idle_definition": "首个动作开始至最后动作结束区间减去机械臂动作区间并集",
    }
    assert ledger["samples"] == [
        {
            "sample_id": "sample-1",
            "action_count": 3,
            "completed_count": 3,
            "failed_count": 0,
            "running_count": 0,
            "total_action_time_ms": 3_500,
            "average_action_time_ms": 1_167,
            "max_action_time_ms": 2_000,
            "elapsed_time_ms": 2_100,
            "first_started_at": 1_000,
            "last_finished_at": 3_100,
        }
    ]
    assert {item["device_id"] for item in ledger["devices"]} == {
        "AI4C_robot_arm",
        "szlab_s07_solid_addition",
    }
    assert {item["station"] for item in ledger["stations"]} == {
        "S03",
        "S06",
        "S07",
    }


def test_station_ledger_keeps_full_result_events_and_s07_measurement(tmp_path):
    store = RunHistoryStore(tmp_path, session_id="run-s07")
    _start_action(
        store,
        execution_id="dose-1",
        device_id="szlab_s07_solid_addition",
        action_name="dose_powder",
        sample_id="sample-7",
        params={"target_weight": 12.5, "recipe_name": "A"},
        started_at=1_000,
    )
    store.append_event(
        execution_id="dose-1",
        workflow_path="main-process.json",
        instance_id="instance-sample-7",
        sample_id="sample-7",
        node_id="node-dose-1",
        level="info",
        message="粗加完成，读取天平",
        detail={"balance_reading": 12.42},
        timestamp=1_800,
    )
    result = {
        "success": True,
        "data": {
            "station": "S07",
            "target_weight": 12.5,
            "balance_reading": 12.42,
            "balance_sample_count": 8,
            "display_message": "误差 -0.08 g",
        },
    }
    store.record_action_finish(
        execution_id="dose-1",
        status="completed",
        result=result,
        finished_at=2_000,
    )

    ledger = store.station_ledger("run-s07", station="s07")
    action = ledger["stations"][0]["actions"][0]

    assert ledger["stations"][0]["station"] == "S07"
    assert action["params"] == {"target_weight": 12.5, "recipe_name": "A"}
    assert action["result"] == result
    assert action["measurements"] == [
        {
            "name": "固体重量",
            "target": 12.5,
            "actual": 12.42,
            "unit": "g",
            "source": "mapping[1]",
            "error": -0.08,
            "absolute_error": 0.08,
            "relative_error_percent": -0.64,
        }
    ]
    assert action["events"] == [
        {
            "timestamp": 1_800,
            "level": "info",
            "event_type": "log",
            "message": "粗加完成，读取天平",
            "detail": {"balance_reading": 12.42},
        }
    ]


def test_store_recovers_interrupted_actions_and_persists_across_restart(tmp_path):
    first = RunHistoryStore(tmp_path, session_id="run-old", clock=MutableClock(1_000))
    _start_action(
        first,
        execution_id="unfinished",
        device_id="szlab_s06_pump",
        action_name="pump",
        started_at=500,
    )
    first._connection.close()
    first._connection = None

    second = RunHistoryStore(tmp_path, session_id="run-new", clock=MutableClock(2_000))
    runs = second.list_runs()

    old_run = next(item for item in runs["runs"] if item["run_id"] == "run-old")
    assert old_run["status"] == "interrupted"
    station_action = second.station_ledger("run-old")["stations"][0]["actions"][0]
    assert station_action["status"] == "interrupted"
    assert station_action["duration_ms"] == 1_500


def test_json_and_csv_exports_are_written_under_exports_directory(tmp_path):
    store = RunHistoryStore(tmp_path, session_id="run-export")
    _start_action(
        store,
        execution_id="s05-photo",
        device_id="szlab_s05_camera",
        action_name="take_photo",
        started_at=1_000,
    )
    store.record_action_finish(
        execution_id="s05-photo",
        status="completed",
        result={"success": True, "photo_path": "/tmp/photo.jpg"},
        finished_at=1_500,
    )

    timing_path = store.export_ledger(
        ledger="timing", run_id="run-export", file_format="json"
    )
    station_path = store.export_ledger(
        ledger="stations", run_id="run-export", file_format="csv"
    )

    assert timing_path.parent == tmp_path / "exports"
    assert json.loads(timing_path.read_text(encoding="utf-8"))["total_time_ms"] == 500
    with station_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["station"] == "S05"
    assert json.loads(rows[0]["result_json"])["photo_path"] == "/tmp/photo.jpg"


def test_infer_station_prefers_explicit_result_then_method_and_device():
    assert infer_station(
        device_id="AI4C_robot_arm",
        action_name="submit_pick_from_s03",
        result={"station": "S072"},
    ) == "S072"
    assert infer_station(
        device_id="AI4C_robot_arm",
        action_name="submit_pick_from_s03",
    ) == "S03"
    assert infer_station(
        device_id="szlab_s09_pipetting_station",
        action_name="add_liquid",
    ) == "S09"


def test_sqlite_uses_expected_tables(tmp_path):
    store = RunHistoryStore(tmp_path, session_id="run-schema")
    store.list_runs()

    with sqlite3.connect(tmp_path / "history.db") as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert {
        "run_sessions",
        "action_records",
        "action_events",
        "scheduler_incidents",
    } <= tables


def test_task_action_wrapper_records_automatically_without_changing_result(
    tmp_path,
    monkeypatch,
):
    store = RunHistoryStore(tmp_path, session_id="run-wrapper")
    preset = load_preset("szlab_robot_action_workflow")
    manager = WorkflowRunManager(
        preset,
        _load_preset_runtime_config(preset),
        run_history_store=store,
    )
    node = WorkflowNode(
        uuid="dose-node",
        name="auto-dose_powder",
        device_name="szlab_s07_solid_addition",
        method="dose_powder",
        param={"target_weight": 10.0},
    )
    expected = [
        {
            "uuid": "dose-node",
            "result": {
                "success": True,
                "data": {
                    "station": "S07",
                    "target_weight": 10.0,
                    "balance_reading": 9.98,
                },
            },
        }
    ]

    def fake_run(*_args, logger, **_kwargs):
        logger.log("读取天平完成", detail={"balance_reading": 9.98})
        return expected

    monkeypatch.setattr(workflow_ui, "_run_node_with_live_opc_sampling", fake_run)

    result = manager._run_task_action_node(
        node,
        {},
        lambda **_kwargs: None,
        {
            "workflow_path": "main-process.json",
            "instance_id": "instance-1",
            "sample_id": "sample-1",
            "node_id": "dose-node",
            "execution_id": "execution-1",
        },
    )

    assert result is expected
    action = store.station_ledger("run-wrapper")["stations"][0]["actions"][0]
    assert action["status"] == "completed"
    assert action["result"] == expected
    assert action["events"][0]["message"] == "读取天平完成"
    manager.shutdown()


def test_dispatch_stall_keeps_sample_context_escalates_and_recovers(tmp_path):
    clock = MutableClock(1_000)
    store = RunHistoryStore(
        tmp_path,
        session_id="run-stall",
        clock=clock,
        dispatch_warning_ms=60,
        dispatch_critical_ms=300,
    )
    diagnostic = {
        "code": "input_trigger_wait",
        "message": "等待 S09 AllowProcess 条件满足",
        "instance_id": "instance-12",
        "sample_id": "sample-12",
        "node_id": "s09-add-liquid",
        "device_id": "szlab_s09_pipetting_station",
        "action_name": "add_liquid_with_reusable_tip",
        "phase": "等待派发",
        "detail": {"variable": "AllowProcess9", "actual": False},
    }

    store.observe_scheduler_cycle(
        workflow_path="main-process.json",
        diagnostics=[diagnostic],
        cycle_stats={"active": 1, "claimed": 0, "in_flight": 0},
    )
    assert store.incident_ledger("run-stall")["incidents"] == []

    clock.value = 1_061
    store.observe_scheduler_cycle(
        workflow_path="main-process.json",
        diagnostics=[diagnostic],
        cycle_stats={"active": 1, "claimed": 0, "in_flight": 0},
    )
    incident = store.incident_ledger("run-stall")["incidents"][0]
    assert incident["severity"] == "warning"
    assert incident["status"] == "open"
    assert incident["sample_id"] == "sample-12"
    assert incident["instance_id"] == "instance-12"
    assert incident["node_id"] == "s09-add-liquid"
    assert incident["station"] == "S09"
    assert incident["first_seen_at"] == 1_000
    assert incident["duration_ms"] == 61

    clock.value = 1_301
    store.observe_scheduler_cycle(
        workflow_path="main-process.json",
        diagnostics=[diagnostic],
        cycle_stats={"active": 1, "claimed": 0, "in_flight": 0},
    )
    incident = store.incident_ledger("run-stall")["incidents"][0]
    assert incident["severity"] == "critical"
    assert incident["duration_ms"] == 301

    clock.value = 1_400
    store.observe_scheduler_cycle(
        workflow_path="main-process.json",
        diagnostics=[],
        cycle_stats={"active": 1, "claimed": 1, "in_flight": 1},
    )
    incident = store.incident_ledger("run-stall")["incidents"][0]
    assert incident["status"] == "recovered"
    assert incident["recovered_at"] == 1_400
    assert incident["duration_ms"] == 400


def test_task_action_error_is_recorded_with_sample_station_and_time(
    tmp_path,
    monkeypatch,
):
    clock = MutableClock(5_000)
    store = RunHistoryStore(
        tmp_path,
        session_id="run-error",
        clock=clock,
    )
    preset = load_preset("szlab_robot_action_workflow")
    manager = WorkflowRunManager(
        preset,
        _load_preset_runtime_config(preset),
        run_history_store=store,
    )
    node = WorkflowNode(
        uuid="s08-cap",
        name="auto-process_cap",
        device_name="szlab_s08_cap_station",
        method="process_cap",
        param={"sample_id": "sample-8"},
    )

    def fail_run(*_args, **_kwargs):
        raise RuntimeError("S08 工站报警中")

    monkeypatch.setattr(workflow_ui, "_run_node_with_live_opc_sampling", fail_run)

    with pytest.raises(RuntimeError, match="S08 工站报警中"):
        manager._run_task_action_node(
            node,
            {},
            lambda **_kwargs: None,
            {
                "workflow_path": "main-process.json",
                "instance_id": "instance-8",
                "sample_id": "sample-8",
                "node_id": "s08-cap",
                "execution_id": "execution-8",
            },
        )

    entries = manager.list_task_action_logs(
        workflow_path="main-process.json"
    )["entries"]
    assert len(entries) == 1
    assert entries[0]["category"] == "action"
    assert entries[0]["level"] == "error"
    assert entries[0]["code"] == "action_exception"
    assert entries[0]["phase"] == "executing"
    assert entries[0]["message"] == (
        "Action 执行失败：RuntimeError：S08 工站报警中"
    )
    assert entries[0]["detail"]["exception_type"] == "RuntimeError"

    incidents = store.incident_ledger("run-error")["incidents"]
    assert len(incidents) == 1
    incident = incidents[0]
    assert incident["code"] == "action_failed"
    assert incident["sample_id"] == "sample-8"
    assert incident["instance_id"] == "instance-8"
    assert incident["station"] == "S08"
    assert incident["first_seen_at"] == 5_000
    assert incident["message"] == "S08 工站报警中"
    manager.shutdown()


def test_task_action_false_result_is_logged_once_as_failed_result(tmp_path):
    store = RunHistoryStore(tmp_path, session_id="run-failed-result")
    preset = load_preset("szlab_robot_action_workflow")
    manager = WorkflowRunManager(
        preset,
        _load_preset_runtime_config(preset),
        run_history_store=store,
    )
    node = WorkflowNode(
        uuid="failed-node",
        name="fail_action",
        device_name="device",
        method="fail_action",
        param={},
        legacy_route_compatible=False,
    )

    class FailedDevice:
        def fail_action(self):
            return {"success": False, "message": "目标工位被占用"}

    device = FailedDevice()
    with pytest.raises(workflow_ui.ActionReturnedFailure):
        manager._run_task_action_node(
            node,
            {"device": device},
            device.fail_action,
            {
                "workflow_path": "main-process.json",
                "instance_id": "instance-failed",
                "sample_id": "sample-failed",
                "template_id": "template-failed",
                "node_id": "failed-node",
                "execution_id": "execution-failed",
                "device_id": "device",
                "action_name": "fail_action",
            },
        )

    entries = manager.list_task_action_logs(
        workflow_path="main-process.json"
    )["entries"]
    assert len(entries) == 2
    result_entries = [entry for entry in entries if entry["category"] == "result"]
    assert len(result_entries) == 1
    assert result_entries[0]["level"] == "error"
    assert result_entries[0]["code"] == "action_returned_failure"
    assert result_entries[0]["message"] == "动作结果：失败 · 目标工位被占用"
    assert not any(entry["code"] == "action_exception" for entry in entries)

    incidents = store.incident_ledger("run-failed-result")["incidents"]
    assert len(incidents) == 1
    assert incidents[0]["code"] == "action_returned_failure"
    manager.shutdown()


def test_scheduler_errors_flow_to_task_logs_once_until_recovered(tmp_path):
    store = RunHistoryStore(tmp_path, session_id="run-scheduler-error")
    preset = load_preset("szlab_robot_action_workflow")
    manager = WorkflowRunManager(
        preset,
        _load_preset_runtime_config(preset),
        run_history_store=store,
    )
    manager._task_execution_coordinator.shutdown()
    diagnostic = {
        "category": "dispatch_error",
        "code": "unsupported_action",
        "message": "不支持的 Task 动作节点: unknown-node",
        "severity": "error",
        "immediate": True,
        "phase": "dispatching",
        "instance_id": "instance-1",
        "sample_id": "sample-1",
        "template_id": "template-1",
        "node_id": "unknown-node",
        "execution_id": "execution-1",
        "device_id": "missing-device",
        "action_name": "missing-action",
        "detail": {"reason": "method_not_found"},
    }

    class FakeCoordinator:
        def __init__(self):
            self.calls = 0

        def cycle(self, **_kwargs):
            self.calls += 1
            diagnostics = [] if self.calls == 3 else [dict(diagnostic)]
            return {
                "success": True,
                "active": 1,
                "in_flight": 0,
                "claimed": 0,
                "completed": 0,
                "failed": 0,
                "diagnostics": diagnostics,
            }

        def shutdown(self):
            return {"success": True, "in_flight": 0}

    manager._task_execution_coordinator = FakeCoordinator()
    for _index in range(4):
        manager.run_task_execution_cycle(
            workflow_path="main-process.json",
            workflow_payload={"nodes": []},
            harvest_only=True,
        )

    entries = manager.list_task_action_logs(
        workflow_path="main-process.json"
    )["entries"]
    assert len(entries) == 3
    assert [entry["category"] for entry in entries] == [
        "schedule",
        "schedule",
        "schedule",
    ]
    assert [entry["level"] for entry in entries] == ["error", "info", "error"]
    assert [entry["code"] for entry in entries] == [
        "unsupported_action",
        "scheduler_error_recovered",
        "unsupported_action",
    ]
    assert entries[0]["phase"] == "dispatching"
    assert entries[0]["execution_id"] == "execution-1"
    assert entries[0]["detail"] == {
        "type": "scheduler_diagnostic",
        "diagnostic_category": "dispatch_error",
        "immediate": True,
        "diagnostic": {"reason": "method_not_found"},
    }
    assert entries[1]["phase"] == "recovered"
    assert entries[1]["detail"]["original_code"] == "unsupported_action"
    manager.shutdown()


def test_scheduler_cycle_exception_flows_to_task_error_log_without_spam(tmp_path):
    store = RunHistoryStore(tmp_path, session_id="run-cycle-error")
    preset = load_preset("szlab_robot_action_workflow")
    manager = WorkflowRunManager(
        preset,
        _load_preset_runtime_config(preset),
        run_history_store=store,
    )
    manager._task_execution_coordinator.shutdown()

    class RaisingCoordinator:
        def cycle(self, **_kwargs):
            raise TimeoutError("Task 服务连接超时")

        def shutdown(self):
            return {"success": True, "in_flight": 0}

    manager._task_execution_coordinator = RaisingCoordinator()
    for _index in range(2):
        with pytest.raises(TimeoutError, match="Task 服务连接超时"):
            manager.run_task_execution_cycle(
                workflow_path="main-process.json",
                workflow_payload={"nodes": []},
                harvest_only=True,
            )

    entries = manager.list_task_action_logs(
        workflow_path="main-process.json"
    )["entries"]
    assert len(entries) == 1
    assert entries[0]["category"] == "schedule"
    assert entries[0]["level"] == "error"
    assert entries[0]["code"] == "execution_cycle_failed"
    assert entries[0]["phase"] == "scheduling"
    assert entries[0]["message"] == (
        "调度循环失败：TimeoutError：Task 服务连接超时"
    )
    assert entries[0]["detail"]["diagnostic"]["exception_type"] == (
        "TimeoutError"
    )
    manager.shutdown()


def test_action_opc_snapshot_failures_are_structured_errors(tmp_path):
    store = RunHistoryStore(tmp_path, session_id="run-opc-snapshot-error")
    runtime_config = RuntimeConfig(
        path=tmp_path / "runtime.json",
        device_factory=RuntimeDeviceFactoryConfig(),
        opc_snapshot=RuntimeOpcSnapshotConfig(
            action_variables={"run": ["ready"]}
        ),
    )
    manager = WorkflowRunManager(
        load_preset("szlab_robot_action_workflow"),
        runtime_config,
        run_history_store=store,
    )

    class OfflineDevice:
        @staticmethod
        def get_variables(_names, use_cache=False):
            assert use_cache is False
            return {"ready": {"success": False, "error": "offline"}}

        @staticmethod
        def run():
            return {"success": True}

    device = OfflineDevice()
    result = manager._run_task_action_node(
        WorkflowNode(
            uuid="opc-node",
            name="run",
            device_name="device",
            method="run",
            param={},
            legacy_route_compatible=False,
        ),
        {"device": device},
        device.run,
        {
            "workflow_path": "main-process.json",
            "instance_id": "instance-opc",
            "sample_id": "sample-opc",
            "template_id": "template-opc",
            "node_id": "opc-node",
            "execution_id": "execution-opc",
            "device_id": "device",
            "action_name": "run",
        },
    )

    assert result[0]["result"] == {"success": True}
    entries = manager.list_task_action_logs(
        workflow_path="main-process.json"
    )["entries"]
    failures = [
        entry
        for entry in entries
        if entry["code"] == "opc_snapshot_read_failed"
    ]
    assert [entry["phase"] for entry in failures] == [
        "sampling_before",
        "sampling_after",
    ]
    assert all(entry["category"] == "opc" for entry in failures)
    assert all(entry["level"] == "error" for entry in failures)
    assert failures[0]["detail"]["failures"][0]["name"] == "ready"
    assert failures[0]["detail"]["failures"][0]["error"] == "offline"

    incidents = store.incident_ledger("run-opc-snapshot-error")["incidents"]
    assert len(incidents) == 2
    assert all(incident["category"] == "opc_error" for incident in incidents)
    manager.shutdown()


def test_action_opc_snapshot_failure_emits_recovery_after_success(tmp_path):
    store = RunHistoryStore(tmp_path, session_id="run-opc-snapshot-recovery")
    runtime_config = RuntimeConfig(
        path=tmp_path / "runtime.json",
        device_factory=RuntimeDeviceFactoryConfig(),
        opc_snapshot=RuntimeOpcSnapshotConfig(
            action_variables={"run": ["ready"]}
        ),
    )
    manager = WorkflowRunManager(
        load_preset("szlab_robot_action_workflow"),
        runtime_config,
        run_history_store=store,
    )

    class RecoveringDevice:
        reads = 0

        @classmethod
        def get_variables(cls, _names, use_cache=False):
            assert use_cache is False
            cls.reads += 1
            if cls.reads == 1:
                return {"ready": {"success": False, "error": "offline"}}
            return {"ready": {"success": True, "value": True}}

        @staticmethod
        def run():
            return {"success": True}

    device = RecoveringDevice()
    manager._run_task_action_node(
        WorkflowNode(
            uuid="opc-recovery-node",
            name="run",
            device_name="device",
            method="run",
            param={},
            legacy_route_compatible=False,
        ),
        {"device": device},
        device.run,
        {
            "workflow_path": "main-process.json",
            "instance_id": "instance-opc-recovery",
            "node_id": "opc-recovery-node",
            "execution_id": "execution-opc-recovery",
            "device_id": "device",
            "action_name": "run",
        },
    )

    entries = manager.list_task_action_logs(
        workflow_path="main-process.json"
    )["entries"]
    opc_entries = [
        entry
        for entry in entries
        if entry["code"]
        in {"opc_snapshot_read_failed", "opc_snapshot_recovered"}
    ]
    assert [entry["code"] for entry in opc_entries] == [
        "opc_snapshot_read_failed",
        "opc_snapshot_recovered",
    ]
    assert [entry["level"] for entry in opc_entries] == ["error", "info"]
    assert opc_entries[1]["phase"] == "sampling_after"
    assert opc_entries[1]["detail"]["recovered_failures"][0]["name"] == "ready"
    manager.shutdown()


def test_task_opc_poll_error_is_deduplicated_until_recovery(tmp_path):
    store = RunHistoryStore(tmp_path, session_id="run-opc-poll-error")
    preset = load_preset("stack_s05_s06")
    manager = WorkflowRunManager(
        preset,
        _load_preset_runtime_config(preset),
        run_history_store=store,
    )

    class FakePLC:
        client = object()
        failed = True

        @staticmethod
        def registered_variables():
            return ["ready"]

        def get_variables(self, names, use_cache=False):
            assert names == ["ready"]
            assert use_cache is False
            if self.failed:
                return {"ready": {"success": False, "error": "offline"}}
            return {"ready": {"success": True, "value": True}}

    class FakePublisher:
        @staticmethod
        def get_workspace(**_kwargs):
            return {
                "version": 7,
                "workspace": {
                    "templates": [
                        {
                            "id": "template-1",
                            "input_triggers": [
                                {
                                    "kind": "opc",
                                    "config": {
                                        "plc_device_id": "szlab_poly_plc",
                                        "variable": "ready",
                                        "value": True,
                                    },
                                }
                            ],
                            "output_triggers": [],
                        }
                    ],
                    "task_instances": [
                        {"template_id": "template-1", "status": "waiting"}
                    ],
                },
            }

        @staticmethod
        def publish_snapshot(**_kwargs):
            return None

    plc = FakePLC()
    manager._cached_devices = {"szlab_poly_plc": plc}
    manager._task_snapshot_publisher = FakePublisher()

    first = manager.poll_task_opc(workflow_path="main-process.json")
    second = manager.poll_task_opc(workflow_path="main-process.json")
    plc.failed = False
    recovered = manager.poll_task_opc(workflow_path="main-process.json")
    plc.failed = True
    repeated = manager.poll_task_opc(workflow_path="main-process.json")

    assert first == second == repeated == {
        "success": False,
        "active": True,
        "message": "当前 Task 条件变量均无法读取",
    }
    assert recovered == {
        "success": True,
        "active": True,
        "variable_count": 1,
    }
    entries = manager.list_task_action_logs(
        workflow_path="main-process.json"
    )["entries"]
    assert len(entries) == 3
    assert [entry["code"] for entry in entries] == [
        "opc_input_snapshot_failed",
        "opc_error_recovered",
        "opc_input_snapshot_failed",
    ]
    assert all(entry["category"] == "opc" for entry in entries)
    assert [entry["level"] for entry in entries] == ["error", "info", "error"]
    assert entries[1]["phase"] == "recovered"
    assert entries[1]["detail"]["operation"] == "poll"
    assert entries[1]["detail"]["original_code"] == (
        "opc_input_snapshot_failed"
    )
    manager.shutdown()
