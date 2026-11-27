"""显式冻结的中心扩展能力；不按参数名称推断设备已支持协议。"""
from collections.abc import Mapping
from typing import Any

CAPABILITIES = frozenset({'center_transport.placement.v2'})

def validate_center_capabilities(value: Any) -> list[str]:
    if (not isinstance(value, list) or len(value) > 32
            or any(not isinstance(item, str) or item not in CAPABILITIES for item in value)
            or len(set(value)) != len(value)):
        raise ValueError('中心扩展能力声明无效或包含未知能力')
    return list(value)

def capabilities_from_graph(graph: Mapping[str, Any]) -> list[str]:
    return validate_center_capabilities(graph['workflow']['meta_data'].get('center_capabilities', []))
