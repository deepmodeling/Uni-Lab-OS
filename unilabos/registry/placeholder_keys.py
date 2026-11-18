"""动作参数的前端选择器 ``placeholder_keys``：按类型推出的与手写的取并集，同一参数以手写的为准。

``placeholder_keys`` 只决定前端用哪种选择器填写一个参数（值是 ``placeholder_type`` 里的
``PLACEHOLDER_*``）。注册表按参数类型推出一份：

- ``ResourceSlot`` / ``list[ResourceSlot]`` → ``unilabos_resources``；
- ``DeviceSlot`` → ``unilabos_devices``；
- ``SiteSlot`` → ``unilabos_sites``。

``@action(placeholder_keys={"参数名": PLACEHOLDER_...})`` 手写一份。最终给前端的（注册表条目的
``placeholder_keys``，以及 schema goal 里的 ``_unilabos_placeholder_info``）是两者的并集，同一参数两边
不同时以手写的为准：传进去的是物料（``ResourceSlot``），网页上却希望显示另一种选择器时，只改显示。

参数是什么（是不是物料、要不要解析成物料实例、加不加物料锁、怎么校验）仍由类型决定，与手写的选择器
无关。唯一的兼容例外沿用此前的规则：手写成 ``unilabos_resources`` 的参数也算物料参数、加共享资源锁
（见 :mod:`unilabos.registry.material_locks`）。

手写的键必须是动作的参数名（注册时对照签名报错）；值不是已知种类时按定义告警一次、照常写入（前端以后
可能加新的种类）。

本模块只依赖标准库：AST 扫描在线程池里逐文件调用它，不导入 ``placeholder_type``（它依赖 pylabrobot）。
"""

from __future__ import annotations

import ast
import logging
import threading
from collections.abc import Iterable, Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: ``PLACEHOLDER_*`` 常量所在模块。
PLACEHOLDER_MODULE = "unilabos.registry.placeholder_type"
#: 由框架注入、不属于动作参数的名字（与 ``action_schema.FRAMEWORK_PARAMETERS`` 一致）。
_FRAMEWORK_PARAMETERS = frozenset({"sample_uuids", "action_context"})

_warned: set[tuple[str, str]] = set()
_warned_lock = threading.Lock()


class PlaceholderKeysError(ValueError):
    """``@action(placeholder_keys=...)`` 写法不对：注册失败并指出原因。"""


@lru_cache(maxsize=1)
def placeholder_constants() -> Dict[str, str]:
    """静态读取 ``placeholder_type.py`` 顶层 ``NAME = "str"`` 常量（不导入该模块）。"""

    constants: Dict[str, str] = {}
    try:
        path = Path(__file__).with_name("placeholder_type.py")
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return constants
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value.value
    return constants


def known_placeholder_kinds() -> frozenset[str]:
    """``placeholder_type`` 里已知的选择器种类（``PLACEHOLDER_*`` 常量的值）。"""

    return frozenset(value for name, value in placeholder_constants().items() if name.startswith("PLACEHOLDER_"))


def _kind(value: Any) -> Optional[str]:
    """手写的值 → 选择器种类；AST 扫描把 ``placeholder_type.PLACEHOLDER_X`` 写法解析成带模块的名字，这里还原。"""

    if not isinstance(value, str):
        return None
    text = value.strip()
    for separator in (":", "."):
        prefix = f"{PLACEHOLDER_MODULE}{separator}"
        if text.startswith(prefix):
            return placeholder_constants().get(text[len(prefix):], text)
    return text or None


def placeholder_parameter_names(
    parameter_names: Iterable[str],
    *,
    goal: Optional[Mapping[str, Any]] = None,
    ros_action: bool = False,
    parent: bool = False,
    is_protocol: bool = False,
) -> Optional[list[str]]:
    """``placeholder_keys`` 可以写的键（装饰器与 AST 扫描同一规则）。

    ``parameter_names`` 是方法的具名参数（不含 ``self`` / ``cls`` 与 ``*args`` / ``**kwargs``）；框架注入的
    ``sample_uuids`` / ``action_context`` 不算。ROS 动作另加 ``goal=`` 映射的字段名。签名未知时返回 None、
    不核对：``parent=True`` 借父类签名，协议动作（``is_protocol=True``）按 ROS 消息字段。
    """

    if parent or is_protocol:
        return None
    names = [name for name in parameter_names if name not in _FRAMEWORK_PARAMETERS]
    if ros_action and isinstance(goal, Mapping):
        names.extend(str(key) for key in goal)
    return names


def check_placeholder_keys(
    value: Any,
    *,
    parameter_names: Optional[Iterable[str]],
    definition: str,
) -> Dict[str, str]:
    """校验 ``@action(placeholder_keys=...)``，返回 ``{参数名: 选择器种类}``。

    ``parameter_names`` 是可以写选择器的参数名；为 ``None`` 表示签名未知（``parent=True`` 借父类签名、
    协议动作按 ROS 消息字段），此时不核对键。键不是参数名、值不是非空字符串时抛
    :class:`PlaceholderKeysError`；值不是已知种类时按定义告警一次并照常返回。
    """

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise PlaceholderKeysError(f"@action(placeholder_keys=...) 必须是 {{参数名: 选择器种类}} 字典（{definition}）")
    result: Dict[str, str] = {}
    for key, raw in value.items():
        name = key.strip() if isinstance(key, str) else ""
        kind = _kind(raw)
        if not name or kind is None:
            raise PlaceholderKeysError(
                f"@action(placeholder_keys=...) 的键必须是参数名、值必须是 placeholder_type 里的选择器常量"
                f"（{definition}：{key!r}: {raw!r}）"
            )
        result[name] = kind
    if parameter_names is not None:
        allowed = set(parameter_names)
        unknown = [name for name in result if name not in allowed]
        if unknown:
            raise PlaceholderKeysError(
                f"@action(placeholder_keys=...) 的键必须是动作的参数名（{definition}）：{'、'.join(unknown)} 不是参数"
            )
    known = known_placeholder_kinds()
    for name, kind in result.items():
        if kind not in known:
            warn_unknown_placeholder_kind(definition, name, kind)
    return result


def warn_unknown_placeholder_kind(definition: str, name: str, kind: str) -> bool:
    """手写了不认识的选择器种类：按定义 + 种类告警一次（前端可能加了新种类，不报错），返回是否首次告警。"""

    key = (definition, kind)
    with _warned_lock:
        if key in _warned:
            return False
        _warned.add(key)
    logger.warning(
        "动作 %s 的 placeholder_keys 给参数 %s 写了未知的选择器种类 %r（已知：%s），照常写入注册表",
        definition,
        name,
        kind,
        "、".join(sorted(known_placeholder_kinds())),
    )
    return True


def merge_placeholder_keys(derived: Mapping[str, str], declared: Mapping[str, str]) -> Dict[str, str]:
    """按类型推出的 ∪ 手写的；同一参数以手写的为准。"""

    merged = dict(derived)
    merged.update(declared)
    return merged


__all__ = [
    "PLACEHOLDER_MODULE",
    "PlaceholderKeysError",
    "check_placeholder_keys",
    "known_placeholder_kinds",
    "merge_placeholder_keys",
    "placeholder_constants",
    "placeholder_parameter_names",
    "warn_unknown_placeholder_kind",
]
