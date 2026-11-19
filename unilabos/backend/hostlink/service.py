"""HostLink 的 ROS-shaped service/client 兼容实现。"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional, Protocol

from unilabos.backend.hostlink.topic import normalize_topic, value_to_message

ServiceCallback = Callable[[Any], Awaitable[Any]]


def normalize_service_name(name: str, device_id: str = "") -> str:
    return normalize_topic(name, device_id)


class ServiceBus(Protocol):
    def register_service(
        self,
        name: str,
        callback: ServiceCallback,
        *,
        owner_device_id: str,
    ) -> None: ...

    def unregister_service(self, name: str, *, owner_device_id: str) -> None: ...

    def has_service(self, name: str) -> bool: ...

    async def call_service_async(
        self,
        name: str,
        request: Any,
        *,
        caller_device_id: str,
        timeout: Optional[float] = None,
    ) -> Any: ...


@dataclass(frozen=True)
class _ServiceRecord:
    callback: ServiceCallback
    owner_device_id: str


class LocalServiceBus:
    def __init__(self) -> None:
        self._services: Dict[str, _ServiceRecord] = {}
        self._lock = threading.RLock()

    def register_service(
        self,
        name: str,
        callback: ServiceCallback,
        *,
        owner_device_id: str,
    ) -> None:
        normalized = normalize_service_name(name)
        with self._lock:
            if normalized in self._services:
                raise ValueError(f"service 已存在：{normalized}")
            self._services[normalized] = _ServiceRecord(
                callback=callback,
                owner_device_id=str(owner_device_id),
            )

    def unregister_service(self, name: str, *, owner_device_id: str) -> None:
        normalized = normalize_service_name(name)
        with self._lock:
            record = self._services.get(normalized)
            if record is not None and record.owner_device_id == str(owner_device_id):
                self._services.pop(normalized, None)

    def has_service(self, name: str) -> bool:
        with self._lock:
            return normalize_service_name(name) in self._services

    def services(self, owner_device_id: str = "") -> list[str]:
        with self._lock:
            return sorted(
                name
                for name, record in self._services.items()
                if not owner_device_id or record.owner_device_id == owner_device_id
            )

    async def call_service_async(
        self,
        name: str,
        request: Any,
        *,
        caller_device_id: str = "",
        timeout: Optional[float] = None,
    ) -> Any:
        del caller_device_id
        normalized = normalize_service_name(name)
        with self._lock:
            record = self._services.get(normalized)
        if record is None:
            raise KeyError(f"未知 service：{normalized}")
        operation = record.callback(request)
        if timeout is None:
            return await operation
        return await asyncio.wait_for(operation, float(timeout))

    def call_service(
        self,
        name: str,
        request: Any,
        *,
        caller_device_id: str = "",
        timeout: Optional[float] = None,
    ) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(
                self.call_service_async(
                    name,
                    request,
                    caller_device_id=caller_device_id,
                    timeout=timeout,
                )
            )
        raise RuntimeError("异步设备方法中请使用 client.call_async(request)")


class DeviceService:
    def __init__(
        self,
        bus: ServiceBus,
        service_name: str,
        srv_type: Any,
        owner_device_id: str,
    ) -> None:
        self._bus = bus
        self.service_name = service_name
        self.srv_type = srv_type
        self.owner_device_id = owner_device_id
        self._destroyed = False

    def destroy(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        self._bus.unregister_service(
            self.service_name,
            owner_device_id=self.owner_device_id,
        )


class DeviceServiceClient:
    def __init__(
        self,
        bus: ServiceBus,
        service_name: str,
        srv_type: Any,
        caller_device_id: str,
    ) -> None:
        self._bus = bus
        self.service_name = service_name
        self.srv_type = srv_type
        self.caller_device_id = caller_device_id

    def service_is_ready(self) -> bool:
        return self._bus.has_service(self.service_name)

    def wait_for_service(self, timeout_sec: Optional[float] = None) -> bool:
        deadline = None if timeout_sec is None else time.monotonic() + timeout_sec
        while not self.service_is_ready():
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.02)
        return True

    def call(self, request: Any, *, timeout: Optional[float] = None) -> Any:
        call = getattr(self._bus, "call_service", None)
        if callable(call):
            result = call(
                self.service_name,
                request,
                caller_device_id=self.caller_device_id,
                timeout=timeout,
            )
            return value_to_message(getattr(self.srv_type, "Response", None), result)
        return asyncio.run(self.call_async(request, timeout=timeout))

    async def call_async(
        self,
        request: Any,
        *,
        timeout: Optional[float] = None,
    ) -> Any:
        result = await self._bus.call_service_async(
            self.service_name,
            request,
            caller_device_id=self.caller_device_id,
            timeout=timeout,
        )
        return value_to_message(getattr(self.srv_type, "Response", None), result)


def build_service_callback(
    node: Any,
    srv_type: Any,
    callback: Callable[..., Any],
) -> ServiceCallback:
    parameters = list(inspect.signature(callback).parameters.values())
    accepts_response = len(parameters) >= 2 or any(
        parameter.kind is inspect.Parameter.VAR_POSITIONAL for parameter in parameters
    )

    async def invoke(request: Any) -> Any:
        request = value_to_message(getattr(srv_type, "Request", None), request)
        response_type = getattr(srv_type, "Response", None)
        response = response_type() if callable(response_type) else None
        result = callback(request, response) if accepts_response else callback(request)
        if inspect.isawaitable(result):
            result = await result
        return response if result is None else result

    async def dispatch(request: Any) -> Any:
        future = node.create_task(invoke(request))
        if inspect.isawaitable(future):
            return await future
        if hasattr(future, "__await__"):
            return await future
        return await asyncio.wrap_future(future)

    return dispatch


__all__ = [
    "DeviceService",
    "DeviceServiceClient",
    "LocalServiceBus",
    "ServiceBus",
    "build_service_callback",
    "normalize_service_name",
]
