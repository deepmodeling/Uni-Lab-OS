"""框架注入的动作上下文参数：按类型识别 ``ActionContext``，不看参数名。

注解为 ``ActionContext``、``Optional[ActionContext]``、``ActionContext | None``（或 ``Union[ActionContext, None]``）
的参数由框架注入，名字随作者起：

- 可空的（上面后两种写法，或默认值是 ``None``）：没有上下文时（例如 ROS 2 执行、作者直接调用）注入 ``None``；
- 不可空的：总能拿到一个上下文，框架没有时现建一个。

这些参数不属于动作接口：注册表 AST 扫描与运行时签名推断按类型把它们排除在 goal schema、必填列表、
``placeholder_keys``、生成的端口、物料锁参数与 ``goal_default`` 之外。

以前按名字注入的写法（参数叫 ``action_context`` 但没有这个类型注解）已删除：装饰器与 AST 扫描都报错，
要求作者注解为 ``Optional[ActionContext]``（见 :mod:`unilabos.registry.action_options`）。

本模块只依赖标准库：AST 扫描在线程池里逐文件调用它，不导入 ``ActionContext`` 所在的运行时包，
运行时按类的 ``模块:名字`` 身份比较。
"""

from __future__ import annotations

import ast
import inspect
import types
import typing
from collections.abc import Callable, Mapping
from typing import Any, Optional

#: ``ActionContext`` 的导入身份（``模块:名字``）：定义处与运行时包的再导出。
ACTION_CONTEXT_IDENTITIES = frozenset(
    {
        "unilabos.client.host.runtime.action:ActionContext",
        "unilabos.client.host.runtime:ActionContext",
    }
)
#: 已删除的按名字注入写法的参数名。
LEGACY_ACTION_CONTEXT_NAME = "action_context"

_OPTIONAL = frozenset({"typing:Optional", "typing_extensions:Optional"})
_UNION = frozenset({"typing:Union", "typing_extensions:Union"})


def _is_none(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def ast_action_context_kind(node: Optional[ast.AST], identity: Callable[[ast.AST], str]) -> Optional[bool]:
    """一个参数注解（AST）是不是动作上下文：不是返回 ``None``；``ActionContext`` 返回 ``False``；可空写法返回 ``True``。

    ``identity`` 把 ``Name`` / ``Attribute`` 换成导入身份（``模块:名字``），由调用方按模块的导入表提供。
    字符串前向引用（``"Optional[ActionContext]"``）先解析再判断。
    """

    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            parsed = ast.parse(node.value, mode="eval").body
        except SyntaxError:
            return None
        return ast_action_context_kind(parsed, identity)
    if isinstance(node, (ast.Name, ast.Attribute)):
        return False if identity(node) in ACTION_CONTEXT_IDENTITIES else None
    if isinstance(node, ast.Subscript):
        origin = identity(node.value)
        members = list(node.slice.elts) if isinstance(node.slice, ast.Tuple) else [node.slice]
        if origin in _OPTIONAL and len(members) == 1:
            return True if ast_action_context_kind(members[0], identity) is not None else None
        if origin in _UNION and len(members) == 2 and any(_is_none(member) for member in members):
            other = next(member for member in members if not _is_none(member))
            return True if ast_action_context_kind(other, identity) is not None else None
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        if _is_none(node.right):
            return True if ast_action_context_kind(node.left, identity) is not None else None
        if _is_none(node.left):
            return True if ast_action_context_kind(node.right, identity) is not None else None
    return None


def ast_action_context_parameters(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    identity: Callable[[ast.AST], str],
) -> dict[str, bool]:
    """函数（AST）里由框架注入的上下文参数：``{参数名: 是否可空}``（默认值是 ``None`` 也算可空）。"""

    arguments = function.args
    positional = [*arguments.posonlyargs, *arguments.args]
    defaults: list[Optional[ast.expr]] = [None] * (len(positional) - len(arguments.defaults)) + list(arguments.defaults)
    scheduled = [*zip(positional, defaults), *zip(arguments.kwonlyargs, arguments.kw_defaults)]
    result: dict[str, bool] = {}
    for argument, default in scheduled:
        kind = ast_action_context_kind(argument.annotation, identity)
        if kind is not None:
            result[argument.arg] = kind or (default is not None and _is_none(default))
    return result


def ast_import_identity(imports: Mapping[str, str]) -> Callable[[ast.AST], str]:
    """按导入表（``本地名 → 模块:名字`` 或 ``本地名 → 模块``）把 ``Name`` / ``a.b.Name`` 换成导入身份。"""

    def identity(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return str(imports.get(node.id, ""))
        if isinstance(node, ast.Attribute):
            parts: list[str] = []
            current: ast.AST = node
            while isinstance(current, ast.Attribute):
                parts.append(current.attr)
                current = current.value
            if not isinstance(current, ast.Name):
                return ""
            base = imports.get(current.id)
            if not base or ":" in base:
                return ""
            parts.reverse()
            return f"{'.'.join([base, *parts[:-1]])}:{parts[-1]}"
        return ""

    return identity


def untyped_action_context_message(definition: str) -> str:
    """参数叫 ``action_context`` 却没有 ``ActionContext`` 注解时的报错（含新写法）。"""

    return (
        f"动作 {definition} 的参数 {LEGACY_ACTION_CONTEXT_NAME} 没有 ActionContext 类型注解：框架已改为按类型注入，"
        "不再认参数名。请注解为 Optional[ActionContext]（from unilabos.client.host.runtime.action import ActionContext），"
        "或换一个参数名"
    )


# ---------------------------------------------------------------------------
# 运行时（已导入的函数）
# ---------------------------------------------------------------------------


def _object_identity(value: Any) -> str:
    module = getattr(value, "__module__", None)
    name = getattr(value, "__qualname__", None) or getattr(value, "__name__", None)
    return f"{module}:{name}" if isinstance(module, str) and isinstance(name, str) else ""


def _is_context_class(value: Any) -> bool:
    if not isinstance(value, type):
        return False
    return any(_object_identity(cls) in ACTION_CONTEXT_IDENTITIES for cls in value.__mro__)


def _runtime_kind(annotation: Any) -> Optional[bool]:
    if _is_context_class(annotation):
        return False
    origin = typing.get_origin(annotation)
    if origin is typing.Union or origin is types.UnionType:
        members = typing.get_args(annotation)
        rest = [member for member in members if member is not type(None)]
        if len(members) == 2 and len(rest) == 1 and _is_context_class(rest[0]):
            return True
    return None


def _textual_identity(globalns: Mapping[str, Any]) -> Callable[[ast.AST], str]:
    """字符串注解求值失败（如只在 ``TYPE_CHECKING`` 下导入）时，按名字与模块全局变量猜身份。"""

    def identity(node: ast.AST) -> str:
        name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else ""
        bound = globalns.get(name) if isinstance(node, ast.Name) else None
        if bound is not None:
            if _is_context_class(bound):
                return next(iter(sorted(ACTION_CONTEXT_IDENTITIES)))
            if bound is typing.Optional:
                return "typing:Optional"
            if bound is typing.Union:
                return "typing:Union"
            return _object_identity(bound)
        if name == "ActionContext":
            return next(iter(sorted(ACTION_CONTEXT_IDENTITIES)))
        if name in {"Optional", "Union"}:
            return f"typing:{name}"
        return ""

    return identity


def runtime_action_context_kind(annotation: Any, globalns: Mapping[str, Any]) -> Optional[bool]:
    """运行时参数注解是不是动作上下文（返回值同 :func:`ast_action_context_kind`）；字符串注解按函数的模块全局变量求值。"""

    if annotation is inspect.Parameter.empty:
        return None
    if isinstance(annotation, str):
        try:
            annotation = eval(annotation, dict(globalns), {})  # noqa: S307 - 只求值作者写的类型注解
        except Exception:  # noqa: BLE001 - 求值不了（TYPE_CHECKING 导入等）退回按文本判断
            try:
                parsed = ast.parse(annotation, mode="eval").body
            except SyntaxError:
                return None
            return ast_action_context_kind(parsed, _textual_identity(globalns))
    return _runtime_kind(annotation)


def action_context_parameters(function: Callable[..., Any]) -> dict[str, bool]:
    """已导入函数里由框架注入的上下文参数：``{参数名: 是否可空}``；签名读不到时返回空。"""

    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return {}
    try:
        target = inspect.unwrap(function)
    except ValueError:
        target = function
    globalns = getattr(target, "__globals__", None) or {}
    result: dict[str, bool] = {}
    for name, parameter in signature.parameters.items():
        if parameter.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        kind = runtime_action_context_kind(parameter.annotation, globalns)
        if kind is not None:
            result[name] = kind or parameter.default is None
    return result


def untyped_action_context_parameter(function: Callable[..., Any]) -> bool:
    """已导入函数是否还用已删除的按名字写法：有 ``action_context`` 参数，但它不是按类型识别的上下文参数。"""

    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False
    return LEGACY_ACTION_CONTEXT_NAME in parameters and LEGACY_ACTION_CONTEXT_NAME not in action_context_parameters(function)


__all__ = [
    "ACTION_CONTEXT_IDENTITIES",
    "LEGACY_ACTION_CONTEXT_NAME",
    "action_context_parameters",
    "ast_action_context_kind",
    "ast_action_context_parameters",
    "ast_import_identity",
    "runtime_action_context_kind",
    "untyped_action_context_message",
    "untyped_action_context_parameter",
]
