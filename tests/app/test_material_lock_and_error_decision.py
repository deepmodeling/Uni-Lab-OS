"""动作物料锁（Action Material Lock）与报错异常决策的调度联动测试。

覆盖：
- 动作 Schema 声明的物料被在执行作业（Job）占用 → 后续节点跨设备也串行
- 实体型物料需求的 ``instance_uuid`` 自动并入锁键
- suc_type=skip（异常后人工跳过）→ 节点算成功推进，但已消费物料 quarantined
- 设备异常经 Edge 控制客户端提交内置后端
"""

from typing import Any

from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.inventory.domain import MaterialRequirement
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.scheduler.models import WorkflowEdge, WorkflowNode, WorkflowSpec
from unilabos.app.scheduler.service import EdgeScheduler

_MATERIAL_UUID_A = "11111111-1111-1111-1111-111111111111"
_MATERIAL_UUID_B = "22222222-2222-2222-2222-222222222222"


def _node(
    node_id,
    device="dev1",
    action="run",
    param=None,
    materials=None,
    param_schema=None,
):
    """构造本地调度测试使用的工作流节点。

    Args:
        node_id: 工作流节点的稳定测试身份。
        device: 执行动作的设备身份。
        action: 注册表中的公开动作名。
        param: 合并前的静态动作参数。
        materials: 节点声明的物料需求。

    Returns:
        可提交给本地调度器（Local Scheduler）的工作流节点。
    """

    return WorkflowNode(
        id=node_id, device_id=device, action_name=action, action_type="goal",
        param=param or {}, material_requirements=materials or [],
        param_schema=param_schema,
    )


def _edge(src, dst):
    """构造两个工作流节点之间的顺序依赖边。

    Args:
        src: 上游节点身份。
        dst: 下游节点身份。

    Returns:
        不传递参数的工作流依赖边。
    """

    return WorkflowEdge(uuid=f"{src}->{dst}", source_node_id=src, target_node_id=dst)


def _plate_action_schema() -> dict[str, Any]:
    """返回任务创建时冻结的 plate 物料锁动作合同。"""

    return {
        "type": "object",
        "properties": {
            "goal": {
                "type": "object",
                "properties": {
                    "plate": {
                        "type": "object",
                        "x-unilabos-material-lock": True,
                        "properties": {
                            "uuid": {"type": "string", "format": "uuid"},
                        },
                        "required": ["uuid"],
                        "additionalProperties": False,
                    },
                },
                "required": ["plate"],
                "additionalProperties": False,
            },
        },
    }


class TestSchemaMaterialLockSerializesAcrossDevices:
    def _make(self):
        """装配使用真实 Schema 提取接缝的本地调度器。"""

        dispatcher = RecordingDispatcher()
        scheduler = EdgeScheduler(dispatcher=dispatcher)
        return scheduler, dispatcher

    def test_shared_resource_waits(self):
        """A(dev1) 与 B(dev2) 用同一 plate：设备锁不冲突，物料锁强制串行。"""
        scheduler, dispatcher = self._make()
        spec = WorkflowSpec(
            workflow_id="wf1",
            nodes=[
                _node("A", device="dev1", param={"plate": {"uuid": _MATERIAL_UUID_A}}, param_schema=_plate_action_schema()),
                _node("B", device="dev2", param={"plate": {"uuid": _MATERIAL_UUID_A}}, param_schema=_plate_action_schema()),
            ],
        )
        result = scheduler.submit_workflow(spec)
        assert len(result["dispatched"]) == 1  # 只有一个拿到 rack-1 锁

        snap = scheduler.snapshot()
        (job_id, job), = snap["inflight_jobs"].items()
        assert job["resource_locks"] == [
            "/devices/dev1",
            f"material/{_MATERIAL_UUID_A}/exclusive"
        ]

        # 持锁 job 完成 → 锁释放 → 另一节点下发
        r2 = scheduler.on_job_finished(job_id, success=True)
        assert len(r2["dispatched"]) == 1
        assert {d["node_id"] for d in dispatcher.dispatched} == {"A", "B"}

    def test_disjoint_resources_parallel(self):
        """不同物料 UUID 在不同设备上应允许并行执行。"""

        scheduler, _ = self._make()
        spec = WorkflowSpec(
            workflow_id="wf1",
            nodes=[
                _node("A", device="dev1", param={"plate": {"uuid": _MATERIAL_UUID_A}}, param_schema=_plate_action_schema()),
                _node("B", device="dev2", param={"plate": {"uuid": _MATERIAL_UUID_B}}, param_schema=_plate_action_schema()),
            ],
        )
        result = scheduler.submit_workflow(spec)
        assert len(result["dispatched"]) == 2  # 不同资源，跨设备并行

    def test_resolver_absent_no_lock(self):
        """隔离测试未注入注册表解析器时，不凭参数形状猜测物料身份。"""

        dispatcher = RecordingDispatcher()
        scheduler = EdgeScheduler(dispatcher=dispatcher)  # 未注入 resolver
        spec = WorkflowSpec(
            workflow_id="wf1",
            nodes=[
                _node("A", device="dev1", param={"plate": {"uuid": _MATERIAL_UUID_A}}),
                _node("B", device="dev2", param={"plate": {"uuid": _MATERIAL_UUID_A}}),
            ],
        )
        assert len(scheduler.submit_workflow(spec)["dispatched"]) == 2

    def test_invalid_final_param_fails_closed_without_dispatch(self):
        """最终参数不满足动作 Schema 时，工作流失败且不得下发设备命令。"""

        scheduler, dispatcher = self._make()
        spec = WorkflowSpec(
            workflow_id="wf-invalid-param",
            nodes=[
                _node(
                    "A",
                    param={"plate": {}},
                    param_schema=_plate_action_schema(),
                )
            ],
        )

        result = scheduler.submit_workflow(spec)

        assert result["dispatched"] == []
        assert dispatcher.dispatched == []
        assert scheduler.workflow_snapshot("wf-invalid-param")["state"] == "failed"


class TestInstanceRequirementLock:
    def test_same_instance_serialized_without_resolver(self):
        """实体型物料需求即使没有动作 Schema 解析器也按 UUID 互斥。"""
        dispatcher = RecordingDispatcher()
        scheduler = EdgeScheduler(dispatcher=dispatcher)
        spec = WorkflowSpec(
            workflow_id="wf1",
            nodes=[
                _node("A", device="dev1", materials=[MaterialRequirement(instance_uuid=_MATERIAL_UUID_A)]),
                _node("B", device="dev2", materials=[MaterialRequirement(instance_uuid=_MATERIAL_UUID_A)]),
            ],
        )
        result = scheduler.submit_workflow(spec)
        assert len(result["dispatched"]) == 1
        job_id = result["dispatched"][0]["job_id"]
        r2 = scheduler.on_job_finished(job_id, success=True)
        assert len(r2["dispatched"]) == 1

    def test_barcode_is_not_a_formal_material_lock_identity(self):
        """条码不是正式物料锁身份，不能生成持久锁键。"""

        dispatcher = RecordingDispatcher()
        scheduler = EdgeScheduler(dispatcher=dispatcher)
        spec = WorkflowSpec(
            workflow_id="wf1",
            nodes=[_node("A", materials=[MaterialRequirement(barcode="BC-7")])],
        )
        scheduler.submit_workflow(spec)
        snap = scheduler.snapshot()
        (job,) = snap["inflight_jobs"].values()
        assert job["resource_locks"] == ["/devices/dev1"]


class TestSkipQuarantinesMaterials:
    def test_skip_marks_success_but_quarantines(self):
        """异常后人工 skip：节点成功推进，其已消费物料隔离待复核。"""
        svc = InventoryService(InventoryStore(":memory:"))
        svc.inbound_lot("tpl-w", 100.0, lot_id="lot-1")
        dispatcher = RecordingDispatcher()
        scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=svc)
        spec = WorkflowSpec(
            workflow_id="wf1",
            nodes=[
                _node("A", materials=[MaterialRequirement(lot_id="lot-1", quantity=20.0)]),
                _node("B", device="dev2"),
            ],
            edges=[_edge("A", "B")],
        )
        scheduler.submit_workflow(spec)
        job_a = dispatcher.dispatched[0]["job_id"]

        r2 = scheduler.on_job_finished(job_a, success=True, suc_type="skip")
        # 节点按成功推进（B 继续下发），workflow 不失败
        assert [d["node_id"] for d in r2["dispatched"]] == ["B"]
        # 但 A 已消费的物料转 quarantined（人工复核）
        assert svc.store.get_reservation("wf1", "A", 1)["status"] == "quarantined"

    def test_normal_success_untouched(self):
        svc = InventoryService(InventoryStore(":memory:"))
        svc.inbound_lot("tpl-w", 100.0, lot_id="lot-1")
        dispatcher = RecordingDispatcher()
        scheduler = EdgeScheduler(dispatcher=dispatcher, inventory=svc)
        spec = WorkflowSpec(
            workflow_id="wf1",
            nodes=[_node("A", materials=[MaterialRequirement(lot_id="lot-1", quantity=20.0)])],
        )
        scheduler.submit_workflow(spec)
        scheduler.on_job_finished(dispatcher.dispatched[0]["job_id"], success=True)
        assert svc.store.get_reservation("wf1", "A", 1)["status"] == "consumed"


class TestErrorDecisionRest:
    def test_backend_absent_503(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from unilabos.app.scheduler.api import create_scheduler_router

        app = FastAPI()
        app.include_router(create_scheduler_router(lambda: None))
        client = TestClient(app)
        assert client.get("/api/v1/error-decisions").status_code == 503


class TestEdgeDecisionReport:
    """设备异常仅通过 Edge 控制客户端上报。"""

    class _Stub:
        def __init__(self, client=None):
            self._client = client

        def _get_communication_client(self):
            return self._client

        def lab_logger(self):
            import logging
            return logging.getLogger("stub-device")

    def _publish(self, client, report):
        from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode
        return BaseROS2DeviceNode._publish_error_decision_report(self._Stub(client), report)

    def test_edge_client_receives_report(self):
        sent = []

        class Client:
            def publish_job_error_decision_required(self, report):
                sent.append(report)
                return True

        report = {"decision_id": "d-edge", "device_id": "dev1"}
        assert self._publish(Client(), report) is True
        assert sent == [report]

    def test_unavailable_client_does_not_accept_report(self):
        assert self._publish(None, {"decision_id": "d-edge"}) is False

    def test_failed_client_does_not_accept_report(self):
        class Client:
            def publish_job_error_decision_required(self, report):
                raise ConnectionError("后端不可达")

        assert self._publish(Client(), {"decision_id": "d-edge"}) is False
