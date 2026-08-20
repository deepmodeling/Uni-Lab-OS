"""HostLink backend 的单进程 Python 驱动执行引擎。"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import threading
import types
from dataclasses import dataclass, field
from typing import (
    Any,
    Awaitable,
    Callable,
    Coroutine,
    Dict,
    Iterable,
    Optional,
    Type,
    Union,
    get_args,
    get_origin,
)

from unilabos.device_runtime.action import ActionContext
from unilabos.device_runtime.driver_creator import (
    select_driver_creator,
)
from unilabos.device_runtime.node import BackendCapabilityError, DeviceNode
from unilabos.device_runtime.resource import ResourceService
from unilabos.device_runtime.service import LocalServiceBus
from unilabos.device_runtime.topic import LocalTopicBus, message_to_value
from unilabos.registry.decorators import get_topic_config
from unilabos.resources.resource_tracker import (
    DeviceNodeResourceTracker,
    PARAM_SAMPLE_UUIDS,
    ResourceDictInstance,
    ResourceTreeSet,
)
from unilabos.utils.decorator import get_all_subscriptions


_MISSING = object()


def _read_path(value: Any, path: str) -> Any:
    """Read a ROS-style dotted/array path from a JSON-compatible value."""

    parts = str(path or "").split(".")

    def read(current: Any, index: int) -> Any:
        if index >= len(parts):
            return current
        part = parts[index]
        is_array = part.endswith("[]")
        name = part[:-2] if is_array else part
        if isinstance(current, dict):
            child = current.get(name, _MISSING)
        else:
            child = getattr(current, name, _MISSING)
        if child is _MISSING:
            return _MISSING
        if is_array:
            if not isinstance(child, (list, tuple)):
                return _MISSING
            if index == len(parts) - 1:
                return list(child)
            values = [read(item, index + 1) for item in child]
            return _MISSING if any(item is _MISSING for item in values) else values
        return read(child, index + 1)

    return read(value, 0)


def _write_path(target: Dict[str, Any], path: str, value: Any) -> None:
    """Write one mapped value to a dotted result/feedback path."""

    parts = str(path or "").split(".")
    if not parts or not parts[0]:
        return
    current: Any = target
    for index, part in enumerate(parts):
        is_array = part.endswith("[]")
        name = part[:-2] if is_array else part
        last = index == len(parts) - 1
        if is_array:
            values = list(value) if isinstance(value, (list, tuple)) else []
            if last:
                current[name] = values
                return
            remaining = ".".join(parts[index + 1 :])
            items: list[Dict[str, Any]] = []
            for item_value in values:
                item: Dict[str, Any] = {}
                _write_path(item, remaining, item_value)
                items.append(item)
            current[name] = items
            return
        if last:
            current[name] = value
            return
        current = current.setdefault(name, {})


@dataclass(frozen=True)
class _StatusBinding:
    source_name: str
    publish_name: str
    period: float
    print_publish: bool
    qos: int
    configured: bool


def _resource_slot_shape(annotation: Any) -> Optional[str]:
    """Return ``single``/``list`` for ResourceSlot-compatible annotations."""

    if annotation is inspect.Parameter.empty:
        return None
    if isinstance(annotation, str):
        normalized = annotation.replace(" ", "").replace("typing.", "")
        if "ResourceSlot" not in normalized:
            return None
        if normalized.startswith(("list[", "List[")):
            return "list"
        return "single"

    origin = get_origin(annotation)
    if origin is list:
        arguments = get_args(annotation)
        if arguments and _resource_slot_shape(arguments[0]) == "single":
            return "list"
        return None
    if origin in (Union, types.UnionType):
        shapes = {
            shape
            for item in get_args(annotation)
            if item is not type(None) and (shape := _resource_slot_shape(item))
        }
        return shapes.pop() if len(shapes) == 1 else None

    name = str(getattr(annotation, "__qualname__", "") or "")
    module = str(getattr(annotation, "__module__", "") or "")
    return "single" if name == "ResourceSlot" and module else None


def instantiate_driver(
    driver_class: Type[Any],
    device_id: str,
    config: Optional[Dict[str, Any]] = None,
    *,
    device_config: Any = None,
    resource_tracker: Optional[DeviceNodeResourceTracker] = None,
) -> Any:
    """使用 ``config`` 对象或展开参数实例化驱动。

    新驱动通常接收 ``device_id`` 和 ``config``，旧驱动则把配置项直接声明为构造参数。
    HostLink 运行时不导入 ROS 设备包装器，同时兼容这两种形式。
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
    selection = select_driver_creator(
        driver_class,
        children=list(device_config.children) if device_config is not None else [],
        resource_tracker=resource_tracker or DeviceNodeResourceTracker(),
    )
    driver = selection.creator.create_instance(kwargs)
    if driver is None:
        raise RuntimeError(f"HostLink 设备 {device_id!r} 的驱动实例创建失败")
    return driver


class HostLinkDeviceNode(DeviceNode):
    """向驱动提供异步辅助能力的轻量节点适配器。"""

    def __init__(
        self,
        driver: Any,
        device_id: str,
        *,
        resource_uuid: str = "",
        registry_name: str = "",
        display_name: str = "",
        action_names: Iterable[str] = (),
        action_value_mappings: Optional[Dict[str, Any]] = None,
        status_names: Iterable[str] = (),
        resource_tracker: Optional[DeviceNodeResourceTracker] = None,
    ) -> None:
        self.driver = driver
        self.device_id = device_id
        self.backend_name = "hostlink"
        self.namespace = self.get_namespace()
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
        self.action_value_mappings = message_to_value(dict(action_value_mappings or {}))
        self.status_names = tuple(
            sorted({str(name).strip() for name in status_names if str(name).strip()})
        )
        self._logger = logging.getLogger(f"unilabos.hostlink.{device_id}")
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_loop,
            name=f"hostlink-driver-{device_id}",
            daemon=True,
        )
        self._loop_ready = threading.Event()
        self._started = False
        self._status_lock = threading.Lock()
        self._decorated_subscriptions: list[Any] = []
        self._status_bindings = self._build_status_bindings()
        self.resource_tracker = resource_tracker or DeviceNodeResourceTracker()

    _ROS2_RUNTIME_ATTRIBUTES = frozenset(
        {
            "create_guard_condition",
            "create_action_server",
            "create_action_client",
            "get_publishers_info_by_topic",
            "get_subscriptions_info_by_topic",
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

    def create_device(self, device_id: str, config: Any) -> dict[str, Any]:
        """使用与启动阶段相同的工厂动态创建普通设备或子设备。"""

        runtime = self.__dict__.get("_hostlink_runtime")
        if runtime is None:
            return {"success": False, "error": "HostLink runtime is unavailable"}
        try:
            if isinstance(config, ResourceDictInstance):
                device_config = config
            else:
                payload = dict(config or {})
                payload.setdefault("id", device_id)
                payload.setdefault("type", "device")
                device_config = ResourceDictInstance.get_resource_instance_from_dict(
                    payload
                )
            node = runtime.add_device_from_config(device_id, device_config)
            return {
                "success": True,
                "device_id": node.device_id,
                "registry_name": node.registry_name,
            }
        except Exception as exc:  # noqa: BLE001 - service boundary returns detail
            self._logger.exception("动态创建设备失败：%s", device_id)
            return {"success": False, "error": str(exc)}

    def destroy_device(self, device_id: str) -> dict[str, Any]:
        runtime = self.__dict__.get("_hostlink_runtime")
        if runtime is None:
            return {"success": False, "error": "HostLink runtime is unavailable"}
        removed = runtime.remove_device(device_id)
        return {
            "success": removed,
            "device_id": str(device_id),
            **({} if removed else {"error": f"device {device_id!r} not found"}),
        }

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
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
            raise RuntimeError(f"HostLink 设备 {self.device_id!r} 事件循环启动超时")
        self._started = True
        try:
            if self.__dict__.get("_device_service_bus") is not None:
                self.setup_material_sync_service()
            if hasattr(self.driver, "post_init"):
                self.driver.post_init(self)
            self._setup_decorated_subscriptions()
            setup = getattr(self.driver, "setup", None)
            if callable(setup):
                self._call(setup, _wait_timeout=30)
            initialize = getattr(self.driver, "initialize", None)
            if callable(initialize):
                self._call(initialize, _wait_timeout=30)
            self._setup_status_publishers()
        except AttributeError as exc:
            self.stop()
            self._raise_backend_attribute_error(exc)
        except Exception:
            self.stop()
            raise
        self._logger.info("HostLink 设备已就绪：%s", self.device_id)

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
            if not str(topic).startswith("/"):
                raise ValueError(f"@subscribe topic 必须是绝对路径：{topic!r}")
            self._decorated_subscriptions.append(
                self.create_subscription(
                    config.get("msg_type"),
                    topic,
                    method,
                    config.get("qos", 10),
                    trigger_when_change=config.get("trigger_when_change", False),
                )
            )

    @staticmethod
    def _topic_config_for(driver_class: type, source_name: str) -> Dict[str, Any]:
        try:
            member = inspect.getattr_static(driver_class, source_name)
        except AttributeError:
            return {}
        if isinstance(member, property):
            return get_topic_config(member.fget) if member.fget is not None else {}
        return get_topic_config(member)

    def _build_status_bindings(self) -> tuple[_StatusBinding, ...]:
        driver_class = type(self.driver)
        bindings: list[_StatusBinding] = []
        for status_name in self.status_names:
            source_name = status_name
            try:
                exact = inspect.getattr_static(driver_class, status_name)
            except AttributeError:
                exact = _MISSING
            getter_name = f"get_{status_name}"
            if not isinstance(exact, property) and hasattr(self.driver, getter_name):
                source_name = getter_name

            config = self._topic_config_for(driver_class, source_name)
            publish_name = str(config.get("name") or status_name)
            period = float(
                config["period"] if config.get("period") is not None else 5.0
            )
            if period <= 0:
                raise ValueError(
                    f"设备 {self.device_id!r} 状态 {status_name!r} 的发布周期必须大于 0"
                )
            bindings.append(
                _StatusBinding(
                    source_name=source_name,
                    publish_name=publish_name,
                    period=period,
                    print_publish=bool(config.get("print_publish", False)),
                    qos=int(config["qos"] if config.get("qos") is not None else 10),
                    configured=bool(config),
                )
            )
        return tuple(bindings)

    def _read_driver_value(self, source_name: str) -> Any:
        value = getattr(self.driver, source_name)
        return self._call(value) if callable(value) else value

    async def _read_driver_value_async(self, source_name: str) -> Any:
        value = getattr(self.driver, source_name)
        if callable(value):
            value = value()
        return await value if inspect.isawaitable(value) else value

    async def _publish_status_binding(self, binding: _StatusBinding) -> None:
        try:
            value = await self._read_driver_value_async(binding.source_name)
            self.emit_status(binding.publish_name, value)
            if binding.print_publish:
                self._logger.info(
                    "状态发布：%s.%s = %r",
                    self.device_id,
                    binding.publish_name,
                    value,
                )
        except Exception:  # noqa: BLE001 - one status must not stop other timers
            self._logger.exception(
                "状态发布失败：%s.%s",
                self.device_id,
                binding.publish_name,
            )

    def _setup_status_publishers(self) -> None:
        for binding in self._status_bindings:
            self.create_timer(
                binding.period,
                lambda current=binding: self._publish_status_binding(current),
            )

    def _resolve_action(
        self,
        action_name: str,
        *,
        action_context: Optional[ActionContext] = None,
        **kwargs: Any,
    ) -> tuple[Callable[..., Any], ActionContext, Dict[str, Any], Dict[str, Any]]:
        if not self._started:
            raise RuntimeError(f"HostLink 设备 {self.device_id!r} 尚未启动")
        action_name = str(action_name or "").strip()
        if action_name.startswith("_") or (
            self.action_names and action_name not in self.action_names
        ):
            raise AttributeError(
                f"HostLink 设备 {self.device_id!r} 没有动作 {action_name!r}"
            )
        method_name = action_name.removeprefix("auto-")
        action = getattr(self.driver, method_name, None)
        if not callable(action):
            raise AttributeError(
                f"HostLink 设备 {self.device_id!r} 没有动作 {action_name!r}"
            )
        signature = inspect.signature(action)
        mapping = self.action_value_mappings.get(action_name, {})
        if not isinstance(mapping, dict):
            mapping = {}
        kwargs = self._map_action_arguments(signature, mapping, kwargs)
        kwargs = self._resolve_action_resources(signature, kwargs)
        context = action_context or ActionContext()
        if "action_context" in signature.parameters:
            kwargs.setdefault("action_context", context)
        return action, context, kwargs, mapping

    @staticmethod
    def _map_action_arguments(
        signature: inspect.Signature,
        mapping: Dict[str, Any],
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Apply ``@action(goal={wire_field: driver_parameter})`` without ROS."""

        goal_mapping = mapping.get("goal")
        if not isinstance(goal_mapping, dict) or not goal_mapping:
            return dict(arguments)
        accepts_kwargs = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
        mapped = dict(arguments)
        for wire_name, parameter_name in goal_mapping.items():
            if not isinstance(parameter_name, str):
                continue
            target_name = parameter_name.removesuffix("[]")
            if target_name not in signature.parameters and not accepts_kwargs:
                # 兼容旧注册表中仅作为描述信息保存、与驱动签名不一致的映射。
                continue
            value = _read_path(arguments, str(wire_name))
            if value is _MISSING:
                continue
            source_name = str(wire_name)
            source_root = source_name.split(".", 1)[0].removesuffix("[]")
            if source_root != target_name:
                mapped.pop(source_root, None)
            mapped[target_name] = value
        return mapped

    def _resolve_resource_slot(self, value: Any) -> Any:
        """Resolve one ResourceSlot from this device's authoritative snapshot."""

        from pylabrobot.resources import Resource

        if isinstance(value, Resource):
            return value

        if isinstance(value, list):
            if not value:
                raise ValueError("ResourceSlot 扁平资源树不能为空")
            if not all(isinstance(item, dict) for item in value):
                raise TypeError("ResourceSlot 扁平资源树必须由对象节点组成")
            tree_set = ResourceTreeSet.from_raw_dict_list(value)
            if len(tree_set.trees) != 1:
                raise ValueError(
                    "单个 ResourceSlot 必须恰好包含一棵资源树，"
                    f"实际为 {len(tree_set.trees)} 棵"
                )
            resource = tree_set.to_plr_resources()[0]
            matches = self.resource_tracker.figure_resource(resource, try_mode=True)
            if len(matches) > 1:
                raise ValueError(f"ResourceSlot 匹配到多个本地资源：{matches}")
            return matches[0] if matches else resource

        if not isinstance(value, dict):
            raise TypeError(
                "ResourceSlot 必须是资源实例、{uuid/id/name} 引用或扁平资源树"
            )
        if not any(value.get(key) for key in ("uuid", "id", "name")):
            raise ValueError("ResourceSlot 引用缺少 uuid、id 或 name")
        matches = self.resource_tracker.figure_resource(value, try_mode=True)
        if not matches:
            identity = value.get("uuid") or value.get("id") or value.get("name")
            raise ValueError(
                f"设备 {self.device_id!r} 的本地资源快照中找不到 {identity!r}；"
                "应由微后端下发完整资源树，或把资源挂到该设备图中"
            )
        if len(matches) > 1:
            raise ValueError(f"ResourceSlot 匹配到多个本地资源：{matches}")
        return matches[0]

    def _resolve_action_resources(
        self,
        signature: inspect.Signature,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Give HostLink drivers the same ResourceSlot input shape as ROS2."""

        resolved = dict(arguments)
        for name, parameter in signature.parameters.items():
            if name not in resolved:
                continue
            shape = _resource_slot_shape(parameter.annotation)
            if shape == "single":
                resolved[name] = self._resolve_resource_slot(resolved[name])
            elif shape == "list":
                values = resolved[name]
                if not isinstance(values, list):
                    raise TypeError(f"ResourceSlot 列表参数 {name!r} 必须是列表")
                resolved[name] = [
                    self._resolve_resource_slot(value) for value in values
                ]

        sample_uuids = resolved.get(PARAM_SAMPLE_UUIDS)
        if isinstance(sample_uuids, dict):
            resolved[PARAM_SAMPLE_UUIDS] = {
                sample_uuid: self.resource_tracker.uuid_to_resources.get(
                    str(resource_uuid), resource_uuid
                )
                for sample_uuid, resource_uuid in sample_uuids.items()
            }
        return resolved

    async def _feedback_values(self, mapping: Dict[str, Any]) -> Dict[str, Any]:
        feedback_mapping = mapping.get("feedback")
        if not isinstance(feedback_mapping, dict):
            return {}
        values: Dict[str, Any] = {}
        for wire_name, source_name in feedback_mapping.items():
            if not isinstance(source_name, str):
                continue
            attr_name = source_name.removesuffix("[]")
            getter_name = f"get_{attr_name}"
            try:
                if hasattr(self.driver, getter_name):
                    value = await self._read_driver_value_async(getter_name)
                else:
                    value = await self._read_driver_value_async(attr_name)
            except Exception:  # noqa: BLE001 - feedback is best effort
                self._logger.exception(
                    "动作反馈读取失败：%s.%s",
                    self.device_id,
                    attr_name,
                )
                continue
            _write_path(values, str(wire_name), message_to_value(value))
        return values

    async def _poll_action_feedback(
        self,
        context: ActionContext,
        mapping: Dict[str, Any],
    ) -> None:
        interval = float(mapping.get("feedback_interval", 1.0) or 1.0)
        if interval <= 0:
            raise ValueError("feedback_interval 必须大于 0")
        while True:
            await asyncio.sleep(interval)
            context.raise_if_cancelled()
            context.publish_feedback(await self._feedback_values(mapping))

    async def _execute_action(
        self,
        action: Callable[..., Any],
        context: ActionContext,
        kwargs: Dict[str, Any],
        mapping: Dict[str, Any],
    ) -> Any:
        feedback_task: Optional[asyncio.Task[Any]] = None
        if mapping.get("feedback") and context.feedback_callback is not None:
            feedback_task = asyncio.create_task(
                self._poll_action_feedback(context, mapping)
            )
        try:
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
        finally:
            if feedback_task is not None:
                feedback_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await feedback_task

    def call_action(
        self,
        action_name: str,
        *,
        action_context: Optional[ActionContext] = None,
        **kwargs: Any,
    ) -> Any:
        """Run an action synchronously while using the device event loop."""

        action, context, call_kwargs, mapping = self._resolve_action(
            action_name,
            action_context=action_context,
            **kwargs,
        )
        future = asyncio.run_coroutine_threadsafe(
            self._execute_action(action, context, call_kwargs, mapping),
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

        action, context, call_kwargs, mapping = self._resolve_action(
            action_name,
            action_context=action_context,
            **kwargs,
        )
        try:
            if asyncio.get_running_loop() is self._loop:
                return await self._execute_action(
                    action, context, call_kwargs, mapping
                )
            future = asyncio.run_coroutine_threadsafe(
                self._execute_action(action, context, call_kwargs, mapping),
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
            for binding in self._status_bindings:
                try:
                    result[binding.publish_name] = self._read_driver_value(
                        binding.source_name
                    )
                    if not binding.configured:
                        self.emit_status(
                            binding.publish_name,
                            result[binding.publish_name],
                        )
                except Exception as exc:  # noqa: BLE001 - 状态快照需部分成功
                    errors[binding.publish_name] = str(exc)
        if errors:
            result["_errors"] = errors
        return result

    def describe(self) -> Dict[str, Any]:
        descriptor = {
            "id": self.device_id,
            "registry_name": self.registry_name,
            "display_name": self.display_name,
            "actions": list(self.action_names),
            "status_fields": [
                binding.publish_name for binding in self._status_bindings
            ],
        }
        if self.action_value_mappings:
            descriptor["action_value_mappings"] = self.action_value_mappings
        system_parameters: Dict[str, list[str]] = {}
        for action_name in self.action_names:
            method = getattr(self.driver, action_name.removeprefix("auto-"), None)
            if not callable(method):
                continue
            try:
                parameters = inspect.signature(method).parameters
            except (TypeError, ValueError):
                continue
            if PARAM_SAMPLE_UUIDS in parameters:
                system_parameters[action_name] = [PARAM_SAMPLE_UUIDS]
        if system_parameters:
            descriptor["system_parameters"] = system_parameters
        services = self.service_names()
        if services:
            descriptor["services"] = services
        if self.resource_uuid:
            descriptor["resource_uuid"] = self.resource_uuid
        return descriptor

    def stop(self) -> None:
        if not self._started:
            return
        for subscription in self._decorated_subscriptions:
            subscription.destroy()
        self._decorated_subscriptions.clear()
        for timer in tuple(self.__dict__.get("_device_timers", [])):
            self.destroy_timer(timer)
        # 让 timer cancellation 在关闭 event loop 前真正落地，避免遗留 pending Task。
        asyncio.run_coroutine_threadsafe(asyncio.sleep(0), self._loop).result(
            timeout=2
        )
        for service in tuple(self.__dict__.get("_device_services", [])):
            self.destroy_service(service)
        cleanup = getattr(self.driver, "cleanup", None)
        if not callable(cleanup):
            cleanup = getattr(self.driver, "stop", None)
        if callable(cleanup):
            try:
                self._call(cleanup, _wait_timeout=10)
            except Exception:
                self._logger.exception("HostLink 设备清理失败：%s", self.device_id)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._loop_thread.join(timeout=5)
        self._loop.close()
        self._started = False


@dataclass(frozen=True)
class HostLinkDriverSpec:
    device_id: str
    driver_class: Type[Any]
    config: Dict[str, Any]
    registry_name: str = ""
    action_names: tuple[str, ...] = ()
    action_value_mappings: Dict[str, Any] = field(default_factory=dict)
    status_names: tuple[str, ...] = ()
    display_name: str = ""
    resource_uuid: str = ""
    device_config: Any = None


class HostLinkLocalRuntime:
    """管理一个 HostLink backend 进程内的全部本地驱动实例。"""

    def __init__(self) -> None:
        self.backend_name = "hostlink"
        self.devices: dict[str, HostLinkDeviceNode] = {}
        self.topic_bus = LocalTopicBus()
        self.service_bus = LocalServiceBus()
        self._resource_service: ResourceService | None = None
        self._device_change_listeners: list[
            Callable[[str, HostLinkDeviceNode], None]
        ] = []
        self._started = False
        self._stopped = threading.Event()

    def add_device_change_listener(
        self,
        callback: Callable[[str, HostLinkDeviceNode], None],
    ) -> None:
        if callback not in self._device_change_listeners:
            self._device_change_listeners.append(callback)

    def remove_device_change_listener(
        self,
        callback: Callable[[str, HostLinkDeviceNode], None],
    ) -> None:
        with contextlib.suppress(ValueError):
            self._device_change_listeners.remove(callback)

    def _notify_device_change(self, event: str, node: HostLinkDeviceNode) -> None:
        for callback in tuple(self._device_change_listeners):
            try:
                callback(event, node)
            except Exception:  # noqa: BLE001 - 一个监听器不能破坏设备生命周期
                logging.getLogger(__name__).exception(
                    "HostLink 设备变更监听失败：event=%s device=%s",
                    event,
                    node.device_id,
                )

    def add_driver(self, spec: HostLinkDriverSpec) -> HostLinkDeviceNode:
        if spec.device_id in self.devices:
            raise ValueError(f"HostLink 设备 ID 重复：{spec.device_id}")
        resource_tracker = DeviceNodeResourceTracker()
        driver = instantiate_driver(
            spec.driver_class,
            spec.device_id,
            spec.config,
            device_config=spec.device_config,
            resource_tracker=resource_tracker,
        )
        node = HostLinkDeviceNode(
            driver,
            spec.device_id,
            resource_uuid=spec.resource_uuid,
            registry_name=spec.registry_name,
            display_name=spec.display_name,
            action_names=spec.action_names,
            action_value_mappings=spec.action_value_mappings,
            status_names=spec.status_names,
            resource_tracker=resource_tracker,
        )
        node.set_action_router(self)
        node.set_topic_bus(self.topic_bus)
        node.set_service_bus(self.service_bus)
        node.children = list(spec.device_config.children) if spec.device_config else []
        node.__dict__["_hostlink_runtime"] = self
        if self._resource_service is not None:
            node.set_resource_service(self._resource_service)
        self.devices[spec.device_id] = node
        self._notify_device_change("added", node)
        try:
            if self._started:
                node.start()
        except Exception:
            self.devices.pop(spec.device_id, None)
            self._notify_device_change("removed", node)
            raise
        return node

    def add_device_from_config(
        self,
        device_id: str,
        device_config: ResourceDictInstance,
    ) -> HostLinkDeviceNode:
        from unilabos.device_runtime.definition import resolve_device_definition

        definition = resolve_device_definition(
            device_id,
            device_config,
            backend_name="hostlink",
        )
        return self.add_driver(
            HostLinkDriverSpec(
                device_id=device_id,
                driver_class=definition.driver_class,
                config=definition.runtime_config,
                registry_name=definition.registry_name,
                display_name=definition.display_name,
                action_names=tuple(definition.action_value_mappings),
                action_value_mappings=definition.action_value_mappings,
                status_names=tuple(definition.status_types),
                resource_uuid=definition.resource_uuid,
                device_config=device_config,
            )
        )

    def remove_device(self, device_id: str) -> bool:
        normalized = self._normalize_device_id(device_id)
        node = self.devices.pop(normalized, None)
        if node is None:
            return False
        node.stop()
        self._notify_device_change("removed", node)
        return True

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
        started: list[HostLinkDeviceNode] = []
        try:
            for node in self.devices.values():
                node.start()
                started.append(node)
            self._started = True
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
            raise KeyError(f"未知 HostLink 设备：{device_id}") from exc
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
            raise KeyError(f"未知 HostLink 设备：{device_id}") from exc
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

    def request_stop(self) -> None:
        """Wake the backend owner so it can run its normal shutdown path."""

        self._stopped.set()

    def stop(self) -> None:
        self._started = False
        for node in reversed(tuple(self.devices.values())):
            node.stop()
        self.topic_bus.close()
        self._stopped.set()
