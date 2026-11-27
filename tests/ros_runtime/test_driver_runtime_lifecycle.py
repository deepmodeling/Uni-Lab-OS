"""真实 ROS 包装器的领域运行上下文注入与销毁。"""

import json
from threading import Event, Thread
from types import SimpleNamespace
from uuid import UUID

import pytest
import rclpy
from rclpy.action import ActionClient
from rclpy.executors import MultiThreadedExecutor
from unique_identifier_msgs.msg import UUID as ROSUUID

from unilabos.resources.resource_tracker import ResourceDictInstance
from unilabos.ros.action_transport import build_runtime_action_mappings
from unilabos.ros.driver_runtime import DriverRuntime, create_ros_driver_runtime
from unilabos.ros.nodes.base_device_node import ROS2DeviceNode
from unilabos.ros.nodes.presets.host_node import HostNode
from unilabos.runtime import DeviceInvocationError, current_execution_identity
from unilabos_msgs.action import StrSingleInput


class ContextDriver:
    def bind_runtime(self, context):
        self.context = context
        self.initial_value = context.invoke("sensor", "read")

    def post_init(self, node):
        raise AssertionError("新驱动不得重复执行旧 post_init")


class LegacyDriver:
    def post_init(self, node):
        self.node = node


class RemoteSensor:
    def read(self, value: int) -> int:
        return value + 1

    def identity(self) -> dict:
        return dict(current_execution_identity())


@pytest.fixture
def ros_context(monkeypatch):
    rclpy.init()
    monkeypatch.setattr(HostNode, "_instance", SimpleNamespace(
        devices_instances={"sensor": SimpleNamespace(
            driver_instance=SimpleNamespace(read=lambda: 25), ros_node_instance=SimpleNamespace()
        )}
    ))
    monkeypatch.setattr(HostNode, "_shutting_down", False)
    yield
    rclpy.shutdown()


def create_driver(driver_class, device_id="incubator", mappings=None):
    config = ResourceDictInstance.get_resource_instance_from_dict({
        "id": "incubator", "name": "Incubator", "uuid": str(UUID(int=1)),
        "type": "device", "class": "example.incubator",
    })
    return ROS2DeviceNode(
        device_id=device_id, device_uuid=str(UUID(int=1)), driver_class=driver_class,
        device_config=config, driver_params={}, status_types={}, action_value_mappings=mappings or {},
        hardware_interface={}, print_publish=False,
    )


def test_ros_wrapper_injects_context_during_activation_and_revokes_on_destroy(ros_context):
    device = create_driver(ContextDriver)
    driver = device.driver_instance
    try:
        assert driver.initial_value == 25
        assert driver.context.device_uuid == "00000000-0000-0000-0000-000000000001"
    finally:
        device.ros_node_instance.destroy_node()
    with pytest.raises(DeviceInvocationError) as error:
        driver.context.invoke("sensor", "read")
    assert error.value.code == "closed"


def test_legacy_post_init_remains_available(ros_context):
    device = create_driver(LegacyDriver)
    try:
        assert device.driver_instance.node is device.ros_node_instance
    finally:
        device.ros_node_instance.destroy_node()


def test_dependency_invocation_uses_real_ros_action_transport(ros_context, monkeypatch):
    caller = create_driver(LegacyDriver)
    target = create_driver(RemoteSensor, "remote_sensor", build_runtime_action_mappings({
        "read": {"type": "UniLabJsonCommand"},
    }))
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(caller.ros_node_instance)
    executor.add_node(target.ros_node_instance)
    thread = Thread(target=executor.spin, daemon=True)
    thread.start()
    runtime = DriverRuntime(
        device_id="incubator", device_uuid=str(UUID(int=1)),
        resolve_local=lambda _: None,
        invoke_remote=caller.ros_node_instance.call_device_action,
        remote_ready=lambda: True,
    )
    try:
        assert runtime.invoke("remote_sensor", "read", {"value": 24}, timeout=5) == 25
        assert runtime.invoke("remote_sensor", "identity", timeout=5) == {}
        from unilabos.config.config import EdgeControlConfig, ROSConfig
        from unilabos.utils.tracing import attach_workflow_execution_identity

        monkeypatch.setattr(EdgeControlConfig, "api_key", "")
        monkeypatch.setattr(ROSConfig, "execution_identity_key", "")
        with attach_workflow_execution_identity(str(UUID(int=5)), str(UUID(int=6))):
            with pytest.raises(DeviceInvocationError) as error:
                runtime.invoke("remote_sensor", "identity", timeout=5)
            assert error.value.code == "identity_unavailable"
            monkeypatch.setattr(ROSConfig, "execution_identity_key", "test-runtime-identity-key-32-bytes")
            assert runtime.invoke("remote_sensor", "identity", timeout=5) == {
                "node_job_uuid": str(UUID(int=5)), "task_uuid": str(UUID(int=6)),
            }
        client = ActionClient(
            caller.ros_node_instance, StrSingleInput, "/devices/remote_sensor/_execute_driver_command"
        )
        try:
            assert client.wait_for_server(timeout_sec=5)
            for source, number in (("manual", 2), ("workflow", 3)):
                job_uuid, task_uuid = str(UUID(int=number)), str(UUID(int=4))
                target.ros_node_instance.register_job_context(
                    job_uuid, task_uuid, "identity", execution_source=source,
                )
                goal = StrSingleInput.Goal(string=json.dumps({
                    "function_name": "identity", "function_args": {},
                    "unilabos_param": {"sample_uuids": {}},
                }))
                goal_handle = wait_result(client.send_goal_async(
                    goal, goal_uuid=ROSUUID(uuid=list(UUID(job_uuid).bytes)),
                ))
                assert goal_handle.accepted
                result = wait_result(goal_handle.get_result_async())
                info = json.loads(result.result.return_info)
                assert info["suc"] is True
                value = info["return_value"]
                assert value == ({"node_job_uuid": job_uuid, "task_uuid": task_uuid}
                                 if source == "workflow" else {})
        finally:
            client.destroy()
    finally:
        runtime.close()
        executor.shutdown()
        thread.join(timeout=5)
        caller.ros_node_instance.destroy_node()
        target.ros_node_instance.destroy_node()


def wait_result(future):
    done = Event()
    future.add_done_callback(lambda _: done.set())
    assert done.wait(5)
    return future.result()


def test_system_device_dependency_uses_os_command_pipeline(ros_context, monkeypatch):
    caller = create_driver(LegacyDriver)
    system = create_driver(RemoteSensor, "host_node")
    monkeypatch.setattr(HostNode, "_instance", system.ros_node_instance)
    runtime = create_ros_driver_runtime(caller.ros_node_instance)
    try:
        assert runtime.invoke("host_node", "read", {"value": 24}) == 25
        from unilabos.utils.tracing import attach_workflow_execution_identity

        with attach_workflow_execution_identity(str(UUID(int=2)), str(UUID(int=3))):
            assert runtime.invoke("host_node", "identity") == {
                "node_job_uuid": str(UUID(int=2)), "task_uuid": str(UUID(int=3)),
            }
    finally:
        runtime.close()
        caller.ros_node_instance.destroy_node()
        system.ros_node_instance.destroy_node()
