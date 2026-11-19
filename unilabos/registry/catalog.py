"""本进程看到的注册表目录（设备 / 资源条目，按条目 id 索引）。

调度权威不扫描注册表：它的目录是 Registry Authority 的生效条目（Host 上报、按接受规则维护，
``server/services/runtime/registry.py``）。Host / Slave 进程没有 Registry Authority，读本地扫描的
``lab_registry``。单进程（``--no-safe-restart``）两者都有，以 Registry Authority 为准：它就是本进程
扫描结果的进程内上报（单机，全部自动接受）。

两种来源的条目形状不同（上报 payload 是 ``legacy_adaptor/sync/templates.py`` 的投影），这里的
调用方只读两者共有的字段：``class.module`` / ``class.type`` / ``class.action_value_mappings``、
``display_name``、``available_sites``。
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional


class RegistryNotReported(LookupError):
    """调度权威还没收到 Host 的注册表上报（全新库），目录为空。"""

    code = "registry_not_reported"

    def __init__(self, action: str = "") -> None:
        message = "注册表尚未上报：调度权威不扫描注册表，等待 Host 启动并上报设备注册表后重试"
        super().__init__(f"{message}（{action}）" if action else message)


def authority_registry() -> Optional[Any]:
    """本进程的 Registry Authority（调度权威 / 单进程）；Host / Slave 进程为 None。"""

    try:
        from unilabos.server.services.runtime.registry import get_registry_service
    except Exception:  # noqa: BLE001 - 精简环境没有服务层
        return None
    return get_registry_service()


def _local_table(name: str) -> Dict[str, Mapping[str, Any]]:
    try:
        from unilabos.registry.registry import lab_registry
    except Exception:  # noqa: BLE001 - 注册表模块不可用（缺 ROS 消息包等）视为空目录
        return {}
    table = getattr(lab_registry, name, None) or {}
    return {str(key): value for key, value in table.items() if isinstance(value, Mapping)}


def _entries(registry_type: str, local_table: str) -> Dict[str, Mapping[str, Any]]:
    registry = authority_registry()
    if registry is None:
        return _local_table(local_table)
    return {
        str(payload.get("id") or ""): payload
        for payload in registry.active_payloads(registry_type)
        if str(payload.get("id") or "")
    }


def device_entries() -> Dict[str, Mapping[str, Any]]:
    """设备条目：调度权威读生效条目，Host 读本地扫描。"""

    return _entries("device", "device_type_registry")


def resource_entries() -> Dict[str, Mapping[str, Any]]:
    """资源条目：调度权威读生效条目，Host 读本地扫描。"""

    return _entries("resource", "resource_type_registry")


def _entry(name: str, registry_type: str, local_table: str) -> Optional[Mapping[str, Any]]:
    registry = authority_registry()
    if registry is None:
        try:
            from unilabos.registry.registry import lab_registry
        except Exception:  # noqa: BLE001 - 注册表模块不可用视为空目录
            return None
        entry = (getattr(lab_registry, local_table, None) or {}).get(str(name or ""))
        return entry if isinstance(entry, Mapping) else None
    return registry.active_payload(name, registry_type)


def device_entry(name: str) -> Optional[Mapping[str, Any]]:
    """单个设备条目（不复制整份目录）。"""

    return _entry(name, "device", "device_type_registry")


def resource_entry(name: str) -> Optional[Mapping[str, Any]]:
    """单个资源条目（不复制整份目录）。"""

    return _entry(name, "resource", "resource_type_registry")


def catalog_reported() -> bool:
    """目录是否可用：Host 进程总是可用（本地扫描）；调度权威要收到过上报。"""

    registry = authority_registry()
    return registry is None or bool(registry.has_report())


def require_reported(action: str = "") -> None:
    """调度权威还没收到过上报时抛 :class:`RegistryNotReported`。"""

    if not catalog_reported():
        raise RegistryNotReported(action)


__all__ = [
    "RegistryNotReported",
    "authority_registry",
    "catalog_reported",
    "device_entries",
    "device_entry",
    "require_reported",
    "resource_entries",
    "resource_entry",
]
