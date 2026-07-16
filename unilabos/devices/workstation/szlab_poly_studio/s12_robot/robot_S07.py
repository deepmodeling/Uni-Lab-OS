from __future__ import annotations

from typing import Any

from .robot_tasks import build_variables, powder_container_sensor

S072_MATERIAL_SENSOR = "传感器状态_上位机[3].NO[1]"


class SzlabRobotS07Mixin:
    def _validate_s072_position(self, position: int) -> int:
        position = int(position)
        if position not in (1, 2):
            raise ValueError("S072 位置必须在 1-2 范围内")
        return position

    def _run_s071_place(self, position: str = "1-1") -> dict[str, Any]:
        sensor = powder_container_sensor(position)
        return self._submit_robot_task(
            task="place",
            station="S071",
            task_number=13,
            variables=build_variables("place_to_s071", S071取放料编号=self._slot_number(position)),
            reset_variables={"S071取放料编号": 0, "任务号": 0},
            precheck=lambda: self._ensure_sensor_gate(sensor, False, "S071 放粉罐目标位必须为空"),
            position=str(position),
            target_sensor_variable=sensor,
        )

    def _run_s071_pick(self, position: str = "1-1") -> dict[str, Any]:
        sensor = powder_container_sensor(position)
        return self._submit_robot_task(
            task="pick",
            station="S071",
            task_number=14,
            variables=build_variables("pick_from_s071", S071取放料编号=self._slot_number(position)),
            reset_variables={"S071取放料编号": 0, "任务号": 0},
            precheck=lambda: self._ensure_sensor_gate(sensor, True, "S071 取粉罐源位必须有粉罐"),
            position=str(position),
            source_sensor_variable=sensor,
        )

    def _run_s072_place(self, product_type: int, position: int) -> dict[str, Any]:
        position = self._validate_s072_position(position)
        return self._submit_robot_task(
            task="place",
            station="S072",
            task_number=15,
            variables=build_variables("place_to_s072", S072取放料产品=product_type),
            reset_variables={"S072取放料产品": 0, "任务号": 0},
            product_type=int(product_type),
            position=position,
            target_sensor_variable=S072_MATERIAL_SENSOR,
        )

    def _run_s072_pick(self, product_type: int, position: int) -> dict[str, Any]:
        position = self._validate_s072_position(position)
        return self._submit_robot_task(
            task="pick",
            station="S072",
            task_number=16,
            variables=build_variables("pick_from_s072", S072取放料产品=product_type),
            reset_variables={"S072取放料产品": 0, "任务号": 0},
            product_type=int(product_type),
            position=position,
            source_sensor_variable=S072_MATERIAL_SENSOR,
        )
