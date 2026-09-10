"""云端接口测试：workflow_start / workflow_cancel 消息 + workflow_status 上报。

覆盖 ws_client.MessageProcessor 的整图下发转发与 integration 装配层。
"""

import asyncio
import time
from queue import Queue
from typing import Any, Dict, List

import pytest

from unilabos.app.scheduler import integration
from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.service import EdgeScheduler
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
        dispatcher = RecordingDispatcher()
        scheduler = EdgeScheduler(dispatcher=dispatcher)
        mp.edge_scheduler = scheduler

        asyncio.run(mp._handle_workflow_start(_workflow_payload("wf-c")))
        asyncio.run(mp._handle_workflow_cancel({"workflow_id": "wf-c"}))

        snap = scheduler.workflow_snapshot("wf-c")
        assert snap["state"] == "canceled"
        assert scheduler.snapshot()["inflight_jobs"] == {}

    def test_cancel_unknown_workflow_no_crash(self):
        mp = _make_processor()
        mp.edge_scheduler = EdgeScheduler(dispatcher=RecordingDispatcher())
        asyncio.run(mp._handle_workflow_cancel({"workflow_id": "ghost"}))


class TestIntegrationWiring:
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

    def test_setup_injects_scheduler_and_reports_state(self, tmp_path):
        """缺少设备动作目录时应在派发前失败，并上报工作流终态。

        Args:
            tmp_path: 隔离设备状态库与工作流历史库的临时目录。
        """

        class FakeWsClient:
            def __init__(self):
                self.message_processor = _make_processor()

        ws = FakeWsClient()
        device_state_db = tmp_path / "device-state.db"
        workflow_history_db = tmp_path / "workflow-history.db"
        scheduler, backend = integration.setup_edge_scheduler(
            ws_client=ws,
            host_node_getter=lambda: None,
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
            s2, b2 = integration.setup_edge_scheduler(ws_client=ws)
            assert s2 is scheduler and b2 is backend

            # 终态上报：单节点工作流 job 完成 → success → workflow_status 消息
            r = scheduler.submit_workflow(
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
            # HostNode 不存在意味着动作 Schema 也不存在，调度器必须在发送前失败关闭。
            deadline = time.time() + 5
            statuses = []
            while time.time() < deadline and not statuses:
                statuses = [
                    m
                    for m in _drain(ws.message_processor.send_queue)
                    if m.get("action") == "workflow_status"
                ]
                time.sleep(0.02)
            assert statuses, "expected workflow_status report"
            assert statuses[0]["data"]["workflow_id"] == "wf-report"
            assert statuses[0]["data"]["status"] == "failed"
            assert r["dispatched"] == []
        finally:
            backend.stop()
