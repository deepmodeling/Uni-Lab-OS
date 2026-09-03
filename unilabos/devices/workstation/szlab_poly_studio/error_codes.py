"""Mixing 设备运行时故障码。

故障码依据《Mixing 设备操作与故障处理指南》定义。这里只对驱动在运行时
能够确认的失败进行归类；需要人工观察才能确认的原因不会被主动推断。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MixingErrorDefinition:
    code: str
    station: str
    title: str
    recovery: str


@dataclass(frozen=True)
class PlcAlarmDefinition:
    """PLC 报警位与 UniLab 报错码的确定性映射。"""

    address: str
    variable_name: str
    code: str
    station: str
    title: str
    recovery: str


MIXING_ERROR_CATALOG: dict[str, MixingErrorDefinition] = {
    "MIX-S04-001": MixingErrorDefinition(
        "MIX-S04-001", "S04", "磁力搅拌工站未就绪或初始化异常",
        "切换手动模式，清空 S04 所有磁搅位置，复位并单独初始化 S04。",
    ),
    "MIX-S05-001": MixingErrorDefinition(
        "MIX-S05-001", "S05", "拍照或照明动作异常",
        "检查拍照电脑和程序，解锁后等待约 30 秒，并在手动界面执行一次单次运行。",
    ),
    "MIX-S05-002": MixingErrorDefinition(
        "MIX-S05-002", "S05", "拍照旋转机构未到位",
        "停止后续动作，切换手动模式，确认安全后校正旋转机构并联系工程师。",
    ),
    "MIX-S06-001": MixingErrorDefinition(
        "MIX-S06-001", "S06", "泵加液工站未就绪或未动作",
        "切换手动模式，复位并单独初始化 S06，确认工站准备好后重新启动。",
    ),
    "MIX-S06-002": MixingErrorDefinition(
        "MIX-S06-002", "S06", "泵加液 Task 配置异常",
        "检查 Task，确认泵加液动作未被设置为跳过，修正后重新执行。",
    ),
    "MIX-S07-001": MixingErrorDefinition(
        "MIX-S07-001", "S07", "固体加样工站未就绪或未动作",
        "切换手动模式，复位并单独初始化 S07，确认工站准备好后重新启动。",
    ),
    "MIX-S07-002": MixingErrorDefinition(
        "MIX-S07-002", "S07", "天平读数异常或注粉不下粉",
        "切换手动模式，检查天平读数并清零，确认正常后重新执行加粉。",
    ),
    "MIX-S07-003": MixingErrorDefinition(
        "MIX-S07-003", "S07", "持续注粉或粉罐空罐",
        "停止自动运行，检查天平读数和粉罐余量，由授权人员排除异常后恢复。",
    ),
    "MIX-S08-001": MixingErrorDefinition(
        "MIX-S08-001", "S08", "开关盖工站异常",
        "切换手动模式，检查瓶体、瓶盖和机构干涉，复位并单独初始化 S08。",
    ),
    "MIX-S09-001": MixingErrorDefinition(
        "MIX-S09-001", "S09", "液体加样工站未就绪或未动作",
        "切换手动模式，单独初始化 S09，确认工站准备好后重新启动。",
    ),
    "MIX-S09-002": MixingErrorDefinition(
        "MIX-S09-002", "S09", "TIP 取放异常",
        "切换手动模式，检查缺失、重复或歪斜的 TIP，复位并初始化移液枪动作模块。",
    ),
    "MIX-S09-003": MixingErrorDefinition(
        "MIX-S09-003", "S09", "TIP 探液、吸液或排液异常",
        "切换手动模式并复位，初始化移液枪动作模块，检查 TIP 是否触底。",
    ),
    "MIX-S12-001": MixingErrorDefinition(
        "MIX-S12-001", "S12", "机械臂动作或目标位置异常",
        "立即急停并检查碰撞；由受训人员手动回原点、初始化夹爪和 S12。",
    ),
    "MIX-S12-002": MixingErrorDefinition(
        "MIX-S12-002", "S12", "机械臂任务握手条件未满足",
        "检查任务允许写入、任务写入完成和上一任务完成状态，复位后重试 workflow。",
    ),
}


def _plc_alarm(
    address: str,
    variable_name: str,
    code: str,
    station: str,
    title: str,
    recovery: str,
) -> PlcAlarmDefinition:
    return PlcAlarmDefinition(address, variable_name, code, station, title, recovery)


_RESET_RECOVERY = "暂停 Task，切换手动模式，排除报警原因后复位；确认工站准备好再恢复自动运行。"
_ROBOT_RECOVERY = "立即停止后续动作并检查机构和物料；确认安全后复位、初始化机械臂及对应工站。"

# 数据来源：报错信息.csv 中带有明确“注释”的 D500-D525 报警位。
# 未注释的预留位不映射，避免把未知位误报为已知故障。
PLC_ALARM_MAP: dict[str, PlcAlarmDefinition] = {
    item.address: item
    for item in (
        _plc_alarm("D500.0", "公共_报警[0].NO[0]", "MIX-COMMON-101", "公共", "急停报警", "保持设备停止，确认现场安全并排除急停原因后再复位。"),
        _plc_alarm("D500.1", "公共_报警[0].NO[1]", "MIX-COMMON-102", "公共", "初始化超时报警", "切换手动模式，检查各工站初始化条件，复位后单独初始化异常工站。"),
        _plc_alarm("D500.2", "公共_报警[0].NO[2]", "MIX-COMMON-103", "公共", "COM0 通信异常", "检查 COM0 串口、电源、线缆和通信参数，恢复通信后复位。"),
        _plc_alarm("D500.3", "公共_报警[0].NO[3]", "MIX-COMMON-104", "公共", "COM2 通信异常", "检查 COM2 串口、电源、线缆和通信参数，恢复通信后复位。"),
        _plc_alarm("D500.4", "公共_报警[0].NO[4]", "MIX-COMMON-105", "公共", "COM3 通信异常", "检查 COM3 串口、电源、线缆和通信参数，恢复通信后复位。"),
        _plc_alarm("D510.0", "S05报警[0].NO[0]", "MIX-S05-101", "S05", "拍照伺服报警", _RESET_RECOVERY),
        _plc_alarm("D510.1", "S05报警[0].NO[1]", "MIX-S05-102", "S05", "机器人在 S05 取放料时拍照电机 Busy", _ROBOT_RECOVERY),
        _plc_alarm("D514.0", "S07报警[0].NO[0]", "MIX-S07-101", "S07", "加样仪报警", _RESET_RECOVERY),
        _plc_alarm("D516.0", "S08报警[0].NO[0]", "MIX-S08-101", "S08", "开盖左右电机报警", _RESET_RECOVERY),
        _plc_alarm("D516.1", "S08报警[0].NO[1]", "MIX-S08-102", "S08", "开盖上下电机报警", _RESET_RECOVERY),
        _plc_alarm("D516.2", "S08报警[0].NO[2]", "MIX-S08-103", "S08", "机器人在 S08 取放料时工位不在安全位", _ROBOT_RECOVERY),
        _plc_alarm("D518.0", "S09报警[0].NO[0]", "MIX-S09-101", "S09", "移液左右电机报警", _RESET_RECOVERY),
        _plc_alarm("D518.1", "S09报警[0].NO[1]", "MIX-S09-102", "S09", "移液前后电机报警", _RESET_RECOVERY),
        _plc_alarm("D518.2", "S09报警[0].NO[2]", "MIX-S09-103", "S09", "移液枪错误", "暂停 Task，切换手动模式并复位，检查 TIP 后初始化移液枪动作模块。"),
        _plc_alarm("D518.3", "S09报警[0].NO[3]", "MIX-S09-104", "S09", "天平错误", "检查 S09 天平状态、读数和连接，恢复正常后复位。"),
        _plc_alarm("D518.4", "S09报警[0].NO[4]", "MIX-S09-105", "S09", "天平无效命令码", "检查发送给 S09 天平的命令及通信协议，修正后复位。"),
        _plc_alarm("D518.5", "S09报警[0].NO[5]", "MIX-S09-106", "S09", "天平无法连接", "检查 S09 天平电源、通信线缆、端口和通信参数，恢复连接后复位。"),
        _plc_alarm("D518.6", "S09报警[0].NO[6]", "MIX-S09-107", "S09", "天平命令反馈报错", "记录天平反馈，检查命令和天平状态，排除原因后复位。"),
        _plc_alarm("D518.7", "S09报警[0].NO[7]", "MIX-S09-108", "S09", "天平通信超时", "检查天平连接和响应状态，恢复通信后复位并重试。"),
        _plc_alarm("D518.8", "S09报警[0].NO[8]", "MIX-S09-109", "S09", "天平其他错误", "记录天平原始错误信息，检查天平状态，排除原因后复位。"),
        _plc_alarm("D518.9", "S09报警[0].NO[9]", "MIX-S09-110", "S09", "机器人在 S09 取放料时工位不在安全位", _ROBOT_RECOVERY),
        _plc_alarm("D518.10", "S09报警[0].NO[10]", "MIX-S09-111", "S09", "探液后吸液动作将超过移液枪最大行程", "停止吸液动作，检查液面、容器位置和吸液参数，初始化移液枪模块后重试。"),
        _plc_alarm("D520.0", "S10报警[0].NO[0]", "MIX-S10-101", "S10", "机器人报警", _ROBOT_RECOVERY),
        _plc_alarm("D520.1", "S10报警[0].NO[1]", "MIX-S10-102", "S10", "移栽伺服报警", _RESET_RECOVERY),
        _plc_alarm("D522.0", "S11报警[0].NO[0]", "MIX-S11-101", "S11", "输送伺服报警", _RESET_RECOVERY),
        _plc_alarm("D524.0", "S12报警[0].NO[0]", "MIX-S12-101", "S12", "机器人移栽伺服报警", _ROBOT_RECOVERY),
        _plc_alarm("D524.1", "S12报警[0].NO[1]", "MIX-S12-102", "S12", "机器人异常", _ROBOT_RECOVERY),
        _plc_alarm("D524.2", "S12报警[0].NO[2]", "MIX-S12-103", "S12", "机器人夹爪产品掉落", "立即停止设备，检查掉落物、夹爪和周边干涉；清理并确认安全后重新初始化。"),
        _plc_alarm("D524.3", "S12报警[0].NO[3]", "MIX-S12-104", "S12", "机器人运行时移栽伺服 Busy", _ROBOT_RECOVERY),
    )
}


def plc_alarm_for_address(address: str) -> PlcAlarmDefinition | None:
    """返回软元件位地址对应的 UniLab 报警定义。"""
    return PLC_ALARM_MAP.get(address.strip().upper())


def _word_bit(value: Any, bit: int) -> bool:
    if isinstance(value, (list, tuple)):
        return bit < len(value) and bool(value[bit])
    if isinstance(value, dict) and "value" in value:
        return _word_bit(value["value"], bit)
    return bool(int(value) & (1 << bit))


def _alarm_parent_names(variable_name: str, register: str) -> tuple[str, ...]:
    """返回报警位的父字候选，例如 S09报警[0].NO[2] -> S09报警[0]。"""
    parent = variable_name.split(".NO[", 1)[0]
    root = parent.split("[", 1)[0]
    return tuple(dict.fromkeys((parent, root, register)))


def _alarm_fallback_bit(
    value: Any,
    *,
    fallback_name: str,
    variable_name: str,
    bit: int,
) -> bool:
    parent = variable_name.split(".NO[", 1)[0]
    root = parent.split("[", 1)[0]
    if fallback_name == root and root != parent and isinstance(value, (list, tuple)):
        word_index = int(parent.rsplit("[", 1)[1].rstrip("]"))
        if word_index >= len(value):
            return False
        return _word_bit(value[word_index], bit)
    return _word_bit(value, bit)


def read_active_plc_alarms(
    reader: Any,
    *,
    stations: set[str] | None = None,
) -> list[PlcAlarmDefinition]:
    """读取已映射报警位；单个位不可读时回退读取其 BOOL 数组或 D 字。"""
    read_variable = getattr(reader, "read_variable", None) or getattr(reader, "read", None)
    if not callable(read_variable):
        return []
    allowed = set(stations or ())
    allowed.add("公共")
    cache: dict[str, Any] = {}
    active: list[PlcAlarmDefinition] = []
    for alarm in PLC_ALARM_MAP.values():
        if stations is not None and alarm.station not in allowed:
            continue
        register, bit_text = alarm.address.split(".", 1)
        bit = int(bit_text)
        is_active: bool | None = None
        try:
            is_active = bool(read_variable(alarm.variable_name, use_cache=False))
        except TypeError:
            try:
                is_active = bool(read_variable(alarm.variable_name))
            except Exception:
                is_active = None
        except Exception:
            is_active = None
        # 某些 PLC OPC UA 服务会暴露位节点但位值不刷新，而父报警字正常刷新。
        # 因此位节点为 False 或不可读时都必须继续核对父字，不能只在异常时回退。
        if not is_active:
            for fallback_name in _alarm_parent_names(alarm.variable_name, register):
                if fallback_name not in cache:
                    try:
                        cache[fallback_name] = read_variable(fallback_name, use_cache=False)
                    except TypeError:
                        try:
                            cache[fallback_name] = read_variable(fallback_name)
                        except Exception:
                            cache[fallback_name] = None
                    except Exception:
                        cache[fallback_name] = None
                value = cache[fallback_name]
                if value is not None:
                    try:
                        if _alarm_fallback_bit(
                            value,
                            fallback_name=fallback_name,
                            variable_name=alarm.variable_name,
                            bit=bit,
                        ):
                            is_active = True
                            break
                    except (TypeError, ValueError):
                        continue
        if is_active:
            active.append(alarm)
    return active


def enrich_with_plc_alarm(
    failure: Any,
    *,
    alarm: PlcAlarmDefinition,
) -> dict[str, Any]:
    """用确定的 PLC 位报警覆盖推断类错误码，同时保留原始失败。"""
    result = dict(failure) if isinstance(failure, dict) else {"failure": failure}
    previous_code = str(result.get("error_code") or result.get("code") or "")
    if previous_code and previous_code != alarm.code:
        result["original_code"] = previous_code
    original_message = str(result.get("message") or result.get("error") or "设备动作失败")
    result.update({
        "success": False,
        "code": alarm.code,
        "error_code": alarm.code,
        "station": alarm.station,
        "plc_address": alarm.address,
        "plc_variable": alarm.variable_name,
        "error_title": alarm.title,
        "recovery": alarm.recovery,
        "message": f"[{alarm.code}] {alarm.title}（PLC {alarm.address}=ON）；原始失败：{original_message}；处理建议：{alarm.recovery}",
    })
    return result


_DEVICE_STATIONS = {
    "szlab_mixer_stirrer": "S04",
    "szlab_mixer_photoshotting": "S05",
    "szlab_mixer_pump": "S06",
    "szlab_s07_solid_addition": "S07",
    "szlab_s08_cap_station": "S08",
    "szlab_mixer_pipetting_station": "S09",
    "szlab_mixer_robot": "S12",
}


def _station_for(device_id: str, message: str) -> str | None:
    upper = f"{device_id} {message}".upper()
    for station in ("S04", "S05", "S06", "S07", "S08", "S09", "S12"):
        if station in upper:
            return station
    lowered = device_id.lower()
    for fragment, station in _DEVICE_STATIONS.items():
        if fragment in lowered:
            return station
    return None


def classify_mixing_error(device_id: str, message: str) -> MixingErrorDefinition | None:
    """根据工站和已确认的运行时失败文本选择最具体的故障码。"""
    station = _station_for(device_id, message)
    if station is None:
        return None
    text = message.lower()
    if station == "S05" and any(word in text for word in ("旋转", "角度", "到位")):
        return MIXING_ERROR_CATALOG["MIX-S05-002"]
    if station == "S07":
        if any(word in text for word in ("持续", "空罐", "余量", "重量无变化")):
            return MIXING_ERROR_CATALOG["MIX-S07-003"]
        if any(word in text for word in ("天平", "清零", "不下粉", "读数")):
            return MIXING_ERROR_CATALOG["MIX-S07-002"]
    if station == "S09":
        if any(word in text for word in ("探液", "吸液", "排液", "触底")):
            return MIXING_ERROR_CATALOG["MIX-S09-003"]
        if "tip" in text:
            return MIXING_ERROR_CATALOG["MIX-S09-002"]
    if station == "S12":
        if any(word in text for word in ("允许写入", "写入完成", "任务完成", "握手", "等待", "wait")):
            return MIXING_ERROR_CATALOG["MIX-S12-002"]
        return MIXING_ERROR_CATALOG["MIX-S12-001"]
    return MIXING_ERROR_CATALOG.get(f"MIX-{station}-001")


def enrich_mixing_failure(failure: Any, *, device_id: str = "") -> dict[str, Any] | None:
    """把 action 的失败返回转换为 UniLab 可直接上报的结构化报警。"""
    if isinstance(failure, dict):
        result = dict(failure)
        message = str(result.get("message") or result.get("error") or "设备动作失败")
        existing_code = str(result.get("error_code") or result.get("code") or "")
        definition = MIXING_ERROR_CATALOG.get(existing_code)
        if definition is None:
            definition = classify_mixing_error(device_id, message)
        if definition is None:
            return None
        result["success"] = False
        original_code = str(result.get("code") or "")
        if original_code and original_code != definition.code:
            result["original_code"] = original_code
        result["code"] = definition.code
        result["error_code"] = definition.code
        result["station"] = definition.station
        result["error_title"] = definition.title
        result["recovery"] = definition.recovery
        if not message.startswith(f"[{definition.code}]"):
            result["message"] = f"[{definition.code}] {message}；处理建议：{definition.recovery}"
        return result
    message = "动作返回 False" if failure is False else str(failure)
    definition = classify_mixing_error(device_id, message)
    if definition is None:
        return None
    return {
        "success": False,
        "code": definition.code,
        "error_code": definition.code,
        "station": definition.station,
        "error_title": definition.title,
        "message": f"[{definition.code}] {message}；处理建议：{definition.recovery}",
        "recovery": definition.recovery,
    }
