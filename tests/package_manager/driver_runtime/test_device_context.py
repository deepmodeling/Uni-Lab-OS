"""通过领域驱动公共接口验证依赖调用与执行身份。"""

import asyncio
from threading import Event, Thread
from types import SimpleNamespace
from uuid import UUID

import pytest

from unilabos.ros.driver_runtime import DriverRuntime
from unilabos.runtime import DeviceInvocationError, current_execution_identity
from unilabos.utils.tracing import attach_workflow_execution_identity


def test_second_domain_driver_calls_dependency_with_authenticated_identity():
    class Thermometer:
        def read_temperature(self, scale):
            assert scale == "celsius"
            return 23.5, dict(current_execution_identity())

    class Incubator:
        def bind_runtime(self, context):
            self.context = context

        def temperature(self):
            return self.context.invoke("thermometer", "read_temperature", {"scale": "celsius"})

    runtime = DriverRuntime(
        device_id="incubator",
        device_uuid=str(UUID(int=1)),
        resolve_local=lambda device_id: Thermometer() if device_id == "thermometer" else None,
        invoke_remote=lambda *args, **kwargs: None,
        remote_ready=lambda: False,
    )
    driver = Incubator()
    runtime.bind(driver)
    assert driver.context.device_id == "incubator"
    assert driver.context.device_uuid == "00000000-0000-0000-0000-000000000001"
    with attach_workflow_execution_identity(str(UUID(int=2)), str(UUID(int=3))):
        assert driver.temperature() == (23.5, {
            "node_job_uuid": "00000000-0000-0000-0000-000000000002",
            "task_uuid": "00000000-0000-0000-0000-000000000003",
        })
    assert dict(current_execution_identity()) == {}


def runtime_for(driver=None, *, remote=None, ready=False):
    return DriverRuntime(
        device_id="caller", device_uuid=str(UUID(int=1)),
        resolve_local=lambda _: driver,
        invoke_remote=remote or (lambda *args, **kwargs: "remote-result"),
        remote_ready=lambda: ready,
    )


def test_bootstrap_missing_dependency_fails_immediately_and_closed_context_stays_closed():
    runtime = runtime_for()
    with pytest.raises(DeviceInvocationError) as error:
        runtime.invoke("missing", "read", timeout=None)
    assert error.value.code == "not_ready"
    runtime.close()
    with pytest.raises(DeviceInvocationError) as error:
        runtime.invoke("missing", "read")
    assert error.value.code == "closed"


@pytest.mark.parametrize("local", [True, False])
def test_local_and_remote_calls_share_result_failure_and_timeout_contract(local):
    released, started = Event(), Event()

    def wait():
        started.set()
        released.wait(2)
        return 42

    def fail():
        raise ValueError("invalid-point")

    driver = SimpleNamespace(wait=wait, fail=fail)
    runtime = runtime_for(
        driver if local else None,
        remote=lambda device, operation, arguments, **options: getattr(driver, operation)(**arguments),
        ready=True,
    )
    try:
        with pytest.raises(DeviceInvocationError) as error:
            runtime.invoke("sensor", "wait", timeout=0.05)
        assert started.is_set()
        assert error.value.code == "timeout"
        assert error.value.outcome_unknown is True
    finally:
        released.set()
    assert runtime.invoke("sensor", "wait") == 42
    with pytest.raises(DeviceInvocationError) as error:
        runtime.invoke("sensor", "fail")
    assert error.value.code == "failed"
    assert "invalid-point" in str(error.value)


def test_remote_adapter_receives_original_authenticated_identity():
    called = []
    runtime = runtime_for(remote=lambda *args, **kwargs: called.append(dict(current_execution_identity())), ready=True)
    with attach_workflow_execution_identity(str(UUID(int=2)), str(UUID(int=3))):
        with pytest.raises(TypeError):
            current_execution_identity()["node_job_uuid"] = "forged"
        runtime.invoke("remote-robot", "pick")
    assert called == [{"node_job_uuid": str(UUID(int=2)), "task_uuid": str(UUID(int=3))}]


def test_failed_binding_revokes_retained_context():
    class BrokenDriver:
        def bind_runtime(self, context):
            self.context = context
            raise ValueError("setup-failed")

    driver = BrokenDriver()
    with pytest.raises(ValueError, match="setup-failed"):
        runtime_for().bind(driver)
    with pytest.raises(DeviceInvocationError) as error:
        driver.context.invoke("sensor", "read")
    assert error.value.code == "closed"


def test_async_dependency_returns_value_and_preserves_identity():
    async def read():
        return dict(current_execution_identity())

    with attach_workflow_execution_identity(str(UUID(int=2)), str(UUID(int=3))):
        result = runtime_for(SimpleNamespace(read=read)).invoke("sensor", "read")
    assert result["node_job_uuid"] == "00000000-0000-0000-0000-000000000002"


@pytest.mark.parametrize("code,unknown", [("not_ready", False), ("rejected", False), ("timeout", True)])
def test_ros_transport_errors_keep_their_meaning(code, unknown):
    from unilabos.utils.exception import DeviceActionError

    def remote(*args, **kwargs):
        raise DeviceActionError("sensor", "read", "transport-error", code=code, outcome_unknown=unknown)

    with pytest.raises(DeviceInvocationError) as error:
        runtime_for(remote=remote, ready=True).invoke("sensor", "read")
    assert error.value.code == code
    assert error.value.outcome_unknown is unknown


def test_nested_dependency_keeps_unknown_outcome():
    def inner():
        raise DeviceInvocationError("pump", "dispense", "timeout", outcome_unknown=True)

    with pytest.raises(DeviceInvocationError) as error:
        runtime_for(SimpleNamespace(transfer=inner)).invoke("robot", "transfer")
    assert error.value.code == "timeout"
    assert error.value.outcome_unknown is True
    assert error.value.device_id == "pump"


def test_dependency_uses_existing_device_event_loop_and_cannot_block_it():
    loop = asyncio.new_event_loop()
    started = Event()
    loop.call_soon(started.set)
    thread = Thread(target=loop.run_forever, daemon=True)
    thread.start()
    assert started.wait(2)

    async def read():
        future = loop.create_future()
        loop.call_later(0.01, future.set_result, 27)
        return await future

    runtime = DriverRuntime(
        device_id="incubator", device_uuid=str(UUID(int=1)),
        resolve_local=lambda _: SimpleNamespace(read=read),
        invoke_remote=lambda *args, **kwargs: None, remote_ready=lambda: False,
        async_loop=lambda: loop,
    )

    async def from_driver_loop():
        with pytest.raises(RuntimeError, match="asyncio.to_thread"):
            runtime.invoke("sensor", "read")
        return await asyncio.to_thread(runtime.invoke, "sensor", "read")

    try:
        assert runtime.invoke("sensor", "read") == 27
        assert asyncio.run_coroutine_threadsafe(from_driver_loop(), loop).result(timeout=2) == 27
    finally:
        runtime.close()
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()
