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


@dataclass(frozen=True, slots=True, kw_only=True)
class SiteSelector:
    """声明字符串参数由库位选择器（SiteSelector）提供。

    参数说明：``owner`` 指向拥有候选库位（Site）的物料占位符
    （ResourceSlot）输入；``occupant`` 可选指向待放入物料；
    ``show_occupied`` 决定是否展示已占用库位；``allow_occupied`` 决定这些库位
    是否可选。该对象服务编辑器、静态合同和 Backend 调度门禁；自身不查询库存
    权威，实际库位占用仍由 Backend 数据库事务判定。
    """

    owner: str
    occupant: str | None = None
    show_occupied: bool = True
    allow_occupied: bool = False

    def __post_init__(self) -> None:
        """校验作者直接导入时的注解参数形状。

        参数：无。返回：无。异常：字段名为空、含首尾空白或策略不是布尔值时
        抛出 ``ValueError``；静态 AST 编译器会独立重复同一关闭式校验。
        """

        for label, value in (("owner", self.owner), ("occupant", self.occupant)):
            if value is not None and (
                not isinstance(value, str) or not value or value != value.strip()
            ):
                raise ValueError(f"SiteSelector {label} 必须是非空字段名")
        if (
            type(self.show_occupied) is not bool
            or type(self.allow_occupied) is not bool
        ):
            raise ValueError("SiteSelector 占用策略必须是布尔值")


def __getattr__(name: str):
    """已删除的 ``Placeholder``：导入时直接报错并给出新写法。

    抛 ``ImportError``（不是 ``AttributeError``），``from ... import Placeholder`` 才会带着这句话失败。
    """

    from unilabos.registry.action_options import REMOVED_ANNOTATIONS, removed_annotation_message

    if name in REMOVED_ANNOTATIONS:
        raise ImportError(removed_annotation_message(name), name=name, path=__file__)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
