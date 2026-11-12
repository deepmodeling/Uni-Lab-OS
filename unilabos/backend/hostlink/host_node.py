"""HostLink 形态的 Host 执行编排节点。

「host_node」在系统中拆成两层：

1. **host_node 服务设备** —— :class:`unilabos.backend.host_services.HostServices`
   是唯一的动作定义源（registry 单独扫描 ``backend/host_services.py``），
   HostLink 形态经 :func:`unilabos.backend.hostlink.host_services.register_host_services`
   装进本进程 runtime；
2. **执行编排（本类）** —— 本地驱动线程池执行、设备快照刷新、结果规范化。
   ROS2 对应物是 :class:`unilabos.backend.ros2.presets.host_node.HostNode`，
   两者共享 :class:`HostAdapterBase` 的簿记/桥接通知/ping-pong/test_mode 逻辑。

物料/设备管理下行是 :mod:`unilabos.backend.hostlink.downlink` 的模块级函数，
不挂在本类上。微后端只依赖 ``adapter_registry.get_execution_adapter()`` 契约。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import traceback

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from typing import TYPE_CHECKING, Any, ClassVar, Dict, List, Optional

from unilabos.backend.hostlink.adapter_registry import set_execution_adapter
from unilabos.backend.hostlink.backend import to_wire_value
from unilabos.backend.hostlink.protocol import RemoteError
from unilabos.backend.runtime.action import ActionCancelled, ActionContext
from unilabos.backend.runtime.host_adapter import (
    HostAdapterBase,
    execution_result_bridges,
)
from unilabos.config.config import BasicConfig
from unilabos.registry.action_policy import SUCCESS_TYPE_CANCELLATION
from unilabos.resources.resource_tracker import PARAM_SAMPLE_UUIDS
from unilabos.utils import logger
from unilabos.utils.serialization import serialize_result_info

if TYPE_CHECKING:
    from unilabos.backend.hostlink.backend import HostLinkBackend
    from unilabos.server.backend.execution_queue import QueueItem


class HostNode(HostAdapterBase):
    """HostLink 的 host 执行编排节点（单例）。

    构造即启动：注册执行适配器、刷新设备快照并启动监控线程；
    结束时调用 :meth:`stop`。
    """

    supports_execution_start_guard = True

    _instance: ClassVar[Optional["HostNode"]] = None
    _ready_event: ClassVar[threading.Event] = threading.Event()

    @classmethod
    def get_instance(cls, timeout=None) -> Optional["HostNode"]:
        if cls._ready_event.wait(timeout):
            return cls._instance
        return None

    @classmethod
    def reset_state(cls) -> None:
        """重置单例状态（销毁实例后调用，供重启/干净退出）。"""
        cls._instance = None
        cls._ready_event.clear()
        logger.info("[Host Node] State reset complete")

    def __init__(
        self,
        device_id: str,
        runtime: "HostLinkBackend",
        bridges: Optional[List[Any]] = None,
    ):
        """初始化 HostLink 主机编排节点。

        Args:
            device_id: host_node 服务设备的 device_id
            runtime: HostLink backend 实例
            bridges: 桥接器列表
        """
        if self._instance is not None:
            self._instance.lab_logger().critical("[Host Node] HostNode instance already exists.")
        self.__class__._instance = self

        # 共享簿记（devices_names / device_status / ping / goal 状态等）
        HostAdapterBase.__init__(self, bridges=bridges)
        self.device_id = device_id

        self.runtime = runtime
        self._device_descriptors: Dict[str, Dict[str, Any]] = {}
        self._contexts: Dict[str, ActionContext] = {}
        self._contexts_lock = threading.RLock()
        self._executor = ThreadPoolExecutor(
            max_workers=max(4, len(runtime.local.devices) * 2),
            thread_name_prefix="hostlink-host-action",
        )
        self._stop_event = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None

        for node in self.runtime.local.devices.values():
            node.add_status_listener(self._on_local_status)
        self.refresh_devices(initial=True, notify_ready=False)
        self._monitor_thread = threading.Thread(
            target=self._monitor_devices,
            name="hostlink-host-node",
            daemon=True,
        )
        self._monitor_thread.start()

        self.lab_logger().info("[Host Node] Host node initialized (backend=hostlink).")
        HostNode._ready_event.set()
        set_execution_adapter(self)
        self.notify_ready()

    def stop(self) -> None:
        """停止执行编排（关闭监控线程与动作线程池）。"""
        self._stop_event.set()
        monitor = self._monitor_thread
        if monitor is not None and monitor is not threading.current_thread():
            monitor.join(timeout=2.0)
        for node in self.runtime.local.devices.values():
            node.remove_status_listener(self._on_local_status)
        with self._contexts_lock:
            contexts = list(self._contexts.values())
        for context in contexts:
            context.request_cancel()
        # 给协作式取消的 driver 一个短窗口退出，避免其工作线程越过 runtime
        # 的关停边界。非协作式 driver 仍不应无限阻塞 Host 退出。
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            with self._contexts_lock:
                if not self._contexts:
                    break
            time.sleep(0.01)
        with self._contexts_lock:
            drained = not self._contexts
        self._executor.shutdown(wait=drained, cancel_futures=True)

    # ------------------------------------------------------------------
    # 设备快照与状态
    # ------------------------------------------------------------------

    def _monitor_devices(self) -> None:
        while not self._stop_event.wait(0.5):
            try:
                self.refresh_devices(initial=False, notify_ready=True)
            except Exception:  # noqa: BLE001 - monitor must remain alive
                logger.exception("[Host Node] 设备状态刷新失败")

    def _device_snapshot(self, *, initial: bool) -> Dict[str, Dict[str, Any]]:
        if initial:
            return {
                key: value for key, value in self.runtime.devices(online_only=True).items()
                if value.get("device", {}).get("ready", True)
            }
        result: Dict[str, Dict[str, Any]] = {}
        for descriptor in self.runtime.local.descriptors():
            if not descriptor.get("ready", True):
                continue
            device_id = str(descriptor["id"])
            node = self.runtime.local.devices[device_id]
            result[device_id] = {
                "device": descriptor,
                "state": to_wire_value(node.latest_status()),
                "location": "local",
                "online": True,
            }
        if self.runtime.server is not None:
            for device_id, peer in self.runtime.server.devices(True).items():
                if not peer.get("device", {}).get("ready", True):
                    continue
                remote = dict(peer)
                remote["location"] = "remote"
                result.setdefault(device_id, remote)
        return result

    def refresh_devices(self, *, initial: bool = False, notify_ready: bool = False) -> None:
        snapshot = self._device_snapshot(initial=initial)
        self._subscribed_topics = set(self.runtime.local.topic_bus.subscribed_topics())
        old_devices = set(self.devices_names)
        current_devices = set(snapshot)

        self.devices_names = {device_id: self.namespace for device_id in sorted(current_devices)}
        self._online_devices = {f"{self.namespace}/{device_id}" for device_id in current_devices}
        self.device_machine_names = {}
        self._action_value_mappings = {}
        self._device_descriptors = {}

        for device_id, info in snapshot.items():
            descriptor = dict(info.get("device") or {"id": device_id})
            self._device_descriptors[device_id] = descriptor
            mappings = descriptor.get("action_value_mappings")
            self._action_value_mappings[device_id] = dict(mappings) if isinstance(mappings, dict) else {}
            if info.get("location") == "local":
                machine_name = BasicConfig.machine_name or "本地"
            else:
                machine_name = str(info.get("machine_name") or "远程")
            self.device_machine_names[device_id] = machine_name

            state = info.get("state")
            if isinstance(state, dict):
                for name, value in state.items():
                    self._update_device_status(device_id, str(name), value)

        for device_id in old_devices - current_devices:
            self.device_status.pop(device_id, None)
            self.device_status_timestamps.pop(device_id, None)

        if notify_ready and old_devices != current_devices:
            for bridge in self.bridges:
                publish_ready = getattr(bridge, "publish_host_ready", None)
                if callable(publish_ready):
                    try:
                        publish_ready()
                    except Exception:  # noqa: BLE001 - reconnect will retry
                        logger.exception("[Host Node] host_ready 更新失败")

    def _on_local_status(self, device_id: str, name: str, value: Any) -> None:
        self._update_device_status(device_id, name, to_wire_value(value))

    def _update_device_status(self, device_id: str, name: str, value: Any) -> None:
        statuses = self.device_status.setdefault(device_id, {})
        changed = name not in statuses or statuses[name] != value
        statuses[name] = value
        self.device_status_timestamps.setdefault(device_id, {})[name] = time.time()
        if not changed:
            return
        for bridge in self.bridges:
            publish_status = getattr(bridge, "publish_device_status", None)
            if callable(publish_status):
                publish_status(self.device_status, device_id, name)

    # ------------------------------------------------------------------
    # 动作派发（线程池执行）
    # ------------------------------------------------------------------

    def _action_descriptor(self, device_id: str) -> Dict[str, Any]:
        return self._device_descriptors.get(device_id, {})

    def _prepare_action_kwargs(
        self,
        item: "QueueItem",
        action_kwargs: Dict[str, Any],
        sample_material: Dict[str, Any],
    ) -> Dict[str, Any]:
        kwargs = deepcopy(action_kwargs)
        if not sample_material:
            return kwargs

        descriptor = self._action_descriptor(item.device_id)
        system_parameters = descriptor.get("system_parameters")
        action_parameters = (
            system_parameters.get(item.action_name, [])
            if isinstance(system_parameters, dict)
            else []
        )
        if PARAM_SAMPLE_UUIDS in action_parameters:
            kwargs.setdefault(PARAM_SAMPLE_UUIDS, deepcopy(sample_material))
        return kwargs

    def send_goal(
        self,
        item: "QueueItem",
        action_type: str,
        action_kwargs: Dict[str, Any],
        sample_material: Dict[str, Any],
        server_info: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._goal_state_lock:
            if item.job_id in self._canceled_jobs:
                self.lab_logger().info(f"[Host Node] Skip canceled goal {item.job_id[:8]}")
                return
        if item.action_name == "test_latency" and server_info is not None:
            self.server_latest_timestamp = float(server_info.get("send_timestamp", 0.0))

        if item.device_id not in self.devices_names:
            self.refresh_devices(initial=False, notify_ready=False)
        if item.device_id not in self.devices_names:
            raise KeyError(f"未知 HostLink 设备：{item.device_id}")

        local_node = self.runtime.local.devices.get(item.device_id)
        run_simulator = bool(
            local_node is not None
            and getattr(local_node.driver, "run_in_test_mode", False) is True
        )
        if BasicConfig.test_mode and not run_simulator:
            from unilabos.server.backend.execution_start import confirm_execution_start
            confirm_execution_start(item)
            result = self._build_test_mode_return(item.device_id, item.action_name, action_kwargs)
            return_info = serialize_result_info("", True, result)
            result_data = self._result_data(item, result, return_info)
            self._publish_terminal_result(item, "success", return_info, result_data)
            return

        context = ActionContext(
            action_id=item.job_id,
            feedback_callback=lambda _action_id, feedback: self._publish_feedback(item, feedback),
            late_result_callback=lambda _action_id, value, error: self._publish_late_result(
                item, action_type, value, error
            ),
        )
        kwargs = self._prepare_action_kwargs(item, action_kwargs, sample_material)
        with self._contexts_lock:
            self._contexts[item.job_id] = context
        self._inflight_goal_jobs.add(item.job_id)
        try:
            future = self._executor.submit(
                self._execute_job,
                item,
                action_type,
                kwargs,
                context,
            )
        except Exception:
            with self._contexts_lock:
                self._contexts.pop(item.job_id, None)
            self._inflight_goal_jobs.discard(item.job_id)
            raise
        self._goals[item.job_id] = future

    def _execute_job(
        self,
        item: "QueueItem",
        action_type: str,
        action_kwargs: Dict[str, Any],
        context: ActionContext,
    ) -> None:
        from unilabos.server.backend.execution_start import confirm_execution_start, ExecutionStartUnknown
        try:
            context.raise_if_cancelled()
            confirm_execution_start(item)
            # A scheduler action may legitimately run for hours.  ``-1`` means
            # no transport request deadline; cancellation remains explicit.
            result = asyncio.run(
                self.runtime.call_action_async(
                    item.device_id,
                    item.action_name,
                    action_context=context,
                    request_timeout=-1,
                    **action_kwargs,
                )
            )
            status, return_info = self._normalize_result(item, action_type, result)
            result_data = self._result_data(item, result, return_info)
        except ExecutionStartUnknown as exc:
            from unilabos.registry.action_policy import SUCCESS_TYPE_EXECUTION_UNKNOWN
            status = "failed"
            return_info = serialize_result_info(str(exc), False, {}, suc_type=SUCCESS_TYPE_EXECUTION_UNKNOWN)
            return_info["uncertainty_reason"] = str(exc)
            result_data = self._result_data(item, {}, return_info)
        except ActionCancelled:
            status = "failed"
            return_info = serialize_result_info(
                "Job was cancelled",
                False,
                {},
                suc_type=SUCCESS_TYPE_CANCELLATION,
            )
            result_data = self._result_data(item, {}, return_info)
        except Exception as exc:  # noqa: BLE001 - 驱动明确失败与传输结果未知必须区分
            from unilabos.backend.hostlink.protocol import LinkError, RemoteError
            from unilabos.registry.action_policy import SUCCESS_TYPE_EXECUTION_UNKNOWN
            status = "failed"
            error_text = traceback.format_exc()
            if isinstance(exc, LinkError) and not isinstance(exc, RemoteError):
                return_info = serialize_result_info(error_text, False, {}, suc_type=SUCCESS_TYPE_EXECUTION_UNKNOWN)
                return_info["uncertainty_reason"] = str(exc)
            else:
                error_info = self._exception_error_info(item.action_name, exc, error_text)
                return_info = serialize_result_info(error_text, False, {}, error_info=error_info)
            result_data = self._result_data(item, {}, return_info)
        finally:
            with self._contexts_lock:
                self._contexts.pop(item.job_id, None)
            self._inflight_goal_jobs.discard(item.job_id)

        with self._goal_state_lock:
            canceled = item.job_id in self._canceled_jobs
        from unilabos.registry.action_policy import SUCCESS_TYPE_EXECUTION_UNKNOWN
        if canceled and status != "canceled" and return_info.get("suc_type") != SUCCESS_TYPE_EXECUTION_UNKNOWN:
            status = "failed"
            return_info = serialize_result_info(
                "Job was cancelled",
                False,
                {},
                suc_type=SUCCESS_TYPE_CANCELLATION,
            )
            result_data = self._result_data(item, {}, return_info)

        self._publish_terminal_result(item, status, return_info, result_data)

    def _publish_late_result(
        self,
        item: "QueueItem",
        action_type: str,
        value: Any,
        error: Optional[BaseException],
    ) -> None:
        """硬超时之后驱动线程才返回：按真实结果回报，执行面把它当迟到结果处理（ADR 0008）。"""

        try:
            if error is None:
                status, return_info = self._normalize_result(item, action_type, value)
                result_data = self._result_data(item, value, return_info)
            else:
                status = "failed"
                error_text = "".join(traceback.format_exception(type(error), error, error.__traceback__))
                error_info = self._exception_error_info(item.action_name, error, error_text)
                return_info = serialize_result_info(error_text, False, {}, error_info=error_info)
                result_data = self._result_data(item, {}, return_info)
            for bridge in execution_result_bridges(self.bridges):
                publish_status = getattr(bridge, "publish_job_status", None)
                if callable(publish_status):
                    publish_status(result_data, item, status, return_info)
        except Exception:  # noqa: BLE001 - 迟到结果回报失败只影响锁的自动释放，可人工强制释放
            self.lab_logger().exception(f"作业 {item.job_id} 的迟到结果回报失败")

    def _result_data(
        self,
        item: "QueueItem",
        result: Any,
        return_info: Dict[str, Any],
    ) -> Dict[str, Any]:
        data = {
            "return_value": to_wire_value(result),
            "return_info": json.dumps(return_info, ensure_ascii=False),
        }
        mapping = self._action_value_mappings.get(item.device_id, {}).get(item.action_name, {})
        result_mapping = mapping.get("result") if isinstance(mapping, dict) else None
        if not isinstance(result_mapping, dict):
            return data

        wire_result = to_wire_value(result)
        for wire_name, source_name in result_mapping.items():
            if not isinstance(source_name, str):
                continue
            source_name = source_name.removesuffix("[]")
            if source_name == "return_info":
                value = data["return_info"]
            elif source_name in {"success", "reached_goal"}:
                value = bool(return_info.get("suc"))
            elif isinstance(wire_result, dict) and source_name in wire_result:
                value = wire_result[source_name]
            else:
                value = getattr(result, source_name, None)
                if value is None:
                    continue
                value = to_wire_value(value)
            self._set_result_path(data, str(wire_name), value)
        return data

    @staticmethod
    def _set_result_path(target: Dict[str, Any], path: str, value: Any) -> None:
        """Set a dotted ROS result field on the transport result dictionary."""
        parts = [part for part in path.split(".") if part]

        def write(current: Dict[str, Any], index: int, current_value: Any) -> None:
            part = parts[index]
            is_array = part.endswith("[]")
            name = part.removesuffix("[]")
            last = index == len(parts) - 1
            if is_array:
                values = (
                    list(current_value)
                    if isinstance(current_value, (list, tuple))
                    else []
                )
                if last:
                    current[name] = values
                    return
                children: list[Dict[str, Any]] = []
                for item_value in values:
                    child: Dict[str, Any] = {}
                    write(child, index + 1, item_value)
                    children.append(child)
                current[name] = children
                return
            if last:
                current[name] = current_value
                return
            child = current.setdefault(name, {})
            write(child, index + 1, current_value)

        if parts:
            write(target, 0, value)

    @staticmethod
    def _normalize_result(
        item: "QueueItem",
        action_type: str,
        result: Any,
    ) -> tuple[str, Dict[str, Any]]:
        if isinstance(result, dict):
            raw_return_info = result.get("return_info")
            if isinstance(raw_return_info, str):
                try:
                    parsed = json.loads(raw_return_info)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, dict) and isinstance(parsed.get("suc"), bool):
                    return ("success" if parsed["suc"] else "failed"), parsed
            if isinstance(result.get("suc"), bool) and "return_value" in result:
                return ("success" if result["suc"] else "failed"), dict(result)

        json_command = str(action_type or "").startswith("UniLabJsonCommand")
        failed = not json_command and (
            result is False
            or (isinstance(result, dict) and result.get("success") is False)
        )
        if not failed:
            return "success", serialize_result_info("", True, result)

        error_message = (
            str(result.get("error") or result.get("message") or result)
            if isinstance(result, dict)
            else f"driver returned an unsuccessful result: {result!r}"
        )
        source = result if isinstance(result, dict) else {}
        error_info = {
            "action_name": str(source.get("action_name") or item.action_name),
            "exception_type": str(source.get("exception_type") or "ActionResultError"),
            "exception_mro": list(
                source.get("exception_mro")
                or [
                    "ActionResultError",
                    "RuntimeError",
                    "Exception",
                    "BaseException",
                    "object",
                ]
            ),
            "error_message": str(source.get("error_message") or error_message),
            "traceback": str(source.get("traceback") or error_message),
        }
        for key in ("category", "severity"):
            if source.get(key) is not None:
                error_info[key] = str(source[key])
        return "failed", serialize_result_info(
            error_message,
            False,
            result,
            error_info=error_info,
        )

    @staticmethod
    def _exception_error_info(
        action_name: str,
        exc: BaseException,
        error_text: str,
    ) -> Dict[str, Any]:
        if isinstance(exc, RemoteError) and exc.error_info:
            info = deepcopy(exc.error_info)
            info.setdefault("action_name", action_name)
            info.setdefault("error_message", str(exc))
            info.setdefault("traceback", error_text)
            return info
        to_error_info = getattr(exc, "to_error_info", None)
        structured = to_error_info() if callable(to_error_info) else {}
        info: Dict[str, Any] = {
            # 超时异常自带结构化身份（timeout_seconds、driver_still_running 等）
            **(structured if isinstance(structured, dict) else {}),
            "action_name": action_name,
            "exception_type": type(exc).__name__,
            "exception_mro": [kind.__name__ for kind in type(exc).__mro__],
            "error_message": str(exc),
            "traceback": error_text,
        }
        for key in ("category", "severity"):
            value = getattr(exc, key, None)
            if value is not None:
                info[key] = str(getattr(value, "value", value))
        return info

    def _publish_feedback(self, item: "QueueItem", feedback: Any) -> None:
        feedback_data = (
            to_wire_value(feedback) if isinstance(feedback, dict) else {"value": to_wire_value(feedback)}
        )
        for bridge in execution_result_bridges(self.bridges):
            publish_status = getattr(bridge, "publish_job_status", None)
            if callable(publish_status):
                publish_status(feedback_data, item, "running")

    def cancel_job(self, job_id: str) -> bool:
        with self._contexts_lock:
            context = self._contexts.get(job_id)
        if context is None:
            return False
        with self._goal_state_lock:
            self._canceled_jobs.add(job_id)
        context.request_cancel()
        try:
            return bool(self.runtime.cancel_action(job_id))
        except Exception:  # noqa: BLE001 - context cancellation is still active
            logger.exception("[Host Node] 取消动作失败：%s", job_id)
            return False


__all__ = ["HostNode"]
