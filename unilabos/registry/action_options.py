"""``@action`` 已删除的写法：装饰器（导入期）与注册表 AST 扫描共用同一套报错。

这些写法不再接受，写了就报错并指出新写法（见 ``docs/developer_guide/action_standard.md`` 的「不兼容变更」）：

- ``contract=``：所有 JSON 动作统一按签名生成并校验 schema，没有 opt-in 开关；
- ``resource_contract={...}``：资源语义改用 ``material_contract=MaterialRole.xxx(...)``；
- ``material=``（以及短暂出现过的 ``materials=``）：字段改名为 ``material_contract=``，值不变
  （一条 ``MaterialRole.xxx(...)`` 或它们组成的列表）；
- JSON 动作的 ``goal=`` / ``result=`` 重命名：字段名就是参数名 / 返回 TypedDict 的字段名
  （ROS 动作类型的 ``goal`` / ``result`` 字段映射照旧）；
- 参数注解里的 ``Placeholder(...)``：前端选择器写在 ``@action(placeholder_keys={...})``；
- 参数注解里的 ``SiteSelector(...)``：库位参数注解为 ``SiteSlot``，库位选择器提示写成
  ``material_contract=MaterialRole.site_picker(...)``；
- 按名字注入的动作上下文（参数叫 ``action_context`` 却没有 ``ActionContext`` 注解）：框架按类型注入，
  注解为 ``Optional[ActionContext]``（见 :mod:`unilabos.registry.action_context`）。

本模块只依赖标准库：AST 扫描在线程池里逐文件调用它。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Dict

from unilabos.registry.action_context import LEGACY_ACTION_CONTEXT_NAME, untyped_action_context_message

#: 资源语义字段改名的提示：``material=``（dev 上的旧名）与 ``materials=``（短暂出现过）都改成 ``material_contract=``。
_MATERIAL_CONTRACT_RENAME = (
    "字段改名为 material_contract=，值不变：写成 material_contract=MaterialRole.xxx(...)"
    "（多条照旧写成列表）；material_contract= 是实验性功能"
)

#: 任何动作都不再接受的选项 → 新写法。
REMOVED_ACTION_OPTIONS: Dict[str, str] = {
    "contract": "所有 JSON 动作统一按签名生成并校验 schema，去掉 contract= 即可",
    "resource_contract": (
        "资源语义改用 material_contract=MaterialRole.xxx(...)，例如 "
        "material_contract=MaterialRole.move(\"plate\", to_site=\"target_site\")、"
        "material_contract=MaterialRole.dispense(from_material=\"source\", to_materials=[\"target\"])、"
        "material_contract=[MaterialRole.in_place(\"sample\"), MaterialRole.order_sensitive()]"
    ),
    "material": _MATERIAL_CONTRACT_RENAME,
    "materials": _MATERIAL_CONTRACT_RENAME,
}
#: 只对 JSON 动作（没写 action_type）删除的选项 → 新写法。
REMOVED_JSON_ACTION_OPTIONS: Dict[str, str] = {
    "goal": "JSON 动作的字段名就是参数名，去掉 goal=（要改名就改参数名）",
    "result": "JSON 动作的结果字段就是返回 TypedDict 的字段名，去掉 result=（要改名就改字段名）",
}
#: ``unilabos.registry.annotations`` 里已删除的注解 → 新写法（导入、装饰器与 AST 扫描同一句话）。
REMOVED_ANNOTATIONS: Dict[str, str] = {
    "Placeholder": (
        "前端选择器写在 @action(placeholder_keys={\"参数名\": PLACEHOLDER_...})：注册表按类型推出的"
        "（ResourceSlot / DeviceSlot / SiteSlot）与手写的取并集，同一参数以手写的为准；"
        "参数类型照常写，去掉 Annotated[..., Placeholder(...)] 外壳"
    ),
    "SiteSelector": (
        "库位参数注解为 SiteSlot，库位选择器提示写成 @action(material_contract=MaterialRole.site_picker(\"库位参数\", "
        "of=\"拥有库位的物料或设备参数\", for_material=\"放进去的物料参数\", occupied=\"show\"))"
    ),
}
#: 已删除注解的导入身份（``模块:名字``）→ 名字。
REMOVED_ANNOTATION_IDENTITIES: Dict[str, str] = {
    f"unilabos.registry.annotations:{name}": name for name in REMOVED_ANNOTATIONS
}


class RemovedActionOptionError(TypeError):
    """``@action`` 写了已删除的选项。"""


def removed_annotation_message(name: str) -> str:
    """已删除注解的报错信息（含新写法）。"""

    return f"{name} 已删除：{REMOVED_ANNOTATIONS[name]}"


def check_action_options(options: Mapping[str, Any], *, json_action: bool, definition: str) -> None:
    """写了已删除的选项就报错（``goal`` / ``result`` 只在 JSON 动作上算删除；空值 ``{}`` 视同没写）。"""

    for name, hint in REMOVED_ACTION_OPTIONS.items():
        if name in options:
            raise RemovedActionOptionError(f"@action 不再接受 {name}=（{definition}）：{hint}")
    if json_action:
        for name, hint in REMOVED_JSON_ACTION_OPTIONS.items():
            if options.get(name):
                raise RemovedActionOptionError(f"@action 不再接受 {name}=（{definition}）：{hint}")


def check_action_context_parameter(
    parameter_names: Iterable[str], context_parameters: Iterable[str], *, definition: str
) -> None:
    """参数叫 ``action_context`` 却不是按类型识别的上下文参数（没有 ``ActionContext`` 注解）：报错并给出新写法。"""

    if LEGACY_ACTION_CONTEXT_NAME in set(parameter_names) - set(context_parameters):
        raise RemovedActionOptionError(untyped_action_context_message(definition))


__all__ = [
    "REMOVED_ACTION_OPTIONS",
    "REMOVED_ANNOTATIONS",
    "REMOVED_ANNOTATION_IDENTITIES",
    "REMOVED_JSON_ACTION_OPTIONS",
    "RemovedActionOptionError",
    "check_action_context_parameter",
    "check_action_options",
    "removed_annotation_message",
]
