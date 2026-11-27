"""从零导入的资产行为类生成「只有仿真行为」的设备声明；只读源码（AST），不导入、不执行。

用途：一台设备还没有真机驱动时，先用它的资产行为类串工作流。生成的类只声明合同：

* ``@device`` 的参数由调用方给（注册 ID 就用最终的 ID，以后真驱动接管同一个 ID）；
* 每个公开方法变成一个同名 ``@action``，入参去掉第一个参数（OS 传入的关节视图），说明取文档字符串；
* 方法体只抛 ``simulation_only_device``：仿真时设备运行时不构造驱动，动作由机构运行时按资产行为类执行；
  真机模式下派发到这里会明确失败，不会假装成功。

行为类可以用类属性 ``ACTION_CONTRACTS = {动作名: {...}}`` 给动作补 ``@action`` 的关键字参数
（例如 ``resource_contract``、``estimate_duration_fixed``），必须是字面量。
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Mapping

_ALLOWED_CONTRACT_KEYS = {"resource_contract", "estimate_duration_fixed", "estimate_duration_express",
                          "displayname", "action_name", "always_free"}


def _literal(value: Any) -> ast.expr:
    return ast.parse(repr(value), mode="eval").body


def generate_simulated_device(source: str | Path, class_name: str, *, declaration: Mapping[str, Any],
                              device_class: str | None = None) -> str:
    """返回设备声明模块的源码。``declaration`` 是 ``@device`` 的关键字参数（须含 id 或 ids、category）。"""
    if not (declaration.get("id") or declaration.get("ids")) or not declaration.get("category"):
        raise ValueError("declaration 必须给出 id（或 ids）和 category")
    tree = ast.parse(Path(source).read_text(encoding="utf8"))
    behavior = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name), None)
    if behavior is None:
        raise ValueError(f"{source} 里没有类 {class_name}")
    contracts: dict[str, dict[str, Any]] = {}
    for node in behavior.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "ACTION_CONTRACTS"
                                                for t in node.targets):
            try:
                contracts = ast.literal_eval(node.value)
            except ValueError as error:
                raise ValueError("ACTION_CONTRACTS 必须是字面量") from error
    methods = [n for n in behavior.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
               and not n.name.startswith("_")]
    if not methods:
        raise ValueError(f"{class_name} 没有公开方法，生成不出任何动作")
    unknown = set(contracts) - {m.name for m in methods}
    if unknown:
        raise ValueError(f"ACTION_CONTRACTS 里有不存在的动作：{sorted(unknown)}")
    body: list[ast.stmt] = [ast.Expr(ast.Constant(
        f"由资产行为类 {class_name} 生成的设备声明：只有仿真行为，没有真机驱动。"))]
    for method in methods:
        arguments = method.args
        positional = arguments.posonlyargs + arguments.args
        if len(positional) < 2:
            raise ValueError(f"{class_name}.{method.name} 的第一个参数必须是关节视图")
        if arguments.vararg or arguments.kwarg:
            raise ValueError(f"{class_name}.{method.name} 不能用 *args / **kwargs：动作入参必须能静态确定")
        keywords = dict(contracts.get(method.name) or {})
        bad = set(keywords) - _ALLOWED_CONTRACT_KEYS
        if bad:
            raise ValueError(f"ACTION_CONTRACTS[{method.name}] 不支持的键：{sorted(bad)}")
        keywords.setdefault("description", ast.get_docstring(method) or method.name)
        kept = positional[2:]
        signature = ast.arguments(
            posonlyargs=[], args=[ast.arg("self")] + [ast.arg(a.arg, a.annotation) for a in kept], vararg=None,
            kwonlyargs=[ast.arg(a.arg, a.annotation) for a in arguments.kwonlyargs],
            kw_defaults=list(arguments.kw_defaults), kwarg=None,
            defaults=list(arguments.defaults)[-len(kept):] if kept and arguments.defaults else [])
        raise_stmt = ast.Raise(ast.Call(ast.Name("RuntimeError", ast.Load()), [ast.Constant(
            f"simulation_only_device: {method.name} 只有仿真行为，真机模式下没有可用驱动")], []), None)
        body.append(ast.AsyncFunctionDef(
            name=method.name, args=signature, body=[ast.Expr(ast.Constant(keywords["description"])), raise_stmt],
            decorator_list=[ast.Call(ast.Name("action", ast.Load()), [],
                                     [ast.keyword(k, _literal(v)) for k, v in sorted(keywords.items())])],
            returns=ast.Name("dict", ast.Load()), type_params=[]))
    device = ast.ClassDef(
        name=device_class or class_name, bases=[], keywords=[], body=body, type_params=[],
        decorator_list=[ast.Call(ast.Name("device", ast.Load()), [],
                                 [ast.keyword(k, _literal(v)) for k, v in declaration.items()])])
    module = ast.Module(body=[
        ast.Expr(ast.Constant("由 unilab 资产行为构建生成；不要手改。真机驱动写好后，用它接管同一个注册 ID 并删除本文件。")),
        ast.ImportFrom("unilabos.registry.decorators", [ast.alias("action"), ast.alias("device")], 0),
        device], type_ignores=[])
    return ast.unparse(ast.fix_missing_locations(module)) + "\n"
