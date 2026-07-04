from __future__ import annotations

from typing import Any

from .robot_tasks import build_variables, product_slot_sensor


class SzlabRobotS03Mixin:
    def _run_s03_place(self, product_type: int, position: str = "1-1") -> dict[str, Any]:
        sensor = product_slot_sensor(product_type, position, used=False)
        return self._submit_robot_task(
            task="place",
            station="S03",
            task_number=5,
            variables=build_variables("place_to_s03", S03取放料产品=product_type, S03取放料编号=self._slot_number(position)),
            reset_variables={"S03取放料产品": 0, "S03取放料编号": 0, "任务号": 0},
            precheck=lambda: self._ensure_sensor_gate(sensor, False, "S03 放料目标位必须为空"),
            product_type=int(product_type),
            position=str(position),
            target_sensor_variable=sensor,
        )

    def _run_s03_pick(self, product_type: int, position: str = "1-1") -> dict[str, Any]:
        sensor = product_slot_sensor(product_type, position, used=False)
        return self._submit_robot_task(
            task="pick",
            station="S03",
            task_number=6,
            variables=build_variables("pick_from_s03", S03取放料产品=product_type, S03取放料编号=self._slot_number(position)),
            reset_variables={"S03取放料产品": 0, "S03取放料编号": 0, "任务号": 0},
            precheck=lambda: self._ensure_sensor_gate(sensor, True, "S03 取料源位必须有物料"),
            product_type=int(product_type),
            position=str(position),
            source_sensor_variable=sensor,
        )
