# -*- coding: utf-8 -*-
"""只读查询混匀主机的溶解度判定。

协议为原始 TCP + UTF-8 JSONL（混匀结果接口 2.6.0，端口 8766）。
按 orderName 精确匹配，不按返回顺序贴到 CSV 行上。
"""
from __future__ import annotations

import json
import socket
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence
from urllib.parse import urlparse

MIXING_HOST = "172.16.28.128"
MIXING_PORT = 8766
PROTOCOL = "dissolution-results-v1"
ORDER_NAME_BATCH = 100
PAGE_LIMIT = 200
MAX_PAGES = 50
CLOCK_SKEW = timedelta(seconds=60)
JUDGMENT_COLUMN = "Judgment of mixing state"


class MixingQueryError(RuntimeError):
    """混匀查询没有拿到完整、可用的结果页。"""


def mixing_column_enabled(api_host: str) -> bool:
    """仅实机 172.16.28.128 导出溶解度列。虚拟机 .127 及其他地址不加列。"""
    if not api_host or not str(api_host).strip():
        return False
    text = str(api_host).strip()
    if "://" not in text:
        text = "http://" + text
    return (urlparse(text).hostname or "") == MIXING_HOST


def parse_processed_at(value: Any) -> Optional[datetime]:
    """解析带时区的 processed_at。缺失、空串或无法解析时返回 None。"""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        stamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        return None
    return stamp.astimezone()


def judgment_window(batch_started_at: float, now: Optional[datetime] = None) -> tuple[datetime, datetime]:
    """本轮起点前 60 秒到导出时刻后 60 秒，用来吃掉时钟偏差。"""
    start = datetime.fromtimestamp(batch_started_at).astimezone() - CLOCK_SKEW
    current = now.astimezone() if now is not None and now.tzinfo else (now or datetime.now()).astimezone()
    return start, current + CLOCK_SKEW


def pick_latest_judgments(
    records: Iterable[Dict[str, Any]],
    allowed_names: Iterable[str],
    window_start: datetime,
    window_end: datetime,
) -> Dict[str, str]:
    """同一 orderName 在时间窗内取 processed_at 最新的一条。窗外或对不上的名字丢弃。"""
    allowed = {name for name in allowed_names if name}
    best: Dict[str, tuple[datetime, int]] = {}
    for record in records:
        name = record.get("orderName")
        if name not in allowed:
            continue
        judgment = record.get("judgment")
        if isinstance(judgment, bool) or not isinstance(judgment, int) or judgment not in (-1, 0, 1):
            continue
        stamp = parse_processed_at(record.get("processed_at"))
        if stamp is None or stamp < window_start or stamp > window_end:
            continue
        previous = best.get(name)
        if previous is None or stamp >= previous[0]:
            best[name] = (stamp, judgment)
    return {name: str(item[1]) for name, item in best.items()}


def _exchange(host: str, port: int, request: Dict[str, Any], timeout: float) -> List[Dict[str, Any]]:
    payload = (json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    frames: List[Dict[str, Any]] = []
    with socket.create_connection((host, port), timeout=timeout) as conn:
        conn.settimeout(timeout)
        conn.sendall(payload)
        buf = b""
        while True:
            chunk = conn.recv(8192)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                if not raw.strip():
                    continue
                if len(raw) > 65536:
                    raise MixingQueryError("混匀返回帧过长")
                try:
                    frame = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise MixingQueryError("混匀返回不是有效 JSON") from exc
                if not isinstance(frame, dict):
                    raise MixingQueryError("混匀返回帧不是 JSON 对象")
                frames.append(frame)
                if frame.get("type") in ("query_end", "error"):
                    return frames
    raise MixingQueryError("混匀主机未返回完整查询页")


def _query_page(
    host: str,
    port: int,
    order_names: Sequence[str],
    after: int,
    snapshot: Dict[str, Any],
    timeout: float,
) -> tuple[List[Dict[str, Any]], bool, int, Dict[str, Any]]:
    request: Dict[str, Any] = {
        "type": "query",
        "order_names": list(order_names),
        "after_seq": after,
        "limit": PAGE_LIMIT,
    }
    if snapshot:
        request["stream_id"] = snapshot["stream_id"]
        request["snapshot_seq"] = snapshot["snapshot_seq"]
        if "query_revision" in snapshot:
            request["query_revision"] = snapshot["query_revision"]

    frames = _exchange(host, port, request, timeout)
    allowed = set(order_names)
    begun = False
    rows: List[Dict[str, Any]] = []
    end: Optional[Dict[str, Any]] = None
    pinned = dict(snapshot)
    for frame in frames:
        kind = frame.get("type")
        if kind == "error":
            code = frame.get("code")
            raise MixingQueryError(f"混匀主机拒绝查询: {code if isinstance(code, str) else 'UNKNOWN'}")
        if not begun:
            if kind != "query_begin" or frame.get("protocol") != PROTOCOL:
                raise MixingQueryError("混匀主机没有返回查询握手")
            stream_id = frame.get("stream_id")
            snapshot_seq = frame.get("snapshot_seq")
            if not isinstance(stream_id, str) or not stream_id:
                raise MixingQueryError("query_begin 缺少 stream_id")
            if isinstance(snapshot_seq, bool) or not isinstance(snapshot_seq, int):
                raise MixingQueryError("query_begin 缺少 snapshot_seq")
            if pinned and (pinned.get("stream_id") != stream_id or pinned.get("snapshot_seq") != snapshot_seq):
                raise MixingQueryError("分页期间混匀结果库发生变化")
            pinned = {"stream_id": stream_id, "snapshot_seq": snapshot_seq}
            revision = frame.get("query_revision")
            if isinstance(revision, int) and not isinstance(revision, bool):
                pinned["query_revision"] = revision
            begun = True
            continue
        if kind == "result":
            name = frame.get("orderName")
            judgment = frame.get("judgment")
            if name in allowed and not isinstance(judgment, bool) and judgment in (-1, 0, 1):
                rows.append({
                    "orderName": name,
                    "judgment": judgment,
                    "processed_at": frame.get("processed_at"),
                })
            continue
        if kind == "query_end":
            end = frame
            break
    if not begun or end is None:
        raise MixingQueryError("混匀查询页不完整")
    has_more = end.get("has_more")
    next_after = end.get("next_after_seq")
    if not isinstance(has_more, bool) or isinstance(next_after, bool) or not isinstance(next_after, int):
        raise MixingQueryError("query_end 分页信息不完整")
    if has_more and next_after <= after:
        raise MixingQueryError("混匀查询分页没有前进")
    return rows, has_more, next_after, pinned


def fetch_mixing_records(
    order_names: Sequence[str],
    host: str = MIXING_HOST,
    port: int = MIXING_PORT,
    timeout: float = 15.0,
) -> List[Dict[str, Any]]:
    """按样品编号查询。单次最多 100 个编号，超出自动分批。"""
    names: List[str] = []
    seen = set()
    for name in order_names:
        if isinstance(name, str) and name and name not in seen:
            seen.add(name)
            names.append(name)
    records: List[Dict[str, Any]] = []
    for offset in range(0, len(names), ORDER_NAME_BATCH):
        chunk = names[offset:offset + ORDER_NAME_BATCH]
        after = 0
        snapshot: Dict[str, Any] = {}
        for _ in range(MAX_PAGES):
            rows, has_more, after, snapshot = _query_page(host, port, chunk, after, snapshot, timeout)
            records.extend(rows)
            if not has_more:
                break
        else:
            raise MixingQueryError("混匀查询分页超过上限")
    return records


def lookup_mixing_judgments(
    order_names: Sequence[str],
    batch_started_at: float,
    host: str = MIXING_HOST,
    port: int = MIXING_PORT,
    timeout: float = 15.0,
    now: Optional[datetime] = None,
) -> Dict[str, str]:
    """返回 orderName → \"1\"/\"0\"/\"-1\"。时间窗外、对不上或查不到的名字不出现在结果里。"""
    records = fetch_mixing_records(order_names, host=host, port=port, timeout=timeout)
    window_start, window_end = judgment_window(batch_started_at, now=now)
    return pick_latest_judgments(records, order_names, window_start, window_end)
