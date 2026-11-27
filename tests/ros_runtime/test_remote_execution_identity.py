"""真实双进程 ROS：父作业身份继承与篡改拒绝，禁止依赖共享 Python 内存。"""

import json
import os
import subprocess
import sys
import time

import pytest

pytest.importorskip("rclpy")

_COMMON = '''
import asyncio, json, os, sys, threading, time, uuid
import yaml
from pathlib import Path
import rclpy
from rclpy.executors import MultiThreadedExecutor
from unique_identifier_msgs.msg import UUID
from unilabos.config.config import ROSConfig
from unilabos.resources.resource_tracker import ResourceDictInstance
from unilabos.ros.nodes.base_device_node import ROS2DeviceNode
from unilabos.ros.driver_runtime import DriverRuntime
from unilabos.ros.action_transport import build_runtime_action_mappings
from unilabos_msgs.action import StrSingleInput
from unilabos.utils.tracing import attach_workflow_execution_identity
ROSConfig.execution_identity_key = os.environ["UNILAB_TEST_RUNTIME_KEY"]
rclpy.init()
def wrap(driver, device_id):
    mapping = {"type": "UniLabJsonCommand"}
    if os.environ["UNILAB_TEST_TRANSPORT"] == "native":
        mapping = {"type": StrSingleInput, "goal": {}, "feedback": {}, "result": {}}
    config = ResourceDictInstance.get_resource_instance_from_dict({
        "id": device_id, "name": device_id, "uuid": str(uuid.uuid4()),
        "type": "device", "class": "test.sensor",
    })
    return ROS2DeviceNode(device_id=device_id, device_uuid=str(uuid.uuid4()), driver_class=driver,
        device_config=config, driver_params={}, status_types={},
        action_value_mappings=build_runtime_action_mappings({"identity": mapping}),
        hardware_interface={}, print_publish=False)
'''

_SERVER = _COMMON + '''
from unilabos.runtime import current_execution_identity
class Sensor:
    def identity(self):
        with open(sys.argv[2], "a") as stream:
            stream.write("called\\n")
        return dict(current_execution_identity())
node = wrap(Sensor, os.environ["UNILAB_TEST_REMOTE_ID"])
executor = MultiThreadedExecutor(num_threads=4)
executor.add_node(node.ros_node_instance)
Path(sys.argv[1]).write_text("ready")
executor.spin()
'''

_CLIENT = _COMMON + '''
from unilabos.runtime import DeviceInvocationError
class Caller: pass
node = wrap(Caller, "caller_" + uuid.uuid4().hex)
executor = MultiThreadedExecutor(num_threads=4)
executor.add_node(node.ros_node_instance)
thread = threading.Thread(target=executor.spin, daemon=True)
thread.start()
target = os.environ["UNILAB_TEST_REMOTE_ID"]
runtime = DriverRuntime(device_id="caller", device_uuid=str(uuid.uuid4()),
    resolve_local=lambda _: None, invoke_remote=node.ros_node_instance.call_device_action,
    remote_ready=lambda: True)
def wait(future):
    done = threading.Event()
    future.add_done_callback(lambda _: done.set())
    assert done.wait(10)
    return future.result()
try:
    result = {}
    if os.environ["UNILAB_TEST_TRANSPORT"] == "json":
        result["anonymous"] = runtime.invoke(target, "identity", timeout=10)
    job_id, task_id = str(uuid.uuid4()), str(uuid.uuid4())
    expected = {"node_job_uuid": job_id, "task_uuid": task_id}
    with attach_workflow_execution_identity(job_id, task_id):
        result["signed"] = runtime.invoke(target, "identity", timeout=10)
        result["async"] = asyncio.run(node.ros_node_instance.call_device_action_async(target, "identity"))
        goal_id = uuid.uuid4()
        _, client, goal = node.ros_node_instance._build_action_call(
            target, "identity", {}, False, None, execution_goal_id=str(goal_id))
        command = json.loads(goal.string)
        command["unilabos_param"]["injected"] = True
        goal.string = json.dumps(command)
        handle = wait(client.send_goal_async(goal, goal_uuid=UUID(uuid=list(goal_id.bytes))))
        result["tampered_success"] = wait(handle.get_result_async()).result.success
        yaml_goal_id = uuid.uuid4()
        _, client, goal = node.ros_node_instance._build_action_call(
            target, "identity", {}, False, None, execution_goal_id=str(yaml_goal_id))
        command = json.loads(goal.string)
        command["unilabos_param"]["injected"] = True
        goal.string = yaml.safe_dump(command)
        handle = wait(client.send_goal_async(goal, goal_uuid=UUID(uuid=list(yaml_goal_id.bytes))))
        result["tampered_yaml_success"] = wait(handle.get_result_async()).result.success
        ROSConfig.execution_identity_key = "different-runtime-key-at-least-32-bytes"
        try:
            runtime.invoke(target, "identity", timeout=10)
        except DeviceInvocationError:
            result["wrong_key_rejected"] = True
    result["expected"] = expected
    Path(sys.argv[1]).write_text(json.dumps(result))
finally:
    runtime.close()
    executor.shutdown()
    thread.join(5)
    node.ros_node_instance.destroy_node()
    rclpy.shutdown()
'''


@pytest.mark.parametrize("transport", ["json", "native"])
def test_identity_crosses_process_boundary_without_authorizing_tampered_calls(tmp_path, transport):
    environment = {
        **os.environ, "ROS_DOMAIN_ID": str(110 + os.getpid() % 80), "ROS_LOCALHOST_ONLY": "0",
        # 本机单播发现不依赖 VPN/无线网卡多播；两个进程仍通过真实 DDS 通信。
        "CYCLONEDDS_URI": (
            '<CycloneDDS><Domain><General><Interfaces><NetworkInterface address="127.0.0.1"/>'
            '</Interfaces><AllowMulticast>false</AllowMulticast></General><Discovery>'
            '<ParticipantIndex>auto</ParticipantIndex><Peers><Peer Address="127.0.0.1"/>'
            '</Peers></Discovery></Domain></CycloneDDS>'
        ),
        "UNILAB_TEST_RUNTIME_KEY": "isolated-test-runtime-identity-32-bytes",
        "UNILAB_TEST_TRANSPORT": transport,
        "UNILAB_TEST_REMOTE_ID": "sensor_" + tmp_path.name.replace("-", "_")[:40],
    }
    ready, audit, result = (tmp_path / name for name in ("ready", "calls", "result.json"))
    with (tmp_path / "server.log").open("w") as log:
        server = subprocess.Popen([sys.executable, "-c", _SERVER, str(ready), str(audit)],
                                  env=environment, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 15
            while not ready.exists() and server.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            assert ready.exists(), (tmp_path / "server.log").read_text()[-3000:]
            client = subprocess.run([sys.executable, "-c", _CLIENT, str(result)], env=environment,
                                    text=True, capture_output=True, timeout=40)
            assert client.returncode == 0, (client.stdout + client.stderr)[-4000:]
            evidence = json.loads(result.read_text())
            if transport == "json":
                assert evidence["anonymous"] == {}
            assert evidence["signed"] == evidence["async"] == evidence["expected"]
            assert evidence["tampered_success"] is False
            assert evidence["tampered_yaml_success"] is False
            assert evidence["wrong_key_rejected"] is True
            assert audit.read_text().splitlines() == ["called"] * (3 if transport == "json" else 2)
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
