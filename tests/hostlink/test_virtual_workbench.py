"""virtual_workbench 的无 ROS HostLink 运行时闭环。"""

from __future__ import annotations

from unilabos.devices.virtual.workbench import VirtualWorkbench
from unilabos.hostlink.local_runtime import HostLinkDriverSpec, HostLinkLocalRuntime


class _PeerDriver:
    def __init__(self, device_id: str | None = None, config=None) -> None:
        self.device_id = device_id
        self.config = config

    @staticmethod
    def echo(value: int) -> dict[str, int]:
        return {"value": value}


def test_virtual_workbench_hostlink_action_status_and_peer_call() -> None:
    runtime = HostLinkLocalRuntime()
    workbench = runtime.add_driver(
        HostLinkDriverSpec(
            "workbench",
            VirtualWorkbench,
            {
                "arm_operation_time": 0,
                "heating_time": 0.01,
                "num_heating_stations": 3,
            },
            status_names=(
                "status",
                "arm_state",
                "heating_station_1_state",
                "heating_station_1_material",
                "heating_station_1_progress",
            ),
        )
    )
    runtime.add_driver(HostLinkDriverSpec("peer", _PeerDriver, {}))
    runtime.start()
    try:
        assert workbench.driver._device_node is workbench
        assert workbench.backend_name == "hostlink"

        peer_result = runtime.call_action(
            "workbench",
            "call_peer",
            target_device="peer",
            function_name="echo",
            function_args='{"value": 7}',
        )
        assert peer_result["return_value"] == {"value": 7}

        placed = runtime.call_action(
            "workbench",
            "move_to_heating_station",
            sample_uuids={},
            material_number=1,
        )
        assert placed["success"] is True
        assert placed["station_id"] == 1
        placed_status = runtime.snapshot_states()["workbench"]
        assert placed_status["heating_station_1_state"] == "occupied"
        assert placed_status["heating_station_1_material"] == "A1"

        heated = runtime.call_action(
            "workbench",
            "start_heating",
            sample_uuids={},
            station_id=1,
            material_number=1,
        )
        assert heated["success"] is True
        assert runtime.snapshot_states()["workbench"]["heating_station_1_state"] == (
            "completed"
        )

        moved = runtime.call_action(
            "workbench",
            "move_to_output",
            sample_uuids={},
            station_id=1,
            material_number=1,
        )
        assert moved["success"] is True
        assert moved["output_position"] == "C1"
        final_status = runtime.snapshot_states()["workbench"]
        assert final_status["heating_station_1_state"] == "idle"
        assert final_status["heating_station_1_material"] == ""
        assert final_status["arm_state"] == "idle"
    finally:
        runtime.stop()
