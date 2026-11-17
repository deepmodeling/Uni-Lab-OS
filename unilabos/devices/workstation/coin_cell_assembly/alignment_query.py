# -*- coding: utf-8 -*-
"""只读查询扣电对中同轴度，并按电池编号回填组装 CSV。

被监控端固定 172.16.28.102:19102，文本行协议（对中接口 v1.2.0）。
每一行只用自己的 coin_cell_code 查询，返回编号不一致则留空。
"""
from __future__ import annotations

import csv
import os
import socket
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from unilabos.utils.log import logger

ALIGN_HOST = "172.16.28.102"
ALIGN_PORT = 19102
ALIGNMENT_CSV_COLUMNS = (
    "pos_sheet_to_pos_case",
    "neg_sheet_to_pos_case",
    "pos_sheet_to_neg_sheet",
    "coaxiality",
)
STALE_BEFORE = timedelta(minutes=30)
EMPTY_VALUES: Tuple[str, str, str, str] = ("", "", "", "")

AlignmentLookup = Callable[[str], Optional[Dict[str, object]]]


class AlignmentUnavailable(OSError):
    """对中机连不上或本次查询超时。调用方应停止后续查询。"""


def _parse_stamp(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.strptime(value.strip(), "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def parse_alignment_line(text: str) -> Optional[Dict[str, object]]:
    """解析一行 ROW。编号或四列缺失时仍返回已有字段，由调用方决定是否采用。"""
    try:
        fields = next(csv.reader([text]))
    except csv.Error:
        return None
    if len(fields) < 3 or fields[0] != "ROW":
        return None
    values = list(fields[3:7])
    while len(values) < 4:
        values.append("")
    return {"time": fields[1], "code": fields[2], "values": tuple(values[:4])}


def measurement_belongs_to_row(
    row_index: int,
    assembly_time: str,
    align_time: object,
    siblings: Sequence[Tuple[int, str]],
) -> bool:
    """同一编号只保留最新一条时，把结果归到不晚于该测量、且之后没有同号电池的那一行。"""
    built = _parse_stamp(assembly_time)
    measured = _parse_stamp(align_time)
    if built is None or measured is None:
        return False
    if measured < built - STALE_BEFORE:
        return False
    for other_index, other_time in siblings:
        if other_index == row_index:
            continue
        other = _parse_stamp(other_time)
        if other is None:
            continue
        if built < other <= measured:
            return False
        if other == built and other_index > row_index and measured >= built:
            return False
    return True


def alignment_code_candidates(code: str) -> List[str]:
    """先用 CSV 原文查。读码把编号固定截成 8 位时，短编号末尾会多一个寄存器里的 5。

    8 位编号（如 YS807728）原文就能对上，不会走到第二候选。
    只有原文查不到时才去掉这一个末尾 5，例如 15031005 → 1503100。
    """
    if not code or code == "N/A":
        return []
    candidates = [code]
    trimmed = code[:-1]
    if len(code) == 8 and code.endswith("5") and trimmed and trimmed not in candidates:
        candidates.append(trimmed)
    return candidates


def alignment_codes_match(csv_code: str, reported_code: object) -> bool:
    """对中返回的编号必须是本行原文，或原文去掉末尾多余 5 之后的那一个。"""
    if not isinstance(reported_code, str) or not csv_code:
        return False
    return reported_code in alignment_code_candidates(csv_code)


def resolve_alignment_row(code: str, query: AlignmentLookup) -> Optional[Dict[str, object]]:
    """按候选编号依次查询，只收返回编号与本次所查编号完全一致的那一行。"""
    for candidate in alignment_code_candidates(code):
        found = query(candidate)
        if found is not None and found.get("code") == candidate:
            return found
    return None


def values_for_battery(
    code: str,
    assembly_time: str,
    row: Optional[Dict[str, object]],
) -> Tuple[str, str, str, str]:
    """编号必须与本行对应，且对中时间不能早于组装时间超过 30 分钟。"""
    if not code or code == "N/A" or row is None or not alignment_codes_match(code, row.get("code")):
        return EMPTY_VALUES
    align_time = _parse_stamp(row.get("time"))
    built_at = _parse_stamp(assembly_time)
    if align_time is None or built_at is None:
        return EMPTY_VALUES
    if align_time < built_at - STALE_BEFORE:
        return EMPTY_VALUES
    raw = row.get("values")
    if not isinstance(raw, tuple) or len(raw) != 4:
        return EMPTY_VALUES
    return (
        "" if raw[0] is None else str(raw[0]),
        "" if raw[1] is None else str(raw[1]),
        "" if raw[2] is None else str(raw[2]),
        "" if raw[3] is None else str(raw[3]),
    )


def query_alignment_row(
    code: str,
    host: str = ALIGN_HOST,
    port: int = ALIGN_PORT,
    timeout: float = 5.0,
) -> Optional[Dict[str, object]]:
    """按单个电池编号查询。MISS 或没有对得上的 ROW 时返回 None。"""
    if not code or any(ch in code for ch in ",\r\n"):
        return None
    try:
        conn = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        raise AlignmentUnavailable(str(exc)) from exc
    try:
        conn.settimeout(timeout)
        conn.sendall(f"QUERY {code}\n".encode("utf-8"))
        buf = b""
        matched: Optional[Dict[str, object]] = None
        while True:
            try:
                chunk = conn.recv(4096)
            except socket.timeout as exc:
                raise AlignmentUnavailable("对中查询超时") from exc
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                text = raw.decode("utf-8", "ignore").strip()
                if text.startswith("ROW,"):
                    parsed = parse_alignment_line(text)
                    if parsed is not None and parsed.get("code") == code:
                        matched = parsed
                elif text.startswith("END,"):
                    return matched
        return matched
    finally:
        conn.close()


def backfill_alignment_csv(
    csv_path: str,
    lookup: Optional[AlignmentLookup] = None,
) -> int:
    """按每行 coin_cell_code 回填四列并原地重写 CSV。连接失败只告警一次。

    Returns:
        成功写入非空同轴度数据的行数
    """
    query = lookup or query_alignment_row
    with open(csv_path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames: List[str] = list(reader.fieldnames or [])
        rows = list(reader)
    for column in ALIGNMENT_CSV_COLUMNS:
        if column not in fieldnames:
            fieldnames.append(column)

    unavailable = False
    filled = 0
    siblings: Dict[str, List[Tuple[int, str]]] = {}
    indexed_codes: List[str] = []
    for index, row in enumerate(rows):
        code = (row.get("coin_cell_code") or "").strip()
        indexed_codes.append(code)
        siblings.setdefault(code, []).append((index, row.get("Time") or ""))

    cached: Dict[str, Optional[Dict[str, object]]] = {}
    for index, row in enumerate(rows):
        code = indexed_codes[index]
        assembly_time = row.get("Time") or ""
        found: Optional[Dict[str, object]] = None
        if code and code != "N/A":
            if code not in cached and not unavailable:
                try:
                    cached[code] = resolve_alignment_row(code, query)
                except AlignmentUnavailable as exc:
                    logger.warning(f"[对中回填] 连接失败，本批后续电池不再查询: {exc}")
                    unavailable = True
                    cached[code] = None
                except OSError as exc:
                    logger.warning(f"[对中回填] 连接失败，本批后续电池不再查询: {exc}")
                    unavailable = True
                    cached[code] = None
            found = cached.get(code)
        values = values_for_battery(code, assembly_time, found)
        if values != EMPTY_VALUES and not measurement_belongs_to_row(
            index, assembly_time, None if found is None else found.get("time"), siblings.get(code, [])
        ):
            values = EMPTY_VALUES
        for column, value in zip(ALIGNMENT_CSV_COLUMNS, values):
            row[column] = value
        if any(values):
            filled += 1

    for row in rows:
        for key in fieldnames:
            if row.get(key) is None:
                row[key] = ""

    temporary = csv_path + ".align.tmp"
    try:
        with open(temporary, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, csv_path)
    except Exception:
        if os.path.exists(temporary):
            os.remove(temporary)
        raise
    return filled
