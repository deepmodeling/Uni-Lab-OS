from __future__ import annotations

from typing import Any

from .robot_tasks import build_variables, s09_sensor


class SzlabRobotS09Mixin:
    def _run_s09_place(self, product_type: int, position: int) -> dict[str, Any]:
        sensor = s09_sensor(product_type, position)
        return self._submit_robot_task(
            task="place",
            station="S09",
            task_number=19,
            variables=build_variables("place_to_s09", S09取放料产品=product_type, S09取放料编号=position),
            reset_variables={"S09取放料产品": 0, "S09取放料编号": 0, "任务号": 0},
            precheck=lambda: self._ensure_sensor_gate(sensor, False, "S09 放料目标位必须为空"),
            product_type=int(product_type),
            position=int(position),
            target_sensor_variable=sensor,
        )

    def _run_s09_pick(self, product_type: int, position: int) -> dict[str, Any]:
        sensor = s09_sensor(product_type, position)
        return self._submit_robot_task(
            task="pick",
            station="S09",
            task_number=20,
            variables=build_variables("pick_from_s09", S09取放料产品=product_type, S09取放料编号=position),
            reset_variables={"S09取放料产品": 0, "S09取放料编号": 0, "任务号": 0},
            precheck=lambda: self._ensure_sensor_gate(sensor, True, "S09 取料源位必须有物料"),
            product_type=int(product_type),
            position=int(position),
            source_sensor_variable=sensor,
        )
