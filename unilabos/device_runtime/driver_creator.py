"""Backend-neutral device instance construction helpers.

These creators understand Uni-Lab resource children and PyLabRobot resource
references.  Backend adapters may optionally provide a task scheduler for
drivers whose ``setup`` method is asynchronous.
"""

from __future__ import annotations

import asyncio
import inspect
import traceback
from abc import abstractmethod
from typing import Any, Callable, Dict, Generic, List, Optional, Type, TypeVar

from unilabos.device_runtime.async_utils import schedule_async_func
from unilabos.resources.resource_tracker import (
    DeviceNodeResourceTracker,
    ResourceDictInstance,
    ResourceTreeInstance,
    ResourceTreeSet,
)
from unilabos.utils import logger
from unilabos.utils.cls_creator import create_instance_from_config

T = TypeVar("T")
TaskScheduler = Callable[[Any], Any]


class ClassCreator(Generic[T]):
    @abstractmethod
    def create_instance(self, *args: Any, **kwargs: Any) -> T:
        raise NotImplementedError


class DeviceClassCreator(Generic[T]):
    """Create a Python driver and attach its non-device child resources."""

    def __init__(
        self,
        cls: Type[T],
        children: List[ResourceDictInstance],
        resource_tracker: DeviceNodeResourceTracker,
    ) -> None:
        self.device_cls = cls
        self.device_instance: Optional[T] = None
        self.children = list(children)
        self.resource_tracker = resource_tracker

    def attach_resource(self) -> None:
        if self.device_instance is None:
            return
        for child in self.children:
            if child.res_content.type != "device":
                resource = ResourceTreeSet(
                    [ResourceTreeInstance(child)]
                ).to_plr_resources()[0]
                self.resource_tracker.add_resource(resource)

    def create_instance(self, data: Dict[str, Any]) -> T:
        self.device_instance = create_instance_from_config(
            {
                "_cls": f"{self.device_cls.__module__}:{self.device_cls.__name__}",
                "_params": dict(data),
            }
        )
        self.post_create()
        self.attach_resource()
        return self.device_instance

    def get_instance(self) -> Optional[T]:
        return self.device_instance

    def post_create(self) -> None:
        pass


class PyLabRobotCreator(DeviceClassCreator[T]):
    """Create PyLabRobot-style drivers and resolve graph child references."""

    def __init__(
        self,
        cls: Type[T],
        children: List[ResourceDictInstance],
        resource_tracker: DeviceNodeResourceTracker,
        *,
        task_scheduler: Optional[TaskScheduler] = None,
    ) -> None:
        super().__init__(cls, children, resource_tracker)
        self.task_scheduler = task_scheduler
        self.has_deserialize = hasattr(cls, "deserialize") and callable(
            getattr(cls, "deserialize")
        )
        if not self.has_deserialize:
            logger.warning(
                "类 %s 没有 deserialize 方法，将使用标准构造函数", cls.__name__
            )

    def attach_resource(self) -> None:
        # PyLabRobot resources are attached while references are resolved.
        pass

    def _process_resource_references(
        self,
        data: Any,
        processed_child_names: Dict[str, Any],
        *,
        to_dict: bool = False,
        states: Optional[Dict[str, Any]] = None,
        prefix_path: str = "",
        name_to_uuid: Optional[Dict[str, str]] = None,
    ) -> Any:
        from pylabrobot.resources import Resource

        if states is None:
            states = {}
        if isinstance(data, dict):
            if "_resource_child_name" in data:
                child_name = str(data["_resource_child_name"])
                resource = next(
                    (
                        child
                        for child in self.children
                        if child.res_content.name == child_name
                    ),
                    None,
                )
                if resource is None:
                    logger.warning("找不到资源引用 %r，保持原值不变", child_name)
                    return data
                if "_resource_type" not in data:
                    logger.debug(
                        "找不到资源类型，请补全 _resource_type %s %s",
                        self.device_cls.__name__,
                        data.keys(),
                    )
                    return resource
                try:
                    resource_instance: Resource = ResourceTreeSet(
                        [ResourceTreeInstance(resource)]
                    ).to_plr_resources()[0]
                    states[prefix_path] = resource_instance.serialize_all_state()
                    if to_dict:
                        return resource_instance.serialize()
                    processed_child_names[child_name] = resource_instance
                    self.resource_tracker.add_resource(resource_instance)
                    if name_to_uuid:
                        self.resource_tracker.loop_set_uuid(
                            resource_instance,
                            name_to_uuid,
                        )
                    return resource_instance
                except Exception as exc:  # noqa: BLE001 - report the resource path
                    logger.warning(
                        "无法加载资源类型 %s: %s",
                        data.get("_resource_type"),
                        exc,
                    )
                    logger.warning(traceback.format_exc())
                    return resource
            return {
                key: self._process_resource_references(
                    value,
                    processed_child_names,
                    to_dict=to_dict,
                    states=states,
                    prefix_path=f"{prefix_path}.{key}" if prefix_path else key,
                    name_to_uuid=name_to_uuid,
                )
                for key, value in data.items()
            }
        if isinstance(data, list):
            return [
                self._process_resource_references(
                    item,
                    processed_child_names,
                    to_dict=to_dict,
                    states=states,
                    prefix_path=f"{prefix_path}[{index}]",
                    name_to_uuid=name_to_uuid,
                )
                for index, item in enumerate(data)
            ]
        return data

    def _complete_resource_types(self, data: Dict[str, Any], callable_obj: Any) -> None:
        parameters = inspect.signature(callable_obj).parameters
        for name, value in data.items():
            if not (
                isinstance(value, dict)
                and "_resource_child_name" in value
                and "_resource_type" not in value
            ):
                continue
            parameter = parameters.get(name)
            annotation = getattr(parameter, "annotation", inspect.Parameter.empty)
            if annotation is inspect.Parameter.empty:
                continue
            annotation_name = (
                annotation
                if isinstance(annotation, str)
                else getattr(annotation, "__name__", str(annotation))
            )
            value["_resource_type"] = f"{self.device_cls.__module__}:{annotation_name}"
            logger.debug("自动补充 _resource_type: %s", value["_resource_type"])

    def create_instance(self, data: Dict[str, Any]) -> Optional[T]:
        data = dict(data)
        deserialize_error: Optional[BaseException] = None
        deserialize_stack = ""

        def collect_name_to_uuid(
            children: List[ResourceDictInstance],
            result: Dict[str, str],
        ) -> None:
            for child in children:
                result[child.res_content.name] = child.res_content.uuid
                collect_name_to_uuid(child.children, result)

        name_to_uuid: Dict[str, str] = {}
        collect_name_to_uuid(self.children, name_to_uuid)

        if self.has_deserialize:
            deserialize = getattr(self.device_cls, "deserialize")
            self._complete_resource_types(data, deserialize)
            states: Dict[str, Any] = {}
            processed_data = self._process_resource_references(
                data,
                {},
                to_dict=True,
                states=states,
                name_to_uuid=name_to_uuid,
            )
            try:
                self.device_instance = deserialize(**processed_data)
                self.resource_tracker.loop_set_uuid(
                    self.device_instance,
                    name_to_uuid,
                )
                all_states = self.device_instance.serialize_all_state()
                for state in states.values():
                    for key, value in all_states.items():
                        state.setdefault(key, value)
                    self.device_instance.load_all_state(state)
                self.resource_tracker.add_resource(self.device_instance)
                self.post_create()
                return self.device_instance
            except Exception as exc:  # noqa: BLE001 - fallback to constructor
                deserialize_error = exc
                deserialize_stack = traceback.format_exc()

        try:
            self._complete_resource_types(data, self.device_cls.__init__)
            processed_children: Dict[str, Any] = {}
            processed_data = self._process_resource_references(
                data,
                processed_children,
                name_to_uuid=name_to_uuid,
            )
            used_children = set(processed_children)
            self.children = [
                child
                for child in self.children
                if child.res_content.name not in used_children
            ]
            return super().create_instance(processed_data)
        except Exception as exc:  # noqa: BLE001 - include both creation attempts
            logger.error("PyLabRobot 创建实例失败: %s", exc)
            logger.error("PyLabRobot 创建实例堆栈: %s", traceback.format_exc())
            if deserialize_error is not None:
                logger.error("PyLabRobot 反序列化失败: %s", deserialize_error)
                logger.error("PyLabRobot 反序列化堆栈: %s", deserialize_stack)
            return None

    def post_create(self) -> None:
        setup = getattr(self.device_instance, "setup", None)
        if self.task_scheduler is None or not asyncio.iscoroutinefunction(setup):
            return

        future = schedule_async_func(
            self.task_scheduler,
            setup,
            error_callback=logger.error,
        )

        def setup_done(done_future: Any) -> None:
            try:
                done_future.result()
            except BaseException:
                return
            from pylabrobot.resources import set_volume_tracking

            set_volume_tracking(enabled=True)
            logger.debug("PyLabRobot 设备实例 %s 设置完成", self.device_instance)
            from unilabos.config.config import BasicConfig

            if not BasicConfig.vis_2d_enable:
                return
            from pylabrobot.visualizer.visualizer import Visualizer

            visualizer = Visualizer(resource=self.device_instance, open_browser=True)
            schedule_async_func(
                self.task_scheduler,
                visualizer.setup,
                error_callback=logger.error,
            )

        future.add_done_callback(setup_done)


class WorkstationNodeCreator(DeviceClassCreator[T]):
    """Create a workstation driver and its optional PyLabRobot deck."""

    def __init__(
        self,
        cls: Type[T],
        children: List[ResourceDictInstance],
        resource_tracker: DeviceNodeResourceTracker,
        *,
        task_scheduler: Optional[TaskScheduler] = None,
    ) -> None:
        super().__init__(cls, children, resource_tracker)
        self.task_scheduler = task_scheduler

    def create_instance(self, data: Dict[str, Any]) -> T:
        params = dict(data)
        params["children"] = self.children
        deck_data = params.get("deck")
        if deck_data:
            from pylabrobot.resources import Deck

            params["deck"] = PyLabRobotCreator(
                Deck,
                self.children,
                self.resource_tracker,
                task_scheduler=self.task_scheduler,
            ).create_instance(deck_data)
        else:
            params["deck"] = None
        return super().create_instance(params)


def uses_pylabrobot_creator(driver_class: Type[Any]) -> bool:
    """Return whether a driver needs graph child/resource resolution."""

    return driver_class.__module__.startswith(
        "pylabrobot"
    ) or driver_class.__name__ in {
        "LiquidHandlerAbstract",
        "LiquidHandlerBiomek",
        "PRCXI9300Handler",
        "TransformXYZHandler",
        "OpcUaClient",
    }


__all__ = [
    "ClassCreator",
    "DeviceClassCreator",
    "PyLabRobotCreator",
    "WorkstationNodeCreator",
    "uses_pylabrobot_creator",
]
