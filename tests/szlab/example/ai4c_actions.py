"""AI4C local debug actions used by the szlab example preset.

This file intentionally lives under ``tests/szlab/example`` so new actions
can be taught and debugged without editing the production AI4C device class.
"""

from __future__ import annotations

import time
from enum import IntEnum
from typing import Any


MIN_RACK_POSITION = 1
MAX_RACK_POSITION = 8


class RoboticArmTargetPosition(IntEnum):
    PIPETTING_STATION = 3
    PLATE_LOADING_RACK = 6


class RoboticArmAction(IntEnum):
    PICK = 1
    PLACE = 2


class ExampleAI4CActions:
    """Minimal local action class for the example workflow UI.

    ``scripts/run_workflow_local.py`` patches ``_call_plc_command`` at runtime so these
    methods can reuse the configured PLC client without ROS.
    """

    def __init__(self, plc_device_id: str = "AI4C_plc", plc_action_timeout: float = 300.0):
        self.plc_device_id = plc_device_id
        self.plc_action_timeout = plc_action_timeout

    def _call_plc_command(self, function_name: str, function_args: dict[str, Any]) -> Any:
        raise RuntimeError("PLC command bridge is not initialized")

    def _read_plc_variable(self, node_name: str) -> Any:
        return self._call_plc_command(
            "read_variable",
            {
                "node_name": node_name,
                "use_cache": False,
            },
        )

    def _write_plc_variable(self, node_name: str, value: Any) -> None:
        self._call_plc_command(
            "write_variable",
            {
                "node_name": node_name,
                "value": value,
            },
        )

    def _wait_plc_bool(
        self,
        node_name: str,
        expected: bool,
        timeout: float | None = None,
        interval: float = 0.2,
    ) -> bool:
        deadline = time.time() + (timeout or self.plc_action_timeout)
        while time.time() < deadline:
            if bool(self._read_plc_variable(node_name)) is expected:
                return True
            time.sleep(interval)
        return False

    def _run_robot_arm_action(
        self,
        target_position: RoboticArmTargetPosition,
        pick_place_code: int,
        arm_action: RoboticArmAction,
        success_message: str,
    ) -> dict[str, Any]:
        self._write_plc_variable("Robotic_Arm_Target_Position_Code", int(target_position))
        self._write_plc_variable("Robotic_Arm_Target_Pick_Place_Code", pick_place_code)
        self._write_plc_variable("Robotic_Arm_Action_Code", int(arm_action))
        self._write_plc_variable("Robotic_Arm_Action_Trigger", True)

        if not self._wait_plc_bool("Robotic_Arm_Action_Complete", True):
            return {"success": False, "message": f"{success_message}失败，机械臂动作未完成"}

        self._write_plc_variable("Robotic_Arm_Action_Trigger", False)
        if not self._wait_plc_bool("Robotic_Arm_Action_Complete", False):
            return {"success": False, "message": f"{success_message}失败，完成复位超时"}

        return {"success": True, "message": success_message}

    def pick_well_plate_from_loading_rack(self, position: int = 1) -> dict[str, Any]:
        if position < MIN_RACK_POSITION or position > MAX_RACK_POSITION:
            return {"success": False, "message": "上料架位置错误"}

        occupied = self._read_plc_variable(f"Well_Plate_Loading_Rack_InPut[{position - 1}]")
        if not bool(occupied):
            return {"success": False, "message": f"上料架位置{position}没有孔板"}

        return self._run_robot_arm_action(
            target_position=RoboticArmTargetPosition.PLATE_LOADING_RACK,
            pick_place_code=position,
            arm_action=RoboticArmAction.PICK,
            success_message="从上料架抓取孔板完成",
        )

    def place_well_plate_to_pipetting_station(self) -> dict[str, Any]:
        if bool(self._read_plc_variable("Pipetting_Station_Occupied")):
            return {"success": False, "message": "移液站已有孔板"}

        return self._run_robot_arm_action(
            target_position=RoboticArmTargetPosition.PIPETTING_STATION,
            pick_place_code=1,
            arm_action=RoboticArmAction.PLACE,
            success_message="将孔板放置到移液站完成",
        )

# PYTHONPATH=. python -m scripts.run_workflow_local \
#   --runtime-config tests/szlab/example/ai4c_runtime.json \
#   --graph tests/szlab/example/ai4c_graph.json \
#   --workflow tests/szlab/example/ai4c_workflow.json \
#   --url opc.tcp://jdht1471820.bohrium.tech:50001 \
#   --no-subscription \
#   --timeout 60
