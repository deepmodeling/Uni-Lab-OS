"""根据 workflow 节点的 device/method/params 解析 Action 关联的物料传感器变量。"""

from __future__ import annotations
from unilabos.devices.workstation.szlab_poly_studio.sensor import S08Sensors
from unilabos.devices.workstation.szlab_poly_studio.s04_magnetic_stirring.sensors import (
    s04_material_sensor_var,
)

from typing import Any, Callable

from unilabos.devices.workstation.szlab_poly_studio.s06_pump.sensors import (
    ADDITION_BEAKER_SENSOR,
)
from unilabos.devices.workstation.szlab_poly_studio.s08_decap.decap_s08_cap_station import (
    CAP_STORAGE_SLOT_SENSORS,
    PROCESS_TYPE_TO_VIAL_TYPE,
    SENSOR_CAP_STATION,
    S08ProcessType,
    VIAL_TYPE_TO_CAP_STATION,
)
from unilabos.devices.workstation.szlab_poly_studio.s09_pipetting_station.sensors import (
    S09_STATION_SENSORS,
    S09_TIP_BOX_SENSORS,
    validate_liquid_bottle,
    validate_station,
    validate_tip_box,
)
from unilabos.devices.workstation.szlab_poly_studio.s12_robot.robot_tasks import (
    ROBOT_ACTION_SPECS,
    ROBOT_HOME_VARIABLE,
    ROBOT_TASK_COMPLETE_VARIABLE,
    ROBOT_TASK_NUMBER_VARIABLE,
    ROBOT_WRITE_ALLOWED_VARIABLE,
    ROBOT_WRITE_DONE_VARIABLE,
    S05_MATERIAL_SENSOR,
    S06_MATERIAL_SENSOR,
    powder_container_sensor,
    product_slot_sensor,
    s02_sensor,
    s04_sensor,
    s09_sensor,
    s10_sensor,
)

ROBOT_HANDSHAKE_OPC_VARIABLES: tuple[str, ...] = (
    ROBOT_HOME_VARIABLE,
    ROBOT_WRITE_ALLOWED_VARIABLE,
    ROBOT_WRITE_DONE_VARIABLE,
    ROBOT_TASK_NUMBER_VARIABLE,
    ROBOT_TASK_COMPLETE_VARIABLE,
)
ROBOT_MANUAL_OPC_VARIABLES: tuple[str, ...] = (
    ROBOT_HOME_VARIABLE,
    ROBOT_WRITE_ALLOWED_VARIABLE,
    ROBOT_WRITE_DONE_VARIABLE,
    ROBOT_TASK_COMPLETE_VARIABLE,
)
ROBOT_HANDSHAKE_MANUAL_INITIAL_VALUES: dict[str, bool | int] = {
    ROBOT_HOME_VARIABLE: True,
    ROBOT_WRITE_ALLOWED_VARIABLE: True,
    ROBOT_TASK_COMPLETE_VARIABLE: 0,
}

SENSOR_VARIABLE_PREFIX = "传感器状态_上位机"

_DEVICE_ALIASES: dict[str, str] = {
    "szlab_mixer_robot": "szlab_mixer_robot",
    "szlab_mixer_stirrer": "szlab_mixer_stirrer",
    "szlab_s04_magnetic_stirring": "szlab_mixer_stirrer",
    "szlab_mixer_photoshotting": "szlab_mixer_photoshotting",
    "szlab_s05_photoshotting": "szlab_mixer_photoshotting",
    "szlab_mixer_pump": "szlab_mixer_pump",
    "szlab_s06_pump": "szlab_mixer_pump",
    "szlab_s07_solid_addition": "szlab_s07_solid_addition",
    "szlab_s08_cap_station": "szlab_s08_cap_station",
    "szlab_mixer_pipetting_station": "szlab_mixer_pipetting_station",
}


def _param(params: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in params:
            return params[key]
    return default


def _dedupe(names: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def _safe(resolve: Callable[[], str | list[str]]) -> list[str]:
    try:
        value = resolve()
    except (KeyError, TypeError, ValueError):
        return []
    if isinstance(value, str):
        return [value]
    return list(value)


def _robot_action_name(method: str) -> str | None:
    if method.startswith("submit_"):
        return method[len("submit_"):]
    return None


def _resolve_robot_submit_sensors(method: str, params: dict[str, Any]) -> list[str]:
    action_name = _robot_action_name(method)
    if action_name is None:
        return []

    position = _param(params, "position", default=1)
    product_type = _param(params, "product_type", default=1)

    resolvers: dict[str, Callable[[], str | list[str]]] = {
        "pick_from_s01": lambda: [],
        "place_to_s02": lambda: s02_sensor(int(position)),
        "pick_from_s02": lambda: s02_sensor(int(position)),
        "place_to_s03": lambda: product_slot_sensor(
            int(product_type), position, used=False
        ),
        "pick_from_s03": lambda: product_slot_sensor(
            int(product_type), position, used=False
        ),
        "place_to_s04": lambda: s04_sensor(int(position)),
        "pick_from_s04": lambda: s04_sensor(int(position)),
        "place_to_s05": lambda: S05_MATERIAL_SENSOR,
        "pick_from_s05": lambda: S05_MATERIAL_SENSOR,
        "place_to_s06": lambda: S06_MATERIAL_SENSOR,
        "pick_from_s06": lambda: S06_MATERIAL_SENSOR,
        "place_to_s071": lambda: powder_container_sensor(position),
        "pick_from_s071": lambda: powder_container_sensor(position),
        "place_to_s072": lambda: [],
        "pick_from_s072": lambda: [],
        "place_to_s08": lambda: S08Sensors.CAP_STATION[int(position)],
        "pick_from_s08": lambda: S08Sensors.CAP_STATION[int(position)],
        "pour_from_s08": lambda: S08Sensors.POUR_SAMPLE_VIAL,
        "place_to_s09": lambda: s09_sensor(int(product_type), int(position)),
        "pick_from_s09": lambda: s09_sensor(int(product_type), int(position)),
        "place_to_s10": lambda: s10_sensor(int(position)),
        "pick_from_s10": lambda: s10_sensor(int(position)),
        "place_to_s11": lambda: product_slot_sensor(
            int(product_type), position, used=True
        ),
        "pick_from_s11": lambda: product_slot_sensor(
            int(product_type), position, used=True
        ),
    }
    resolver = resolvers.get(action_name)
    if resolver is None:
        return []
    return _safe(resolver)


def _resolve_stirrer_sensors(params: dict[str, Any]) -> list[str]:
    position = int(_param(params, "position", default=1))
    return _safe(lambda: s04_material_sensor_var(position))


def _resolve_photoshotting_sensors(_: dict[str, Any]) -> list[str]:
    return [S05_MATERIAL_SENSOR]


def _resolve_pump_sensors(_: dict[str, Any]) -> list[str]:
    return [ADDITION_BEAKER_SENSOR]


def _resolve_s08_cap_sensors(params: dict[str, Any]) -> list[str]:
    process_raw = _param(params, "工艺选择", "process_type", "process", default=5)
    cap_slot = int(_param(params, "瓶盖暂存位", "cap_storage_slot", default=1))

    def resolve() -> list[str]:
        process_type = S08ProcessType(int(process_raw))
        vial_type = PROCESS_TYPE_TO_VIAL_TYPE[process_type]
        station_id = VIAL_TYPE_TO_CAP_STATION[vial_type]
        return [
            SENSOR_CAP_STATION[station_id],
            CAP_STORAGE_SLOT_SENSORS[cap_slot],
        ]

    return _safe(resolve)


def _resolve_pipetting_sensors(params: dict[str, Any]) -> list[str]:
    def resolve() -> list[str]:
        take_tip_box = validate_tip_box(
            int(_param(params, "take_tip_box_index", default=1))
        )
        release_tip_box = validate_tip_box(
            int(_param(params, "release_tip_box_index", default=2))
        )
        liquid_bottle = validate_liquid_bottle(
            int(_param(params, "liquid_bottle_index", default=1))
        )
        station = validate_station(int(_param(params, "station", default=1)))
        return [
            S09_TIP_BOX_SENSORS[take_tip_box],
            S09_TIP_BOX_SENSORS[release_tip_box],
            S09_STATION_SENSORS[liquid_bottle],
            S09_STATION_SENSORS[station],
        ]

    return _safe(resolve)


def _resolve_reusable_pipetting_sensors(params: dict[str, Any]) -> list[str]:
    def resolve() -> list[str]:
        liquid_station = validate_liquid_bottle(
            int(_param(params, "liquid_station_index", default=1))
        )
        return [S09_STATION_SENSORS[liquid_station]]

    return _safe(resolve)


_STATION_METHOD_RESOLVERS: dict[tuple[str, str], Callable[[dict[str, Any]], list[str]]] = {
    ("szlab_mixer_stirrer", "run_stirring"): _resolve_stirrer_sensors,
    ("szlab_mixer_photoshotting", "take_photo"): _resolve_photoshotting_sensors,
    ("szlab_mixer_photoshotting", "take_dual_view_photos"): _resolve_photoshotting_sensors,
    ("szlab_mixer_pump", "run_solvent_addition"): _resolve_pump_sensors,
    ("szlab_s08_cap_station", "process_cap"): _resolve_s08_cap_sensors,
    ("szlab_mixer_pipetting_station", "add_liquid_to_beaker"): _resolve_pipetting_sensors,
    ("szlab_mixer_pipetting_station", "add_liquid"): _resolve_pipetting_sensors,
    (
        "szlab_mixer_pipetting_station",
        "add_liquid_with_reusable_tip",
    ): _resolve_reusable_pipetting_sensors,
    ("szlab_mixer_pipetting_station", "measure_density"): _resolve_pipetting_sensors,
}


def infer_sensor_variable_data_type(name: str) -> str:
    if name.startswith(SENSOR_VARIABLE_PREFIX):
        return "bool"
    return "unknown"


def resolve_robot_action_opc_variables(
    device_id: str,
    method: str,
) -> list[str]:
    """返回 s12_robot submit_* 动作关联的 OPC 变量（握手 + 任务参数字段名）。"""
    if not isinstance(device_id, str) or not device_id.strip():
        return []
    if not isinstance(method, str) or not method.strip():
        return []
    if _DEVICE_ALIASES.get(device_id.strip()) != "szlab_mixer_robot":
        return []
    action_name = _robot_action_name(method.strip())
    if action_name is None:
        return []
    variables = list(ROBOT_HANDSHAKE_OPC_VARIABLES)
    spec = ROBOT_ACTION_SPECS.get(action_name)
    if spec is not None:
        variables.extend(spec.variables)
    return _dedupe(variables)


def resolve_action_sensor_variables(
    device_id: str,
    method: str,
    params: dict[str, Any] | None = None,
) -> list[str]:
    """返回指定 Action 会读取的物料传感器 OPC 变量名（去重、保序）。"""
    if not isinstance(device_id, str) or not device_id.strip():
        return []
    if not isinstance(method, str) or not method.strip():
        return []
    normalized_device = _DEVICE_ALIASES.get(device_id.strip())
    if normalized_device is None:
        return []
    method = method.strip()
    payload = dict(params) if isinstance(params, dict) else {}

    if normalized_device == "szlab_mixer_robot":
        return _resolve_robot_submit_sensors(method, payload)

    resolver = _STATION_METHOD_RESOLVERS.get((normalized_device, method))
    if resolver is None:
        return []
    return _dedupe(resolver(payload))
