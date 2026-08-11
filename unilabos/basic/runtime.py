"""供 ``basic`` backend 使用的单进程运行时。"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Coroutine, Dict, Iterable, Optional, Type

from unilabos.device_runtime.node import BackendCapabilityError, DeviceNode
from unilabos.device_runtime.action import ActionContext
from unilabos.device_runtime.resource import ResourceService
from unilabos.device_runtime.topic import LocalTopicBus
from unilabos.utils.decorator import get_all_subscriptions


def instantiate_driver(
    driver_class: Type[Any], device_id: str, config: Optional[Dict[str, Any]] = None
) -> Any:
    """使用 ``config`` 对象或展开参数实例化驱动。

    新驱动通常接收 ``device_id`` 和 ``config``，旧驱动则把配置项直接声明为构造参数。
    Basic 运行时不导入 ROS 设备包装器，同时兼容这两种形式。
    """

    config = dict(config or {})
    signature = inspect.signature(driver_class.__init__)
    parameters = {
        name: parameter
        for name, parameter in signature.parameters.items()
        if name != "self"
    }
    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )

    kwargs: Dict[str, Any]
    if "config" in parameters:
        kwargs = {"config": config}
    else:
        kwargs = dict(config)

    if "device_id" in parameters or accepts_kwargs:
        kwargs.setdefault("device_id", device_id)
    elif "id" in parameters:
        kwargs.setdefault("id", device_id)
    return driver_class(**kwargs)


class BasicDeviceNode(DeviceNode):
    """向驱动提供异步辅助能力的轻量节点适配器。"""

    def __init__(
        self,
        driver: Any,
        device_id: str,
        *,
        backend_name: str = "basic",
        resource_uuid: str = "",
        registry_name: str = "",
        display_name: str = "",
        action_names: Iterable[str] = (),
        status_names: Iterable[str] = (),
    ) -> None:
        self.driver = driver
        self.device_id = device_id
        self.backend_name = str(backend_name or "basic")
        self.resource_uuid = str(resource_uuid or "")
        self.registry_name = str(registry_name or "")
        self.display_name = str(display_name or registry_name or device_id)
        self.action_names = tuple(
            sorted(
                {
                    str(name).strip()
                    for name in action_names
                    if str(name).strip() and not str(name).startswith("_")
                }
            )
        )
        self.status_names = tuple(
            sorted({str(name).strip() for name in status_names if str(name).strip()})
        )
        self._logger = logging.getLogger(f"unilabos.basic.{device_id}")
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_loop,
            name=f"basic-driver-{device_id}",
            daemon=True,
        )
        self._loop_ready = threading.Event()
        self._started = False
        self._action_lock: asyncio.Lock | None = None
        self._status_lock = threading.Lock()
        self._decorated_subscriptions: list[Any] = []

    _ROS2_RUNTIME_ATTRIBUTES = frozenset(
        {
            "create_timer",
            "create_rate",
            "create_service",
            "create_client",
            "create_guard_condition",
            "create_action_server",
            "create_action_client",
            "declare_parameter",
            "declare_parameters",
            "get_parameter",
            "get_parameters",
            "set_parameters",
            "get_clock",
            "get_publishers_info_by_topic",
            "get_subscriptions_info_by_topic",
            "wait_for_service",
        }
    )

    def _raise_backend_attribute_error(self, exc: AttributeError) -> None:
        """把驱动对 ROS2 Node API 的直接调用转换成容易定位的错误。"""

        name = str(getattr(exc, "name", "") or "")
        owner = getattr(exc, "obj", None)
        if owner is self and name in self._ROS2_RUNTIME_ATTRIBUTES:
            raise BackendCapabilityError(
                f"设备 {self.device_id!r} 在 backend '{self.backend_name}' 中调用了 "
                f"ROS2 Node 方法 {name!r}；请改用通用 DeviceNode 接口，或把该设备的 "
                "supported_backends 限制为 ros2"
            ) from exc
        raise exc

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._action_lock = asyncio.Lock()
        self._loop_ready.set()
        self._loop.run_forever()

    async def sleep(self, rel_time: float, callback_group: Any = None) -> None:
        await asyncio.sleep(rel_time)

    def lab_logger(self) -> logging.Logger:
        return self._logger

    def create_task(self, coroutine: Coroutine[Any, Any, Any]):
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    def _call(
        self,
        method: Callable[..., Any],
        *args: Any,
        _wait_timeout: Optional[float] = None,
        **kwargs: Any,
    ) -> Any:
        result = method(*args, **kwargs)
        if inspect.isawaitable(result):
            async def await_result(value: Awaitable[Any]) -> Any:
                return await value

            return asyncio.run_coroutine_threadsafe(
                await_result(result), self._loop
            ).result(timeout=_wait_timeout)
        return result

    def start(self) -> None:
        if self._started:
            return
        self._loop_thread.start()
        if not self._loop_ready.wait(timeout=5):
            raise RuntimeError(f"Basic 设备 {self.device_id!r} 事件循环启动超时")
        self._started = True
        try:
            if hasattr(self.driver, "post_init"):
                self.driver.post_init(self)
            self._setup_decorated_subscriptions()
            initialize = getattr(self.driver, "initialize", None)
            if callable(initialize):
                self._call(initialize, _wait_timeout=30)
        except AttributeError as exc:
            self.stop()
            self._raise_backend_attribute_error(exc)
        except Exception:
            self.stop()
            raise
        self._logger.info("Basic 设备已就绪：%s", self.device_id)

    def _setup_decorated_subscriptions(self) -> None:
        for _method_name, method, config in get_all_subscriptions(self.driver):
            topic = config.get("topic")
            target_device = config.get("device_id")
            status_name = config.get("status_name")
            if target_device or status_name:
                if not target_device or not status_name:
                    raise ValueError("@subscribe 需要同时提供 device_id 和 status_name")
                topic = f"/devices/{target_device}/{status_name}"
            if not topic:
                raise ValueError("@subscribe 缺少 topic")
            self._decorated_subscriptions.append(
                self.create_subscription(
                    config.get("msg_type"),
                    topic,
                    method,
                    config.get("qos", 10),
                    trigger_when_change=config.get("trigger_when_change", False),
                )
            )

    def _resolve_action(
        self,
        action_name: str,
        *,
        action_context: Optional[ActionContext] = None,
        **kwargs: Any,
    ) -> tuple[Callable[..., Any], ActionContext, Dict[str, Any]]:
        if not self._started:
            raise RuntimeError(f"Basic 设备 {self.device_id!r} 尚未启动")
        action_name = str(action_name or "").strip()
        if action_name.startswith("_") or (
            self.action_names and action_name not in self.action_names
        ):
            raise AttributeError(
                f"Basic 设备 {self.device_id!r} 没有动作 {action_name!r}"
            )
        method_name = action_name.removeprefix("auto-")
        action = getattr(self.driver, method_name, None)
        if not callable(action):
            raise AttributeError(
                f"Basic 设备 {self.device_id!r} 没有动作 {action_name!r}"
            )
        context = action_context or ActionContext()
        signature = inspect.signature(action)
        if "action_context" in signature.parameters:
            kwargs.setdefault("action_context", context)
        return action, context, kwargs

    async def _execute_action(
        self,
        action: Callable[..., Any],
        context: ActionContext,
        kwargs: Dict[str, Any],
    ) -> Any:
        lock = self._action_lock
        if lock is None:
            raise RuntimeError(f"Basic 设备 {self.device_id!r} 事件循环尚未就绪")
        try:
            async with lock:
                context.raise_if_cancelled()
                if inspect.iscoroutinefunction(action):
                    result = await action(**kwargs)
                else:
                    result = await asyncio.to_thread(action, **kwargs)
                    if inspect.isawaitable(result):
                        result = await result
                context.raise_if_cancelled()
                return result
        except AttributeError as exc:
            self._raise_backend_attribute_error(exc)

    def call_action(
        self,
        action_name: str,
        *,
        action_context: Optional[ActionContext] = None,
        **kwargs: Any,
    ) -> Any:
        """Run an action synchronously while using the device event loop."""

        action, context, call_kwargs = self._resolve_action(
            action_name,
            action_context=action_context,
            **kwargs,
        )
        future = asyncio.run_coroutine_threadsafe(
            self._execute_action(action, context, call_kwargs),
            self._loop,
        )
        return future.result()

    async def call_action_async(
        self,
        action_name: str,
        *,
        action_context: Optional[ActionContext] = None,
        **kwargs: Any,
    ) -> Any:
        """Await an action on this device's own Python event loop."""

        action, context, call_kwargs = self._resolve_action(
            action_name,
            action_context=action_context,
            **kwargs,
        )
        try:
            if asyncio.get_running_loop() is self._loop:
                return await self._execute_action(action, context, call_kwargs)
            future = asyncio.run_coroutine_threadsafe(
                self._execute_action(action, context, call_kwargs),
                self._loop,
            )
            return await asyncio.wrap_future(future)
        except asyncio.CancelledError:
            context.request_cancel()
            raise

    def snapshot_status(self) -> Dict[str, Any]:
        """读取注册表声明的状态；单个状态失败不影响其他字段。"""

        result: Dict[str, Any] = {}
        errors: Dict[str, str] = {}
        with self._status_lock:
            for name in self.status_names:
                try:
                    getter = getattr(self.driver, f"get_{name}", None)
                    if callable(getter):
                        value = self._call(getter)
                    else:
                        value = getattr(self.driver, name)
                        if callable(value):
                            value = self._call(value)
                    result[name] = value
                    self.emit_status(name, value)
                except Exception as exc:  # noqa: BLE001 - 状态快照需部分成功
                    errors[name] = str(exc)
        if errors:
            result["_errors"] = errors
        return result

    def describe(self) -> Dict[str, Any]:
        descriptor = {
            "id": self.device_id,
            "registry_name": self.registry_name,
            "display_name": self.display_name,
            "actions": list(self.action_names),
            "status_fields": list(self.status_names),
        }
        if self.resource_uuid:
            descriptor["resource_uuid"] = self.resource_uuid
        return descriptor

    def stop(self) -> None:
        if not self._started:
            return
        for subscription in self._decorated_subscriptions:
            subscription.destroy()
        self._decorated_subscriptions.clear()
        cleanup = getattr(self.driver, "cleanup", None)
        if callable(cleanup):
            try:
                self._call(cleanup, _wait_timeout=10)
            except Exception:
                self._logger.exception("Basic 设备清理失败：%s", self.device_id)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join(timeout=5)
        self._loop.close()
        self._started = False


@dataclass(frozen=True)
class BasicDriverSpec:
    device_id: str
    driver_class: Type[Any]
    config: Dict[str, Any]
    registry_name: str = ""
    action_names: tuple[str, ...] = ()
    status_names: tuple[str, ...] = ()
    display_name: str = ""
    resource_uuid: str = ""


class BasicRuntime:
    """管理一个 Basic backend 进程内的全部驱动实例。"""

    def __init__(self, backend_name: str = "basic") -> None:
        self.backend_name = str(backend_name or "basic")
        self.devices: dict[str, BasicDeviceNode] = {}
        self.topic_bus = LocalTopicBus()
        self._resource_service: ResourceService | None = None
        self._stopped = threading.Event()

    def add_driver(self, spec: BasicDriverSpec) -> BasicDeviceNode:
        if spec.device_id in self.devices:
            raise ValueError(f"Basic 设备 ID 重复：{spec.device_id}")
        driver = instantiate_driver(spec.driver_class, spec.device_id, spec.config)
        node = BasicDeviceNode(
            driver,
            spec.device_id,
            backend_name=self.backend_name,
            resource_uuid=spec.resource_uuid,
            registry_name=spec.registry_name,
            display_name=spec.display_name,
            action_names=spec.action_names,
            status_names=spec.status_names,
        )
        if self._resource_service is not None:
            node.set_resource_service(self._resource_service)
        node.set_action_router(self)
        node.set_topic_bus(self.topic_bus)
        self.devices[spec.device_id] = node
        return node

    def set_resource_service(self, service: ResourceService) -> None:
        self._resource_service = service
        for node in self.devices.values():
            node.set_resource_service(service)

    @staticmethod
    def _normalize_device_id(device_id: str) -> str:
        normalized = str(device_id or "").strip()
        if normalized.startswith("/devices/"):
            normalized = normalized[len("/devices/") :]
        return normalized.lstrip("/")

    def route_action(
        self,
        caller_device_id: str,
        device_id: str,
        action_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        **options: Any,
    ) -> Any:
        target = self._normalize_device_id(device_id)
        if target == caller_device_id:
            raise ValueError("跨设备动作不能回调当前设备自身")
        context = options.get("action_context")
        if context is None and (
            options.get("action_id") or options.get("feedback_callback")
        ):
            context = ActionContext(
                action_id=str(options.get("action_id") or "")
                or ActionContext().action_id,
                feedback_callback=options.get("feedback_callback"),
            )
        return self.call_action(
            target,
            action_name,
            action_context=context,
            **dict(arguments or {}),
        )

    async def route_action_async(
        self,
        caller_device_id: str,
        device_id: str,
        action_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        **options: Any,
    ) -> Any:
        target = self._normalize_device_id(device_id)
        if target == caller_device_id:
            raise ValueError("跨设备动作不能回调当前设备自身")
        context = options.get("action_context")
        if context is None and (
            options.get("action_id") or options.get("feedback_callback")
        ):
            context = ActionContext(
                action_id=str(options.get("action_id") or "")
                or ActionContext().action_id,
                feedback_callback=options.get("feedback_callback"),
            )
        operation = self.call_action_async(
            target,
            action_name,
            action_context=context,
            **dict(arguments or {}),
        )
        timeout = options.get("timeout")
        if timeout is None:
            return await operation
        return await asyncio.wait_for(operation, float(timeout))

    def start(self) -> None:
        self._stopped.clear()
        started: list[BasicDeviceNode] = []
        try:
            for node in self.devices.values():
                node.start()
                started.append(node)
        except Exception:
            for node in reversed(started):
                node.stop()
            raise

    def call_action(
        self,
        device_id: str,
        action_name: str,
        *,
        action_context: Optional[ActionContext] = None,
        **kwargs: Any,
    ) -> Any:
        try:
            node = self.devices[device_id]
        except KeyError as exc:
            raise KeyError(f"未知 Basic 设备：{device_id}") from exc
        return node.call_action(
            action_name,
            action_context=action_context,
            **kwargs,
        )

    async def call_action_async(
        self,
        device_id: str,
        action_name: str,
        *,
        action_context: Optional[ActionContext] = None,
        **kwargs: Any,
    ) -> Any:
        try:
            node = self.devices[device_id]
        except KeyError as exc:
            raise KeyError(f"未知 Basic 设备：{device_id}") from exc
        return await node.call_action_async(
            action_name,
            action_context=action_context,
            **kwargs,
        )

    def descriptors(self) -> list[Dict[str, Any]]:
        return [node.describe() for node in self.devices.values()]

    def snapshot_states(self) -> Dict[str, Dict[str, Any]]:
        return {
            device_id: node.snapshot_status()
            for device_id, node in self.devices.items()
        }

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self._stopped.wait(timeout)

    def stop(self) -> None:
        for node in reversed(tuple(self.devices.values())):
            node.stop()
        self.topic_bus.close()
        self._stopped.set()
