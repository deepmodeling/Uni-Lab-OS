"""公共 Workflow HTTP 边界使用的有界非递归 JSON 编解码器。"""

from __future__ import annotations

import math
import re
from json.decoder import scanstring
from json.encoder import encode_basestring
from typing import Any

MAX_BACKEND_JSON_DEPTH = 10_000

_JSON_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")
_JSON_WHITESPACE = " \t\r\n"
_MISSING = object()


def decode_json_bytes(
    body: bytes,
    *,
    max_depth: int = MAX_BACKEND_JSON_DEPTH,
) -> Any:
    """在不修改 Python 递归上限的情况下解码一个 UTF-8 JSON 值。"""

    text = body.decode("utf-8")
    length = len(text)
    position = 0
    result: Any = _MISSING
    stack: list[dict[str, Any]] = []

    def skip_whitespace(index: int) -> int:
        while index < length and text[index] in _JSON_WHITESPACE:
            index += 1
        return index

    def deliver(value: Any) -> None:
        nonlocal result
        if not stack:
            if result is not _MISSING:
                raise ValueError("JSON contains more than one value")
            result = value
            return
        frame = stack[-1]
        if frame["kind"] == "array":
            if frame["state"] != "value":
                raise ValueError("JSON array value is not expected")
            frame["value"].append(value)
            frame["state"] = "comma"
            return
        if frame["state"] != "value":
            raise ValueError("JSON object value is not expected")
        frame["value"][frame["key"]] = value
        frame["key"] = None
        frame["state"] = "comma"

    def begin_container(kind: str) -> None:
        if len(stack) + 1 > max_depth:
            raise ValueError("JSON nesting exceeds the Backend limit")
        stack.append(
            {
                "kind": kind,
                "value": [] if kind == "array" else {},
                "state": "first",
                "key": None,
            }
        )

    def parse_value(index: int) -> int:
        if index >= length:
            raise ValueError("JSON value is missing")
        token = text[index]
        if token == "{":
            begin_container("object")
            return index + 1
        if token == "[":
            begin_container("array")
            return index + 1
        if token == '"':
            value, end = scanstring(text, index + 1, True)
            deliver(value)
            return end
        if text.startswith("true", index):
            deliver(True)
            return index + 4
        if text.startswith("false", index):
            deliver(False)
            return index + 5
        if text.startswith("null", index):
            deliver(None)
            return index + 4
        match = _JSON_NUMBER.match(text, index)
        if match is None:
            raise ValueError("invalid JSON token")
        raw = match.group(0)
        value: Any
        if "." in raw or "e" in raw.lower():
            value = float(raw)
            if not math.isfinite(value):
                raise ValueError("JSON numbers must be finite")
        else:
            value = int(raw)
        deliver(value)
        return match.end()

    while True:
        position = skip_whitespace(position)
        if not stack:
            if result is _MISSING:
                position = parse_value(position)
                continue
            if position != length:
                raise ValueError("trailing data after JSON value")
            return result

        frame = stack[-1]
        token = text[position] if position < length else ""
        if frame["kind"] == "array":
            if frame["state"] == "first":
                if token == "]":
                    stack.pop()
                    position += 1
                    deliver(frame["value"])
                else:
                    frame["state"] = "value"
                    position = parse_value(position)
            elif frame["state"] == "value":
                position = parse_value(position)
            elif token == ",":
                frame["state"] = "value"
                position += 1
            elif token == "]":
                stack.pop()
                position += 1
                deliver(frame["value"])
            else:
                raise ValueError("JSON array delimiter is invalid")
            continue

        if frame["state"] == "first":
            if token == "}":
                stack.pop()
                position += 1
                deliver(frame["value"])
            else:
                frame["state"] = "key"
            continue
        if frame["state"] == "key":
            if token != '"':
                raise ValueError("JSON object key must be a string")
            key, position = scanstring(text, position + 1, True)
            frame["key"] = key
            frame["state"] = "colon"
            continue
        if frame["state"] == "colon":
            if token != ":":
                raise ValueError("JSON object key is missing ':'")
            frame["state"] = "value"
            position += 1
            continue
        if frame["state"] == "value":
            position = parse_value(position)
            continue
        if token == ",":
            frame["state"] = "key"
            position += 1
        elif token == "}":
            stack.pop()
            position += 1
            deliver(frame["value"])
        else:
            raise ValueError("JSON object delimiter is invalid")


def encode_json(value: Any, *, sort_keys: bool = False) -> bytes:
    """以非递归方式编码一个有限 JSON 值。"""

    output: list[str] = []
    stack: list[tuple[str, Any]] = [("value", value)]
    while stack:
        kind, item = stack.pop()
        if kind == "token":
            output.append(item)
            continue
        if item is None:
            output.append("null")
        elif type(item) is bool:
            output.append("true" if item else "false")
        elif type(item) is int:
            output.append(str(item))
        elif type(item) is float:
            if not math.isfinite(item):
                raise ValueError("JSON numbers must be finite")
            output.append(repr(item))
        elif isinstance(item, str):
            output.append(encode_basestring(item))
        elif isinstance(item, list):
            output.append("[")
            stack.append(("token", "]"))
            for index in range(len(item) - 1, -1, -1):
                stack.append(("value", item[index]))
                if index:
                    stack.append(("token", ","))
        elif isinstance(item, dict):
            output.append("{")
            stack.append(("token", "}"))
            entries = sorted(item.items()) if sort_keys else list(item.items())
            for index in range(len(entries) - 1, -1, -1):
                key, child = entries[index]
                if not isinstance(key, str):
                    raise ValueError("JSON object keys must be strings")
                stack.append(("value", child))
                stack.append(("token", ":"))
                stack.append(("token", encode_basestring(key)))
                if index:
                    stack.append(("token", ","))
        else:
            raise ValueError(f"{type(item).__name__} is not a JSON value")
    return "".join(output).encode("utf-8")


def strict_json_equal(left: Any, right: Any) -> bool:
    """迭代比较 JSON 值，并区分 bool、int 与 float。"""

    pending = [(left, right)]
    while pending:
        left_item, right_item = pending.pop()
        if type(left_item) is not type(right_item):
            return False
        if isinstance(left_item, dict):
            if left_item.keys() != right_item.keys():
                return False
            pending.extend((value, right_item[key]) for key, value in left_item.items())
        elif isinstance(left_item, list):
            if len(left_item) != len(right_item):
                return False
            pending.extend(zip(left_item, right_item, strict=True))
        elif left_item != right_item:
            return False
    return True


__all__ = [
    "MAX_BACKEND_JSON_DEPTH",
    "decode_json_bytes",
    "encode_json",
    "strict_json_equal",
]
