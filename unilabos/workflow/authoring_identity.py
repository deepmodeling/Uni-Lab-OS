"""工作流创作（Workflow Authoring）的确定性身份规则。"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from uuid import UUID, uuid5

from unilabos.workflow.models import validate_uuid

_COMPOSITE_NODE_PREFIX = "unilabos:c1:node:v1:"
_WORKFLOW_DECORATOR_PATHS = {
    "unilabos.workflow.authoring.workflow",
    "unilabos.workflow.authoring.workflow_definition",
}


def _import_map(module: ast.Module) -> dict[str, str]:
    """建立源码局部导入名到绝对 Python 身份的静态映射。

    参数：``module`` 是已解析且未执行的 Python AST。返回：只包含显式 import
    语句的局部名映射；星号导入不推断任何身份。异常：无。
    """

    imports: dict[str, str] = {}
    for statement in module.body:
        if isinstance(statement, ast.ImportFrom) and statement.module:
            for alias in statement.names:
                if alias.name != "*":
                    imports[alias.asname or alias.name] = (
                        f"{statement.module}.{alias.name}"
                    )
        elif isinstance(statement, ast.Import):
            for alias in statement.names:
                imports[alias.asname or alias.name.split(".", 1)[0]] = alias.name
    return imports


def _expression_path(expression: ast.expr, imports: Mapping[str, str]) -> str | None:
    """把装饰器表达式解析为静态绝对路径。

    参数：``expression`` 是装饰器函数表达式，``imports`` 是显式导入映射。
    返回：名称/属性链的静态路径；动态表达式返回 ``None``。异常：无。
    """

    if isinstance(expression, ast.Name):
        return imports.get(expression.id, expression.id)
    if isinstance(expression, ast.Attribute):
        parent = _expression_path(expression.value, imports)
        return f"{parent}.{expression.attr}" if parent is not None else None
    return None


def declared_workflow_uuid(python_source: str) -> str | None:
    """只读提取源码唯一、显式且有效的工作流 UUID。

    参数：``python_source`` 是待写入已登记工作流源码路径的完整 Python 文本。
    返回：唯一规范 ``@workflow``/``@workflow_definition`` 声明中的 UUID；语法
    错误、声明缺失或歧义、值不是字符串字面量、UUID 无效时返回 ``None``。
    异常：无；所有不确定输入均关闭为“无法证明跨工作流”。

    安全不变量：只解析 AST，绝不导入或执行用户源码。结果只用于写入前路由
    保护；已授权的 package.yaml 与登记路径仍是工作流持久身份权威。
    """

    try:
        module = ast.parse(python_source)
    except (SyntaxError, TypeError, ValueError):
        return None
    imports = _import_map(module)
    declarations: list[str] = []
    for statement in module.body:
        if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in statement.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            decorator_path = _expression_path(decorator.func, imports)
            if decorator_path not in _WORKFLOW_DECORATOR_PATHS:
                continue
            workflow_uuid_keywords = [
                keyword
                for keyword in decorator.keywords
                if keyword.arg == "workflow_uuid"
            ]
            if len(workflow_uuid_keywords) != 1:
                return None
            value = workflow_uuid_keywords[0].value
            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                return None
            try:
                declarations.append(validate_uuid(value.value))
            except (TypeError, ValueError):
                return None
    return declarations[0] if len(declarations) == 1 else None


def expanded_node_uuid(invocation_uuid: str, child_node_uuid: str) -> str:
    """把子节点身份确定性派生到一次组合工作流调用命名空间。

    参数：``invocation_uuid`` 是真实调用节点 UUID，``child_node_uuid`` 是已应用
    子工作流中的规范节点 UUID。返回：C1 v1 固定 UUIDv5；非法身份抛出
    ``ValueError``。
    异常：任一身份非规范或为 nil UUID 时抛出 ``ValueError``。
    """

    namespace = UUID(validate_uuid(invocation_uuid))
    child = validate_uuid(child_node_uuid)
    return str(uuid5(namespace, _COMPOSITE_NODE_PREFIX + child))


def authoring_edge_uuid(
    *,
    workflow_uuid: str,
    source_node_uuid: str,
    source_handle_uuid: str,
    target_node_uuid: str,
    target_handle_uuid: str,
) -> str:
    """为一条创作边（Authoring Edge）生成确定性 UUIDv5。

    参数说明：``workflow_uuid`` 是身份命名空间；其余四个 UUID 是源节点、
    源连接点（Handle）、目标节点和目标连接点身份。返回值对相同端点稳定，
    任一端点变化都会产生不同 UUID；非法或 nil UUID 会抛出 ``ValueError``。
    异常：任一身份非规范或为 nil UUID 时抛出 ``ValueError``。
    """

    normalized_workflow = validate_uuid(workflow_uuid)
    source_node = validate_uuid(source_node_uuid)
    source_handle = validate_uuid(source_handle_uuid)
    target_node = validate_uuid(target_node_uuid)
    target_handle = validate_uuid(target_handle_uuid)
    # C1 v1 与前端共同冻结了该名称字节序；不能改用路径或 JSON 编码。
    name = f"authoring-edge:{source_node}:{source_handle}:{target_node}:{target_handle}"
    return str(uuid5(UUID(normalized_workflow), name))


__all__ = [
    "authoring_edge_uuid",
    "declared_workflow_uuid",
    "expanded_node_uuid",
]
