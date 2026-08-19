"""物料查询/写入权威来源的统一归一化规则。"""

from __future__ import annotations

from typing import Literal


MaterialSource = Literal["microbackend", "backend", "auto"]


def normalize_material_source(value: object) -> MaterialSource:
    """把配置别名归一化为对外稳定的三值枚举。

    历史配置中的 edge/local 与 cloud/remote 仍被接受。未知值按本地
    microbackend 处理，确保健康检查和本地写保护采用同一套保守语义。
    """

    source = str(value or "microbackend").strip().lower()
    source = {
        "edge": "microbackend",
        "local": "microbackend",
        "cloud": "backend",
        "remote": "backend",
    }.get(source, source)
    if source not in {"microbackend", "backend", "auto"}:
        return "microbackend"
    return source  # type: ignore[return-value]
