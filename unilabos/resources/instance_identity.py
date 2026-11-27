"""生产资源实例身份的规范化规则。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def normalize_resource_instance_barcode(raw_barcode: Any, local_id: str) -> str:
    """返回 Backend 实例同步、Edge 注册与资源水合共用的稳定条码。"""

    if isinstance(raw_barcode, Mapping):
        raw_barcode = raw_barcode.get("data")
    barcode = str(raw_barcode or "").strip()
    return barcode or f"UNILAB-GRAPH-{local_id}"
