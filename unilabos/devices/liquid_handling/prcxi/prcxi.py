import asyncio
import collections
from collections import OrderedDict
import contextlib
from enum import Enum
import json
import logging
import os
import socket
import sys
import time
import uuid
import warnings
from typing import Any, List, Dict, Optional, Tuple, TypedDict, Union, Sequence, Iterator, Literal, Callable, Awaitable
from pylabrobot.liquid_handling.standard import GripDirection

from pylabrobot.liquid_handling import (
    LiquidHandlerBackend,
    Pickup,
    SingleChannelAspiration,
    Drop,
    SingleChannelDispense,
    PickupTipRack,
    DropTipRack,
    MultiHeadAspirationPlate,
    ChatterBoxBackend,
    LiquidHandlerChatterboxBackend,
)
from pylabrobot.liquid_handling.standard import (
    MultiHeadAspirationContainer,
    MultiHeadDispenseContainer,
    MultiHeadDispensePlate,
    ResourcePickup,
    ResourceMove,
    ResourceDrop,
)
from pylabrobot.resources import (
    ResourceHolder,
    ResourceStack,
    Tip,
    Deck,
    Plate,
    Well,
    TipRack,
    Resource,
    Container,
    Coordinate,
    TipSpot,
    Trash,
    PlateAdapter,
    TubeRack,
    create_homogeneous_resources,
)

from unilabos.devices.liquid_handling.liquid_handler_abstract import (
    LiquidHandlerAbstract,
    SimpleReturn,
    SetLiquidReturn,
    SetLiquidFromPlateReturn,
    TransferLiquidReturn,
)
from unilabos.devices.liquid_handling.prcxi.flatten_utils import (
    flatten_multi_channel_kwargs as _flatten_multi_channel_kwargs_impl,
    normalize_pip_setting as _normalize_pip_setting,
    select_axis as _select_axis,
    axis_channel_list as _axis_channel_list,
    axis_from_channels as _axis_from_channels_util,
    RIGHT_CHANNEL_BASE as _RIGHT_CHANNEL_BASE,
)
from unilabos.registry.decorators import topic_config
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.resources.itemized_carrier import ItemizedCarrier
from unilabos.resources.resource_tracker import ResourceTreeSet
from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode, ROS2DeviceNode


class PRCXIError(RuntimeError):
    """Lilith 返回 Success=false 时抛出的业务异常"""


# 放液方式（服务端接收 LiquidDispensingMethodEnum 的枚举名字符串）：
# NormalDispense=0（正常放液）、WallContactAfterDispense_Left=3（放液后靠左壁）、
# WallContactAfterDispense_Right=4（放液后靠右壁）。
LIQUID_METHOD_NORMAL = "NormalDispense"
LIQUID_METHOD_WALL_LEFT = "WallContactAfterDispense_Left"
LIQUID_METHOD_WALL_RIGHT = "WallContactAfterDispense_Right"

# touch_tip 实现模式：native=原生放液后靠壁；software=通用软件式(孔内左右壁各 0 体积 aspirate)；
# both=两者同时。仅在单次 transfer 的 touch_tip=True 时触发，且仅 9320 有意义。
TOUCH_TIP_MODES = ("native", "software", "both")
TOUCH_TIP_WALLS = ("follow_axis", "left", "right")


# =============================================================
# V04 协议支持：Board 数据模型 + 老版本→V04 布局/位置映射
#
# 说明：这些原本在独立模块 ``prcxi_v04.py``，因 V04 不再走“XML 方案写盘”，
# XAML 生成/落盘那部分已删除，剩余仍被 ``PRCXI9300Api`` 用到的 Board 模型/映射合并至此。
# 字段大小写严格对齐服务端：坐标类多为 camelCase（``xPosition`` / ``gripperPos`` /
# ``xSpacing``），其余为 PascalCase；易错拼写照抄。
# =============================================================
def to_rpc_value(value: Any) -> Any:
    """把 Python 对象序列化为服务端兼容的 JSON 值（枚举取 .name，对象取 to_rpc_dict）。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Enum):
        return value.name
    if isinstance(value, dict):
        return {str(k): to_rpc_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_rpc_value(v) for v in value]
    if hasattr(value, "to_rpc_dict"):
        return value.to_rpc_dict()
    if hasattr(value, "to_dict"):
        return value.to_dict()
    raise TypeError(f"无法序列化为 RPC 参数：{type(value)!r}")


def _coerce_bool(value: Any, default: bool = False) -> bool:
    """把配置入参稳健转换为布尔值（兼容 'false'/'0' 等字符串）。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        s = value.strip().strip('"').lower()
        if s in {"true", "1", "yes", "y", "on"}:
            return True
        if s in {"false", "0", "no", "n", "off", ""}:
            return False
    return bool(value)


def _as_int(value: Any, default: int = 0) -> int:
    """把 PRCXI step 字段稳健转为 int。"""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    """把 PRCXI step 字段稳健转为 float。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _v04_axis_type(axis: Any) -> str:
    """旧 StepAxis -> v7 V04 AxisType。"""
    text = str(axis or "").strip().lower()
    return "Axis2" if text in {"right", "axis2", "2"} else "Axis1"


def _v04_tips_type(step: Dict[str, Any]) -> str:
    """根据旧步骤信息推断 v7 V04 Tips 枚举名。

    96 整板模式在 V03（legacy）里靠 ``IsWholePlate=True`` 标志表达；V04 没有该标志，改用
    ``Tips=Tips96`` 表达整板。因此 V04 转换时优先看 ``IsWholePlate``：为真直接判 96 头，
    避免整板 step 的 ``HoleNumbers`` 为空被误判成 Tips1。其余按孔号数量回退（8 连排 / 单头）。
    """
    if _coerce_bool(step.get("IsWholePlate")):
        return "Tips96"
    raw = str(step.get("HoleNumbers") or "")
    numbers = [p.strip() for p in raw.split(",") if p.strip()]
    if len(numbers) >= 96:
        return "Tips96"
    if len(numbers) > 1:
        return "Tips8"
    return "Tips1"


def _v04_position_fields(step: Dict[str, Any], idx: int) -> Dict[str, Any]:
    """提取 V04 液体/枪头步骤共用位置字段。"""
    return {
        "DisplayName": f"T{idx + 1}",
        "Position": str(_as_int(step.get("PlateNo"), 1)),
        "Row": str(_as_int(step.get("HoleRow"), 1)),
        "Col": str(_as_int(step.get("HoleCol"), 1)),
        "AxisType": _v04_axis_type(step.get("StepAxis")),
        "Tips": _v04_tips_type(step),
    }


def legacy_steps_to_v04_solution_steps(steps: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把驱动内部旧 StepData 风格步骤转换为 V04 v7 ``AddSolution_V04`` payload。

    PRCXI 驱动内部仍用历史 ``Function``/``PlateNo``/``HoleRow`` 字段积累动作；v7 服务端
    要求传 ``Kind`` 驱动的 V04 步骤模型，因此在真正建方案前集中做一次结构转换。
    """
    converted: List[Dict[str, Any]] = []
    idx = 0
    steps_list = list(steps or [])
    while idx < len(steps_list):
        step = steps_list[idx]
        if not isinstance(step, dict):
            raise PRCXIError(f"V04 v7 方案步骤必须是 dict，实际 {type(step)!r}")

        function = str(step.get("Function") or "").strip()
        base = {
            "Kind": "",
            "Comment": step.get("Comment"),
            "IsEnabled": _coerce_bool(step.get("IsEnabled"), default=True),
        }

        if function == "Load":
            data = {**base, "Kind": "LoadTips", **_v04_position_fields(step, idx)}
        elif function == "UnLoad":
            data = {**base, "Kind": "UnloadTips", **_v04_position_fields(step, idx)}
        elif function == "Imbibing":
            data = {
                **base,
                "Kind": "Aspirate",
                **_v04_position_fields(step, idx),
                "AspirateVolume": str(_as_float(step.get("DosageNum"))),
                "XOffset": 0.0,
                "YOffset": 0.0,
                "ZOffset": 0.0,
            }
        elif function == "Tapping":
            data = {
                **base,
                "Kind": "Dispense",
                **_v04_position_fields(step, idx),
                "DispenseVolume": str(_as_float(step.get("DosageNum"))),
            }
        elif function == "Blending":
            data = {
                **base,
                "Kind": "Mix",
                **_v04_position_fields(step, idx),
                "MixLoopVolume": _as_int(step.get("DosageNum")),
                "MixLoopCounts": _as_int(step.get("BlendingTimes"), 1),
            }
        elif function == "DefectiveLift":
            source = _as_int(step.get("PlateNo"), 1)
            destination = source
            put_down_position = _as_int(step.get("Hierarchy"), 1)
            force = _as_int(step.get("Force"), 1)
            if idx + 1 < len(steps_list) and steps_list[idx + 1].get("Function") == "PutDown":
                next_step = steps_list[idx + 1]
                destination = _as_int(next_step.get("PlateNo"), destination)
                put_down_position = _as_int(next_step.get("Hierarchy"), put_down_position)
                # 合并 PutDown 时若其带 Force 则覆盖（仍默认 1）
                if next_step.get("Force") is not None:
                    force = _as_int(next_step.get("Force"), force)
                idx += 1
            data = {
                **base,
                "Kind": "MvKit",
                "Source": source,
                "Destination": destination,
                "PinchItUpPosition": _as_int(step.get("Hierarchy"), 1),
                "PutDownPosition": put_down_position,
                "Force": force,
            }
        elif function == "PutDown":
            destination = _as_int(step.get("PlateNo"), 1)
            data = {
                **base,
                "Kind": "MvKit",
                "Source": destination,
                "Destination": destination,
                "PinchItUpPosition": _as_int(step.get("Hierarchy"), 1),
                "PutDownPosition": _as_int(step.get("Hierarchy"), 1),
                "Force": _as_int(step.get("Force"), 1),
            }
        elif function == "Shaking":
            data = {
                **base,
                "Kind": "OscSet",
                "Number": _as_int(step.get("AssistFun2"), 1),
                "OscTime": _as_int(step.get("AssistFun1"), 0),
                "OscRate": _as_int(step.get("AssistFun3"), 0),
                "IsWait": _coerce_bool(step.get("AssistFun4"), default=True),
            }
        elif function == "Shaking_Incubation":
            # 孵育+振荡 → v7 TempAndOsc（温度 + 振荡速率）。
            data = {
                **base,
                "Kind": "TempAndOsc",
                "DisplayName": f"T{idx + 1}",
                "Number": _as_int(step.get("AssistFun2"), 1),
                "Temp": _as_float(step.get("AssistFun5"), 37.0),
                "Time": _as_int(step.get("AssistFun1"), 0),
                "OscRate": _as_int(step.get("AssistFun3"), 0),
                "IsWait": _coerce_bool(step.get("AssistFun4"), default=True),
            }
        elif function == "Magnetic":
            # 磁力架 → v7 MagneticStand。
            data = {
                **base,
                "Kind": "MagneticStand",
                "Number": _as_int(step.get("AssistFun2"), 1),
                "Time": _as_int(step.get("AssistFun1"), 0),
                "Height": _as_float(step.get("AssistFun3"), 0.0),
                "IsWait": _coerce_bool(step.get("AssistFun4"), default=True),
            }
        else:
            raise PRCXIError(f"暂不支持转换为 V04 v7 AddSolution_V04 的 PRCXI 步骤: {function!r}")

        converted.append(data)
        idx += 1

    return converted


class PipettingPos:
    """V04 移液位。"""

    def __init__(
        self,
        id: Optional[str] = None,
        create_time: Optional[str] = None,
        update_time: Optional[str] = None,
        board_detail_id: Optional[str] = None,
        axis_enum: Optional[str] = None,
        volume_enum: Optional[str] = None,
        x_position: float = 0.0,
        y_position: float = 0.0,
        bottle_mouth_position: float = 0.0,
        bottle_bottom_position: float = 0.0,
        left_wall_distance: float = 0.0,
        right_wall_distance: float = 0.0,
        z_wall_distance: float = 0.0,
        safe_altitude: float = 0.0,
    ) -> None:
        self.id = id
        self.create_time = create_time
        self.update_time = update_time
        self.board_detail_id = board_detail_id
        self.axis_enum = axis_enum
        self.volume_enum = volume_enum
        self.x_position = x_position
        self.y_position = y_position
        self.bottle_mouth_position = bottle_mouth_position
        self.bottle_bottom_position = bottle_bottom_position
        self.left_wall_distance = left_wall_distance
        self.right_wall_distance = right_wall_distance
        self.z_wall_distance = z_wall_distance
        self.safe_altitude = safe_altitude

    def to_rpc_dict(self) -> Dict[str, Any]:
        return {
            "Id": self.id,
            "CreateTime": self.create_time,
            "UpdateTime": self.update_time,
            "BoardDetailId": self.board_detail_id,
            "AxisEnum": self.axis_enum,
            "VolumeEnum": self.volume_enum,
            "xPosition": self.x_position,
            "yPosition": self.y_position,
            "bottleMouthPosition": self.bottle_mouth_position,
            "bottleBottomPosition": self.bottle_bottom_position,
            "leftWallDistance": self.left_wall_distance,
            "rightWallDistance": self.right_wall_distance,
            "zWallDistance": self.z_wall_distance,
            "SafeAltitude": self.safe_altitude,
        }


class GripperPos:
    """V04 夹爪位。"""

    def __init__(
        self,
        id: Optional[str] = None,
        create_time: Optional[str] = None,
        update_time: Optional[str] = None,
        board_detail_id: Optional[str] = None,
        axis_enum: Optional[str] = None,
        x_position: float = 0.0,
        y_position: float = 0.0,
        z_buffer_position: float = 0.0,
        z_position: float = 0.0,
        z2_position: float = 0.0,
        gripper_position: float = 0.0,
    ) -> None:
        self.id = id
        self.create_time = create_time
        self.update_time = update_time
        self.board_detail_id = board_detail_id
        self.axis_enum = axis_enum
        self.x_position = x_position
        self.y_position = y_position
        self.z_buffer_position = z_buffer_position
        self.z_position = z_position
        self.z2_position = z2_position
        self.gripper_position = gripper_position

    def to_rpc_dict(self) -> Dict[str, Any]:
        return {
            "Id": self.id,
            "CreateTime": self.create_time,
            "UpdateTime": self.update_time,
            "BoardDetailId": self.board_detail_id,
            "AxisEnum": self.axis_enum,
            "xPosition": self.x_position,
            "yPosition": self.y_position,
            "zBufferPosition": self.z_buffer_position,
            "zPosition": self.z_position,
            "z2Position": self.z2_position,
            "gripperPosition": self.gripper_position,
        }


class BoardPosition:
    """V04 板位定位信息。"""

    def __init__(
        self,
        id: Optional[str] = None,
        create_time: Optional[str] = None,
        update_time: Optional[str] = None,
        board_detail_id: Optional[str] = None,
        board_name: Optional[str] = None,
        board_number: int = 0,
        x_spacing: float = 0.0,
        y_spacing: float = 0.0,
    ) -> None:
        self.id = id
        self.create_time = create_time
        self.update_time = update_time
        self.board_detail_id = board_detail_id
        self.board_name = board_name
        self.board_number = board_number
        self.x_spacing = x_spacing
        self.y_spacing = y_spacing

    def to_rpc_dict(self) -> Dict[str, Any]:
        return {
            "Id": self.id,
            "CreateTime": self.create_time,
            "UpdateTime": self.update_time,
            "BoardDetailId": self.board_detail_id,
            "BoardName": self.board_name,
            "BoardNumber": self.board_number,
            "xSpacing": self.x_spacing,
            "ySpacing": self.y_spacing,
        }


class BoardDetail:
    """V04 板位明细（一个槽位 / 一块耗材）。"""

    def __init__(
        self,
        id: Optional[str] = None,
        create_time: Optional[str] = None,
        update_time: Optional[str] = None,
        board_id: Optional[str] = None,
        name: Optional[str] = None,
        number: int = 0,
        row: int = 0,
        column: int = 0,
        row_span: int = 1,
        column_span: int = 1,
        volume: int = 0,
        material_id: Optional[str] = None,
        module: Optional[str] = None,
        position: Optional[BoardPosition] = None,
        pipetting_pos_list: Optional[List[PipettingPos]] = None,
        gripper_pos: Optional[GripperPos] = None,
    ) -> None:
        self.id = id
        self.create_time = create_time
        self.update_time = update_time
        self.board_id = board_id
        self.name = name
        self.number = number
        self.row = row
        self.column = column
        self.row_span = row_span
        self.column_span = column_span
        self.volume = volume
        self.material_id = material_id
        self.module = module
        self.position = position
        self.pipetting_pos_list = pipetting_pos_list or []
        self.gripper_pos = gripper_pos

    def to_rpc_dict(self) -> Dict[str, Any]:
        return {
            "Id": self.id,
            "CreateTime": self.create_time,
            "UpdateTime": self.update_time,
            "BoardId": self.board_id,
            "Name": self.name,
            "Number": self.number,
            "Row": self.row,
            "Column": self.column,
            "RowSpan": self.row_span,
            "ColumnSpan": self.column_span,
            "Volume": self.volume,
            "MaterialId": self.material_id,
            "Module": self.module,
            "Position": self.position.to_rpc_dict() if self.position else None,
            "PipettingPosList": [p.to_rpc_dict() for p in self.pipetting_pos_list],
            "gripperPos": self.gripper_pos.to_rpc_dict() if self.gripper_pos else None,
        }


class Board:
    """V04 工作台布局（IMatrix 的一个 matrix）。"""

    def __init__(
        self,
        id: Optional[str] = None,
        create_time: Optional[str] = None,
        update_time: Optional[str] = None,
        name: Optional[str] = None,
        rows: int = 0,
        columns: int = 0,
        device_type: Optional[str] = None,
        details: Optional[List[BoardDetail]] = None,
    ) -> None:
        self.id = id
        self.create_time = create_time
        self.update_time = update_time
        self.name = name
        self.rows = rows
        self.columns = columns
        self.device_type = device_type
        self.details = details or []

    def to_rpc_dict(self) -> Dict[str, Any]:
        return {
            "Id": self.id,
            "CreateTime": self.create_time,
            "UpdateTime": self.update_time,
            "Name": self.name,
            "Rows": self.rows,
            "Columns": self.columns,
            "DeviceType": self.device_type,
            "Details": [d.to_rpc_dict() for d in self.details],
        }


def _pick_material_id(material: Dict[str, Any], is_v04: bool) -> Optional[str]:
    """按接口版本选择 BoardDetail.MaterialId。

    - v04：物料主键是 ``id_v4``（如 ``238c27e6-...``）；仅当数据缺 id_v4 时才回退 uuid 兜底。
    - v03：只用 ``uuid``（老服务端不认 id_v4，即使物料字典里带了也不能用）。
    """
    material = material or {}
    if is_v04:
        return material.get("id_v4") or material.get("uuid")
    return material.get("uuid")


def worktablets_to_board(
    matrix_info: Dict[str, Any],
    *,
    columns: int = 4,
    rows: Optional[int] = None,
    device_type: str = "SC9320",
    is_v04: bool = True,
) -> Board:
    """把老版本 ``MatrixInfo``（``MatrixId/MatrixName/WorkTablets[...]``）映射为 V04 ``Board``。

    映射规则（⚠ 需真机联调核对，见《修改计划》决策点 B）：
    - ``MatrixId → Board.Id``，``MatrixName → Board.Name``。
    - 每个 ``WorkTablet(Number/Code/Material) → 一个 BoardDetail``：``Number → Number``，
      ``Code → Name``，``Material.uuid → MaterialId``，并按 ``columns`` 反算 ``Row/Column``
      （``Number`` 从 1 开始，行优先）。
    - 位置（``PipettingPosList`` / ``gripperPos``）此处留空，由 ``merge_positions_into_board``
      后续填充（位置需真机标定）。
    """
    tablets = list(matrix_info.get("WorkTablets", []) or [])
    if columns <= 0:
        columns = 4
    max_number = 0
    details: List[BoardDetail] = []
    for wt in tablets:
        number = int(wt.get("Number", 0) or 0)
        max_number = max(max_number, number)
        material = wt.get("Material", {}) or {}
        idx = max(number - 1, 0)
        row = idx // columns + 1
        col = idx % columns + 1
        details.append(
            BoardDetail(
                name=wt.get("Code") or f"T{number}",
                number=number,
                row=row,
                column=col,
                # 按接口版本选 id：v04 用 id_v4，v03 用 uuid。
                material_id=_pick_material_id(material, is_v04),
                volume=int(material.get("Volume", 0) or 0),
            )
        )

    if rows is None:
        rows = (max_number + columns - 1) // columns if max_number else 0

    return Board(
        id=matrix_info.get("MatrixId"),
        name=matrix_info.get("MatrixName"),
        rows=rows,
        columns=columns,
        device_type=device_type,
        details=details,
    )


def prc_sites_to_board(
    row_nums: int,
    column_nums: int,
    prc_sites_config: Sequence[Dict[str, Any]],
    *,
    board_id: Optional[str] = None,
    device_type: int = 0,
) -> Board:
    """把 deck 布局（``row_nums``/``column_nums`` + ``prc_sites_config``）构建为 V04 ``Board``。

    结构对齐 ``boards.json``（``Rows/Columns/DeviceType/Details[...]``）。

    ID/名称规则：
    - ``board_id`` 缺省时生成 ``auto_board_{机器时间(ms)}``；``Board.Id == Board.Name == board_id``。
    - 每个 slot ``Number=x`` → ``Name="T{x}"``、``BoardDetail.Id = f"{board_id}_T{x}"``、``BoardId=board_id``。
    - ``Position.Id = f"{board_id}_T{x}_pose"``。

    纯布局：点位/物料留空（``Position=None``、``PipettingPosList=[]``、``gripperPos=None``、
    ``MaterialId=None``）。解析/校验（重复编号、越界、重叠、中文键兼容）复用
    ``PRCXI9300Deck._prc_site_dicts``，仅取其归一化后的 number/row/col/跨度。
    """
    if board_id is None:
        board_id = f"auto_board_{int(time.time() * 1000)}"

    # CreateTime 按 boards.json 的机器时间格式（本地时间 "YYYY-MM-DD HH:MM:SS"）；UpdateTime 留空。
    create_time = time.strftime("%Y-%m-%d %H:%M:%S")

    row_nums = int(row_nums)
    column_nums = int(column_nums)
    # 复用 deck 的解析+校验；col_pitch 仅用于 deck 坐标计算，本函数用不到，传 1.0。
    normalized = PRCXI9300Deck._prc_site_dicts(
        list(prc_sites_config or []), row_nums, column_nums, col_pitch=1.0
    )

    details: List[BoardDetail] = []
    for site in normalized:
        number = int(site["number"])
        detail_id = f"{board_id}_T{number}"
        details.append(
            BoardDetail(
                id=detail_id,
                create_time=create_time,
                board_id=board_id,
                name=f"T{number}",
                number=number,
                row=int(site["row"]),
                column=int(site["col"]),
                row_span=int(site["row_span"]),
                column_span=int(site["col_span"]),
                volume=0,
                material_id=None,
                module=0,
                # Position 元数据壳（对齐 boards.json，避免 UpdatePosition 因 Position=null 异常）；
                # 真正 XY 仍在 PipettingPosList，由 update_pipetting_position 回写。
                position=BoardPosition(
                    id=f"{detail_id}_pose",
                    board_detail_id=detail_id,
                    board_name=f"T{number}",
                    board_number=number,
                    create_time=create_time,
                ),
                pipetting_pos_list=[],
                gripper_pos=None,
            )
        )

    return Board(
        id=board_id,
        create_time=create_time,
        name=board_id,
        rows=row_nums,
        columns=column_nums,
        device_type=device_type,
        details=details,
    )


# AxisEnum 对齐 boards.json / AxisNum：Left=1 / Right=2 / ClampingJaw=3（int）。
_AXIS_ENUM_INT = {"Left": 1, "Right": 2, "ClampingJaw": 3}

# VolumeEnum(MaterialVolumeEnum) 不能为 null（服务端会报转换失败）；缺省用 1000（μL）。
_DEFAULT_VOLUME_ENUM = 1000

# trash 落枪头抬高量（mm）：trash 槽位注册移液坐标时把 Z 各分量减去此值（数值越小物理越高），
# 避免落枪头时下探过深。抬高后统一 clamp 到 ≥ 0。
_TRASH_Z_RAISE_MM = 60.0

_logger = logging.getLogger(__name__)


def _safe_print(text: str, chunk_size: int = 8192) -> None:
    """安全打印超大文本：Windows 控制台单次写入过大字符串会抛
    ``OSError: [Errno 22] Invalid argument``（96 孔协议 steps_todo_list 序列化后可达数十 KB，
    在 debugpy 控制台里尤其容易触发）。这里分块写入并吞掉写失败——纯调试输出，
    不应因此中断方案执行。"""
    try:
        for i in range(0, len(text), chunk_size):
            sys.stdout.write(text[i : i + chunk_size])
        sys.stdout.write("\n")
        sys.stdout.flush()
    except OSError:
        pass

# vol(µL) → prcxi_labware tip 工厂名；长度取 tip.total_tip_length。
# 10µL 与 eTips 长度相同，取标准 PRCXI_10uL_Tips。
_TIP_LENGTH_CACHE: Dict[int, float] = {}
_TIP_VOLUME_FACTORY_NAMES: Dict[int, str] = {
    10: "PRCXI_10uL_Tips",
    50: "PRCXI_50uL_tips",
    200: "PRCXI_200uL_Tips",
    300: "PRCXI_300ul_Tips",
    1000: "PRCXI_1000uL_Tips",
    1250: "PRCXI_1250uL_Tips",
}


def _tip_total_length_from_rack(rack: TipRack) -> Optional[float]:
    """从 tip rack 首孔读取 tip 原型的 ``total_tip_length``（mm）。"""
    children = getattr(rack, "children", None) or []
    if not children:
        return None
    spot = children[0]
    tip = None
    tr = getattr(spot, "tracker", None)
    if tr is not None:
        tip = getattr(tr, "_tip", None) or getattr(tr, "tip", None)
    if tip is None:
        tip = getattr(spot, "tip", None)
    length = getattr(tip, "total_tip_length", None) if tip is not None else None
    if length is None:
        return None
    try:
        return float(length)
    except (TypeError, ValueError):
        return None


def resolve_tip_length_mm(vol: Any) -> float:
    """按 ``pip_setting`` 量程(µL)解析对应枪头长度(mm)。

    查表实例化 ``prcxi_labware`` tip rack 探针，读 ``total_tip_length``；
    未知量程 warning 并回退 0（不加 tip 补偿，与历史 ``tip_height=0`` 一致）。
    """
    try:
        key = int(float(vol))
    except (TypeError, ValueError):
        warnings.warn(f"resolve_tip_length_mm: 无法解析量程 {vol!r}，回退 tip 长度 0", stacklevel=2)
        return 0.0
    if key <= 0:
        warnings.warn(f"resolve_tip_length_mm: 非法量程 {vol!r}，回退 tip 长度 0", stacklevel=2)
        return 0.0
    if key in _TIP_LENGTH_CACHE:
        return _TIP_LENGTH_CACHE[key]

    factory_name = _TIP_VOLUME_FACTORY_NAMES.get(key)
    if factory_name is None:
        warnings.warn(
            f"resolve_tip_length_mm: 量程 {key}µL 无对应 tip 工厂，回退 tip 长度 0；"
            f"请补到 _TIP_VOLUME_FACTORY_NAMES",
            stacklevel=2,
        )
        return 0.0

    try:
        from . import prcxi_labware as _labware
    except Exception as exc:  # pragma: no cover
        warnings.warn(
            f"resolve_tip_length_mm: 无法导入 prcxi_labware ({exc!r})，回退 tip 长度 0",
            stacklevel=2,
        )
        return 0.0

    factory = getattr(_labware, factory_name, None)
    if not callable(factory):
        warnings.warn(
            f"resolve_tip_length_mm: 工厂 {factory_name} 不可用，回退 tip 长度 0",
            stacklevel=2,
        )
        return 0.0

    try:
        rack = factory(f"_tip_len_probe_{key}")
        length = _tip_total_length_from_rack(rack)
    except Exception as exc:  # pragma: no cover
        warnings.warn(
            f"resolve_tip_length_mm: 探针 {factory_name} 失败 ({exc!r})，回退 tip 长度 0",
            stacklevel=2,
        )
        return 0.0

    if length is None:
        warnings.warn(
            f"resolve_tip_length_mm: {factory_name} 未读到 total_tip_length，回退 0",
            stacklevel=2,
        )
        return 0.0

    _TIP_LENGTH_CACHE[key] = length
    _logger.debug("resolve_tip_length_mm: vol=%s → %s mm (%s)", key, length, factory_name)
    return length


def _to_volume_enum(value: Any) -> int:
    """把体积归一化为合法 MaterialVolumeEnum(int)；空/非法回退默认值，避免下发 null。"""
    try:
        v = int(float(value))
    except (TypeError, ValueError):
        return _DEFAULT_VOLUME_ENUM
    return v if v > 0 else _DEFAULT_VOLUME_ENUM


def legacy_pipetting_pos_to_v04(
    pos: Dict[str, Any],
    *,
    create_time: Optional[str] = None,
    board_detail_id: Optional[str] = None,
) -> Tuple[List[PipettingPos], List[str]]:
    """把老版本移液位置 dict（左/右轴合一）映射为 V04 ``PipettingPos`` 列表（左、右各一）。

    字段结构对齐 ``boards.json`` 的 ``PipettingPosList``：每条位置都带 ``Id``/``CreateTime``/
    ``BoardDetailId``，``AxisEnum`` 用 int（Left=1/Right=2）。``bottleMouthPosition`` /
    ``SafeAltitude`` 若入参 pos 带同名键则读取，否则置 0。

    靠壁三个字段 ``leftWallDistance/rightWallDistance/zWallDistance`` 恒置 0（对齐古DNA）：
    native 贴壁由放液步骤的 ``LiquidDispensingMethod`` 控制，与板位这三个字段无关。
    """
    ct = create_time or time.strftime("%Y-%m-%d %H:%M:%S")

    def _f(key: str, default: float = 0.0) -> float:
        try:
            return float(pos.get(key, default) or default)
        except (TypeError, ValueError):
            return default

    left = PipettingPos(
        id=str(uuid.uuid4()),
        create_time=ct,
        board_detail_id=board_detail_id,
        axis_enum=_AXIS_ENUM_INT["Left"],
        volume_enum=_to_volume_enum(pos.get("VolumeEnum")),
        x_position=_f("XPos"),
        y_position=_f("YPos"),
        bottle_mouth_position=_f("bottleMouthPosition"),
        bottle_bottom_position=_f("ZPos"),
        left_wall_distance=0.0,
        right_wall_distance=0.0,
        z_wall_distance=0.0,
        safe_altitude=_f("SafeAltitude"),
    )
    result = [left]

    if any(k in pos for k in ("X2Pos", "Y2Pos", "Z2Pos")):
        result.append(
            PipettingPos(
                id=str(uuid.uuid4()),
                create_time=ct,
                board_detail_id=board_detail_id,
                axis_enum=_AXIS_ENUM_INT["Right"],
                volume_enum=_to_volume_enum(pos.get("VolumeEnum2", pos.get("VolumeEnum"))),
                x_position=_f("X2Pos"),
                y_position=_f("Y2Pos"),
                bottle_mouth_position=_f("bottleMouthPosition2"),
                bottle_bottom_position=_f("Z2Pos"),
                left_wall_distance=0.0,
                right_wall_distance=0.0,
                z_wall_distance=0.0,
                safe_altitude=_f("SafeAltitude2"),
            )
        )

    return result, []


def legacy_claw_pos_to_v04(
    pos: Dict[str, Any],
    *,
    create_time: Optional[str] = None,
    board_detail_id: Optional[str] = None,
) -> GripperPos:
    """把老版本夹爪位置 dict（``XPos/YPos/ZPos``）映射为 V04 ``GripperPos``。

    对齐 ``boards.json`` 的 ``gripperPos``：带 ``Id``/``CreateTime``/``BoardDetailId``，
    ``AxisEnum`` 用 int（ClampingJaw=3）。
    """

    def _f(key: str) -> float:
        try:
            return float(pos.get(key, 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    return GripperPos(
        id=str(uuid.uuid4()),
        create_time=create_time or time.strftime("%Y-%m-%d %H:%M:%S"),
        board_detail_id=board_detail_id,
        axis_enum=_AXIS_ENUM_INT["ClampingJaw"],
        x_position=_f("XPos"),
        y_position=_f("YPos"),
        z_position=_f("ZPos"),
        z2_position=_f("Z2Pos"),
    )


def merge_positions_into_board(
    board: Union[Board, Dict[str, Any]],
    pipetting_positions: Optional[List[Dict[str, Any]]] = None,
    claw_positions: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Dict[str, Any], List[str]]:
    """把老版本位置列表按 ``Number`` 合并进 Board 的对应 ``BoardDetail``，返回 RPC dict + warnings。

    ``board`` 可为 :class:`Board` 或服务端返回的 Board dict。合并后可直接作为
    ``UpdatePosition_V04`` / ``AddWorkTabletMatrix_V04`` 入参。
    """
    warnings: List[str] = []
    board_dict = board.to_rpc_dict() if isinstance(board, Board) else dict(board or {})
    details = board_dict.get("Details") or []

    pip_by_num: Dict[int, Dict[str, Any]] = {}
    for p in pipetting_positions or []:
        try:
            pip_by_num[int(p.get("Number"))] = p
        except (TypeError, ValueError):
            continue
    claw_by_num: Dict[int, Dict[str, Any]] = {}
    for c in claw_positions or []:
        try:
            claw_by_num[int(c.get("Number"))] = c
        except (TypeError, ValueError):
            continue

    for detail in details:
        try:
            number = int(detail.get("Number"))
        except (TypeError, ValueError):
            continue
        if number in pip_by_num:
            # CreateTime 在此刻（输入/回写时）赋值；BoardDetailId 取该 Detail 的 Id。
            pip_list, w = legacy_pipetting_pos_to_v04(
                pip_by_num[number], board_detail_id=detail.get("Id")
            )
            detail["PipettingPosList"] = [p.to_rpc_dict() for p in pip_list]
            warnings.extend(w)
        if number in claw_by_num:
            detail["gripperPos"] = legacy_claw_pos_to_v04(
                claw_by_num[number], board_detail_id=detail.get("Id")
            ).to_rpc_dict()

    board_dict["Details"] = details
    return board_dict, sorted(set(warnings))


class Material(TypedDict):  # 和Plate同关系
    uuid: str
    Code: Optional[str]
    Name: Optional[str]
    SummaryName: Optional[str]
    PipetteHeight: Optional[int]
    materialEnum: Optional[int]


class WorkTablets(TypedDict):
    Number: int
    Code: str
    Material: Dict[str, Any]


class MatrixInfo(TypedDict):
    MatrixId: str
    MatrixName: str
    MatrixCount: int
    WorkTablets: list[WorkTablets]


def _get_slot_number(resource, deck: Optional["PRCXI9300Deck"] = None) -> Optional[int]:
    """从 resource 的 ``update_resource_site`` 或位置反算 1-based 槽位号。"""
    extra = getattr(resource, "unilabos_extra", {}) or {}
    site = extra.get("update_resource_site", "")
    if site:
        digits = "".join(c for c in str(site) if c.isdigit())
        return int(digits) if digits else None

    loc = getattr(resource, "location", None)

    # 优先使用 deck.sites 的真实坐标映射，兼容 9320 动态列布局。
    deck_cls = globals().get("PRCXI9300Deck")
    if deck is None and deck_cls is not None:
        cur = resource
        while cur is not None:
            if isinstance(cur, deck_cls):
                deck = cur
                break
            cur = getattr(cur, "parent", None)
    if deck is not None and loc is not None:
        slot_from_deck = deck.slot_from_location(loc, tolerance=1.0)
        if slot_from_deck is not None:
            return slot_from_deck

    # 兜底：兼容历史 4×4 固定布局反算（9320 默认布局）。
    if loc is not None and loc.x is not None and loc.y is not None:
        col = round((loc.x - 5) / 137.5)
        row = round(3 - (loc.y - 13) / 96)
        idx = row * 4 + col
        if 0 <= idx < 16:
            return idx + 1
    return None


class PRCXI9300Deck(Deck):
    """PRCXI 9300 的专用 Deck 类，继承自 Deck。

    该类定义了 PRCXI 9300 的工作台布局和槽位信息。
    """

    _9320_ROWS = 4
    _9320_COLUMN_RAILS = 5
    _9320_X_OFFSET = 5.0
    _9320_ROW_PITCH = 96.0
    _9320_Y_OFFSET = 13.0
    _9320_DEFAULT_RAIL_WIDTH = 27.5
    _9320_DEFAULT_COL_PITCH = _9320_COLUMN_RAILS * _9320_DEFAULT_RAIL_WIDTH
    # 注意：类变量推导式在 Python 作用域下无法稳定引用同级类变量，故这里保留字面值，
    # 动态布局统一走 ``build_9320_site_positions``。
    _9320_SITE_POSITIONS = [((i % 4) * 137.5 + 5, (3 - int(i / 4)) * 96 + 13, 0) for i in range(0, 16)]


    # 9300: 3列×2行 = 6 slots，间距与9320相同（X: 138mm, Y: 96mm）
    _9300_SITE_POSITIONS = [
        (0, 96, 0),  (138, 96, 0),  (276, 96, 0),   # T1-T3 (第1行, 上)
        (0, 0, 0),   (138, 0, 0),   (276, 0, 0),     # T4-T6 (第2行, 下)
    ]

    # 向后兼容别名
    _DEFAULT_SITE_POSITIONS = _9320_SITE_POSITIONS
    _DEFAULT_SITE_SIZE = {"width": 128.0, "height": 86, "depth": 0}
    _DEFAULT_CONTENT_TYPE = ["plate", "tip_rack", "plates", "tip_racks", "tube_rack", "adaptor", "plateadapter", "module", "trash"]

    @property
    def sites(self):
        sites_out = []
        for i, site in enumerate(self._sites):
            occupied = self._get_site_resource(i)
            sites_out.append({
                "label": site["label"],
                "number": int(site.get("number", i + 1)),
                "row": site.get("row", -1),
                "col": site.get("col", -1),
                "row_span": site.get("row_span", 1),
                "col_span": site.get("col_span", 1),
                "visible": site.get("visible", True),
                "occupied_by": occupied.name if occupied is not None else None,
                "position": site["position"],
                "size": site["size"],
                "content_type": site["content_type"],
            })
        return sites_out

    def __init__(self, name: str, size_x: float, size_y: float, size_z: float,
                 sites: Optional[List[Dict[str, Any]]] = None, **kwargs):
        super().__init__(size_x, size_y, size_z, name=name)

        # Deck 基类有 model 字段，PRCXI 这里保留并用于区分 9300/9320 默认布局。
        model = kwargs.pop("model", None)
        if model is not None:
            self.model = model

        # 记录 9320 动态布局参数（默认对齐历史 4 行 4 列、0 间隔）。
        self._layout_row_nums: int = self._9320_ROWS
        self._layout_column_nums: int = 4
        self._layout_rail_interval: float = 0.0
        self._layout_rail_width: float = self._9320_DEFAULT_RAIL_WIDTH
        self._layout_col_pitch: float = self._9320_DEFAULT_COL_PITCH
        # prc_sites_config：自定义板位（编号/行/列/跨行/跨列）；为空时用 origin 网格。
        self._prc_sites_config: List[Dict[str, Any]] = []
        # slot 号 → _sites 下标映射（支持 prc_sites_config 的显式编号）。
        self._number_to_index: Dict[int, int] = {}

        if sites is not None:
            self._sites: List[Dict[str, Any]] = [dict(s) for s in sites]
        else:
            model_name = str(getattr(self, "model", "") or "").strip().lower()
            default_positions = (
                self._9300_SITE_POSITIONS
                if model_name == "9300"
                else self._DEFAULT_SITE_POSITIONS
            )
            self._set_sites_from_positions(default_positions)

        self._refresh_ordering()
        self.root = self.get_root()

    def _refresh_ordering(self) -> None:
        # _ordering: label -> None, 用于外部通过 list(keys()).index(site) 将 Tn 转换为 spot index
        self._ordering = collections.OrderedDict(
            (site["label"], None) for site in self._sites
        )
        # 重建 slot 号 → 下标映射（origin 网格为连续 1..N；prc_sites_config 用显式编号）。
        self._number_to_index = {}
        for idx, site in enumerate(self._sites):
            self._number_to_index[int(site.get("number", idx + 1))] = idx

    @classmethod
    def _make_site(
        cls,
        number: int,
        label: str,
        row: int,
        col: int,
        row_span: int,
        col_span: int,
        x: float,
        y: float,
        width: float,
        height: float,
        z: float = 0.0,
    ) -> Dict[str, Any]:
        return {
            "label": str(label),
            "number": int(number),
            "row": int(row),
            "col": int(col),
            "row_span": int(row_span),
            "col_span": int(col_span),
            "visible": True,
            "position": {"x": float(x), "y": float(y), "z": float(z)},
            "size": {"width": float(width), "height": float(height), "depth": 0.0},
            "content_type": list(cls._DEFAULT_CONTENT_TYPE),
        }

    def _set_sites_from_positions(self, positions: Sequence[Tuple[float, float, float]]) -> None:
        self._sites = []
        for i, (x, y, z) in enumerate(positions):
            self._sites.append(self._make_site(
                number=i + 1, label=f"T{i + 1}", row=-1, col=-1,
                row_span=1, col_span=1, x=x, y=y, z=z,
                width=self._DEFAULT_SITE_SIZE["width"],
                height=self._DEFAULT_SITE_SIZE["height"],
            ))
        self._refresh_ordering()

    @classmethod
    def _entry_int(cls, entry: Dict[str, Any], keys: Sequence[str], default: Optional[int] = None,
                   required: bool = False) -> Optional[int]:
        """从 prc_sites_config 元素读取整数字段，兼容多种键名（含中文）。"""
        for k in keys:
            if k in entry and entry[k] is not None:
                return int(entry[k])
        if required:
            raise ValueError(f"prc_sites_config 元素缺少必填字段 {keys[0]!r}: {entry}")
        return default

    @classmethod
    def _span_geometry(cls, row: int, col: int, row_span: int, col_span: int,
                       row_nums: int, col_pitch: float) -> Tuple[float, float, float, float]:
        """跨行/跨列板位的角坐标与包围盒尺寸，返回 (x, y, width, height)。

        与单格板位保持同一约定：position 为板位**左下角**（前端 y 轴向上），
        起始格 (row,col) 位于覆盖区域左上角、末格 (row+row_span-1, col+col_span-1)
        位于右下角。size 为覆盖这些单元格足迹的包围盒：单格用默认足迹 128×86，
        每多跨一行/列再加一个行距/列距（不放大成整节距，避免与相邻格重叠）。
        """
        bottom_row = row + row_span - 1
        x = cls._9320_X_OFFSET + col * col_pitch
        y = (row_nums - 1 - bottom_row) * cls._9320_ROW_PITCH + cls._9320_Y_OFFSET
        width = (col_span - 1) * col_pitch + float(cls._DEFAULT_SITE_SIZE["width"])
        height = (row_span - 1) * cls._9320_ROW_PITCH + float(cls._DEFAULT_SITE_SIZE["height"])
        return x, y, width, height

    @classmethod
    def _origin_site_dicts(cls, row_nums: int, column_nums: int, col_pitch: float) -> List[Dict[str, Any]]:
        """origin 网格：row_nums × column_nums，行主序编号 T1..T(R*C)，单格默认尺寸。"""
        sites: List[Dict[str, Any]] = []
        for row in range(row_nums):
            for col in range(column_nums):
                n = row * column_nums + col + 1
                x = cls._9320_X_OFFSET + col * col_pitch
                y = (row_nums - 1 - row) * cls._9320_ROW_PITCH + cls._9320_Y_OFFSET
                sites.append(cls._make_site(
                    number=n, label=f"T{n}", row=row, col=col,
                    row_span=1, col_span=1, x=x, y=y,
                    width=cls._DEFAULT_SITE_SIZE["width"],
                    height=cls._DEFAULT_SITE_SIZE["height"],
                ))
        return sites

    @classmethod
    def _prc_site_dicts(cls, prc_sites_config: Sequence[Dict[str, Any]], row_nums: int,
                        column_nums: int, col_pitch: float) -> List[Dict[str, Any]]:
        """按 prc_sites_config 生成板位；origin 网格仅用于坐标计算与占用/越界校验。

        每个元素含【编号(number)、行(row)、列(col)、跨行(row_span)、跨列(col_span)】，
        名称默认为 ``T{编号}``。坐标与尺寸与单格板位同一约定（左下角 + 足迹包围盒），
        单格尺寸即默认足迹 128×86，跨格时按跨度扩展包围盒（见 ``_span_geometry``）。
        """
        occupied: Dict[Tuple[int, int], int] = {}
        built: List[Dict[str, Any]] = []
        for entry in prc_sites_config:
            number = cls._entry_int(entry, ["number", "编号", "Number", "板位号"], required=True)
            row = cls._entry_int(entry, ["row", "行", "Row"], default=0)
            col = cls._entry_int(entry, ["col", "column", "列", "Col", "Column"], default=0)
            row_span = cls._entry_int(entry, ["row_span", "跨行", "RowSpan", "rowspan"], default=1) or 1
            col_span = cls._entry_int(entry, ["col_span", "跨列", "ColSpan", "colspan"], default=1) or 1
            name = entry.get("name") or entry.get("名称") or entry.get("Name") or f"T{number}"

            if row < 0 or col < 0 or row_span < 1 or col_span < 1:
                raise ValueError(
                    f"prc_sites_config T{number} 参数非法："
                    f"row={row}, col={col}, row_span={row_span}, col_span={col_span}"
                )
            if row + row_span > row_nums or col + col_span > column_nums:
                raise ValueError(
                    f"prc_sites_config T{number} 覆盖区域 "
                    f"(行{row}..{row + row_span - 1}, 列{col}..{col + col_span - 1}) "
                    f"超出 {row_nums}×{column_nums} 网格"
                )
            for r in range(row, row + row_span):
                for c in range(col, col + col_span):
                    if (r, c) in occupied:
                        raise ValueError(
                            f"prc_sites_config T{number} 与 T{occupied[(r, c)]} 在格 ({r},{c}) 重叠"
                        )
                    occupied[(r, c)] = number

            x, y, width, height = cls._span_geometry(row, col, row_span, col_span, row_nums, col_pitch)
            built.append(cls._make_site(
                number=number, label=str(name), row=row, col=col,
                row_span=row_span, col_span=col_span, x=x, y=y,
                width=width, height=height,
            ))

        numbers = [s["number"] for s in built]
        if len(set(numbers)) != len(numbers):
            raise ValueError(f"prc_sites_config 存在重复编号：{sorted(numbers)}")
        built.sort(key=lambda s: s["number"])
        return built

    @staticmethod
    def _slot_to_number(slot: Union[int, str]) -> Optional[int]:
        """把 slot 标识（int / '3' / 'T3'）解析为槽位号。"""
        if isinstance(slot, bool):
            return None
        if isinstance(slot, int):
            return slot
        if isinstance(slot, str):
            digits = "".join(c for c in slot.strip() if c.isdigit())
            return int(digits) if digits else None
        return None

    def slot_index(self, slot: Union[int, str]) -> Optional[int]:
        """槽位号 → ``_sites`` 下标；不存在返回 None。"""
        number = self._slot_to_number(slot)
        if number is None:
            return None
        return self._number_to_index.get(number)

    @classmethod
    def build_9320_site_positions(
        cls,
        column_nums: int,
        rail_interval: float,
        rail_width: float,
        row_nums: Optional[int] = None,
    ) -> List[Tuple[float, float, float]]:
        """按 9320 规则生成 origin 网格坐标（row_nums 行 × column_nums 列，行主序）。"""
        if column_nums <= 0:
            raise ValueError(f"column_nums 必须 > 0，收到 {column_nums}")
        rows = int(row_nums) if row_nums is not None else cls._9320_ROWS
        if rows <= 0:
            raise ValueError(f"row_nums 必须 > 0，收到 {rows}")
        col_pitch = (cls._9320_COLUMN_RAILS + float(rail_interval)) * float(rail_width)
        positions: List[Tuple[float, float, float]] = []
        for row in range(rows):
            for col in range(column_nums):
                x = cls._9320_X_OFFSET + col * col_pitch
                y = (rows - 1 - row) * cls._9320_ROW_PITCH + cls._9320_Y_OFFSET
                positions.append((x, y, 0.0))
        return positions

    @staticmethod
    def _slot_from_sites(
        location: Optional[Coordinate],
        sites: Sequence[Dict[str, Any]],
        tolerance: float = 1.0,
    ) -> Optional[int]:
        if location is None or location.x is None or location.y is None:
            return None

        # 优先精确匹配（assign_child_at_slot 的标准路径）。
        for idx, site in enumerate(sites):
            pos = site.get("position", {})
            sx = float(pos.get("x", 0.0))
            sy = float(pos.get("y", 0.0))
            sz = float(pos.get("z", 0.0))
            lz = float(location.z or 0.0)
            if abs(location.x - sx) < 1e-6 and abs(location.y - sy) < 1e-6 and abs(lz - sz) < 1e-6:
                return int(site.get("number", idx + 1))

        # 再做带阈值最近邻匹配，兼容云端坐标轻微浮点偏差。
        nearest_idx: Optional[int] = None
        nearest_dist_sq = float("inf")
        second_dist_sq = float("inf")
        for idx, site in enumerate(sites):
            pos = site.get("position", {})
            sx = float(pos.get("x", 0.0))
            sy = float(pos.get("y", 0.0))
            dist_sq = (location.x - sx) ** 2 + (location.y - sy) ** 2
            if dist_sq < nearest_dist_sq:
                second_dist_sq = nearest_dist_sq
                nearest_dist_sq = dist_sq
                nearest_idx = idx
            elif dist_sq < second_dist_sq:
                second_dist_sq = dist_sq
        _within = nearest_idx is not None and nearest_dist_sq <= float(tolerance) ** 2
        if _within:
            return int(sites[nearest_idx].get("number", nearest_idx + 1))
        return None

    def slot_from_location(self, location: Optional[Coordinate], tolerance: float = 1.0) -> Optional[int]:
        return self._slot_from_sites(location=location, sites=self.sites, tolerance=tolerance)

    def reconfigure_9320_layout(
        self,
        column_nums: int,
        rail_interval: float,
        rail_width: float,
        row_nums: Optional[int] = None,
        prc_sites_config: Optional[Sequence[Dict[str, Any]]] = None,
        preserve_children: bool = True,
    ) -> None:
        """按 9320 参数重建 sites，并尽量保持已挂载子资源槽位不变。

        - ``row_nums`` × ``column_nums`` 定义 origin 网格（默认 4 × column_nums）。
        - ``prc_sites_config`` 非空时：完全以其为准生成板位（origin 网格仅用于
          按 行/列/跨度 计算坐标与占用/越界校验），未列出的 origin 格不作为板位。
        - ``prc_sites_config`` 为空时：使用 origin 网格（行主序 T1..T(R*C)）。
        """
        old_sites = [dict(site) for site in self.sites]
        child_slot_map: Dict[Resource, int] = {}
        if preserve_children:
            for child in list(self.children):
                slot_no = _get_slot_number(child, deck=self)
                if slot_no is None:
                    slot_no = self._slot_from_sites(getattr(child, "location", None), old_sites, tolerance=1.0)
                if slot_no is not None:
                    child_slot_map[child] = slot_no

        column_nums = int(column_nums)
        rows = int(row_nums) if row_nums is not None else self._9320_ROWS
        if column_nums <= 0 or rows <= 0:
            raise ValueError(f"row_nums/column_nums 必须 > 0，收到 row_nums={rows}, column_nums={column_nums}")
        col_pitch = (self._9320_COLUMN_RAILS + float(rail_interval)) * float(rail_width)

        prc_sites_config = list(prc_sites_config or [])
        if prc_sites_config:
            self._sites = self._prc_site_dicts(prc_sites_config, rows, column_nums, col_pitch)
        else:
            self._sites = self._origin_site_dicts(rows, column_nums, col_pitch)
        self._refresh_ordering()

        self._layout_row_nums = rows
        self._layout_column_nums = column_nums
        self._layout_rail_interval = float(rail_interval)
        self._layout_rail_width = float(rail_width)
        self._layout_col_pitch = col_pitch
        self._prc_sites_config = prc_sites_config

        if preserve_children:
            for child, slot_no in child_slot_map.items():
                idx = self.slot_index(slot_no)
                if idx is not None:
                    child.location = self._get_site_location(idx)
                else:
                    print(
                        f"[PRCXI9300Deck] 子资源 {getattr(child, 'name', '?')} 原槽位 T{slot_no}"
                        f" 在新布局中不存在，保留原位置"
                    )

    def _get_site_location(self, idx: int) -> Coordinate:
        pos = self._sites[idx]["position"]
        return Coordinate(pos["x"], pos["y"], pos["z"])

    def get_slot_location(self, slot: Union[int, str]) -> Coordinate:
        """根据 slot 标识返回该 slot 的坐标。

        支持的输入：
        - int: 1-based slot 序号（与 ``assign_child_at_slot`` 一致），1 → sites[0]
        - str: 纯数字字符串 ``"3"``，或带前缀的 label ``"T3"``（不区分大小写）

        Raises:
            ValueError: slot 解析失败或越界
        """
        idx: Optional[int] = self.slot_index(slot)
        if idx is None and isinstance(slot, str):
            # 退而求其次：直接按 label 全等匹配
            s = slot.strip()
            for i, site in enumerate(self._sites):
                if site.get("label") == s:
                    idx = i
                    break
        if idx is None:
            raise ValueError(f"无法解析 slot 标识: {slot!r}")
        if idx < 0 or idx >= len(self._sites):
            raise ValueError(
                f"slot {slot!r} 超出范围 (共 {len(self._sites)} 个板位，解析为 idx={idx})"
            )
        return self._get_site_location(idx)

    def _get_site_resource(self, idx: int) -> Optional[Resource]:
        site_loc = self._get_site_location(idx)
        for child in self.children:
            if child.location == site_loc:
                return child
        return None

    def assign_child_resource(
        self,
        resource: Resource,
        location: Optional[Coordinate] = None,
        reassign: bool = True,
        spot: Optional[int] = None,
    ):
        idx = spot
        if spot is not None:
            idx = spot
        else:
            for i, site in enumerate(self.sites):
                site_loc = self._get_site_location(i)
                if site.get("label") == resource.name:
                    idx = i
                    break
                if location is not None and site_loc == location:
                    idx = i
                    break

        if idx is None:
            for i in range(len(self.sites)):
                if self._get_site_resource(i) is None:
                    idx = i
                    break

        if idx is None:
            raise ValueError(f"No available site on deck '{self.name}' for resource '{resource.name}'")

        if not reassign and self._get_site_resource(idx) is not None:
            # 当指定 slot 已占用时，直接按 slot 现有占位物料做替换。
            # 旧逻辑按 ``resource.name`` 去 root 检索，若名称尚未注册会抛 ResourceNotFoundError，
            # 进而把可恢复的 create_resource 变成硬失败。
            occupant = self._get_site_resource(idx)
            if occupant is not None and occupant is not resource and occupant.parent is not None:
                occupant.parent.unassign_child_resource(occupant)


        loc = self._get_site_location(idx)
        super().assign_child_resource(resource, location=loc, reassign=reassign)

    def assign_child_at_slot(self, resource: Resource, slot: int, reassign: bool = False) -> None:
        idx = self.slot_index(slot)
        if idx is None:
            raise ValueError(f"slot {slot!r} 不存在于当前布局（共 {len(self._sites)} 个板位）")
        self.assign_child_resource(resource, spot=idx, reassign=reassign)

    def serialize(self) -> dict:
        data = super().serialize()
        data["model"] = self.model
        sites_out = []
        for i, site in enumerate(self._sites):
            occupied = self._get_site_resource(i)
            sites_out.append({
                "label": site["label"],
                "number": int(site.get("number", i + 1)),
                "row": site.get("row", -1),
                "col": site.get("col", -1),
                "row_span": site.get("row_span", 1),
                "col_span": site.get("col_span", 1),
                "visible": site.get("visible", True),
                "occupied_by": occupied.name if occupied is not None else None,
                "position": site["position"],
                "size": site["size"],
                "content_type": site["content_type"],
            })
        data["sites"] = sites_out
        return data


class PRCXI9300Container(Container):
    """PRCXI 9300 的专用 Container 类，继承自 Plate，用于槽位定位和未知模块。

    该类定义了 PRCXI 9300 的工作台布局和槽位信息。
    """

    def __init__(
        self,
        name: str,
        size_x: float,
        size_y: float,
        size_z: float,
        category: str,
        model: Optional[str] = None,
        **kwargs,
    ):
        super().__init__(name, size_x, size_y, size_z, category=category, model=model)
        self._unilabos_state = {}

    def load_state(self, state: Dict[str, Any]) -> None:
        """从给定的状态加载工作台信息。"""
        super().load_state(state)
        self._unilabos_state = state

    def serialize_state(self) -> Dict[str, Dict[str, Any]]:
        data = super().serialize_state()
        data.update(self._unilabos_state)
        return data


class PRCXI9300Plate(Plate):
    """
    专用孔板类：
    1. 继承自 PLR 原生 Plate，保留所有物理特性。
    2. 增加 material_info 参数，用于在初始化时直接绑定 Unilab UUID。
    """

    def __init__(
        self,
        name: str,
        size_x: float,
        size_y: float,
        size_z: float,
        category: str = "plate",
        ordered_items: collections.OrderedDict = None,
        ordering: Optional[collections.OrderedDict] = None,
        model: Optional[str] = None,
        material_info: Optional[Dict[str, Any]] = None,
        **kwargs,
    ):
        # 如果 ordered_items 不为 None，直接使用
        items = None
        ordering_param = None
        if ordered_items is not None:
            items = ordered_items
        elif ordering is not None:
            # 检查 ordering 中的值是否是字符串（从 JSON 反序列化时的情况）
            # 如果是字符串，说明这是位置名称，需要让 Plate 自己创建 Well 对象
            # 我们只传递位置信息（键），不传递值，使用 ordering 参数
            if ordering:
                values = list(ordering.values())
                value = values[0]
                if isinstance(value, str):
                    # ordering 的值是字符串，只使用键（位置信息）创建新的 OrderedDict
                    # 传递 ordering 参数而不是 ordered_items，让 Plate 自己创建 Well 对象
                    items = None
                    # 使用 ordering 参数，只包含位置信息（键）
                    ordering_param = collections.OrderedDict((k, None) for k in ordering.keys())
                elif value is None:
                    ordering_param = ordering
            else:
                # ordering 的值已经是对象，可以直接使用
                items = ordering
                ordering_param = None

        # 根据情况传递不同的参数
        if items is not None:
            super().__init__(
                name, size_x, size_y, size_z, ordered_items=items, category=category, model=model, **kwargs
            )
        elif ordering_param is not None:
            # 传递 ordering 参数，让 Plate 自己创建 Well 对象
            super().__init__(
                name, size_x, size_y, size_z, ordering=ordering_param, category=category, model=model, **kwargs
            )
        else:
            super().__init__(name, size_x, size_y, size_z, category=category, model=model, **kwargs)

        self._unilabos_state = {}
        if material_info:
            self._unilabos_state["Material"] = material_info

    def load_state(self, state: Dict[str, Any]) -> None:
        super().load_state(state)
        self._unilabos_state = state

    def serialize_state(self) -> Dict[str, Dict[str, Any]]:
        try:
            data = super().serialize_state()
        except AttributeError:
            data = {}
        if hasattr(self, "_unilabos_state") and self._unilabos_state:
            safe_state = {}
            for k, v in self._unilabos_state.items():
                # 如果是 Material 字典，深入检查
                if k == "Material" and isinstance(v, dict):
                    safe_material = {}
                    for mk, mv in v.items():
                        # 只保留基本数据类型 (字符串, 数字, 布尔值, 列表, 字典)
                        if isinstance(mv, (str, int, float, bool, list, dict, type(None))):
                            safe_material[mk] = mv
                        else:
                            # 打印日志提醒（可选）
                            # print(f"Warning: Removing non-serializable key {mk} from {self.name}")
                            pass
                    safe_state[k] = safe_material
                # 其他顶层属性也进行类型检查
                elif isinstance(v, (str, int, float, bool, list, dict, type(None))):
                    safe_state[k] = v

            data.update(safe_state)
        return data  # 其他顶层属性也进行类型检查


class PRCXI9300TipRack(TipRack):
    """专用吸头盒类"""

    def __init__(
        self,
        name: str,
        size_x: float,
        size_y: float,
        size_z: float,
        category: str = "tip_rack",
        ordered_items: collections.OrderedDict = None,
        ordering: Optional[collections.OrderedDict] = None,
        model: Optional[str] = None,
        material_info: Optional[Dict[str, Any]] = None,
        **kwargs,
    ):
        # 如果 ordered_items 不为 None，直接使用
        if ordered_items is not None:
            items = ordered_items
        elif ordering is not None:
            # 检查 ordering 中的值类型来决定如何处理：
            # - 字符串值（从 JSON 反序列化）: 只用键创建 ordering_param
            # - None 值（从第二次往返序列化）: 同样只用键创建 ordering_param
            # - 对象值（已经是实际的 Resource 对象）: 直接作为 ordered_items 使用
            first_val = next(iter(ordering.values()), None) if ordering else None
            if not ordering or first_val is None or isinstance(first_val, str):
                # ordering 的值是字符串或 None，只使用键（位置信息）创建新的 OrderedDict
                # 传递 ordering 参数而不是 ordered_items，让 TipRack 自己创建 Tip 对象
                items = None
                ordering_param = collections.OrderedDict((k, None) for k in ordering.keys())
            else:
                # ordering 的值已经是对象，可以直接使用
                items = ordering
                ordering_param = None
        else:
            items = None
            ordering_param = None

        # 根据情况传递不同的参数
        if items is not None:
            super().__init__(
                name, size_x, size_y, size_z, ordered_items=items, category=category, model=model, **kwargs
            )
        elif ordering_param is not None:
            # 传递 ordering 参数，让 TipRack 自己创建 Tip 对象
            super().__init__(
                name, size_x, size_y, size_z, ordering=ordering_param, category=category, model=model, **kwargs
            )
        else:
            super().__init__(name, size_x, size_y, size_z, category=category, model=model, **kwargs)
        self._unilabos_state = {}
        if material_info:
            self._unilabos_state["Material"] = material_info

    def load_state(self, state: Dict[str, Any]) -> None:
        super().load_state(state)
        self._unilabos_state = state

    def serialize_state(self) -> Dict[str, Dict[str, Any]]:
        try:
            data = super().serialize_state()
        except AttributeError:
            data = {}
        if hasattr(self, "_unilabos_state") and self._unilabos_state:
            safe_state = {}
            for k, v in self._unilabos_state.items():
                # 如果是 Material 字典，深入检查
                if k == "Material" and isinstance(v, dict):
                    safe_material = {}
                    for mk, mv in v.items():
                        # 只保留基本数据类型 (字符串, 数字, 布尔值, 列表, 字典)
                        if isinstance(mv, (str, int, float, bool, list, dict, type(None))):
                            safe_material[mk] = mv
                        else:
                            # 打印日志提醒（可选）
                            # print(f"Warning: Removing non-serializable key {mk} from {self.name}")
                            pass
                    safe_state[k] = safe_material
                # 其他顶层属性也进行类型检查
                elif isinstance(v, (str, int, float, bool, list, dict, type(None))):
                    safe_state[k] = v

            data.update(safe_state)
        return data


class PRCXI9300Trash(Trash):
    """PRCXI 9300 的专用 Trash 类，继承自 Trash。

    该类定义了 PRCXI 9300 的工作台布局和槽位信息。
    """

    def __init__(
        self,
        name: str,
        size_x: float,
        size_y: float,
        size_z: float,
        category: str = "trash",
        material_info: Optional[Dict[str, Any]] = None,
        **kwargs,
    ):

        if name != "trash":
            print(f"Warning: PRCXI9300Trash usually expects name='trash' for backend logic, but got '{name}'.")
        super().__init__(name, size_x, size_y, size_z, category=category, **kwargs)
        self._unilabos_state = {}
        # 初始化时注入 UUID
        if material_info:
            self._unilabos_state["Material"] = material_info

    def load_state(self, state: Dict[str, Any]) -> None:
        """从给定的状态加载工作台信息。"""
        # super().load_state(state)
        self._unilabos_state = state

    def serialize_state(self) -> Dict[str, Dict[str, Any]]:
        try:
            data = super().serialize_state()
        except AttributeError:
            data = {}
        if hasattr(self, "_unilabos_state") and self._unilabos_state:
            safe_state = {}
            for k, v in self._unilabos_state.items():
                # 如果是 Material 字典，深入检查
                if k == "Material" and isinstance(v, dict):
                    safe_material = {}
                    for mk, mv in v.items():
                        # 只保留基本数据类型 (字符串, 数字, 布尔值, 列表, 字典)
                        if isinstance(mv, (str, int, float, bool, list, dict, type(None))):
                            safe_material[mk] = mv
                        else:
                            # 打印日志提醒（可选）
                            # print(f"Warning: Removing non-serializable key {mk} from {self.name}")
                            pass
                    safe_state[k] = safe_material
                # 其他顶层属性也进行类型检查
                elif isinstance(v, (str, int, float, bool, list, dict, type(None))):
                    safe_state[k] = v

            data.update(safe_state)
        return data


class PRCXI9300TubeRack(TubeRack):
    """
    专用管架类：用于 EP 管架、试管架等。
    继承自 PLR 的 TubeRack，并支持注入 material_info (UUID)。
    """

    def __init__(
        self,
        name: str,
        size_x: float,
        size_y: float,
        size_z: float,
        category: str = "tube_rack",
        items: Optional[Dict[str, Any]] = None,
        ordered_items: Optional[OrderedDict] = None,
        ordering: Optional[OrderedDict] = None,
        model: Optional[str] = None,
        material_info: Optional[Dict[str, Any]] = None,
        **kwargs,
    ):

        # 如果 ordered_items 不为 None，直接使用
        if ordered_items is not None:
            items_to_pass = ordered_items
            ordering_param = None
        elif ordering is not None:
            # 检查 ordering 中的值类型来决定如何处理：
            # - 字符串值（从 JSON 反序列化）: 只用键创建 ordering_param
            # - None 值（从第二次往返序列化）: 同样只用键创建 ordering_param
            # - 对象值（已经是实际的 Resource 对象）: 直接作为 ordered_items 使用
            first_val = next(iter(ordering.values()), None) if ordering else None
            if not ordering or first_val is None or isinstance(first_val, str):
                # ordering 的值是字符串或 None，只使用键（位置信息）创建新的 OrderedDict
                # 传递 ordering 参数而不是 ordered_items，让 TubeRack 自己创建 Tube 对象
                items_to_pass = None
                ordering_param = collections.OrderedDict((k, None) for k in ordering.keys())
            else:
                # ordering 的值已经是对象，可以直接使用
                items_to_pass = ordering
                ordering_param = None
        elif items is not None:
            # 兼容旧的 items 参数
            items_to_pass = items
            ordering_param = None
        else:
            items_to_pass = None
            ordering_param = None

        # 根据情况传递不同的参数
        if items_to_pass is not None:
            super().__init__(
                name, size_x, size_y, size_z, ordered_items=items_to_pass, category=category, model=model, **kwargs
            )
        elif ordering_param is not None:
            # 传递 ordering 参数，让 TubeRack 自己创建 Tube 对象
            super().__init__(
                name, size_x, size_y, size_z, ordering=ordering_param, category=category, model=model, **kwargs
            )
        else:
            super().__init__(name, size_x, size_y, size_z, category=category, model=model, **kwargs)

        self._unilabos_state = {}
        if material_info:
            self._unilabos_state["Material"] = material_info

    def serialize_state(self) -> Dict[str, Dict[str, Any]]:
        try:
            data = super().serialize_state()
        except AttributeError:
            data = {}
        if hasattr(self, "_unilabos_state") and self._unilabos_state:
            safe_state = {}
            for k, v in self._unilabos_state.items():
                # 如果是 Material 字典，深入检查
                if k == "Material" and isinstance(v, dict):
                    safe_material = {}
                    for mk, mv in v.items():
                        # 只保留基本数据类型 (字符串, 数字, 布尔值, 列表, 字典)
                        if isinstance(mv, (str, int, float, bool, list, dict, type(None))):
                            safe_material[mk] = mv
                        else:
                            # 打印日志提醒（可选）
                            # print(f"Warning: Removing non-serializable key {mk} from {self.name}")
                            pass
                    safe_state[k] = safe_material
                # 其他顶层属性也进行类型检查
                elif isinstance(v, (str, int, float, bool, list, dict, type(None))):
                    safe_state[k] = v

            data.update(safe_state)
        return data


class PRCXI9300ModuleSite(ItemizedCarrier):
    """
    PRCXI 功能模块的基础站点类（加热/冷却/震荡/磁吸等）。

    - 继承 ItemizedCarrier，可被拖放到 Deck 槽位上
    - 顶面有一个 ResourceHolder 站点，可吸附板类资源（叠放）
    - content_type 包含 "plateadapter" 以支持适配器叠放
    - 支持 material_info 注入
    """

    def __init__(self, name: str, size_x: float, size_y: float, size_z: float,
                 material_info: Optional[Dict[str, Any]] = None, **kwargs):
        sites = create_homogeneous_resources(
            klass=ResourceHolder,
            locations=[Coordinate(0, 0, 0)],
            resource_size_x=size_x,
            resource_size_y=size_y,
            resource_size_z=size_z,
            name_prefix=name,
        )[0]

        kwargs.pop('layout', None)
        sites_in = kwargs.pop('sites', None)

        sites_dict = {name: sites}

        content_type = [
            "plate",
            "tip_rack",
            "plates",
            "tip_racks",
            "tube_rack",
            "plateadapter",
        ]

        if sites_in is not None and isinstance(sites_in, dict):
            for site_key, site_value in sites_in.items():
                if site_key in sites_dict:
                    sites_dict[site_key] = site_value

        super().__init__(
            name, size_x, size_y, size_z,
            sites=sites_dict,
            num_items_x=kwargs.pop('num_items_x', 1),
            num_items_y=kwargs.pop('num_items_y', 1),
            num_items_z=kwargs.pop('num_items_z', 1),
            content_type=content_type,
            **kwargs,
        )
        self._unilabos_state = {}
        if material_info:
            self._unilabos_state["Material"] = material_info

    def assign_child_resource(self, resource, location=Coordinate(0, 0, 0), reassign=True, spot=None):
        from pylabrobot.resources.resource import Resource
        Resource.assign_child_resource(self, resource, location=location, reassign=reassign)

    def unassign_child_resource(self, resource):
        from pylabrobot.resources.resource import Resource
        Resource.unassign_child_resource(self, resource)

    def serialize_state(self) -> Dict[str, Dict[str, Any]]:
        try:
            data = super().serialize_state()
        except AttributeError:
            data = {}

        if hasattr(self, 'sites') and self.sites:
            sites_info = []
            for site in self.sites:
                if hasattr(site, '__class__') and 'pylabrobot' in str(site.__class__.__module__):
                    sites_info.append({
                        "__pylabrobot_object__": True,
                        "class": site.__class__.__name__,
                        "module": site.__class__.__module__,
                        "name": getattr(site, 'name', str(site))
                    })
                else:
                    sites_info.append(site)
            data['sites'] = sites_info

        if hasattr(self, "_unilabos_state") and self._unilabos_state:
            safe_state: Dict[str, Any] = {}
            for k, v in self._unilabos_state.items():
                if k == "Material" and isinstance(v, dict):
                    safe_material: Dict[str, Any] = {}
                    for mk, mv in v.items():
                        if isinstance(mv, (str, int, float, bool, list, dict, type(None))):
                            safe_material[mk] = mv
                    safe_state[k] = safe_material
                elif isinstance(v, (str, int, float, bool, list, dict, type(None))):
                    safe_state[k] = v
            data.update(safe_state)

        return data

    def load_state(self, state: Dict[str, Any]) -> None:
        super().load_state(state)
        if 'sites' in state:
            self.sites = [state['sites']]


class PRCXI9300PlateAdapter(PlateAdapter):
    """
    专用板式适配器类：用于承载 Plate 的底座（如 PCR 适配器、磁吸架等）。
    支持注入 material_info (UUID)。
    """

    def __init__(
        self,
        name: str,
        size_x: float,
        size_y: float,
        size_z: float,
        category: str = "plate_adapter",
        model: Optional[str] = None,
        material_info: Optional[Dict[str, Any]] = None,
        # 参数给予默认值 (标准96孔板尺寸)
        adapter_hole_size_x: float = 127.76,
        adapter_hole_size_y: float = 85.48,
        adapter_hole_size_z: float = 10.0,  # 假设凹槽深度或板子放置高度
        dx: Optional[float] = None,
        dy: Optional[float] = None,
        dz: float = 0.0,  # 默认Z轴偏移
        **kwargs,
    ):

        # 自动居中计算：如果未指定 dx/dy，则根据适配器尺寸和孔尺寸计算居中位置
        if dx is None:
            dx = (size_x - adapter_hole_size_x) / 2
        if dy is None:
            dy = (size_y - adapter_hole_size_y) / 2

        super().__init__(
            name=name,
            size_x=size_x,
            size_y=size_y,
            size_z=size_z,
            dx=dx,
            dy=dy,
            dz=dz,
            adapter_hole_size_x=adapter_hole_size_x,
            adapter_hole_size_y=adapter_hole_size_y,
            adapter_hole_size_z=adapter_hole_size_z,
            category=category,
            model=model,
            **kwargs,
        )

        self._unilabos_state = {}
        if material_info:
            self._unilabos_state["Material"] = material_info

    def serialize_state(self) -> Dict[str, Dict[str, Any]]:
        try:
            data = super().serialize_state()
        except AttributeError:
            data = {}
        if hasattr(self, "_unilabos_state") and self._unilabos_state:
            safe_state = {}
            for k, v in self._unilabos_state.items():
                # 如果是 Material 字典，深入检查
                if k == "Material" and isinstance(v, dict):
                    safe_material = {}
                    for mk, mv in v.items():
                        # 只保留基本数据类型 (字符串, 数字, 布尔值, 列表, 字典)
                        if isinstance(mv, (str, int, float, bool, list, dict, type(None))):
                            safe_material[mk] = mv
                        else:
                            # 打印日志提醒（可选）
                            # print(f"Warning: Removing non-serializable key {mk} from {self.name}")
                            pass
                    safe_state[k] = safe_material
                # 其他顶层属性也进行类型检查
                elif isinstance(v, (str, int, float, bool, list, dict, type(None))):
                    safe_state[k] = v

            data.update(safe_state)
        return data


class PRCXI9300Handler(LiquidHandlerAbstract):
    support_touch_tip = False
    # PRCXI 为列式 8 通道硬件，整列取枪头：开启列对齐（当前列剩余不足整列时跳过残余、
    # 从下一整列开头取），保证 8 通道 pick/asp/disp/drop 始终是完整列。
    _pickup_column_aligned = True

    @property
    def reset_ok(self) -> bool:
        """检查设备是否已重置成功。"""
        if self._unilabos_backend.debug:
            return True
        return self._unilabos_backend.is_reset_ok

    @property
    @topic_config(period=1.0)
    def state_list(self) -> str:
        """当前方案步骤进度 datakey：百分比字符串，如 ``"42.9%"``。

        与 ``reset_ok`` 同款：由 PropertyPublisher 定时器（1s）驱动，是全系统唯一
        发 RPC 刷新步骤缓存的入口；所有 ``wait_for_finish`` 方法只读该缓存。

        注：host_node 的 datakey 通道仅支持 float/int/str，数组（Int64MultiArray →
        array.array）会被丢弃，故这里以百分比字符串上报；绝对步数仍可经
        ``backend.step_progress`` 内部读取。
        """
        if self._unilabos_backend.debug:
            return "0.0%"
        completed, total = self._unilabos_backend.step_progress
        ratio = (completed / total) if total else 0.0
        return f"{round(ratio * 100, 1)}%"

    def __init__(
        self,
        deck: PRCXI9300Deck,
        host: str,
        port: int,
        timeout: float,
        channel_num=8,
        axis="Left",
        setup=True,
        debug=False,
        simulator=False,
        step_mode=False,
        matrix_id="",
        is_9320=False,
        start_rail=2,
        column_nums=4,
        row_nums=4,
        rail_nums: Optional[int] = None,
        rail_interval=0,
        rail_width=27.5,
        prc_sites_config: Optional[List[Dict[str, Any]]] = None,
        touch_tip_mode: Literal["native", "software", "both"] = "native",
        touch_tip_wall: Literal["follow_axis", "left", "right"] = "follow_axis",
        x_increase = -0.003636,
        y_increase = -0.003636,
        x_offset = -1.8,
        y_offset = -37.48,
        deck_z = 235.5,
        deck_y = 400,
        xy_coupling = -0.0045,
        calibration_points: Optional[Dict[str, List[List[float]]]] = None,
        calibration_labware_type: Optional[str] = "PRCXI_300ul_Tips",
        pip_setting: Optional[Dict[str, Dict[str, Any]]] = None,
        skip_position_recalc_when_matrix_exists: bool = True,
        protocol_version: Literal["v03", "v04"] = "v03",
        reset_status_inverted: Optional[bool] = None,
        wait_finish_timeout_s: Optional[float] = None,
    ):
        # 枪头轴配置：``{"left": {"vol": 100, "channels": 8}, "right": {"vol": 1000, "channels": 1}}``
        # 代表左轴 100µL/8 通道、右轴 1000µL/1 通道。None → 走 legacy 路由（≤10µL→右单通道[1]、
        # 8 通道[0..7]扁平化、backend [0]→Left/[1]→Right）。设置后启用 pip_setting 路由：
        # 按「通道优先、再看体积」选轴，通道编号约定左[0..7]/右[8..15]，channels 即真并行度。
        self.pip_setting: Optional[Dict[str, Dict[str, Any]]] = _normalize_pip_setting(pip_setting)

        # 根据 pip_setting 判断是否为「真 8 通道并行」硬件。若 pip_setting 任一轴 channels >= 8，则视为真 8 通道。
        self.has_true_8channel: bool = (
            self.pip_setting is not None and
            any(
                isinstance(cfg, dict) and cfg.get("channels", 0) >= 8
                for cfg in self.pip_setting.values()
            )
        )

        # 左右轴枪头长度(mm)：由 pip_setting.vol → tip 原型 total_tip_length。
        # 无 pip_setting 时为空，写板位 / 坐标转换回退到单一 self.tip_height。
        self._tip_height_by_axis: Dict[str, float] = {}
        if self.pip_setting is not None:
            for _axis_key in ("left", "right"):
                _cfg = self.pip_setting.get(_axis_key) or {}
                self._tip_height_by_axis[_axis_key] = float(
                    resolve_tip_length_mm(_cfg.get("vol"))
                )

        # rail_nums 是历史参数名，兼容旧配置（优先使用显式 rail_nums）。
        if rail_nums is not None:
            column_nums = rail_nums
        start_rail = float(start_rail)
        column_nums = int(column_nums)
        row_nums = int(row_nums)
        rail_interval = float(rail_interval)
        rail_width = float(rail_width)
        if column_nums <= 0:
            raise ValueError(f"column_nums 必须 > 0，收到 {column_nums}")
        if row_nums <= 0:
            raise ValueError(f"row_nums 必须 > 0，收到 {row_nums}")

        # prc_sites_config：自定义板位（编号/行/列/跨行/跨列）；为空则用 origin 网格。
        self._prc_sites_config: List[Dict[str, Any]] = list(prc_sites_config or [])

        # touch_tip 模式与靠壁方向（仅单次 touch_tip=True 时生效）。
        touch_tip_mode = str(touch_tip_mode or "native").strip().lower()
        touch_tip_wall = str(touch_tip_wall or "follow_axis").strip().lower()
        if touch_tip_mode not in TOUCH_TIP_MODES:
            raise ValueError(f"touch_tip_mode 必须是 {TOUCH_TIP_MODES}，收到 {touch_tip_mode!r}")
        if touch_tip_wall not in TOUCH_TIP_WALLS:
            raise ValueError(f"touch_tip_wall 必须是 {TOUCH_TIP_WALLS}，收到 {touch_tip_wall!r}")
        self.touch_tip_mode = touch_tip_mode
        self.touch_tip_wall = touch_tip_wall
        # software/both 才需要抽象层的软件式贴壁能力；native 走 dispense 靠壁，不用它。
        self.support_touch_tip = touch_tip_mode in ("software", "both")
        # 本次 transfer 是否勾选 touch_tip（由 transfer_liquid 设置、finally 清除）。
        self._touch_tip_pending: bool = False

        self._start_rail = start_rail
        self._column_nums = column_nums
        self._row_nums = row_nums
        self._rail_width = rail_width
        self._rail_interval = rail_interval
        # 这里区分两个物理量（历史上被塞进同一个 deck_x，导致绿框右侧多出 start_rail 死区宽度）：
        # - _x_axis_span：机器 X 轴量程，含 start_rail 死区（真实行程），仅用于坐标变换与边界钳制。
        # - deck_x：deck 物理 footprint，不含 start_rail 死区，用于可视绿框 + PLR/labware 仿真。
        # 槽位 X 锚在 X_OFFSET（不含 start_rail），故 footprint 必须去掉死区才能与槽位对称。
        self._x_axis_span = (start_rail + column_nums * 5 + (column_nums - 1) * rail_interval) * rail_width
        self.deck_x = (column_nums * 5 + (column_nums - 1) * rail_interval) * rail_width
        self.deck_y = deck_y
        self.deck_z = deck_z
        self.x_increase = x_increase
        self.y_increase = y_increase
        self.x_offset = x_offset
        self.y_offset = y_offset
        self.xy_coupling = xy_coupling
        self._slot_prcxi_positions: Dict[int, Tuple[float, float]] = {}
        self.calibration_labware_type = calibration_labware_type
        self.max_z_pipetting = 185
        self.max_z_claw = 300
        # 类级配置：当 backend.matrix_id 已存在时，是否跳过运行期位置重算/回写。
        # 设为 True 可避免每次 transfer 重复 update_pipetting_position。
        self.skip_position_recalc_when_matrix_exists = bool(
            skip_position_recalc_when_matrix_exists
        )

        if calibration_points is not None:
            self.calibrate_from_points(calibration_points, labware_type=self.calibration_labware_type)

        self.left_2_claw = Coordinate(130.2, -34, 74)
        self.right_2_left = Coordinate(22,-1, 12)
        self.tip_height = 0
        tablets_info = []

        if is_9320 is None:
            is_9320 = str(getattr(deck, "model", "9300")).strip().lower() == "9320"

        # 9320 支持按行列/轨道参数 + prc_sites_config 动态重建 deck 槽位布局。
        if is_9320 and isinstance(deck, PRCXI9300Deck):
            deck.reconfigure_9320_layout(
                column_nums=column_nums,
                rail_interval=rail_interval,
                rail_width=rail_width,
                row_nums=row_nums,
                prc_sites_config=self._prc_sites_config,
            )
            deck._size_x = self.deck_x
            deck._size_y = self.deck_y
            deck._size_z = self.deck_z

        if is_9320:
            print("当前设备是9320")
        else:
            for site_id in range(len(deck.sites)):
                child = deck._get_site_resource(site_id)
                # 如果放其他类型的物料，是不可以的
                if hasattr(child, "_unilabos_state") and "Material" in child._unilabos_state:
                    number = site_id + 1
                    tablets_info.append(
                        WorkTablets(
                            Number=number, Code=f"T{number}", Material=child._unilabos_state["Material"]
                        )
                    )
        # 始终初始化 step_mode 属性
        self.step_mode = False
        # step_mode 重入标志：复合动作（transfer_liquid 等）打开自己的 protocol 后置 True，
        # 内部子原语（尤其 mix）据此跳过各自的 create_protocol/run_protocol，避免嵌套
        # create_protocol 清空复合动作已累计的 pickup/aspirate/dispense 步骤。
        self._step_protocol_open = False
        if step_mode:
            if is_9320:
                self.step_mode = step_mode
            else:
                print("9300设备不支持 单点动作模式")
        self._unilabos_backend = PRCXI9300Backend(
            tablets_info, host, port, timeout, channel_num, axis, setup, debug, matrix_id, is_9320,
            pip_setting=self.pip_setting,
            protocol_version=protocol_version,
            reset_status_inverted=reset_status_inverted,
            wait_finish_timeout_s=wait_finish_timeout_s,
        )
        super().__init__(backend=self._unilabos_backend, deck=deck, simulator=simulator, channel_num=channel_num)
        self._first_transfer_done = False
        # backend 在做槽位反查时若拿不到 deck，需要回退到 handler.deck，这里建立反向引用
        self._unilabos_backend._handler = self

    @staticmethod
    def _get_slot_number(resource, deck: Optional[PRCXI9300Deck] = None) -> Optional[int]:
        """从 resource 的 unilabos_extra["update_resource_site"]（如 "T13"）或位置反算槽位号。"""
        return _get_slot_number(resource, deck=deck)

    def _matrix_id_has_value(self) -> bool:
        """当前 backend.matrix_id 是否已有有效值。"""
        matrix_id = str(getattr(self._unilabos_backend, "matrix_id", "") or "").strip()
        return bool(matrix_id)

    def _should_skip_runtime_position_recalc(self) -> bool:
        """是否跳过运行期位置重算（供各方法复用）。"""
        return (
            self.skip_position_recalc_when_matrix_exists
            and self._matrix_id_has_value()
        )

    def _top_level_consumable(self, resource):
        """从任意 PLR 资源沿 parent 向上找"放在 deck 上的那一层耗材"。"""
        if resource is None:
            return None
        cur = resource
        while cur is not None:
            parent = getattr(cur, "parent", None)
            if isinstance(parent, PRCXI9300Deck):
                return cur
            if parent is None:
                # 已到顶；若 cur 本身就是 deck，没有"耗材"层
                if isinstance(cur, PRCXI9300Deck):
                    return None
                return cur
            cur = parent
        return None

    def _attach_resources_to_deck_if_needed(self, items: Sequence[Resource]) -> None:
        """把通过 _resolve_to_plr_resources 拿回的"游离"耗材自动挂到 self.deck。

        - 已经在 PRCXI9300Deck 上（含 name 同名）的跳过；
        - 优先按 ``unilabos_extra.update_resource_site`` 的 Tn 解析槽位；
        - 否则交给 ``Deck.assign_child_resource`` 找空槽。
        - 任意失败仅打印告警，不中断主流程（backend 仍可走名字兜底）。
        """
        deck = getattr(self, "deck", None)
        if not isinstance(deck, PRCXI9300Deck):
            return
        existing_names = {getattr(c, "name", None) for c in deck.children}
        for item in items:
            top = self._top_level_consumable(item)
            if top is None or not isinstance(top, Resource):
                continue
            if isinstance(getattr(top, "parent", None), PRCXI9300Deck):
                continue
            top_name = getattr(top, "name", None)
            if top_name in existing_names:
                continue
            spot_idx: Optional[int] = None
            extra = getattr(top, "unilabos_extra", {}) or {}
            site = str(extra.get("update_resource_site", ""))
            if site:
                digits = "".join(c for c in site if c.isdigit())
                if digits:
                    # 槽位号 → 下标（兼容 prc_sites_config 的非连续编号）。
                    spot_idx = deck.slot_index(int(digits))
            try:
                deck.assign_child_resource(top, spot=spot_idx, reassign=False)
                existing_names.add(top_name)
            except Exception as e:
                print(f"[PRCXI] 自动挂载到 deck 失败: name={top_name}, site={site or '?'}, err={e}")

    @staticmethod
    def _is_success(res: Any) -> bool:
        """兼容 V04/v03 返回：``True`` 或 ``{"Success": True}`` 均视为成功。"""
        if res is True:
            return True
        if isinstance(res, dict):
            return bool(res.get("Success"))
        return False

    def _slot_prcxi_xy(self, number: int) -> Tuple[float, float]:
        """槽位在 PRCXI 机器坐标系的 (x, y) 参考点。

        有标定（``calibration_points`` → ``_slot_prcxi_positions``）时用标定值(A)；
        否则按 deck 该槽 site 足迹中心套用 ``plr_pos_to_prcxi`` 的无标定几何公式推算(C，近似)。
        """
        cal = self._slot_prcxi_positions.get(number)
        if cal is not None:
            return cal
        cx = cy = 0.0
        deck = self.deck
        if isinstance(deck, PRCXI9300Deck):
            try:
                loc = deck.get_slot_location(number)
                idx = deck.slot_index(number)
                site = deck.sites[idx] if idx is not None else None
                w = float((site or {}).get("size", {}).get("width", 0.0))
                h = float((site or {}).get("size", {}).get("height", 0.0))
                cx = loc.x + w / 2.0
                cy = loc.y + h / 2.0
            except Exception:
                cx = cy = 0.0
        prcxi_x = (self._x_axis_span - cx) * (1 + self.x_increase) + self.x_offset + self.xy_coupling * (self.deck_y - cy)
        prcxi_y = (self.deck_y - cy) * (1 + self.y_increase) + self.y_offset
        return (prcxi_x, prcxi_y)

    def _log_v04_board(self, matrix_id: str, tag: str = "") -> None:
        """拉取当前板位并打印每个 Detail 的坐标写入情况（排查坐标是否已下发）。"""
        api = self._unilabos_backend.api_client
        if not getattr(api, "is_v04", False):
            return
        try:
            board = api.matrix_by_id(matrix_id)
        except Exception as e:
            print(f"[PRCXI][v04][board {tag}] 拉取板位失败 matrix_id={matrix_id}: {e}")
            return
        if not isinstance(board, dict) or not board:
            print(f"[PRCXI][v04][board {tag}] matrix_id={matrix_id} 未拉到有效板位")
            return
        details = board.get("Details") or []
        summary = " ".join(
            f"T{d.get('Number')}:pip={len(d.get('PipettingPosList') or [])}"
            f",pos={'Y' if d.get('Position') else 'N'},grip={'Y' if d.get('gripperPos') else 'N'}"
            for d in details
        )
        print(f"[PRCXI][v04][board {tag}] matrix_id={matrix_id} details={len(details)} | {summary}")

    def _build_prc_sites_board(self, number_to_material: Dict[int, Dict[str, Any]]):
        """按 prc_sites_config 构建带物料的板位（Board）+ 兼容 MatrixInfo。

        - 布局（列主序/跨格/编号）来自 prc_sites_config，board_id 为 ``auto_board_{ms}``；
        - 每个 Detail 的 MaterialId/Volume 取自 ``number_to_material``（按板位号匹配）；
        - 点位仍由调用方通过 ``update_pipetting_position`` 单独下发（不放进 Board）。
        返回 ``(board, matrix_info)``。
        """
        board = prc_sites_to_board(
            getattr(self, "_row_nums", 4),
            getattr(self, "_column_nums", 6),
            self._prc_sites_config,
        )
        is_v04 = bool(getattr(self._unilabos_backend.api_client, "is_v04", False))
        for detail in board.details:
            mat = number_to_material.get(int(detail.number), {}) or {}
            # 按接口版本选 id：v04 用 id_v4，v03 用 uuid。
            detail.material_id = _pick_material_id(mat, is_v04)
            detail.volume = int(mat.get("Volume", 0) or 0)
        matrix_info = {
            "MatrixId": board.id,
            "MatrixName": board.name,
            "WorkTablets": [
                {"Number": d.number, "Code": d.name, "Material": number_to_material.get(int(d.number), {})}
                for d in board.details
            ],
        }
        return board, matrix_info

    def _match_and_create_matrix(self):
        """首次 transfer_liquid 时，根据 deck 上的 resource 自动匹配耗材并创建 WorkTabletMatrix。"""
        backend = self._unilabos_backend
        api = backend.api_client

        if backend.matrix_id:
            return

        material_list = api.get_all_materials()
        if not material_list:
            return

        # 按 materialEnum 分组: {enum_value: [material, ...]}
        material_dict = {}
        material_uuid_map = {}
        material_id_v4_map = {}
        for m in material_list:
            enum_key = m.get("materialEnum")
            material_dict.setdefault(enum_key, []).append(m)
            if m.get("uuid"):
                material_uuid_map[m["uuid"]] = m
            if m.get("id_v4"):
                material_id_v4_map[m["id_v4"]] = m

        work_tablets = []
        # 空闲槽位以「实际板位号」为准（兼容 prc_sites_config 的非连续编号）。
        if isinstance(self.deck, PRCXI9300Deck):
            slot_none = [int(s["number"]) for s in self.deck.sites]
        else:
            slot_none = [i for i in range(1, 17)]

        for child in self.deck.children:

            resource = child
            number = self._get_slot_number(resource, deck=self.deck)
            if number is None:
                continue

            # 如果 resource 已声明具体物料，优先精确匹配（V04 用 id_v4，v03 用 uuid）。
            if hasattr(resource, "_unilabos_state") and "Material" in getattr(resource, "_unilabos_state", {}):
                stored_material = resource._unilabos_state["Material"] or {}
                # V04：耗材主键是 id_v4，服务端没有 v03 uuid，必须靠 id_v4 命中。
                mat_id_v4 = stored_material.get("id_v4")
                if mat_id_v4 and mat_id_v4 in material_id_v4_map:
                    work_tablets.append({"Number": number, "Material": material_id_v4_map[mat_id_v4]})
                    if number in slot_none:
                        slot_none.remove(number)
                    continue
                mat_uuid = stored_material.get("uuid")
                if mat_uuid and mat_uuid in material_uuid_map:
                    work_tablets.append({"Number": number, "Material": material_uuid_map[mat_uuid]})
                    if number in slot_none:
                        slot_none.remove(number)
                    continue

            # 根据 resource 类型推断 materialEnum
            # MaterialEnum: Other=0, Tips=1, DeepWellPlate=2, PCRPlate=3, ELISAPlate=4, Reservoir=5, WasteBox=6
            expected_enum = None
            if isinstance(resource, TipRack):
                expected_enum = 1  # Tips
            elif isinstance(resource, Trash):
                expected_enum = 6  # WasteBox
            elif isinstance(resource, (PRCXI9300Plate, Plate)):
                expected_enum = None  # Plate 可能是 DeepWellPlate/PCRPlate/ELISAPlate，不限定


            # 根据 expected_enum 筛选候选耗材列表
            if expected_enum is not None:
                candidates = material_dict.get(expected_enum, [])
            else:
                # expected_enum 未确定时，搜索所有耗材
                candidates = material_list

            # 根据 children 个数和容量匹配最相似的耗材
            num_children = len(resource.children)
            child_max_volume = None
            if resource.children:
                first_child = resource.children[0]
                if hasattr(first_child, "max_volume") and first_child.max_volume is not None:
                    child_max_volume = first_child.max_volume

            best_material = None
            best_score = float("inf")

            for material in candidates:
                hole_count = (material.get("HoleRow", 0) or 0) * (material.get("HoleColum", 0) or 0)
                material_volume = material.get("Volume", 0) or 0

                # 孔数差异（高权重优先匹配孔数）
                hole_diff = abs(num_children - hole_count)
                # 容量差异（归一化）
                if child_max_volume is not None and material_volume > 0:
                    vol_diff = abs(child_max_volume - material_volume) / material_volume
                else:
                    vol_diff = 0

                score = hole_diff * 1000 + vol_diff
                if score < best_score:
                    best_score = score
                    best_material = material

            if best_material:
                work_tablets.append({"Number": number, "Material": best_material})
                if number in slot_none:
                    slot_none.remove(number)

        if not work_tablets:
            return

        # Number→Material 映射。
        default_material = {"uuid": "730067cf07ae43849ddf4034299030e9", "id_v4": "238c27e6-0ad7-4718-81cc-03f80b993de7", "Code": "q1", "Name": "废弃槽", "materialEnum": 0, "SupplyType": 1}
        is_v04 = bool(getattr(api, "is_v04", False))
        number_to_material: Dict[int, Dict[str, Any]] = {
            int(wt["Number"]): (wt.get("Material") or {}) for wt in work_tablets
        }
        # 仅 v03 给空槽兜底废弃槽物料；V04 下空槽保持为空，不自动放 238c27e6...。
        if not is_v04:
            for number in slot_none:
                number_to_material.setdefault(int(number), dict(default_material))

        # 有 prc_sites_config（9320 + v04）时：真实下发按 prc_sites 布局构建的板位
        # （列主序/跨格/auto_board_ 命名），并把匹配到的物料写入各 Detail；
        # 否则沿用原有 WorkTablets → worktablets_to_board 路径。
        use_prc_board = (
            bool(getattr(self, "_prc_sites_config", None))
            and getattr(backend, "is_9320", False)
            and getattr(api, "is_v04", False)
        )
        if use_prc_board:
            board, matrix_info = self._build_prc_sites_board(number_to_material)
            matrix_id = board.id
            res = api.add_board_v04(board)
            self._log_v04_board(matrix_id, tag="after-create")
        else:
            matrix_id = str(uuid.uuid4())
            # V04 下空槽用空物料（不放 238c27e6...），v03 才兜底废弃槽物料。
            empty_tablets = [
                {"Number": number, "Material": ({} if is_v04 else dict(default_material))}
                for number in slot_none
            ]
            matrix_info = {
                "MatrixId": matrix_id,
                "MatrixName": "matrix_" + str(time.time()),
                "WorkTablets": work_tablets + empty_tablets,
            }
            res = api.add_WorkTablet_Matrix(matrix_info)
        if self._is_success(res):
            backend.matrix_id = matrix_id
            backend.matrix_info = matrix_info

            # 重新计算所有槽位的位置（初始化时 deck 可能为空，此时才有资源）
            pipetting_positions = []
            claw_positions = []
            seen_numbers = set()
            # 各轴的 VolumeEnum 取自 pip_setting（left/right 的 vol）；缺省回退默认，避免下发 null。
            _ps = getattr(self, "pip_setting", None) or {}
            left_vol_enum = _to_volume_enum((_ps.get("left") or {}).get("vol"))
            right_vol_enum = _to_volume_enum((_ps.get("right") or {}).get("vol"))
            for child in self.deck.children:
                number = self._get_slot_number(child, deck=self.deck)

                if number is None:
                    continue
                seen_numbers.add(number)

                # 槽位机器坐标参考点：有标定→A(标定值)，否则→C(几何推算)。
                # 移液位对带孔板走 plr_pos_to_prcxi（同样 A/C 自动），因此无标定也能出移液坐标。
                slot_pos = self._slot_prcxi_xy(number)

                # 若 slot 上有 module/plate_adapter，下钻到其上承载的板(leaf)并取支撑层真实高度。
                leaf, support, support_layer = self._slot_plate_and_support(child)
                plate_h = self._recover_height(leaf)

                # 夹爪：物理基准 = 支撑层高度 + 板中心；加 claw 帧偏移后 clamp 到 max_z_claw（不截到
                # deck_z，否则 +offset 会把所有矮板都顶到 deck_z 导致夹爪高度对板高/支撑不敏感）。
                pos = self.plr_pos_to_prcxi(leaf, self.left_2_claw)
                pos.x = slot_pos[0] - child.get_size_x() / 2 + self.left_2_claw.x
                pos.y = slot_pos[1] - child.get_size_y() / 2 + self.left_2_claw.y
                pos.z = self.deck_z - (support + plate_h / 2.0) + self.left_2_claw.z
                claw_positions.append({"Number": number, "XPos": pos.x, "YPos": pos.y, "ZPos": max(min(pos.z, self.max_z_claw),0)})

                # 移液：以承载板的 A1 孔为目标（孔几何完好），再按支撑层高度抬高一层。
                # mouth=z='t'、bottom=z='b'；tip_height=0 求基准后再按左右枪头长度补偿。
                if getattr(leaf, "children", None):
                    well = leaf.children[0]
                    pip_pos = self.plr_pos_to_prcxi(well, tip_height=0.0)
                    z_mouth, z_bottom = self._pipetting_z_anchors(
                        well, leaf, support, support_layer
                    )
                else:
                    pip_pos = self.plr_pos_to_prcxi(leaf, tip_height=0.0)
                    pip_pos.x = slot_pos[0] - 40
                    pip_pos.y = slot_pos[1] - leaf.get_size_y() / 2
                    z_mouth, z_bottom = self._pipetting_z_anchors(
                        leaf, leaf, support, support_layer
                    )
                pip_bottom, pip_mouth, pip2_bottom, pip2_mouth = self._pipetting_z_from_base(
                    z_mouth, z_bottom
                )

                # trash 落枪头下探过深：整体抬高 _TRASH_Z_RAISE_MM（数值越小物理越高），
                # 左右轴 bottom + mouth 四个分量同抬，clamp 到 ≥ 0（高度不能小于 0）。
                if getattr(leaf, "category", "") == "trash" or getattr(child, "name", "") == "trash":
                    pip_bottom = max(pip_bottom - _TRASH_Z_RAISE_MM, 0.0)
                    pip_mouth = max(pip_mouth - _TRASH_Z_RAISE_MM, 0.0)
                    pip2_bottom = max(pip2_bottom - _TRASH_Z_RAISE_MM, 0.0)
                    pip2_mouth = max(pip2_mouth - _TRASH_Z_RAISE_MM, 0.0)

                pipetting_positions.append({
                    "Number": number,
                    "VolumeEnum": left_vol_enum,
                    "VolumeEnum2": right_vol_enum,
                    "XPos": pip_pos.x,
                    "YPos": pip_pos.y,
                    "ZPos": pip_bottom,
                    "bottleMouthPosition": pip_mouth,
                    "X2Pos": pip_pos.x + self.right_2_left.x,
                    "Y2Pos": pip_pos.y + self.right_2_left.y,
                    "Z2Pos": pip2_bottom,
                    "bottleMouthPosition2": pip2_mouth,
                })

            # 空 slot（无物料）也初始化点位：按默认 labware 足迹（标准板 128×86）+ 台面高度，
            # 镜像上面「无 children」分支的算法，保证每个已校准 slot 都有夹爪 + 移液位置。
            default_w = float(PRCXI9300Deck._DEFAULT_SITE_SIZE.get("width", 128.0))
            default_h = float(PRCXI9300Deck._DEFAULT_SITE_SIZE.get("height", 86.0))
            # 空槽也初始化点位：遍历全部板位号（有标定用标定，无标定用几何），
            # 兼容 prc_sites_config 的非连续/列主序编号。
            if isinstance(self.deck, PRCXI9300Deck):
                all_slot_numbers = [int(s["number"]) for s in self.deck.sites]
            else:
                all_slot_numbers = sorted(self._slot_prcxi_positions)
            for number in all_slot_numbers:
                if number in seen_numbers:
                    continue
                if isinstance(self.deck, PRCXI9300Deck):
                    idx = self.deck.slot_index(number)
                else:
                    idx = number - 1
                if idx is not None and self.deck._get_site_resource(idx) is not None:
                    continue
                slot_pos = self._slot_prcxi_xy(number)

                # 夹爪：台面高度（z=0 → prcxi_z=deck_z），按默认足迹居中。
                claw_z = self.deck_z + self.left_2_claw.z
                claw_x = slot_pos[0] - default_w / 2 + self.left_2_claw.x
                claw_y = slot_pos[1] - default_h / 2 + self.left_2_claw.y
                claw_positions.append({
                    "Number": number,
                    "XPos": min(max(0, claw_x), self._x_axis_span),
                    "YPos": min(max(0, claw_y), self.deck_y),
                    "ZPos": max(min(claw_z, self.max_z_claw), 0),
                })

                # 移液：台面高度下探 70（与「无 children」分支一致）；空槽无孔几何，mouth=bottom。
                pip_x = slot_pos[0] - 40
                pip_y = slot_pos[1] - default_h / 2
                z_base = self.deck_z - 70
                pip_bottom, pip_mouth, pip2_bottom, pip2_mouth = self._pipetting_z_from_base(
                    z_base, z_base
                )
                pipetting_positions.append({
                    "Number": number,
                    "VolumeEnum": left_vol_enum,
                    "VolumeEnum2": right_vol_enum,
                    "XPos": min(max(0, pip_x), self._x_axis_span),
                    "YPos": min(max(0, pip_y), self.deck_y),
                    "ZPos": pip_bottom,
                    "bottleMouthPosition": pip_mouth,
                    "X2Pos": pip_x + self.right_2_left.x,
                    "Y2Pos": pip_y + self.right_2_left.y,
                    "Z2Pos": pip2_bottom,
                    "bottleMouthPosition2": pip2_mouth,
                })

            if pipetting_positions:
                api.update_pipetting_position(matrix_id, pipetting_positions)
            # 更新 backend 中的 claw_positions
            backend.claw_positions = claw_positions

            if claw_positions:
                api.update_clamp_jaw_position(matrix_id, claw_positions)

            coord_mode = "A(标定)" if self._slot_prcxi_positions else "C(几何推算)"
            print(
                f"[PRCXI][v04] 坐标来源={coord_mode}；已下发 移液点位 {len(pipetting_positions)} 条、"
                f"夹爪点位 {len(claw_positions)} 条 (matrix_id={matrix_id})"
            )
            self._log_v04_board(matrix_id, tag="after-update")

            print(f"Auto-matched materials and created matrix: {matrix_id}")
        else:
            raise PRCXIError(f"Failed to create auto-matched matrix: {res.get('Message', 'Unknown error')}")

    def calibrate_from_points(
        self,
        calibration_points: Dict[str, List[List[float]]],
        labware_type: Optional[str] = "PRCXI_300ul_Tips",
    ):
        """从实测 PRCXI 机器坐标直接计算每个 slot 的 PRCXI 原点坐标。

        校准点是将参考物料放在各 slot 后，机器移至其 A1 位置所读取的
        PRCXI 坐标。通过 ``labware_type`` 创建临时实例，取 ``children[0]``
        （即 A1）的 location 作为偏移量，逆运算得 slot 原点。
        line_1~line_N 依次对应 T1~T4, T5~T8, ...

        Args:
            calibration_points: ``{"line_1": [[px, py], ...], ...}``。
                ``[0, 0]`` 表示该点无效，不计入。
            labware_type: prcxi_labware 中的工厂函数名（如 ``"PRCXI_300ul_Tips"``）。
                为 ``None`` 时 dx=dy=0，即校准点直接作为 slot 原点。
        """
        dx, dy = 0.0, 0.0
        if labware_type is not None:
            from . import prcxi_labware
            factory = getattr(prcxi_labware, labware_type)
            temp = factory("_calibration_ref")
            a1 = temp.children[0]
            dx, dy = a1.location.x + a1.get_size_x() / 2, a1.location.y + a1.get_size_y() / 2


        sorted_keys = sorted(
            calibration_points.keys(),
            key=lambda k: int("".join(c for c in k if c.isdigit()) or "0"),
        )

        slot_number = 0
        for key in sorted_keys:
            for pt in calibration_points[key]:
                slot_number += 1
                if isinstance(pt, (list, tuple)) and len(pt) >= 2 and not (pt[0] == 0 and pt[1] == 0):
                    self._slot_prcxi_positions[slot_number] = (
                        float(pt[0]) + dx,
                        float(pt[1]) + dy,
                    )

    def _find_slot_for_resource(self, resource: Resource) -> Optional[int]:
        """沿 parent 链向上找到 Deck 的直接子节点，返回其槽位号。"""
        current = resource
        while current is not None:
            if isinstance(current.parent, (PRCXI9300Deck, LiquidHandlerAbstract)):
                return self._get_slot_number(current, deck=self.deck)
            current = getattr(current, "parent", None)
        return self._get_slot_number(resource, deck=self.deck)

    def _slot_plate_and_support(self, deck_child):
        """返回 ``(leaf_plate_or_self, support_height, support_layer)``。

        若 ``deck_child`` 是 module / plate_adapter（``support_layer``），则下钻到其上
        承载的板（``leaf``），``support_height`` = 该 module/adapter 层的 ``get_size_z()``
        （用于把移液枪 / 夹爪高度抬高一个支撑层的高度，PRCXI 坐标系下即 prcxi_z 减去该高度）。
        若 ``deck_child`` 本身就是板（直接放在 deck 上），则 support_height=0、support_layer=None。
        """
        if isinstance(deck_child, (PRCXI9300ModuleSite, PlateAdapter)):
            support = self._recover_height(deck_child)
            leaf = deck_child.children[0] if getattr(deck_child, "children", None) else deck_child
            return leaf, support, deck_child
        return deck_child, 0.0, None

    def _recover_height(self, resource) -> float:
        """还原资源真实高度（mm）。云端反序列化的 deck 资源 ``get_size_z()`` 往往为 0，
        但其几何信息散落在别处，按以下顺序还原：

        1. ``get_size_z()`` 本身 >0 时直接用；
        2. 子物体（孔 / tip）顶面 ``max(child.location.z + child.get_size_z())`` —— 适用于板 / tip_rack；
        3. 按 ``model`` / ``unilabos_resource_class`` 在 prcxi_modules / prcxi_labware 工厂还原原始
           size_z —— 适用于 module / plate_adapter（其子物体是 size 同样为 0 的板，无法靠 extent 推断）。
        """
        if resource is None:
            return 0.0
        try:
            h = float(resource.get_size_z() or 0.0)
        except Exception:
            h = 0.0
        if h > 0:
            return h
        try:
            tops = []
            for c in (getattr(resource, "children", None) or []):
                lz = getattr(getattr(c, "location", None), "z", 0) or 0
                sz = c.get_size_z() if hasattr(c, "get_size_z") else 0
                top = (lz or 0) + (sz or 0)
                if top and top > 0:
                    tops.append(top)
            if tops:
                return float(max(tops))
        except Exception:
            pass
        model = getattr(resource, "model", None)
        if not model:
            extra = getattr(resource, "unilabos_extra", {}) or {}
            model = extra.get("unilabos_resource_class")
        if model:
            try:
                from . import prcxi_modules, prcxi_labware
            except Exception:
                prcxi_modules = prcxi_labware = None
            for _mod in (prcxi_modules, prcxi_labware):
                fac = getattr(_mod, str(model), None) if _mod is not None else None
                if callable(fac):
                    try:
                        return float(fac("_h_probe").get_size_z() or 0.0)
                    except Exception:
                        pass
        return 0.0

    def _axis_tip_heights(self) -> Tuple[float, float]:
        """返回 (left_tip_h, right_tip_h) mm。

        有 ``_tip_height_by_axis``（来自 pip_setting）时按轴取；否则左右都用
        ``self.tip_height``（legacy / 本次 tip_rack 高度）。
        """
        by_axis = getattr(self, "_tip_height_by_axis", None) or {}
        if by_axis:
            return float(by_axis.get("left", 0.0) or 0.0), float(by_axis.get("right", 0.0) or 0.0)
        h = float(getattr(self, "tip_height", 0.0) or 0.0)
        return h, h

    def _pipetting_z_from_base(
        self, prcxi_z_mouth_base: float, prcxi_z_bottom_base: float
    ) -> Tuple[float, float, float, float]:
        """由不含 tip 的 mouth/bottom 基准 prcxi z 生成左右轴 bottleBottom / bottleMouth。

        mouth 基准来自 ``get_absolute_location(..., z='t')``，bottom 来自 ``z='b'``。
        ``prcxi_z = deck_z - (abs_z + tip_h)`` ⇒ 在 tip_h=0 的基准上再减 tip_h。
        右轴额外叠加 ``right_2_left.z``。
        """
        tip_l, tip_r = self._axis_tip_heights()
        pip_bottom = max(min(prcxi_z_bottom_base - tip_l, self.deck_z), 0.0)
        pip_mouth = max(min(prcxi_z_mouth_base - tip_l, self.deck_z), 0.0)
        pip2_bottom = max(min(prcxi_z_bottom_base - tip_r + self.right_2_left.z, self.deck_z), 0.0)
        pip2_mouth = max(min(prcxi_z_mouth_base - tip_r + self.right_2_left.z, self.deck_z), 0.0)
        return pip_bottom, pip_mouth, pip2_bottom, pip2_mouth

    def _pipetting_z_anchors(
        self, target, leaf, support: float, support_layer
    ) -> Tuple[float, float]:
        """返回 (mouth_base, bottom_base)：孔口 z='t'、孔底 z='b'，均不含 tip、已减 support。"""
        z_mouth = self._support_free_prcxi_z(
            target, leaf, support, support_layer, tip_height=0.0, z_anchor="t"
        ) - support
        z_bottom = self._support_free_prcxi_z(
            target, leaf, support, support_layer, tip_height=0.0, z_anchor="b"
        ) - support
        return z_mouth, z_bottom

    def _support_free_prcxi_z(
        self,
        target,
        leaf,
        support,
        support_layer,
        offset_z: float = 0.0,
        tip_height: Optional[float] = None,
        z_anchor: Optional[str] = None,
    ) -> float:
        """计算 ``target`` 的「无支撑层」prcxi z（含 deck_z 顶面截断），供调用方再统一减去 support。

        直接用 ``get_absolute_location`` 而非 plr_pos_to_prcxi 的父链 hack，避免父链对
        deck 高度/支撑层高度的不一致累加。当板叠放在 module/adapter 顶面（``leaf.location.z != 0``）
        时，``get_absolute_location`` 已把支撑高度算进绝对坐标，这里先剔除，得到「板若直接放
        deck 表面」的基准 z。调用方随后统一 ``- support`` 抬高一层，再各自做 max_z 截断，
        保证支撑高度不被各级 clamp 吃掉，且无支撑物料行为与历史完全一致。

        ``tip_height`` 显式传入时覆盖 ``self.tip_height``（写板位时用 0 求基准，再按左右轴分别补偿）。
        ``z_anchor``：传给 ``get_absolute_location`` 的 z（``'t'`` 孔口 / ``'b'`` 孔底 / ``'c'`` 中心）；
        缺省时 TipSpot 用 ``'t'``，其余用 ``'c'``。
        """
        if z_anchor is None:
            z_pos = "t" if isinstance(target, TipSpot) else "c"
        else:
            z_pos = z_anchor
        tip_h = 0.0 if isinstance(target, TipSpot) else (
            float(self.tip_height or 0.0) if tip_height is None else float(tip_height)
        )
        abs_z = target.get_absolute_location(x="c", y="c", z=z_pos).z + tip_h
        leaf_loc = getattr(leaf, "location", None)
        if support_layer is not None and leaf_loc is not None and getattr(leaf_loc, "z", 0) != 0:
            abs_z -= support
        prcxi_z = self.deck_z - abs_z + offset_z
        return min(max(0, prcxi_z), self.deck_z)

    def plr_pos_to_prcxi(
        self,
        resource: Resource,
        resource_offset: Coordinate = Coordinate(0, 0, 0),
        offset: Coordinate = Coordinate(0, 0, 0),
        tip_height: Optional[float] = None,
    ):
        z_pos = 'c'
        tip_h = float(self.tip_height or 0.0) if tip_height is None else float(tip_height)
        if isinstance(resource, TipSpot):
            z_pos = 't'
            tip_h = 0.0
        resource_pos = resource.get_absolute_location(x="c",y="c",z=z_pos)
        x = resource_pos.x 
        y = resource_pos.y 
        z = resource_pos.z + tip_h

        parent = resource.parent
        res_z = resource.location.z
        while not isinstance(parent, LiquidHandlerAbstract) and (res_z == 0) and parent is not None:
            z += parent.get_size_z()
            res_z = parent.location.z
            parent = getattr(parent, "parent", None)

        slot_number = self._find_slot_for_resource(resource) if self._slot_prcxi_positions else None
        if slot_number is not None and slot_number in self._slot_prcxi_positions and self.calibration_labware_type is not None:
            slot_prcxi_x, slot_prcxi_y = self._slot_prcxi_positions[slot_number]
            prcxi_x = slot_prcxi_x - resource.location.x - resource.get_size_x() / 2
            prcxi_y = slot_prcxi_y - resource.location.y - resource.get_size_y() / 2
        else:
            prcxi_x = (self._x_axis_span - x)*(1+self.x_increase) + self.x_offset + self.xy_coupling * (self.deck_y - y)
            prcxi_y = (self.deck_y - y)*(1+self.y_increase) + self.y_offset

        prcxi_z = self.deck_z - z

        prcxi_x = min(max(0, prcxi_x+resource_offset.x),self._x_axis_span)
        prcxi_y = min(max(0, prcxi_y+resource_offset.y),self.deck_y)
        prcxi_z = min(max(0, prcxi_z+resource_offset.z),self.deck_z)

        return Coordinate(prcxi_x, prcxi_y, prcxi_z)

    def post_init(self, ros_node: BaseROS2DeviceNode):
        super().post_init(ros_node)
        self._unilabos_backend.post_init(ros_node)

    def set_liquid(self, wells: list[Well], liquid_names: list[str], volumes: list[float]) -> SetLiquidReturn:
        return super().set_liquid(wells, liquid_names, volumes)

    def set_liquid_from_plate(
        self,
        wells: Optional[Sequence[Union[Well, Dict[str, Any]]]] = None,
        liquid_names: Optional[list[str]] = None,
        volumes: Optional[list[float]] = None,
        *,
        plate: Optional[ResourceSlot] = None,
        well_names: Optional[list[str]] = None,
    ) -> SetLiquidFromPlateReturn:
        return super().set_liquid_from_plate(
            wells=wells,
            liquid_names=liquid_names,
            volumes=volumes,
            plate=plate,
            well_names=well_names,
        )

    def set_group(self, group_name: str, wells: List[Well], volumes: List[float]):
        return super().set_group(group_name, wells, volumes)

    async def transfer_group(self, source_group_name: str, target_group_name: str, unit_volume: float):
        return await super().transfer_group(source_group_name, target_group_name, unit_volume)

    async def create_protocol(
        self,
        protocol_name: str = "",
        protocol_description: str = "",
        protocol_version: str = "",
        protocol_author: str = "",
        protocol_date: str = "",
        protocol_type: str = "",
        none_keys: List[str] = [],
    ):
        self._unilabos_backend.create_protocol(protocol_name)

    async def run_protocol(self, protocol_id: str = None):
        return await self._unilabos_backend.run_protocol_async(protocol_id)

    async def remove_liquid(
        self,
        vols: List[float],
        sources: Sequence[Container],
        waste_liquid: Optional[Container] = None,
        *,
        use_channels: Optional[List[int]] = None,
        flow_rates: Optional[List[Optional[float]]] = None,
        offsets: Optional[List[Coordinate]] = None,
        liquid_height: Optional[List[Optional[float]]] = None,
        blow_out_air_volume: Optional[List[Optional[float]]] = None,
        spread: Optional[Literal["wide", "tight", "custom"]] = "wide",
        delays: Optional[List[int]] = None,
        is_96_well: Optional[bool] = False,
        top: Optional[List[float]] = None,
        none_keys: List[str] = [],
    ):
        return await super().remove_liquid(
            vols,
            sources,
            waste_liquid,
            use_channels=use_channels,
            flow_rates=flow_rates,
            offsets=offsets,
            liquid_height=liquid_height,
            blow_out_air_volume=blow_out_air_volume,
            spread=spread,
            delays=delays,
            is_96_well=is_96_well,
            top=top,
            none_keys=none_keys,
        )

    async def add_liquid(
        self,
        asp_vols: Union[List[float], float],
        dis_vols: Union[List[float], float],
        reagent_sources: Sequence[Container],
        targets: Sequence[Container],
        *,
        use_channels: Optional[List[int]] = None,
        flow_rates: Optional[List[Optional[float]]] = None,
        offsets: Optional[List[Coordinate]] = None,
        liquid_height: Optional[List[Optional[float]]] = None,
        blow_out_air_volume: Optional[List[Optional[float]]] = None,
        spread: Optional[Literal["wide", "tight", "custom"]] = "wide",
        is_96_well: bool = False,
        delays: Optional[List[int]] = None,
        mix_time: Optional[int] = None,
        mix_vol: Optional[int] = None,
        mix_rate: Optional[int] = None,
        mix_liquid_height: Optional[float] = None,
        none_keys: List[str] = [],
    ):
        return await super().add_liquid(
            asp_vols,
            dis_vols,
            reagent_sources,
            targets,
            use_channels=use_channels,
            flow_rates=flow_rates,
            offsets=offsets,
            liquid_height=liquid_height,
            blow_out_air_volume=blow_out_air_volume,
            spread=spread,
            is_96_well=is_96_well,
            delays=delays,
            mix_time=mix_time,
            mix_vol=mix_vol,
            mix_rate=mix_rate,
            mix_liquid_height=mix_liquid_height,
            none_keys=none_keys,
        )

    @staticmethod
    def _tip_rack_is_10ul_range(rack: TipRack) -> bool:
        """判断 tip 盒是否为 10µL 量程（对应右头）；优先用孔位上 prototype tip 的 maximal_volume。"""
        children = getattr(rack, "children", None) or []
        if children:
            spot = children[0]
            tr = getattr(spot, "tracker", None)
            tip = None
            if tr is not None:
                tip = getattr(tr, "_tip", None) or getattr(tr, "tip", None)
            if tip is None:
                tip = getattr(spot, "tip", None)
            mv = getattr(tip, "maximal_volume", None) if tip is not None else None
            if mv is not None:
                try:
                    return float(mv) <= 10.0
                except (TypeError, ValueError):
                    pass
        ident = f"{getattr(rack, 'model', '') or ''} {type(rack).__name__}".lower()
        return "10ul" in ident

    # P1 v5 — 扁平化 helper：实现位于 PLR-free 模块 ``prcxi.flatten_utils``，
    # 这里做薄包装以保留"helper 与 PRCXI 静态方法聚在一起"的设计语义
    # （详见 ``product_designs/protocol_convert/01-multi-channel-flatten.md`` §11.3）。
    # 拆分原因：本地 PLR 版本不匹配时也能跑 helper 单测（与 P10 v2 的
    # ``liquid_history.py`` 同策略）。
    _flatten_multi_channel_kwargs = staticmethod(_flatten_multi_channel_kwargs_impl)

    async def _cleanup_after_failed_transfer(self):
        """transfer_liquid 出错后尽力把 head 上残留 tip 丢到 trash 并清空 head 软件状态，
        避免下次 pickup 报 'Channel has tip' 且无需重启 edge。本方法自身不抛异常。"""
        try:
            mounted = self.get_mounted_tips()  # 各通道当前是否有 tip
        except Exception:
            mounted = []
        if any(t is not None for t in (mounted or [])):
            try:
                # step_mode 下需单独建一个清理 protocol 并执行（丢到 trash）
                if self.step_mode:
                    await self.create_protocol(f"cleanup_drop_tips{time.time()}")
                # use_channels=None → PLR 自动取「当前有 tip 的通道」丢到 trash
                await self.discard_tips()
                if self.step_mode:
                    await self.run_protocol()
            except Exception as _e:
                # 物理丢弃尽力而为：若错误发生在「构建步骤期」(机器尚未真正夹 tip)，设备丢空 tip 可能报错，忽略
                if hasattr(self, "_ros_node") and self._ros_node is not None:
                    try:
                        self._ros_node.lab_logger().warning(f"清理残留 tip 失败（已忽略）: {_e}")
                    except Exception:
                        pass
        # 兜底：无论物理丢弃成败，清空 PLR head 软件状态，保证下次 pickup 不再报 'Channel has tip'
        try:
            self.clear_head_state()
        except Exception:
            pass

    async def transfer_liquid(
        self,
        sources: Sequence[Container],
        targets: Sequence[Container],
        tip_racks: Sequence[TipRack],
        *,
        use_channels: Optional[List[int]] = None,
        asp_vols: Union[List[float], float],
        dis_vols: Union[List[float], float],
        asp_flow_rates: Optional[List[Optional[float]]] = None,
        dis_flow_rates: Optional[List[Optional[float]]] = None,
        offsets: Optional[List[Coordinate]] = None,
        touch_tip: bool = False,
        liquid_height: Optional[List[Optional[float]]] = None,
        blow_out_air_volume: Optional[List[Optional[float]]] = None,
        blow_out_air_volume_before: Optional[List[Optional[float]]] = None,
        spread: Literal["wide", "tight", "custom"] = "wide",
        is_96_well: bool = False,
        mix_stage: Optional[Literal["none", "before", "after", "both"]] = "none",
        mix_times: Optional[List[int]] = None,
        mix_vol: Optional[int] = None,
        mix_rate: Optional[int] = None,
        mix_liquid_height: Optional[float] = None,
        delays: Optional[List[int]] = None,
        pre_aspirate_from_target: Optional[float] = None,
        none_keys: List[str] = [],
    ) -> TransferLiquidReturn:
        # 必须在 _match_and_create_matrix 之前判断：
        # _match_and_create_matrix 会在首次 transfer 时创建 matrix_id，若在其后判断会恒为「有值」。
        skip_pipetting_position_recalc = self._should_skip_runtime_position_recalc()
        # 记录本次 transfer 是否需要 touch_tip，供 backend.dispense（native 靠壁）与
        # touch_tip() 重写（software 贴壁）读取；在 finally 中清除。
        self._touch_tip_pending = bool(touch_tip)
        if not self._first_transfer_done:
            self._match_and_create_matrix()
            self._first_transfer_done = True
        if self.step_mode:
            await self.create_protocol(f"transfer_liquid{time.time()}")

        _asp_list = asp_vols if isinstance(asp_vols, list) else [asp_vols]
        _dis_list = dis_vols if isinstance(dis_vols, list) else [dis_vols]
        sources = await self._resolve_to_plr_resources(sources)
        targets = await self._resolve_to_plr_resources(targets)
        tip_racks = list(await self._resolve_to_plr_resources(tip_racks))
        # 退化的空 transfer：workflow 偶发下发 sources/targets/tip_racks/asp_vols/dis_vols
        # 全为 None 的占位节点（runtime 实测：真实 transfer 前后各夹了一个全 None 的 goal）。
        # 这类「无源无目标」的传输本质是 no-op，直接返回空结果，避免整个 action 因后续
        # "empty tip_racks" 校验而崩溃。仍保留下方校验以覆盖「有源有目标但缺 tip_rack」的真实误配。
        if len(sources) == 0 and len(targets) == 0:
            if hasattr(self, "_ros_node") and self._ros_node is not None:
                try:
                    self._ros_node.lab_logger().warning(
                        "transfer_liquid 收到空的 sources/targets（占位 / no-op 节点），跳过本次传输。"
                    )
                except Exception:
                    pass
            return TransferLiquidReturn(sources=[], targets=[])
        if len(tip_racks) == 0:
            raise ValueError(
                "transfer_liquid requires at least one tip rack, but got empty tip_racks."
            )
        # 本次 transfer 独占 protocol 生命周期：置位后内部 mix 不再各自建/跑协议，避免其
        # create_protocol 清空本 transfer 已累计的取头/吸液/放液步骤（否则机器只执行 mix）。
        # 置于两个提前退出（空 transfer / 空 tip_rack）之后，确保只有真正执行的 96 / 非 96
        # 路径才置位，且它们各自的 finally 会复位，杜绝标志泄漏。
        if self.step_mode:
            self._step_protocol_open = True
        # 远端解析回来的 PLR 实例可能未挂到 self.deck，主动绑定一次，避免 backend 取 plate.parent==None
        self._attach_resources_to_deck_if_needed(list(sources) + list(targets) + list(tip_racks))
        if isinstance(tip_racks[0], TipRack):
            tip_rack = tip_racks[0]
        else:
            tip_rack = tip_racks[0].parent

        # === 96 孔整板模式 ===
        # is_96_well=True：选定 pip_setting 中 channels==96 的轴写入 backend._active_axis，
        # 设定该轴 tip 高度，跳过 8 通道扁平化 / 逐列展开，直接走抽象层的整板 96 头 API。
        if is_96_well:
            return await self._transfer_liquid_96well_route(
                sources,
                targets,
                tip_racks,
                tip_rack,
                skip_pipetting_position_recalc,
                use_channels=use_channels,
                asp_vols=asp_vols,
                dis_vols=dis_vols,
                asp_flow_rates=asp_flow_rates,
                dis_flow_rates=dis_flow_rates,
                offsets=offsets,
                touch_tip=touch_tip,
                liquid_height=liquid_height,
                blow_out_air_volume=blow_out_air_volume,
                spread=spread,
                mix_stage=mix_stage,
                mix_times=mix_times,
                mix_vol=mix_vol,
                mix_rate=mix_rate,
                mix_liquid_height=mix_liquid_height,
                delays=delays,
                pre_aspirate_from_target=pre_aspirate_from_target,
                none_keys=none_keys,
            )

        # === P1 v5：8 通道扁平化 ===
        # 设计文档：product_designs/protocol_convert/01-multi-channel-flatten.md
        #   §0   framework convention：8 通道 pipette 方向恒为 A~H column（governing rule）
        #   §11  v5 设计变更：抽象层去掉 fanout，PRCXI 子类内扁平化
        #   §13  length-8 → tile M（A~H channel column 复用 M 个目标列）
        # 触发条件：caller 传 use_channels=[0..7] 且当前 PRCXI 不是真 8 通道并行硬件。
        # 单头硬件（9300 / 9320）把 8 通道意图按列展开为 8 × M 次单通道顺序执行。
        _is_eight_channel_request = (
            isinstance(use_channels, (list, tuple))
            and len(use_channels) == 8
            and list(use_channels) == [0, 1, 2, 3, 4, 5, 6, 7]
        )

        # 选轴/扁平化判定：pip_setting 路由（通道优先、再看体积；channels 即真并行度）
        # vs. legacy（≤10µL→右单通道[1]、8 通道[0..7]按 has_true_8channel 扁平化）。
        _pip_setting = getattr(self, "pip_setting", None)
        if _pip_setting is not None:
            _n_req = 8 if _is_eight_channel_request else 1
            _all_vols = [float(v) for v in (_asp_list + _dis_list) if v is not None]
            _max_vol = max(_all_vols) if _all_vols else 0.0
            _sel_axis = _select_axis(_pip_setting, _n_req, _max_vol)
            _axis_ch = int(_pip_setting[_sel_axis]["channels"])
            # 多通道请求落到并行度不足的轴（典型：8 通道但体积超过多通道轴量程→右单通道）→ 扁平化。
            _flatten_8_to_1 = _n_req == 8 and _axis_ch < 8
        else:
            _sel_axis = None
            _axis_ch = None
            _flatten_8_to_1 = _is_eight_channel_request and not getattr(
                self, "has_true_8channel", False
            )

        # === [P-DBG] PRCXI use_channels 翻倍排查（候选 C）===
        # 51b9a5 协议未传 use_channels；进入 PRCXI 后小体积 head 切换会把它设为 [1]；
        # _flatten_8_to_1 应为 False。若 use_channels=[0..7] 或 _flatten_8_to_1=True → 命中候选 C。
        if hasattr(self, "_ros_node") and self._ros_node is not None:
            try:
                _src_names = [f"{getattr(s.parent, 'name', '?')}/{s.name}" for s in sources]
                _tgt_names = [f"{getattr(t.parent, 'name', '?')}/{t.name}" for t in targets]
                self._ros_node.lab_logger().info(
                    f"[P-DBG] prcxi.transfer_liquid handler={id(self):x} "
                    f"use_channels={use_channels} _flatten_8_to_1={_flatten_8_to_1} "
                    f"pip_setting={_pip_setting} sel_axis={_sel_axis} "
                    f"has_true_8channel={getattr(self, 'has_true_8channel', False)} "
                    f"asp_list_len={len(_asp_list)} dis_list_len={len(_dis_list)} "
                    f"n_sources={len(sources)} n_targets={len(targets)} "
                    f"sources={_src_names} targets={_tgt_names}"
                )
            except Exception as _e:
                self._ros_node.lab_logger().warning(f"[P-DBG] log failed: {_e}")

        if _flatten_8_to_1:
            flattened = self._flatten_multi_channel_kwargs(
                sources=sources,
                targets=targets,
                asp_vols=_asp_list,
                dis_vols=_dis_list,
                asp_flow_rates=asp_flow_rates,
                dis_flow_rates=dis_flow_rates,
                offsets=offsets,
                liquid_height=liquid_height,
                blow_out_air_volume=blow_out_air_volume,
                blow_out_air_volume_before=blow_out_air_volume_before,
                delays=delays,
                pre_aspirate_from_target=pre_aspirate_from_target,
            )
            sources = flattened["sources"]
            targets = flattened["targets"]
            asp_vols = flattened["asp_vols"]
            dis_vols = flattened["dis_vols"]
            asp_flow_rates = flattened["asp_flow_rates"]
            dis_flow_rates = flattened["dis_flow_rates"]
            offsets = flattened["offsets"]
            liquid_height = flattened["liquid_height"]
            blow_out_air_volume = flattened["blow_out_air_volume"]
            blow_out_air_volume_before = flattened["blow_out_air_volume_before"]
            delays = flattened["delays"]
            pre_aspirate_from_target = flattened["pre_aspirate_from_target"]
            if _pip_setting is None:
                # legacy：让下面的 small-vols heuristic 自由选 [0] / [1]
                use_channels = None
            # 扁平化后 _asp_list / _dis_list 已经是 8×M 长度的真实逐孔体积，
            # 此后的判定基于全量逐孔体积（与原 8 通道一致）。
            _asp_list = list(asp_vols)
            _dis_list = list(dis_vols)
        # === P1 v5 end ===

        if _pip_setting is not None:
            # 选定轴 → use_channels（新编号：左[0..7]/右[8..15]）。扁平化后为单通道。
            _n_final = 1 if _flatten_8_to_1 else min(_n_req, _axis_ch)
            use_channels = _axis_channel_list(_sel_axis, _n_final)
            # mix 体积按所选轴量程上限收口（避免在小量程轴上下发超量 mix）。
            _axis_vol = float(_pip_setting[_sel_axis]["vol"])
            if mix_vol is not None:
                mix_vol = max(min(mix_vol, _axis_vol), 0)
        else:
            # 小体积单通道 head 切换：仅当 caller 没显式指定多通道时才生效。
            # P1 v4 多通道协议（use_channels=[0..7]）即便体积 ≤ 10uL 也应保留 8 通道，
            # 避免把 dis_vols=[8.3]*8 这种「8 通道每孔 8.3uL」的展开退化为单通道串行。
            small_vols = all(v <= 10.0 for v in _asp_list) and all(v <= 10.0 for v in _dis_list)
            _explicit_multi = isinstance(use_channels, (list, tuple)) and len(use_channels) > 1
            if small_vols and self._tip_rack_is_10ul_range(tip_rack) and not _explicit_multi:
                use_channels = [1]
                mix_vol = max(min(mix_vol, 10), 0) if mix_vol is not None else None
        # tip 高度：有 pip_setting 时用所选轴量程对应枪头长度；否则用本次 tip_rack 孔高。
        if _pip_setting is not None:
            self.tip_height = float(
                (getattr(self, "_tip_height_by_axis", None) or {}).get(_sel_axis, 0.0) or 0.0
            )
        else:
            self.tip_height = tip_rack.children[0].get_size_z()
        # matrix_id 已有值时，跳过板位重算；仅在首次创建 matrix 后回写板位坐标。
        if not skip_pipetting_position_recalc:
            self._sync_pipetting_positions(sources, targets, tip_rack)


        # P1 v5（Q6=B）：扁平化路径下调 super 时临时关 liquids-keep，防跨孔同名物料潜在污染。
        # identity-keep（同一物理 well 反复抽，例如 reservoir）继续生效 —— 同一液池零污染。
        # 用 try/finally 保证函数返回（含异常）后恢复用户原始 config，影响仅限本次扁平化调用。
        # 详见 product_designs/protocol_convert/01-multi-channel-flatten.md §11.4b。
        _prev_tip_reuse = getattr(self, "_tip_reuse_by_liquid_name", True)
        try:
            if _flatten_8_to_1:
                self._tip_reuse_by_liquid_name = False
            res = await super().transfer_liquid(
                sources,
                targets,
                tip_racks,
                use_channels=use_channels,
                asp_vols=asp_vols,
                dis_vols=dis_vols,
                asp_flow_rates=asp_flow_rates,
                dis_flow_rates=dis_flow_rates,
                offsets=offsets,
                touch_tip=touch_tip,
                liquid_height=liquid_height,
                blow_out_air_volume=blow_out_air_volume,
                blow_out_air_volume_before=None,
                spread=spread,
                is_96_well=is_96_well,
                mix_stage=mix_stage,
                mix_times=mix_times,
                mix_vol=mix_vol,
                mix_rate=mix_rate,
                mix_liquid_height=mix_liquid_height,
                delays=delays,
                pre_aspirate_from_target=pre_aspirate_from_target,
                none_keys=none_keys,
            )
            if self.step_mode:
                await self.run_protocol()
            return res
        except Exception:
            # 中途失败（构建期 super().transfer_liquid 或执行期 run_protocol）：清理残留 tip +
            # 清 head 软件状态，下次 transfer_liquid 无需重启 edge 即可重开。
            await self._cleanup_after_failed_transfer()
            raise
        finally:
            if _flatten_8_to_1:
                self._tip_reuse_by_liquid_name = _prev_tip_reuse
            self._touch_tip_pending = False
            self._step_protocol_open = False

    def _sync_pipetting_positions(self, sources, targets, tip_rack):
        """回写本次 transfer 涉及到的所有板位（source / target / tip_rack）的移液坐标。

        P2 v2：跨板 transfer_liquid 场景下 sources / targets 列表里可能引用多个 plate
        （v1 旧实现只取 [0] 会漏掉 slot 3/5/6 的位置同步）。这里遍历所有 source/target
        的 parent plate，按首次出现顺序去重——既保证跨板都能 update_pipetting_position，
        又避免同板多孔重复发送。详见 02-cross-slot-merge.md §3.3.2 / §9.5 step 5。
        （非 96 单/8 通道路径与 96 整板路径共用此逻辑。）
        """
        change_slots = []
        seen_plates = set()

        def _push_unique_plate(plate_obj):
            if plate_obj is None:
                return
            pname = getattr(plate_obj, "name", None) or id(plate_obj)
            if pname in seen_plates:
                return
            seen_plates.add(pname)
            change_slots.append(plate_obj)

        for src in sources:
            _push_unique_plate(getattr(src, "parent", None))
        for tgt in targets:
            _push_unique_plate(getattr(tgt, "parent", None))
        _push_unique_plate(tip_rack)

        change_slots_positions = []
        for slot in change_slots:
            number = self._get_slot_number(slot, deck=self.deck)

            well = slot.children[0]
            # 板叠放在 module/plate_adapter 上时，移液头按「无支撑基准 - support」抬高一层；
            # support 取支撑层真实高度（云端反序列化的 get_size_z 常为 0，需 _recover_height 还原）。
            slot_parent = getattr(slot, "parent", None)
            if isinstance(slot_parent, (PRCXI9300ModuleSite, PlateAdapter)):
                support = self._recover_height(slot_parent)
                support_layer = slot_parent
            else:
                support, support_layer = 0.0, None
            pip_pos = self.plr_pos_to_prcxi(well, tip_height=0.0)
            # 孔口 z='t'、孔底 z='b'，再按左右 tip 长度分别补偿。
            z_mouth, z_bottom = self._pipetting_z_anchors(
                well, slot, support, support_layer
            )
            pip_bottom, pip_mouth, pip2_bottom, pip2_mouth = self._pipetting_z_from_base(
                z_mouth, z_bottom
            )
            _ps = getattr(self, "pip_setting", None) or {}
            left_vol_enum = _to_volume_enum((_ps.get("left") or {}).get("vol"))
            right_vol_enum = _to_volume_enum((_ps.get("right") or {}).get("vol"))

            change_slots_positions.append({
                "Number": number,
                "VolumeEnum": left_vol_enum,
                "VolumeEnum2": right_vol_enum,
                "XPos": pip_pos.x,
                "YPos": pip_pos.y,
                "ZPos": pip_bottom,
                "bottleMouthPosition": pip_mouth,
                "X2Pos": pip_pos.x + self.right_2_left.x,
                "Y2Pos": pip_pos.y + self.right_2_left.y,
                "Z2Pos": pip2_bottom,
                "bottleMouthPosition2": pip2_mouth,
            })
        if change_slots_positions:
            self._unilabos_backend.api_client.update_pipetting_position(
                self._unilabos_backend.matrix_id, change_slots_positions
            )

    def _select_96well_axis(self) -> str:
        """从 ``pip_setting`` 里选出 ``channels == 96`` 的轴，返回 ``"left"`` / ``"right"``。

        96 整板模式**必须**有一个 96 通道轴配置；未配置时直接抛错，不静默降级。
        """
        _pip_setting = getattr(self, "pip_setting", None)
        if not _pip_setting:
            raise ValueError(
                "96 孔整板模式需要在 pip_setting 中配置一个 channels==96 的轴，"
                "但当前未配置 pip_setting。"
            )
        for axis_key in ("left", "right"):
            cfg = _pip_setting.get(axis_key) or {}
            try:
                if int(cfg.get("channels", 0)) == 96:
                    return axis_key
            except (TypeError, ValueError):
                continue
        raise ValueError(
            "96 孔整板模式需要在 pip_setting 中配置一个 channels==96 的轴"
            f"（当前 pip_setting={_pip_setting!r}）。"
        )

    async def _transfer_liquid_96well_route(
        self,
        sources: Sequence[Container],
        targets: Sequence[Container],
        tip_racks: Sequence[TipRack],
        tip_rack: TipRack,
        skip_pipetting_position_recalc: bool,
        *,
        use_channels: Optional[List[int]] = None,
        asp_vols: Union[List[float], float],
        dis_vols: Union[List[float], float],
        asp_flow_rates: Optional[List[Optional[float]]] = None,
        dis_flow_rates: Optional[List[Optional[float]]] = None,
        offsets: Optional[List[Coordinate]] = None,
        touch_tip: bool = False,
        liquid_height: Optional[List[Optional[float]]] = None,
        blow_out_air_volume: Optional[List[Optional[float]]] = None,
        spread: Literal["wide", "tight", "custom"] = "wide",
        mix_stage: Optional[Literal["none", "before", "after", "both"]] = "none",
        mix_times: Optional[List[int]] = None,
        mix_vol: Optional[int] = None,
        mix_rate: Optional[int] = None,
        mix_liquid_height: Optional[float] = None,
        delays: Optional[List[int]] = None,
        pre_aspirate_from_target: Optional[float] = None,
        none_keys: List[str] = [],
    ) -> TransferLiquidReturn:
        """96 孔整板转移路由：选定 96 通道轴 → 板位坐标同步 → 走抽象层整板 96 头 API。

        与非 96 路径共用 ``_sync_pipetting_positions`` 做板位坐标回写；不做 8 通道扁平化 /
        逐列展开。轴信息通过 ``backend._active_axis`` 传给 backend 的 ``*96`` 方法。
        """
        axis96 = self._select_96well_axis()
        # 写入 backend 选定轴（"Left"/"Right"），供 backend *96 方法下发整板指令时判轴。
        self._unilabos_backend._active_axis = "Left" if axis96 == "left" else "Right"
        # tip 高度：按所选 96 轴量程对应枪头长度（与非 96 路径 pip_setting 分支一致）。
        self.tip_height = float(
            (getattr(self, "_tip_height_by_axis", None) or {}).get(axis96, 0.0) or 0.0
        )
        # 整板同样需要板位坐标：首次创建 matrix 后回写 source / target / tip_rack 的坐标。
        if not skip_pipetting_position_recalc:
            self._sync_pipetting_positions(sources, targets, tip_rack)
        try:
            res = await super().transfer_liquid(
                sources,
                targets,
                tip_racks,
                use_channels=use_channels,
                asp_vols=asp_vols,
                dis_vols=dis_vols,
                asp_flow_rates=asp_flow_rates,
                dis_flow_rates=dis_flow_rates,
                offsets=offsets,
                touch_tip=touch_tip,
                liquid_height=liquid_height,
                blow_out_air_volume=blow_out_air_volume,
                blow_out_air_volume_before=None,
                spread=spread,
                is_96_well=True,
                mix_stage=mix_stage,
                mix_times=mix_times,
                mix_vol=mix_vol,
                mix_rate=mix_rate,
                mix_liquid_height=mix_liquid_height,
                delays=delays,
                pre_aspirate_from_target=pre_aspirate_from_target,
                none_keys=none_keys,
            )
            if self.step_mode:
                await self.run_protocol()
            return res
        except Exception:
            # 中途失败：清理残留 tip + 清 head 软件状态，下次 transfer 无需重启 edge。
            await self._cleanup_after_failed_transfer()
            raise
        finally:
            self._touch_tip_pending = False
            self._step_protocol_open = False

    async def custom_delay(self, seconds=0, msg=None):
        return await super().custom_delay(seconds, msg)

    async def touch_tip(self, targets: Sequence[Container]):
        # 仅当本次 transfer 勾选 touch_tip 且模式包含软件式(software/both)时，
        # 才执行抽象层的「孔内左右壁各做一次 0 体积 aspirate」贴壁；
        # native 模式走 dispense 的放液后靠壁（见 backend.dispense），此处不动。
        if self._touch_tip_pending and self.touch_tip_mode in ("software", "both"):
            return await super().touch_tip(targets)
        return None

    def _route_axis_and_channels(self, use_channels):
        """pip_setting 路由：从 use_channels 推轴写入 ``backend._active_axis``，返回 PLR 合法的
        0-based 通道。右轴 ``[8..15]`` → 减 8 为 ``[0..7]`` 并置 ``Right``；左轴 ``[0..7]`` 原样
        并置 ``Left``。未配置 pip_setting 或空入参 → 原样返回（legacy 行为不变）。

        说明：右轴下标 ``[8..15]`` 仅作设备内部的「选轴意图」信号，绝不透传给 PLR
        （PLR 只接受 ``0..channel_num-1`` 且会 ``head[channel]`` 索引）；轴信息改由
        ``_active_axis`` 传给 backend。
        """
        if getattr(self, "pip_setting", None) is None or not use_channels:
            return use_channels
        chans = list(use_channels)
        axis = _axis_from_channels_util(chans)  # "Left"/"Right"（跨段/越界会抛错）
        self._unilabos_backend._active_axis = axis
        if axis == "Right":
            return [c - _RIGHT_CHANNEL_BASE for c in chans]  # [8..15] -> [0..7]
        return chans

    async def mix(
        self,
        targets: Sequence[Container],
        mix_time: int = None,
        mix_vol: Optional[int] = None,
        height_to_bottom: Optional[float] = None,
        offsets: Optional[Coordinate] = None,
        mix_rate: Optional[float] = None,
        none_keys: List[str] = [],
        use_channels: Optional[List[int]] = [0],
    ):
        use_channels = self._route_axis_and_channels(use_channels)
        # 仅当 mix 是顶层动作（无外层复合动作打开的 protocol）时，才自建/自跑协议。
        # 若已被 transfer_liquid 等复合动作打开 protocol，则只 append，交由复合动作统一下发。
        _own_protocol = self.step_mode and not getattr(self, "_step_protocol_open", False)
        if _own_protocol:
            await self.create_protocol(f"mix{time.time()}")
        res = await self._unilabos_backend.mix(
            targets, mix_time, mix_vol, height_to_bottom, offsets, mix_rate, none_keys, use_channels
        )
        if _own_protocol:
            await self.run_protocol()
        return res

    def iter_tips(self, tip_racks: Sequence[TipRack]) -> Iterator[Resource]:
        return super().iter_tips(tip_racks)

    async def pick_up_tips(
        self,
        tip_spots: List[TipSpot],
        use_channels: Optional[List[int]] = None,
        offsets: Optional[List[Coordinate]] = None,
        **backend_kwargs,
    ):
        use_channels = self._route_axis_and_channels(use_channels)
        return await super().pick_up_tips(tip_spots, use_channels, offsets, **backend_kwargs)

    async def aspirate(
        self,
        resources: Sequence[Container],
        vols: List[float],
        use_channels: Optional[List[int]] = None,
        flow_rates: Optional[List[Optional[float]]] = None,
        offsets: Optional[List[Coordinate]] = None,
        liquid_height: Optional[List[Optional[float]]] = None,
        blow_out_air_volume: Optional[List[Optional[float]]] = None,
        spread: Literal["wide", "tight", "custom"] = "wide",
        **backend_kwargs,
    ):
        use_channels = self._route_axis_and_channels(use_channels)
        return await super().aspirate(
            resources,
            vols,
            use_channels,
            flow_rates,
            offsets,
            liquid_height,
            blow_out_air_volume,
            spread,
            **backend_kwargs,
        )

    async def drop_tips(
        self,
        tip_spots: Sequence[Union[TipSpot, Trash]],
        use_channels: Optional[List[int]] = None,
        offsets: Optional[List[Coordinate]] = None,
        allow_nonzero_volume: bool = False,
        **backend_kwargs,
    ):
        # 注意：此处**不**做 _route_axis_and_channels 路由。drop_tips 在转移流程里仅作为
        # PLR ``discard_tips → self.drop_tips`` 的回调进入（见 PLR liquid_handler.discard_tips），
        # 此时 use_channels 已被上游 ``discard_tips`` override 翻译为 0-based [0..7]、
        # ``backend._active_axis`` 也已置好。若在此再次路由，[0..7] 会被误判为左轴而把
        # ``_active_axis`` 覆写成 Left（导致右轴转移的 UnLoad 走错轴）。
        return await super().drop_tips(tip_spots, use_channels, offsets, allow_nonzero_volume, **backend_kwargs)

    async def dispense(
        self,
        resources: Sequence[Container],
        vols: List[float],
        use_channels: Optional[List[int]] = None,
        flow_rates: Optional[List[Optional[float]]] = None,
        offsets: Optional[List[Coordinate]] = None,
        liquid_height: Optional[List[Optional[float]]] = None,
        blow_out_air_volume: Optional[List[Optional[float]]] = None,
        spread: Literal["wide", "tight", "custom"] = "wide",
        **backend_kwargs,
    ):
        use_channels = self._route_axis_and_channels(use_channels)
        return await super().dispense(
            resources,
            vols,
            use_channels,
            flow_rates,
            offsets,
            liquid_height,
            blow_out_air_volume,
            spread,
            **backend_kwargs,
        )

    async def discard_tips(
        self,
        use_channels: Optional[List[int]] = None,
        allow_nonzero_volume: bool = True,
        offsets: Optional[List[Coordinate]] = None,
        **backend_kwargs,
    ):
        use_channels = self._route_axis_and_channels(use_channels)
        return await super().discard_tips(use_channels, allow_nonzero_volume, offsets, **backend_kwargs)

    def set_tiprack(self, tip_racks: Sequence[TipRack]):
        super().set_tiprack(tip_racks)

    async def move_to(self, well: Well, dis_to_top: float = 0, channel: int = 0):
        return await super().move_to(well, dis_to_top, channel)

    async def shaker_action(self, time: int, module_no: int, amplitude: int, is_wait: bool):
        return await self._unilabos_backend.shaker_action(time, module_no, amplitude, is_wait)

    async def shaking_incubation_action(self, time: int, module_no: int, amplitude: int, is_wait: bool, temperature: int):
        return await self._unilabos_backend.shaking_incubation_action(time, module_no, amplitude, is_wait, temperature)

    async def magnetic_action(self, time: int, module_no: int, height: int, is_wait: bool):
        return await self._unilabos_backend.magnetic_action(time, module_no, height, is_wait)

    async def heater_action(self, temperature: float, time: int):
        return await self._unilabos_backend.heater_action(temperature, time)

    async def move_plate(
        self,
        plate: List[ResourceSlot],
        to: int,
        intermediate_locations: Optional[List[Coordinate]] = None,
        pickup_offset: Coordinate = Coordinate.zero(),
        destination_offset: Coordinate = Coordinate.zero(),
        drop_direction: GripDirection = GripDirection.FRONT,
        pickup_direction: GripDirection = GripDirection.FRONT,
        pickup_distance_from_top: float = 13.2 - 3.33,
        hierarchy: int = 1,
        force: int = 1,
        **backend_kwargs,
    ):
        """把 ``plate`` 搬到 ``to`` 号 slot。

        ``to`` 现在是目标 **slot 号（int）**，不再要求传 Resource：
        - 取板仍沿用移液那套逻辑，从 ``plate`` 物料反推它当前所在 slot；
        - 放板按 ``to`` 号位下发 pick+drop；
        - 放置后把 ``plate`` 在资源树里 reparent 到目标 slot；若该 slot 上有
          plate_adapter 或 module，则 plate 最终挂到该 adapter/module 上，并同步更新物料。

        ``hierarchy``：夹爪夹取/放下高度档位，默认 1。
        ``force``：MvKit 夹持力，默认 1。

        因 pylabrobot 的 ``move_plate/move_resource`` 需要 ``to`` 是 Resource/Coordinate
        来做坐标计算与 reparent，``to:int`` 时不再委托父类，由本方法直接驱动 backend +
        手动 reparent。
        """
        # 注册 schema 中 plate 为「资源数组」（与 transfer_liquid 的 sources 一致，便于网页选取），
        # 运行期解析回来可能是单元素 list；这里统一取首个 Plate。
        if isinstance(plate, (list, tuple)):
            if not plate:
                raise ValueError("move_plate 需要一个 plate，但收到空列表")
            plate = plate[0]

        # 向后兼容：仍允许传 Resource（反推槽位号）。
        if not isinstance(to, int):
            to = self._unilabos_backend._deck_plate_slot_no(to, getattr(to, "parent", None))

        # 确保 plate 已挂到 deck，并从 plate 反推当前（源）slot。
        self._attach_resources_to_deck_if_needed([plate])
        src_slot = self._unilabos_backend._deck_plate_slot_no(plate, getattr(plate, "parent", None))
        if self.step_mode:
            await self.create_protocol(f"move_plate{time.time()}")
        # 下发硬件 pick+drop（simulator 模式只更新物料，不产生硬件步骤）。
        step = None
        if not self._simulator:
            hierarchy = int(backend_kwargs.get("hierarchy", hierarchy))
            force = int(backend_kwargs.get("force", force))
            pick_step = await self._unilabos_backend.pick_up_resource(
                None, source_plate_number=src_slot, hierarchy=hierarchy, force=force
            )
            drop_step = await self._unilabos_backend.drop_resource(
                None, target_plate_number=to, hierarchy=hierarchy, force=force
            )
            step = [pick_step, drop_step]

        # 更新物料：把 plate reparent 到目标 slot；若目标 slot 上有 plate_adapter/module 则挂到其上。
        deck = self.deck
        dst_resource = None
        if isinstance(deck, PRCXI9300Deck):
            try:
                dst_resource = deck._get_site_resource(to - 1)
            except Exception:
                dst_resource = None

        # 入参 plate 可能与 deck 树里的实例不是同一对象（远端反序列化），但同名。pylabrobot
        # 的 assign_child_resource 会按 root 全树做命名查重，若直接挂入参 plate 而旧的同名实例
        # 仍在树上，会抛 "already assigned to deck"。这里统一按名字定位树内真实实例并搬动它。
        target_plate = plate
        if isinstance(deck, PRCXI9300Deck):
            plate_name = getattr(plate, "name", None)
            if plate_name is not None:
                stack = list(deck.children)
                while stack:
                    node = stack.pop()
                    if getattr(node, "name", None) == plate_name:
                        target_plate = node
                        break
                    stack.extend(getattr(node, "children", None) or [])

        old_parent = getattr(target_plate, "parent", None)
        if old_parent is not None and old_parent is not dst_resource:
            try:
                old_parent.unassign_child_resource(target_plate)
            except Exception:
                pass
        if isinstance(dst_resource, (PlateAdapter, PRCXI9300ModuleSite)):
            # 已经在目标 module/adapter 下则无需重复挂（否则触发命名查重报错）。
            if getattr(target_plate, "parent", None) is not dst_resource:
                dst_resource.assign_child_resource(target_plate)
        elif isinstance(deck, PRCXI9300Deck):
            deck.assign_child_at_slot(target_plate, to, reassign=True)
        # 同步槽位标记，保证后续 _get_slot_number 反推一致。
        extra = getattr(target_plate, "unilabos_extra", None)
        if isinstance(extra, dict):
            extra["update_resource_site"] = f"T{to}"

        if self.step_mode and step is not None:
            await self.run_protocol()
        return step

    async def reset(self) -> bool:
        """复位设备（各轴回初始位置），阻塞等待复位完成。

        供注册表 / 前后端调用；真正实现委托给 backend。debug / simulator 模式下
        不触碰硬件，直接返回 True。
        """
        if self._unilabos_backend.debug or self._simulator:
            return True
        return await self._unilabos_backend.reset()

    async def pause(self) -> bool:
        """暂停当前方案执行。

        供注册表 / 前后端调用；真正实现委托给 backend。debug / simulator 模式下
        不触碰硬件，直接返回 True。
        """
        if self._unilabos_backend.debug or self._simulator:
            return True
        return await self._unilabos_backend.pause()

    async def resume(self) -> bool:
        """继续已暂停的方案执行。

        供注册表 / 前后端调用；真正实现委托给 backend。debug / simulator 模式下
        不触碰硬件，直接返回 True。
        """
        if self._unilabos_backend.debug or self._simulator:
            return True
        return await self._unilabos_backend.resume()


class PRCXI9300Backend(LiquidHandlerBackend):
    """PRCXI 9300 的后端实现，继承自 LiquidHandlerBackend。

    该类提供了与 PRCXI 9300 设备进行通信的基本方法，包括方案管理、自动化控制、运行状态查询等。
    """

    _num_channels = 8  # 默认通道数为 8
    _is_reset_ok = False
    _ros_node: BaseROS2DeviceNode
    _handler: Optional["PRCXI9300Handler"] = None  # 由 PRCXI9300Handler.__init__ 注入

    @property
    def is_reset_ok(self) -> bool:
        """设备是否可执行流程（已完成复位且当前不在复位中）。"""
        if self.debug:
            return True
        # GetResetStatus 语义：true=复位中。只要仍在复位中，就标记为未就绪。
        in_reset = self.api_client.get_reset_status()
        if in_reset:
            self._is_reset_ok = False
        return self._is_reset_ok

    @property
    def step_progress(self) -> List[int]:
        """当前方案步骤进度 ``[已完成步骤, 总步骤]``（仿 ``is_reset_ok`` 的读写模式）。

        这是全系统**唯一**发 RPC 并回写步骤缓存的地方：由 ``state_list`` datakey 的
        getter 经 PropertyPublisher 定时器驱动。所有 ``wait_for_finish`` 方法只读此缓存。
        """
        if self.debug:
            return [0, 0]
        snap = self.api_client.refresh_step_snapshot()
        prog = snap.get("progress", [0, 0])
        return [int(prog[0]), int(prog[1])]

    matrix_info: MatrixInfo
    protocol_name: str
    steps_todo_list = []

    def __init__(
        self,
        tablets_info: list[WorkTablets],
        host: str = "127.0.0.1",
        port: int = 9999,
        timeout: float = 10.0,
        channel_num: int = 8,
        axis: str = "Left",
        setup=True,
        debug=False,
        matrix_id="",
        is_9320=False,
        pip_setting: Optional[Dict[str, Dict[str, Any]]] = None,
        protocol_version: Literal["v03", "v04"] = "v04",
        reset_status_inverted: Optional[bool] = None,
        reset_timeout: float = 120.0,
        wait_finish_timeout_s: Optional[float] = None,
    ) -> None:
        super().__init__()
        # 声明 96 头能力：PRCXI 通过“96 通道轴”整板移液（is_96_well=True）。
        # PLR ``LiquidHandler.setup()`` 仅在 ``backend.head96_installed`` 为真时才构建 96 孔
        # TipTracker（head96 字典）；否则 head96={}，调用 pick_up_tips96/aspirate96 会 KeyError:0。
        # 未配置 96 轴时也无害：整板路由 ``_select_96well_axis`` 会在触达 PLR 前给出清晰报错。
        self._head96_installed = True
        self.tablets_info = tablets_info
        self.matrix_id = matrix_id
        self.protocol_version = PRCXI9300Api._normalize_protocol_version(protocol_version)
        self.api_client = PRCXI9300Api(
            host, port, timeout, axis, debug, is_9320,
            protocol_version=self.protocol_version,
            reset_status_inverted=reset_status_inverted,
        )
        self.host, self.port, self.timeout = host, port, timeout
        self._num_channels = channel_num
        setup = _coerce_bool(setup, default=True)
        self._execute_setup = setup
        # setup=False 表示由外部流程托管复位，不应被 run_protocol 的 reset 门禁阻塞。
        # 仍保留 is_reset_ok 对“当前是否处于复位中”的实时拦截（GetResetStatus=true 时会置 False）。
        self._is_reset_ok = not bool(setup)
        self.debug = debug
        self.axis = "Left"
        self.is_9320 = is_9320
        # setup 复位等待超时上限（秒），防止 GetResetStatus 语义异常导致死循环（决策点 C）。
        self.reset_timeout = float(reset_timeout)
        # 方案执行总超时（秒）：默认 None=不设总超时，避免提前终止；仅显式配置时生效。
        self.wait_finish_timeout_s = self._normalize_wait_finish_timeout(wait_finish_timeout_s)
        # 枪头轴配置（由 PRCXI9300Handler 透传）。None → legacy [0]→Left/[1]→Right。
        self.pip_setting: Optional[Dict[str, Dict[str, Any]]] = pip_setting
        # 当前操作选定的物理轴（"Left"/"Right"），由设备层 op override 在调用前写入。
        # pip_setting 模式下 backend 凭此判轴（而非解码通道下标），避免把右轴下标 [8..15]
        # 透传给 PLR（PLR 只接受 0..channel_num-1）。
        self._active_axis: Optional[str] = None

    @staticmethod
    def _normalize_wait_finish_timeout(value: Optional[float]) -> Optional[float]:
        """把 wait_for_finish 超时配置规范化；None/<=0 表示不设总超时（不会提前终止）。"""
        if value is None or value == "":
            return None
        try:
            timeout_s = float(value)
        except (TypeError, ValueError):
            print(f"[PRCXI][WARN] wait_finish_timeout_s 配置非法（{value!r}），回退为不设超时。")
            return None
        return timeout_s if timeout_s > 0 else None

    def _resolve_deck(self, plate, deck=None) -> Optional["PRCXI9300Deck"]:
        """定位 plate 所属的 PRCXI9300Deck：按 deck 入参 → plate 的祖先链 → handler.deck 顺序回退。"""
        if isinstance(deck, PRCXI9300Deck):
            return deck
        cur = plate
        while cur is not None:
            if isinstance(cur, PRCXI9300Deck):
                return cur
            cur = getattr(cur, "parent", None)
        if self._handler is not None:
            handler_deck = getattr(self._handler, "deck", None)
            if isinstance(handler_deck, PRCXI9300Deck):
                return handler_deck
        return None

    def _deck_plate_slot_no(self, plate, deck=None) -> int:
        """台面板位槽号（1–16）。

        plate 可能并非直接挂在 slot，而是嵌套在 slot 上的 plate_adapter / module 之下
        （资源树 deck -> module -> plate）。此时沿 parent 链上溯到 deck 的直接子节点，
        用最接近 deck 的那层（其 location 才是真正的 slot 坐标）解析槽位号。
        """
        # 沿 parent 链收集 [plate, ..., deck 直接子节点]。
        chain = []
        cur = plate
        while cur is not None and not isinstance(cur, PRCXI9300Deck):
            chain.append(cur)
            if isinstance(getattr(cur, "parent", None), PRCXI9300Deck):
                break
            cur = getattr(cur, "parent", None)

        # 1) 显式 update_resource_site 最优先（move_plate 写回 / 声明），plate 自身或其上层皆可。
        for cand in chain:
            extra = getattr(cand, "unilabos_extra", {}) or {}
            digits = "".join(c for c in str(extra.get("update_resource_site", "") or "") if c.isdigit())
            if digits:
                return int(digits)

        actual_deck = self._resolve_deck(plate, deck)

        # 2) 位置反算：优先最接近 deck 的那层（嵌套 plate 的 location 相对父级，不可信）。
        for cand in reversed(chain):
            sn = PRCXI9300Handler._get_slot_number(cand, deck=actual_deck)
            if sn is not None:
                return sn

        # 3) 名字兜底：远端解析回来的实例与 deck 上不是同一对象时，按 name 找到 deck 上
        #    真正在位的同名子节点 c，用 c 的位置（权威坐标）反算槽位号——绝不能用
        #    children 的数组下标 i+1，因为 deck.children 并非按槽位号 1..16 顺序排列
        #    （实测顺序会跨运行变化，如 [slot_3, slot_13, slot_2, trash, slot_6, slot_7]），
        #    i+1 会给出完全错误且随运行漂移的槽位（slot_6 曾被判成 5/4/2/1），即 tip 架误判成邻位。
        if actual_deck is not None:
            for cand in reversed(chain):
                cname = getattr(cand, "name", None)
                if cname is not None:
                    for c in actual_deck.children:
                        if getattr(c, "name", None) == cname:
                            sn = PRCXI9300Handler._get_slot_number(c, deck=actual_deck)
                            if sn is not None:
                                return sn

        raise RuntimeError(
            f"无法定位 {getattr(plate, 'name', '?')} 所在的 PRCXI 槽位"
            "（已沿 parent 链上溯 adapter/module；请确认已挂到 deck 或在 unilabos_extra 中提供 update_resource_site=Tn）。"
        )

    @staticmethod
    def _resource_num_items_y(resource) -> int:
        """板/TipRack 等在 Y 向孔位数；无 ``num_items_y`` 或非正数时返回 1。"""
        ny = getattr(resource, "num_items_y", None)
        try:
            n = int(ny) if ny is not None else 1
        except (TypeError, ValueError):
            n = 1
        return n if n >= 1 else 1

    async def shaker_action(self, time: int, module_no: int, amplitude: int, is_wait: bool):
        step = self.api_client.shaker_action(
            time=time,
            module_no=module_no,
            amplitude=amplitude,
            is_wait=is_wait,
        )
        self.steps_todo_list.append(step)
        return step

    async def shaking_incubation_action(self, time: int, module_no: int, amplitude: int, is_wait: bool, temperature: int):
        step = self.api_client.shaking_incubation_action(
            time=time,
            module_no=module_no,
            amplitude=amplitude,
            is_wait=is_wait,
            temperature=temperature,
        )
        self.steps_todo_list.append(step)
        return step

    async def magnetic_action(self, time: int, module_no: int, height: int, is_wait: bool):
        step = self.api_client.magnetic_action(
            time=time,
            module_no=module_no,
            height=height,
            is_wait=is_wait,
        )
        self.steps_todo_list.append(step)
        return step

    async def pick_up_resource(self, pickup: Optional[ResourcePickup] = None, **backend_kwargs):

        # 优先用调用方显式给出的源 slot 号（int）；否则回退到从 pickup.resource 反推。
        source_plate_number = backend_kwargs.get("source_plate_number", None)
        if isinstance(source_plate_number, int):
            plate_number = source_plate_number
        else:
            if pickup is None:
                raise ValueError("pick_up_resource requires either source_plate_number(int) or a ResourcePickup")
            # pickup.resource 即被夹取的 plate 本身（move_plate→move_resource→pick_up_resource
            # 传入的就是 plate），直接据此反推槽号，不再向上取 parent。
            plate = pickup.resource
            plate_number = self._deck_plate_slot_no(plate, getattr(plate, "parent", None))
        is_whole_plate = True
        balance_height = 0
        hierarchy = int(backend_kwargs.get("hierarchy", 1))  # 夹取层级，默认 1
        force = int(backend_kwargs.get("force", 1))  # MvKit 夹持力，默认 1
        step = self.api_client.clamp_jaw_pick_up(
            plate_number, is_whole_plate, balance_height, hierarchy=hierarchy, force=force
        )

        self.steps_todo_list.append(step)
        return step

    async def drop_resource(self, drop: Optional[ResourceDrop] = None, **backend_kwargs):

        plate_number = None
        target_plate_number = backend_kwargs.get("target_plate_number", None)
        if isinstance(target_plate_number, int):
            # 调用方直接给出目标 slot 号（int）。
            plate_number = target_plate_number
        elif target_plate_number is not None:
            # 向后兼容：target_plate_number 为 Resource 时反推槽位号。
            plate = target_plate_number
            deck = plate.parent
            plate_number = self._deck_plate_slot_no(plate, deck)

        is_whole_plate = True
        balance_height = 0
        if plate_number is None:
            raise ValueError("target_plate_number is required when dropping a resource")
        hierarchy = int(backend_kwargs.get("hierarchy", 1))  # 放下层级，默认 1
        force = int(backend_kwargs.get("force", 1))  # MvKit 夹持力，默认 1
        step = self.api_client.clamp_jaw_drop(
            plate_number, is_whole_plate, balance_height, hierarchy=hierarchy, force=force
        )
        self.steps_todo_list.append(step)
        return step

    async def heater_action(self, temperature: float, time: int):
        print(f"\n\nHeater action: temperature={temperature}, time={time}\n\n")
        # return await self.api_client.heater_action(temperature, time)

    def post_init(self, ros_node: BaseROS2DeviceNode):
        self._ros_node = ros_node

    def create_protocol(self, protocol_name):
        self.protocol_name = protocol_name
        self.steps_todo_list = []

        if not len(self.matrix_id):
            handler = getattr(self, "_handler", None)
            if handler is not None and hasattr(handler, "_match_and_create_matrix"):
                handler._match_and_create_matrix()

            if not len(self.matrix_id):
                raise AssertionError(
                    "create_protocol 未能创建/匹配 WorkTabletMatrix："
                    "deck 上无可识别耗材或自动匹配失败（请确认槽位物料已挂载）。"
                )

    def _ensure_run_ready(self) -> None:
        """执行前 reset 门禁检查（同步/异步 run 复用）。"""
        if self._execute_setup:
            assert self.is_reset_ok, (
                "PRCXI9300Backend is not reset-ready. "
                "Please call setup() first (or ensure setup=False mode device is not resetting)."
            )
        else:
            # setup=False：默认信任外部流程托管复位状态，不做硬阻断。
            # 仍触发一次实时检测，便于在日志中暴露“当前可能仍处于复位中”的风险。
            if not self.is_reset_ok:
                print(
                    "[PRCXI][WARN] setup=False 且 reset 状态未就绪，按外部托管模式继续执行。"
                    "如需严格拦截请使用 setup=True。"
                )

    async def _wait_sleep(self, seconds: float = 1.0) -> None:
        """等待轮询间隔：优先使用 ROS 节点 sleep，避免阻塞执行器。"""
        if hasattr(self, "_ros_node") and self._ros_node is not None:
            await self._ros_node.sleep(seconds)
        else:
            await asyncio.sleep(seconds)

    def _log_wait_for_finish_failure(self, reason: str) -> None:
        """wait_for_finish 失败时输出可观测上下文。"""
        timeout_cfg = self.wait_finish_timeout_s
        timeout_desc = "None(不设总超时)" if timeout_cfg is None else f"{timeout_cfg:.1f}s"
        err_code: Any = None
        try:
            err_code = self.api_client.get_error_code()
        except Exception as e:
            err_code = f"<获取失败: {e}>"

        last_step: Any = None
        try:
            state_list = self.api_client.step_state_list()
            if isinstance(state_list, list) and state_list:
                tail = state_list[-1]
                if isinstance(tail, dict):
                    last_step = {
                        "SequenceNumber": tail.get("SequenceNumber"),
                        "Name": tail.get("Name"),
                        "State": tail.get("State"),
                    }
                else:
                    last_step = tail
        except Exception as e:
            last_step = f"<读取失败: {e}>"

        timeout_hit = bool(getattr(self.api_client, "last_wait_timed_out", False))
        print(
            f"[PRCXI][{self.protocol_version}] wait_for_finish 失败：reason={reason} "
            f"timeout_hit={timeout_hit} timeout_cfg={timeout_desc} "
            f"error_code={err_code} last_step={last_step}"
        )

    def run_protocol(self, protocol_id: str = None):
        self._ensure_run_ready()
        if self.protocol_version == "v04":
            return self._run_protocol_v04(protocol_id)
        return self._run_protocol_v03(protocol_id)

    async def run_protocol_async(self, protocol_id: str = None):
        """异步执行方案，避免在协程里阻塞式 sleep。"""
        self._ensure_run_ready()
        if self.protocol_version == "v04":
            return await self._run_protocol_v04_async(protocol_id)
        return await self._run_protocol_v03_async(protocol_id)

    def _run_protocol_v03(self, protocol_id: str = None):
        """v03：AddSolution(steps) → LoadSolution(guid) → Start → wait_for_finish（保持原行为）。"""
        run_time = time.time()
        if protocol_id == "" or protocol_id is None:
            solution_id = self.api_client.add_solution(
                f"protocol_{run_time}", self.matrix_id, self.steps_todo_list
            )
        else:
            solution_id = protocol_id
        print(f"PRCXI9300Backend created solution with ID: {solution_id}")
        self.api_client.load_solution(solution_id)
        _safe_print(json.dumps(self.steps_todo_list, indent=2))
        if not self.api_client.start():
            return False
        if not self.api_client.wait_for_finish(timeout_s=self.wait_finish_timeout_s):
            self._log_wait_for_finish_failure("v03 执行未成功完成")
            return False
        return True


    def _run_protocol_v04(self, protocol_id: str = None):
        """v04：建布局已在 create_protocol 完成；这里 AddSolution_V04/加载方案 → Start → wait。

        - ``protocol_id`` 非空：视为已有方案名，走真实链路 ``LoadSolution(方案名)`` → Start → wait。
        - ``protocol_id`` 为空：按 v7 协议调用 ``ISolution.AddSolution_V04``，由服务端生成方案 XML。
        """
        if not protocol_id:
            plan_name = getattr(self, "protocol_name", "") or f"protocol_{int(time.time())}"
            if not self.steps_todo_list:
                print(f"[PRCXI][v04] 方案 {plan_name} 无动作步骤，按空协议完成。")
                return True
            print(f"[PRCXI][v04] AddSolution_V04(方案名={plan_name}, boardId={self.matrix_id})")
            created_plan = self.api_client.add_solution_v04(
                plan_name,
                self.matrix_id,
                self.steps_todo_list,
            )
            if isinstance(created_plan, str) and created_plan.strip():
                plan_name = created_plan.strip()
            print(f"[PRCXI][v04] 服务端已创建方案：{plan_name}")
        else:
            plan_name = str(protocol_id)

        print(f"[PRCXI][v04] LoadSolution(方案名={plan_name})")
        if not self.api_client.load_solution(plan_name):
            print(f"[PRCXI][v04] 加载方案失败：{plan_name}（确认方案已存在于 NeonGenesis 并被识别）")
            return False
        if not self.api_client.start():
            return False
        if not self.api_client.wait_for_finish(timeout_s=self.wait_finish_timeout_s):
            self._log_wait_for_finish_failure("v04 执行未成功完成")
            return False
        return True

    async def _run_protocol_v03_async(self, protocol_id: str = None):
        """v03 异步执行：保留原链路，wait 阶段改为非阻塞轮询。"""
        run_time = time.time()
        if protocol_id == "" or protocol_id is None:
            solution_id = self.api_client.add_solution(
                f"protocol_{run_time}", self.matrix_id, self.steps_todo_list
            )
        else:
            solution_id = protocol_id
        print(f"PRCXI9300Backend created solution with ID: {solution_id}")
        self.api_client.load_solution(solution_id)
        _safe_print(json.dumps(self.steps_todo_list, indent=2))
        if not self.api_client.start():
            return False
        if not await self.api_client.wait_for_finish_async(
            sleep_coro=self._wait_sleep,
            timeout_s=self.wait_finish_timeout_s,
        ):
            self._log_wait_for_finish_failure("v03 异步执行未成功完成")
            return False
        return True

    async def _run_protocol_v04_async(self, protocol_id: str = None):
        """v04 异步执行：保留原判定语义（GetStartStatus + StepState），只替换阻塞等待。"""
        if not protocol_id:
            plan_name = getattr(self, "protocol_name", "") or f"protocol_{int(time.time())}"
            if not self.steps_todo_list:
                print(f"[PRCXI][v04] 方案 {plan_name} 无动作步骤，按空协议完成。")
                return True
            print(f"[PRCXI][v04] AddSolution_V04(方案名={plan_name}, boardId={self.matrix_id})")
            created_plan = self.api_client.add_solution_v04(
                plan_name,
                self.matrix_id,
                self.steps_todo_list,
            )
            if isinstance(created_plan, str) and created_plan.strip():
                plan_name = created_plan.strip()
            print(f"[PRCXI][v04] 服务端已创建方案：{plan_name}")
        else:
            plan_name = str(protocol_id)

        print(f"[PRCXI][v04] LoadSolution(方案名={plan_name})")
        if not self.api_client.load_solution(plan_name):
            print(f"[PRCXI][v04] 加载方案失败：{plan_name}（确认方案已存在于 NeonGenesis 并被识别）")
            return False
        if not self.api_client.start():
            return False
        if not await self.api_client.wait_for_finish_async(
            sleep_coro=self._wait_sleep,
            timeout_s=self.wait_finish_timeout_s,
        ):
            self._log_wait_for_finish_failure("v04 异步执行未成功完成")
            return False
        return True

    @classmethod
    def check_channels(cls, use_channels: List[int]) -> List[int]:
        """检查通道是否符合要求，PRCXI9300Backend 只支持所有 8 个通道。"""
        if use_channels != [0, 1, 2, 3, 4, 5, 6, 7]:
            print("PRCXI9300Backend only supports all 8 channels, using default [0, 1, 2, 3, 4, 5, 6, 7].")
            return [0, 1, 2, 3, 4, 5, 6, 7]
        return use_channels

    @staticmethod
    def _normalize_use_channels(use_channels) -> Optional[List[int]]:
        """numpy / list / None → list[int] | None。"""
        if use_channels is None:
            return None
        if hasattr(use_channels, "tolist"):
            return list(use_channels.tolist())
        return list(use_channels)

    def _axis_from_channels(self, use_channels, volume: Optional[float] = None) -> str:
        """决定本次操作的物理轴 → ``"Left"`` / ``"Right"``。

        - 配置了 ``pip_setting``：用设备层在调用前写入的 ``self._active_axis``（默认 ``"Left"``），
          并在给出 ``volume`` 时按对应轴 ``vol`` 校验是否超量程。``use_channels`` 此时已是
          PLR 合法的 0-based 下标，不再用于判轴。
        - 未配置：走 legacy 约定（``[0]`` → Left，``[1]`` → Right，其余报错）。
        """
        if self.pip_setting is not None:
            axis = self._active_axis or "Left"
            if volume is not None:
                key = "left" if axis == "Left" else "right"
                spec = self.pip_setting.get(key)
                if spec is not None and float(volume) > float(spec["vol"]) + 1e-9:
                    raise ValueError(
                        f"体积 {volume}µL 超过 {key} 轴量程 {spec['vol']}µL（active_axis={axis}）"
                    )
            return axis
        chans = self._normalize_use_channels(use_channels)
        if chans == [0]:
            return "Left"
        if chans == [1]:
            return "Right"
        raise ValueError("Invalid use channels: " + str(chans))

    def _effective_num_channels(self, use_channels) -> int:
        """当前操作的有效并行通道数。

        配置 ``pip_setting`` 时取所选轴（``self._active_axis``）的 ``channels`` 与本次
        ``use_channels`` 长度的较小值；未配置时回退到全局 ``self.num_channels``（legacy）。
        """
        if self.pip_setting is None:
            return self.num_channels
        chans = self._normalize_use_channels(use_channels) or []
        axis = self._active_axis or "Left"
        key = "left" if axis == "Left" else "right"
        spec = self.pip_setting.get(key) or {}
        axis_ch = int(spec.get("channels", self.num_channels))
        return min(len(chans), axis_ch) if chans else axis_ch

    async def reset(self) -> bool:
        """复位设备（各轴回初始位置），阻塞等待复位完成。

        封装「获取错误码 → 清错 → stop → reset → 轮询 GetResetStatus 等待复位完成」。
        setup 与对外 reset action 共用此方法，便于统一维护。
        """
        # 先获取错误代码
        error_code = self.api_client.get_error_code()
        if error_code:
            print(f"PRCXI9300 error code detected: {error_code}")

        # 清除错误代码
        self.api_client.clear_error_code()
        print("PRCXI9300 error code cleared.")
        self.api_client.stop()
        # 执行重置
        print("Starting PRCXI9300 reset...")
        self.api_client.reset()

        # 检查重置状态并等待完成：
        # GetResetStatus = true 表示“正在复位”，复位完成后回到 false。
        deadline = time.time() + self.reset_timeout
        start_deadline = min(deadline, time.time() + min(5.0, max(1.0, self.reset_timeout * 0.1)))
        seen_resetting = False
        self._is_reset_ok = False

        while True:
            in_reset = self.api_client.get_reset_status()
            if in_reset:
                seen_resetting = True
                print("Waiting for PRCXI9300 to reset...")
            elif seen_resetting:
                # 已观察到“复位中”且现在退出复位，判定复位完成。
                self._is_reset_ok = True
                break
            elif time.time() >= start_deadline:
                # 避免某些固件“瞬时复位/不上报复位中”导致一直等待启动状态。
                print("GetResetStatus 未观测到复位中状态，按复位已完成处理。")
                self._is_reset_ok = True
                break

            if time.time() >= deadline:
                raise RuntimeError(
                    f"PRCXI9300 复位等待超时（{self.reset_timeout}s）。"
                    "请检查设备复位状态；若 GetResetStatus 语义与预期相反"
                    "（预期: true=复位中），"
                    "可在初始化时设置 reset_status_inverted 覆盖（protocol_version="
                    f"{self.protocol_version}）。"
                )

            if hasattr(self, "_ros_node") and self._ros_node is not None:
                await self._ros_node.sleep(1)
            else:
                await asyncio.sleep(1)
        print("PRCXI9300 reset successfully.")
        return True

    async def setup(self):
        await super().setup()
        try:
            if self._execute_setup:
                await self.reset()

                # self.api_client.update_clamp_jaw_position(self.matrix_id, self.claw_positions)

        except ConnectionRefusedError as e:
            raise RuntimeError(
                f"Failed to connect to PRCXI9300 API at {self.host}:{self.port}. "
                "Please ensure the PRCXI9300 service is running."
            ) from e

    async def stop(self):
        self.api_client.stop()

    async def pause(self) -> bool:
        """暂停当前方案执行。"""
        return bool(self.api_client.pause())

    async def resume(self) -> bool:
        """继续已暂停的方案执行。"""
        return bool(self.api_client.resume())

    async def pick_up_tips(self, ops: List[Pickup], use_channels: List[int] = None):
        """Pick up tips from the specified resource."""
        axis = self._axis_from_channels(use_channels)
        _eff_nc = self._effective_num_channels(use_channels)
        plate_slots = []
        for op in ops:
            plate = op.resource.parent
            deck = plate.parent
            plate_slots.append(self._deck_plate_slot_no(plate, deck))

        if len(set(plate_slots)) != 1:
            raise ValueError("All pickups must be from the same plate (slot). Found different slots: " + str(plate_slots))

        _rack = ops[0].resource.parent
        ny = self._resource_num_items_y(_rack)
        tip_columns = []
        for op in ops:
            tipspot = op.resource
            if self._resource_num_items_y(tipspot.parent) != ny:
                raise ValueError("All pickups must use tip racks with the same num_items_y")
            tipspot_index = tipspot.parent.children.index(tipspot)
            tip_columns.append(tipspot_index // ny)
        if len(set(tip_columns)) != 1:
            raise ValueError(
                "All pickups must be from the same tip column. Found different columns: " + str(tip_columns)
            )
        PlateNo = plate_slots[0]
        hole_col = tip_columns[0] + 1
        hole_row = 1
        if _eff_nc != 8:
            hole_row = tipspot_index % ny + 1

        step = self.api_client.Load(
            axis=axis,
            dosage=0,
            plate_no=PlateNo,
            is_whole_plate=False,
            hole_row=hole_row,
            hole_col=hole_col,
            blending_times=0,
            balance_height=0,
            plate_or_hole=f"H{hole_col}-{ny},T{PlateNo}",
            hole_numbers=f"{(hole_col - 1) * ny + hole_row}" if _eff_nc != 8 else "1,2,3,4,5",
        )
        self.steps_todo_list.append(step)

    async def drop_tips(self, ops: List[Drop], use_channels: List[int] = None):
        """Pick up tips from the specified resource."""
        axis = self._axis_from_channels(use_channels)
        _eff_nc = self._effective_num_channels(use_channels)
        # 检查trash #
        if ops[0].resource.name == "trash":
            _plate = ops[0].resource
            _deck = _plate.parent
            PlateNo = self._deck_plate_slot_no(_plate, _deck)

            step = self.api_client.UnLoad(
                axis=axis,
                dosage=0,
                plate_no=PlateNo,
                is_whole_plate=False,
                hole_row=1,
                hole_col=3,
                blending_times=0,
                balance_height=0,
                plate_or_hole=f"H{1}-8,T{PlateNo}",
                hole_numbers="1,2,3,4,5,6,7,8",
            )
            self.steps_todo_list.append(step)
            return
        # print(ops[0].resource.parent.children.index(ops[0].resource))

        plate_slots = []
        for op in ops:
            plate = op.resource.parent
            deck = plate.parent
            plate_slots.append(self._deck_plate_slot_no(plate, deck))
        if len(set(plate_slots)) != 1:
            raise ValueError(
                "All drop_tips must be from the same plate (slot). Found different slots: " + str(plate_slots)
            )

        _rack = ops[0].resource.parent
        ny = self._resource_num_items_y(_rack)
        tip_columns = []
        for op in ops:
            tipspot = op.resource
            if self._resource_num_items_y(tipspot.parent) != ny:
                raise ValueError("All drop_tips must use tip racks with the same num_items_y")
            tipspot_index = tipspot.parent.children.index(tipspot)
            tip_columns.append(tipspot_index // ny)
        if len(set(tip_columns)) != 1:
            raise ValueError(
                "All drop_tips must be from the same tip column. Found different columns: " + str(tip_columns)
            )

        PlateNo = plate_slots[0]
        hole_col = tip_columns[0] + 1
        hole_row = 1
        if _eff_nc != 8:
            hole_row = tipspot_index % ny + 1

        step = self.api_client.UnLoad(
            axis=axis,
            dosage=0,
            plate_no=PlateNo,
            is_whole_plate=False,
            hole_row=hole_row,
            hole_col=hole_col,
            blending_times=0,
            balance_height=0,
            plate_or_hole=f"H{hole_col}-{ny},T{PlateNo}",
            hole_numbers="1,2,3,4,5,6,7,8",
        )
        self.steps_todo_list.append(step)

    async def mix(
        self,
        targets: Sequence[Container],
        mix_time: int = None,
        mix_vol: Optional[int] = None,
        height_to_bottom: Optional[float] = None,
        offsets: Optional[Coordinate] = None,
        mix_rate: Optional[float] = None,
        none_keys: List[str] = [],
        use_channels: Optional[List[int]] = [0],
    ):
        """Mix liquid in the specified resources."""
        axis = self._axis_from_channels(use_channels)
        _eff_nc = self._effective_num_channels(use_channels)
        plate_slots = []
        for op in targets:
            deck = op.parent.parent.parent
            plate = op.parent
            plate_slots.append(self._deck_plate_slot_no(plate, deck))

        if len(set(plate_slots)) != 1:
            raise ValueError("All mix targets must be from the same plate (slot). Found different slots: " + str(plate_slots))

        _plate0 = targets[0].parent
        ny = self._resource_num_items_y(_plate0)
        tip_columns = []
        for op in targets:
            if self._resource_num_items_y(op.parent) != ny:
                raise ValueError("All mix targets must be on plates with the same num_items_y")
            tipspot_index = op.parent.children.index(op)
            tip_columns.append(tipspot_index // ny)

        if len(set(tip_columns)) != 1:
            raise ValueError(
                "All mix targets must be in the same column group. Found different columns: " + str(tip_columns)
            )

        PlateNo = plate_slots[0]
        hole_col = tip_columns[0] + 1
        hole_row = 1
        if _eff_nc != 8:
            hole_row = tipspot_index % ny + 1

        assert mix_time > 0
        step = self.api_client.Blending(
            axis=axis,
            dosage=mix_vol,
            plate_no=PlateNo,
            is_whole_plate=False,
            hole_row=hole_row,
            hole_col=hole_col,
            blending_times=mix_time,
            balance_height=0,
            plate_or_hole=f"H{hole_col}-{ny},T{PlateNo}",
            hole_numbers="1,2,3,4,5,6,7,8",
        )
        self.steps_todo_list.append(step)

    async def aspirate(self, ops: List[SingleChannelAspiration], use_channels: List[int] = None):
        """Aspirate liquid from the specified resources."""
        axis = self._axis_from_channels(
            use_channels, volume=getattr(ops[0], "volume", None) if ops else None
        )
        _eff_nc = self._effective_num_channels(use_channels)
        plate_slots = []
        for op in ops:
            plate = op.resource.parent
            deck = plate.parent
            plate_slots.append(self._deck_plate_slot_no(plate, deck))

        if len(set(plate_slots)) != 1:
            raise ValueError("All aspirate must be from the same plate (slot). Found different slots: " + str(plate_slots))

        _plate0 = ops[0].resource.parent
        ny = self._resource_num_items_y(_plate0)
        tip_columns = []
        for op in ops:
            tipspot = op.resource
            if self._resource_num_items_y(tipspot.parent) != ny:
                raise ValueError("All aspirate wells must be on plates with the same num_items_y")
            tipspot_index = tipspot.parent.children.index(tipspot)
            tip_columns.append(tipspot_index // ny)

        if len(set(tip_columns)) != 1:
            raise ValueError(
                "All aspirate must be from the same tip column. Found different columns: " + str(tip_columns)
            )

        volumes = [op.volume for op in ops]
        if len(set(volumes)) != 1:
            raise ValueError("All aspirate volumes must be the same. Found different volumes: " + str(volumes))

        PlateNo = plate_slots[0]
        hole_col = tip_columns[0] + 1
        hole_row = 1
        assist_fun1 = ""
        if _eff_nc != 8:
            hole_row = tipspot_index % ny + 1
        if ops[0].blow_out_air_volume is not None:
            assist_fun1 = f"反向吸液({float(min(max(ops[0].blow_out_air_volume,0),10))}ul)"
        raw_liquid_height = ops[0].liquid_height
        safe_liquid_height = 0.0 if raw_liquid_height is None else float(raw_liquid_height)

        step = self.api_client.Imbibing(
            axis=axis,
            dosage=float(volumes[0]),
            plate_no=PlateNo,
            is_whole_plate=False,
            hole_row=hole_row,
            hole_col=hole_col,
            blending_times=0,
            balance_height=int(min(max(safe_liquid_height,0),10)),
            plate_or_hole=f"H{hole_col}-{ny},T{PlateNo}",
            hole_numbers="1,2,3,4,5,6,7,8",
            assist_fun1=assist_fun1,
        )
        self.steps_todo_list.append(step)

    async def dispense(self, ops: List[SingleChannelDispense], use_channels: List[int] = None):
        """Dispense liquid into the specified resources."""
        # 丢弃零体积的空操作通道：8 通道整列 dispense 经 target free-volume 裁剪后会出现
        # 形如 [250,0,0,...,0]（满孔被裁成 0）。PRCXI 八连排要求整列同量，但零体积本就是
        # 空操作；过滤掉零体积、无吹样的通道后，按实际有量的通道执行（部分列退化为单通道），
        # 避免误触发 "All dispense volumes must be the same"。保留带 blow_out 的零体积 op（air-gap）。
        if ops and use_channels is not None and len(use_channels) == len(ops):
            keep = [
                i
                for i, op in enumerate(ops)
                if (getattr(op, "volume", 0) or 0) > 0
                or (getattr(op, "blow_out_air_volume", 0) or 0)
            ]
            if keep and len(keep) < len(ops):
                ops = [ops[i] for i in keep]
                use_channels = [use_channels[i] for i in keep]
        axis = self._axis_from_channels(
            use_channels, volume=getattr(ops[0], "volume", None) if ops else None
        )
        _eff_nc = self._effective_num_channels(use_channels)
        plate_slots = []
        for op in ops:
            plate = op.resource.parent
            deck = plate.parent
            plate_slots.append(self._deck_plate_slot_no(plate, deck))

        if len(set(plate_slots)) != 1:
            raise ValueError("All dispense must be from the same plate (slot). Found different slots: " + str(plate_slots))

        _plate0 = ops[0].resource.parent
        ny = self._resource_num_items_y(_plate0)
        tip_columns = []
        for op in ops:
            tipspot = op.resource
            if self._resource_num_items_y(tipspot.parent) != ny:
                raise ValueError("All dispense wells must be on plates with the same num_items_y")
            tipspot_index = tipspot.parent.children.index(tipspot)
            tip_columns.append(tipspot_index // ny)

        if len(set(tip_columns)) != 1:
            raise ValueError(
                "All dispense must be from the same tip column. Found different columns: " + str(tip_columns)
            )

        volumes = [op.volume for op in ops]
        if len(set(volumes)) != 1:
            raise ValueError("All dispense volumes must be the same. Found different volumes: " + str(volumes))

        PlateNo = plate_slots[0]
        hole_col = tip_columns[0] + 1

        hole_row = 1
        if _eff_nc != 8:
            hole_row = tipspot_index % ny + 1

        assist_fun1 = ""
        if ops[0].blow_out_air_volume is not None:
            assist_fun1 = f"吹样({float(min(max(ops[0].blow_out_air_volume,5),10))}ul)"
        else :
            assist_fun1 = f"吹样({5.0}ul)"
        raw_liquid_height = ops[0].liquid_height
        safe_liquid_height = 0.0 if raw_liquid_height is None else float(raw_liquid_height)

        step = self.api_client.Tapping(
            axis=axis,
            dosage=float(volumes[0]),
            plate_no=PlateNo,
            is_whole_plate=False,
            hole_row=hole_row,
            hole_col=hole_col,
            blending_times=0,
            balance_height=int(min(max(safe_liquid_height,0),10)),
            plate_or_hole=f"H{hole_col}-{ny},T{PlateNo}",
            hole_numbers="1,2,3,4,5,6,7,8",
            assist_fun1=assist_fun1,
            liquid_method=self._resolve_dispense_liquid_method(axis),
        )
        self.steps_todo_list.append(step)

    def _resolve_dispense_liquid_method(self, axis: str) -> str:
        """按 handler 的 touch_tip 模式/方向决定本次放液的 LiquidDispensingMethod。

        仅当本次 transfer 勾选 touch_tip（``handler._touch_tip_pending``）且模式为
        native/both 时，返回「放液后靠壁」；否则返回正常放液（行为不变）。
        靠壁方向：follow_axis → 左轴靠左壁、右轴靠右壁；left/right → 固定一侧。
        """
        handler = self._handler
        pending = bool(getattr(handler, "_touch_tip_pending", False)) if handler else False
        mode = str(getattr(handler, "touch_tip_mode", "native") or "native") if handler else "native"
        wall = str(getattr(handler, "touch_tip_wall", "follow_axis") or "follow_axis") if handler else "follow_axis"
        if not pending or mode not in ("native", "both"):
            return LIQUID_METHOD_NORMAL
        if wall == "left":
            return LIQUID_METHOD_WALL_LEFT
        if wall == "right":
            return LIQUID_METHOD_WALL_RIGHT
        # follow_axis
        return LIQUID_METHOD_WALL_RIGHT if str(axis).strip().lower() == "right" else LIQUID_METHOD_WALL_LEFT

    def _whole_plate_step_kwargs(
        self,
        plate_no: int,
        *,
        dosage: float = 0,
        balance_height: int = 0,
        axis: Optional[str] = None,
    ) -> Dict[str, Any]:
        """96 整板指令的公共入参：``is_whole_plate=True`` + 整板孔位占位。

        整板模式下机器忽略逐孔字段：``hole_row/hole_col`` 置 1、``plate_or_hole=f"T{plate_no}"``、
        ``hole_numbers`` 留空。轴取入参 ``axis`` 或已由 handler 选定的 ``self._active_axis``。
        """
        return dict(
            axis=axis or (self._active_axis or "Left"),
            dosage=dosage,
            plate_no=plate_no,
            is_whole_plate=True,
            hole_row=1,
            hole_col=1,
            blending_times=0,
            balance_height=balance_height,
            plate_or_hole=f"T{plate_no}",
            hole_numbers="",
        )

    async def pick_up_tips96(self, pickup: PickupTipRack):
        """整板取枪头：按选定轴（``_active_axis``）下发 ``is_whole_plate=True`` 的 Load。"""
        rack = pickup.resource
        PlateNo = self._deck_plate_slot_no(rack, getattr(rack, "parent", None))
        step = self.api_client.Load(**self._whole_plate_step_kwargs(PlateNo))
        self.steps_todo_list.append(step)

    async def drop_tips96(self, drop: DropTipRack):
        """整板丢枪头：trash / tip_rack 皆按 ``is_whole_plate=True`` 下发 UnLoad。"""
        res = drop.resource
        PlateNo = self._deck_plate_slot_no(res, getattr(res, "parent", None))
        step = self.api_client.UnLoad(**self._whole_plate_step_kwargs(PlateNo))
        self.steps_todo_list.append(step)

    async def aspirate96(self, aspiration: Union[MultiHeadAspirationPlate, MultiHeadAspirationContainer]):
        """整板吸液：按选定轴下发 ``is_whole_plate=True`` 的 Imbibing。"""
        if isinstance(aspiration, MultiHeadAspirationPlate):
            plate = aspiration.wells[0].parent
        else:
            plate = aspiration.container
        PlateNo = self._deck_plate_slot_no(plate, getattr(plate, "parent", None))
        axis = self._axis_from_channels(None, volume=getattr(aspiration, "volume", None))
        raw_liquid_height = getattr(aspiration, "liquid_height", None)
        safe_liquid_height = 0.0 if raw_liquid_height is None else float(raw_liquid_height)
        assist_fun1 = ""
        blow = getattr(aspiration, "blow_out_air_volume", None)
        if blow is not None:
            assist_fun1 = f"反向吸液({float(min(max(blow, 0), 10))}ul)"
        step = self.api_client.Imbibing(
            **self._whole_plate_step_kwargs(
                PlateNo,
                dosage=float(aspiration.volume),
                balance_height=int(min(max(safe_liquid_height, 0), 10)),
                axis=axis,
            ),
            assist_fun1=assist_fun1,
        )
        self.steps_todo_list.append(step)

    async def dispense96(self, dispense: Union[MultiHeadDispensePlate, MultiHeadDispenseContainer]):
        """整板放液：按选定轴下发 ``is_whole_plate=True`` 的 Tapping（含 touch_tip 靠壁）。"""
        if isinstance(dispense, MultiHeadDispensePlate):
            plate = dispense.wells[0].parent
        else:
            plate = dispense.container
        PlateNo = self._deck_plate_slot_no(plate, getattr(plate, "parent", None))
        axis = self._axis_from_channels(None, volume=getattr(dispense, "volume", None))
        raw_liquid_height = getattr(dispense, "liquid_height", None)
        safe_liquid_height = 0.0 if raw_liquid_height is None else float(raw_liquid_height)
        blow = getattr(dispense, "blow_out_air_volume", None)
        if blow is not None:
            assist_fun1 = f"吹样({float(min(max(blow, 5), 10))}ul)"
        else:
            assist_fun1 = f"吹样({5.0}ul)"
        step = self.api_client.Tapping(
            **self._whole_plate_step_kwargs(
                PlateNo,
                dosage=float(dispense.volume),
                balance_height=int(min(max(safe_liquid_height, 0), 10)),
                axis=axis,
            ),
            assist_fun1=assist_fun1,
            liquid_method=self._resolve_dispense_liquid_method(axis),
        )
        self.steps_todo_list.append(step)

    async def move_picked_up_resource(self, move: ResourceMove):
        pass

    def can_pick_up_tip(self, channel_idx: int, tip: Tip) -> bool:
        return True  # PRCXI9300Backend does not have tip compatibility issues

    def serialize(self) -> dict:
        raise NotImplementedError()

    @property
    def num_channels(self) -> int:
        return self._num_channels


class PRCXI9300Api:
    """PRCXI 移液站 RPC 客户端，支持 v03（旧版，历史名 legacy）与 v04（新版）双协议。

    协议由构造参数 ``protocol_version`` 统一切换（唯一入口）：
    - ``"v03"``（历史名 ``"legacy"``，仍兼容传入）：旧版协议（``AddSolution``、无 ``_V04``
      后缀的 IMatrix、``AddWorkTabletMatrix``/``AddWorkTabletMatrix2``、``LoadSolution`` 传 GUID）。
    - ``"v04"``：新版协议（IMatrix 全部 ``_V04``、v7 方案走 ``AddSolution_V04`` 并由服务端
      生成 XML，``LoadSolution`` 传方案名，新增 ``IClientSession.IsConnect`` /
      ``GetStartStatus`` / ``RemoveSolution`` 等）。

    ``is_9320`` 仅保留“机型能力”语义（step_mode / 旧版建布局用 2 参版本），
    不再承担协议判定职责（见《修改计划》决策点 D/E）。
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 9999,
        timeout: float = 10.0,
        axis="Left",
        debug: bool = False,
        is_9320: bool = False,
        protocol_version: Literal["v03", "v04"] = "v04",
        reset_status_inverted: Optional[bool] = None,
        connect_retries: int = 10,
        connect_retry_delay_s: float = 1.0,
    ) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.debug = debug
        self.axis = axis
        self.is_9320 = is_9320
        self.protocol_version = self._normalize_protocol_version(protocol_version)
        # GetResetStatus 原始语义：true=正在复位，false=非复位中。
        # 若固件返回语义相反（true=非复位中），可显式传 reset_status_inverted=True 覆盖。
        if reset_status_inverted is None or reset_status_inverted == "":
            reset_status_inverted = False
        self.reset_status_inverted = bool(reset_status_inverted)
        self._wait_timeout_last = False
        # RPC 短连接瞬断重试：总尝试次数（含首次）与首次退避秒数（线性封顶 5s）。
        self.connect_retries = max(1, int(connect_retries))
        self.connect_retry_delay_s = max(0.0, float(connect_retry_delay_s))
        # 步骤进度缓存：唯一写入者是 refresh_step_snapshot()（由 state_list datakey 的
        # getter 每秒驱动）。所有 wait_for_finish 方法只读该快照、循环体内不再发 RPC，
        # 从而彻底避免阻塞事件循环 / 与 datakey 轮询并发抢占同一台设备的 RPC 服务。
        self._step_snapshot: Dict[str, Any] = {
            "progress": [0, 0],
            "last_state": -1,
            "num_steps": 0,
            "started": False,
            # seen_active：本轮 start() 之后是否已经**正向观测到新一轮执行开始**
            # （步骤列表出现 last_state in {0,1} 的未完成态）。用于区分「机器尚未归零、
            # GetStepStateList 仍返回上一轮已完成列表(last=2)」的 stale 读，避免误判完成。
            "seen_active": False,
        }

    @staticmethod
    def _normalize_protocol_version(value: Optional[str]) -> str:
        v = str(value or "v04").strip().lower()
        # 统一口径为 v03 / v04（便于记忆）；兼容历史命名 legacy / 03 / v3 → v03。
        if v in {"legacy", "03", "v3"}:
            v = "v03"
        if v not in {"v03", "v04"}:
            raise ValueError(f"不支持的 protocol_version: {value!r}（仅 'v03' / 'v04'，兼容旧名 'legacy'）")
        return v

    @property
    def is_v04(self) -> bool:
        return self.protocol_version == "v04"

    def _matrix_method(self, base: str) -> str:
        """IMatrix 方法名：v04 加 ``_V04`` 后缀，v03 用原名。"""
        return f"{base}_V04" if self.is_v04 else base

    @staticmethod
    def _as_bool(value: Any) -> bool:
        """把服务端 Data 归一化为布尔（兼容 True/1/'true'/'1' 及带引号形式）。"""
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.strip().strip('"').lower() in {"true", "1"}
        return False

    @staticmethod
    def _len_prefix(n: int) -> bytes:
        return bytes.fromhex(format(n, "016x"))

    def _debug_response(self, payload: str) -> str:
        """debug/仿真模式：按方法名返回可解析的模拟 JSON（含 V04 新方法）。"""
        try:
            req = json.loads(payload)
            method = req.get("MethodName")
        except Exception:
            method = None

        data: Any = True
        if method in {"AddSolution"}:
            data = str(uuid.uuid4())
        elif method in {"AddSolution_V04"}:
            try:
                params = req.get("Paramters") or []
                data = params[0] if params else "debug_v04_plan"
            except Exception:
                data = "debug_v04_plan"
        elif method in {
            "AddWorkTabletMatrix",
            "AddWorkTabletMatrix2",
            "AddWorkTabletMatrix_V04",
            "RemoveWorkTabletMatrix_V04",
            "UpdatePosition_V04",
            "UpdateClampJawPosition",
            "UpdatePipettingPosition",
        }:
            data = {"Success": True, "Message": "debug mock"}
        elif method in {"GetErrorCode"}:
            data = ""
        elif method in {
            "RemoveErrorCodet",
            "Reset",
            "Start",
            "Stop",
            "Pause",
            "Resume",
            "LoadSolution",
            "RemoveSolution",
            "IsConnect",
        }:
            data = True
        elif method in {"GetStartStatus"}:
            data = False
        elif method in {"GetStepStateList", "GetStepStatus", "GetStepState"}:
            data = []
        elif method in {
            "GetSolutionList",
            "GetAllMaterial",
            "GetAllMaterial_V04",
            "GetWorkTabletMatrices",
            "GetWorkTabletMatrices_V04",
        }:
            data = []
        elif method in {
            "GetWorkTabletMatrixById",
            "GetWorkTabletMatrixById_V04",
            "GetMaterialById_V04",
        }:
            data = {}
        elif method in {"GetLocation"}:
            data = {"X": 0, "Y": 0, "Z": 0}
        elif method in {"GetResetStatus"}:
            # true=复位中；debug 下默认返回空闲态（false）。
            data = False
        return json.dumps({"Success": True, "Msg": "debug mock", "Data": data})

    @staticmethod
    def _recv_exact(sock: socket.socket, size: int) -> bytes:
        """从 socket 精确读取 ``size`` 字节，读不满即报错（帧读取，不依赖对端关闭）。"""
        chunks: List[bytes] = []
        remaining = size
        while remaining > 0:
            chunk = sock.recv(remaining)
            if not chunk:
                raise PRCXIError(f"响应长度不完整：期望 {size} 字节，还差 {remaining} 字节")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _raw_request(self, payload: str) -> str:
        if self.debug:
            # 调试/仿真模式下直接返回可解析的模拟 JSON，避免后续 json.loads 报错
            return self._debug_response(payload)

        # 每次 RPC 新建短连接：网络抖动时整次请求重试（不中断上层 wait/transfer）。
        # 半包（连接中断）可重试；非法长度属于协议异常，不重试。
        last_exc: Optional[BaseException] = None
        attempts = self.connect_retries
        for attempt in range(1, attempts + 1):
            try:
                with contextlib.closing(socket.socket()) as sock:
                    sock.settimeout(self.timeout)
                    sock.connect((self.host, self.port))
                    data = payload.encode()
                    sock.sendall(self._len_prefix(len(data)) + data)

                    # 帧读取：先读 8 字节大端长度头 → 精确读 N 字节正文（不依赖对端关闭连接，更稳）。
                    header = self._recv_exact(sock, 8)
                    payload_size = int.from_bytes(header, byteorder="big", signed=False)
                    if payload_size <= 0 or payload_size > 64 * 1024 * 1024:
                        raise PRCXIError(f"响应长度非法：{payload_size}")
                    return self._recv_exact(sock, payload_size).decode()
            except PRCXIError as e:
                if "响应长度不完整" not in str(e):
                    raise
                last_exc = e
            except (TimeoutError, ConnectionError, OSError) as e:
                last_exc = e
            if attempt >= attempts:
                break
            delay = min(self.connect_retry_delay_s * attempt, 5.0)
            print(
                f"[PRCXI] RPC 网络瞬断，第 {attempt}/{attempts} 次失败: {last_exc!r}；"
                f"{delay:.1f}s 后重连…"
            )
            time.sleep(delay)
        assert last_exc is not None
        raise last_exc

    # ---------------------------------------------------- 方案相关（ISolution）
    def list_solutions(self) -> List[Dict[str, Any]]:
        """GetSolutionList"""
        return self.call("ISolution", "GetSolutionList")

    def load_solution(self, plan_or_solution: str) -> bool:
        """LoadSolution。

        ⚠ 入参语义随协议不同：
        - ``v04``：方案名 ``PlanName``（《调用文档》5.3 确认）。
        - ``v03``：方案 GUID（``solution_id``）。

        RPC 方法名两者一致（``LoadSolution``），差异仅在业务语义。
        """
        return self.call("ISolution", "LoadSolution", [plan_or_solution])

    def remove_solution(self, plan_name: str) -> bool:
        """RemoveSolution（按方案名删除；V04 新增，v03 服务端可能未实现）。"""
        return self.call("ISolution", "RemoveSolution", [plan_name])

    def add_solution(self, name: str, matrix_id: str, steps: List[Dict[str, Any]]) -> str:
        """AddSolution → 返回新方案 GUID（仅 v03）。

        V04 v7 正式建方案请用 ``add_solution_v04`` / ``ISolution.AddSolution_V04``。
        """
        if self.is_v04:
            raise PRCXIError(
                "V04 v7 不支持旧 ISolution.AddSolution：请使用 ISolution.AddSolution_V04(name, boardId, steps)。"
            )
        return self.call("ISolution", "AddSolution", [name, matrix_id, steps])

    def add_solution_v04(self, name: str, board_id: str, steps: Sequence[Dict[str, Any]]) -> Any:
        """AddSolution_V04 → 服务端生成方案 XML 并返回方案名/结果（仅 V04 v7）。"""
        if not self.is_v04:
            raise PRCXIError("AddSolution_V04 仅 V04 协议可用；v03 请使用 add_solution。")
        plan_name = str(name or "").strip()
        if not plan_name:
            raise PRCXIError("AddSolution_V04 方案名不能为空。")
        board_id = str(board_id or "").strip()
        if not board_id:
            raise PRCXIError("AddSolution_V04 需要有效 boardId；请先创建或选择 V04 Board。")
        v04_steps = legacy_steps_to_v04_solution_steps(steps)
        if not v04_steps:
            raise PRCXIError("AddSolution_V04 至少需要一个方案步骤。")
        return self.call("ISolution", "AddSolution_V04", [plan_name, board_id, v04_steps])

    # ---------------------------------------------------- 连接会话（IClientSession）
    def is_connect(self) -> bool:
        """IClientSession.IsConnect —— V04 判定“已连接”的唯一依据。

        v03 服务端未必实现该接口，debug 下返回 True。
        """
        return self._as_bool(self.call("IClientSession", "IsConnect"))

    # ---------------------------------------------------- 自动化控制（IAutomation）
    def start(self) -> bool:
        # 新方案启动前重置步骤缓存，避免 wait 方法读到上一轮的完成态而立即返回成功。
        self._step_snapshot = {
            "progress": [0, 0],
            "last_state": -1,
            "num_steps": 0,
            "started": False,
            "seen_active": False,
        }
        return self.call("IAutomation", "Start")

    def stop(self) -> bool:
        """Stop"""
        return self.call("IAutomation", "Stop")

    def reset(self) -> bool:
        """Reset（各轴回初始位置）"""
        return self.call("IAutomation", "Reset")

    def get_start_status(self) -> bool:
        """GetStartStatus —— 是否运行中（V04 新增，轮询用，比轮询步骤更轻）。"""
        return self._as_bool(self.call("IAutomation", "GetStartStatus"))

    @staticmethod
    def _normalize_step_state(value: Any) -> int:
        """把 GetStepStateList 的 ``State`` 归一化为 0/1/2（未知返回 -1）。

        兼容两种编码：
        - v03：数值 ``0=未执行 / 1=执行中 / 2=已完成``；
        - v04：可能返回枚举名字符串 ``"None"/"NotStarted"/"Running"/"Completed"``
          （见 ``prcxi_socket_client_v04.StepState.describe``），也可能是数字串。
        """
        if isinstance(value, bool):
            return -1
        if isinstance(value, (int, float)):
            return int(value)
        s = str(value).strip().strip('"').lower()
        mapping = {
            "0": 0, "none": 0, "notstarted": 0, "not_started": 0,
            "1": 1, "running": 1,
            "2": 2, "completed": 2, "complete": 2, "finished": 2,
        }
        return mapping.get(s, -1)

    @property
    def last_wait_timed_out(self) -> bool:
        """最近一次 wait_for_finish 是否因「显式配置的总超时」退出。"""
        return bool(self._wait_timeout_last)

    @staticmethod
    def _normalize_wait_timeout_seconds(timeout_s: Optional[float]) -> Optional[float]:
        """把等待超时秒数归一化；None/<=0 表示不设总超时（不会提前终止）。"""
        if timeout_s is None or timeout_s == "":
            return None
        try:
            timeout_v = float(timeout_s)
        except (TypeError, ValueError):
            return None
        return timeout_v if timeout_v > 0 else None

    @staticmethod
    async def _wait_sleep_async(
        sleep_coro: Optional[Callable[[float], Awaitable[None]]],
        seconds: float,
    ) -> None:
        if sleep_coro is not None:
            await sleep_coro(seconds)
        else:
            await asyncio.sleep(seconds)

    def wait_for_finish(self, timeout_s: Optional[float] = None) -> bool:
        """等待方案执行完成。

        v03 / v04 统一改为读步骤进度缓存 ``_step_snapshot``（唯一写入者是
        ``refresh_step_snapshot``，由 ``state_list`` datakey 的 getter 定时驱动）：
        末步 ``State==2`` 视为完成、``>2`` 视为报错。异步路径纯读缓存、不发 RPC；
        同步路径自刷新缓存兜底。``timeout_s`` 默认 None：不设总超时。
        """
        self._wait_timeout_last = False
        if self.is_v04:
            return self._wait_for_finish_v04(timeout_s=timeout_s)
        return self._wait_for_finish_v03(timeout_s=timeout_s)

    async def wait_for_finish_async(
        self,
        *,
        sleep_coro: Optional[Callable[[float], Awaitable[None]]] = None,
        timeout_s: Optional[float] = None,
    ) -> bool:
        """异步等待方案执行完成（用于 async action，避免阻塞执行器）。"""
        self._wait_timeout_last = False
        if self.is_v04:
            return await self._wait_for_finish_v04_async(sleep_coro=sleep_coro, timeout_s=timeout_s)
        return await self._wait_for_finish_v03_async(sleep_coro=sleep_coro, timeout_s=timeout_s)

    def _eval_wait_snapshot(
        self, snap: Dict[str, Any], start_deadline: float
    ) -> Optional[bool]:
        """基于步骤缓存快照判定等待结果（v03/v04 共用，只读、不发 RPC）。

        返回：``True`` 成功；``False`` 失败/报错；``None`` 继续等待。

        - 末步 ``State==2`` 且**已正向观测到本轮执行开始**（``seen_active``）→ 成功；
        - 末步 ``State>2``（报错态）→ 失败；
        - 到 ``start_deadline`` 仍未观测到本轮执行开始：仅空协议（``num==0``）按完成兜底；
          有步骤但尚未见到未完成态时**继续等待**（不信任上一轮遗留的 stale ``last==2``）。

        关键：``start()`` 后机器的 ``GetStepStateList`` 存在数秒延迟，仍会返回上一轮
        「已完成」列表（``last==2, completed==num``）。若沿用旧的 ``started`` 判定
        （``num>0`` 即 latch），会读到 stale 完成态而秒回成功，导致后续动作雪崩。
        故这里改用 ``seen_active`` —— 只有观测到本轮出现 ``last in {0,1}`` 的未完成态后，
        才认可随后的 ``last==2`` 为真正完成。
        """
        last = snap.get("last_state", -1)
        seen_active = snap.get("seen_active", False)
        num = snap.get("num_steps", 0)
        if seen_active and last == 2:
            return True
        if isinstance(last, int) and last > 2:
            return False
        if not seen_active and time.time() >= start_deadline:
            # 宽限期到仍未观测到本轮执行开始：空协议按完成兜底；有步骤则继续等待，
            # 绝不用可能是上一轮遗留的 stale last==2 判成功。
            if num == 0:
                return True
        return None

    def _wait_for_finish_v03(self, timeout_s: Optional[float] = None) -> bool:
        """v03：只读步骤缓存判定完成。同步路径自刷新缓存兜底（独立线程内阻塞无害，
        兼容无 ROS datakey 定时器的直跑/测试场景）。"""
        if self.debug:
            return True
        timeout_s = self._normalize_wait_timeout_seconds(timeout_s)
        deadline = (time.time() + timeout_s) if timeout_s is not None else None
        start_deadline = time.time() + 5.0
        while True:
            if deadline is not None and time.time() >= deadline:
                self._wait_timeout_last = True
                return False
            snap = self.refresh_step_snapshot()
            result = self._eval_wait_snapshot(snap, start_deadline)
            if result is not None:
                return result
            time.sleep(1)

    async def _wait_for_finish_v03_async(
        self,
        *,
        sleep_coro: Optional[Callable[[float], Awaitable[None]]] = None,
        timeout_s: Optional[float] = None,
    ) -> bool:
        """v03 异步：循环体**只读**内存缓存 ``_step_snapshot``，绝不发 RPC，
        故永不阻塞事件循环。缓存由 ``state_list`` datakey 的 getter 定时刷新。"""
        if self.debug:
            return True
        timeout_s = self._normalize_wait_timeout_seconds(timeout_s)
        deadline = (time.time() + timeout_s) if timeout_s is not None else None
        start_deadline = time.time() + 5.0
        while True:
            if deadline is not None and time.time() >= deadline:
                self._wait_timeout_last = True
                return False
            result = self._eval_wait_snapshot(self._step_snapshot, start_deadline)
            if result is not None:
                return result
            await self._wait_sleep_async(sleep_coro, 1.0)

    def _wait_for_finish_v04(self, timeout_s: Optional[float] = None) -> bool:
        """v04：完成判定统一改为读步骤缓存（末步 ``State==2``），不再单独轮询
        ``GetStartStatus``。同步路径自刷新缓存兜底。

        ``start_deadline``（5s）仅用于判定「是否观测到启动」，不是总执行超时。
        总超时仅在 ``timeout_s`` 显式传入正数时生效；默认 None 不会提前终止。
        """
        if self.debug:
            return True
        timeout_s = self._normalize_wait_timeout_seconds(timeout_s)
        deadline = (time.time() + timeout_s) if timeout_s is not None else None
        start_deadline = time.time() + 5.0
        while True:
            if deadline is not None and time.time() >= deadline:
                snap = self.refresh_step_snapshot()
                if snap.get("seen_active") and snap.get("last_state") == 2:
                    return True
                self._wait_timeout_last = True
                return False
            snap = self.refresh_step_snapshot()
            result = self._eval_wait_snapshot(snap, start_deadline)
            if result is not None:
                return result
            time.sleep(1)

    async def _wait_for_finish_v04_async(
        self,
        *,
        sleep_coro: Optional[Callable[[float], Awaitable[None]]] = None,
        timeout_s: Optional[float] = None,
    ) -> bool:
        """v04 异步：循环体**只读**内存缓存 ``_step_snapshot``，绝不发 RPC。默认不设总超时。"""
        if self.debug:
            return True
        timeout_s = self._normalize_wait_timeout_seconds(timeout_s)
        deadline = (time.time() + timeout_s) if timeout_s is not None else None
        start_deadline = time.time() + 5.0
        while True:
            if deadline is not None and time.time() >= deadline:
                snap = self._step_snapshot
                if snap.get("seen_active") and snap.get("last_state") == 2:
                    return True
                self._wait_timeout_last = True
                return False
            result = self._eval_wait_snapshot(self._step_snapshot, start_deadline)
            if result is not None:
                return result
            await self._wait_sleep_async(sleep_coro, 1.0)

    def call(self, service: str, method: str, params: Optional[list] = None) -> Any:
        payload = json.dumps(
            {"ServiceName": service, "MethodName": method, "Paramters": params or []}, separators=(",", ":")
        )
        resp = json.loads(self._raw_request(payload))
        if not resp.get("Success", False):
            raise PRCXIError(resp.get("Message") or resp.get("Msg") or "Unknown error")
        data = resp.get("Data")
        try:
            return json.loads(data)
        except (TypeError, json.JSONDecodeError):
            return data

    def pause(self) -> bool:
        """Pause"""
        return self.call("IAutomation", "Pause")

    def resume(self) -> bool:
        """Resume"""
        return self.call("IAutomation", "Resume")

    def get_error_code(self) -> Optional[str]:
        """GetErrorCode"""
        return self.call("IAutomation", "GetErrorCode")

    def get_reset_status(self) -> bool:
        """GetResetStatus → 返回“是否处于复位中”。

        协议语义：``true=复位中``，``false=非复位中``（未复位/已复位完成都可能为 false）。
        若目标固件语义相反，可通过 ``reset_status_inverted=True`` 显式覆盖。
        """
        if self.debug:
            return False
        res = self._as_bool(self.call("IAutomation", "GetResetStatus"))
        return (not res) if self.reset_status_inverted else res

    def clear_error_code(self) -> bool:
        """RemoveErrorCodet"""
        return self.call("IAutomation", "RemoveErrorCodet")

    # ---------------------------------------------------- 运行状态（IMachineState）
    def step_state_list(self) -> List[Dict[str, Any]]:
        """GetStepStateList"""
        return self.call("IMachineState", "GetStepStateList")

    def refresh_step_snapshot(self) -> Dict[str, Any]:
        """唯一的步骤进度 RPC 写入者：拉一次 GetStepStateList 并回写 ``_step_snapshot``。

        由 ``state_list`` datakey 的 getter（经 PropertyPublisher 定时器，period≈1s）驱动，
        以及同步 wait 方法自刷新兜底调用。异步 wait 方法**不**调用它（纯读快照，避免阻塞
        事件循环）。异常时保留上一次快照，返回当前快照，不向上抛。
        """
        try:
            status = self.step_state_list()
            if isinstance(status, list):
                num = len(status)
                if num:
                    last = self._normalize_step_state(status[-1].get("State"))
                    completed = sum(
                        1
                        for s in status
                        if isinstance(s, dict) and self._normalize_step_state(s.get("State")) == 2
                    )
                else:
                    last = -1
                    completed = 0
                prev_started = bool(self._step_snapshot.get("started"))
                started = prev_started or num > 0
                # seen_active：一旦本轮观测到「有步骤且末步未完成」(last in {0,1})，即认定
                # 本轮新执行已真正开始，之后才允许把 last==2 判为完成。start() 会把它清零，
                # 从而屏蔽 start() 后机器尚未归零、仍返回上一轮 last==2 的 stale 读。
                prev_seen_active = bool(self._step_snapshot.get("seen_active"))
                seen_active = prev_seen_active or (num > 0 and last in (0, 1))
                # 原子整体替换，保证并发读到的是一致快照。
                self._step_snapshot = {
                    "progress": [completed, num],
                    "last_state": last,
                    "num_steps": num,
                    "started": started,
                    "seen_active": seen_active,
                }
        except Exception as e:
            print(f"[PRCXI] refresh_step_snapshot 失败，沿用旧快照: {e!r}")
        return self._step_snapshot

    def step_status(self, seq_num: int) -> Dict[str, Any]:
        """GetStepStatus（单步耗时明细）。

        ⛔ V04 服务端未开放（厂商 C# 文档确认），v04 下调用直接抛错，避免误依赖；
        请改用 ``step_state_list`` 三态判断进度。
        """
        if self.is_v04:
            raise PRCXIError("V04 服务端未开放 IMachineState.GetStepStatus，请改用 step_state_list。")
        return self.call("IMachineState", "GetStepStatus", [seq_num])

    def step_state(self, seq_num: int) -> Dict[str, Any]:
        """GetStepState"""
        return self.call("IMachineState", "GetStepState", [seq_num])

    def axis_location(self, axis_num: int = 1) -> Dict[str, Any]:
        """GetLocation（单轴实时坐标）。

        ⛔ V04 服务端未开放（厂商 C# 文档确认），v04 下调用直接抛错。
        """
        if self.is_v04:
            raise PRCXIError("V04 服务端未开放 IMachineState.GetLocation，拿不到轴实时坐标。")
        return self.call("IMachineState", "GetLocation", [axis_num])

    # ---------------------------------------------------- 版位矩阵（IMatrix）
    def get_all_materials(self) -> List[Dict[str, Any]]:
        """GetAllMaterial（v04 用 GetAllMaterial_V04）- 返回所有已注册物料列表。

        PRCXI 服务端在「无物料」或某些边界场景下可能返回非 list
        （bool / None / dict / JSON 字面量 ``true`` / ``false``），这里
        统一归一化为 ``List[Dict]``，避免上游 ``for m in material_list``
        触发 ``TypeError: 'bool' object is not iterable`` 等。
        """
        raw = self.call("IMatrix", self._matrix_method("GetAllMaterial"), [])
        if not isinstance(raw, list):
            return []
        if self.is_v04:
            # GetAllMaterial_V04 原始字段为 Type/Row/Col/Id，与驱动内部匹配用的
            # materialEnum/HoleRow/HoleColum/id_v4 不一致，这里补齐别名后再返回，
            # 否则按 materialEnum 分组会全部落到 None、耗材匹配失效。
            return [self._normalize_v04_material(m) if isinstance(m, dict) else m for m in raw]
        return raw

    @staticmethod
    def _normalize_v04_material(m: Dict[str, Any]) -> Dict[str, Any]:
        """把 GetAllMaterial_V04 原始字段补齐为驱动内部匹配字段（保留原字段）。

        - ``Type``  → ``materialEnum``（枚举含义一致：1=Tips/2=DeepWell/6=WasteBox…）
        - ``Row``   → ``HoleRow``
        - ``Col``   → ``HoleColum``
        - ``Id``    → ``id_v4``（V04 物料主键即 id_v4）
        """
        out = dict(m)
        if out.get("materialEnum") is None and out.get("Type") is not None:
            out["materialEnum"] = out.get("Type")
        if out.get("HoleRow") is None and out.get("Row") is not None:
            out["HoleRow"] = out.get("Row")
        if out.get("HoleColum") is None and out.get("Col") is not None:
            out["HoleColum"] = out.get("Col")
        if not out.get("id_v4") and out.get("Id"):
            out["id_v4"] = out.get("Id")
        return out

    def get_material_by_id(self, material_id: str) -> Dict[str, Any]:
        """GetMaterialById_V04（V04 新增，按 ID 查耗材）。"""
        return self.call("IMatrix", self._matrix_method("GetMaterialById"), [material_id])

    def list_matrices(self) -> List[Dict[str, Any]]:
        """GetWorkTabletMatrices（v04 用 GetWorkTabletMatrices_V04）"""
        return self.call("IMatrix", self._matrix_method("GetWorkTabletMatrices"))

    def matrix_by_id(self, matrix_id: str) -> Dict[str, Any]:
        """GetWorkTabletMatrixById（v04 用 GetWorkTabletMatrixById_V04）"""
        return self.call("IMatrix", self._matrix_method("GetWorkTabletMatrixById"), [matrix_id])

    def remove_work_tablet_matrix(self, matrix_id: str):
        """RemoveWorkTabletMatrix_V04（V04 新增，删除布局）。"""
        return self.call("IMatrix", self._matrix_method("RemoveWorkTabletMatrix"), [matrix_id])

    def update_position(self, board: Any):
        """UpdatePosition_V04（V04：整块 Board 更新，替代旧版夹爪/移液两个更新接口）。"""
        if not self.is_v04:
            raise PRCXIError("update_position 仅 v04 可用；v03 请用 update_clamp_jaw_position / update_pipetting_position。")
        return self.call("IMatrix", "UpdatePosition_V04", [to_rpc_value(board)])

    def update_clamp_jaw_position(self, target_matrix_id: str, claw_positions: List[Dict[str, Any]]):
        """更新夹爪板位位置。

        - v03：``UpdateClampJawPosition``（老版本 MatrixInfo 结构）。
        - v04：无独立夹爪更新接口，改为拉取当前 Board、把老位置字段映射合并进
          ``gripperPos`` 后调 ``UpdatePosition_V04``（字段映射见《修改计划》决策点 B，
          需真机核对）。拉不到 Board 时记录告警并跳过，不阻断主流程。
        """
        if not self.is_v04:
            position_params = {"MatrixId": target_matrix_id, "WorkTablets": claw_positions}
            return self.call("IMatrix", "UpdateClampJawPosition", [position_params])
        return self._v04_update_positions(target_matrix_id, claw_positions=claw_positions)

    def update_pipetting_position(self, target_matrix_id: str, pipetting_positions: List[Dict[str, Any]]):
        """更新移液位置。

        - v03：``UpdatePipettingPosition``。
        - v04：合并进 Board 的 ``PipettingPosList`` 后调 ``UpdatePosition_V04``。
        """
        if not self.is_v04:
            position_params = {"MatrixId": target_matrix_id, "WorkTablets": pipetting_positions}
            return self.call("IMatrix", "UpdatePipettingPosition", [position_params])
        return self._v04_update_positions(target_matrix_id, pipetting_positions=pipetting_positions)

    def _v04_update_positions(
        self,
        matrix_id: str,
        pipetting_positions: Optional[List[Dict[str, Any]]] = None,
        claw_positions: Optional[List[Dict[str, Any]]] = None,
    ):
        """V04：拉取 Board → 合并老位置字段 → UpdatePosition_V04。"""
        board = self.matrix_by_id(matrix_id)
        if not isinstance(board, dict) or not board:
            print(
                f"[PRCXI][v04] update_position 跳过：GetWorkTabletMatrixById_V04({matrix_id}) 未返回有效 Board。"
                "（V04 位置通常在设备侧标定，如需远程回写请确认 matrix_id 有效）"
            )
            return {"Success": False, "Message": "board not found"}
        merged, warnings = merge_positions_into_board(board, pipetting_positions, claw_positions)
        for w in warnings:
            print(f"[PRCXI][v04][位置映射] {w}")
        merged_with_pip = sum(1 for d in (merged.get("Details") or []) if d.get("PipettingPosList"))
        res = self.call("IMatrix", "UpdatePosition_V04", [merged])
        print(
            f"[PRCXI][v04] UpdatePosition_V04 matrix_id={matrix_id} "
            f"pip_in={len(pipetting_positions or [])} claw_in={len(claw_positions or [])} "
            f"details_with_pip={merged_with_pip} -> {res}"
        )
        return res

    def add_WorkTablet_Matrix(self, matrix: MatrixInfo):
        """新增布局。

        - v03：``AddWorkTabletMatrix``（9300）/ ``AddWorkTabletMatrix2``（9320），传老版本 MatrixInfo。
        - v04：把 MatrixInfo 映射为 V04 ``Board`` 后调 ``AddWorkTabletMatrix_V04``
          （字段映射见《修改计划》决策点 B）。
        """
        if not self.is_v04:
            method = "AddWorkTabletMatrix2" if self.is_9320 else "AddWorkTabletMatrix"
            return self.call("IMatrix", method, [matrix])
        columns = 4 if self.is_9320 else 3
        board = worktablets_to_board(dict(matrix), columns=columns, is_v04=self.is_v04)
        return self.call("IMatrix", "AddWorkTabletMatrix_V04", [board.to_rpc_dict()])

    def add_board_v04(self, board: "Board"):
        """v04：直接下发已构建好的 ``Board``（用于 prc_sites 自定义布局的真实下发）。"""
        if not self.is_v04:
            raise PRCXIError("add_board_v04 仅 v04 可用；v03 请用 add_WorkTablet_Matrix。")
        return self.call("IMatrix", "AddWorkTabletMatrix_V04", [board.to_rpc_dict()])

    def Load(
        self,
        dosage: int,
        plate_no: int,
        is_whole_plate: bool,
        hole_row: int,
        hole_col: int,
        blending_times: int,
        balance_height: int,
        plate_or_hole: str,
        hole_numbers: str,
        assist_fun1: str = "",
        assist_fun2: str = "",
        assist_fun3: str = "",
        assist_fun4: str = "",
        assist_fun5: str = "",
        liquid_method: str = "NormalDispense",
        axis: str = "Left",
    ) -> Dict[str, Any]:
        return {
            "StepAxis": axis,
            "Function": "Load",
            "DosageNum": dosage,
            "PlateNo": plate_no,
            "IsWholePlate": is_whole_plate,
            "HoleRow": hole_row,
            "HoleCol": hole_col,
            "BlendingTimes": blending_times,
            "BalanceHeight": balance_height,
            "PlateOrHoleNum": plate_or_hole,
            "AssistFun1": assist_fun1,
            "AssistFun2": assist_fun2,
            "AssistFun3": assist_fun3,
            "AssistFun4": assist_fun4,
            "AssistFun5": assist_fun5,
            "HoleNumbers": hole_numbers,
            "LiquidDispensingMethod": liquid_method,
        }

    def Imbibing(
        self,
        dosage: int,
        plate_no: int,
        is_whole_plate: bool,
        hole_row: int,
        hole_col: int,
        blending_times: int,
        balance_height: int,
        plate_or_hole: str,
        hole_numbers: str,
        assist_fun1: str = "",
        assist_fun2: str = "",
        assist_fun3: str = "",
        assist_fun4: str = "",
        assist_fun5: str = "",
        liquid_method: str = "NormalDispense",
        axis: str = "Left",
    ) -> Dict[str, Any]:
        return {
            "StepAxis": axis,
            "Function": "Imbibing",
            "DosageNum": dosage,
            "PlateNo": plate_no,
            "IsWholePlate": is_whole_plate,
            "HoleRow": hole_row,
            "HoleCol": hole_col,
            "BlendingTimes": blending_times,
            "BalanceHeight": balance_height,
            "PlateOrHoleNum": plate_or_hole,
            "AssistFun1": assist_fun1,
            "AssistFun2": assist_fun2,
            "AssistFun3": assist_fun3,
            "AssistFun4": assist_fun4,
            "AssistFun5": assist_fun5,
            "HoleNumbers": hole_numbers,
            "LiquidDispensingMethod": liquid_method,
        }

    def Tapping(
        self,
        dosage: int,
        plate_no: int,
        is_whole_plate: bool,
        hole_row: int,
        hole_col: int,
        blending_times: int,
        balance_height: int,
        plate_or_hole: str,
        hole_numbers: str,
        assist_fun1: str = "",
        assist_fun2: str = "",
        assist_fun3: str = "",
        assist_fun4: str = "",
        assist_fun5: str = "",
        liquid_method: str = "NormalDispense",
        axis: str = "Left",
    ) -> Dict[str, Any]:
        return {
            "StepAxis": axis,
            "Function": "Tapping",
            "DosageNum": dosage,
            "PlateNo": plate_no,
            "IsWholePlate": is_whole_plate,
            "HoleRow": hole_row,
            "HoleCol": hole_col,
            "BlendingTimes": blending_times,
            "BalanceHeight": balance_height,
            "PlateOrHoleNum": plate_or_hole,
            "AssistFun1": assist_fun1,
            "AssistFun2": assist_fun2,
            "AssistFun3": assist_fun3,
            "AssistFun4": assist_fun4,
            "AssistFun5": assist_fun5,
            "HoleNumbers": hole_numbers,
            "LiquidDispensingMethod": liquid_method,
        }

    def Blending(
        self,
        dosage: int,
        plate_no: int,
        is_whole_plate: bool,
        hole_row: int,
        hole_col: int,
        blending_times: int,
        balance_height: int,
        plate_or_hole: str,
        hole_numbers: str,
        assist_fun1: str = "",
        assist_fun2: str = "",
        assist_fun3: str = "",
        assist_fun4: str = "",
        assist_fun5: str = "",
        liquid_method: str = "NormalDispense",
        axis: str = "Left",
    ) -> Dict[str, Any]:
        return {
            "StepAxis": axis,
            "Function": "Blending",
            "DosageNum": dosage,
            "PlateNo": plate_no,
            "IsWholePlate": is_whole_plate,
            "HoleRow": hole_row,
            "HoleCol": hole_col,
            "BlendingTimes": blending_times,
            "BalanceHeight": balance_height,
            "PlateOrHoleNum": plate_or_hole,
            "AssistFun1": assist_fun1,
            "AssistFun2": assist_fun2,
            "AssistFun3": assist_fun3,
            "AssistFun4": assist_fun4,
            "AssistFun5": assist_fun5,
            "HoleNumbers": hole_numbers,
            "LiquidDispensingMethod": liquid_method,
        }

    def UnLoad(
        self,
        dosage: int,
        plate_no: int,
        is_whole_plate: bool,
        hole_row: int,
        hole_col: int,
        blending_times: int,
        balance_height: int,
        plate_or_hole: str,
        hole_numbers: str,
        assist_fun1: str = "",
        assist_fun2: str = "",
        assist_fun3: str = "",
        assist_fun4: str = "",
        assist_fun5: str = "",
        liquid_method: str = "NormalDispense",
        axis: str = "Left",
    ) -> Dict[str, Any]:
        return {
            "StepAxis": axis,
            "Function": "UnLoad",
            "DosageNum": dosage,
            "PlateNo": plate_no,
            "IsWholePlate": is_whole_plate,
            "HoleRow": hole_row,
            "HoleCol": hole_col,
            "BlendingTimes": blending_times,
            "BalanceHeight": balance_height,
            "PlateOrHoleNum": plate_or_hole,
            "AssistFun1": assist_fun1,
            "AssistFun2": assist_fun2,
            "AssistFun3": assist_fun3,
            "AssistFun4": assist_fun4,
            "AssistFun5": assist_fun5,
            "HoleNumbers": hole_numbers,
            "LiquidDispensingMethod": liquid_method,
        }

    def clamp_jaw_pick_up(
        self,
        plate_no: int,
        is_whole_plate: bool,
        balance_height: int,
        hierarchy: int = 1,
        force: int = 1,
    ) -> Dict[str, Any]:
        # ``Hierarchy``（层级）决定夹爪夹取/放下的高度档位（板位堆叠层级），与 SDK StepData
        # 的 ``hierarchy`` 字段对齐，默认 1。
        # ``Force`` 为 MvKit 夹持力，默认 1。
        return {
            "StepAxis": "ClampingJaw",
            "Function": "DefectiveLift",
            "PlateNo": plate_no,
            "IsWholePlate": is_whole_plate,
            "HoleRow": 1,
            "HoleCol": 1,
            "BalanceHeight": balance_height,
            "PlateOrHoleNum": f"T{plate_no}",
            "Hierarchy": hierarchy,
            "Force": force,
        }

    def clamp_jaw_drop(
        self,
        plate_no: int,
        is_whole_plate: bool,
        balance_height: int,
        hierarchy: int = 1,
        force: int = 1,
    ) -> Dict[str, Any]:
        # ``Hierarchy``（层级）决定夹爪夹取/放下的高度档位（板位堆叠层级），与 SDK StepData
        # 的 ``hierarchy`` 字段对齐，默认 1。
        # ``Force`` 为 MvKit 夹持力，默认 1。
        return {
            "StepAxis": "ClampingJaw",
            "Function": "PutDown",
            "PlateNo": plate_no,
            "IsWholePlate": is_whole_plate,
            "HoleRow": 1,
            "HoleCol": 1,
            "BalanceHeight": balance_height,
            "PlateOrHoleNum": f"T{plate_no}",
            "Hierarchy": hierarchy,
            "Force": force,
        }

    def shaker_action(self, time: int, module_no: int, amplitude: int, is_wait: bool):
        return {
            "StepAxis": "Left",
            "Function": "Shaking",
            "AssistFun1": time,
            "AssistFun2": module_no,
            "AssistFun3": amplitude,
            "AssistFun4": is_wait,
        }

    def shaking_incubation_action(self, time: int, module_no: int, amplitude: int, is_wait: bool, temperature: int):
        return {
            "StepAxis": "Left",
            "Function": "Shaking_Incubation",
            "AssistFun1": time,
            "AssistFun2": module_no,
            "AssistFun3": amplitude,
            "AssistFun4": is_wait,
            "AssistFun5": temperature,
        }

    def magnetic_action(self, time: int, module_no: int, height: int, is_wait: bool):
        return {
            "StepAxis": "Left",
            "Function": "Magnetic",
            "AssistFun1": time,
            "AssistFun2": module_no,
            "AssistFun3": height,
            "AssistFun4": is_wait,
        }


class DefaultLayout:

    def __init__(self, product_name: str = "PRCXI9300"):
        self.labresource = {}
        if product_name not in ["PRCXI9300", "PRCXI9320"]:
            raise ValueError(
                f"Unsupported product_name: {product_name}. Only 'PRCXI9300' and 'PRCXI9320' are supported."
            )

        if product_name == "PRCXI9300":
            self.rows = 2
            self.columns = 3
            self.layout = [1, 2, 3, 4, 5, 6]
            self.trash_slot = 6
            self.default_layout = {
                "MatrixId": f"{time.time()}",
                "MatrixName": f"{time.time()}",
                "MatrixCount": 6,
                "WorkTablets": [
                    {"Number": 1, "Code": "T1", "Material": {"uuid": "57b1e4711e9e4a32b529f3132fc5931f", "materialEnum": 0}},
                    {"Number": 2, "Code": "T2", "Material": {"uuid": "57b1e4711e9e4a32b529f3132fc5931f", "materialEnum": 0}},
                    {"Number": 3, "Code": "T3", "Material": {"uuid": "57b1e4711e9e4a32b529f3132fc5931f", "materialEnum": 0}},
                    {"Number": 4, "Code": "T4", "Material": {"uuid": "57b1e4711e9e4a32b529f3132fc5931f", "materialEnum": 0}},
                    {"Number": 5, "Code": "T5", "Material": {"uuid": "57b1e4711e9e4a32b529f3132fc5931f", "materialEnum": 0}},
                    {"Number": 6, "Code": "T6", "Material": {"uuid": "730067cf07ae43849ddf4034299030e9", "materialEnum": 0}},  # trash
                ],
            }

        elif product_name == "PRCXI9320":
            self.rows = 4
            self.columns = 4
            self.layout = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16]
            self.trash_slot = 16
            self.waste_liquid_slot = 12
            self.default_layout = {
                "MatrixId": f"{time.time()}",
                "MatrixName": f"{time.time()}",
                "MatrixCount": 16,
                "WorkTablets": [
                    {
                        "Number": 1,
                        "Code": "T1",
                        "Material": {"uuid": "57b1e4711e9e4a32b529f3132fc5931f", "materialEnum": 0},
                    },
                    {
                        "Number": 2,
                        "Code": "T2",
                        "Material": {"uuid": "57b1e4711e9e4a32b529f3132fc5931f", "materialEnum": 0},
                    },
                    {
                        "Number": 3,
                        "Code": "T3",
                        "Material": {"uuid": "57b1e4711e9e4a32b529f3132fc5931f", "materialEnum": 0},
                    },
                    {
                        "Number": 4,
                        "Code": "T4",
                        "Material": {"uuid": "57b1e4711e9e4a32b529f3132fc5931f", "materialEnum": 0},
                    },
                    {
                        "Number": 5,
                        "Code": "T5",
                        "Material": {"uuid": "57b1e4711e9e4a32b529f3132fc5931f", "materialEnum": 0},
                    },
                    {
                        "Number": 6,
                        "Code": "T6",
                        "Material": {"uuid": "57b1e4711e9e4a32b529f3132fc5931f", "materialEnum": 0},
                    },
                    {
                        "Number": 7,
                        "Code": "T7",
                        "Material": {"uuid": "57b1e4711e9e4a32b529f3132fc5931f", "materialEnum": 0},
                    },
                    {
                        "Number": 8,
                        "Code": "T8",
                        "Material": {"uuid": "57b1e4711e9e4a32b529f3132fc5931f", "materialEnum": 0},
                    },
                    {
                        "Number": 9,
                        "Code": "T9",
                        "Material": {"uuid": "57b1e4711e9e4a32b529f3132fc5931f", "materialEnum": 0},
                    },
                    {
                        "Number": 10,
                        "Code": "T10",
                        "Material": {"uuid": "57b1e4711e9e4a32b529f3132fc5931f", "materialEnum": 0},
                    },
                    {
                        "Number": 11,
                        "Code": "T11",
                        "Material": {"uuid": "57b1e4711e9e4a32b529f3132fc5931f", "materialEnum": 0},
                    },
                    {
                        "Number": 12,
                        "Code": "T12",
                        "Material": {"uuid": "730067cf07ae43849ddf4034299030e9", "materialEnum": 0},
                    },  # 这个设置成废液槽，用储液槽表示
                    {
                        "Number": 13,
                        "Code": "T13",
                        "Material": {"uuid": "57b1e4711e9e4a32b529f3132fc5931f", "materialEnum": 0},
                    },
                    {
                        "Number": 14,
                        "Code": "T14",
                        "Material": {"uuid": "57b1e4711e9e4a32b529f3132fc5931f", "materialEnum": 0},
                    },
                    {
                        "Number": 15,
                        "Code": "T15",
                        "Material": {"uuid": "57b1e4711e9e4a32b529f3132fc5931f", "materialEnum": 0},
                    },
                    {
                        "Number": 16,
                        "Code": "T16",
                        "Material": {"uuid": "730067cf07ae43849ddf4034299030e9", "materialEnum": 0},
                    },  # 这个设置成垃圾桶，用储液槽表示
                ],
            }

    def get_layout(self) -> Dict[str, Any]:
        result = {
            "rows": self.rows,
            "columns": self.columns,
            "layout": self.layout,
            "trash_slot": self.trash_slot,
        }
        if hasattr(self, 'waste_liquid_slot'):
            result["waste_liquid_slot"] = self.waste_liquid_slot
        return result

    def get_trash_slot(self) -> int:
        return self.trash_slot

    def get_waste_liquid_slot(self) -> int:
        return self.waste_liquid_slot

    def add_lab_resource(self, material_info):
        self.labresource = material_info

    def recommend_layout(self, needs: List[Tuple[str, str, int]]) -> Dict[str, Any]:
        layout_list = []
        for reagent_name, material_name, count in needs:

            if material_name not in self.labresource:
                raise ValueError(f"Material {reagent_name} not found in lab resources.")

            # 预留位置动态计算
        reserved_positions = {self.trash_slot}
        if hasattr(self, 'waste_liquid_slot'):
            reserved_positions.add(self.waste_liquid_slot)
        total_slots = self.rows * self.columns
        available_positions = [i for i in range(1, total_slots + 1) if i not in reserved_positions]

        # 计算总需求
        total_needed = sum(count for _, _, count in needs)
        if total_needed > len(available_positions):
            raise ValueError(
                f"需要 {total_needed} 个位置，但只有 {len(available_positions)} 个可用位置（排除预留位置 {reserved_positions}）"
            )

            # 依次分配位置
        current_pos = 0
        for reagent_name, material_name, count in needs:

            material_uuid = self.labresource[material_name]["uuid"]
            material_enum = self.labresource[material_name]["materialEnum"]

            for _ in range(count):
                if current_pos >= len(available_positions):
                    raise ValueError("位置不足，无法分配更多物料")

                position = available_positions[current_pos]
                # 找到对应的tablet并更新
                for tablet in self.default_layout["WorkTablets"]:
                    if tablet["Number"] == position:
                        tablet["Material"]["uuid"] = material_uuid
                        tablet["Material"]["materialEnum"] = material_enum
                        layout_list.append(
                            dict(reagent_name=reagent_name, material_name=material_name, positions=position)
                        )
                        break
                current_pos += 1
        return self.default_layout, layout_list


if __name__ == "__main__":
    # Example usage
    # 1. 用导出的json，给每个T1 T2板子设定相应的物料，如果是孔板和枪头盒，要对应区分
    # 2. backend需要支持num channel为1的情况
    # 3. 设计一个单点动作流程，可以跑
    # 4.

    # deck = PRCXI9300Deck(name="PRCXI_Deck_9300", size_x=100, size_y=100, size_z=100)

    # from pylabrobot.resources.opentrons.tip_racks import opentrons_96_tiprack_300ul,opentrons_96_tiprack_10ul
    # from pylabrobot.resources.opentrons.plates import corning_96_wellplate_360ul_flat, nest_96_wellplate_2ml_deep

    # def get_well_container(name: str) -> PRCXI9300Container:
    #     well_containers = corning_96_wellplate_360ul_flat(name).serialize()
    #     plate = PRCXI9300Container(name=name, size_x=50, size_y=50, size_z=10, category="plate",
    #                        ordering=well_containers["ordering"])
    #     plate_serialized = plate.serialize()
    #     plate_serialized["parent_name"] = deck.name
    #     well_containers.update({k: v for k, v in plate_serialized.items() if k not in ["children"]})
    #     new_plate: PRCXI9300Container = PRCXI9300Container.deserialize(well_containers)
    #     return new_plate

    # def get_tip_rack(name: str) -> PRCXI9300Container:
    #     tip_racks = opentrons_96_tiprack_300ul("name").serialize()
    #     tip_rack = PRCXI9300Container(name=name, size_x=50, size_y=50, size_z=10, category="tip_rack",
    #                        ordering=tip_racks["ordering"])
    #     tip_rack_serialized = tip_rack.serialize()
    #     tip_rack_serialized["parent_name"] = deck.name
    #     tip_racks.update({k: v for k, v in tip_rack_serialized.items() if k not in ["children"]})
    #     new_tip_rack: PRCXI9300Container = PRCXI9300Container.deserialize(tip_racks)
    #     return new_tip_rack

    # plate1 = get_tip_rack("RackT1")
    # plate1.load_state({
    #     "Material": {
    #         "uuid": "076250742950465b9d6ea29a225dfb00",
    #         "Code": "ZX-001-300",
    #         "Name": "300μL Tip头"
    #     }
    # })

    # plate2 = get_well_container("PlateT2")
    # plate2.load_state({
    #     "Material": {
    #         "uuid": "57b1e4711e9e4a32b529f3132fc5931f",
    #         "Code": "ZX-019-2.2",
    #         "Name": "96深孔板"
    #     }
    # })

    # plate3 = PRCXI9300Trash("trash", size_x=50, size_y=100, size_z=10, category="trash")
    # plate3.load_state({
    #     "Material": {
    #         "uuid": "730067cf07ae43849ddf4034299030e9"
    #     }
    # })

    # plate4 = get_well_container("PlateT4")
    # plate4.load_state({
    #     "Material": {
    #         "uuid": "57b1e4711e9e4a32b529f3132fc5931f",
    #         "Code": "ZX-019-2.2",
    #         "Name": "96深孔板"
    #     }
    # })

    # plate5 = get_well_container("PlateT5")
    # plate5.load_state({
    #     "Material": {
    #         "uuid": "57b1e4711e9e4a32b529f3132fc5931f",
    #         "Code": "ZX-019-2.2",
    #         "Name": "96深孔板"
    #     }
    # })
    # plate6 = get_well_container("PlateT6")

    # plate6.load_state({
    #     "Material": {
    #         "uuid": "57b1e4711e9e4a32b529f3132fc5931f",
    #         "Code": "ZX-019-2.2",
    #         "Name": "96深孔板"
    #     }
    # })

    # deck.assign_child_resource(plate1, location=Coordinate(0, 0, 0))
    # deck.assign_child_resource(plate2, location=Coordinate(0, 0, 0))
    # deck.assign_child_resource(plate3, location=Coordinate(0, 0, 0))
    # deck.assign_child_resource(plate4, location=Coordinate(0, 0, 0))
    # deck.assign_child_resource(plate5, location=Coordinate(0, 0, 0))
    # deck.assign_child_resource(plate6, location=Coordinate(0, 0, 0))

    # # # plate_2_liquids = [[('water', 500)]]*96

    # # # plate2.set_well_liquids(plate_2_liquids)

    # handler = PRCXI9300Handler(deck=deck, host="10.181.214.132", port=9999,
    #                            timeout=10.0, setup=False, debug=False,
    #                            simulator=True,
    #                            matrix_id="71593",
    #                            channel_num=8, axis="Left")  # Initialize the handler with the deck and host settings

    # plate_2_liquids = handler.set_group("water", plate2.children[:8], [200]*8)

    # plate5_liquids = handler.set_group("master_mix", plate5.children[:8], [100]*8)

    # handler.set_tiprack([plate1])
    # asyncio.run(handler.setup())  # Initialize the handler and setup the connection
    # from pylabrobot.resources import set_volume_tracking
    # from pylabrobot.resources import set_tip_tracking
    # set_volume_tracking(enabled=True)
    # from unilabos.resources.graphio import *
    # # A = tree_to_list([resource_plr_to_ulab(deck)])
    # # with open("deck_9300_new.json", "w", encoding="utf-8") as f:
    # #     json.dump(A, f, indent=4, ensure_ascii=False)
    # asyncio.run(handler.create_protocol(protocol_name="Test Protocol"))  # Initialize the backend and setup the connection
    # asyncio.run(handler.transfer_group("water", "master_mix", 100))  # Reset tip tracking

    # asyncio.run(handler.pick_up_tips(plate1.children[:8],[0,1,2,3,4,5,6,7]))
    # print(plate1.children[:8])
    # asyncio.run(handler.aspirate(plate2.children[:8],[50]*8, [0,1,2,3,4,5,6,7]))
    # print(plate2.children[:8])
    # asyncio.run(handler.dispense(plate5.children[:8],[50]*8,[0,1,2,3,4,5,6,7]))
    # print(plate5.children[:8])

    # #asyncio.run(handler.drop_tips(tip_rack.children[8:16],[0,1,2,3,4,5,6,7]))
    # asyncio.run(handler.discard_tips([0,1,2,3,4,5,6,7]))

    # asyncio.run(handler.mix(well_containers.children[:8
    # ], mix_time=3, mix_vol=50, height_to_bottom=0.5, offsets=Coordinate(0, 0, 0), mix_rate=100))
    # #print(json.dumps(handler._unilabos_backend.steps_todo_list, indent=2))  # Print matrix info
    # asyncio.run(handler.add_liquid(
    #     asp_vols=[100]*16,
    #     dis_vols=[100]*16,
    #     reagent_sources=plate2.children[:16],
    #     targets=plate5.children[:16],
    #     use_channels=[0, 1, 2, 3, 4, 5, 6, 7],
    #     flow_rates=[None] * 32,
    #     offsets=[Coordinate(0, 0, 0)] * 32,
    #     liquid_height=[None] * 16,
    #     blow_out_air_volume=[None] * 16,
    #     delays=None,
    #     mix_time=3,
    #     mix_vol=50,
    #     spread="wide",
    # ))
    # asyncio.run(handler.run_protocol())  # Run the protocol
    # asyncio.run(handler.remove_liquid(
    #     vols=[100]*16,
    #     sources=plate2.children[-16:],
    #     waste_liquid=plate5.children[:16], # 这个有些奇怪，但是好像也只能这么写
    #     use_channels=[0, 1, 2, 3, 4, 5, 6, 7],
    #     flow_rates=[None] * 32,
    #     offsets=[Coordinate(0, 0, 0)] * 32,
    #     liquid_height=[None] * 32,
    #     blow_out_air_volume=[None] * 32,
    #     spread="wide",
    # ))

    # acid = [20]*8+[40]*8+[60]*8+[80]*8+[100]*8+[120]*8+[140]*8+[160]*8+[180]*8+[200]*8+[220]*8+[240]*8
    # alkaline = acid[::-1]  # Reverse the acid list for alkaline
    # asyncio.run(handler.transfer_liquid(
    #     asp_vols=acid,
    #     dis_vols=acid,
    #     tip_racks=[plate1],
    #     sources=plate2.children[:],
    #     targets=plate5.children[:],
    #     use_channels=[0, 1, 2, 3, 4, 5, 6, 7],
    #     offsets=[Coordinate(0, 0, 0)] * 32,
    #     asp_flow_rates=[None] * 16,
    #     dis_flow_rates=[None] * 16,
    #     liquid_height=[None] * 32,
    #     blow_out_air_volume=[None] * 32,
    #     mix_times=3,
    #     mix_vol=50,
    #     spread="wide",
    # ))
    # asyncio.run(handler.run_protocol())  # Run the protocol
    # # input("Running protocol...")
    # # input("Press Enter to continue...")  # Wait for user input before proceeding
    # # print("PRCXI9300Handler initialized with deck and host settings.")

    ### 9320 ###

    deck = PRCXI9300Deck(name="PRCXI_Deck", size_x=100, size_y=100, size_z=100)

    from pylabrobot.resources.opentrons.tip_racks import tipone_96_tiprack_200ul, opentrons_96_tiprack_10ul
    from pylabrobot.resources.opentrons.plates import corning_96_wellplate_360ul_flat, nest_96_wellplate_2ml_deep

    def get_well_container(name: str) -> PRCXI9300Plate:
        well_containers = corning_96_wellplate_360ul_flat(name).serialize()
        plate = PRCXI9300Plate(
            name=name, size_x=50, size_y=50, size_z=10, category="plate", ordered_items=well_containers["ordering"]
        )
        plate_serialized = plate.serialize()
        plate_serialized["parent_name"] = deck.name
        well_containers.update({k: v for k, v in plate_serialized.items() if k not in ["children"]})
        new_plate: PRCXI9300Plate = PRCXI9300Plate.deserialize(well_containers)
        return new_plate

    def get_tip_rack(name: str, child_prefix: str = "tip") -> PRCXI9300TipRack:
        tip_racks = opentrons_96_tiprack_10ul(name).serialize()
        tip_rack = PRCXI9300TipRack(
            name=name,
            size_x=50,
            size_y=50,
            size_z=10,
            category="tip_rack",
            ordered_items=collections.OrderedDict(
                {k: f"{child_prefix}_{k}" for k, v in tip_racks["ordering"].items()}
            ),
        )
        tip_rack_serialized = tip_rack.serialize()
        tip_rack_serialized["parent_name"] = deck.name
        tip_racks.update({k: v for k, v in tip_rack_serialized.items() if k not in ["children"]})
        new_tip_rack: PRCXI9300TipRack = PRCXI9300TipRack.deserialize(tip_racks)
        return new_tip_rack

    plate1 = get_tip_rack("RackT1")
    plate1.load_state(
        {"Material": {"uuid": "068b3815e36b4a72a59bae017011b29f", "Code": "ZX-001-10+", "Name": "10μL加长 Tip头"}}
    )
    plate2 = get_well_container("PlateT2")
    plate2.load_state(
        {"Material": {"uuid": "b05b3b2aafd94ec38ea0cd3215ecea8f", "Code": "ZX-78-096", "Name": "细菌培养皿"}}
    )
    plate3 = get_well_container("PlateT3")
    plate3.load_state(
        {
            "Material": {
                "uuid": "04211a2dc93547fe9bf6121eac533650",
            }
        }
    )
    plate4 = get_well_container("PlateT4")
    plate4.load_state(
        {"Material": {"uuid": "b05b3b2aafd94ec38ea0cd3215ecea8f", "Code": "ZX-78-096", "Name": "细菌培养皿"}}
    )

    plate5 = get_tip_rack("RackT5")
    plate5.load_state(
        {
            "Material": {
                "uuid": "076250742950465b9d6ea29a225dfb00",
                "Code": "ZX-001-300",
                "SupplyType": 1,
                "Name": "300μL Tip头",
            }
        }
    )
    plate6 = get_well_container("PlateT6")
    plate6.load_state(
        {
            "Material": {
                "uuid": "e146697c395e4eabb3d6b74f0dd6aaf7",
                "Code": "1",
                "SupplyType": 1,
                "Name": "ep适配器",
                "SummaryName": "ep适配器",
            }
        }
    )
    plate7 = PRCXI9300Plate(
        name="plateT7", size_x=50, size_y=50, size_z=10, category="plate", ordered_items=collections.OrderedDict()
    )
    plate7.load_state({"Material": {"uuid": "04211a2dc93547fe9bf6121eac533650"}})
    plate8 = get_tip_rack("PlateT8")
    plate8.load_state({"Material": {"uuid": "04211a2dc93547fe9bf6121eac533650"}})
    plate9 = get_well_container("PlateT9")
    plate9.load_state(
        {
            "Material": {
                "uuid": "4a043a07c65a4f9bb97745e1f129b165",
                "Code": "ZX-58-0001",
                "SupplyType": 2,
                "Name": "全裙边 PCR适配器",
                "SummaryName": "全裙边 PCR适配器",
            }
        }
    )
    plate10 = get_well_container("PlateT10")
    plate10.load_state(
        {
            "Material": {
                "uuid": "4a043a07c65a4f9bb97745e1f129b165",
                "Code": "ZX-58-0001",
                "SupplyType": 2,
                "Name": "全裙边 PCR适配器",
                "SummaryName": "全裙边 PCR适配器",
            }
        }
    )
    plate11 = get_well_container("PlateT11")
    plate11.load_state(
        {
            "Material": {
                "uuid": "04211a2dc93547fe9bf6121eac533650",
            }
        }
    )
    plate12 = get_well_container("PlateT12")
    plate12.load_state({"Material": {"uuid": "04211a2dc93547fe9bf6121eac533650"}})
    plate13 = get_well_container("PlateT13")
    plate13.load_state(
        {
            "Material": {
                "uuid": "4a043a07c65a4f9bb97745e1f129b165",
                "Code": "ZX-58-0001",
                "SupplyType": 2,
                "Name": "全裙边 PCR适配器",
                "SummaryName": "全裙边 PCR适配器",
            }
        }
    ),
    plate14 = get_well_container("PlateT14")
    plate14.load_state(
        {
            "Material": {
                "uuid": "4a043a07c65a4f9bb97745e1f129b165",
                "Code": "ZX-58-0001",
                "SupplyType": 2,
                "Name": "全裙边 PCR适配器",
                "SummaryName": "全裙边 PCR适配器",
            }
        }
    ),
    plate15 = get_well_container("PlateT15")
    plate15.load_state({"Material": {"uuid": "04211a2dc93547fe9bf6121eac533650"}})

    trash = PRCXI9300Trash(name="trash", size_x=50, size_y=50, size_z=10, category="trash")
    trash.load_state({"Material": {"uuid": "730067cf07ae43849ddf4034299030e9"}})

    # container_for_nothing = PRCXI9300Container(name="container_for_nothing", size_x=50, size_y=50, size_z=10, category="plate", ordering=collections.OrderedDict())

    deck.assign_child_resource(plate1, location=Coordinate(0, 0, 0))
    deck.assign_child_resource(plate2, location=Coordinate(0, 0, 0))
    deck.assign_child_resource(
        PRCXI9300Plate(
            name="container_for_nothin3",
            size_x=50,
            size_y=50,
            size_z=10,
            category="plate",
            ordered_items=collections.OrderedDict(),
        ),
        location=Coordinate(0, 0, 0),
    )
    deck.assign_child_resource(plate4, location=Coordinate(0, 0, 0))
    deck.assign_child_resource(plate5, location=Coordinate(0, 0, 0))
    deck.assign_child_resource(plate6, location=Coordinate(0, 0, 0))
    deck.assign_child_resource(
        PRCXI9300Plate(
            name="container_for_nothing7",
            size_x=50,
            size_y=50,
            size_z=10,
            category="plate",
            ordered_items=collections.OrderedDict(),
        ),
        location=Coordinate(0, 0, 0),
    )
    deck.assign_child_resource(
        PRCXI9300Plate(
            name="container_for_nothing8",
            size_x=50,
            size_y=50,
            size_z=10,
            category="plate",
            ordered_items=collections.OrderedDict(),
        ),
        location=Coordinate(0, 0, 0),
    )
    deck.assign_child_resource(plate9, location=Coordinate(0, 0, 0))
    deck.assign_child_resource(plate10, location=Coordinate(0, 0, 0))
    deck.assign_child_resource(
        PRCXI9300Plate(
            name="container_for_nothing11",
            size_x=50,
            size_y=50,
            size_z=10,
            category="plate",
            ordered_items=collections.OrderedDict(),
        ),
        location=Coordinate(0, 0, 0),
    )
    deck.assign_child_resource(
        PRCXI9300Plate(
            name="container_for_nothing12",
            size_x=50,
            size_y=50,
            size_z=10,
            category="plate",
            ordered_items=collections.OrderedDict(),
        ),
        location=Coordinate(0, 0, 0),
    )
    deck.assign_child_resource(plate13, location=Coordinate(0, 0, 0))
    deck.assign_child_resource(plate14, location=Coordinate(0, 0, 0))
    deck.assign_child_resource(plate15, location=Coordinate(0, 0, 0))
    deck.assign_child_resource(trash, location=Coordinate(0, 0, 0))

    from unilabos.resources.graphio import tree_to_list, resource_plr_to_ulab

    A = tree_to_list([resource_plr_to_ulab(deck)])
    with open("deck.json", "w", encoding="utf-8") as f:
        A.insert(
            0,
            {
                "id": "PRCXI",
                "name": "PRCXI",
                "parent": None,
                "type": "device",
                "class": "liquid_handler.prcxi",
                "position": {"x": 0, "y": 0, "z": 0},
                "config": {
                    "deck": {
                        "_resource_child_name": "PRCXI_Deck",
                        "_resource_type": "unilabos.devices.liquid_handling.prcxi.prcxi:PRCXI9300Deck",
                    },
                    "host": "127.0.0.1",
                    "port": 9999,
                    "timeout": 10.0,
                    "axis": "Right",
                    "channel_num": 1,
                    "setup": False,
                    "debug": True,
                    "simulator": True,
                    "matrix_id": "e5f8fe93-cc58-4518-90f5-15682407724b",
                    "is_9320": True,
                },
                "data": {},
                "children": ["PRCXI_Deck"],
            },
        )
        A[1]["parent"] = "PRCXI"
        json.dump({"nodes": A, "links": []}, f, indent=4, ensure_ascii=False)

    handler = PRCXI9300Handler(
        deck=deck,
        host="127.0.0.1",
        port=9999,
        timeout=10.0,
        setup=True,
        debug=False,
        matrix_id="",
        channel_num=1,
        axis="Right",
        simulator=False,
        is_9320=True,
    )
    backend: PRCXI9300Backend = handler.backend
    from pylabrobot.resources import set_volume_tracking

    set_volume_tracking(enabled=True)
    # res = backend.api_client.get_all_materials()
    asyncio.run(handler.setup())  # Initialize the handler and setup the connection
    handler.set_tiprack([plate1, plate5])  # Set the tip rack for the handler
    handler.set_liquid([plate9.get_well("H12")], ["water"], [5])
    asyncio.run(handler.create_protocol(protocol_name="Test Protocol"))
    asyncio.run(handler.pick_up_tips([plate5.get_item("C5")], [0]))
    asyncio.run(handler.aspirate([plate9.get_item("H12")], [5], [0]))

    for well in plate13.get_all_items():
        # well_pos = well.name.split("_")[1]       # 走一行
        # if well_pos.startswith("A"):
        if well.name.startswith("PlateT13"):  # 走整个Plate
            asyncio.run(handler.dispense([well], [0.01], [0]))

    # asyncio.run(handler.dispense([plate10.get_item("H12")], [1], [0]))
    # asyncio.run(handler.dispense([plate13.get_item("A1")], [1], [0]))
    # asyncio.run(handler.dispense([plate14.get_item("C5")], [1], [0]))
    asyncio.run(handler.mix([plate10.get_item("H12")], mix_time=3, mix_vol=5))
    asyncio.run(handler.discard_tips([0]))
    asyncio.run(handler.run_protocol())
    time.sleep(5)
    os._exit(0)

    prcxi_api = PRCXI9300Api(host="127.0.0.1", port=9999)
    prcxi_api.list_matrices()
    prcxi_api.get_all_materials()

    # 第一种情景：一个孔往多个孔加液
    # plate_2_liquids = handler.set_group("water", [plate2.children[0]], [300])
    # plate5_liquids = handler.set_group("master_mix", plate5.children[:23], [100]*23)
    # 第二个情景：多个孔往多个孔加液(但是个数得对应)
    plate_2_liquids = handler.set_group("water", plate2.children[:23], [300] * 23)
    plate5_liquids = handler.set_group("master_mix", plate5.children[:23], [100] * 23)

    # plate11.set_well_liquids([("Water", 100) if (i % 8 == 0 and i // 8 < 6) else (None, 100) for i in range(96)])  # Set liquids for every 8 wells in plate8

    # plate11.set_well_liquids([("Water", 100) if (i % 8 == 0 and i // 8 < 6) else (None, 100) for i in range(96)])  # Set liquids for every 8 wells in plate8

    #     A = tree_to_list([resource_plr_to_ulab(deck)])
    #     # with open("deck.json", "w", encoding="utf-8") as f:
    #     #     json.dump(A, f, indent=4, ensure_ascii=False)

    #     print(plate11.get_well(0).tracker.get_used_volume())
    # Initialize the backend and setup the connection
    asyncio.run(handler.transfer_group("water", "master_mix", 10))  # Reset tip tracking

    # asyncio.run(handler.pick_up_tips([plate8.children[8]],[0]))
    # print(plate8.children[8])
    # asyncio.run(handler.run_protocol())
    # asyncio.run(handler.aspirate([plate11.children[0]],[10], [0]))
    # print(plate11.children[0])
    # # asyncio.run(handler.run_protocol())
    # asyncio.run(handler.dispense([plate1.children[0]],[10],[0]))
    # print(plate1.children[0])
    # asyncio.run(handler.run_protocol())
    # asyncio.run(handler.mix([plate1.children[0]], mix_time=3, mix_vol=5, height_to_bottom=0.5, offsets=Coordinate(0, 0, 0), mix_rate=100))
    # print(plate1.children[0])
    # asyncio.run(handler.discard_tips([0]))

    #     asyncio.run(handler.add_liquid(
    #     asp_vols=[10]*7,
    #     dis_vols=[10]*7,
    #     reagent_sources=plate11.children[:7],
    #     targets=plate1.children[2:9],
    #     use_channels=[0],
    #     flow_rates=[None] * 7,
    #     offsets=[Coordinate(0, 0, 0)] * 7,
    #     liquid_height=[None] * 7,
    #     blow_out_air_volume=[None] * 2,
    #     delays=None,
    #     mix_time=3,
    #     mix_vol=5,
    #     spread="custom",
    # ))

    # asyncio.run(handler.run_protocol())  # Run the protocol

    # # #     asyncio.run(handler.transfer_liquid(
    # # #     asp_vols=[10]*2,
    # # #     dis_vols=[10]*2,
    # # #     sources=plate11.children[:2],
    # # #     targets=plate11.children[-2:],
    # # #     use_channels=[0],
    # # #     offsets=[Coordinate(0, 0, 0)] * 4,
    # # #     liquid_height=[None] * 2,
    # # #     blow_out_air_volume=[None] * 2,
    # # #     delays=None,
    # # #     mix_times=3,
    # # #     mix_vol=5,
    # # #     spread="wide",
    # # #     tip_racks=[plate8]
    # # # ))

    # # #     asyncio.run(handler.remove_liquid(
    # # #     vols=[10]*2,
    # # #     sources=plate11.children[:2],
    # # #     waste_liquid=plate11.children[43],
    # # #     use_channels=[0],
    # # #     offsets=[Coordinate(0, 0, 0)] * 4,
    # # #     liquid_height=[None] * 2,
    # # #     blow_out_air_volume=[None] * 2,
    # # #     delays=None,
    # # #     spread="wide"
    # # # ))
    # #     asyncio.run(handler.run_protocol())

    # #     # asyncio.run(handler.discard_tips())
    # #     # asyncio.run(handler.mix(well_containers.children[:8
    # #     # ], mix_time=3, mix_vol=50, height_to_bottom=0.5, offsets=Coordinate(0, 0, 0), mix_rate=100))
    # #     #print(json.dumps(handler._unilabos_backend.steps_todo_list, indent=2))  # Print matrix info

    # #     # asyncio.run(handler.remove_liquid(
    # #     #     vols=[100]*16,
    # #     #     sources=well_containers.children[-16:],
    # #     #     waste_liquid=well_containers.children[:16], # 这个有些奇怪，但是好像也只能这么写
    # #     #     use_channels=[0, 1, 2, 3, 4, 5, 6, 7],
    # #     #     flow_rates=[None] * 32,
    # #     #     offsets=[Coordinate(0, 0, 0)] * 32,
    # #     #     liquid_height=[None] * 32,
    # #     #     blow_out_air_volume=[None] * 32,
    # #     #     spread="wide",
    # #     # ))
    # #     # asyncio.run(handler.transfer_liquid(
    # #     #     asp_vols=[100]*16,
    # #     #     dis_vols=[100]*16,
    # #     #     tip_racks=[tip_rack],
    # #     #     sources=well_containers.children[-16:],
    # #     #     targets=well_containers.children[:16],
    # #     #     use_channels=[0, 1, 2, 3, 4, 5, 6, 7],
    # #     #     offsets=[Coordinate(0, 0, 0)] * 32,
    # #     #     asp_flow_rates=[None] * 16,
    # #     #     dis_flow_rates=[None] * 16,
    # #     #     liquid_height=[None] * 32,
    # #     #     blow_out_air_volume=[None] * 32,
    # #     #     mix_times=3,
    # #     #     mix_vol=50,
    # #     #     spread="wide",
    # #     # ))
    #       # print(json.dumps(handler._unilabos_backend.steps_todo_list, indent=2))  # Print matrix info
    # #     # input("pick_up_tips add step")
    # asyncio.run(handler.run_protocol())  # Run the protocol
    # #     # input("Running protocol...")
    # #     # input("Press Enter to continue...")  # Wait for user input before proceeding
    # #     # print("PRCXI9300Handler initialized with deck and host settings.")

    # 一些推荐版位组合的测试样例：

    # 一些推荐版位组合的测试样例：

    with open("prcxi_material.json", "r") as f:
        material_info = json.load(f)

    layout = DefaultLayout("PRCXI9320")
    layout.add_lab_resource(material_info)
    MatrixLayout_1, dict_1 = layout.recommend_layout(
        [
            ("reagent_1", "96 细胞培养皿", 3),
            ("reagent_2", "12道储液槽", 1),
            ("reagent_3", "200μL Tip头", 7),
            ("reagent_4", "10μL加长 Tip头", 1),
        ]
    )
    print(dict_1)
    MatrixLayout_2, dict_2 = layout.recommend_layout(
        [
            ("reagent_1", "96深孔板", 4),
            ("reagent_2", "12道储液槽", 1),
            ("reagent_3", "200μL Tip头", 1),
            ("reagent_4", "10μL加长 Tip头", 1),
        ]
    )

# with open("prcxi_material.json", "r") as f:
#     material_info = json.load(f)

# layout = DefaultLayout("PRCXI9320")
# layout.add_lab_resource(material_info)
# MatrixLayout_1, dict_1 = layout.recommend_layout([
#     ("reagent_1", "96 细胞培养皿", 3),
#     ("reagent_2", "12道储液槽", 1),
#     ("reagent_3", "200μL Tip头", 7),
#     ("reagent_4", "10μL加长 Tip头", 1),
# ])
# print(dict_1)
# MatrixLayout_2, dict_2 = layout.recommend_layout([
#     ("reagent_1", "96深孔板", 4),
#     ("reagent_2", "12道储液槽", 1),
#     ("reagent_3", "200μL Tip头", 1),
#     ("reagent_4", "10μL加长 Tip头", 1),
# ])
