from __future__ import annotations

import time
from typing import Any

from .robot_tasks import build_variables, s09_sensor
from unilabos.devices.workstation.szlab_poly_studio.s09_pipetting_station.sensors import (
    S09_HOME_SIGNALS,
    S09_PARAM_WRITTEN_VAR,
    S09_PROCESS_DONE_VAR,
    S09_PROCESS_SELECT_VAR,
)

S09_PARAM_WRITTEN_HOLD_SECONDS = 0.2


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

    def _reset_s09_safe_position_params(self) -> dict[str, Any]:
        return self._reset_pc_to_plc_variables(
            {
                S09_PARAM_WRITTEN_VAR: False,
                S09_PROCESS_SELECT_VAR: 0,
            }
        )

    def _prepare_s09_safe_position(self, product_type: int, position: int) -> dict[str, Any] | None:
        safe_position = self._s09_safe_position(product_type, position)
        home_signal = S09_HOME_SIGNALS[safe_position]
        self._write_variable(S09_PROCESS_SELECT_VAR, safe_position)
        self._write_variable(S09_PARAM_WRITTEN_VAR, False)
        self._write_variable(S09_PARAM_WRITTEN_VAR, True)
        time.sleep(S09_PARAM_WRITTEN_HOLD_SECONDS)
        self._write_variable(S09_PARAM_WRITTEN_VAR, False)
        if not self._wait_variable_equal(S09_PROCESS_DONE_VAR, safe_position, timeout=self.timeout, interval=self.poll_interval):
            reset_result = self._reset_s09_safe_position_params()
            return {
                "success": False,
                "message": f"S09 去安全位{safe_position}完成等待超时",
                "s09_safe_position": safe_position,
                "s09_home_signal": home_signal,
                "reset": reset_result,
            }
        if not self._wait_variable_equal(home_signal, True, timeout=self.timeout, interval=self.poll_interval):
            reset_result = self._reset_s09_safe_position_params()
            return {
                "success": False,
                "message": f"S09 安全位{safe_position}原点信号等待超时",
                "s09_safe_position": safe_position,
                "s09_home_signal": home_signal,
                "reset": reset_result,
            }
        self._reset_s09_safe_position_params()
        return None

    def _run_s09_place(self, product_type: int, position: int) -> dict[str, Any]:
        sensor = s09_sensor(product_type, position)
        safe_position = self._s09_safe_position(product_type, position)

        def precheck():
            safe_result = self._prepare_s09_safe_position(product_type, position)
            if safe_result is not None:
                return safe_result
            if sensor:
                return self._ensure_sensor_gate(sensor, False, "S09 放料目标位必须为空")
            return None

        return self._submit_robot_task(
            task="place",
            station="S09",
            task_number=19,
            variables=build_variables("place_to_s09", S09取放料产品=product_type, S09取放料编号=position),
            reset_variables={"S09取放料产品": 0, "S09取放料编号": 0, "任务号": 0},
            precheck=precheck,
            product_type=int(product_type),
            position=int(position),
            s09_safe_position=safe_position,
            s09_home_signal=S09_HOME_SIGNALS[safe_position],
            target_sensor_variable=sensor,
        )

    def _run_s09_pick(self, product_type: int, position: int) -> dict[str, Any]:
        sensor = s09_sensor(product_type, position)
        safe_position = self._s09_safe_position(product_type, position)

        def precheck():
            safe_result = self._prepare_s09_safe_position(product_type, position)
            if safe_result is not None:
                return safe_result
            if sensor:
                return self._ensure_sensor_gate(sensor, True, "S09 取料源位必须有物料")
            return None

        return self._submit_robot_task(
            task="pick",
            station="S09",
            task_number=20,
            variables=build_variables("pick_from_s09", S09取放料产品=product_type, S09取放料编号=position),
            reset_variables={"S09取放料产品": 0, "S09取放料编号": 0, "任务号": 0},
            precheck=precheck,
            product_type=int(product_type),
            position=int(position),
            s09_safe_position=safe_position,
            s09_home_signal=S09_HOME_SIGNALS[safe_position],
            source_sensor_variable=sensor,
        )
