"""工作流（Workflow）与动作（Action）源码可使用的有限注解辅助类型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TypeAlias

JSONScalar: TypeAlias = None | bool | int | float | str
JSONValue: TypeAlias = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]


@dataclass(frozen=True, slots=True, init=False)
class AllowedResourceTemplates:
    """保留源码中的 ResourceTemplate symbol，不在注解层解析 UUID。"""

    resource_templates: tuple[object, ...]

    def __init__(self, *resource_templates: object) -> None:
        object.__setattr__(self, "resource_templates", tuple(resource_templates))


@dataclass(frozen=True, slots=True)
class MaterialLock:
    """已废弃：曾用于声明动作输入物料占位符（ResourceSlot）不取得物料锁。

    物料锁不再按参数豁免，每个物料入参都默认加共享资源锁。为让旧驱动照常导入与扫描，
    ``MaterialLock(free=True)`` 仍可写、仍按原语法校验，但注册表扫描会忽略它并告警。
    """

    free: bool

    def __post_init__(self) -> None:
        if self.free is not True:
            raise ValueError("MaterialLock 只接受显式 free=True")


def __getattr__(name: str):
    """已删除的 ``Placeholder`` / ``SiteSelector``：导入时直接报错并给出新写法。

    抛 ``ImportError``（不是 ``AttributeError``），``from ... import Placeholder`` 才会带着这句话失败。
    """

    from unilabos.registry.action_options import REMOVED_ANNOTATIONS, removed_annotation_message

    if name in REMOVED_ANNOTATIONS:
        raise ImportError(removed_annotation_message(name), name=name, path=__file__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
