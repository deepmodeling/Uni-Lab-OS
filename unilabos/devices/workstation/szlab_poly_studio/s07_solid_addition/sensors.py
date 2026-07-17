"""S07 固体加料工位 OPC 变量映射。

变量表见同目录 ``s07_nodes.csv``（S07 专用最小集）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from unilabos.devices.workstation.szlab_poly_studio.sensor import S07Sensors


CSV_REFERENCE = str(Path(__file__).resolve().parent / "s07_nodes.csv")

NODE_STATION_STATUS = "工站状态[6]"
NODE_HOME = "S07原点信号"
NODE_ALLOW_PROCESS = "S07允许加工"
NODE_PROCESS_SELECT = "S07工艺选择"
NODE_PARAMS_WRITTEN = "S07参数写入完成"
NODE_PROCESS_COMPLETE = "S07工艺完成"

NODE_LOAD_POSITION = "S07上下料位置"
NODE_COARSE_POSITION = "S07粗注粉位置号"
NODE_FINE_POSITION = "S07精注粉位置号"
NODE_TARGET_WEIGHT = "S07注粉重量"
NODE_COARSE_SHAKE_MAX_SPEED = "S07_粗注粉震荡最高速度"
NODE_FINE_SHAKE_MAX_SPEED = "S07_精注粉震荡最高速度"

ROBOT_S071_PICK_PLACE_VAR = "S071取放料编号"
ROBOT_S072_PRODUCT_VAR = "S072取放料产品"

POWDER_CONTAINER_SENSORS = S07Sensors.POWDER_CONTAINER_BY_INDEX

PROCESS_SCAN_CARTRIDGES = 1
PROCESS_ROTATE_TO_FEED = 2
PROCESS_DOSE_POWDER = 3

POSITION_RANGE = range(1, 11)
QR_CODE_LENGTH = 30
POWDER_PARAM_LENGTH = 5


def s07_qr_code_var(position: int, index: int) -> str:
    if position not in POSITION_RANGE:
        raise ValueError(f"position 必须在 1-10 范围内，收到: {position}")
    if index not in range(QR_CODE_LENGTH):
        raise ValueError(f"二维码下标必须在 0-{QR_CODE_LENGTH - 1} 范围内，收到: {index}")
    return f"S07位置{position}二维码[{index}]"


def s07_powder_param_var(kind: str, field: str, index: int) -> str:
    if kind not in {"粗注粉", "精注粉"}:
        raise ValueError(f"注粉类型必须是 粗注粉 或 精注粉，收到: {kind}")
    if field not in {"开口量", "落粉匀速", "旋转速度", "提请停止量"}:
        raise ValueError(f"未知注粉参数: {field}")
    if index not in range(POWDER_PARAM_LENGTH):
        raise ValueError(f"注粉参数下标必须在 0-{POWDER_PARAM_LENGTH - 1} 范围内，收到: {index}")
    return f"S07_{kind}{field}[{index}]"


def iter_s07_powder_param_vars() -> list[tuple[str, float | int]]:
    reset_values: list[tuple[str, float | int]] = []
    for kind in ("粗注粉", "精注粉"):
        for field in ("开口量", "旋转速度", "提请停止量"):
            reset_values.extend((s07_powder_param_var(kind, field, index), 0) for index in range(POWDER_PARAM_LENGTH))
        reset_values.extend((s07_powder_param_var(kind, "落粉匀速", index), 0.0) for index in range(POWDER_PARAM_LENGTH))
    reset_values.extend(
        [
            (NODE_COARSE_SHAKE_MAX_SPEED, 0),
            (NODE_FINE_SHAKE_MAX_SPEED, 0),
        ]
    )
    return reset_values


def normalize_powder_params(params: dict[str, Any] | None) -> dict[str, list[float | int] | int]:
    params = dict(params or {})
    return {
        "opening": list(params.get("opening", [0] * POWDER_PARAM_LENGTH)),
        "feed_speed": list(params.get("feed_speed", [0.0] * POWDER_PARAM_LENGTH)),
        "rotation_speed": list(params.get("rotation_speed", [0] * POWDER_PARAM_LENGTH)),
        "stop_amount": list(params.get("stop_amount", [0] * POWDER_PARAM_LENGTH)),
        "shake_max_speed": int(params.get("shake_max_speed", 0)),
    }
