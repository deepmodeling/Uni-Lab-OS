from __future__ import annotations

from typing import Any

from .robot_tasks import build_variables

S01_GRIPPER_PRODUCT_SENSOR = "传感器状态_上位机[3].NO[6]"
S01_SENSOR_BY_POSITION = {
    1: S01_GRIPPER_PRODUCT_SENSOR,
}


class SzlabRobotS01Mixin:
    def _s01_sensor_variable(self, position: int = 1) -> str:
        position = int(position)
        if position not in S01_SENSOR_BY_POSITION:
            raise ValueError("S01 取料位置必须在 1-1 范围内")
        return S01_SENSOR_BY_POSITION[position]

    def _run_s01_pick(
        self,
        product_type: int,
        position: int = 1,
    ) -> dict[str, Any]:
        sensor = self._s01_sensor_variable(position)
        return self._submit_robot_task(
            task="pick",
            station="S01",
            task_number=1,
            variables=build_variables("pick_from_s01", S01出入料产品=product_type, S01取放料编号=position),
            reset_variables={"S01出入料产品": 0, "S01取放料编号": 0, "任务号": 0},
            precheck=lambda: self._ensure_sensor_gate(sensor, True, "S01 取料源位必须有物料"),
            product_type=int(product_type),
            position=int(position),
            source_sensor_variable=sensor,
        )
