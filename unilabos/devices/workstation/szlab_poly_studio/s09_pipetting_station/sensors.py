"""S09 移液站 OPC 变量与工艺映射。"""

from __future__ import annotations

from pathlib import Path

from unilabos.devices.workstation.szlab_poly_studio.sensor import S09Sensors


CSV_REFERENCE = str(Path(__file__).resolve().parent / "pipetting_station_nodes.csv")

S09_HOME_SIGNALS: dict[int, str] = {
    1: "S09原点信号_1",
    2: "S09原点信号_2",
    3: "S09原点信号_3",
    4: "S09原点信号_4",
}

S09_HOME_LABELS: dict[int, str] = {
    1: "机器人 TIP 盒取放",
    2: "机器人液体试剂 1/2/3 取放",
    3: "机器人液体试剂 4/5 取放",
    4: "机器人烧杯取放",
}

S09_PROCESS_LABELS: dict[int, str] = {
    5: "取 TIP",
    6: "放 TIP",
    7: "液体瓶取液（润洗一次后取液）",
    8: "烧杯放液",
    9: "测密度抽排液并记录天平数据",
}

S09_PROCESS_SELECT_VAR = "S09工艺选择"
S09_PARAM_WRITTEN_VAR = "S09参数写入完成"
S09_PROCESS_DONE_VAR = "S09工艺完成"
S09_ALLOW_PROCESS_VAR = "S09允许加工"
S09_STATION_STATUS_VAR = "工站状态[8]"

S09_TIP_BOX_VAR = "S09TIP盒工位编号"
S09_TIP_VAR = "S09TIP编号"
S09_LIQUID_BOTTLE_VAR = "S09液体瓶编号"
S09_ASPIRATE_VOLUME_VAR = "S09抽液量"
S09_DISPENSE_VOLUME_VAR = "S09放液量"

S09_BALANCE_STABLE_VAR = "S09天平读数稳定"
S09_BALANCE_READING_VAR = "S09天平读数"
S09_DENSITY_COUNT_VAR = "S09测密度次数"
S09_ASPIRATE_BALANCE_READINGS_VAR = "S09抽液天平读数"
S09_DISPENSE_BALANCE_READINGS_VAR = "S09放液天平读数"
S09_DENSITY_COUNT_RANGE = range(1, 11)

S09_TRANSFER_PRODUCT_VAR = "S09取放料产品"
S09_TRANSFER_POSITION_VAR = "S09取放料编号"
PLC_ROBOT_TASK_VAR = "PLC_R任务号"

S09_TIP_BOX_RANGE = range(1, 3)
S09_TIP_RANGE = range(1, 97)
S09_LIQUID_BOTTLE_RANGE = range(1, 6)
S09_STATION_RANGE = range(1, 6)
S09_TIP_BOX_SENSORS = S09Sensors.TIP_BOX
S09_STATION_SENSORS = S09Sensors.STATION


def s09_remaining_volume_var(bottle: int) -> str:
    return f"S09液体瓶{int(bottle)}剩余液量"


def s09_remaining_volume_vars() -> list[str]:
    return [s09_remaining_volume_var(index) for index in S09_LIQUID_BOTTLE_RANGE]


def s09_density_balance_vars(base_name: str) -> list[str]:
    return [f"{base_name}[{index}]" for index in range(10)]


def validate_density_count(count: int) -> int:
    count = int(count)
    if count not in S09_DENSITY_COUNT_RANGE:
        raise ValueError("S09 测密度次数必须在 1-10 范围内")
    return count


def validate_process(process: int) -> int:
    process = int(process)
    if process not in S09_PROCESS_LABELS:
        raise ValueError("S09 工艺选择必须在 5-9 范围内")
    return process


def validate_home_position(home_position: int) -> int:
    home_position = int(home_position)
    if home_position not in S09_HOME_SIGNALS:
        raise ValueError("S09 安全位必须在 1-4 范围内")
    return home_position


def validate_station(station: int) -> int:
    station = int(station)
    if station not in S09_STATION_RANGE:
        raise ValueError("S09 加液/烧杯工位必须在 1-5 范围内")
    return station


def validate_tip_box(tip_box: int) -> int:
    tip_box = int(tip_box)
    if tip_box not in S09_TIP_BOX_RANGE:
        raise ValueError("S09 TIP 盒工位编号必须在 1-2 范围内")
    return tip_box


def validate_tip(tip: int) -> int:
    tip = int(tip)
    if tip not in S09_TIP_RANGE:
        raise ValueError("S09 TIP 编号必须在 1-96 范围内")
    return tip


def validate_liquid_bottle(bottle: int) -> int:
    bottle = int(bottle)
    if bottle not in S09_LIQUID_BOTTLE_RANGE:
        raise ValueError("S09 液体瓶编号必须在 1-5 范围内")
    return bottle


def s09_opcua_node_id_map() -> dict[str, str]:
    """S09 调试时可直接使用的 NodeId 映射，变量名与 PLC 浏览名保持一致。"""
    names = [
        *S09_HOME_SIGNALS.values(),
        S09_ALLOW_PROCESS_VAR,
        S09_PROCESS_SELECT_VAR,
        S09_PROCESS_DONE_VAR,
        S09_TIP_BOX_VAR,
        S09_TIP_VAR,
        S09_LIQUID_BOTTLE_VAR,
        S09_ASPIRATE_VOLUME_VAR,
        S09_DISPENSE_VOLUME_VAR,
        S09_BALANCE_READING_VAR,
        S09_DENSITY_COUNT_VAR,
        *s09_density_balance_vars(S09_ASPIRATE_BALANCE_READINGS_VAR),
        *s09_density_balance_vars(S09_DISPENSE_BALANCE_READINGS_VAR),
        *s09_remaining_volume_vars(),
        S09_STATION_STATUS_VAR,
        PLC_ROBOT_TASK_VAR,
        S09_TRANSFER_PRODUCT_VAR,
        S09_TRANSFER_POSITION_VAR,
        *S09_TIP_BOX_SENSORS.values(),
        *S09_STATION_SENSORS.values(),
    ]
    return {name: f"ns=4;s=上位机通讯|{name}" for name in names}
