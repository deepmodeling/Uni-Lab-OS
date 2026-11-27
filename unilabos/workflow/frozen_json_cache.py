"""大体量持久 JSON 字段（冻结执行计划、工作流快照）的进程内解码缓存。

背景：任务一旦创建，``workflow_task.execution_plan`` 与 ``workflow_snapshot`` 就是
冻结事实，之后只在首次派发前的库存资源绑定处整体重写一次。但每个作业完成的
投影（就绪输出边界、保留资源区间、终态区间对账）与桥接层的 ``get_task`` 都会把
这两列从 SQLite 读出并完整解码。pTLC 全流程（5383 节点，计划 42.6 MB、快照
9.6 MB）实测单次 ``get_task`` 约 0.7 s，每个作业完成要重复十次上下，相邻设备
动作之间的调度空档因此达到秒级，且随图规模线性增长。

本模块按**文本内容**缓存解码结果：命中要求长度、首尾片段和整段字符串完全相等，
所以计划被重写后旧缓存自然失效，不需要任何显式失效逻辑。返回值的顶层容器是
每次新建的浅拷贝，避免调用方对顶层键的写入污染缓存；嵌套结构在调用方之间
共享，调用方必须把它当作只读事实——仓库内所有原地修改计划或快照的路径
（``prepare_task_input``、``bind_inventory_resource_plan``、
``ExecutionPlanBuilder.refresh_resource_plan``、``_scope_debug_task_input``）都先
``clone_json``/``deepcopy``/``dict()`` 再改，本模块以此为前提。
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any, Callable

# 小于该长度的字段直接解码：作业参数、回执之类的小 JSON 不值得比较整段文本。
CACHE_MIN_TEXT_LENGTH = 256 * 1024
# 通常只有当前任务的计划与快照两份；多留几份给并行任务，避免抖动即可。
CACHE_MAX_ENTRIES = 6
_PROBE_LENGTH = 4096

_lock = threading.Lock()
_entries: "OrderedDict[tuple[int, str, str], tuple[str, Any]]" = OrderedDict()
_stats = {"hits": 0, "misses": 0}


def _probe_key(text: str) -> tuple[int, str, str]:
    return (len(text), text[:_PROBE_LENGTH], text[-_PROBE_LENGTH:])


def _fresh_top_level(value: Any) -> Any:
    """顶层容器每次新建，嵌套内容共享；调用方按只读事实使用。"""

    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, list):
        return list(value)
    return value


def decode_large_json_text(text: str, decoder: Callable[[str], Any]) -> Any:
    """解码大体量 JSON 文本；同一内容在进程内只解码一次。

    参数：``text`` 是数据库字段文本；``decoder`` 是调用方原本使用的解码函数，
    缓存未命中或文本低于阈值时原样调用，保留其全部预算与拒绝规则。
    返回：解码值；命中时为缓存对象的顶层浅拷贝。
    """

    if len(text) < CACHE_MIN_TEXT_LENGTH:
        return decoder(text)
    key = _probe_key(text)
    with _lock:
        entry = _entries.get(key)
        if entry is not None and entry[0] == text:
            _entries.move_to_end(key)
            _stats["hits"] += 1
            return _fresh_top_level(entry[1])
    decoded = decoder(text)
    with _lock:
        _stats["misses"] += 1
        _entries[key] = (text, decoded)
        _entries.move_to_end(key)
        while len(_entries) > CACHE_MAX_ENTRIES:
            _entries.popitem(last=False)
    return _fresh_top_level(decoded)


def cache_stats() -> dict[str, int]:
    """返回命中/未命中计数（诊断与测试用）。"""

    with _lock:
        return {**_stats, "entries": len(_entries)}


def clear_cache() -> None:
    """清空缓存（测试用）。"""

    with _lock:
        _entries.clear()
        _stats["hits"] = 0
        _stats["misses"] = 0


__all__ = [
    "CACHE_MAX_ENTRIES",
    "CACHE_MIN_TEXT_LENGTH",
    "cache_stats",
    "clear_cache",
    "decode_large_json_text",
]
