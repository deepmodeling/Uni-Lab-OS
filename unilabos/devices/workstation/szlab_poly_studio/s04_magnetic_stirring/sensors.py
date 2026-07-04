"""SZLab Poly Studio S04 磁搅工位 OPC UA 变量。"""

S04_POSITION_RANGE = range(1, 7)
S04_PROCESS_MODES = {
    1: "搅拌",
    2: "加热",
    3: "搅拌+加热",
}


def s04_station_prefix(position: int) -> str:
    return f"S04{int(position)}"


def s04_allow_var(position: int) -> str:
    return f"{s04_station_prefix(position)}允许加工"


def s04_process_var(position: int) -> str:
    return f"{s04_station_prefix(position)}磁搅工艺选择"


def s04_params_written_var(position: int) -> str:
    return f"{s04_station_prefix(position)}参数写入完成"


def s04_done_var(position: int) -> str:
    return f"{s04_station_prefix(position)}加工完成"


def s04_temperature_feedback_var(position: int) -> str:
    return f"磁搅温度反馈_上位机[{int(position) - 1}]"


def s04_temperature_var(position: int) -> str:
    return f"磁搅温度设置_上位机[{int(position) - 1}]"


def s04_speed_var(position: int) -> str:
    return f"磁搅速度设置_上位机[{int(position) - 1}]"


def s04_duration_var(position: int) -> str:
    return f"磁搅时间设置_上位机[{int(position) - 1}]"


def s04_safe_temperature_var(position: int) -> str:
    return f"磁搅安全温度设置_上位机[{int(position) - 1}]"


def s04_public_variables() -> list[str]:
    variables: list[str] = []
    for position in S04_POSITION_RANGE:
        variables.extend(
            [
                s04_allow_var(position),
                s04_process_var(position),
                s04_params_written_var(position),
                s04_done_var(position),
                s04_temperature_feedback_var(position),
                s04_speed_var(position),
                s04_temperature_var(position),
                s04_duration_var(position),
                s04_safe_temperature_var(position),
            ]
        )
    return variables


def s04_opcua_node_id_map() -> dict[str, str]:
    return {name: f"ns=4;s=上位机通讯|{name}" for name in s04_public_variables()}
