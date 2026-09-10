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
    """显式声明动作输入物料占位符（ResourceSlot）不取得物料锁。"""

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
    是否可选。该对象只服务编辑器和静态合同，不查询库存权威。
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
