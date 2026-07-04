from __future__ import annotations

from typing import Any

from .robot_tasks import build_variables, s10_sensor


class SzlabRobotS10Mixin:
    def _run_s10_place(self, position: int) -> dict[str, Any]:
        sensor = s10_sensor(position)
        return self._submit_robot_task(
            task="place",
            station="S10",
            task_number=21,
            variables=build_variables("place_to_s10", S10取放料编号=position),
            reset_variables={"S10取放料编号": 0, "任务号": 0},
            precheck=lambda: self._ensure_sensor_gate(sensor, False, "S10 放试剂瓶目标位必须为空"),
            position=int(position),
            target_sensor_variable=sensor,
        )

    def _run_s10_pick(self, position: int) -> dict[str, Any]:
        sensor = s10_sensor(position)
        return self._submit_robot_task(
            task="pick",
            station="S10",
            task_number=22,
            variables=build_variables("pick_from_s10", S10取放料编号=position),
            reset_variables={"S10取放料编号": 0, "任务号": 0},
            precheck=lambda: self._ensure_sensor_gate(sensor, True, "S10 取试剂瓶源位必须有试剂瓶"),
            position=int(position),
            source_sensor_variable=sensor,
        )
