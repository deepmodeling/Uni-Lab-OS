from __future__ import annotations

from typing import Any

from .robot_tasks import build_variables, product_slot_sensor


class SzlabRobotS11Mixin:
    def _run_s11_place(self, product_type: int, position: str = "1-1") -> dict[str, Any]:
        sensor = product_slot_sensor(product_type, position, used=True)
        return self._submit_robot_task(
            task="place",
            station="S11",
            task_number=23,
            variables=build_variables("place_to_s11", S11取放料产品=product_type, S11取放料编号=self._slot_number(position)),
            reset_variables={"S11取放料产品": 0, "S11取放料编号": 0, "任务号": 0},
            precheck=lambda: self._ensure_sensor_gate(sensor, False, "S11 放料目标位必须为空"),
            product_type=int(product_type),
            position=str(position),
            target_sensor_variable=sensor,
        )

    def _run_s11_pick(self, product_type: int, position: str = "1-1") -> dict[str, Any]:
        sensor = product_slot_sensor(product_type, position, used=True)
        return self._submit_robot_task(
            task="pick",
            station="S11",
            task_number=24,
            variables=build_variables("pick_from_s11", S11取放料产品=product_type, S11取放料编号=self._slot_number(position)),
            reset_variables={"S11取放料产品": 0, "S11取放料编号": 0, "任务号": 0},
            precheck=lambda: self._ensure_sensor_gate(sensor, True, "S11 取料源位必须有物料"),
            product_type=int(product_type),
            position=str(position),
            source_sensor_variable=sensor,
        )
