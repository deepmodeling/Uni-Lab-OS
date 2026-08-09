"""供 ``basic`` backend 使用的单进程运行时。"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Coroutine, Dict, Optional, Type


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


class BasicDeviceNode:
    """向驱动提供异步辅助能力的轻量节点适配器。"""

    def __init__(self, driver: Any, device_id: str) -> None:
        self.driver = driver
        self.device_id = device_id
        self._logger = logging.getLogger(f"unilabos.basic.{device_id}")
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(
            target=self._run_loop,
            name=f"basic-driver-{device_id}",
            daemon=True,
        )
        self._started = False

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    async def sleep(self, rel_time: float, callback_group: Any = None) -> None:
        await asyncio.sleep(rel_time)

    def lab_logger(self) -> logging.Logger:
        return self._logger

    def create_task(self, coroutine: Coroutine[Any, Any, Any]):
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop)

    async def update_resource(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self._logger.debug("Basic backend 不发布资源更新")
        return {}

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
        self._started = True
        try:
            if hasattr(self.driver, "post_init"):
                self.driver.post_init(self)
            initialize = getattr(self.driver, "initialize", None)
            if callable(initialize):
                self._call(initialize, _wait_timeout=30)
        except Exception:
            self.stop()
            raise
        self._logger.info("Basic 设备已就绪：%s", self.device_id)

    def call_action(self, action_name: str, **kwargs: Any) -> Any:
        if not self._started:
            raise RuntimeError(f"Basic 设备 {self.device_id!r} 尚未启动")
        action = getattr(self.driver, action_name, None)
        if not callable(action) or action_name.startswith("_"):
            raise AttributeError(
                f"Basic 设备 {self.device_id!r} 没有动作 {action_name!r}"
            )
        return self._call(action, **kwargs)

    def stop(self) -> None:
        if not self._started:
            return
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


class BasicRuntime:
    """管理一个 Basic backend 进程内的全部驱动实例。"""

    def __init__(self) -> None:
        self.devices: dict[str, BasicDeviceNode] = {}
        self._stopped = threading.Event()

    def add_driver(self, spec: BasicDriverSpec) -> BasicDeviceNode:
        if spec.device_id in self.devices:
            raise ValueError(f"Basic 设备 ID 重复：{spec.device_id}")
        driver = instantiate_driver(spec.driver_class, spec.device_id, spec.config)
        node = BasicDeviceNode(driver, spec.device_id)
        self.devices[spec.device_id] = node
        return node

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

    def call_action(self, device_id: str, action_name: str, **kwargs: Any) -> Any:
        try:
            node = self.devices[device_id]
        except KeyError as exc:
            raise KeyError(f"未知 Basic 设备：{device_id}") from exc
        return node.call_action(action_name, **kwargs)

    def wait(self, timeout: Optional[float] = None) -> bool:
        return self._stopped.wait(timeout)

    def stop(self) -> None:
        for node in reversed(tuple(self.devices.values())):
            node.stop()
        self._stopped.set()
