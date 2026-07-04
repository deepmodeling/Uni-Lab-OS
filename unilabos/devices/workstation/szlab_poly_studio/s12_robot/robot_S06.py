from __future__ import annotations

from typing import Any

from .robot_tasks import S06_MATERIAL_SENSOR


class SzlabRobotS06Mixin:
    def _run_s06_place(self) -> dict[str, Any]:
        return self._submit_robot_task(
            task="place",
            station="S06",
            task_number=11,
            variables=None,
            reset_variables={"任务号": 0},
            precheck=lambda: self._ensure_sensor_gate(S06_MATERIAL_SENSOR, False, "S06 放料目标位必须为空"),
            target_sensor_variable=S06_MATERIAL_SENSOR,
        )

    def _run_s06_pick(self) -> dict[str, Any]:
        return self._submit_robot_task(
            task="pick",
            station="S06",
            task_number=12,
            variables=None,
            reset_variables={"任务号": 0},
            precheck=lambda: self._ensure_sensor_gate(S06_MATERIAL_SENSOR, True, "S06 取料源位必须有物料"),
            source_sensor_variable=S06_MATERIAL_SENSOR,
        )
