"""人工判定表单的纯数据合同；创作和运行时共用，禁止类型隐式转换。"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any


def _validate_value(field: Mapping[str, Any], value: Any) -> None:
    """校验单个已填写值，包括 False 和 0，空值不表示未填写。"""
    kind = field["kind"]
    valid = False
    if kind == "choice":
        valid = isinstance(value, str) and value in field["options"]
    elif kind == "text":
        valid = isinstance(value, str) and len(value) <= 4096
        if field["required"]:
            valid = valid and bool(value.strip())
    elif kind == "boolean":
        valid = type(value) is bool
    elif kind == "number":
        valid = type(value) in (int, float)
        if valid:
            try:
                valid = math.isfinite(value)
            except OverflowError:
                valid = False
        if valid:
            valid = field.get("minimum", -math.inf) <= value <= field.get("maximum", math.inf)
    if not valid:
        raise ValueError(f"人工判定字段 {field['name']} 的值或范围无效")


def normalize_form_fields(value: Any) -> list[dict[str, Any]]:
    """冻结 1–32 个命名字段；拒绝重复名称、无效默认值和不适用的约束。"""
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        raise ValueError("人工判定必须包含 1–32 个字段")
    result: list[dict[str, Any]] = []
    names: set[str] = set()
    for raw in value:
        if not isinstance(raw, Mapping):
            raise ValueError("人工判定字段必须是对象")
        field = deepcopy(dict(raw))
        name, label, kind = (field.get(key) for key in ("name", "label", "kind"))
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name)
            or name in names
            or name in {"constructor", "prototype"}
        ):
            raise ValueError("人工判定字段名称必须唯一，以英文字母开头，长度不超过 64")
        if not isinstance(label, str) or not label.strip() or len(label) > 200:
            raise ValueError("人工判定字段必须有不超过 200 字的显示名称")
        if kind not in ("choice", "text", "number", "boolean"):
            raise ValueError("人工判定字段类型必须是 choice、text、number 或 boolean")
        allowed = {"name", "label", "kind", "required", "default"}
        if kind == "choice":
            allowed |= {"options", "labels"}
        elif kind == "number":
            allowed |= {"minimum", "maximum"}
        if set(field) - allowed:
            raise ValueError(f"人工判定字段 {name} 包含未知或不适用的配置")
        field.setdefault("required", True)
        if type(field["required"]) is not bool:
            raise ValueError("人工判定 required 必须是布尔值")
        if kind == "choice":
            options = field.get("options")
            if (
                not isinstance(options, list)
                or not 1 <= len(options) <= 32
                or any(not isinstance(v, str) or not v.strip() or len(v) > 200 for v in options)
                or len(set(options)) != len(options)
            ):
                raise ValueError("人工判定选项必须包含 1–32 个不重复的非空字符串")
            field.setdefault("labels", list(options))
            labels = field["labels"]
            if (
                not isinstance(labels, list)
                or len(labels) != len(options)
                or any(not isinstance(v, str) or not v.strip() or len(v) > 200 for v in labels)
            ):
                raise ValueError("人工判定选项标签必须与选项一一对应")
        if kind == "number":
            for key in ("minimum", "maximum"):
                if key in field:
                    bound = field[key]
                    try:
                        valid_bound = type(bound) in (int, float) and math.isfinite(bound)
                    except OverflowError:
                        valid_bound = False
                    if not valid_bound:
                        raise ValueError("人工判定数值范围必须是有限数值")
            if field.get("minimum", -math.inf) > field.get("maximum", math.inf):
                raise ValueError("人工判定数值范围下限不能大于上限")
        if "default" in field:
            _validate_value(field, field["default"])
        names.add(name)
        result.append(field)
    return result


def validate_form_answers(fields: Any, answers: Any) -> None:
    """只接受本次请求声明的字段；可选字段可以省略，必填字段必须显式提交。"""
    normalized = normalize_form_fields(fields)
    if not isinstance(answers, dict):
        raise ValueError("人工判定 answers 必须是对象")
    by_name = {field["name"]: field for field in normalized}
    if set(answers) - set(by_name):
        raise ValueError("人工判定回复包含未声明的字段")
    for name, field in by_name.items():
        if name not in answers:
            if field["required"]:
                raise ValueError(f"人工判定字段 {name} 必须填写")
            continue
        _validate_value(field, answers[name])
