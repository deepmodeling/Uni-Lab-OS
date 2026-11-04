"""Action-level material-lock metadata.

物料天然带锁：动作的物料入参（``ResourceSlot`` 及其列表 / Optional，或
``placeholder_keys`` 标为 ``unilabos_resources`` 的入参）默认在执行期间独占。

- 未声明 ``materials_need_lock`` 时，上述物料入参全部加锁，``materials_free`` 列出的除外；
- 显式声明 ``materials_need_lock``（包括 ``[]``）时按声明原样生效，不再推导，
  此时不能再写 ``materials_free``；
- 注册表条目里的 ``materials_need_lock`` 永远是推导后的最终列表，调度与执行面只读这一份。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

# 与 unilabos.registry.placeholder_type 中的同名常量一致；那个模块依赖 pylabrobot，
# 调度面不引入它。
PLACEHOLDER_RESOURCES = "unilabos_resources"
MATERIAL_SLOT_TYPE = "ResourceSlot"

# 允许包住 ResourceSlot 的类型构造：单个、列表 / 元组 / 集合、Optional / Union[..., None]。
# Dict[str, ResourceSlot] 等其它形状不默认加锁（值里没有可直接提取的物料身份），需要时显式声明。
_SLOT_WRAPPERS = frozenset(
    {
        "typing",
        "Optional",
        "Union",
        "None",
        "NoneType",
        "List",
        "list",
        "Sequence",
        "MutableSequence",
        "Collection",
        "Iterable",
        "Tuple",
        "tuple",
        "Set",
        "set",
        "FrozenSet",
        "frozenset",
    }
)
_TYPE_TOKEN = re.compile(r"[A-Za-z_][\w.:]*")


def normalize_material_parameter_names(
    value: Any,
    *,
    action_parameter_names: Iterable[str] | None = None,
    action_name: str = "action",
    field: str = "materials_need_lock",
) -> list[str]:
    """Validate and de-duplicate a list of action parameter names.

    ``materials_need_lock`` / ``materials_free`` name driver method parameters,
    not ROS goal field aliases.  When the action signature is available, reject
    misspellings at declaration/AST-scan time instead of silently dispatching
    without a lock.
    """

    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
        raise TypeError(f"{field} 必须是参数名列表")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field} 的每一项必须是非空参数名")
        name = item.strip()
        if name not in seen:
            result.append(name)
            seen.add(name)

    if action_parameter_names is not None:
        available = {
            str(name)
            for name in action_parameter_names
            if str(name) not in {"self", "cls"}
        }
        unknown = [name for name in result if name not in available]
        if unknown:
            raise ValueError(
                f"{action_name} 的 {field} 包含非动作入参: "
                f"{', '.join(unknown)}"
            )
    return result


def _type_text(type_hint: Any) -> str:
    if type_hint is None:
        return ""
    if isinstance(type_hint, str):
        return type_hint
    if isinstance(type_hint, tuple):
        # import 期签名分析的列表形态：("list", "unilabos.registry.placeholder_type:ResourceSlot")
        return " ".join(_type_text(item) for item in type_hint)
    if isinstance(type_hint, type):
        return f"{type_hint.__module__}.{type_hint.__qualname__}"
    return repr(type_hint)


def is_material_slot_type(type_hint: Any) -> bool:
    """``ResourceSlot``、其列表 / 元组 / 集合或 Optional 形态返回 True。

    接受 AST 裸名（``List[ResourceSlot]``）、import 期全路径
    （``unilabos.registry.placeholder_type:ResourceSlot``）与运行期注解对象。
    """

    names = [re.split(r"[.:]", token)[-1] for token in _TYPE_TOKEN.findall(_type_text(type_hint))]
    return MATERIAL_SLOT_TYPE in names and all(
        name == MATERIAL_SLOT_TYPE or name in _SLOT_WRAPPERS for name in names
    )


def _parameter(param: Any) -> tuple[str, Any]:
    if isinstance(param, Mapping):
        return str(param.get("name") or ""), param.get("type")
    # inspect.Parameter
    return str(getattr(param, "name", "") or ""), getattr(param, "annotation", None)


def material_parameter_names(
    params: Iterable[Any],
    placeholder_keys: Mapping[str, Any] | None = None,
) -> list[str]:
    """动作的物料入参（按签名顺序）：ResourceSlot 类型，或 placeholder 标为物料的入参。"""

    marked = {
        str(key)
        for key, kind in (placeholder_keys or {}).items()
        if kind == PLACEHOLDER_RESOURCES
    }
    result: list[str] = []
    for param in params:
        name, type_hint = _parameter(param)
        if not name or name in {"self", "cls"} or name in result:
            continue
        if name in marked or is_material_slot_type(type_hint):
            result.append(name)
    return result


def effective_material_lock_parameters(
    declared: Any,
    *,
    params: Iterable[Any] | None,
    free: Any = None,
    placeholder_keys: Mapping[str, Any] | None = None,
    action_name: str = "action",
) -> list[str]:
    """动作执行期间要独占的物料入参（写入注册表的最终 ``materials_need_lock``）。

    ``params`` 为动作签名（``{"name", "type"}`` 字典或 ``inspect.Parameter``）；
    为 None 表示签名未知，此时不校验参数名、也不推导默认锁。
    """

    param_list = list(params) if params is not None else None
    parameter_names = (
        [name for name, _hint in map(_parameter, param_list) if name]
        if param_list is not None
        else None
    )
    free_names = normalize_material_parameter_names(
        free,
        action_parameter_names=parameter_names,
        action_name=action_name,
        field="materials_free",
    )
    if declared is not None:
        if free_names:
            raise ValueError(
                f"{action_name} 不能同时声明 materials_need_lock 与 materials_free："
                "显式列表即最终加锁集合"
            )
        return normalize_material_parameter_names(
            declared,
            action_parameter_names=parameter_names,
            action_name=action_name,
        )
    if param_list is None:
        return []
    return [
        name
        for name in material_parameter_names(param_list, placeholder_keys)
        if name not in free_names
    ]


__all__ = [
    "MATERIAL_SLOT_TYPE",
    "PLACEHOLDER_RESOURCES",
    "effective_material_lock_parameters",
    "is_material_slot_type",
    "material_parameter_names",
    "normalize_material_parameter_names",
]
