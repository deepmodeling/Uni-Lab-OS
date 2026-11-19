"""dora 消息编解码：Python 对象 <-> pyarrow 数组。

dora 节点间以 pyarrow 数组传递数据（本机走共享内存、零拷贝）。这里统一约定：
业务负载序列化为紧凑 JSON，再以 uint8 字节数组承载，便于精确控制消息大小并做零拷贝解码。

消息信封（envelope）统一字段：
    t_send_ns : 发送方 time.perf_counter_ns()（同机单调时钟，可直接做单向时延差）
    seq       : 序号
    op        : 操作类型（echo / burst / action / status / result ...）
    body      : 业务数据
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict

import pyarrow as pa


def now_ns() -> int:
    """同机单调时钟（纳秒）。同一物理机上不同进程可直接相减得到单向时延。"""
    return time.perf_counter_ns()


def encode(obj: Dict[str, Any]) -> pa.Array:
    """把 dict 编码为 uint8 的 pyarrow 数组。"""
    data = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return pa.array(data, type=pa.uint8())


def decode(arrow_value: pa.Array) -> Dict[str, Any]:
    """把 dora 传来的 uint8 pyarrow 数组解码回 dict。"""
    raw = arrow_value.to_numpy(zero_copy_only=False).tobytes()
    return json.loads(raw.decode("utf-8"))


def make_pad(size_bytes: int) -> str:
    """构造指定字节数的填充串，用于测试不同消息体量下的通信性能。"""
    if size_bytes <= 0:
        return ""
    return "x" * size_bytes
