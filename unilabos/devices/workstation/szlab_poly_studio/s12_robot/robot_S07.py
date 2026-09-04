from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from unilabos.devices.workstation.szlab_poly_studio.sensor import S07Sensors
from unilabos.devices.workstation.szlab_poly_studio.s07_solid_addition.s07 import (
    SZLabS07SolidAdditionDevice,
)
from unilabos.devices.workstation.szlab_poly_studio.s07_solid_addition.sensors import (
    NODE_ALLOW_PROCESS,
    NODE_HOME,
    POSITION_RANGE,
)

from .robot_tasks import build_variables, powder_container_sensor


class SzlabRobotS07Mixin:
    def _validate_s072_position(self, position: int) -> int:
        position = int(position)
        if position not in (1, 2):
            raise ValueError("S072 位置必须在 1-2 范围内")
        return position

    def _resolve_s071_place_position(self, position: str) -> str:
        if str(position).strip().lower() != "auto":
            return str(position)
        while True:
            if self._plc_alarm_active():
                raise RuntimeError("PLC 报警已中止 S071 空位等待")
            read_errors: list[str] = []
            for candidate, sensor in S07Sensors.POWDER_CONTAINER_BY_POSITION.items():
                try:
                    occupied = bool(self._read_variable(sensor, use_cache=False))
                except Exception as exc:
                    read_errors.append(f"{candidate}: {exc}")
                    continue
                if not occupied:
                    return candidate
            if read_errors:
                raise RuntimeError(f"无法确定 S071 空位: {'; '.join(read_errors)}")
            time.sleep(self.poll_interval)

    def _run_s071_place(self, position: str = "1-1") -> dict[str, Any]:
        position = self._resolve_s071_place_position(position)
        sensor = powder_container_sensor(position)
        return self._submit_robot_task(
            task="place",
            station="S071",
            task_number=13,
            variables=build_variables("place_to_s071", S071取放料编号=self._slot_number(position)),
            reset_variables={"S071取放料编号": 0, "任务号": 0},
            precheck=lambda: self._ensure_sensor_gate(sensor, False, "S071 放粉罐目标位必须为空"),
            position=str(position),
            target_sensor_variable=sensor,
        )

    def _run_s071_pick(self, position: str = "1-1") -> dict[str, Any]:
        sensor = powder_container_sensor(position)
        return self._submit_robot_task(
            task="pick",
            station="S071",
            task_number=14,
            variables=build_variables("pick_from_s071", S071取放料编号=self._slot_number(position)),
            reset_variables={"S071取放料编号": 0, "任务号": 0},
            precheck=lambda: self._ensure_sensor_gate(sensor, True, "S071 取粉罐源位必须有粉罐"),
            position=str(position),
            source_sensor_variable=sensor,
        )

    def _run_s071_pick_and_rotate_to_feed(
        self,
        position: str = "1-1",
        load_position: int = 1,
    ) -> dict[str, Any]:
        position = str(position)
        load_position = int(load_position)
        sensor = powder_container_sensor(position)
        self._slot_number(position)
        if load_position not in POSITION_RANGE:
            raise ValueError("load_position 必须在 1-10 范围内")
        if self._plc_gateway is None:
            raise RuntimeError("S071 并行上料需要注入 szlab_poly_plc 网关")

        sensor_precheck = self._wait_sensor_conditions({sensor: True}, phase="pre")
        if not sensor_precheck["success"]:
            return {
                "success": False,
                "message": "S071 取粉罐源位等待失败",
                "status": "rejected",
                "position": position,
                "load_position": load_position,
                "sensor_precheck": sensor_precheck,
            }
        handshake_precheck = self._run_robot_handshake_precheck("S071")

        s07 = SZLabS07SolidAdditionDevice(
            plc_device_id=self.plc_device_id,
            poll_interval=0.2,
        )
        s07.set_plc_gateway(self._plc_gateway)
        if not s07._wait_plc_bool(NODE_HOME, True, "S07 原点信号"):
            return {
                "success": False,
                "message": "等待 S07 原点信号失败",
                "status": "rejected",
                "position": position,
                "load_position": load_position,
            }
        if not s07._wait_plc_bool(NODE_ALLOW_PROCESS, True, "S07 允许加工"):
            return {
                "success": False,
                "message": "等待 S07 允许加工失败",
                "status": "rejected",
                "position": position,
                "load_position": load_position,
            }

        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="S071ParallelLoading") as executor:
            robot_future = executor.submit(self._run_s071_pick, position)
            rotate_future = executor.submit(
                s07.rotate_powder_cartridge_to_feed,
                load_position,
            )
            try:
                robot_result = robot_future.result()
            except Exception as exc:
                robot_result = {"success": False, "message": str(exc)}
            try:
                rotate_result = rotate_future.result()
            except Exception as exc:
                rotate_result = {"success": False, "message": str(exc)}

        success = bool(robot_result.get("success")) and bool(rotate_result.get("success"))
        return {
            "success": success,
            "message": (
                "S071 取粉罐与 S07 旋转到上料位均已完成"
                if success
                else "S071 并行上料部分失败；现场状态可能已变化，禁止自动重试"
            ),
            "status": "completed" if success else "partial_failure",
            "position": position,
            "load_position": load_position,
            "handshake_precheck": handshake_precheck,
            "robot_pick": robot_result,
            "s07_rotate": rotate_result,
        }

    def _run_s072_place(self, product_type: int, position: int) -> dict[str, Any]:
        position = self._validate_s072_position(position)
        return self._submit_robot_task(
            task="place",
            station="S072",
            task_number=15,
            variables=build_variables("place_to_s072", S072取放料产品=product_type),
            reset_variables={"S072取放料产品": 0, "任务号": 0},
            product_type=int(product_type),
            position=position,
            sensor_check_skipped_reason="S072 取放料暂不检查传感器",
        )

    def _run_s072_pick(self, product_type: int, position: int) -> dict[str, Any]:
        position = self._validate_s072_position(position)
        return self._submit_robot_task(
            task="pick",
            station="S072",
            task_number=16,
            variables=build_variables("pick_from_s072", S072取放料产品=product_type),
            reset_variables={"S072取放料产品": 0, "任务号": 0},
            product_type=int(product_type),
            position=position,
            sensor_check_skipped_reason="S072 取放料暂不检查传感器",
        )
