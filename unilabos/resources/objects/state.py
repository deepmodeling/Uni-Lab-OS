"""资源根级运行态的公共类型。"""

from typing import Optional, Tuple


SubstanceStateEntry = Tuple[str, float, str]
# 兼容仍引用旧类型名的插件；运行时的规范字段已经是 substances。
LiquidStateEntry = SubstanceStateEntry
LiquidHistoryEntry = Tuple[Optional[str], float, str]

# PLR Container.serialize_state() 中属于物质运行态的字段。它们像 barcode 一样
# 在 ResourceDict 中只保留根字段；max_volume/thing 仍留在 data。
TRACKER_STATE_KEYS = ("substances", "liquid_history", "unknown_counter")


__all__ = [
    "LiquidHistoryEntry",
    "LiquidStateEntry",
    "SubstanceStateEntry",
    "TRACKER_STATE_KEYS",
]
