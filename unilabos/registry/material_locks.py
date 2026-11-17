"""Action-level material-lock metadata.

物料天然带锁：动作的每个物料入参（``ResourceSlot`` 及其列表 / Optional，或
``placeholder_keys`` 标为 ``unilabos_resources`` 的入参）都默认加共享资源锁。

共享资源锁按调度器现有语义：真正使用期间互斥——动作准入时连同设备一起取得，
正常终态释放；取消未完成、执行结果未知或超时后驱动仍在执行时继续持有。它不跨
动作持有物料，空闲的物料因此谁都可以用，不需要显式借出。不能借出的独占只有
设备托管（``resource_contract.device_tenancy``）：样品和设备在装载到卸载之间
都不能借出。

驱动不能再按动作改写这一集合：旧的 ``@action(materials_need_lock=...)`` /
``@action(materials_free=...)``、YAML 条目里的同名键，以及 typed 动作的
``MaterialLock(free=True)``，一律忽略并按定义告警一次。注册表条目里的
``materials_need_lock`` 只是框架按签名推导出的结果，调度与执行面只读这一份。
"""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Iterable, Mapping
from typing import Any

logger = logging.getLogger(__name__)

# 与 unilabos.registry.placeholder_type 中的同名常量一致；那个模块依赖 pylabrobot，
# 调度面不引入它。
PLACEHOLDER_RESOURCES = "unilabos_resources"
MATERIAL_SLOT_TYPE = "ResourceSlot"

# 允许包住 ResourceSlot 的类型构造：单个、列表 / 元组 / 集合、Optional / Union[..., None]。
# Dict[str, ResourceSlot] 等其它形状不默认加锁（值里没有可直接提取的物料身份）；需要加锁时
# 用 placeholder_keys 把该入参标为 unilabos_resources。
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

    注册表与派发载荷里的 ``materials_need_lock`` 是推导出的驱动方法入参名（不是
    ROS goal 字段别名）；读取方用它校验形状并去重。给出动作签名时同时拒绝签名里
    没有的名字。
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


def default_material_lock_parameters(
    *,
    params: Iterable[Any] | None,
    placeholder_keys: Mapping[str, Any] | None = None,
) -> list[str]:
    """动作执行期间要加锁的物料入参（写入注册表的 ``materials_need_lock``）。

    ``params`` 为动作签名（``{"name", "type"}`` 字典或 ``inspect.Parameter``）；
    为 None 表示签名未知（直接加载的 YAML 条目）：只按 ``placeholder_keys`` 里标为
    ``unilabos_resources`` 的字段推导。
    """

    if params is None:
        return [
            str(key)
            for key, kind in (placeholder_keys or {}).items()
            if kind == PLACEHOLDER_RESOURCES
        ]
    return material_parameter_names(params, placeholder_keys)


#: 已移除的按动作改写物料锁的选项：装饰器参数与 YAML 条目键同名。
REMOVED_MATERIAL_LOCK_OPTIONS = ("materials_need_lock", "materials_free")

_warned_definitions: set[str] = set()
_warned_lock = threading.Lock()


def warn_ignored_material_lock_override(definition: str, ignored: Iterable[str]) -> bool:
    """一个动作定义里写了已废弃的物料锁改写：按定义告警一次，返回是否首次告警。

    ``definition`` 是定义的稳定身份：Python 动作用 ``模块.类.方法``（装饰器与 AST
    扫描同一写法，进程内只告警一次），YAML 动作用 ``文件:设备.动作``。``ignored``
    是被忽略的写法，原样放进告警里，方便作者定位。
    """

    names = [str(item) for item in ignored if str(item)]
    if not names:
        return False
    with _warned_lock:
        if definition in _warned_definitions:
            return False
        _warned_definitions.add(definition)
    logger.warning(
        "动作 %s 的物料锁改写 %s 已废弃并被忽略：动作的每个物料入参都默认加共享资源锁"
        "（使用期间互斥，空闲时不持有），请从定义里删掉这些写法",
        definition,
        "、".join(names),
    )
    return True


__all__ = [
    "MATERIAL_SLOT_TYPE",
    "PLACEHOLDER_RESOURCES",
    "REMOVED_MATERIAL_LOCK_OPTIONS",
    "default_material_lock_parameters",
    "is_material_slot_type",
    "material_parameter_names",
    "normalize_material_parameter_names",
    "warn_ignored_material_lock_override",
]
