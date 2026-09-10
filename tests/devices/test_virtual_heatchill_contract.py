"""虚拟温控驱动必须接受 Registry 暴露的完整 HeatChill Goal。"""

from __future__ import annotations

import asyncio

from unilabos.devices.virtual.virtual_heatchill import VirtualHeatChill
from unilabos.devices.virtual.virtual_transferpump import VirtualTransferPump
from unilabos.ros.nodes.base_device_node import _native_driver_result_failed


class _InstantROSNode:
    async def sleep(self, _seconds: float) -> None:
        return None


def test_heat_chill_accepts_complete_ros_goal() -> None:
    driver = VirtualHeatChill("heater-test", {"port": "MOCK"})
    driver.post_init(_InstantROSNode())

    result = asyncio.run(
        driver.heat_chill(
            vessel={},
            temp=25.0,
            time="1",
            temp_spec="",
            time_spec="",
            pressure="",
            reflux_solvent="",
            stir=False,
            stir_speed=300.0,
            purpose="contract-test",
        )
    )

    assert result is True

    legacy_default_result = asyncio.run(
        driver.heat_chill(
            vessel={},
            temp=25.0,
            time="",
            stir=False,
            stir_speed=300.0,
            purpose="legacy-default",
        )
    )
    assert legacy_default_result is True


def test_transfer_pump_accepts_complete_ros_goal() -> None:
    driver = VirtualTransferPump("pump-test", {"port": "MOCK"})
    driver.post_init(_InstantROSNode())

    asyncio.run(
        driver.transfer(
            from_vessel="source",
            to_vessel="target",
            volume=1.0,
            amount="",
            time=0.0,
            viscous=False,
            rinsing_solvent="",
            rinsing_volume=0.0,
            rinsing_repeats=0,
            solid=False,
        )
    )

    assert driver.current_volume == 0.0

    position_result = asyncio.run(driver.set_position(1.0, max_velocity=0.0))
    assert position_result["success"] is True


def test_native_action_failure_is_not_confused_with_json_command_boolean_data() -> None:
    class HeatChill:
        pass

    class UniLabJsonCommand:
        pass

    assert _native_driver_result_failed("heat_chill", HeatChill, False)
    assert _native_driver_result_failed("set_position", HeatChill, {"success": False})
    assert not _native_driver_result_failed("heat_chill", HeatChill, {"success": True})
    assert not _native_driver_result_failed("auto-is_empty", UniLabJsonCommand, False)
    assert not _native_driver_result_failed("_execute_driver_command", HeatChill, False)
