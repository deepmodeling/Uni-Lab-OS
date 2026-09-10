"""动作物料锁（Action Material Lock）与报错异常决策的调度联动测试。

覆盖：
- 动作 Schema 声明的物料被在执行作业（Job）占用 → 后续节点跨设备也串行
- 实体型物料需求的 ``instance_uuid`` 自动并入锁键
- suc_type=skip（异常后人工跳过）→ 节点算成功推进，但已消费物料 quarantined
- JobExecutionBackend 解析 return_info.suc_type 并 4 参回调（兼容旧 3 参 listener）
- 本地异常决策通道：publish_job_error_decision_required 暂存 →
  list/resolve 经 REST 路由回设备节点
"""

from typing import Any, Dict, List, Optional

import pytest

from unilabos.app.scheduler.backend import (
    JobExecutionBackend,
    make_device_material_lock_resolver,
)
from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.inventory.domain import MaterialRequirement
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.store import InventoryStore
from unilabos.app.scheduler.models import WorkflowEdge, WorkflowNode, WorkflowSpec
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.app.ws_client import QueueItem
from unilabos.registry.material_lock_schema import (
    MaterialLockSchemaError,
    compile_material_lock_schema,
)
from unilabos.utils.type_check import serialize_result_info

_MATERIAL_UUID_A = "11111111-1111-1111-1111-111111111111"
_MATERIAL_UUID_B = "22222222-2222-2222-2222-222222222222"


def _node(node_id, device="dev1", action="run", param=None, materials=None):
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


def _compiled_plate_resolver(device_id, action_name, final_param):
    """按测试动作 Schema 校验参数并返回需要锁定的物料 UUID。

    Args:
        device_id: 当前设备身份，本测试只验证解析接口形状。
        action_name: 当前动作名，本测试只验证解析接口形状。
        final_param: 合并完成的动作最终参数。

    Returns:
        规范化、去重和稳定排序的物料 UUID。
    """

    del device_id, action_name
    # ``compiled`` 表示设备动作目录中已固化的 Goal 参数合同。
    compiled = compile_material_lock_schema(
        {
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
    )
    return compiled.material_lock_uuids(final_param)


class TestSchemaMaterialLockSerializesAcrossDevices:
    def _make(self):
        """装配使用真实 Schema 提取接缝的本地调度器。"""

        dispatcher = RecordingDispatcher()
        # 两台设备的 run 动作共享同一个规范参数合同。
        scheduler = EdgeScheduler(
            dispatcher=dispatcher,
            material_lock_resolver=_compiled_plate_resolver,
        )
        return scheduler, dispatcher

    def test_shared_resource_waits(self):
        """A(dev1) 与 B(dev2) 用同一 plate：设备锁不冲突，物料锁强制串行。"""
        scheduler, dispatcher = self._make()
        spec = WorkflowSpec(
            workflow_id="wf1",
            nodes=[
                _node("A", device="dev1", param={"plate": {"uuid": _MATERIAL_UUID_A}}),
                _node("B", device="dev2", param={"plate": {"uuid": _MATERIAL_UUID_A}}),
            ],
        )
        result = scheduler.submit_workflow(spec)
        assert len(result["dispatched"]) == 1  # 只有一个拿到 rack-1 锁

        snap = scheduler.snapshot()
        (job_id, job), = snap["inflight_jobs"].items()
        assert job["resource_locks"] == [
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
                _node("A", device="dev1", param={"plate": {"uuid": _MATERIAL_UUID_A}}),
                _node("B", device="dev2", param={"plate": {"uuid": _MATERIAL_UUID_B}}),
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
            nodes=[_node("A", param={"plate": {}})],
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
        assert job["resource_locks"] == []


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


class _FakeHost:
    """最小 host：send_goal 按预设 suc_type 立即回报。"""

    def __init__(self, suc_type="normal"):
        self.suc_type = suc_type
        self.backend: Optional[JobExecutionBackend] = None
        self.devices_instances: Dict[str, Any] = {}

    def send_goal(self, item: QueueItem, action_type, action_kwargs,
                  sample_material, server_info=None):
        assert self.backend is not None
        self.backend.publish_job_status(
            {}, item, "success",
            serialize_result_info("", True, {"v": 1}, suc_type=self.suc_type),
        )


class TestBackendSucTypePropagation:
    def _run_one(self, listener, suc_type="skip"):
        host = _FakeHost(suc_type=suc_type)
        backend = JobExecutionBackend(host_node_getter=lambda: host)
        host.backend = backend
        backend.start()
        backend.add_job_finished_listener(listener)
        backend.dispatch({
            "job_id": "job-1", "task_id": "t", "device_id": "dev1",
            "action": "run", "action_type": "goal", "action_args": {},
        })
        assert backend.wait_idle(timeout=5)
        backend.stop()

    def test_four_arg_listener_gets_suc_type(self):
        received: List[tuple] = []
        self._run_one(lambda job_id, success, ret, suc_type: received.append(
            (job_id, success, ret, suc_type)))
        assert received == [("job-1", True, {"v": 1}, "skip")]

    def test_three_arg_listener_still_works(self):
        received: List[tuple] = []

        def legacy(job_id, success, ret):
            received.append((job_id, success, ret))

        self._run_one(legacy, suc_type="normal")
        assert received == [("job-1", True, {"v": 1})]


class _FakeBaseNode:
    def __init__(self):
        self.decisions: List[Dict[str, Any]] = []

    def handle_action_error_decision(self, decision_id, job_id, decision):
        self.decisions.append(decision)
        return True


class _Wrapper:
    def __init__(self, node):
        self._ros_node = node


class TestLocalErrorDecisionChannel:
    def _make(self):
        host = _FakeHost()
        base_node = _FakeBaseNode()
        host.devices_instances = {"dev1": _Wrapper(base_node)}
        backend = JobExecutionBackend(host_node_getter=lambda: host)
        host.backend = backend
        return backend, base_node

    def _report(self, decision_id="d-1"):
        return {
            "decision_id": decision_id,
            "device_id": "dev1",
            "action_name": "move",
            "job_id": "job-9",
            "exception_type": "CommunicationError",
            "error_message": "port closed",
            "options": [{"action": "retry", "label": "重试"},
                        {"action": "skip", "label": "跳过"}],
        }

    def test_store_list_resolve(self):
        backend, base_node = self._make()
        assert backend.publish_job_error_decision_required(self._report()) is True
        decisions = backend.list_error_decisions()
        assert len(decisions) == 1
        assert decisions[0]["decision_id"] == "d-1"
        assert "_received_at" not in decisions[0]

        assert backend.resolve_error_decision("d-1", {"action": "retry"}) is True
        assert base_node.decisions[0]["action"] == "retry"
        assert base_node.decisions[0]["job_id"] == "job-9"
        assert backend.list_error_decisions() == []

    def test_resolve_unknown_decision(self):
        backend, _ = self._make()
        assert backend.resolve_error_decision("nope", {"action": "skip"}) is False

    def test_missing_decision_id_rejected(self):
        backend, _ = self._make()
        assert backend.publish_job_error_decision_required({"device_id": "dev1"}) is False

    def test_device_gone_keeps_report(self):
        """设备暂不可用时审批结果不丢：报告放回，等重试。"""
        host = _FakeHost()  # 没有 devices_instances["dev1"]
        backend = JobExecutionBackend(host_node_getter=lambda: host)
        backend.publish_job_error_decision_required(self._report())
        assert backend.resolve_error_decision("d-1", {"action": "retry"}) is False
        assert len(backend.list_error_decisions()) == 1


class TestErrorDecisionRest:
    def test_rest_roundtrip(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from unilabos.app.scheduler.api import create_scheduler_router

        backend, base_node = TestLocalErrorDecisionChannel()._make()
        backend.publish_job_error_decision_required(
            TestLocalErrorDecisionChannel()._report("d-rest"))

        app = FastAPI()
        app.include_router(
            create_scheduler_router(lambda: None, get_backend=lambda: backend))
        client = TestClient(app)

        resp = client.get("/api/v1/error-decisions")
        assert resp.status_code == 200
        assert resp.json()["decisions"][0]["decision_id"] == "d-rest"

        resp = client.post("/api/v1/error-decisions/d-rest", json={"action": "skip"})
        assert resp.status_code == 200
        assert base_node.decisions[0]["action"] == "skip"

        resp = client.post("/api/v1/error-decisions/d-rest", json={"action": "skip"})
        assert resp.status_code == 404

    def test_backend_absent_503(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from unilabos.app.scheduler.api import create_scheduler_router

        app = FastAPI()
        app.include_router(create_scheduler_router(lambda: None))
        client = TestClient(app)
        assert client.get("/api/v1/error-decisions").status_code == 503


class TestDecisionChannelFallback:
    """base_device_node._publish_error_decision_report：云端 WS 失败 → bridges 回退。"""

    class _Stub:
        def __init__(self, client=None):
            self._client = client

        def _get_communication_client(self):
            return self._client

        def lab_logger(self):
            import logging

            return logging.getLogger("stub-device")

    def _publish(self, stub, report):
        from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode

        return BaseROS2DeviceNode._publish_error_decision_report(stub, report)

    def test_falls_back_to_backend_bridge(self, monkeypatch):
        from unilabos.ros.nodes.presets.host_node import HostNode

        backend = JobExecutionBackend(host_node_getter=lambda: None)

        class _HostWithBridges:
            bridges = [object(), backend]  # 第一个 bridge 没有决策接口 → 跳过

        monkeypatch.setattr(
            HostNode, "get_instance", classmethod(lambda cls, idx=0: _HostWithBridges())
        )
        stub = self._Stub(client=None)  # 云端通道不可用
        assert self._publish(stub, {"decision_id": "d-fb", "device_id": "dev1"}) is True
        assert backend.list_error_decisions()[0]["decision_id"] == "d-fb"

    def test_cloud_client_wins_when_available(self, monkeypatch):
        from unilabos.ros.nodes.presets.host_node import HostNode

        sent: List[Dict[str, Any]] = []

        class _CloudClient:
            def publish_job_error_decision_required(self, report):
                sent.append(report)
                return True

        backend = JobExecutionBackend(host_node_getter=lambda: None)

        class _HostWithBridges:
            bridges = [backend]

        monkeypatch.setattr(
            HostNode, "get_instance", classmethod(lambda cls, idx=0: _HostWithBridges())
        )
        stub = self._Stub(client=_CloudClient())
        assert self._publish(stub, {"decision_id": "d-cloud", "device_id": "dev1"}) is True
        assert len(sent) == 1
        assert backend.list_error_decisions() == []  # 云端成功，不落本地

    def test_all_channels_unavailable(self, monkeypatch):
        from unilabos.ros.nodes.presets.host_node import HostNode

        monkeypatch.setattr(
            HostNode, "get_instance", classmethod(lambda cls, idx=0: None)
        )
        stub = self._Stub(client=None)
        assert self._publish(stub, {"decision_id": "d-none", "device_id": "dev1"}) is False


def _typed_action_mapping(*material_fields: str) -> Dict[str, Any]:
    """构造带动作物料锁标记的规范注册表条目。

    Args:
        material_fields: 最终参数中代表实际物料引用的字段名。

    Returns:
        可由本地动作物料锁解析器消费的注册表动作条目。
    """

    # ``goal_properties`` 表示动作最终参数中需要独占的全部物料字段。
    goal_properties = {
        field_name: {
            "type": "object",
            "x-unilabos-material-lock": True,
            "properties": {
                "uuid": {"type": "string", "format": "uuid"},
            },
            "required": ["uuid"],
            "additionalProperties": False,
        }
        for field_name in material_fields
    }
    return {
        "contract_kind": "typed",
        "schema": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "object",
                    "properties": goal_properties,
                    "additionalProperties": False,
                },
                "feedback": {},
                "result": {},
            },
            "required": ["goal"],
        },
    }


class TestMaterialLockResolverFactory:
    def test_host_replica_covers_slave_devices(self):
        """Host._action_value_mappings 是权威副本：本地设备装配时写入，
        slave 设备经 registry_config 上报写入 —— 两类设备同一查找路径。"""

        class _Host:
            # dev-slave 不在 devices_instances（跑在 slave 机器上），
            # 但注册表副本里有它的 action mappings
            _action_value_mappings = {
                "dev-slave": {
                    "run": _typed_action_mapping("plate", "tips"),
                    "auto-move": _typed_action_mapping("arm"),
                },
            }
            devices_instances: Dict[str, Any] = {}

        resolver = make_device_material_lock_resolver(lambda: _Host())
        assert resolver(
            "dev-slave",
            "run",
            {
                "plate": {"uuid": _MATERIAL_UUID_B},
                "tips": {"uuid": _MATERIAL_UUID_A},
            },
        ) == (_MATERIAL_UUID_A, _MATERIAL_UUID_B)
        assert resolver(
            "dev-slave",
            "move",
            {"arm": {"uuid": _MATERIAL_UUID_A}},
        ) == (_MATERIAL_UUID_A,)  # ``auto-`` 前缀回退
        with pytest.raises(MaterialLockSchemaError) as unknown_action:
            resolver("dev-slave", "unknown", {})
        assert unknown_action.value.code == "material_lock_schema_missing"

    def test_local_instance_fallback(self):
        """Host 副本尚未写入时回退本地设备实例的 mappings。"""

        class _Node:
            """模拟本地设备节点持有的动作目录副本。"""

            _action_value_mappings = {"run": _typed_action_mapping("rack")}

        class _Host:
            """模拟 Host 副本尚未接收设备动作目录的启动窗口。"""

            _action_value_mappings: Dict[str, Any] = {}
            devices_instances = {"dev-local": _Wrapper(_Node())}

        resolver = make_device_material_lock_resolver(lambda: _Host())
        assert resolver(
            "dev-local",
            "run",
            {"rack": {"uuid": _MATERIAL_UUID_A}},
        ) == (_MATERIAL_UUID_A,)

    def test_host_replica_wins_over_instance(self):
        """Host 注册表副本存在时必须覆盖本地设备实例中的陈旧 Schema。"""

        class _Node:
            """模拟仍持有陈旧动作字段的本地设备节点。"""

            _action_value_mappings = {"run": _typed_action_mapping("stale")}

        class _Host:
            """模拟已接收最新设备动作目录的 HostNode。"""

            _action_value_mappings = {
                "dev1": {"run": _typed_action_mapping("fresh")},
            }
            devices_instances = {"dev1": _Wrapper(_Node())}

        resolver = make_device_material_lock_resolver(lambda: _Host())
        assert resolver(
            "dev1",
            "run",
            {"fresh": {"uuid": _MATERIAL_UUID_B}},
        ) == (_MATERIAL_UUID_B,)
