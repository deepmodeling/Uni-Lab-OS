from __future__ import annotations

from typing import Any

from .robot_tasks import S05_MATERIAL_SENSOR

S05_PLACE_TASK_NUMBER = 9
S05_PICK_TASK_NUMBER = 10


class SzlabRobotS05Mixin:
    def _run_s05_pick(self, sample_id: str = "") -> dict[str, Any]:
        return self._submit_robot_task(
            task="pick",
            station="S05",
            task_number=S05_PICK_TASK_NUMBER,
            variables=None,
            reset_variables={"任务号": 0},
            precheck=lambda: self._ensure_sensor_gate(S05_MATERIAL_SENSOR, True, "S05 取料源位必须有物料"),
            sample_id=sample_id,
            source_sensor_variable=S05_MATERIAL_SENSOR,
        )

    def _run_s05_place(self, sample_id: str = "") -> dict[str, Any]:
        return self._submit_robot_task(
            task="place",
            station="S05",
            task_number=S05_PLACE_TASK_NUMBER,
            variables=None,
            reset_variables={"任务号": 0},
            precheck=lambda: self._ensure_sensor_gate(S05_MATERIAL_SENSOR, False, "S05 放料目标位必须为空"),
            sample_id=sample_id,
            target_sensor_variable=S05_MATERIAL_SENSOR,
        )
