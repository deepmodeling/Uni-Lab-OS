from __future__ import annotations

import os
from typing import Any

from unilabos.devices.workstation.szlab_poly_studio.s04_magnetic_stirring.sensors import s04_ready_var
from unilabos.devices.workstation.szlab_poly_studio.s05_photoshotting.sensors import S05_READY
from unilabos.devices.workstation.szlab_poly_studio.sensor import S04Sensors

from .robot_tasks import S05_MATERIAL_SENSOR


S04_PLACE_TASK_NUMBER = 7
S04_PICK_TASK_NUMBER = 8
S04_POSITION_RANGE = range(1, 5)
S04_POSITION_VARIABLE = "S04取放料编号"
S04_SENSOR_BY_POSITION = S04Sensors.MATERIAL_BY_POSITION


class SzlabRobotS04Mixin:
    def _validate_s04_position(self, position: int) -> int:
        position = int(position)
        if position not in S04_POSITION_RANGE:
            raise ValueError("磁搅位置必须在 1-4 范围内")
        return position

    def _s04_sensor_variable(self, position: int) -> str:
        return S04_SENSOR_BY_POSITION[self._validate_s04_position(position)]

    def _read_s04_position_occupied(self, position: int) -> bool:
        return bool(self._read_variable(self._s04_sensor_variable(position), use_cache=False))

    def _ensure_s04_pick_allowed(self, position: int) -> dict[str, Any] | None:
        sensor_variable = self._s04_sensor_variable(position)
        if os.environ.get("SKIP_SENSOR_PRECHECK") == "1":
            return None
        if self._should_skip_robot_precheck_variable(sensor_variable):
            return None
        occupied = self._read_s04_position_occupied(position)
        if occupied:
            return None
        return {
            "success": False,
            "message": f"S04 位置 {position} 无物料，机械臂不能取料",
            "task": "pick",
            "station": "S04",
            "position": position,
            "sensor_variable": self._s04_sensor_variable(position),
            "occupied": occupied,
        }

    def _run_s04_pick(self, position: int) -> dict[str, Any]:
        position = self._validate_s04_position(position)
        source_sensor_variable = self._s04_sensor_variable(position)
        return self._submit_robot_task(
            task="pick",
            station="S04",
            task_number=S04_PICK_TASK_NUMBER,
            variables={S04_POSITION_VARIABLE: position},
            reset_variables={S04_POSITION_VARIABLE: 0, "任务号": 0},
            position=position,
            source_sensor_variable=source_sensor_variable,
            pre_sensor_conditions={
                source_sensor_variable: True,
                S05_MATERIAL_SENSOR: False,
                S05_READY: True,
            },
            post_sensor_conditions={source_sensor_variable: False},
        )

    def _run_s04_place(self, position: int, sample_id: str = "") -> dict[str, Any]:
        position = self._validate_s04_position(position)
        target_sensor_variable = self._s04_sensor_variable(position)
        return self._submit_robot_task(
            task="place",
            station="S04",
            task_number=S04_PLACE_TASK_NUMBER,
            variables={S04_POSITION_VARIABLE: position},
            reset_variables={S04_POSITION_VARIABLE: 0, "任务号": 0},
            position=position,
            sample_id=sample_id,
            target_sensor_variable=target_sensor_variable,
            pre_sensor_conditions={
                target_sensor_variable: False,
                s04_ready_var(position): True,
            },
            post_sensor_conditions={target_sensor_variable: True},
        )
