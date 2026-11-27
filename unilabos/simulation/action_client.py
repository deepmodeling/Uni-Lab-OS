"""原生设备 Action 到 OS 模拟执行服务的阻塞调用，不拥有推进循环。"""
import json
import os
import socket

from unilabos.utils.tracing import capture_workflow_execution_identity
from unilabos.utils.type_check import resource_reference_uuid


def _resource_reference(value):
    """沿用 ResourceSlot 水合后的权威 UUID，拒绝把对象名称当身份。"""
    from pylabrobot.resources import Resource
    if isinstance(value, Resource):
        reference = resource_reference_uuid(value)
        if reference:
            return {"uuid": reference}
    raise TypeError("native_resource_reference_missing")


def execute_simulated_action(device_type, operation, parameters, *, resources):
    from .configuration import validate_simulation_environment

    validate_simulation_environment()
    path = os.environ.get("UNILABOS_SIMULATION_ACTION_SOCKET")
    if not path:
        raise RuntimeError("simulation_provider_not_configured")
    identity = capture_workflow_execution_identity()
    if not identity.get("task_uuid") or not identity.get("node_job_uuid"):
        raise RuntimeError("native_task_job_identity_required")
    request = {"device_type": device_type, "operation": operation, "parameters": parameters, "identity": identity, "resources": resources}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(1800)
        client.connect(path)
        client.sendall(json.dumps(request, allow_nan=False, default=_resource_reference).encode() + b"\n")
        with client.makefile("rb") as stream:
            response = json.loads(stream.readline(16 * 1024 * 1024))
    if response.get("ok") is not True:
        raise RuntimeError(response.get("error", "simulation_execution_unknown"))
    result = response["result"]
    if result.get("success") is not True or result.get("inventory_receipt", {}).get("scope") != "canonical_inventory":
        raise RuntimeError("canonical_inventory_receipt_missing")
    return result
