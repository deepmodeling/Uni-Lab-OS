from __future__ import annotations

from typing import Any

from .robot_tasks import build_variables, s02_sensor


class SzlabRobotS02Mixin:
    def _run_s02_place(self, position: int) -> dict[str, Any]:
        sensor = s02_sensor(position)
        return self._submit_robot_task(
            task="place",
            station="S02",
            task_number=3,
            variables=build_variables("place_to_s02", S02取放料编号=position),
            reset_variables={"S02取放料编号": 0, "任务号": 0},
            precheck=lambda: self._ensure_sensor_gate(sensor, False, "S02 放料目标位必须为空"),
            position=int(position),
            target_sensor_variable=sensor,
        )

    def _run_s02_pick(self, position: int) -> dict[str, Any]:
        sensor = s02_sensor(position)
        return self._submit_robot_task(
            task="pick",
            station="S02",
            task_number=4,
            variables=build_variables("pick_from_s02", S02取放料编号=position),
            reset_variables={"S02取放料编号": 0, "任务号": 0},
            precheck=lambda: self._ensure_sensor_gate(sensor, True, "S02 取料源位必须有 TIP"),
            position=int(position),
            source_sensor_variable=sensor,
        )
