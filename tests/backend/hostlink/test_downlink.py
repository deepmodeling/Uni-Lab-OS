"""物料下行链路（hostlink/downlink）行为测试。

覆盖四条路径：
- 协程桥 run_node_coroutine（成功 / 异常 / 超时）；
- 本进程分发 local_resource_tree_sync / local_resource_append
  （ros2 形态查 registered_devices，hostlink 形态查 HostLinkLocalRuntime）；
- Host 侧 sync_resource_tree_to_device 本地优先，跨机经 HostLinkServer.request_device；
- 模块级 notify_resource_tree_update 的 True / False / None 语义。
"""

import asyncio
import threading
from typing import Any, Dict, List

import pytest

from unilabos.backend.hostlink.protocol import ActionType
from unilabos.backend.runtime.node import DeviceNode
from unilabos.backend.hostlink import downlink
from unilabos.backend.hostlink.downlink import (
    local_resource_append,
    local_resource_tree_sync,
    notify_resource_tree_update,
    register_hostlink_resource_handlers,
    run_node_coroutine,
    sync_resource_tree_to_device,
)
from unilabos.config.config import BasicConfig


class _FakeTask:
    """最小 rclpy.Task 替身：add_done_callback / result / cancel。"""

    def __init__(self) -> None:
        self._done = threading.Event()
        self._result: Any = None
        self._exception: Any = None
        self._callbacks: List[Any] = []
        self._lock = threading.Lock()

    def _finish(self, result: Any = None, exception: Any = None) -> None:
        with self._lock:
            self._result = result
            self._exception = exception
            self._done.set()
            callbacks, self._callbacks = self._callbacks, []
        for callback in callbacks:
            callback(self)

    def add_done_callback(self, callback: Any) -> None:
        with self._lock:
            if not self._done.is_set():
                self._callbacks.append(callback)
                return
        callback(self)

    def result(self) -> Any:
        if self._exception is not None:
            raise self._exception
        return self._result

    def cancel(self) -> None:
        self._finish(exception=RuntimeError("cancelled"))


class _FakeNode:
    """最小设备节点替身：create_task 在后台线程驱动协程（模拟 rclpy executor）。"""

    def __init__(self, device_id: str = "fake_device") -> None:
        self.device_id = device_id
        self.tree_sync_calls: List[List[Dict[str, Any]]] = []
        self.append_calls: List[Dict[str, Any]] = []

    def create_task(self, coroutine: Any) -> _FakeTask:
        task = _FakeTask()

        def _run() -> None:
            try:
                task._finish(result=asyncio.run(coroutine))
            except BaseException as exc:  # noqa: BLE001 - 测试替身需转存所有异常
                task._finish(exception=exc)

        threading.Thread(target=_run, daemon=True).start()
        return task

    async def apply_resource_tree_update(self, operations: List[Dict[str, Any]]) -> Dict[str, Any]:
        self.tree_sync_calls.append(operations)
        return {"results": [{"success": True}], "total": len(operations)}

    async def append_resource(self, payload: Dict[str, Any], *, commit_point: Any = None) -> Dict[str, Any]:
        self.append_calls.append(payload)
        return {"created_resource_tree": [[{"id": "r1"}]], "substance_resource_tree": []}

    # 阻塞挂载入口与真实设备节点同一实现（提交点握手见 DeviceNode.append_resource_blocking）
    append_resource_blocking = DeviceNode.append_resource_blocking


@pytest.fixture(params=["hostlink", "ros2"])
def fake_device(request, monkeypatch):
    node = _FakeNode()
    monkeypatch.setattr(BasicConfig, "backend", request.param)
    if request.param == "hostlink":
        from types import SimpleNamespace
        from unilabos.backend.hostlink import main_hostlink_run

        runtime = SimpleNamespace(local=SimpleNamespace(
            get_device=lambda identity: node if identity == node.device_id else None,
        ))
        monkeypatch.setattr(main_hostlink_run, "get_runtime", lambda: runtime)
        yield node
        return
    pytest.importorskip("rclpy", reason="仅 ROS2 设备表分支需要选装 ROS")
    from unilabos.backend.ros2.base_device_node import registered_devices

    registered_devices[node.device_id] = {"base_node_instance": node}  # type: ignore[typeddict-item]
    try:
        yield node
    finally:
        registered_devices.pop(node.device_id, None)


def test_run_node_coroutine_returns_result_and_raises() -> None:
    node = _FakeNode()

    async def _ok() -> str:
        return "done"

    async def _boom() -> None:
        raise ValueError("boom")

    assert run_node_coroutine(node, _ok()) == "done"
    with pytest.raises(ValueError, match="boom"):
        run_node_coroutine(node, _boom())


def test_run_node_coroutine_timeout() -> None:
    node = _FakeNode()

    async def _slow() -> None:
        await asyncio.sleep(5)

    with pytest.raises(TimeoutError):
        run_node_coroutine(node, _slow(), timeout=0.2)


def test_local_resource_tree_sync_dispatches_to_registered_node(fake_device: _FakeNode) -> None:
    operations = [{"action": "remove", "data": ["uuid-1"]}]
    result = local_resource_tree_sync({"device_id": fake_device.device_id, "operations": operations})
    assert result == {"results": [{"success": True}], "total": 1}
    assert fake_device.tree_sync_calls == [operations]


def test_local_resource_append_strips_device_id(fake_device: _FakeNode) -> None:
    result = local_resource_append(
        {
            "device_id": fake_device.device_id,
            "resource_uuid": ["uuid-1"],
            "bind_parent_id": "deck",
            "bind_location": {"x": 0.0, "y": 0.0, "z": 0.0},
            "other_calling_param": {"slot": "1"},
        }
    )
    assert result["created_resource_tree"] == [[{"id": "r1"}]]
    assert fake_device.append_calls == [
        {
            "resource_uuid": ["uuid-1"],
            "bind_parent_id": "deck",
            "bind_location": {"x": 0.0, "y": 0.0, "z": 0.0},
            "other_calling_param": {"slot": "1"},
        }
    ]


def test_local_dispatch_unknown_device_raises() -> None:
    with pytest.raises(ValueError, match="没有设备节点"):
        local_resource_tree_sync({"device_id": "ghost", "operations": []})


def test_get_local_device_node_queries_hostlink_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """hostlink 形态：本进程设备表来自 HostLinkLocalRuntime，而非 registered_devices。"""
    from unilabos.config.config import BasicConfig

    node = _FakeNode("hl_dev")

    class _FakeLocal:
        def get_device(self, device_id: str) -> Any:
            return node if device_id == "hl_dev" else None

    class _FakeRuntime:
        local = _FakeLocal()

    monkeypatch.setattr(BasicConfig, "backend", "hostlink")
    monkeypatch.setattr(
        "unilabos.backend.hostlink.main_hostlink_run.get_runtime", lambda: _FakeRuntime()
    )
    assert downlink.get_local_device_node("hl_dev") is node
    assert downlink.get_local_device_node("ghost") is None


def test_register_hostlink_resource_handlers_binds_both_actions() -> None:
    handlers: Dict[str, Any] = {}

    class _FakeClient:
        def register_handler(self, action_type: str, handler: Any) -> None:
            handlers[action_type] = handler

    register_hostlink_resource_handlers(_FakeClient())
    assert handlers[ActionType.RESOURCE_TREE_SYNC] is local_resource_tree_sync
    assert handlers[ActionType.RESOURCE_APPEND] is local_resource_append


def test_sync_resource_tree_prefers_local_then_hostlink(
    fake_device: _FakeNode, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 本进程命中：不触发 HostLink
    result = sync_resource_tree_to_device(fake_device.device_id, [{"action": "remove", "data": ["u"]}])
    assert result["total"] == 1

    # 跨机：经 HostLinkServer.request_device 下行
    remote_calls: List[Any] = []

    class _FakeServer:
        def request_device(self, device_id: str, action_type: str, data: Any, timeout: Any) -> Dict[str, Any]:
            remote_calls.append((device_id, action_type, data, timeout))
            return {"results": [], "total": 0}

    monkeypatch.setattr(downlink, "get_hostlink_server", lambda: _FakeServer())
    sync_resource_tree_to_device("remote_dev", [{"action": "add", "data": ["u2"]}], timeout=7.0)
    assert remote_calls == [
        (
            "remote_dev",
            ActionType.RESOURCE_TREE_SYNC,
            {"device_id": "remote_dev", "operations": [{"action": "add", "data": ["u2"]}]},
            7.0,
        )
    ]


def test_sync_resource_tree_without_hostlink_server_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(downlink, "get_hostlink_server", lambda: None)
    with pytest.raises(RuntimeError, match="HostLink server 未启动"):
        sync_resource_tree_to_device("remote_dev", [])


def test_notify_resource_tree_update_semantics(
    fake_device: _FakeNode, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模块级 notify：本进程命中 True，不可达 None，分发失败 False。"""
    # 本进程命中 → True
    assert notify_resource_tree_update(fake_device.device_id, "remove", ["u1"]) is True
    assert fake_device.tree_sync_calls == [[{"action": "remove", "data": ["u1"]}]]

    # 不可达（无 server）→ None（有意跳过）
    monkeypatch.setattr(downlink, "get_hostlink_server", lambda: None)
    assert notify_resource_tree_update("ghost", "add", ["u2"]) is None

    # 在线表命中但分发失败 → False
    class _BrokenServer:
        def has_device(self, device_id: str) -> bool:
            return True

        def request_device(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
            raise RuntimeError("downlink broken")

    monkeypatch.setattr(downlink, "get_hostlink_server", lambda: _BrokenServer())
    assert notify_resource_tree_update("remote_dev", "add", ["u3"]) is False
