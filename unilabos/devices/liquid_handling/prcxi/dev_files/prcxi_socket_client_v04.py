# -*- coding: utf-8 -*-
"""
PRCXI V04 标机 —— 纯 Socket 客户端（新版，不依赖 prcxi_sdk）
=============================================================

协议：自定义 JSON-RPC over TCP
帧格式：[ 8 字节长度头(大端) ] + [ UTF-8 JSON 正文 ]
连接模型：短连接，每次调用新建 socket，发一次、收一次、立即关闭。

与旧版 python_sdk_demo0603.py 的差异见文末「新旧接口对照」以及
docs/网络通信协议.md 第 9 节。

仅使用 Python 标准库（socket / json），Python 3.8+。
"""

from __future__ import annotations

import json
import socket
import time
from enum import Enum, auto
from typing import Any, Dict, List, Optional


# =============================================================
# 一、枚举（沿用旧版；服务端按“名称”识别，切勿修改项名）
# =============================================================
class MaterialEnum(Enum):
    Other = 0            # 其它
    Tips = 1             # 吸头
    DeepWellPlate = 2    # 深孔板
    PCRPlate = 3         # PCR 板
    ELISAPlate = 4       # 酶标板
    Reservoir = 5        # 储液槽
    WasteBox = 6         # 废弃盒


class AxisNum(Enum):
    Left = 1             # 左轴
    Right = 2            # 右轴
    ClampingJaw = 3      # 夹爪轴


class MajorFun(Enum):
    Load = auto()                 # 装载
    UnLoad = auto()               # 卸载
    Imbibing = auto()             # 吸液
    Tapping = auto()              # 放液
    Blending = auto()             # 混匀
    DefectiveLift = auto()        # 夹板
    PutDown = auto()              # 放板
    Shaking = auto()              # 振荡
    Incubation = auto()           # 孵育
    Magnetic = auto()             # 磁力架
    Shaking_Incubation = auto()   # 孵育+振荡


class MaterialType(Enum):
    BlankPipe = 0        # 空管
    Agentia = 1          # 试剂
    Used = 2             # 已编辑


class LiquidDispensingMethodEnum(Enum):
    NormalDispense = 0                    # 正常放液
    WallContactAfterDispense_Left = 3     # 放液后靠左壁
    WallContactAfterDispense_Right = 4    # 放液后靠右壁


class StepState(Enum):
    """GetStepStateList 返回的 State 数值含义。"""
    NotStarted = 0       # 未执行
    Running = 1          # 执行中
    Completed = 2        # 已完成

    @staticmethod
    def describe(value: Any) -> str:
        mapping = {"0": "未执行", "1": "执行中", "2": "已完成",
                   "None": "未执行", "Running": "执行中", "Completed": "已完成"}
        return mapping.get(str(value), str(value))


class DeviceErrorStatusFlags(Enum):
    """设备错误码 → 描述。"""
    DeviceNotConnected = 1
    XAxisOutOfRange = 17
    YAxisOutOfRange = 18
    ZAxisOutOfRange = 19
    PipetteAxisOutOfRange = 20
    CurrentPositionXZero = 33
    CurrentPositionYZero = 34
    CurrentPositionZZero = 35

    @staticmethod
    def get_description(error_code: Any) -> str:
        error_map = {
            1: "设备未连接(检查IP、端口或设备电源)",
            17: "X轴运动超出其允许的限位范围(手动复位X轴)",
            18: "Y轴运动超出其允许的限位范围(手动复位Y轴)",
            19: "Z轴运动超出其允许的限位范围(手动复位Z轴)",
            20: "移液轴运动超出其允许的限位范围(手动复位移液轴)",
            33: "当前板位的X轴位置为零(重新校准X轴位置)",
            34: "当前板位的Y轴位置为零(重新校准Y轴位置)",
            35: "当前板位的Z轴位置为零(重新校准Z轴位置)",
        }
        try:
            code = int(error_code)
        except (TypeError, ValueError):
            return f"未知错误码: {error_code}"
        return error_map.get(code, f"未知错误码: {code}")


# =============================================================
# 二、V04 工作台布局数据模型（对应 IMatrix *_V04 接口的 Board）
#     注意字段大小写：坐标类多为 camelCase，其余为 PascalCase。
# =============================================================
class PipettingPos:
    """移液位。"""

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
    ):
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
    """夹爪位。"""

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
    ):
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
    """板位定位信息。"""

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
    ):
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
    """板位明细。"""

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
    ):
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
    """工作台布局（V04 IMatrix 的一个 matrix）。"""

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
    ):
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

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "Board":
        return cls(
            id=payload.get("Id"),
            create_time=payload.get("CreateTime"),
            update_time=payload.get("UpdateTime"),
            name=payload.get("Name"),
            rows=int(payload.get("Rows") or 0),
            columns=int(payload.get("Columns") or 0),
            device_type=payload.get("DeviceType"),
        )


# =============================================================
# 三、旧版遗留数据模型（用于 AddSolution / 旧 IMatrix 更新接口）
#     ⚠ V04 服务端可能返回 NotImplementedException，使用前请确认固件支持。
# =============================================================
class MaterialEntity:
    """耗材实体（旧版）。"""

    def __init__(self, uuid: str, code: str = "", name: Optional[str] = None,
                 summary_name: Optional[str] = None, pipette_height: float = 0.0,
                 material_enum: Any = None, **extra: Any):
        self.uuid = uuid
        self.code = code
        self.name = name
        self.summary_name = summary_name
        self.pipette_height = pipette_height
        self.material_enum = material_enum
        self.extra = extra

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "uuid": self.uuid,
            "Code": self.code,
            "Name": self.name,
            "SummaryName": self.summary_name,
            "PipetteHeight": self.pipette_height,
            "materialEnum": (self.material_enum.value
                             if isinstance(self.material_enum, Enum) else self.material_enum),
        }
        data.update(self.extra)
        return data


class WorkTablet:
    """工作板位（旧版）。"""

    def __init__(self, number: int, material: Optional[MaterialEntity] = None,
                 code: Optional[str] = None, x_pos: float = 0.0, y_pos: float = 0.0,
                 z_pos: float = 0.0, z2_pos: float = 0.0, adapter: Optional[MaterialEntity] = None,
                 **extra: Any):
        self.number = number
        self.material = material
        self.code = code
        self.x_pos = x_pos
        self.y_pos = y_pos
        self.z_pos = z_pos
        self.z2_pos = z2_pos
        self.adapter = adapter
        self.extra = extra

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "Code": self.code,
            "Number": self.number,
            "XPos": self.x_pos,
            "YPos": self.y_pos,
            "ZPos": self.z_pos,
            "Z2Pos": self.z2_pos,
            "Material": self.material.to_dict() if self.material else None,
            "Adapter": self.adapter.to_dict() if self.adapter else None,
        }
        data.update(self.extra)
        return data


class WorkTabletMatrix:
    """工作板位矩阵（旧版）。"""

    def __init__(self, matrix_id: str, matrix_name: str, work_tablets: List[WorkTablet],
                 matrix_count: int = 0, length_two_edge: float = 139.0,
                 width_two_edge: float = 95.8, **extra: Any):
        self.matrix_id = matrix_id
        self.matrix_name = matrix_name
        self.work_tablets = work_tablets
        self.matrix_count = matrix_count
        self.length_two_edge = length_two_edge
        self.width_two_edge = width_two_edge
        self.extra = extra

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "LengthTwoEdge": self.length_two_edge,
            "WidthTwoEdge": self.width_two_edge,
            "MatrixId": self.matrix_id,
            "MatrixName": self.matrix_name,
            "MatrixCount": self.matrix_count,
            "WorkTablets": [wt.to_dict() for wt in self.work_tablets],
        }
        data.update(self.extra)
        return data


class Solution:
    def __init__(self, id="", create_time="", matrix_id="", plan_code="",
                 plan_name="", plan_targe="", annotate=""):
        self.id = id
        self.create_time = create_time
        self.matrix_id = matrix_id
        self.plan_code = plan_code
        self.plan_name = plan_name
        self.plan_targe = plan_targe
        self.annotate = annotate

    def to_dict(self) -> Dict[str, Any]:
        return {
            "Id": self.id, "CreateTime": self.create_time, "MatrixId": self.matrix_id,
            "PlanCode": self.plan_code, "PlanName": self.plan_name,
            "PlanTarge": self.plan_targe, "Annotate": self.annotate,
        }

    @classmethod
    def from_dict(cls, item: Dict[str, Any]) -> "Solution":
        return cls(
            id=item.get("Id"), create_time=item.get("CreateTime"),
            matrix_id=item.get("MatrixId"), plan_code=item.get("PlanCode"),
            plan_name=item.get("PlanName"), plan_targe=item.get("PlanTarge"),
            annotate=item.get("Annotate"),
        )


class StepData:
    """方案步骤（配合旧版 AddSolution）。"""

    def __init__(
        self,
        step_axis: AxisNum,
        function: MajorFun,
        dosage_num: float = 0.0,
        sequence_number: int = 0,
        plate_no: int = 0,
        hole_col: int = 0,
        hole_row: int = 0,
        is_whole_plate: bool = False,
        balance_height: int = 2,
        mate_type: MaterialType = MaterialType.BlankPipe,
        plate_or_hole_num: Optional[str] = None,
        assist_fun1: Optional[str] = None,
        assist_fun2: Optional[str] = None,
        assist_fun3: Optional[str] = None,
        assist_fun4: Optional[str] = None,
        assist_fun5: Optional[str] = None,
        hole_nums: Optional[List[int]] = None,
        hole_numbers: Optional[str] = None,
        liquid_dispensing_method: LiquidDispensingMethodEnum = LiquidDispensingMethodEnum.NormalDispense,
        dosage_speed: int = 1,
        blending_times: int = 0,
        hierarchy: int = 1,
    ):
        self.step_axis = step_axis
        self.function = function
        self.dosage_num = dosage_num
        self.sequence_number = sequence_number
        self.plate_no = plate_no
        self.hole_col = hole_col
        self.hole_row = hole_row
        self.is_whole_plate = is_whole_plate
        self.balance_height = balance_height
        self.mate_type = mate_type
        self.plate_or_hole_num = plate_or_hole_num
        self.assist_fun1 = assist_fun1
        self.assist_fun2 = assist_fun2
        self.assist_fun3 = assist_fun3
        self.assist_fun4 = assist_fun4
        self.assist_fun5 = assist_fun5
        self.hole_nums = hole_nums or []
        self.hole_numbers = hole_numbers
        self.liquid_dispensing_method = liquid_dispensing_method
        self.dosage_speed = dosage_speed
        self.blending_times = blending_times
        self.hierarchy = hierarchy

    def to_dict(self) -> Dict[str, Any]:
        return {
            "StepAxis": self.step_axis.name if self.step_axis else None,
            "Function": self.function.name if self.function else None,
            "DosageNum": self.dosage_num,
            "SequenceNumber": self.sequence_number,
            "PlateNo": self.plate_no,
            "HoleCol": self.hole_col,
            "HoleRow": self.hole_row,
            "IsWholePlate": self.is_whole_plate,
            "BalanceHeight": self.balance_height,
            "MateType": self.mate_type.name if self.mate_type else None,
            "PlateOrHoleNum": self.plate_or_hole_num,
            "AssistFun1": self.assist_fun1,
            "AssistFun2": self.assist_fun2,
            "AssistFun3": self.assist_fun3,
            "AssistFun4": self.assist_fun4,
            "AssistFun5": self.assist_fun5,
            "HoleNums": self.hole_nums,
            "HoleNumbers": self.hole_numbers,
            "LiquidDispensingMethod": (
                self.liquid_dispensing_method.name if self.liquid_dispensing_method else None
            ),
            "DosageSpeed": self.dosage_speed,
            "BlendingTimes": self.blending_times,
            "Hierarchy": self.hierarchy,
        }


# =============================================================
# 四、参数序列化辅助
# =============================================================
def to_rpc_value(value: Any) -> Any:
    """把 Python 对象转换为服务端兼容的 JSON 值。"""
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


def parse_data(response: Dict[str, Any]) -> Any:
    """取出 Data 并做二次解析（Data 常是 JSON 字符串）。"""
    data = response.get("Data")
    if isinstance(data, str):
        text = data.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return data
    return data


def as_bool(response: Dict[str, Any]) -> bool:
    """把返回的 Data 解析为布尔（兼容 'true'/'false'/0/1）。"""
    data = response.get("Data")
    if isinstance(data, bool):
        return data
    if isinstance(data, (int, float)):
        return bool(data)
    if isinstance(data, str):
        text = data.strip().strip('"').lower()
        return text in ("true", "1")
    return False


# =============================================================
# 五、纯 Socket RPC 客户端（新版）
# =============================================================
class PrcxiSocketClientV04:
    """PRCXI V04 标机纯 socket 客户端；每个方法都是一次独立的短连接 RPC。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 9999, timeout: float = 15.0, verbose: bool = True):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.verbose = verbose

    # ---------------- 传输层 ----------------
    @staticmethod
    def _recv_exact(sock: socket.socket, size: int) -> bytes:
        chunks: List[bytes] = []
        remaining = size
        while remaining > 0:
            chunk = sock.recv(remaining)
            if not chunk:
                raise ConnectionError(f"响应长度不完整：期望 {size} 字节，还差 {remaining} 字节")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _send(self, service_name: str, method_name: str,
              parameters: Optional[List[Any]] = None) -> Dict[str, Any]:
        """发送一条 RPC 指令并返回响应字典 {Success, Msg/Message, Data}。"""
        params = [to_rpc_value(p) for p in (parameters or [])]
        cmd = {"ServiceName": service_name, "MethodName": method_name, "Paramters": params}
        body = json.dumps(cmd, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        # 8 字节大端长度头（16 个十六进制字符）
        frame = bytes.fromhex(format(len(body), "016x")) + body
        rpc_name = f"{service_name}.{method_name}"
        if self.verbose:
            print(f"[SEND] {rpc_name} params={params}")

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect((self.host, self.port))
            sock.sendall(frame)
            header = self._recv_exact(sock, 8)
            payload_size = int.from_bytes(header, byteorder="big", signed=False)
            if payload_size <= 0:
                return {"Success": False, "Message": f"响应长度非法：{payload_size}", "Data": None}
            payload = self._recv_exact(sock, payload_size)
            response = json.loads(payload.decode("utf-8"))
            if self.verbose:
                print(f"[RECV] {rpc_name} Success={response.get('Success')} "
                      f"Msg={response.get('Message') or response.get('Msg')} Data={response.get('Data')}")
            return response
        except socket.timeout:
            return {"Success": False, "Message": f"连接超时({self.timeout}秒)", "Data": None}
        except ConnectionRefusedError:
            return {"Success": False, "Message": "设备拒绝连接，请检查 IP 和端口", "Data": None}
        except json.JSONDecodeError:
            return {"Success": False, "Message": "响应 JSON 解析失败", "Data": None}
        except Exception as exc:  # noqa: BLE001
            return {"Success": False, "Message": f"指令发送失败：{exc}", "Data": None}
        finally:
            try:
                sock.close()
            except OSError:
                pass

    # ---------------- 1. IClientSession（新版新增）----------------
    def is_connect(self) -> Dict[str, Any]:
        """判断设备是否处于连接状态。返回 Data 为 'true'/'false'。"""
        return self._send("IClientSession", "IsConnect", [])

    # ---------------- 2. IAutomation 运行控制 ----------------
    def automation_start(self) -> Dict[str, Any]:
        """启动已加载的方案（需先 load_solution）。"""
        return self._send("IAutomation", "Start", [])

    def automation_stop(self) -> Dict[str, Any]:
        """停止（停止后一般会自动复位）。"""
        return self._send("IAutomation", "Stop", [])

    def automation_pause(self) -> Dict[str, Any]:
        """暂停。"""
        return self._send("IAutomation", "Pause", [])

    def automation_resume(self) -> Dict[str, Any]:
        """继续。"""
        return self._send("IAutomation", "Resume", [])

    def automation_reset(self) -> Dict[str, Any]:
        """复位（各轴回初始位置）。"""
        return self._send("IAutomation", "Reset", [])

    def automation_get_error_code(self) -> Dict[str, Any]:
        """获取错误码。"""
        return self._send("IAutomation", "GetErrorCode", [])

    def automation_clear_error_code(self) -> Dict[str, Any]:
        """清空错误码（注意服务端拼写 RemoveErrorCodet）。"""
        return self._send("IAutomation", "RemoveErrorCodet", [])

    def automation_get_start_status(self) -> Dict[str, Any]:
        """（新版新增）是否运行中，Data 为 'true'/'false'。"""
        return self._send("IAutomation", "GetStartStatus", [])

    def automation_get_reset_status(self) -> Dict[str, Any]:
        """（新版新增）复位状态，Data 为 'true'/'false'。"""
        return self._send("IAutomation", "GetResetStatus", [])

    # ---------------- 3. ISolution 方案管理 ----------------
    def solution_get_list(self) -> Dict[str, Any]:
        """获取方案列表。"""
        return self._send("ISolution", "GetSolutionList", [])

    def solution_load(self, plan_name: str) -> Dict[str, Any]:
        """加载方案。⚠ 新版参数是【方案名 PlanName】，不是旧版的 solution_id。"""
        return self._send("ISolution", "LoadSolution", [plan_name])

    def solution_remove(self, plan_name: str) -> Dict[str, Any]:
        """（新版新增）删除方案（按方案名）。"""
        return self._send("ISolution", "RemoveSolution", [plan_name])

    # ---------------- 4. IMachineState 状态查询 ----------------
    def machine_state_get_step_list(self) -> Dict[str, Any]:
        """获取全部步骤状态列表。"""
        return self._send("IMachineState", "GetStepStateList", [])

    def machine_state_get_step_status(self, step_no: int) -> Dict[str, Any]:
        """获取指定步骤的详细状态（开始/结束时间等）。"""
        return self._send("IMachineState", "GetStepStatus", [step_no])

    def machine_state_get_step_state(self, step_no: int) -> Dict[str, Any]:
        """获取指定步骤的运行状态。"""
        return self._send("IMachineState", "GetStepState", [step_no])

    def machine_state_get_location(self, axis_no: int) -> Dict[str, Any]:
        """获取指定轴位置（单位 mm）。"""
        return self._send("IMachineState", "GetLocation", [axis_no])

    def machine_state_get_location_list(self) -> Dict[str, Any]:
        """（新版新增）获取全部轴位置。"""
        return self._send("IMachineState", "GetLocationList", [])

    # ---------------- 5. IMatrix 布局（仅 _V04 接口）----------------
    def matrix_get_all(self) -> Dict[str, Any]:
        """获取全部布局。"""
        return self._send("IMatrix", "GetWorkTabletMatrices_V04", [])

    def matrix_get_by_id(self, matrix_id: str) -> Dict[str, Any]:
        """按 ID 获取布局。"""
        return self._send("IMatrix", "GetWorkTabletMatrixById_V04", [matrix_id])

    def matrix_add(self, board: Board) -> Dict[str, Any]:
        """新增布局。参数为 Board 对象。"""
        return self._send("IMatrix", "AddWorkTabletMatrix_V04", [board])

    def matrix_remove(self, matrix_id: str) -> Dict[str, Any]:
        """（新版新增）删除布局。"""
        return self._send("IMatrix", "RemoveWorkTabletMatrix_V04", [matrix_id])

    def matrix_update_position(self, board: Board) -> Dict[str, Any]:
        """更新布局位置（替代旧版 UpdateClampJawPosition / UpdatePipettingPosition）。"""
        return self._send("IMatrix", "UpdatePosition_V04", [board])

    def matrix_get_all_material(self) -> Dict[str, Any]:
        """获取全部耗材。"""
        return self._send("IMatrix", "GetAllMaterial_V04", [])

    def matrix_get_material_by_id(self, material_id: str) -> Dict[str, Any]:
        """（新版新增）按 ID 获取耗材。"""
        return self._send("IMatrix", "GetMaterialById_V04", [material_id])

    # =========================================================
    # 六、旧版遗留接口（V04 未在新版 SDK 中封装）
    # ⚠ V04 服务端很可能返回 NotImplementedException / Success=False。
    #    保留仅为对照旧版 python_sdk_demo0603.py，请勿在 V04 直接依赖。
    # =========================================================
    def legacy_solution_add(self, solution_name: str, matrix_id: str,
                            solution_content: List["StepData"]) -> Dict[str, Any]:
        """【旧版】按步骤列表创建方案（V04 改为生成 XML 方案文件，未走此 RPC）。"""
        content = [s.to_dict() for s in solution_content]
        return self._send("ISolution", "AddSolution", [solution_name, matrix_id, content])

    def legacy_matrix_get_all(self) -> Dict[str, Any]:
        """【旧版】无 _V04 后缀，V04 会抛 NotImplementedException。"""
        return self._send("IMatrix", "GetWorkTabletMatrices", [])

    def legacy_matrix_get_by_id(self, matrix_id: str) -> Dict[str, Any]:
        """【旧版】无 _V04 后缀。"""
        return self._send("IMatrix", "GetWorkTabletMatrixById", [matrix_id])

    def legacy_matrix_add(self, matrix: "WorkTabletMatrix") -> Dict[str, Any]:
        """【旧版】AddWorkTabletMatrix2，V04 用 AddWorkTabletMatrix_V04 替代。"""
        return self._send("IMatrix", "AddWorkTabletMatrix2", [matrix])

    def legacy_matrix_get_all_material(self) -> Dict[str, Any]:
        """【旧版】无 _V04 后缀。"""
        return self._send("IMatrix", "GetAllMaterial", [])

    def legacy_matrix_update_clampjaw_position(self, matrix: Any) -> Dict[str, Any]:
        """【旧版】更新夹爪板位位置，V04 用 UpdatePosition_V04 替代。"""
        return self._send("IMatrix", "UpdateClampJawPosition", [matrix])

    def legacy_matrix_update_pipetting_position(self, matrix: Any) -> Dict[str, Any]:
        """【旧版】更新移液轴板位位置，V04 用 UpdatePosition_V04 替代。"""
        return self._send("IMatrix", "UpdatePipettingPosition", [matrix])


# =============================================================
# 七、使用示例（默认只做只读/查询，避免误触发设备动作）
# =============================================================
if __name__ == "__main__":
    sdk = PrcxiSocketClientV04(host="127.0.0.1", port=9999, timeout=15)

    # 1. 连接判定（新版连接语义 = IClientSession.IsConnect）
    print("\n=== 1. 判断连接 ===")
    resp = sdk.is_connect()
    if not (resp.get("Success") and as_bool(resp)):
        print("设备未连接：", resp.get("Message") or resp.get("Msg"))
        raise SystemExit(1)
    print("设备已连接")

    # 2. 设备状态（新版新增的三个轮询接口）
    print("\n=== 2. 设备状态 ===")
    print("运行中：", as_bool(sdk.automation_get_start_status()))
    print("复位状态：", as_bool(sdk.automation_get_reset_status()))
    err = sdk.automation_get_error_code()
    print("错误码：", parse_data(err))

    # 3. 获取方案列表
    print("\n=== 3. 获取方案列表 ===")
    solutions_resp = sdk.solution_get_list()
    solutions = parse_data(solutions_resp) or []
    print(f"共 {len(solutions)} 个方案")
    for item in solutions[:5]:
        print("  -", item.get("PlanName"))

    # 4. 获取布局（V04 _V04 接口）
    print("\n=== 4. 获取布局 ===")
    boards_resp = sdk.matrix_get_all()
    boards = parse_data(boards_resp) or []
    print(f"共 {len(boards)} 个布局")

    # 5. 加载方案 + 步骤状态（新版按【方案名】加载）
    if solutions:
        plan_name = solutions[0].get("PlanName")
        print(f"\n=== 5. 加载方案：{plan_name} ===")
        load_resp = sdk.solution_load(plan_name)
        if as_bool(load_resp):
            steps = parse_data(sdk.machine_state_get_step_list()) or []
            for s in steps:
                print(f"  步骤{s.get('SequenceNumber')} {s.get('Name')}："
                      f"{StepState.describe(s.get('State'))}")

    # 6. 运行控制（默认注释，避免误动作；确认安全后再放开）
    # print("\n=== 6. 启动 ===")
    # print("启动：", as_bool(sdk.automation_start()))
    # for _ in range(5):
    #     time.sleep(1)
    #     print("运行中：", as_bool(sdk.automation_get_start_status()))
    # print("停止：", as_bool(sdk.automation_stop()))
    # print("复位：", as_bool(sdk.automation_reset()))

    # 7. 布局写操作示例（默认注释）
    # board = Board(name="SC9320", rows=4, columns=7, device_type="SC9320")
    # print("新增布局：", sdk.matrix_add(board))
    # print("更新位置：", sdk.matrix_update_position(board))
    # print("删除布局：", sdk.matrix_remove("<matrix_id>"))

    # 8. 旧版 AddSolution（V04 未封装，仅示意，默认注释）
    # steps = [StepData(step_axis=AxisNum.Left, function=MajorFun.Load, dosage_num=20,
    #                   plate_no=1, hole_row=1, hole_col=1, plate_or_hole_num="H1-8,T1",
    #                   hole_nums=[1, 2, 3, 4, 5, 6, 7, 8])]
    # print(sdk.legacy_solution_add("示例方案", "<matrix_id>", steps))
