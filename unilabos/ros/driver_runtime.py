"""OS 的设备运行上下文实现；领域包仅依赖 unilabos.runtime。"""

import asyncio
import inspect
import json
import math
from collections.abc import Callable, Mapping
from concurrent.futures import Future, TimeoutError
from contextvars import copy_context
from threading import Event, Thread
from typing import Any

from unilabos.runtime import DeviceInvocationError
from unilabos.runtime.commands import CommandJournal, LegacyCommandTables, open_command_journal
from unilabos.utils.exception import DeviceActionError


class DriverRuntime:
    """把本地依赖与既有 ROS 通道收敛为有生命周期的依赖调用接口。"""

    def __init__(
        self, *, device_id: str, device_uuid: str,
        resolve_local: Callable[[str], Any | None],
        invoke_remote: Callable[..., Any], remote_ready: Callable[[], bool],
        async_loop: Callable[[], asyncio.AbstractEventLoop | None] | None = None,
    ) -> None:
        self._device_id = device_id
        self._device_uuid = device_uuid
        self._resolve_local = resolve_local
        self._invoke_remote = invoke_remote
        self._remote_ready = remote_ready
        self._async_loop = async_loop
        self._closed = Event()

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def device_uuid(self) -> str:
        return self._device_uuid

    def bind(self, driver: Any) -> None:
        """新驱动绑定失败即关闭上下文，交由激活入口清理节点。"""
        try:
            driver.bind_runtime(self)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        """禁止后续调用；不把已有物理调用伪装成已取消。"""
        self._closed.set()

    def open_command_journal(
        self, name: str, path: str, *, legacy_tables: LegacyCommandTables | None = None,
    ) -> CommandJournal:
        if self._closed.is_set():
            raise RuntimeError("设备上下文已关闭")
        if not name or ":" in name:
            raise ValueError("命令日志名称不能为空或包含冒号")
        return open_command_journal(
            path, namespace=f"{self.device_uuid}:{name}", legacy_tables=legacy_tables,
        )

    def invoke(
        self, device_id: str, operation: str, arguments: Mapping[str, Any] | None = None,
        *, timeout: float | None = 300.0, server_wait_timeout: float = 10.0,
    ) -> Any:
        device_id = device_id.removeprefix("/devices/").lstrip("/")
        operation = operation.removeprefix("auto-")
        if self._closed.is_set():
            raise DeviceInvocationError(device_id, operation, "closed")
        if self._async_loop is not None:
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            if running_loop is not None and running_loop is self._async_loop():
                raise RuntimeError("不能在设备事件循环内同步等待依赖；请使用 asyncio.to_thread")
        if not device_id or not operation or operation.startswith("_"):
            raise ValueError("依赖设备和公开操作名称不能为空，且不能调用私有方法")
        # 保留既有 PLC wait 的无限等待语义，拒绝 NaN 和负预算。
        if timeout is not None and (math.isnan(timeout) or timeout <= 0):
            raise ValueError("timeout 必须为正数或 None")
        if not math.isfinite(server_wait_timeout) or server_wait_timeout < 0:
            raise ValueError("server_wait_timeout 必须为非负有限值")
        wait_timeout = None if timeout is None or math.isinf(timeout) else timeout
        kwargs = dict(arguments or {})
        driver = self._resolve_local(device_id)
        if driver is not None:
            method = getattr(driver, operation, None)
            if not callable(method):
                raise DeviceInvocationError(device_id, operation, "unavailable", "依赖没有此操作")
            call = lambda: method(**kwargs)
        else:
            if not self._remote_ready():
                raise DeviceInvocationError(device_id, operation, "not_ready", "初始化期依赖尚未就绪")
            call = lambda: self._invoke_remote(
                device_id, operation, kwargs, timeout=wait_timeout,
                server_wait_timeout=min(server_wait_timeout, wait_timeout)
                if wait_timeout is not None else server_wait_timeout,
            )
        future: Future[Any] = Future()
        context = copy_context()

        async def await_result(result: Any) -> Any:
            return await result

        def invoke_and_resolve() -> Any:
            result = call()
            if not inspect.isawaitable(result):
                return result
            # 生产驱动复用既有 ROS2 设备循环，避免连接/Future 跨循环失效。
            # 独立测试适配器未提供循环时才创建隔离循环。
            if self._async_loop is None:
                return asyncio.run(await_result(result))
            loop = self._async_loop()
            if loop is None or loop.is_closed() or not loop.is_running():
                if inspect.iscoroutine(result):
                    result.close()
                raise DeviceInvocationError(device_id, operation, "not_ready", "设备事件循环未运行")
            return asyncio.run_coroutine_threadsafe(await_result(result), loop).result()

        def run() -> None:
            try:
                future.set_result(context.run(invoke_and_resolve))
            except BaseException as exc:
                future.set_exception(exc)

        # 同步驱动无法被安全强杀。超时停止等待，后台操作可能仍在执行；
        # 复制 ContextVar，避免线程切换丢失可信作业身份。
        Thread(target=run, name=f"dependency-{device_id}", daemon=True).start()
        try:
            return future.result(timeout=wait_timeout)
        except TimeoutError as exc:
            raise DeviceInvocationError(device_id, operation, "timeout", "等待预算耗尽",
                                        outcome_unknown=True) from exc
        except DeviceActionError as exc:
            raise DeviceInvocationError(device_id, operation, exc.code, exc.remote_error,
                                        outcome_unknown=exc.outcome_unknown) from exc
        except DeviceInvocationError:
            raise
        except Exception as exc:
            raise DeviceInvocationError(device_id, operation, "failed", str(exc)) from exc


def create_ros_driver_runtime(node: Any) -> DriverRuntime:
    """只在 OS 内解析本地包装器；绝不等待 HostNode.get_instance()。"""
    from unilabos.ros.nodes.base_device_node import ROS2DeviceNode
    from unilabos.ros.nodes.presets.host_node import HostNode
    from unilabos.utils.type_check import TypeEncoder

    class HostCommands:
        """Host 系统动作复用参数水合与共享循环，不把其节点暴露给领域包。"""

        def __init__(self, host: Any) -> None:
            self.host = host

        def __getattr__(self, operation: str) -> Any:
            def call(**arguments: Any) -> Any:
                command = {
                    "function_name": operation, "function_args": arguments,
                    "unilabos_param": {"sample_uuids": {}},
                }
                return self.host._execute_driver_command(
                    json.dumps(command, ensure_ascii=False, cls=TypeEncoder)
                )
            return call

    def resolve_local(device_id: str) -> Any | None:
        host = HostNode._instance
        if host is None or HostNode._shutting_down:
            return None
        if device_id == getattr(host, "device_id", None):
            return HostCommands(host)
        wrapper = host.devices_instances.get(device_id)
        if wrapper is not None:
            runtime = getattr(wrapper.ros_node_instance, "_driver_runtime", None)
            if runtime is not None and runtime._closed.is_set():
                raise DeviceInvocationError(device_id, "", "not_ready", "依赖已关闭")
        return wrapper.driver_instance if wrapper is not None else None

    return DriverRuntime(
        device_id=node.device_id, device_uuid=node.uuid,
        resolve_local=resolve_local, invoke_remote=node.call_device_action,
        remote_ready=lambda: HostNode._ready_event.is_set() and not HostNode._shutting_down,
        async_loop=ROS2DeviceNode.get_asyncio_loop,
    )
