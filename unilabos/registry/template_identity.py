"""设备目录（Device Catalog）的确定性 UUID 身份规则。"""

from __future__ import annotations

import unicodedata
from uuid import UUID, uuid5

# 这些 UUID 是公开持久身份合同的一部分。发布后不得修改，也不得按部署环境覆盖。
DEVICE_TEMPLATE_NAMESPACE = UUID("5b574efe-d13b-5578-aa1e-5b6a65c70b81")
DEVICE_ACTION_TEMPLATE_NAMESPACE = UUID("6efd5d11-867c-5553-b483-19efbc7cbf18")
DEVICE_ACTION_HANDLE_NAMESPACE = UUID("92317222-ea3c-56a8-a922-f555dde14637")
DEVICE_RESOURCE_HANDLE_NAMESPACE = UUID("5ca7087f-102a-51ed-b035-94233f26976b")


class TemplateIdentityError(ValueError):
    """设备、动作或连接点缺少可重复生成的稳定业务身份。"""


def device_template_uuid(device_name: str) -> str:
    """由全局唯一设备业务名生成稳定设备模板 UUID。

    参数：``device_name`` 是领域包声明的稳定 ``name``，不是可变展示名称。
    返回：跨进程、跨机器和跨重启一致的 UUIDv5 字符串。
    异常：名称为空、包含首尾空白、控制字符或不是 NFC 规范形式时抛出
    ``TemplateIdentityError``。
    """

    return str(uuid5(DEVICE_TEMPLATE_NAMESPACE, _stable_name(device_name, "设备")))


def action_template_uuid(device_name: str, action_name: str) -> str:
    """由设备名和动作业务名生成稳定动作模板 UUID。

    参数：``device_name`` 与 ``action_name`` 都是 AST 静态目录中的稳定业务名。
    返回：不依赖 SQLite、安装路径、包版本或启动时间的 UUIDv5 字符串。
    """

    return str(
        uuid5(
            DEVICE_ACTION_TEMPLATE_NAMESPACE,
            action_template_key(device_name, action_name),
        )
    )


def action_template_key(device_name: str, action_name: str) -> str:
    """生成 ``设备名.动作名`` 形式的规范内存目录键。

    参数：两个名称来自同一 AST 设备聚合。返回：可直接持久化到工作流节点引用
    字段的稳定键；动作名不能包含点号，设备名可保留领域命名空间前缀。
    """

    device_identity = _stable_name(device_name, "设备")
    action_identity = _stable_name(action_name, "动作")
    if "." in action_identity:
        raise TemplateIdentityError("动作业务名不能包含点号")
    return f"{device_identity}.{action_identity}"


def action_handle_uuid(
    action_uuid: str,
    *,
    io_type: str,
    handle_key: str,
) -> str:
    """由父动作、方向和连接点业务键生成稳定 Handle UUID。

    参数：``action_uuid`` 是已经确定的动作模板 UUID；``io_type`` 只能是
    ``source`` 或 ``target``；``handle_key`` 是动作合同内稳定连接点业务键。
    返回：确定性 UUIDv5 字符串。
    """

    try:
        parent_uuid = UUID(action_uuid)
    except (AttributeError, TypeError, ValueError):
        raise TemplateIdentityError("动作模板 UUID 非法") from None
    if io_type not in {"source", "target"}:
        raise TemplateIdentityError("连接点方向必须是 source 或 target")
    key = _stable_name(handle_key, "连接点")
    return str(
        uuid5(
            DEVICE_ACTION_HANDLE_NAMESPACE,
            f"{parent_uuid}\u001f{io_type}\u001f{key}",
        )
    )


def device_resource_handle_uuid(
    device_name: str,
    *,
    io_type: str,
    handle_name: str,
) -> str:
    """由设备名、方向和资源连接点名生成稳定资源 Handle UUID。

    参数：``device_name`` 是设备模板稳定业务名；``io_type`` 是 Backend 资源
    Handle 方向；``handle_name`` 是设备模板内的连接点业务名。返回：不依赖
    SQLite 的确定性 UUIDv5 字符串。
    """

    if io_type not in {"source", "target", "bidirectional"}:
        raise TemplateIdentityError("资源连接点方向非法")
    return str(
        uuid5(
            DEVICE_RESOURCE_HANDLE_NAMESPACE,
            "\u001f".join(
                (
                    _stable_name(device_name, "设备"),
                    io_type,
                    _stable_name(handle_name, "连接点"),
                )
            ),
        )
    )


def _stable_name(value: str, label: str) -> str:
    """校验一个直接参与 UUIDv5 的规范业务名。

    参数：``value`` 是领域声明原值，``label`` 用于稳定错误消息。返回未经改写的
    NFC 名称；拒绝隐式 trim 或 Unicode 归一化，避免两个进程采用不同身份。
    """

    if not isinstance(value, str) or not value:
        raise TemplateIdentityError(f"{label}业务名不能为空")
    if value != value.strip():
        raise TemplateIdentityError(f"{label}业务名不能包含首尾空白")
    if unicodedata.normalize("NFC", value) != value:
        raise TemplateIdentityError(f"{label}业务名必须使用 NFC 规范形式")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise TemplateIdentityError(f"{label}业务名不能包含控制字符")
    return value


__all__ = [
    "DEVICE_ACTION_HANDLE_NAMESPACE",
    "DEVICE_ACTION_TEMPLATE_NAMESPACE",
    "DEVICE_RESOURCE_HANDLE_NAMESPACE",
    "DEVICE_TEMPLATE_NAMESPACE",
    "TemplateIdentityError",
    "action_handle_uuid",
    "action_template_key",
    "action_template_uuid",
    "device_resource_handle_uuid",
    "device_template_uuid",
]
