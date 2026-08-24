r"""Python 版节点传参解析，对齐 Go gjson/sjson 语义子集。

Go 侧（dag.go parsePreNodeParam，snapshot 行 600-670）：

1. ``res = gjson.Get(父节点返回值JSON, source_handle.data_key)``
2. ``dataKeys = strings.Split(target_handle.data_key, "@@@")``
   前 n-1 段逐层 ``gjson.Get`` 继续下钻 res，最后一段作为 set 路径
3. ``sjson.Set(node.Param, setKey, res.Value())``

本实现覆盖工作流实际使用的 gjson 路径子集：

- dot 路径：``a.b.c``
- 数组下标：``a.1.b``（数字段在 list 上是索引）
- ``\.`` 转义字面点号
- ``#`` 取数组长度（gjson 行为）

sjson.Set 子集：dot 路径逐层创建缺失的 dict；数字段在 list 上按索引赋值，
超界时以 None 填充扩容（sjson 对 array 的 pad 行为）；在 dict 上数字段作为普通 key。
"""

from __future__ import annotations

import json
from typing import Any, List, Tuple

from unilabos.server.scheduler.models import DATA_KEY_SPLIT, HandlePair


class ParamResolveError(Exception):
    """对齐 Go code.ValueNotExistErr / CanNotGetParentJobErr 一类的解析失败。"""


_MISSING = object()


def split_path(path: str) -> List[str]:
    """按未转义的 ``.`` 分段，支持 ``\\.`` 转义（gjson 语义）。"""
    segments: List[str] = []
    cur: List[str] = []
    escaped = False
    for ch in path:
        if escaped:
            cur.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ".":
            segments.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    segments.append("".join(cur))
    return segments


def _get_segment(value: Any, segment: str) -> Any:
    if isinstance(value, dict):
        return value.get(segment, _MISSING)
    if isinstance(value, list):
        if segment == "#":
            return len(value)
        if segment.isdigit():
            idx = int(segment)
            return value[idx] if idx < len(value) else _MISSING
        return _MISSING
    return _MISSING


def json_get(value: Any, path: str) -> Any:
    """gjson.Get 等价：路径不存在时返回 _MISSING 哨兵（调用方判 exists）。"""
    if path == "":
        return _MISSING
    cur = value
    for segment in split_path(path):
        cur = _get_segment(cur, segment)
        if cur is _MISSING:
            return _MISSING
    return cur


def json_get_exists(value: Any, path: str) -> Tuple[bool, Any]:
    res = json_get(value, path)
    if res is _MISSING:
        return False, None
    return True, res


def json_set(target: Any, path: str, value: Any) -> Any:
    """sjson.Set 等价：返回新的顶层对象（不修改入参）。

    - 缺失的中间层创建 dict
    - list 上数字段按索引赋值，超界以 None 填充到位（sjson pad 行为）
    - dict 上数字段作为字符串 key
    """
    segments = split_path(path)
    if not segments or segments == [""]:
        raise ParamResolveError(f"invalid set path: {path!r}")

    root = _clone_top(target)
    cur = root
    for i, segment in enumerate(segments):
        last = i == len(segments) - 1
        if isinstance(cur, list):
            if not segment.isdigit():
                raise ParamResolveError(f"non-numeric segment {segment!r} on array in path {path!r}")
            idx = int(segment)
            while len(cur) <= idx:
                cur.append(None)
            if last:
                cur[idx] = value
            else:
                nxt = cur[idx]
                nxt = _clone_or_new(nxt, segments[i + 1])
                cur[idx] = nxt
                cur = nxt
        elif isinstance(cur, dict):
            if last:
                cur[segment] = value
            else:
                nxt = cur.get(segment)
                nxt = _clone_or_new(nxt, segments[i + 1])
                cur[segment] = nxt
                cur = nxt
        else:
            raise ParamResolveError(f"cannot descend into scalar at segment {segment!r} of {path!r}")
    return root


def _clone_top(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.loads(json.dumps(value))
    if value is None:
        return {}
    raise ParamResolveError(f"set target must be object/array, got {type(value).__name__}")


def _clone_or_new(existing: Any, next_segment: str) -> Any:
    if isinstance(existing, (dict, list)):
        return existing
    # sjson: 中间层缺失时创建 object（我们不自动创建 array，与实际用例一致）
    return {}


def resolve_parent_params(
    node_param: Any,
    pairs: List[HandlePair],
    parent_ret_values: "dict[str, Any]",
) -> Any:
    """Go parsePreNodeParam 等价：按 handle 传参对把父节点返回值写入本节点参数。

    node_param: 节点原始参数（dict）
    pairs: 本节点的所有传参边（已按 Go buildNodeHandlePair 规则过滤）
    parent_ret_values: source_node_id → 父节点执行返回值（JSON 反序列化后的对象）

    返回覆写后的新参数对象；任一取值失败抛 ParamResolveError（对齐 Go 直接失败）。
    """
    param = node_param if node_param is not None else {}
    for pair in pairs:
        if pair.source_handle.data_key == "" or pair.target_handle.data_key == "":
            continue

        if pair.source_node_id not in parent_ret_values:
            raise ParamResolveError(
                f"parent ret value missing: source node {pair.source_node_id}"
            )
        ret_value = parent_ret_values[pair.source_node_id]

        exists, res = json_get_exists(ret_value, pair.source_handle.data_key)
        if not exists:
            raise ParamResolveError(
                f"value not exist: source data_key {pair.source_handle.data_key!r} "
                f"on node {pair.source_node_id}"
            )

        data_keys = pair.target_handle.data_key.split(DATA_KEY_SPLIT)
        set_key = data_keys[-1]
        for key in data_keys[:-1]:
            exists, res = json_get_exists(res, key)
            if not exists:
                raise ParamResolveError(
                    f"value not exist: nested key {key!r} of target data_key "
                    f"{pair.target_handle.data_key!r}"
                )

        param = json_set(param, set_key, res)
    return param


__all__ = [
    "ParamResolveError",
    "json_get",
    "json_get_exists",
    "json_set",
    "resolve_parent_params",
    "split_path",
]
