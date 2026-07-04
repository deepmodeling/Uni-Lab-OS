"""S06 加溶液工位 OPC 变量映射。

变量表见同目录 ``pump_nodes.csv``（pump 专用最小集）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

CSV_REFERENCE = str(Path(__file__).resolve().parent / "pump_nodes.csv")

# CSV 行 60：加溶剂检测（加液位烧杯）
ADDITION_BEAKER_SENSOR = "传感器状态_上位机[3].NO[1]"

# 新 CSV：S06 加溶液工位握手与参数
S06_READY_VAR = "S06准备信号"
S06_ALLOW_PROCESS_VAR = "S06允许加工"
S06_PARAM_WRITTEN_VAR = "S06参数写入完成"
S06_DONE_VAR = "S06加工完成"
S06_PROCESS_SELECT_VAR = "S06工艺选择"

S06PipelineKind = Literal["aspirate", "dispense", "air"]

# CSV 仅有液体试剂瓶在位检测（S10），无储液瓶液位点位；液量充足由 PLC 置位 S06允许加工
STORAGE_BOTTLE_PRESENT: dict[int, str] = {
    1: "传感器状态_上位机[4].NO[12]",  # 液体试剂瓶1-1
    2: "传感器状态_上位机[5].NO[1]",  # 液体试剂瓶2-1
}

# CSV 行 188-189：机器人夹爪取放料位置号_烧杯（PC→PLC）
ROBOT_BEAKER_PICK_VAR = "S03_1取料编号"
ROBOT_BEAKER_PLACE_VAR = "S03_1放料编号"


def s06_solution_amount_var(pump: int) -> str:
    return f"S06_{pump}号溶液添加量"


@dataclass(frozen=True)
class S06PipelineRoute:
    """写入 S06注射泵{n}控制阀 / 绝对位置控制 的路由参数（阀位值待 PLC 确认）。"""

    control_valve: int
    absolute_position: int


def s06_pump_valve_var(pump: int) -> str:
    return f"S06注射泵{pump}控制阀"


def s06_pump_position_var(pump: int) -> str:
    return f"S06注射泵{pump}绝对位置控制"


def s06_pump_aspirate_var(pump: int) -> str:
    return f"S06注射泵{pump}抽液"


def s06_pump_dispense_var(pump: int) -> str:
    return f"S06注射泵{pump}排液"


def parse_pipeline_route_specs(
    specs: list[dict[str, Any]] | None,
    *,
    base: dict[tuple[int, S06PipelineKind], S06PipelineRoute] | None = None,
) -> dict[tuple[int, S06PipelineKind], S06PipelineRoute]:
    """从 graph JSON 中的 ``pipeline_route_specs`` 列表解析阀位路由。"""
    routes = dict(base or default_s06_pipeline_routes())
    if not specs:
        return routes
    for item in specs:
        pump = int(item["pump"])
        pipeline = item["pipeline"]
        routes[(pump, pipeline)] = S06PipelineRoute(  # type: ignore[index]
            control_valve=int(item["control_valve"]),
            absolute_position=int(item["absolute_position"]),
        )
    return routes


def default_s06_pipeline_routes() -> dict[tuple[int, S06PipelineKind], S06PipelineRoute]:
    """泵 × 管路默认阀位。"""
    routes: dict[tuple[int, S06PipelineKind], S06PipelineRoute] = {}
    for pump in (1, 2):
        for kind in ("aspirate", "dispense", "air"):
            routes[(pump, kind)] = S06PipelineRoute(control_valve=0, absolute_position=0)
    return routes
