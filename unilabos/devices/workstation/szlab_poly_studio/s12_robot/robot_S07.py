from __future__ import annotations

from typing import Any

from unilabos.devices.workstation.szlab_poly_studio.sensor import S07Sensors

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
        raise RuntimeError("S071 没有可用于旧粉罐回库的空位")

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
