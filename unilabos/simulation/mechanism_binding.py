"""驱动动作 ↔ 资产机构动作的静态对应：只读源码（AST），不导入、不执行。

规则（都是显式的，不按相似名字猜）：

1. 驱动 ``@action(motion=...)`` 写了机构动作名或 ``{"action", "args"}`` 的，按它绑定；
2. 否则驱动方法与资产行为类的方法**同名**即对应；机构动作的必填参数必须都能在驱动动作的参数里
   按同名找到（驱动多出来的参数，如超时，不传给机构动作）；
3. 其余驱动动作没有机构运动（工艺动作），列在 ``unbound`` 里，不算错误。

只看类体里直接声明的 ``@action`` 方法，与 OS 注册表静态扫描的口径一致。
"""
from __future__ import annotations

import ast
from pathlib import Path
from typing import Any


def _decorator(node: ast.FunctionDef | ast.AsyncFunctionDef, name: str) -> ast.Call | ast.Name | None:
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        leaf = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", None)
        if leaf == name:
            return decorator
    return None


def scan_actions(source: str | Path, class_name: str, *, behavior: bool = False) -> dict[str, dict[str, Any]]:
    """类体里的动作：{方法名: {action_name, parameters, required, motion}}。

    驱动类取 ``@action`` 标注的方法。资产行为类（``behavior=True``）没有标注：公开方法即机构动作，
    第一个参数是 OS 传入的关节视图，不算动作入参。
    """
    tree = ast.parse(Path(source).read_text(encoding="utf8"))
    cls = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name), None)
    if cls is None:
        raise ValueError(f"{source} 里没有类 {class_name}")
    found: dict[str, dict[str, Any]] = {}
    for node in cls.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        decorator = _decorator(node, "action")
        if behavior:
            if node.name.startswith("_"):
                continue
        elif decorator is None:
            continue
        keywords = {}
        if isinstance(decorator, ast.Call):
            for keyword in decorator.keywords:
                if keyword.arg in ("action_name", "motion"):
                    try:
                        keywords[keyword.arg] = ast.literal_eval(keyword.value)
                    except ValueError as error:
                        raise ValueError(f"{class_name}.{node.name} 的 {keyword.arg} 必须是字面量") from error
        positional = [a.arg for a in node.args.posonlyargs + node.args.args][2 if behavior else 1:]  # 去掉 self（和关节视图）
        defaults = len(node.args.defaults)
        required = positional[: len(positional) - defaults] if defaults else list(positional)
        required += [a.arg for a, d in zip(node.args.kwonlyargs, node.args.kw_defaults) if d is None]
        parameters = positional + [a.arg for a in node.args.kwonlyargs]
        if behavior:  # OS 按名字注入的只读视图不是动作入参
            parameters = [name for name in parameters if name not in ("sites", "materials")]
            required = [name for name in required if name not in ("sites", "materials")]
        found[node.name] = {"action_name": keywords.get("action_name") or node.name, "parameters": parameters,
                            "required": required, "motion": keywords.get("motion")}
    return found


def bind_actions(driver_source: str | Path, driver_class: str, behavior_source: str | Path,
                 behavior_class: str) -> dict[str, Any]:
    """返回 ``{"actions": {对外动作名: {"mechanism", "args"}}, "unbound", "unused", "problems"}``。

    ``problems`` 非空表示声明自相矛盾（motion 指向不存在的机构动作、同名但参数对不上），调用方应当失败。
    """
    driver = scan_actions(driver_source, driver_class)
    behavior = scan_actions(behavior_source, behavior_class, behavior=True)
    actions: dict[str, dict[str, Any]] = {}
    unbound: list[str] = []
    problems: list[str] = []
    used: set[str] = set()
    for method, spec in driver.items():
        motion = spec["motion"]
        if motion is not None:
            target = motion if isinstance(motion, str) else motion.get("action")
            arguments = {} if isinstance(motion, str) else dict(motion.get("args") or {})
        elif method in behavior:
            target, arguments = method, {}
        else:
            unbound.append(spec["action_name"])
            continue
        if target not in behavior:
            problems.append(f"{driver_class}.{method} 的 motion 指向不存在的机构动作 {target}")
            continue
        mechanism = behavior[target]
        unknown = set(arguments) - set(mechanism["parameters"])
        if unknown:
            problems.append(f"{driver_class}.{method} 给了机构动作 {target} 没有的参数 {sorted(unknown)}")
            continue
        missing = [name for name in mechanism["required"]
                   if name not in arguments and (arguments or name not in spec["parameters"])]
        if missing:
            problems.append(f"{driver_class}.{method} → {target}：机构动作的必填参数 {missing} 没有来源；"
                            f"请在驱动的 @action 上写 motion 的 args")
            continue
        if spec["action_name"] in actions:
            problems.append(f"{driver_class} 有两个动作都叫 {spec['action_name']}")
            continue
        actions[spec["action_name"]] = {"mechanism": target, "args": arguments}
        used.add(target)
    return {"actions": actions, "unbound": sorted(unbound), "unused": sorted(set(behavior) - used),
            "problems": problems}
