from types import SimpleNamespace

from unilabos.ros.nodes.presets.host_node import HostNode


def _host_with_driver(driver: object) -> HostNode:
    host = object.__new__(HostNode)
    host.devices_instances = {
        "device-01": SimpleNamespace(
            _ros_node=SimpleNamespace(driver_instance=driver)
        )
    }
    return host


def test_operator_can_resolve_backend_uncertainty_for_non_durable_driver() -> None:
    host = _host_with_driver(object())

    result = host.resolve_unknown_device_command(
        "device-01",
        "workflow-node-job:job-01",
        "resolution-01",
        "操作员确认仿真设备已复位且空闲",
    )

    assert result == {
        "command_id": "workflow-node-job:job-01",
        "state": "CANCELED",
        "previous_state": "UNKNOWN",
        "resolution_committed": True,
        "resolution_command_uuid": "resolution-01",
        "message": "操作员确认仿真设备已复位且空闲",
        "durable_journal": False,
    }


def test_non_durable_driver_cannot_bypass_a_declared_dispatch_block() -> None:
    class BlockedDriver:
        def dispatch_block_reason(self) -> str:
            return "device_requires_reconciliation"

    host = _host_with_driver(BlockedDriver())

    try:
        host.resolve_unknown_device_command(
            "device-01",
            "workflow-node-job:job-01",
            "resolution-01",
            "操作员确认仿真设备已复位且空闲",
        )
    except ValueError as error:
        assert "不支持 UNKNOWN 命令人工对账" in str(error)
    else:
        raise AssertionError("declared dispatch block must require a durable resolver")


def test_missing_device_cannot_be_reconciled_as_non_durable() -> None:
    host = object.__new__(HostNode)
    host.devices_instances = {}

    try:
        host.resolve_unknown_device_command(
            "missing-device",
            "workflow-node-job:job-01",
            "resolution-01",
            "操作员确认仿真设备已复位且空闲",
        )
    except ValueError as error:
        assert "不存在或驱动尚未就绪" in str(error)
    else:
        raise AssertionError("missing device must fail closed")
