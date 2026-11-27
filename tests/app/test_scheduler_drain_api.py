"""调度器（Scheduler）排空公开接口的纵向合同测试。"""

from fastapi.testclient import TestClient

from unilabos.app.scheduler.api import create_app
from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.service import EdgeScheduler


def _sequential_workflow() -> dict[str, object]:
    """构造含两个顺序设备作业的最小工作流请求。

    参数：无。返回：可直接提交给调度器公开 HTTP 接口的工作流对象。
    异常：不访问外部状态；测试数据结构变化时由调用断言暴露。
    """

    return {
        "workflow_id": "workflow-drain",
        "nodes": [
            {
                "id": "prepare",
                "device_id": "device-a",
                "action_name": "prepare",
                "action_type": "goal",
            },
            {
                "id": "finish",
                "device_id": "device-b",
                "action_name": "finish",
                "action_type": "goal",
            },
        ],
        "edges": [
            {
                "uuid": "edge-prepare-finish",
                "source_node_id": "prepare",
                "target_node_id": "finish",
            }
        ],
    }


def test_drain_waits_for_active_job_and_blocks_following_dispatch() -> None:
    """排空期间等待当前设备作业，但不得派发工作流的下一节点。

    参数：无。返回：无；通过公开 HTTP 接口断言排空状态与派发边界。
    异常：若排空误停当前作业、误派发下一节点或状态不收敛则测试失败。
    """

    dispatcher = RecordingDispatcher()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    client = TestClient(create_app(scheduler))

    submitted = client.post("/api/v1/workflows", json=_sequential_workflow())
    assert submitted.status_code == 200
    first_job_id = submitted.json()["dispatched"][0]["job_id"]

    draining = client.post("/api/v1/scheduler/drain")
    assert draining.status_code == 200
    assert draining.json()["phase"] == "draining"
    assert draining.json()["active_device_job_ids"] == [first_job_id]

    finished = client.post(
        f"/api/v1/jobs/{first_job_id}/finish",
        json={"success": True},
    )
    assert finished.status_code == 200
    assert finished.json()["dispatched"] == []

    drained = client.get("/api/v1/scheduler/drain")
    assert drained.status_code == 200
    assert drained.json() == {
        "phase": "drained",
        "accepting_new_dispatches": False,
        "active_device_job_count": 0,
        "active_device_job_ids": [],
    }

    resumed = client.post("/api/v1/scheduler/drain/resume")
    assert resumed.status_code == 200
    assert resumed.json()["phase"] == "running"
    assert len(resumed.json()["dispatched"]) == 1
    assert resumed.json()["dispatched"][0]["node_id"] == "finish"


def test_drain_is_idempotent_when_no_device_job_is_active() -> None:
    """没有设备作业时重复排空保持幂等并立即进入已排空状态。

    参数：无。返回：无；通过公开 HTTP 接口断言重复命令的稳定结果。
    异常：若重复命令改变状态或产生在途作业，则测试失败。
    """

    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    client = TestClient(create_app(scheduler))

    first = client.post("/api/v1/scheduler/drain")
    second = client.post("/api/v1/scheduler/drain")
    assert first.json() == second.json()
    assert second.json()["phase"] == "drained"
    assert second.json()["active_device_job_count"] == 0


def test_drain_includes_persisted_running_reconciliation_job() -> None:
    """持久层中等待物理对账的 running 作业必须阻止报告已排空。

    参数：无。返回：无；通过公开 HTTP 接口验证持久安全事实被合并进状态。
    异常：若排空只查看进程内作业并错误报告 drained，则测试失败。
    """

    scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
    scheduler.set_drain_blocker_provider(lambda: {"job-running-reconciliation"})
    client = TestClient(create_app(scheduler))

    response = client.post("/api/v1/scheduler/drain")

    assert response.status_code == 200
    assert response.json()["phase"] == "draining"
    assert response.json()["active_device_job_ids"] == [
        "job-running-reconciliation"
    ]
