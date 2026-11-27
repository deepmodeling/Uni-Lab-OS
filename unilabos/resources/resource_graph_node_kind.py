"""资源图节点种类的轻量判定。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

SITE_RESOURCE_TYPES = frozenset({"well", "tipspot", "tip_spot", "tip-spot"})
_NORMALIZED_SITE_RESOURCE_TYPES = frozenset(
    value.replace("-", "_").casefold() for value in SITE_RESOURCE_TYPES
)


def is_site_resource_node(node: Mapping[str, Any]) -> bool:
    """判断资源图节点是否表达 Backend Site。

    ``node`` 可用根 ``type``/``class`` 或 ``config.category``/``config.type``
    声明类型。判定与库存首次投影共享，避免 Edge 注册把 Site 当作 Material。
    """

    raw_config = node.get("config")
    config = raw_config if isinstance(raw_config, Mapping) else {}
    candidates = {
        str(node.get("type") or "").replace("-", "_").casefold(),
        str(node.get("class") or "").replace("-", "_").casefold(),
        str(config.get("category") or "").replace("-", "_").casefold(),
        str(config.get("type") or "").replace("-", "_").casefold(),
    }
    return bool(candidates & _NORMALIZED_SITE_RESOURCE_TYPES)
