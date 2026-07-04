"""
S08 开盖/关盖工位子设备。

通过自带 OPC UA 客户端直连 PLC 变量，实现 S08 开关盖工站统一握手（新版 PLC 协议）。

对外仅暴露一个工艺 Action ``process_cap``，由入参 ``operation``（open/close）与
``vial_type``（sample_500ml / sample_250ml / liquid_100ml）选择「S08工艺选择 / S08工艺完成」1–6。

瓶盖暂存映射由 UniLab 写入/读取 OPC UA「S082_{1..5}数据缓存」INT[30]（样品 ID），
配合「S082瓶盖暂存位」下发工艺。开盖须传入机械臂扫描的 sample_id，分配第一个空闲暂存位并
写入 ID–Slot 绑定；关盖须传入相同样品 ID，按缓存反查暂存位，关盖成功后清除该 Slot 的 ID 记录。

开/关盖前读取开盖工位传感器（工位1=NO[14]：500/250ml 样品瓶；工位2=NO[15]：100ml 液体瓶），
若对应工位无瓶（传感器为 False）则直接返回错误。开盖分配暂存位时同时要求瓶盖暂存位传感器
（NO[0-4]）为 False；关盖前要求目标暂存位传感器为 True（有盖可取）。

``S08取放料产品`` / ``S08取放料编号`` 由 workflow 写入，本驱动不读写。

总原则：谁写入谁复位。初始化及启动前仅复位 UniLab 负责写入的变量；对端写入、UniLab 只读的变量
（S08工艺完成、S08允许加工、S08原点信号、传感器等）不在启动前等待或干预其取值。

握手时序（实机，一轮工艺）：
1. UniLab 写入工艺参数并置位「S08参数写入完成」→ 对端开始动作；
2. UniLab 读取「S08工艺完成」，直到等于工艺号；
3. UniLab 复位本侧握手参数（写入，不等）；
4. UniLab 读取「S08工艺完成」为 0，表示对端已响应本侧复位、本轮结束。

UniLab 写入、对端读取：S08工艺选择、S08参数写入完成、S082瓶盖暂存位、数据缓存等。
对端写入、UniLab 读取：S08工艺完成、S08允许加工、S08原点信号、传感器等。
"""

from __future__ import annotations

import importlib.util
import os
import time
from enum import IntEnum
from pathlib import Path
from typing import Any, Optional, Sequence

from unilabos.registry.decorators import action, device, not_action, topic_config
from unilabos.utils.log import logger


def _load_opcua_client_class():
    try:
        from .decap_s08_opcua_client import SzlabS08OpcUaClient

        return SzlabS08OpcUaClient
    except ImportError:
        module_path = Path(__file__).resolve().parent / "decap_s08_opcua_client.py"
        spec = importlib.util.spec_from_file_location("szlab_decap_s08_opcua_client", module_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载 S08 OPC UA 客户端: {module_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.SzlabS08OpcUaClient


SzlabS08OpcUaClient = _load_opcua_client_class()

DEFAULT_OPCUA_URL = os.environ.get(
    "UNILABOS_SZLAB_S08_OPCUA_URL",
    "opc.tcp://127.0.0.1:50102/",
)


NODE_HOME = "S08原点信号"
NODE_ALLOW_PROCESS = "S08允许加工"
NODE_PROCESS_SELECT = "S08工艺选择"
NODE_PARAMS_WRITTEN = "S08参数写入完成"
NODE_PROCESS_COMPLETE = "S08工艺完成"
NODE_CAP_STORAGE_SLOT = "S082瓶盖暂存位"
NODE_STATION_STATUS = "工站状态[7]"

# 工站状态[7]：0报警 1未准备好 2准备好 3运行中 4单循环 5寸动 6初始化
S08_STATION_STATUS_LABELS: dict[int, str] = {
    0: "报警中",
    1: "未准备好",
    2: "准备好",
    3: "运行中",
    4: "单循环中",
    5: "寸动中",
    6: "初始化中",
}
S08_STATION_STATUS_READY_VALUES = frozenset({2, 3, 4, 5, 6})

CAP_CACHE_LENGTH = 30
CAP_STORAGE_SLOTS = tuple(range(1, 6))

SENSOR_CAP_STATION = {
    1: "传感器状态_上位机[3].NO[14]",
    2: "传感器状态_上位机[3].NO[15]",
}

CAP_STORAGE_SLOT_SENSORS = {
    1: "传感器状态_上位机[4].NO[0]",
    2: "传感器状态_上位机[4].NO[1]",
    3: "传感器状态_上位机[4].NO[2]",
    4: "传感器状态_上位机[4].NO[3]",
    5: "传感器状态_上位机[4].NO[4]",
}


class S08ProcessType(IntEnum):
    OPEN_SAMPLE_VIAL_500ML = 1
    CLOSE_SAMPLE_VIAL_500ML = 2
    OPEN_SAMPLE_VIAL_250ML = 3
    CLOSE_SAMPLE_VIAL_250ML = 4
    OPEN_LIQUID_VIAL_100ML = 5
    CLOSE_LIQUID_VIAL_100ML = 6


OPEN_PROCESS_IDS = frozenset(
    {
        S08ProcessType.OPEN_SAMPLE_VIAL_500ML,
        S08ProcessType.OPEN_SAMPLE_VIAL_250ML,
        S08ProcessType.OPEN_LIQUID_VIAL_100ML,
    }
)

VIAL_TYPE_ALIASES = {
    "sample_500ml": "sample_500ml",
    "500ml": "sample_500ml",
    "sample_250ml": "sample_250ml",
    "250ml": "sample_250ml",
    "liquid_100ml": "liquid_100ml",
    "100ml": "liquid_100ml",
}

VIAL_PROCESS_TYPES: dict[str, tuple[S08ProcessType, S08ProcessType]] = {
    "sample_500ml": (S08ProcessType.OPEN_SAMPLE_VIAL_500ML, S08ProcessType.CLOSE_SAMPLE_VIAL_500ML),
    "sample_250ml": (S08ProcessType.OPEN_SAMPLE_VIAL_250ML, S08ProcessType.CLOSE_SAMPLE_VIAL_250ML),
    "liquid_100ml": (S08ProcessType.OPEN_LIQUID_VIAL_100ML, S08ProcessType.CLOSE_LIQUID_VIAL_100ML),
}

# 示意图 S08开关盖：工位1=500/250ml 样品瓶，工位2=100ml 液体瓶
VIAL_TYPE_TO_CAP_STATION: dict[str, int] = {
    "sample_500ml": 1,
    "sample_250ml": 1,
    "liquid_100ml": 2,
}

VIAL_TYPE_LABELS: dict[str, str] = {
    "sample_500ml": "样品瓶500ml",
    "sample_250ml": "样品瓶250ml",
    "liquid_100ml": "液体瓶100ml",
}


def _cap_cache_element_name(slot: int, index: int) -> str:
    _validate_cap_storage_slot(slot)
    if index not in range(CAP_CACHE_LENGTH):
        raise ValueError(f"缓存下标必须在 0-{CAP_CACHE_LENGTH - 1} 范围内，收到: {index}")
    return f"S082_{slot}数据缓存[{index}]"


def _normalize_sample_id(sample_id: Sequence[int] | None) -> list[int]:
    if sample_id is None:
        return [0] * CAP_CACHE_LENGTH
    values = [int(v) for v in sample_id]
    if not values:
        raise ValueError("sample_id 不能为空")
    if len(values) > CAP_CACHE_LENGTH:
        raise ValueError(f"sample_id 长度不能超过 {CAP_CACHE_LENGTH}")
    return values + [0] * (CAP_CACHE_LENGTH - len(values))


def _sample_id_is_empty(sample_id: Sequence[int]) -> bool:
    return all(int(v) == 0 for v in sample_id)


def _sample_ids_match(left: Sequence[int], right: Sequence[int]) -> bool:
    left_norm = _normalize_sample_id(list(left))
    right_norm = _normalize_sample_id(list(right))
    return left_norm == right_norm and not _sample_id_is_empty(left_norm)


def _validate_cap_storage_slot(cap_storage_slot: int) -> None:
    if cap_storage_slot not in range(1, 6):
        raise ValueError(f"cap_storage_slot 必须在 1-5 范围内，收到: {cap_storage_slot}")


def _normalize_operation(operation: str) -> str:
    normalized = operation.strip().lower()
    if normalized not in {"open", "close"}:
        raise ValueError("operation 必须是 open 或 close")
    return normalized


def _normalize_vial_type(vial_type: str) -> str:
    normalized = VIAL_TYPE_ALIASES.get(vial_type.strip().lower())
    if normalized is None:
        supported = ", ".join(sorted(VIAL_PROCESS_TYPES))
        raise ValueError(f"vial_type 无效，支持: {supported}")
    return normalized


def _resolve_process_type(operation: str, vial_type: str) -> S08ProcessType:
    normalized_operation = _normalize_operation(operation)
    normalized_vial_type = _normalize_vial_type(vial_type)
    open_type, close_type = VIAL_PROCESS_TYPES[normalized_vial_type]
    return open_type if normalized_operation == "open" else close_type


DEFAULT_UPLINK_COMM_PREFIX = "ns=4;s=上位机通讯"


def _is_virtual_test_opcua_url(url: str) -> bool:
    lowered = url.lower()
    return "127.0.0.1" in lowered or "localhost" in lowered or ":50102" in lowered


def _coerce_opcua_int(value: Any, *, field_name: str) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} 无法解析为整数，OPC UA 返回值={value!r}") from exc


def _format_driver_error(exc: BaseException) -> str:
    message = str(exc).strip()
    if message:
        return message
    return f"{type(exc).__name__}（无详细消息）"


def build_opcua_node_id_map_for_uplink_comm(prefix: str = DEFAULT_UPLINK_COMM_PREFIX) -> dict[str, str]:
    """为真机 OPC UA「上位机通讯」命名空间生成 S08 变量 NodeId 映射。"""
    prefix = prefix.rstrip("|")
    variable_names = [
        NODE_HOME,
        NODE_ALLOW_PROCESS,
        NODE_PROCESS_SELECT,
        NODE_PARAMS_WRITTEN,
        NODE_PROCESS_COMPLETE,
        NODE_CAP_STORAGE_SLOT,
        NODE_STATION_STATUS,
        *SENSOR_CAP_STATION.values(),
        *CAP_STORAGE_SLOT_SENSORS.values(),
    ]
    for slot in CAP_STORAGE_SLOTS:
        for index in range(CAP_CACHE_LENGTH):
            variable_names.append(_cap_cache_element_name(slot, index))
    return {name: f"{prefix}|{name}" for name in variable_names}


@device(
    id="szlab_s08_cap_station",
    display_name="S08 开盖工位",
    category=["workstation", "szlab"],
    description="苏州实验室 S08 开盖/关盖工位（直连 OPC UA）",
)
class SZLabS08CapStationDevice:
    def __init__(
        self,
        url: str = DEFAULT_OPCUA_URL,
        username: str | None = None,
        password: str | None = None,
        timeout: float = 300.0,
        poll_interval: float = 0.2,
        require_station_ready: bool = True,
        require_station_status: bool = False,
        validate_cap_constraints: bool = False,
        opcua_client: SzlabS08OpcUaClient | None = None,
        opcua_browse_depth: int = 8,
        opcua_browse_limit: int = 5000,
        opcua_node_id_map: dict[str, str] | None = None,
        opcua_uplink_comm_prefix: str | None = None,
        opcua_allow_recursive_browse: bool = False,
        opcua_object_name: str = "VirtualS08",
        **kwargs,
    ):
        del kwargs
        self.url = url
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.require_station_ready = require_station_ready
        # 工站状态字 / 瓶盖业务约束：默认关闭；暂不从 device graph 或 workflow UI 透传。
        self.require_station_status = False
        self.validate_cap_constraints = False
        resolved_node_id_map = opcua_node_id_map
        if resolved_node_id_map is None:
            uplink_prefix = opcua_uplink_comm_prefix
            if uplink_prefix is None and not _is_virtual_test_opcua_url(url):
                uplink_prefix = DEFAULT_UPLINK_COMM_PREFIX
                logger.info(
                    f"检测到非虚拟 OPC UA 地址，自动使用上位机通讯 NodeId 映射: {DEFAULT_UPLINK_COMM_PREFIX}"
                )
            if uplink_prefix:
                resolved_node_id_map = build_opcua_node_id_map_for_uplink_comm(uplink_prefix)
        self._client = opcua_client or SzlabS08OpcUaClient(
            url=url,
            username=username,
            password=password,
            browse_depth=opcua_browse_depth,
            browse_limit=opcua_browse_limit,
            node_id_map=resolved_node_id_map,
            allow_recursive_browse=opcua_allow_recursive_browse,
            object_name=opcua_object_name,
        )
        self._last_status: dict[str, Any] = {}
        self._init_unilab_written_state()

    @not_action
    def get_opc_variable_metadata(self, variable_name: str) -> tuple[str, str | None]:
        return self._client.get_opc_variable_metadata(variable_name)

    @not_action
    def _format_opc_variable_ref(self, node_name: str) -> str:
        """返回带 OPC UA 变量名与 NodeId 的引用，便于实机排障。"""
        _, node_id = self.get_opc_variable_metadata(node_name)
        if node_id:
            return f"{node_name}（OPC UA NodeId={node_id}）"
        return f"{node_name}（OPC UA 变量名）"

    @not_action
    def _read_variable(self, node_name: str) -> Any:
        return self._client.read(node_name)

    @not_action
    def _write_variable(self, node_name: str, value: Any) -> None:
        self._client.write(node_name, value)

    @not_action
    def _wait_plc_bool(
        self,
        node_name: str,
        expected: bool,
        timeout: Optional[float] = None,
        description: Optional[str] = None,
    ) -> bool:
        timeout = self.timeout if timeout is None else timeout
        desc = description or node_name
        logger.info(f"等待 {desc} == {expected}")
        start = time.time()
        while time.time() - start < timeout:
            if bool(self._read_variable(node_name)) is expected:
                logger.info(f"✓ {desc} 已变为 {expected}")
                return True
            time.sleep(self.poll_interval)
        logger.error(f"✗ 等待 {desc} 超时 ({timeout}s)")
        return False

    @not_action
    def _read_process_complete_int(self) -> int:
        return _coerce_opcua_int(
            self._read_variable(NODE_PROCESS_COMPLETE),
            field_name=self._format_opc_variable_ref(NODE_PROCESS_COMPLETE),
        )

    @not_action
    def _read_handshake_int(self, node_name: str) -> int:
        return _coerce_opcua_int(self._read_variable(node_name), field_name=node_name)

    @not_action
    def _unilab_handshake_is_dirty(self) -> bool:
        return (
            self._read_handshake_int(NODE_PROCESS_SELECT) != 0
            or bool(self._read_variable(NODE_PARAMS_WRITTEN))
            or int(self._read_variable(NODE_CAP_STORAGE_SLOT) or 0) != 0
        )

    @not_action
    def _wait_process_complete(
        self,
        expected: int,
        timeout: Optional[float] = None,
        description: Optional[str] = None,
    ) -> bool:
        """轮询读取对端「S08工艺完成」，直到等于期望值。"""
        timeout = self.timeout if timeout is None else timeout
        desc = description or f"{NODE_PROCESS_COMPLETE} == {expected}"
        var_ref = self._format_opc_variable_ref(NODE_PROCESS_COMPLETE)
        logger.info(f"等待 {desc}")
        interval = self.poll_interval

        if hasattr(self._client, "wait_equal"):
            ok = self._client.wait_equal(
                NODE_PROCESS_COMPLETE,
                expected,
                timeout=timeout,
                interval=interval,
            )
            if ok:
                logger.info(f"✓ {desc}")
            else:
                try:
                    self._last_process_complete_seen = self._read_process_complete_int()
                except Exception:
                    self._last_process_complete_seen = None
                logger.error(
                    f"✗ 等待 {desc} 超时 ({timeout}s)，"
                    f"最后读取 {var_ref}={self._last_process_complete_seen!r}"
                )
            return ok

        start = time.time()
        last_seen: int | None = None
        while time.time() - start < timeout:
            try:
                last_seen = self._read_process_complete_int()
            except Exception as exc:
                logger.warning(f"读取 {var_ref} 失败，继续等待: {_format_driver_error(exc)}")
                time.sleep(interval)
                continue
            if last_seen == expected:
                logger.info(f"✓ {desc}")
                return True
            time.sleep(interval)

        logger.error(
            f"✗ 等待 {desc} 超时 ({timeout}s)，最后读取 {var_ref}={last_seen!r}"
        )
        self._last_process_complete_seen = last_seen
        return False

    @not_action
    def _process_complete_timeout_message(self, expected: int, task_label: str) -> str:
        var_ref = self._format_opc_variable_ref(NODE_PROCESS_COMPLETE)
        last_seen = getattr(self, "_last_process_complete_seen", None)
        suffix = f"，当前 {var_ref}={last_seen!r}" if last_seen is not None else ""
        return (
            f"S08 {task_label} 等待工艺完成超时（期望 {var_ref}={expected}{suffix}）"
        )

    @not_action
    def _complete_handshake_teardown(self, timeout: Optional[float] = None) -> bool:
        """步骤 3–4：复位本侧握手，再读取对端是否已将 S08工艺完成 置 0。"""
        timeout = self.timeout if timeout is None else timeout
        try:
            self._reset_unilab_written_params()
        except Exception as exc:
            logger.warning(f"复位握手参数失败，尝试 OPC 重连后重试: {_format_driver_error(exc)}")
            if hasattr(self._client, "reconnect"):
                self._client.reconnect()
            self._reset_unilab_written_params()

        if _is_virtual_test_opcua_url(self.url):
            return True

        var_ref = self._format_opc_variable_ref(NODE_PROCESS_COMPLETE)
        return self._wait_process_complete(
            0,
            timeout=timeout,
            description=f"{var_ref} == 0",
        )

    @not_action
    def _reset_unilab_written_params_if_dirty(self) -> None:
        if self._unilab_handshake_is_dirty():
            self._reset_unilab_written_params()

    @not_action
    def _reset_unilab_written_params(self) -> None:
        self._write_variable(NODE_PROCESS_SELECT, 0)
        self._write_variable(NODE_PARAMS_WRITTEN, False)
        self._write_variable(NODE_CAP_STORAGE_SLOT, 0)

    @not_action
    def _read_s08_status(self) -> dict[str, Any]:
        status = {
            "station_ready": bool(self._read_variable(NODE_HOME)),
            "allow_process": bool(self._read_variable(NODE_ALLOW_PROCESS)),
            "process_select": int(self._read_variable(NODE_PROCESS_SELECT) or 0),
            "params_written": bool(self._read_variable(NODE_PARAMS_WRITTEN)),
            "process_complete": self._read_process_complete_int(),
            "cap_storage_slot": int(self._read_variable(NODE_CAP_STORAGE_SLOT) or 0),
        }
        self._last_status = status
        return status

    @not_action
    def _init_unilab_written_state(self) -> None:
        """连接后复位本侧握手参数，不写入对端负责的变量（工艺完成、允许加工、原点信号等）。"""
        try:
            self._reset_unilab_written_params()
            logger.info("S08 本侧握手参数已在连接时复位")
        except Exception as exc:
            logger.warning(f"S08 连接时复位本侧参数失败: {exc}")

    @not_action
    def _read_sample_id_from_plc(self, slot: int) -> list[int]:
        return [
            int(self._read_variable(_cap_cache_element_name(slot, index)) or 0)
            for index in range(CAP_CACHE_LENGTH)
        ]

    @not_action
    def _write_sample_id_to_slot_cache(self, slot: int, sample_id: Sequence[int]) -> None:
        normalized = _normalize_sample_id(sample_id)
        for index, value in enumerate(normalized):
            self._write_variable(_cap_cache_element_name(slot, index), value)

    @not_action
    def _clear_slot_cache(self, slot: int) -> None:
        self._write_sample_id_to_slot_cache(slot, [0] * CAP_CACHE_LENGTH)

    @not_action
    def _try_read_sample_id_from_plc(self, slot: int) -> Optional[list[int]]:
        try:
            return self._read_sample_id_from_plc(slot=slot)
        except Exception as exc:
            logger.warning(f"读取 S082_{slot} 数据缓存失败: {exc}")
            return None

    @not_action
    def _read_cap_slot_sensor(self, slot: int) -> bool:
        _validate_cap_storage_slot(slot)
        return bool(self._read_variable(CAP_STORAGE_SLOT_SENSORS[slot]))

    @not_action
    def _find_free_cap_slot(self) -> Optional[int]:
        for slot in CAP_STORAGE_SLOTS:
            cached = self._try_read_sample_id_from_plc(slot)
            cache_empty = cached is not None and _sample_id_is_empty(cached)
            if not cache_empty:
                continue
            if self.validate_cap_constraints and self._read_cap_slot_sensor(slot):
                continue
            return slot
        return None

    @not_action
    def _find_free_cap_slot_relaxed(self) -> int:
        free_slot = self._find_free_cap_slot()
        if free_slot is not None:
            return free_slot
        return CAP_STORAGE_SLOTS[0]

    @not_action
    def _find_cap_slot_by_sample_id(self, sample_id: Sequence[int]) -> Optional[int]:
        normalized = _normalize_sample_id(sample_id)
        if _sample_id_is_empty(normalized):
            raise ValueError("sample_id 不能全为 0")
        for slot in CAP_STORAGE_SLOTS:
            cached = self._try_read_sample_id_from_plc(slot)
            if cached is not None and _sample_ids_match(cached, normalized):
                return slot
        return None

    @not_action
    def _validate_cap_station_has_bottle(self, vial_type: str, operation: str) -> None:
        normalized_vial_type = _normalize_vial_type(vial_type)
        station_id = VIAL_TYPE_TO_CAP_STATION[normalized_vial_type]
        sensor_node = SENSOR_CAP_STATION[station_id]
        if not bool(self._read_variable(sensor_node)):
            op_label = "开盖" if _normalize_operation(operation) == "open" else "关盖"
            vial_label = VIAL_TYPE_LABELS[normalized_vial_type]
            raise ValueError(
                f"{op_label}前检测到开盖工位{station_id}无{vial_label}（{sensor_node}=False）"
            )

    @not_action
    def _validate_cap_storage_slot_empty(self, slot: int) -> None:
        if self._read_cap_slot_sensor(slot):
            sensor_node = CAP_STORAGE_SLOT_SENSORS[slot]
            raise ValueError(
                f"开盖前检测到瓶盖暂存位{slot}已有瓶盖（{sensor_node}=True），"
                "但该位缓存为空，请确认是否需执行复位或清理暂存位"
            )

    @not_action
    def _validate_cap_storage_slot_has_cap(self, slot: int) -> None:
        if not self._read_cap_slot_sensor(slot):
            sensor_node = CAP_STORAGE_SLOT_SENSORS[slot]
            raise ValueError(
                f"样品 ID 对应暂存位{slot}，但传感器显示该位无瓶盖（{sensor_node}=False），"
                "无法关盖；请确认瓶盖是否在暂存位，或该瓶是否尚未开盖"
            )

    @not_action
    def _resolve_open_cap_storage_slot(self, sample_id: Sequence[int]) -> int:
        if not self.validate_cap_constraints:
            existing_slot = self._find_cap_slot_by_sample_id(sample_id)
            if existing_slot is not None:
                return existing_slot
            return self._find_free_cap_slot_relaxed()

        existing_slot = self._find_cap_slot_by_sample_id(sample_id)
        if existing_slot is not None:
            if self._read_cap_slot_sensor(existing_slot):
                raise ValueError(
                    f"该样品已开盖，瓶盖位于暂存位{existing_slot}，不能对同一瓶重复开盖"
                )
            raise ValueError(
                f"样品 ID 已登记在暂存位{existing_slot}，但传感器显示该位无瓶盖；"
                "缓存与现场状态不一致，请确认 PLC 已完成复位且暂存位实际为空后再操作"
            )

        free_slot = self._find_free_cap_slot()
        if free_slot is None:
            raise ValueError("无可用瓶盖暂存位（1-5 均已绑定样品 ID 或传感器显示已有瓶盖）")
        self._validate_cap_storage_slot_empty(free_slot)
        return free_slot

    @not_action
    def _resolve_close_cap_storage_slot(self, sample_id: Sequence[int]) -> int:
        slot = self._find_cap_slot_by_sample_id(sample_id)
        if slot is None:
            if not self.validate_cap_constraints:
                plc_slot = int(self._read_variable(NODE_CAP_STORAGE_SLOT) or 0)
                if plc_slot in CAP_STORAGE_SLOTS:
                    return plc_slot
                return CAP_STORAGE_SLOTS[0]
            raise ValueError("未找到该样品 ID 对应的瓶盖暂存位，或该瓶尚未开盖，无法关盖")
        if self.validate_cap_constraints:
            self._validate_cap_storage_slot_has_cap(slot)
        return slot

    @not_action
    def _read_s08_station_status_code(self) -> int:
        return int(self._read_variable(NODE_STATION_STATUS) or 0)

    @not_action
    def _validate_s08_station_status_ready(self) -> None:
        status_code = self._read_s08_station_status_code()
        if status_code not in S08_STATION_STATUS_READY_VALUES:
            label = S08_STATION_STATUS_LABELS.get(status_code, f"未知状态{status_code}")
            var_ref = self._format_opc_variable_ref(NODE_STATION_STATUS)
            raise ValueError(
                f"S08 工站未就绪：PLC 状态字 {var_ref}={status_code}（{label}）。"
                f"允许值为 2–6（准备好/运行中/单循环/寸动/初始化），0=报警、1=未准备好；"
                "请在 PLC/HMI 消除报警并使工站进入就绪后再执行 process_cap。"
            )

    @not_action
    def _require_sample_id(self, sample_id: Sequence[int] | None) -> list[int]:
        if sample_id is None:
            raise ValueError("必须传入机械臂扫描获得的 sample_id（list[int]，最长 30）")
        normalized = _normalize_sample_id(sample_id)
        if _sample_id_is_empty(normalized):
            raise ValueError("sample_id 不能全为 0")
        return normalized

    @not_action
    def _run_cap_process(
        self,
        process_type: S08ProcessType,
        cap_storage_slot: int,
        sample_id: Sequence[int],
        timeout: Optional[float] = None,
        clear_cache_on_complete: bool = False,
    ) -> dict[str, Any]:
        _validate_cap_storage_slot(cap_storage_slot)
        timeout = self.timeout if timeout is None else timeout
        process_id = int(process_type)
        is_open = process_type in OPEN_PROCESS_IDS
        task_label = "开瓶盖" if is_open else "关瓶盖"
        normalized_sample_id = _normalize_sample_id(sample_id)

        logger.info(
            f"S08 {task_label}: process={process_id}, cap_storage_slot={cap_storage_slot}, "
            f"sample_id={normalized_sample_id[:8]}..."
        )

        if self.require_station_ready:
            if not self._wait_plc_bool(NODE_HOME, True, timeout=timeout, description="S08 原点信号（机械臂安全位）"):
                return {
                    "success": False,
                    "message": (
                        f"机械臂未回到 S08 安全位：{self._format_opc_variable_ref(NODE_HOME)} 当前为 False"
                    ),
                }

        if not self._wait_plc_bool(
            NODE_ALLOW_PROCESS,
            True,
            timeout=timeout,
            description="S08 允许加工",
        ):
            return {
                "success": False,
                "message": f"等待 {self._format_opc_variable_ref(NODE_ALLOW_PROCESS)} 置 True 超时",
            }

        self._reset_unilab_written_params_if_dirty()

        params_written = False
        handshake_teardown_done = False
        try:
            if is_open:
                self._write_sample_id_to_slot_cache(cap_storage_slot, normalized_sample_id)
            self._write_variable(NODE_CAP_STORAGE_SLOT, int(cap_storage_slot))
            self._write_variable(NODE_PROCESS_SELECT, process_id)
            self._write_variable(NODE_PARAMS_WRITTEN, True)
            params_written = True

            if not self._wait_process_complete(process_id, timeout=timeout):
                return {
                    "success": False,
                    "message": self._process_complete_timeout_message(process_id, task_label),
                }

            if not self._complete_handshake_teardown(timeout=timeout):
                return {
                    "success": False,
                    "message": (
                        f"握手收尾失败：对端 {self._format_opc_variable_ref(NODE_PROCESS_COMPLETE)} "
                        "在复位本侧参数后未清零"
                    ),
                }
            handshake_teardown_done = True

            if clear_cache_on_complete:
                self._clear_slot_cache(cap_storage_slot)
            status = self._read_s08_status()
            return {
                "success": True,
                "message": f"S08 {task_label} 完成",
                "process_type": process_id,
                "cap_storage_slot": cap_storage_slot,
                "sample_id": normalized_sample_id,
                "status": status,
            }
        except Exception as exc:
            logger.exception(f"S08 {task_label} 失败: {exc}")
            return {"success": False, "message": _format_driver_error(exc)}
        finally:
            if params_written and not handshake_teardown_done:
                try:
                    self._complete_handshake_teardown(timeout=min(10.0, timeout))
                except Exception as exc:
                    logger.warning(f"S08 异常退出时握手收尾失败: {_format_driver_error(exc)}")

    @not_action
    def _open_cap(
        self,
        process_type: S08ProcessType,
        sample_id: Sequence[int],
        timeout: float = 300.0,
    ) -> dict[str, Any]:
        try:
            normalized = self._require_sample_id(sample_id)
            slot = self._resolve_open_cap_storage_slot(normalized)
        except ValueError as exc:
            return {"success": False, "message": str(exc)}
        return self._run_cap_process(
            process_type=process_type,
            cap_storage_slot=slot,
            sample_id=normalized,
            timeout=timeout,
        )

    @not_action
    def _close_cap(
        self,
        process_type: S08ProcessType,
        sample_id: Sequence[int],
        timeout: float = 300.0,
    ) -> dict[str, Any]:
        try:
            normalized = _normalize_sample_id(sample_id)
            slot = self._resolve_close_cap_storage_slot(normalized)
        except ValueError as exc:
            return {"success": False, "message": str(exc)}
        return self._run_cap_process(
            process_type=process_type,
            cap_storage_slot=slot,
            sample_id=normalized,
            timeout=timeout,
            clear_cache_on_complete=True,
        )

    @action(
        auto_prefix=True,
        description="S08 开/关盖工艺；operation=open|close，vial_type=sample_500ml|sample_250ml|liquid_100ml；开盖/关盖均须传入机械臂扫描的 sample_id",
    )
    def process_cap(
        self,
        operation: str,
        vial_type: str,
        sample_id: list[int],
        timeout: float = 300.0,
    ) -> dict[str, Any]:
        try:
            process_type = _resolve_process_type(operation, vial_type)
            normalized_sample_id = self._require_sample_id(sample_id)
            if self.require_station_status:
                self._validate_s08_station_status_ready()
            if self.validate_cap_constraints:
                self._validate_cap_station_has_bottle(vial_type, operation)
        except ValueError as exc:
            return {"success": False, "message": str(exc)}

        if process_type in OPEN_PROCESS_IDS:
            return self._open_cap(
                process_type=process_type,
                sample_id=normalized_sample_id,
                timeout=timeout,
            )
        return self._close_cap(
            process_type=process_type,
            sample_id=normalized_sample_id,
            timeout=timeout,
        )

    @topic_config(period=2.0)
    def last_s08_status(self) -> dict[str, Any]:
        return dict(self._last_status)
