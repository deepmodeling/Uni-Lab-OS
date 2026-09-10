"""作业执行微后端（JobExecutionBackend）与本地调度器（EdgeScheduler）全链路测试。

FakeHostNode 模拟设备执行：send_goal 后按配置同步回报 publish_job_status，
验证「调度器 → 微后端 → HostNode → 回报 → 调度器重排」闭环。
"""

import threading
import time
from typing import Any, Dict, List, Optional

from unilabos.app.scheduler.backend import JobExecutionBackend, create_edge_stack
from unilabos.app.scheduler.dispatch import build_job_start_payload
from unilabos.app.scheduler.models import WorkflowEdge, WorkflowNode, WorkflowSpec
from unilabos.app.ws_client import QueueItem
from unilabos.utils.type_check import serialize_result_info


def _unlocked_action_mapping() -> Dict[str, Any]:
    """构造不含物料参数的遗留动作注册表条目。

    Returns:
        可验证任意对象参数、但不会产生动作物料锁的动作 Schema。
    """

    return {
        "contract_kind": "legacy",
        "schema": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": True,
                },
                "feedback": {},
                "result": {},
            },
            "required": ["goal"],
        },
    }


class FakeHostNode:
    """记录 send_goal；auto_complete 时立即回报成功结果。"""

    def __init__(self, backend_ref: Dict[str, Any], auto_complete: bool = True,
                 ret_values: Optional[Dict[str, Any]] = None):
        """初始化带最小动作目录的测试 HostNode。

        Args:
            backend_ref: 延迟绑定作业执行微后端的共享引用。
            auto_complete: 收到设备命令后是否立即回报成功。
            ret_values: 按 ``device/action`` 指定的模拟动作结果。
        """

        self.sent_goals: List[QueueItem] = []
        self.backend_ref = backend_ref  # {"backend": JobExecutionBackend}，延迟绑定
        self.auto_complete = auto_complete
        self.ret_values = ret_values or {}
        self.lock = threading.Lock()
        # 动作目录覆盖端到端用例中的三个设备，证明调度前已取得 Schema。
        self._action_value_mappings = {
            device_id: {"run": _unlocked_action_mapping()}
            for device_id in ("dev1", "dev2", "shared")
        }

    def send_goal(self, item: QueueItem, action_type: str, action_kwargs: Dict[str, Any],
                  sample_material: Dict[str, Any], server_info: Any = None) -> None:
        with self.lock:
            self.sent_goals.append(item)
        if self.auto_complete:
            ret = self.ret_values.get(f"{item.device_id}/{item.action_name}", {"done": True})
            backend = self.backend_ref["backend"]
            backend.publish_job_status(
                {}, item, "success", serialize_result_info("", True, ret)
            )


def _node(node_id: str, device: str = "dev1", action: str = "run") -> WorkflowNode:
    return WorkflowNode(id=node_id, device_id=device, action_name=action,
                        action_type="goal", param={})


def _edge(src: str, dst: str) -> WorkflowEdge:
    return WorkflowEdge(uuid=f"{src}->{dst}", source_node_id=src, target_node_id=dst)


def _make_backend(auto_complete: bool = True):
    ref: Dict[str, Any] = {}
    host = FakeHostNode(ref, auto_complete=auto_complete)
    backend = JobExecutionBackend(host_node_getter=lambda: host)
    ref["backend"] = backend
    backend.start()
    return backend, host


class TestBackendAlone:
    def test_default_host_getter_waits_for_host_startup(self, monkeypatch):
        """默认执行后端应等待 Host 节点完成启动，不应在启动窗口误判失败。"""

        from unilabos.ros.nodes.presets.host_node import HostNode

        observed_timeouts = []
        expected = object()

        def get_instance(timeout):
            observed_timeouts.append(timeout)
            return expected

        monkeypatch.setattr(HostNode, "get_instance", get_instance)

        assert JobExecutionBackend._default_host_getter() is expected
        assert observed_timeouts == [30]

    def test_dispatch_sends_goal(self):
        backend, host = _make_backend(auto_complete=False)
        try:
            backend.dispatch(build_job_start_payload(
                job_id="j1", task_id="t1", workflow_id="wf", node_id="A",
                device_id="d1", action_name="run", action_type="goal", action_args={"x": 1},
            ))
            assert backend.wait_idle()
            assert [g.job_id for g in host.sent_goals] == ["j1"]
            assert backend.busy_device_action_keys() == {"/devices/d1/run"}
        finally:
            backend.stop()

    def test_same_device_queued_then_started_after_finish(self):
        backend, host = _make_backend(auto_complete=False)
        try:
            for jid in ("j1", "j2"):
                backend.dispatch(build_job_start_payload(
                    job_id=jid, task_id="t", workflow_id="wf", node_id=jid,
                    device_id="d1", action_name="run", action_type="goal", action_args={},
                ))
            assert backend.wait_idle()
            # j2 排队未执行
            assert [g.job_id for g in host.sent_goals] == ["j1"]

            # j1 完成回报 → j2 自动出队执行
            backend.publish_job_status({}, host.sent_goals[0], "success",
                                       serialize_result_info("", True, {}))
            assert backend.wait_idle()
            assert [g.job_id for g in host.sent_goals] == ["j1", "j2"]
        finally:
            backend.stop()

    def test_listener_receives_ret_value(self):
        backend, host = _make_backend(auto_complete=False)
        received: List[tuple] = []
        backend.add_job_finished_listener(lambda *args: received.append(args))
        try:
            backend.dispatch(build_job_start_payload(
                job_id="j1", task_id="t", workflow_id="wf", node_id="A",
                device_id="d1", action_name="run", action_type="goal", action_args={},
            ))
            assert backend.wait_idle()
            backend.publish_job_status({}, host.sent_goals[0], "success",
                                       serialize_result_info("", True, {"volume": 7}))
            assert backend.wait_idle()
            # 第 4 参 suc_type：normal / skip / operator_intervention（异常决策来源）
            assert received == [("j1", True, {"volume": 7}, "normal")]
        finally:
            backend.stop()

    def test_explicit_device_business_failure_is_not_reported_as_job_success(self):
        backend, host = _make_backend(auto_complete=False)
        received: List[tuple] = []
        backend.add_job_finished_listener(lambda *args: received.append(args))
        try:
            backend.dispatch(build_job_start_payload(
                job_id="j-business-failed", task_id="t", workflow_id="wf", node_id="A",
                device_id="d1", action_name="run", action_type="goal", action_args={},
            ))
            assert backend.wait_idle()
            rejection = {
                "success": False,
                "state": "REJECTED",
                "message": "设备拒绝执行",
            }
            backend.publish_job_status(
                {},
                host.sent_goals[0],
                "success",
                serialize_result_info("", True, rejection),
            )
            assert backend.wait_idle()

            assert received == [
                ("j-business-failed", False, rejection, "normal")
            ]
        finally:
            backend.stop()

    def test_foreign_job_status_ignored(self):
        backend, _ = _make_backend(auto_complete=False)
        received: List[tuple] = []
        backend.add_job_finished_listener(lambda *args: received.append(args))
        try:
            item = QueueItem(task_type="job_call_back_status", device_id="d", action_name="a",
                             task_id="t", job_id="ghost", notebook_id="",
                             device_action_key="/devices/d/a")
            backend.publish_job_status({}, item, "success", serialize_result_info("", True, {}))
            assert backend.wait_idle()
            assert received == []
        finally:
            backend.stop()

    def test_running_status_does_not_finish(self):
        backend, host = _make_backend(auto_complete=False)
        received: List[tuple] = []
        backend.add_job_finished_listener(lambda *args: received.append(args))
        try:
            backend.dispatch(build_job_start_payload(
                job_id="j1", task_id="t", workflow_id="wf", node_id="A",
                device_id="d1", action_name="run", action_type="goal", action_args={},
            ))
            assert backend.wait_idle()
            backend.publish_job_status({"pct": 50}, host.sent_goals[0], "running", None)
            assert backend.wait_idle()
            assert received == []
            assert backend.busy_device_action_keys() == {"/devices/d1/run"}
        finally:
            backend.stop()

    def test_missing_ros_action_fails_without_hostlink_fallback(self):
        class MissingRosHost:
            def send_goal(self, *_args, **_kwargs):
                raise ValueError("ActionClient /devices/d1/run not found.")

        backend = JobExecutionBackend(host_node_getter=lambda: MissingRosHost())
        received = []
        done = threading.Event()

        def finished(*args):
            received.append(args)
            done.set()

        backend.add_job_finished_listener(finished)
        backend.start()
        try:
            backend.dispatch(
                build_job_start_payload(
                    job_id="j-ros-missing",
                    task_id="t",
                    workflow_id="wf",
                    node_id="A",
                    device_id="d1",
                    action_name="run",
                    action_type="goal",
                    action_args={"x": 7.5},
                )
            )
            assert done.wait(3.0)
            assert backend.wait_idle()
            assert received == [("j-ros-missing", False, None, "normal")]
        finally:
            backend.stop()


class TestEdgeStackEndToEnd:
    def _wait(self, predicate, timeout: float = 5.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_full_workflow_auto_completes(self):
        """submit → 微后端执行 → 回报 → 重排推进，直到工作流 success。"""
        ref: Dict[str, Any] = {}
        host = FakeHostNode(ref, auto_complete=True)
        scheduler, backend = create_edge_stack(host_node_getter=lambda: host)
        ref["backend"] = backend
        try:
            spec = WorkflowSpec(
                workflow_id="wf-e2e",
                nodes=[_node("A"), _node("B"), _node("C", device="dev2")],
                edges=[_edge("A", "B"), _edge("A", "C")],
            )
            scheduler.submit_workflow(spec)
            assert self._wait(
                lambda: (scheduler.workflow_snapshot("wf-e2e") or {}).get("state") == "success"
            ), scheduler.snapshot()
            assert len(host.sent_goals) == 3
        finally:
            backend.stop()

    def test_two_workflows_same_device_serialized(self):
        ref: Dict[str, Any] = {}
        host = FakeHostNode(ref, auto_complete=True)
        scheduler, backend = create_edge_stack(host_node_getter=lambda: host)
        ref["backend"] = backend
        try:
            for wid in ("wf1", "wf2"):
                scheduler.submit_workflow(WorkflowSpec(
                    workflow_id=wid,
                    nodes=[_node("A", device="shared"), _node("B", device="shared")],
                    edges=[_edge("A", "B")],
                ))
            ok = self._wait(lambda: all(
                (scheduler.workflow_snapshot(w) or {}).get("state") == "success"
                for w in ("wf1", "wf2")
            ))
            assert ok, scheduler.snapshot()
            assert len(host.sent_goals) == 4
        finally:
            backend.stop()

    def test_composition_root_serializes_different_actions_on_one_device(self):
        """证明真实组合根在派发器边界前串行同一设备的不同动作。

        参数：无；测试通过 ``create_edge_stack`` 装配真实作业执行微后端。
        返回：无；断言只有第一个动作到达模拟 HostNode。
        异常：若第二个动作越过真实派发器（Dispatcher）边界，断言失败。
        """
        backend_ref: dict[str, Any] = {}
        host = FakeHostNode(backend_ref, auto_complete=False)
        # 为第二个动作提供合法注册表 Schema，避免合同缺失掩盖设备级互斥行为。
        host._action_value_mappings["shared"]["inspect"] = _unlocked_action_mapping()
        scheduler, backend = create_edge_stack(host_node_getter=lambda: host)
        backend_ref["backend"] = backend
        try:
            first_result = scheduler.submit_workflow(
                WorkflowSpec(
                    workflow_id="wf-real-run",
                    nodes=[_node("run-node", device="shared", action="run")],
                )
            )
            second_result = scheduler.submit_workflow(
                WorkflowSpec(
                    workflow_id="wf-real-inspect",
                    nodes=[_node("inspect-node", device="shared", action="inspect")],
                )
            )

            assert backend.wait_idle()
            assert len(first_result["dispatched"]) == 1
            assert second_result["dispatched"] == []
            assert [(goal.device_id, goal.action_name) for goal in host.sent_goals] == [
                ("shared", "run")
            ]
        finally:
            backend.stop()

    def test_canceled_workflow_waits_for_external_device_terminal_before_reuse(self):
        """证明本地取消不能绕过作业执行微后端仍持有的动作级忙碌事实。

        参数：无；测试通过真实 ``create_edge_stack`` 依次执行派发、取消、再次
        准入和明确终态回报。
        返回：无；断言取消后同设备不同动作保持等待，只有执行器（Executor）
        明确终态且外部忙碌事实消失后才允许重新派发。
        异常：若取消被错误当成物理停止证明，或严格动作键未提升为设备键，断言失败。
        """
        backend_ref: dict[str, Any] = {}
        host = FakeHostNode(backend_ref, auto_complete=False)
        # 两个动作合同均合法，使本测试只观察设备级准入而非 Schema 失败。
        host._action_value_mappings["shared"]["inspect"] = _unlocked_action_mapping()
        scheduler, backend = create_edge_stack(host_node_getter=lambda: host)
        backend_ref["backend"] = backend
        try:
            first_result = scheduler.submit_workflow(
                WorkflowSpec(
                    workflow_id="wf-canceled-run",
                    nodes=[_node("run-node", device="shared", action="run")],
                )
            )
            assert backend.wait_idle()
            assert scheduler.cancel_workflow("wf-canceled-run") is True

            # 本地在途作业已移除，但微后端仍持有 run 的动作级物理忙碌事实。
            waiting_result = scheduler.submit_workflow(
                WorkflowSpec(
                    workflow_id="wf-waiting-inspect-after-cancel",
                    nodes=[_node("inspect-node", device="shared", action="inspect")],
                )
            )
            assert waiting_result["dispatched"] == []
            assert [(goal.device_id, goal.action_name) for goal in host.sent_goals] == [
                ("shared", "run")
            ]

            # 明确终态引用原作业身份；微后端释放动作键后，公开重排才可准入 inspect。
            run_job_uuid = first_result["dispatched"][0]["job_id"]
            backend.publish_job_status(
                {},
                host.sent_goals[0],
                "success",
                serialize_result_info("", True, {"stopped": True}),
            )
            assert backend.wait_idle()
            assert backend.device_manager.get_job_info(run_job_uuid) is None

            released = scheduler.reschedule()
            assert len(released) == 1
            assert released[0]["workflow_id"] == "wf-waiting-inspect-after-cancel"
            assert backend.wait_idle()
            assert [(goal.device_id, goal.action_name) for goal in host.sent_goals] == [
                ("shared", "run"),
                ("shared", "inspect"),
            ]
        finally:
            backend.stop()
