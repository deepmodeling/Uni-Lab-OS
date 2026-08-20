from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from unilabos.devices.workstation.szlab_poly_studio.sensor import (
    STACK_SENSOR_GROUPS,
    S02Sensors,
    S03Sensors,
    S04Sensors,
    S05Sensors,
    S06Sensors,
    S07Sensors,
    S09Sensors,
    S10Sensors,
    S11Sensors,
)

GateKind = Literal["pick", "place", "pour"]

ROBOT_HOME_VARIABLE = "Robot_Home"
ROBOT_WRITE_ALLOWED_VARIABLE = "Robot_任务允许写入"
ROBOT_WRITE_DONE_VARIABLE = "Robot_任务写入完成"
ROBOT_TASK_NUMBER_VARIABLE = "任务号"
ROBOT_TASK_COMPLETE_VARIABLE = "Robot_任务完成"
S05_MATERIAL_SENSOR = S05Sensors.MATERIAL
S06_MATERIAL_SENSOR = S06Sensors.MATERIAL


@dataclass(frozen=True)
class RobotActionSpec:
    method_name: str
    station: str
    task: GateKind
    task_number: int
    description: str
    variables: tuple[str, ...] = ()


ROBOT_ACTION_SPECS: dict[str, RobotActionSpec] = {
    "pick_from_s01": RobotActionSpec(
        "pick_from_s01",
        "S01",
        "pick",
        1,
        "S01 取料产品选择",
        ("S01出入料产品", "S01取放料编号"),
    ),
    "place_to_s02": RobotActionSpec("place_to_s02", "S02", "place", 3, "S02 放 TIP", ("S02取放料编号",)),
    "pick_from_s02": RobotActionSpec("pick_from_s02", "S02", "pick", 4, "S02 取 TIP", ("S02取放料编号",)),
    "place_to_s03": RobotActionSpec(
        "place_to_s03",
        "S03",
        "place",
        5,
        "S03 放容器",
        ("S03取放料产品", "S03取放料编号"),
    ),
    "pick_from_s03": RobotActionSpec(
        "pick_from_s03",
        "S03",
        "pick",
        6,
        "S03 取容器",
        ("S03取放料产品", "S03取放料编号"),
    ),
    "place_to_s04": RobotActionSpec("place_to_s04", "S04", "place", 7, "S04 放料", ("S04取放料编号",)),
    "pick_from_s04": RobotActionSpec("pick_from_s04", "S04", "pick", 8, "S04 取料", ("S04取放料编号",)),
    "place_to_s05": RobotActionSpec("place_to_s05", "S05", "place", 9, "S05 放料"),
    "pick_from_s05": RobotActionSpec("pick_from_s05", "S05", "pick", 10, "S05 取料"),
    "place_to_s06": RobotActionSpec("place_to_s06", "S06", "place", 11, "S06 放料"),
    "pick_from_s06": RobotActionSpec("pick_from_s06", "S06", "pick", 12, "S06 取料"),
    "place_to_s071": RobotActionSpec("place_to_s071", "S071", "place", 13, "S071 放粉罐", ("S071取放料编号",)),
    "pick_from_s071": RobotActionSpec("pick_from_s071", "S071", "pick", 14, "S071 取粉罐", ("S071取放料编号",)),
    "place_to_s072": RobotActionSpec("place_to_s072", "S072", "place", 15, "S072 放产品", ("S072取放料产品",)),
    "pick_from_s072": RobotActionSpec("pick_from_s072", "S072", "pick", 16, "S072 取产品", ("S072取放料产品",)),
    "place_to_s08": RobotActionSpec(
        "place_to_s08",
        "S08",
        "place",
        17,
        "S08 放瓶",
        ("S08取放料产品", "S08取放料编号"),
    ),
    "pick_from_s08": RobotActionSpec(
        "pick_from_s08",
        "S08",
        "pick",
        18,
        "S08 取瓶",
        ("S08取放料产品", "S08取放料编号"),
    ),
    "pour_from_s08": RobotActionSpec("pour_from_s08", "S08", "pour", 25, "S08 倒料", ("S08倒料产品选择",)),
    "place_to_s09": RobotActionSpec(
        "place_to_s09",
        "S09",
        "place",
        19,
        "S09 放料",
        ("S09取放料产品", "S09取放料编号"),
    ),
    "pick_from_s09": RobotActionSpec(
        "pick_from_s09",
        "S09",
        "pick",
        20,
        "S09 取料",
        ("S09取放料产品", "S09取放料编号"),
    ),
    "place_to_s10": RobotActionSpec("place_to_s10", "S10", "place", 21, "S10 放试剂瓶", ("S10取放料编号",)),
    "pick_from_s10": RobotActionSpec("pick_from_s10", "S10", "pick", 22, "S10 取试剂瓶", ("S10取放料编号",)),
    "place_to_s11": RobotActionSpec(
        "place_to_s11",
        "S11",
        "place",
        23,
        "S11 放成品",
        ("S11取放料产品", "S11取放料编号"),
    ),
    "pick_from_s11": RobotActionSpec(
        "pick_from_s11",
        "S11",
        "pick",
        24,
        "S11 取成品",
        ("S11取放料产品", "S11取放料编号"),
    ),
}


def numbered_position(position: int, *, min_value: int, max_value: int, label: str) -> int:
    position = int(position)
    if position < min_value or position > max_value:
        raise ValueError(f"{label}必须在 {min_value}-{max_value} 范围内")
    return position


def s02_sensor(position: int) -> str:
    return S02Sensors.TIP_BOX[str(numbered_position(position, min_value=1, max_value=6, label="S02 TIP位置"))]


def s04_sensor(position: int) -> str:
    return S04Sensors.material(position)


def s09_sensor(product_type: int, position: int) -> str:
    product_type = int(product_type)
    position = int(position)
    if product_type == 1:
        if position not in S09Sensors.TIP_BOX:
            raise ValueError("S09 TIP位置必须是 1-2")
        return S09Sensors.TIP_BOX[position]
    if product_type == 2:
        position = numbered_position(position, min_value=1, max_value=5, label="S09 液体试剂瓶位置")
        return S09Sensors.STATION[position]
    if product_type in {3, 4}:
        if position != 1:
            raise ValueError("S09 烧杯位置必须是 1")
        return S09Sensors.STATION[position]
    raise ValueError("S09取放料产品必须是 1(TIP盒)、2(液体试剂瓶)、3(烧杯) 或 4(测密度烧杯)")


def s10_sensor(position: int) -> str:
    values = list(S10Sensors.LIQUID_REAGENT.values())
    position = numbered_position(position, min_value=1, max_value=len(values), label="S10试剂瓶位置")
    return values[position - 1]


def product_slot_sensor(product_type: int, position: str | int, *, used: bool) -> str:
    product_type = int(product_type)
    key = str(position)
    if product_type == 1:
        sensors = S11Sensors.USED_BEAKER if used else S03Sensors.UNUSED_BEAKER
        label = "烧杯"
    elif product_type in (2, 3):
        sensors = S11Sensors.USED_SAMPLE_VIAL if used else S03Sensors.UNUSED_SAMPLE_VIAL
        label = "样品瓶"
    else:
        raise ValueError("产品类型必须是 1(烧杯)、2(样品瓶250ml) 或 3(样品瓶500ml)")
    if key not in sensors:
        raise ValueError(f"{label}位置不存在: {key}")
    return sensors[key]


def powder_container_sensor(position: str | int) -> str:
    key = str(position)
    sensors = S07Sensors.POWDER_CONTAINER_BY_POSITION
    if key not in sensors:
        raise ValueError(f"固体粉末容器位置不存在: {key}")
    return sensors[key]


def build_variables(spec_name: str, **kwargs: Any) -> dict[str, Any]:
    spec = ROBOT_ACTION_SPECS[spec_name]
    return {name: int(kwargs[name]) for name in spec.variables}
