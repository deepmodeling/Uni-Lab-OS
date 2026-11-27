"""工作流人工干预（Intervention）的持久合同测试。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi.testclient import TestClient

from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore

WORKFLOW_UUID = "10000000-0000-4000-8000-000000000301"
TASK_UUID = "20000000-0000-4000-8000-000000000301"
NODE_UUID = "30000000-0000-4000-8000-000000000301"
JOB_UUID = "40000000-0000-4000-8000-000000000301"
CREATED_AT = "2026-08-26T00:00:00Z"


class _InterventionDelivery:
    """模拟本地设备异常决定端口。"""

    def __init__(self, *, accepted: bool = True) -> None:
        """设置设备是否接受干预决定；参数为接受标记，返回无且不写外部状态。"""

        self.listeners: list[Callable[[dict[str, Any]], None]] = []
        self.delivered: list[tuple[str, dict[str, Any]]] = []
        self.accepted = accepted

    def add_error_decision_required_listener(
        self,
        listener: Callable[[dict[str, Any]], None],
    ) -> None:
        self.listeners.append(listener)

    def publish(self, report: dict[str, Any]) -> None:
        for listener in tuple(self.listeners):
            listener(dict(report))

    def remove_error_decision_required_listener(
        self,
        listener: Callable[[dict[str, Any]], None],
    ) -> None:
        """移除异常决定监听器；参数为既有回调，返回无且重复移除幂等。"""

        self.listeners = [item for item in self.listeners if item != listener]

    def resolve_error_decision(
        self,
        decision_id: str,
        decision: dict[str, Any],
    ) -> bool:
        """记录人工决定并返回设备接受结果；参数为决定身份与载荷。"""

        self.delivered.append((decision_id, dict(decision)))
        return self.accepted


def _seed_running_job(store: WorkflowStore) -> None:
    store.create_workflow(
        workflow_uuid=WORKFLOW_UUID,
        name="干预测试",
        tags=[],
        description=None,
        meta_data={},
    )
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO workflow_task(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_uuid, status, workflow_snapshot,
                execution_plan, run_mode, target_node_uuid, control_status,
                cleanup_status, trace_context, input, output, error_info
            ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, 'running', '{}', '{}',
                      'normal', NULL, 'active', 'none', '{}', '{}', '{}', '[]')
            """,
            (TASK_UUID, CREATED_AT, CREATED_AT, WORKFLOW_UUID),
        )
        connection.execute(
            """
            INSERT INTO workflow_node_job(
                uuid, create_time, update_time, deleted_at, description,
                meta_data, workflow_task_uuid, workflow_node_uuid,
                feedback_sequence, topological_index, executor_kind,
                execution_policy, execution_timeout_seconds, status,
                attempt, param, feedback_data, return_info, control_data,
                error_info
            ) VALUES (?, ?, ?, NULL, NULL, '{}', ?, ?, 0, 0,
                      'device_action', '{}', 0, 'dispatched', 1, '{}', '{}',
                      '{}', '{}', '[]')
            """,
            (JOB_UUID, CREATED_AT, CREATED_AT, TASK_UUID, NODE_UUID),
        )


def _report(decision_id: str) -> dict[str, Any]:
    return {
        "decision_id": decision_id,
        "task_id": TASK_UUID,
        "job_id": JOB_UUID,
        "device_id": "reactor-a",
        "action_name": "start",
        "exception_type": "CommunicationError",
        "error_message": "device offline",
        "options": [
            {"action": "retry", "label": "重试"},
            {
                "action": "reset_connection",
                "label": "重置连接",
                "result": {"suc": True, "return_value": {"reset": True}},
            },
        ],
    }


def test_intervention_report_selection_and_replay_use_public_api(tmp_path) -> None:
    """设备报告先持久化，方案选择按修订和幂等键只投递一次。"""

    store = WorkflowStore(tmp_path / "intervention.db")
    try:
        _seed_running_job(store)
        delivery = _InterventionDelivery()
        service = WorkflowService(store)
        service.bind_intervention_delivery(delivery)
        client = TestClient(create_workflow_app(service))

        delivery.publish(_report("50000000-0000-4000-8000-000000000301"))
        opened = client.get(
            "/api/v1/workflow-interventions",
            params={"status": "open", "limit": 10},
        ).json()["data"]
        assert len(opened) == 1
        intervention = opened[0]
        assert intervention["revision"] == 1
        assert [item["id"] for item in intervention["options"]] == [
            "retry",
            "reset_connection",
        ]
        assert store.get_task(TASK_UUID)["control_status"] == "waiting_intervention"

        body = {"revision": 1, "option_id": "reset_connection"}
        first = client.post(
            f"/api/v1/workflow-interventions/{intervention['uuid']}/decisions",
            headers={"Idempotency-Key": "intervention-decision-1"},
            json=body,
        )
        replay = client.post(
            f"/api/v1/workflow-interventions/{intervention['uuid']}/decisions",
            headers={"Idempotency-Key": "intervention-decision-1"},
            json=body,
        )

        assert first.status_code == 201
        assert first.json()["data"]["intervention"]["delivery_status"] == "accepted"
        assert replay.status_code == 200
        assert len(delivery.delivered) == 1
        assert delivery.delivered[0][0] == intervention["edge_command_uuid"]
        assert delivery.delivered[0][1]["action"] == "reset_connection"
        assert store.get_task(TASK_UUID)["control_status"] == "active"
    finally:
        store.close()


def test_new_report_supersedes_previous_revision_and_stale_choice_conflicts(
    tmp_path,
) -> None:
    """同一作业的新异常报告形成新修订，旧修订不能再选择。"""

    store = WorkflowStore(tmp_path / "intervention-revision.db")
    try:
        _seed_running_job(store)
        delivery = _InterventionDelivery()
        service = WorkflowService(store)
        service.bind_intervention_delivery(delivery)
        client = TestClient(create_workflow_app(service))
        delivery.publish(_report("50000000-0000-4000-8000-000000000311"))
        first = service.list_workflow_interventions(status="open", limit=10)[0]
        delivery.publish(_report("50000000-0000-4000-8000-000000000312"))
        second = service.list_workflow_interventions(status="open", limit=10)[0]

        assert second["revision"] == 2
        assert service.get_workflow_intervention(first["uuid"])["status"] == "superseded"
        stale = client.post(
            f"/api/v1/workflow-interventions/{first['uuid']}/decisions",
            headers={"Idempotency-Key": "stale-choice"},
            json={"revision": 1, "option_id": "retry"},
        ).json()
        assert stale["code"] != 0
        assert delivery.delivered == []
    finally:
        store.close()


def test_selected_intervention_replays_persisted_payload_after_service_rebind(
    tmp_path,
) -> None:
    """投递失败后的服务重建必须用同一决定身份和冻结结果安全重投。

    参数：``tmp_path`` 隔离工作流库。返回无；断言第一次拒绝留下 unknown，第二
    个服务绑定可用端口后自动重投用户覆盖结果并恢复 Task，不新增持久表。
    """

    store = WorkflowStore(tmp_path / "intervention-recovery.db")
    try:
        _seed_running_job(store)
        unavailable = _InterventionDelivery(accepted=False)
        first_service = WorkflowService(store)
        first_service.bind_intervention_delivery(unavailable)
        unavailable.publish(_report("50000000-0000-4000-8000-000000000321"))
        intervention = first_service.list_workflow_interventions(
            status="open",
            limit=10,
        )[0]

        response = TestClient(create_workflow_app(first_service)).post(
            f"/api/v1/workflow-interventions/{intervention['uuid']}/decisions",
            headers={"Idempotency-Key": "recover-selected-decision"},
            json={
                "revision": 1,
                "option_id": "reset_connection",
                "result": {"suc": True, "return_value": {"manual": "override"}},
            },
        )
        assert response.status_code == 200
        assert response.json()["code"] != 0
        assert first_service.get_workflow_intervention(intervention["uuid"])[
            "delivery_status"
        ] == "unknown"

        recovered_delivery = _InterventionDelivery()
        recovered_service = WorkflowService(store)
        recovered_service.bind_intervention_delivery(recovered_delivery)
        recovered = recovered_service.get_workflow_intervention(intervention["uuid"])

        assert recovered["delivery_status"] == "accepted"
        assert recovered_delivery.delivered == [
            (
                intervention["edge_command_uuid"],
                {
                    "option": intervention["options"][1],
                    "action": "reset_connection",
                    "job_id": JOB_UUID,
                    "device_id": "reactor-a",
                    "result": {
                        "suc": True,
                        "return_value": {"manual": "override"},
                    },
                    "job_id": JOB_UUID,
                    "device_id": "reactor-a",
                },
            )
        ]
        assert store.get_task(TASK_UUID)["control_status"] == "active"
        table_names = {
            row["name"]
            for row in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "workflow_intervention_delivery" not in table_names
    finally:
        store.close()
