"""工作流执行历史（第三个独立 SQLite）测试。

- 提交建档（含整图 spec_json 回放）、状态流转、终态时间
- job 完结 append（实际/预估/来源/suc_type/截断 ret）
- 跨"进程重启"持久 + mark_interrupted 恢复语义
- 同 workflow_id 重提覆盖旧运行
- 总量裁剪（max_runs）
- 调度器挂钩全链路 + REST 面
"""

import pytest

from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.history import WorkflowHistoryStore
from unilabos.app.scheduler.models import (
    WorkflowEdge,
    WorkflowNode,
    WorkflowSpec,
)
from unilabos.app.scheduler.service import EdgeScheduler


def _spec(workflow_id="wf-h", device="dev1"):
    return WorkflowSpec(
        workflow_id=workflow_id,
        nodes=[
            WorkflowNode(
                id="A", device_id=device, action_name="run",
                action_type="goal", param={"time": 5},
            ),
            WorkflowNode(id="B", device_id=device, action_name="run", action_type="goal"),
        ],
        edges=[WorkflowEdge(uuid="e", source_node_id="A", target_node_id="B")],
    )


def _scheduler(history):
    return EdgeScheduler(dispatcher=RecordingDispatcher(), history=history)


class TestSchedulerHooks:
    def test_full_lifecycle_recorded(self):
        history = WorkflowHistoryStore()
        scheduler = _scheduler(history)
        r = scheduler.submit_workflow(_spec("wf-full"))

        run = history.get_run("wf-full", with_spec=True)
        assert run["state"] == "running"
        assert run["node_count"] == 2
        assert run["finished_at"] is None
        # spec 完整可回放
        assert [n["id"] for n in run["spec"]["nodes"]] == ["A", "B"]
        assert run["spec"]["nodes"][0]["param"] == {"time": 5}

        scheduler.on_job_finished(r["dispatched"][0]["job_id"], True, ret_value={"out": 1})
        job_b = next(iter(scheduler.snapshot()["inflight_jobs"]))
        scheduler.on_job_finished(job_b, True)

        run = history.get_run("wf-full")
        assert run["state"] == "success"
        assert run["finished_at"] is not None
        assert run["duration_s"] >= 0

        jobs = history.list_jobs(workflow_id="wf-full")
        assert len(jobs) == 2
        assert jobs[1]["node_id"] == "A"  # 新→旧
        assert jobs[1]["state"] == "success"
        assert jobs[1]["ret_value"] == {"out": 1}
        assert jobs[1]["actual_s"] >= 0
        assert jobs[1]["estimated_s"] > 0

    def test_failed_and_canceled_states(self):
        history = WorkflowHistoryStore()
        scheduler = _scheduler(history)
        r = scheduler.submit_workflow(_spec("wf-f"))
        scheduler.on_job_finished(r["dispatched"][0]["job_id"], False)
        assert history.get_run("wf-f")["state"] == "failed"

        scheduler.submit_workflow(_spec("wf-c"))
        scheduler.cancel_workflow("wf-c")
        assert history.get_run("wf-c")["state"] == "canceled"
        jobs = history.list_jobs(workflow_id="wf-c")
        assert jobs[0]["state"] == "canceled"

    def test_skip_suc_type_recorded(self):
        history = WorkflowHistoryStore()
        scheduler = _scheduler(history)
        r = scheduler.submit_workflow(_spec("wf-s"))
        scheduler.on_job_finished(r["dispatched"][0]["job_id"], True, suc_type="skip")
        jobs = history.list_jobs(workflow_id="wf-s")
        assert jobs[0]["suc_type"] == "skip"

    def test_history_disabled_is_noop(self):
        scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())  # history=None
        r = scheduler.submit_workflow(_spec("wf-none"))
        scheduler.on_job_finished(r["dispatched"][0]["job_id"], True)  # 不抛


class TestPersistenceAndRecovery:
    def test_survives_restart_and_marks_interrupted(self, tmp_path):
        db = str(tmp_path / "workflow_history.db")
        history = WorkflowHistoryStore(db)
        scheduler = _scheduler(history)
        r = scheduler.submit_workflow(_spec("wf-done"))
        scheduler.on_job_finished(r["dispatched"][0]["job_id"], True)
        job_b = next(iter(scheduler.snapshot()["inflight_jobs"]))
        scheduler.on_job_finished(job_b, True)
        scheduler.submit_workflow(_spec("wf-mid"))  # 执行中不收尾
        history.close()

        # "重启"：重开同一文件
        reopened = WorkflowHistoryStore(db)
        assert reopened.mark_interrupted() == 1  # 只有 wf-mid
        assert reopened.get_run("wf-done")["state"] == "success"
        mid = reopened.get_run("wf-mid")
        assert mid["state"] == "interrupted"
        assert mid["finished_at"] is not None
        # 已完结的 job 记录还在
        assert len(reopened.list_jobs(workflow_id="wf-done")) == 2

    def test_resubmit_same_id_replaces(self):
        history = WorkflowHistoryStore()
        history.record_submitted(_spec("wf-r"), "running")
        history.record_job(
            {
                "job_id": "j-old", "workflow_id": "wf-r", "node_id": "A",
                "started_at": 1.0, "ended_at": 2.0, "state": "success",
            }
        )
        history.record_submitted(_spec("wf-r"), "running")  # 重提
        assert history.list_jobs(workflow_id="wf-r") == []  # 旧 job 清掉
        assert history.stats()["total_runs"] == 1

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "TODO(P0): preserve repeated workflow runs after Cloud/Edge agree "
            "the stable run_id contract and migration"
        ),
    )
    def test_resubmit_same_workflow_should_preserve_prior_run_history(self):
        """Known P0 marker only; do not invent a destructive run_id migration here."""

        history = WorkflowHistoryStore()
        history.record_submitted(_spec("wf-run-contract"), "running")
        history.record_job(
            {
                "job_id": "job-first",
                "workflow_id": "wf-run-contract",
                "node_id": "A",
                "started_at": 1.0,
                "ended_at": 2.0,
                "state": "success",
            }
        )
        history.record_submitted(_spec("wf-run-contract"), "running")
        assert history.list_jobs(workflow_id="wf-run-contract") != []

    def test_prune_keeps_latest(self):
        history = WorkflowHistoryStore(max_runs=3)
        for i in range(6):
            spec = _spec(f"wf-{i}")
            spec.submitted_at = 1000.0 + i
            history.record_submitted(spec, "running")
        runs = history.list_runs(limit=100)
        assert [r["workflow_id"] for r in runs] == ["wf-5", "wf-4", "wf-3"]

    def test_ret_value_truncated(self):
        history = WorkflowHistoryStore()
        history.record_submitted(_spec("wf-big"), "running")
        history.record_job(
            {
                "job_id": "j1", "workflow_id": "wf-big", "node_id": "A",
                "started_at": 1.0, "ended_at": 2.0, "state": "success",
            },
            ret_value={"blob": "x" * 10000},
        )
        job = history.list_jobs(workflow_id="wf-big")[0]
        assert len(str(job["ret_value"])) <= 4200  # 截断后（含 JSON 包装）

    def test_list_filters(self):
        history = WorkflowHistoryStore()
        history.record_submitted(_spec("wf-a", device="dev1"), "running")
        history.record_state("wf-a", "success")
        history.record_submitted(_spec("wf-b", device="dev2"), "running")
        assert [r["workflow_id"] for r in history.list_runs(state="success")] == ["wf-a"]
        history.record_job(
            {
                "job_id": "j1", "workflow_id": "wf-b", "node_id": "A", "device_id": "dev2",
                "started_at": 1.0, "ended_at": 2.0, "state": "success",
            }
        )
        assert history.list_jobs(device_id="dev2")[0]["job_id"] == "j1"
        assert history.list_jobs(device_id="dev1") == []


class TestHistoryApi:
    @pytest.fixture()
    def client(self):
        fastapi = pytest.importorskip("fastapi")  # noqa: F841
        from fastapi.testclient import TestClient

        from unilabos.app.scheduler.api import create_app

        history = WorkflowHistoryStore()
        scheduler = EdgeScheduler(dispatcher=RecordingDispatcher(), history=history)
        return TestClient(create_app(scheduler, history=history))

    def _run_one(self, client, workflow_id="wf-api"):
        body = {
            "workflow_id": workflow_id,
            "nodes": [{"id": "A", "device_id": "d1", "action_name": "run", "action_type": "goal"}],
        }
        r = client.post("/api/v1/workflows", json=body)
        job_id = r.json()["dispatched"][0]["job_id"]
        client.post(f"/api/v1/jobs/{job_id}/finish", json={"success": True, "ret_value": {"k": 1}})

    def test_list_and_detail(self, client):
        self._run_one(client)
        r = client.get("/api/v1/history/workflows").json()
        assert r["stats"]["total_runs"] == 1
        assert r["runs"][0]["state"] == "success"
        assert "spec" not in r["runs"][0]

        entity_rows = client.get(
            "/api/v1/history/workflows?with_spec=true"
        ).json()
        assert entity_rows["runs"][0]["spec"]["nodes"][0]["id"] == "A"

        detail = client.get("/api/v1/history/workflows/wf-api").json()
        assert detail["spec"]["nodes"][0]["id"] == "A"
        assert detail["jobs"][0]["ret_value"] == {"k": 1}

    def test_jobs_filter(self, client):
        self._run_one(client, "wf-j")
        r = client.get("/api/v1/history/jobs?device_id=d1").json()
        assert len(r["jobs"]) == 1
        assert client.get("/api/v1/history/jobs?device_id=nope").json()["jobs"] == []

    def test_404_and_503(self, client):
        assert client.get("/api/v1/history/workflows/nope").status_code == 404
        fastapi = pytest.importorskip("fastapi")  # noqa: F841
        from fastapi.testclient import TestClient

        from unilabos.app.scheduler.api import create_app

        bare = TestClient(create_app())  # history=None 且调度器无 _history
        assert bare.get("/api/v1/history/workflows").status_code == 503


class TestStartedAt:
    """started_at 对齐云端 workflow_task.started_at：首次进入 running 的时间。"""

    def test_running_on_submit_sets_started_at(self):
        history = WorkflowHistoryStore(":memory:")
        spec = _spec("wf-run")
        history.record_submitted(spec, "running")
        run = history.get_run("wf-run")
        assert run["started_at"] == spec.submitted_at

    def test_waiting_then_running_sets_started_at_once(self):
        history = WorkflowHistoryStore(":memory:")
        spec = _spec("wf-wait")
        history.record_submitted(spec, "waiting_for_material")
        assert history.get_run("wf-wait")["started_at"] is None
        history.record_state("wf-wait", "running")
        first = history.get_run("wf-wait")["started_at"]
        assert first is not None
        history.record_state("wf-wait", "running")  # 再次 running 不覆盖
        assert history.get_run("wf-wait")["started_at"] == first
