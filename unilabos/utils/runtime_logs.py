"""有界读取日志文件；文件只能由应用选择，HTTP / HostLink 不接受任意路径。"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from unilabos.protocol.runtime.logs import RuntimeLogBatch, RuntimeLogLine, RuntimeLogQuery

MAX_READ_BYTES = 256 * 1024
MAX_LINE_CHARS = 16 * 1024
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def current_log_path() -> Path | None:
    from unilabos.utils.log import get_log_file_path

    value = get_log_file_path()
    return Path(value) if value else None


def read_log_file(
    path: Path,
    *,
    source_id: str,
    cursor: str = "",
    limit: int = 300,
    pid: int | None = None,
) -> RuntimeLogBatch:
    """首读尾部，后续从游标继续；重启/替换/截断回到新文件尾部并标记 reset。

    一次最多读 256 KiB、返回 1000 行；超长行显式截断，不让单行撑爆响应。
    未写完的普通行留待下次（保证中文 UTF-8 字符及 traceback 行不会重复/丢失）。
    """
    query = RuntimeLogQuery(cursor=cursor, limit=limit)
    with path.open("rb") as handle:
        stat = os.fstat(handle.fileno())
        identity = f"{source_id}|{path.resolve()}|{stat.st_dev}|{stat.st_ino}"
        stream_id = hashlib.sha256(identity.encode()).hexdigest()[:24]
        size = stat.st_size
        old_stream, _, old_offset = query.cursor.partition(":")
        reset = bool(cursor) and (old_stream != stream_id or int(old_offset) > size)
        initial = not cursor or reset
        start = max(0, size - MAX_READ_BYTES) if initial else int(old_offset)
        handle.seek(start)
        raw = handle.read(min(MAX_READ_BYTES, size - start))

    truncated = initial and start > 0
    if initial and start > 0:
        # 尾读可能切入半行；不要把这半行当完整日志。无换行的巨型行由下面的分支处理。
        first_newline = raw.find(b"\n")
        if first_newline >= 0:
            start += first_newline + 1
            raw = raw[first_newline + 1:]

    lines: list[RuntimeLogLine] = []
    offset = start
    for part in raw.split(b"\n")[:-1]:
        segment = part + b"\n"
        text = _ANSI.sub("", segment.decode("utf-8", errors="replace").rstrip("\r\n"))
        if len(text) > MAX_LINE_CHARS:
            text = text[:MAX_LINE_CHARS] + " … [该行已截断]"
            truncated = True
        lines.append(RuntimeLogLine(offset=offset, text=text))
        offset += len(segment)
        if not initial and len(lines) >= limit:
            break

    # 没有换行且已达读取上限：明确展示截断片段并推进，不能永远卡在同一巨型行。
    if not lines and len(raw) >= MAX_READ_BYTES:
        text = _ANSI.sub("", raw.decode("utf-8", errors="replace"))
        lines.append(RuntimeLogLine(offset=start, text=text[:MAX_LINE_CHARS] + " … [该行已截断]"))
        offset = start + len(raw)
        truncated = True
    if initial and len(lines) > limit:
        lines = lines[-limit:]
        truncated = True
    # 只剩尚未结束的短行不要求客户端立即追赶，等下一轮文件追加后再读。
    has_more = offset < size and (
        (not initial and len(lines) >= limit) or start + len(raw) < size
    )
    return RuntimeLogBatch(
        source_id=source_id, stream_id=stream_id, cursor=f"{stream_id}:{offset}",
        lines=lines, has_more=has_more, reset=reset, truncated=truncated,
        path=str(path), pid=pid,
    )


def read_current_log(data: dict) -> dict:
    """HostLink 下行只读本进程日志，不能指定路径或转读其它进程。"""
    query = RuntimeLogQuery.model_validate(data)
    path = current_log_path()
    if path is None:
        raise FileNotFoundError("当前进程未配置日志文件，请重启并使用标准 unilab 入口")
    return read_log_file(path, source_id="self", pid=os.getpid(), **query.model_dump()).model_dump()
