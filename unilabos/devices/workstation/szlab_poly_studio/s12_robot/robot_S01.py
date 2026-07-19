from __future__ import annotations

from typing import Any

from .robot_tasks import build_variables


class SzlabRobotS01Mixin:
    def _validate_s01_position(self, position: int = 1) -> int:
        position = int(position)
        if position != 1:
            raise ValueError("S01 取料位置必须在 1-1 范围内")
        return position

    def _run_s01_pick(
        self,
        product_type: int,
        position: int = 1,
    ) -> dict[str, Any]:
        position = self._validate_s01_position(position)
        return self._submit_robot_task(
            task="pick",
            station="S01",
            task_number=1,
            variables=build_variables("pick_from_s01", S01出入料产品=product_type, S01取放料编号=position),
            reset_variables={"S01出入料产品": 0, "S01取放料编号": 0, "任务号": 0},
            product_type=int(product_type),
            position=position,
            sensor_check_skipped_reason="S01 暂无物料传感器",
        )
