"""时长预估（两种计算模式）与泳道图时间线测试。

- declared：gjson 语义从参数取声明时长（含 sjson 覆写后的父节点传参）
- historical：实际执行时长 EMA，样本随 job 完成积累
- auto：历史优先，无样本回退声明
- timeline：running/completed 起止 + 预估来源，供前端泳道图渲染
"""

import time

import pytest

from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.estimation import (
    SOURCE_DECLARED,
    SOURCE_DEFAULT,
    SOURCE_HISTORICAL,
    DurationEstimator,
)
from unilabos.app.scheduler.models import (
    Handle,
    WorkflowEdge,
    WorkflowNode,
    WorkflowSpec,
)
from unilabos.app.scheduler.service import EdgeScheduler


KEY = "/devices/dev1/run"


class TestDeclaredMode:
    def test_reads_time_field_via_gjson(self):
        est = DurationEstimator(mode="declared")
        seconds, source = est.estimate(KEY, {"time": 12.5})
        assert (seconds, source) == (12.5, SOURCE_DECLARED)

    def test_path_priority_estimated_duration_first(self):
        est = DurationEstimator(mode="declared")
        seconds, _ = est.estimate(KEY, {"estimated_duration_s": 30, "time": 5})
        assert seconds == 30

    def test_nested_path_support(self):
        est = DurationEstimator(
            mode="declared", declared_paths=("config.timeout", "time")
        )
        seconds, source = est.estimate(KEY, {"config": {"timeout": 7}})
        assert (seconds, source) == (7.0, SOURCE_DECLARED)

    def test_missing_falls_back_to_default(self):
        est = DurationEstimator(mode="declared", default_s=42.0)
        seconds, source = est.estimate(KEY, {"volume": 10})
        assert (seconds, source) == (42.0, SOURCE_DEFAULT)

    def test_static_defaults_table(self):
        est = DurationEstimator(mode="declared", static_defaults={KEY: 90.0})
        seconds, source = est.estimate(KEY, {})
        assert (seconds, source) == (90.0, SOURCE_DECLARED)

    def test_non_numeric_declared_ignored(self):
        est = DurationEstimator(mode="declared", default_s=10.0)
        seconds, source = est.estimate(KEY, {"time": "abc"})
        assert (seconds, source) == (10.0, SOURCE_DEFAULT)


class TestHistoricalMode:
    def test_ema_follows_observations(self):
        est = DurationEstimator(mode="historical", ema_alpha=0.5)
        est.observe(KEY, 10.0)
        est.observe(KEY, 20.0)
        seconds, source = est.estimate(KEY, {})
        assert source == SOURCE_HISTORICAL
        assert seconds == pytest.approx(15.0)  # 0.5*20 + 0.5*10

    def test_no_samples_falls_back_to_declared(self):
        est = DurationEstimator(mode="historical")
        seconds, source = est.estimate(KEY, {"time": 8})
        assert (seconds, source) == (8.0, SOURCE_DECLARED)

    def test_stats_exposed(self):
        est = DurationEstimator(mode="historical")
        est.observe(KEY, 3.0)
        stats = est.stats()
        assert stats[0]["device_action_key"] == KEY
        assert stats[0]["samples"] == 1


class TestAutoMode:
    def test_declared_before_samples_then_historical(self):
        est = DurationEstimator(mode="auto")
        seconds, source = est.estimate(KEY, {"time": 5})
        assert (seconds, source) == (5.0, SOURCE_DECLARED)
        est.observe(KEY, 11.0)
        seconds, source = est.estimate(KEY, {"time": 5})
        assert (seconds, source) == (11.0, SOURCE_HISTORICAL)

    def test_unknown_mode_rejected(self):
        with pytest.raises(ValueError):
            DurationEstimator(mode="bogus")


def _spec(workflow_id: str = "wf1", param=None) -> WorkflowSpec:
    return WorkflowSpec(
        workflow_id=workflow_id,
        nodes=[
            WorkflowNode(
                id="A", device_id="dev1", action_name="run",
                action_type="goal", param=dict(param or {}),
            )
        ],
    )


class TestSchedulerTimeline:
    def test_dispatch_carries_estimate(self):
        scheduler = EdgeScheduler(
            dispatcher=RecordingDispatcher(),
            estimator=DurationEstimator(mode="declared"),
        )
        result = scheduler.submit_workflow(_spec(param={"time": 25}))
        job = result["dispatched"][0]
        assert job["estimated_s"] == 25.0
        assert job["estimate_source"] == SOURCE_DECLARED
        # snapshot 的 inflight 也带泳道字段
        snap = scheduler.snapshot()
        inflight = next(iter(snap["inflight_jobs"].values()))
        assert inflight["estimated_s"] == 25.0
        assert inflight["started_at"] > 0

    def test_estimate_uses_resolved_params_from_parent(self):
        """声明式预估读的是 sjson 覆写后的参数：父节点把 time 传给子节点。"""
        scheduler = EdgeScheduler(
            dispatcher=RecordingDispatcher(),
            estimator=DurationEstimator(mode="declared"),
        )
        sh = Handle(uuid="sh", data_source="executor", handle_key="out", data_key="wait_s")
        th = Handle(uuid="th", data_source="handle", handle_key="in", data_key="time")
        spec = WorkflowSpec(
            workflow_id="wf-passing",
            nodes=[
                WorkflowNode(id="A", device_id="dev1", action_name="prepare",
                             action_type="goal", param={}),
                WorkflowNode(id="B", device_id="dev2", action_name="wait",
                             action_type="goal", param={"time": 1}),
            ],
            edges=[
                WorkflowEdge(uuid="e", source_node_id="A", target_node_id="B",
                             source_handle_uuid="sh", target_handle_uuid="th"),
            ],
            handles=[sh, th],
        )
        result = scheduler.submit_workflow(spec)
        job_a = result["dispatched"][0]["job_id"]
        # A 返回 wait_s=300 → gjson 取出经 sjson 写进 B.param.time
        finish = scheduler.on_job_finished(job_a, True, ret_value={"wait_s": 300})
        job_b = finish["dispatched"][0]
        assert job_b["estimated_s"] == 300.0
        assert job_b["estimate_source"] == SOURCE_DECLARED

    def test_timeline_records_actual_and_feeds_history(self):
        est = DurationEstimator(mode="auto", default_s=60.0)
        scheduler = EdgeScheduler(dispatcher=RecordingDispatcher(), estimator=est)
        result = scheduler.submit_workflow(_spec("wf-t"))
        job_id = result["dispatched"][0]["job_id"]
        scheduler.on_job_finished(job_id, True)

        tl = scheduler.timeline()
        assert tl["running"] == []
        assert len(tl["completed"]) == 1
        entry = tl["completed"][0]
        assert entry["state"] == "success"
        assert entry["ended_at"] >= entry["started_at"]
        assert entry["actual_s"] >= 0
        # 完成样本进了历史统计
        assert tl["estimator"]["stats"][0]["samples"] == 1

    def test_failed_and_skip_not_fed_to_history(self):
        est = DurationEstimator(mode="auto")
        scheduler = EdgeScheduler(dispatcher=RecordingDispatcher(), estimator=est)
        r1 = scheduler.submit_workflow(_spec("wf-f"))
        scheduler.on_job_finished(r1["dispatched"][0]["job_id"], False)
        r2 = scheduler.submit_workflow(_spec("wf-s"))
        scheduler.on_job_finished(r2["dispatched"][0]["job_id"], True, suc_type="skip")

        tl = scheduler.timeline()
        states = sorted(e["state"] for e in tl["completed"])
        assert states == ["failed", "success"]
        assert tl["estimator"]["stats"] == []  # 两种都不算真实执行样本

    def test_cancel_records_canceled_entry(self):
        scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
        scheduler.submit_workflow(_spec("wf-c"))
        scheduler.cancel_workflow("wf-c")
        tl = scheduler.timeline()
        assert tl["completed"][0]["state"] == "canceled"
        assert tl["running"] == []

    def test_timeline_window_filters_old_entries(self):
        scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
        result = scheduler.submit_workflow(_spec("wf-w"))
        scheduler.on_job_finished(result["dispatched"][0]["job_id"], True)
        # 把记录改成 2 小时前完结
        scheduler._timeline[0]["ended_at"] = time.time() - 7200
        assert scheduler.timeline(window_s=3600)["completed"] == []
        assert len(scheduler.timeline(window_s=10800)["completed"]) == 1

    def test_running_entry_shape(self):
        scheduler = EdgeScheduler(
            dispatcher=RecordingDispatcher(),
            estimator=DurationEstimator(mode="declared"),
        )
        scheduler.submit_workflow(_spec("wf-r", param={"time": 40}))
        tl = scheduler.timeline()
        assert len(tl["running"]) == 1
        entry = tl["running"][0]
        assert entry["device_id"] == "dev1"
        assert entry["action_name"] == "run"
        assert entry["estimated_s"] == 40.0
        assert entry["elapsed_s"] >= 0
