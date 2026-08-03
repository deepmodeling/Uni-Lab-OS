from __future__ import annotations

from typing import Any

from .robot_tasks import build_variables, s09_sensor
from unilabos.devices.workstation.szlab_poly_studio.s09_pipetting_station.sensors import (
    S09_HOME_SIGNALS,
)


class SzlabRobotS09Mixin:
    def _s09_safe_position(self, product_type: int, position: int) -> int:
        product_type = int(product_type)
        position = int(position)
        if product_type == 1:
            return 1
        if product_type == 2:
            return 2 if position <= 3 else 3
        if product_type == 3:
            return 4
        raise ValueError("S09取放料产品必须是 1(TIP盒)、2(液体试剂瓶) 或 3(烧杯)")

    def _run_s09_place(self, product_type: int, position: int) -> dict[str, Any]:
        sensor = s09_sensor(product_type, position)
        safe_position = self._s09_safe_position(product_type, position)

        return self._submit_robot_task(
            task="place",
            station="S09",
            task_number=19,
            variables=build_variables("place_to_s09", S09取放料产品=product_type, S09取放料编号=position),
            reset_variables={"S09取放料产品": 0, "S09取放料编号": 0, "任务号": 0},
            product_type=int(product_type),
            position=int(position),
            s09_safe_position=safe_position,
            s09_home_signal=S09_HOME_SIGNALS[safe_position],
            target_sensor_variable=sensor,
        )

    def _run_s09_pick(self, product_type: int, position: int) -> dict[str, Any]:
        sensor = s09_sensor(product_type, position)
        safe_position = self._s09_safe_position(product_type, position)

        return self._submit_robot_task(
            task="pick",
            station="S09",
            task_number=20,
            variables=build_variables("pick_from_s09", S09取放料产品=product_type, S09取放料编号=position),
            reset_variables={"S09取放料产品": 0, "S09取放料编号": 0, "任务号": 0},
            product_type=int(product_type),
            position=int(position),
            s09_safe_position=safe_position,
            s09_home_signal=S09_HOME_SIGNALS[safe_position],
            source_sensor_variable=sensor,
        )
