"""Host 侧代理 Slave 的 ``material.template.list``：解码载荷 → 权威 gateway → JSON 应答。

hostlink backend（``backend.py``）与 ROS2 组网服务（``network.py``）各注册一套物料
处理器，模板列表共用这里的实现，两边对载荷的理解才不会漂移。

筛选在权威侧完成：``name`` 精确匹配、``include_definition=False`` 只取目录字段。
Slave 的常见需求（"模板存在吗" / "按名取 uuid"）只需要几百字节，而不加参数的全量
列表要把整个注册表的 definition（十几 MB）拖过链路，所以对这种调用记一次告警提示。
"""

from __future__ import annotations

import threading
from typing import Any, Dict, List, Mapping, Optional, Set

from unilabos.utils import logger

_warned_peers: Set[str] = set()
_warned_lock = threading.Lock()


def template_list_query(data: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """``material.template.list`` 载荷 → ``gateway.list_templates`` 的关键字参数。"""

    data = data or {}
    name = data.get("name")
    return {
        "name": name if isinstance(name, str) and name else None,
        # 链路默认目录模式：不带 include_definition 的对端拿到的是 name/uuid/hash，
        # 不是全注册表的 definition
        "include_definition": bool(data.get("include_definition", False)),
    }


def template_list(
    gateway: Any, data: Optional[Mapping[str, Any]], peer: Mapping[str, Any]
) -> List[Dict[str, Any]]:
    query = template_list_query(data)
    items = gateway.list_templates(**query)
    if query["include_definition"] and query["name"] is None:
        _warn_full_catalog(peer, len(items))
    return [item.model_dump(mode="json", exclude_none=False) for item in items]


def _warn_full_catalog(peer: Mapping[str, Any], count: int) -> None:
    key = str(peer.get("node_id") or peer.get("addr") or "")
    with _warned_lock:
        if key in _warned_peers:
            return
        _warned_peers.add(key)
    logger.warning(
        "[HostLink] %s 请求了全部 %d 个模板的完整 definition（整个注册表可达十几 MB）。"
        "存在性检查 / 按名取 uuid / 比 definition_hash 用默认的目录模式即可，"
        "确实要读 definition 正文请带 name= 按需取",
        peer.get("machine_name") or key or "slave",
        count,
    )


__all__ = ["template_list", "template_list_query"]
