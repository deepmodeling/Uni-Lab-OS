from __future__ import annotations

from typing import Any

from .robot_tasks import build_variables

S08_PICK_SENSOR_BY_POSITION = {
    1: "传感器状态_上位机[3].NO[14]",
    2: "传感器状态_上位机[3].NO[15]",
}
S08_PLACE_SENSOR_BY_POSITION = {
    1: "传感器状态_上位机[4].NO[0]",
    2: "传感器状态_上位机[4].NO[1]",
    3: "传感器状态_上位机[4].NO[2]",
    4: "传感器状态_上位机[4].NO[3]",
    5: "传感器状态_上位机[4].NO[4]",
}


class SzlabRobotS08Mixin:
    def _s08_place_sensor_variable(self, position: int) -> str:
        position = int(position)
        if position not in S08_PLACE_SENSOR_BY_POSITION:
            raise ValueError("S08 放瓶位置必须在 1-5 范围内")
        return S08_PLACE_SENSOR_BY_POSITION[position]

    def _s08_pick_sensor_variable(self, position: int) -> str:
        position = int(position)
        if position not in S08_PICK_SENSOR_BY_POSITION:
            raise ValueError("S08 取瓶位置必须在 1-2 范围内")
        return S08_PICK_SENSOR_BY_POSITION[position]

    def _validate_s08_pour_product_type(self, product_type: int) -> int:
        product_type = int(product_type)
        if product_type not in (1, 2):
            raise ValueError("S08倒料产品选择必须是 1(样品瓶250ml) 或 2(样品瓶500ml)")
        return product_type

    def _run_s08_place(self, product_type: int, position: int) -> dict[str, Any]:
        sensor = self._s08_place_sensor_variable(position)
        return self._submit_robot_task(
            task="place",
            station="S08",
            task_number=17,
            variables=build_variables("place_to_s08", S08取放料产品=product_type, S08取放料编号=position),
            reset_variables={"S08取放料产品": 0, "S08取放料编号": 0, "任务号": 0},
            precheck=lambda: self._ensure_sensor_gate(sensor, False, "S08 放料目标位必须为空"),
            product_type=int(product_type),
            position=int(position),
            target_sensor_variable=sensor,
        )

    def _run_s08_pick(self, product_type: int, position: int) -> dict[str, Any]:
        sensor = self._s08_pick_sensor_variable(position)
        return self._submit_robot_task(
            task="pick",
            station="S08",
            task_number=18,
            variables=build_variables("pick_from_s08", S08取放料产品=product_type, S08取放料编号=position),
            reset_variables={"S08取放料产品": 0, "S08取放料编号": 0, "任务号": 0},
            precheck=lambda: self._ensure_sensor_gate(sensor, True, "S08 取料源位必须有物料"),
            product_type=int(product_type),
            position=int(position),
            source_sensor_variable=sensor,
        )

    def _run_s08_pour(self, product_type: int) -> dict[str, Any]:
        product_type = self._validate_s08_pour_product_type(product_type)
        return self._submit_robot_task(
            task="pour",
            station="S08",
            task_number=25,
            variables=build_variables("pour_from_s08", S08倒料产品选择=product_type),
            reset_variables={"S08倒料产品选择": 0, "任务号": 0},
            product_type=product_type,
        )
