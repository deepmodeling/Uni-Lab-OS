"""云端接口测试：workflow_start / workflow_cancel 消息 + workflow_status 上报。

覆盖 ws_client.MessageProcessor 的整图下发转发与 integration 装配层。
"""

import asyncio
import time
from queue import Queue
from typing import Any, Dict, List

import pytest

from unilabos.app.scheduler import integration
from unilabos.app.scheduler.dispatch import CancelDispatchState, RecordingDispatcher
from unilabos.app.scheduler.service import EdgeScheduler, ExecutionPolicyError
from unilabos.app.ws_client import DeviceActionManager, MessageProcessor


def _make_processor() -> MessageProcessor:
    return MessageProcessor("ws://test", Queue(maxsize=100), DeviceActionManager())


def _workflow_payload(workflow_id: str = "wf-cloud") -> Dict[str, Any]:
    return {
        "workflow_id": workflow_id,
        "task_id": f"task-{workflow_id}",
        "priority": "high",
        "nodes": [
            {
                "id": "A",
                "device_id": "d1",
                "action_name": "run",
                "action_type": "goal",
                "param": {"v": 1},
            },
            {"id": "B", "device_id": "d1", "action_name": "run", "action_type": "goal"},
        ],
        "edges": [{"source_node_id": "A", "target_node_id": "B"}],
        "handles": [],
    }


def _drain(q: Queue) -> List[Dict[str, Any]]:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_send_failure_retains_current_and_remaining_messages_in_pending_buffer():
    """短暂断线时，已取出的异常决策消息必须留在进程内缓冲等待补发。"""

    processor = _make_processor()
    messages = [
        {"action": "job_error_decision_required", "data": {"decision_id": "d-1"}},
        {"action": "job_error_decision_required", "data": {"decision_id": "d-2"}},
    ]
    for message in messages:
        processor.send_queue.put_nowait(message)

    class DisconnectingSocket:
        async def send(self, _message):
            processor.connected = False
            raise ConnectionError("temporary disconnect")

    processor.connected = True
    processor.websocket = DisconnectingSocket()

    asyncio.run(processor._send_handler())

    assert [item["data"]["decision_id"] for item in processor._pending_outbound] == [
        "d-1",
    ]
    assert [item["data"]["decision_id"] for item in _drain(processor.send_queue)] == ["d-2"]


def test_cancelling_send_task_keeps_inflight_message_for_reconnect():
    """取消恰好落在 websocket.send 时，队首消息不能从补发缓冲中消失。"""

    async def run() -> None:
        processor = _make_processor()
        processor.send_queue.put_nowait(
            {"action": "job_error_decision_required", "data": {"decision_id": "d-1"}}
        )
        send_started = asyncio.Event()

        class BlockingSocket:
            async def send(self, _message):
                send_started.set()
                await asyncio.Event().wait()

        processor.connected = True
        processor.websocket = BlockingSocket()
        send_task = asyncio.create_task(processor._send_handler())
        await send_started.wait()
        send_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await send_task

        assert [item["data"]["decision_id"] for item in processor._pending_outbound] == ["d-1"]

    asyncio.run(run())


class TestWorkflowStart:
    def test_forwards_to_edge_scheduler(self):
        mp = _make_processor()
        dispatcher = RecordingDispatcher()
        mp.edge_scheduler = EdgeScheduler(dispatcher=dispatcher)

        asyncio.run(mp._handle_workflow_start(_workflow_payload()))

        assert len(dispatcher.dispatched) == 1
        assert dispatcher.dispatched[0]["node_id"] == "A"
        assert dispatcher.dispatched[0]["task_id"] == "task-wf-cloud"

    def test_duplicate_submit_idempotent(self):
        mp = _make_processor()
        dispatcher = RecordingDispatcher()
        mp.edge_scheduler = EdgeScheduler(dispatcher=dispatcher)

        asyncio.run(mp._handle_workflow_start(_workflow_payload()))
        asyncio.run(mp._handle_workflow_start(_workflow_payload()))
        # 第二次幂等忽略，不重复下发也不回 failed
        assert len(dispatcher.dispatched) == 1
        statuses = [
            m for m in _drain(mp.send_queue) if m.get("action") == "workflow_status"
        ]
        assert statuses == []

    def test_no_scheduler_reports_failed(self):
        mp = _make_processor()
        asyncio.run(mp._handle_workflow_start(_workflow_payload()))
        statuses = [
            m for m in _drain(mp.send_queue) if m.get("action") == "workflow_status"
        ]
        assert len(statuses) == 1
        assert statuses[0]["data"]["status"] == "failed"
        assert "not attached" in statuses[0]["data"]["error"]

    def test_cycle_reports_failed(self):
        mp = _make_processor()
        mp.edge_scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
        payload = _workflow_payload("wf-cycle")
        payload["edges"].append({"source_node_id": "B", "target_node_id": "A"})
        asyncio.run(mp._handle_workflow_start(payload))
        statuses = [
            m for m in _drain(mp.send_queue) if m.get("action") == "workflow_status"
        ]
        assert len(statuses) == 1
        assert statuses[0]["data"]["status"] == "failed"


class TestWorkflowCancel:
    def test_cancel_workflow(self):
        mp = _make_processor()

        class RequestedCancelDispatcher(RecordingDispatcher):
            def __init__(self):
                super().__init__()
                self.cancel_requests: List[str] = []

            def cancel(self, job_id: str, on_accepted) -> CancelDispatchState:
                self.cancel_requests.append(job_id)
                on_accepted(True)
                return CancelDispatchState.REQUESTED

        dispatcher = RequestedCancelDispatcher()
        scheduler = EdgeScheduler(dispatcher=dispatcher)
        mp.edge_scheduler = scheduler

        asyncio.run(mp._handle_workflow_start(_workflow_payload("wf-c")))
        asyncio.run(mp._handle_workflow_cancel({"workflow_id": "wf-c"}))

        snap = scheduler.workflow_snapshot("wf-c")
        assert snap["state"] == "canceling"
        inflight = scheduler.snapshot()["inflight_jobs"]
        assert len(inflight) == 1
        job_uuid = next(iter(inflight))
        assert dispatcher.cancel_requests == [job_uuid]

        # 云端取消消息只请求设备停止；同一本地完成入口收到明确终态后才释放。
        scheduler.on_job_finished(job_uuid, False, None, "canceled")
        assert scheduler.snapshot()["inflight_jobs"] == {}
        assert scheduler.workflow_snapshot("wf-c")["state"] == "canceled"

    def test_cancel_unknown_workflow_no_crash(self):
        mp = _make_processor()
        mp.edge_scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
        asyncio.run(mp._handle_workflow_cancel({"workflow_id": "ghost"}))


class TestIntegrationWiring:
    def setup_method(self):
        integration.reset_for_test()

    def teardown_method(self):
        integration.reset_for_test()

    def test_inventory_starts_without_scheduler(self, tmp_path):
        class FakeWsClient:
            def __init__(self):
                self.message_processor = _make_processor()

        ws = FakeWsClient()
        db_path = tmp_path / "host-material.db"
        inventory = integration.setup_edge_inventory(
            str(db_path),
            ws_client=ws,
        )

        assert integration.get_inventory_service() is inventory
        assert integration.get_edge_scheduler() is None
        assert ws.message_processor.inventory_service is inventory
        assert db_path.exists()
        assert integration.setup_edge_inventory(str(db_path)) is inventory

    def test_inventory_composition_retains_workspace_material_shapes(self, tmp_path):
        """库存组合根必须保留工作区编译后的静态物料外形。

        参数：``tmp_path`` 提供隔离的库存数据库。返回：无；断言 Web 组合可从
        同一进程装配接缝读取外形副本。异常：静态外形丢失或被调用者修改时测试失败。
        """

        # ``material_shapes`` 是包资产编译阶段完成校验的公共只读投影。
        material_shapes = (
            {
                "id": "beaker",
                "bundle": "szlab-poly-studio",
                "categories": ["beaker"],
                "categoryTokens": [],
                "parts": [{"type": "box"}],
            },
        )

        integration.setup_edge_inventory(
            str(tmp_path / "host-material.db"),
            material_shapes=material_shapes,
        )
        first_read = integration.get_material_shapes()
        first_read[0]["id"] = "tampered"

        assert integration.get_material_shapes() == list(material_shapes)

    def test_inventory_composition_retains_public_material_model_catalog(
        self,
        tmp_path,
    ) -> None:
        """库存组合根必须保留 OS 公开物料模型目录的启动代际。

        参数：``tmp_path`` 提供隔离的库存数据库。返回：无；断言 Web
        组合只能取回同一目录对象，不允许在库存启动后换代。异常：目录
        丢失或换代未关闭式失败时测试失败。
        """

        class _ModelCatalog:
            """表示已限定 OS 公开路由的模型目录启动代际。"""

            def __init__(self) -> None:
                """创建单模板快照。参数：无。返回：无。异常：无。"""

                # ``models_by_template`` 只含 OS HTTP 公开路径，不含 local_bridge。
                self.models_by_template = {
                    "m2b_mount": {
                        "path": "/api/v1/material-models/szlab/device.xacro",
                        "format": "xacro",
                    }
                }

        model_catalog = _ModelCatalog()
        integration.setup_edge_inventory(
            str(tmp_path / "host-material.db"),
            material_model_catalog=model_catalog,
        )

        assert integration.get_material_model_catalog() is model_catalog
        with pytest.raises(RuntimeError, match="物料模型目录代际"):
            integration.setup_edge_inventory(
                str(tmp_path / "host-material.db"),
                material_model_catalog=_ModelCatalog(),
            )

    def test_setup_injects_scheduler_and_reports_state(self, tmp_path, monkeypatch):
        """缺少设备动作目录时应在派发前失败，并上报工作流终态。

        Args:
            tmp_path: 隔离设备状态库与工作流历史库的临时目录。
        """

        class FakeWsClient:
            def __init__(self):
                self.message_processor = _make_processor()

        # 本用例验证状态库和派发权威装配；CP-SAT 由 scheduler_core 及真实 Backend 验收。
        # 不让同一测试进程中 OpenCV/ROS 的原生 protobuf 与规划器争用动态库。
        monkeypatch.setenv("UNILABOS_TIME_SCHEDULING_ENABLED", "false")
        ws = FakeWsClient()
        device_state_db = tmp_path / "device-state.db"
        workflow_history_db = tmp_path / "workflow-history.db"
        from unilabos.app.edge_control.local_authority import (
            LocalEdgeAuthorityStore,
            LocalEdgeControlAuthority,
        )

        authority = LocalEdgeControlAuthority(
            LocalEdgeAuthorityStore(tmp_path / "edge-authority.db"), api_key="test"
        )
        scheduler, backend = integration.setup_edge_scheduler(
            ws_client=ws,
            execution_backend=authority,
            device_state_db_path=str(device_state_db),
            workflow_history_db_path=str(workflow_history_db),
        )
        try:
            # 注入成功
            assert ws.message_processor.edge_scheduler is scheduler
            assert integration.get_edge_scheduler() is scheduler
            assert device_state_db.exists()
            assert workflow_history_db.exists()

            # 幂等：重复 setup 返回同一实例
            s2, b2 = integration.setup_edge_scheduler(ws_client=ws, execution_backend=backend)
            assert s2 is scheduler and b2 is backend

            # 云端旧整图路径没有标准 Task/Job 和库存权威，不能再直接产生物理派发。
            with pytest.raises(
                ExecutionPolicyError,
                match="持久派发准入权威未装配",
            ):
                scheduler.submit_workflow(
                    __import__(
                        "unilabos.app.scheduler.models", fromlist=["spec_from_dict"]
                    ).spec_from_dict(
                        {
                            "workflow_id": "wf-report",
                            "nodes": [
                                {
                                    "id": "A",
                                    "device_id": "d9",
                                    "action_name": "run",
                                    "action_type": "goal",
                                }
                            ],
                        }
                    )
                )
        finally:
            backend.stop()
