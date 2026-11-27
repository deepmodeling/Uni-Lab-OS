"""冻结时间线：把一次任务的回放记录（replay-bundle 表头 + 行）定格成按帧的状态序列。

渲染只读冻结时间线，不再访问 OS：同一份时间线 + 同一个引擎适配器版本 = 同一段片子。
时间线带两个摘要：``source_sha256`` 是回放记录的规范 JSON 摘要，``timeline_sha256`` 是
时间线本身（不含该字段）的摘要，成片合同用它确认“片子是由哪一份时间线出的”。

时间策略：
* ``log``：渲染时间 = 日志时间；
* ``focus``：没有作业在执行（event == "idle"）的空闲段超过 ``idle_cap_s`` 时压到 ``idle_cap_s``，
  有作业的段原速保留。
"""

from __future__ import annotations

import hashlib
import json
import math
from bisect import bisect_right
from typing import Any, Iterable

SCHEMA = "unilab.frozen-timeline/1"


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def _idle(row: dict[str, Any]) -> bool:
    return str(row.get("event") or "idle") in ("idle", "")


def derive_commands(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """表头没有作业区间时（高频 MCAP 只有运动段名），按相邻同名 event 合并成段，
    设备取这一段里属性变化最多的节点。"""
    spans: list[dict[str, Any]] = []
    for row in rows:
        if _idle(row):
            continue
        event = str(row["event"])
        if spans and spans[-1]["label"] == event and float(row["t"]) - spans[-1]["t_end"] <= 1.0:
            span = spans[-1]
        else:
            span = {"id": f"segment-{len(spans) + 1}", "label": event, "t_start": float(row["t"]),
                    "t_end": float(row["t"]), "_changes": {}}
            spans.append(span)
        span["t_end"] = float(row["t"])
        for node, values in (row.get("values") or {}).items():
            span["_changes"][node] = span["_changes"].get(node, 0) + len(values)
    for span in spans:
        changes = span.pop("_changes")
        if changes:
            span["device"] = max(changes, key=changes.get)
    return spans


def _time_map(rows: list[dict[str, Any]], policy: str, idle_cap_s: float) -> tuple[list[float], list[float]]:
    """分段线性映射的折点：(源时间, 渲染时间)。"""
    if policy not in ("log", "focus"):
        raise ValueError(f"未知时间策略: {policy}")
    times = [float(row["t"]) for row in rows]
    if not times:
        return [0.0], [0.0]
    source, render = [times[0]], [0.0]
    for previous, current in zip(rows, rows[1:]):
        gap = float(current["t"]) - float(previous["t"])
        idle = policy == "focus" and _idle(previous) and gap > idle_cap_s
        source.append(float(current["t"]))
        render.append(render[-1] + (idle_cap_s if idle else gap))
    return source, render


def _render_to_source(render_t: float, source: list[float], render: list[float]) -> float:
    index = bisect_right(render, render_t) - 1
    if index < 0:
        return source[0]
    if index >= len(render) - 1:
        return source[-1]
    span = render[index + 1] - render[index]
    if span <= 0:
        return source[index + 1]
    return source[index] + (render_t - render[index]) / span * (source[index + 1] - source[index])


def _source_to_render(source_t: float, source: list[float], render: list[float]) -> float:
    index = bisect_right(source, source_t) - 1
    if index < 0:
        return render[0]
    if index >= len(source) - 1:
        return render[-1] + (source_t - source[-1])
    span = source[index + 1] - source[index]
    if span <= 0:
        return render[index + 1]
    return render[index] + (source_t - source[index]) / span * (render[index + 1] - render[index])


def freeze(header: dict[str, Any], rows: list[dict[str, Any]], *, fps: int = 24, policy: str = "focus",
           idle_cap_s: float = 2.0, nodes: Iterable[str] | None = None,
           until_s: float | None = None) -> dict[str, Any]:
    """回放记录 → 冻结时间线。``nodes`` 给出时只保留这些节点（例如只出某台设备的片段）。"""
    if fps <= 0:
        raise ValueError("fps 必须为正")
    rows = sorted(rows, key=lambda row: float(row["t"]))
    if until_s is not None:
        rows = [row for row in rows if float(row["t"]) <= until_s]
        header = {**header, "commands": [c for c in header.get("commands") or [] if float(c["t_start"]) <= until_s]}
    keep = set(nodes) if nodes is not None else None
    source, render = _time_map(rows, policy, idle_cap_s)
    duration = render[-1] if rows else 0.0
    frame_count = max(1, int(math.floor(duration * fps)) + 1)

    frames: list[dict[str, Any]] = []
    state: dict[str, dict[str, Any]] = {}
    cursor = 0
    for frame in range(frame_count):
        source_t = _render_to_source(frame / fps, source, render)
        changes: dict[str, dict[str, Any]] = {}
        while cursor < len(rows) and float(rows[cursor]["t"]) <= source_t + 1e-9:
            for node, values in (rows[cursor].get("values") or {}).items():
                if keep is not None and node not in keep:
                    continue
                for name, value in values.items():
                    if state.get(node, {}).get(name, object()) != value:
                        state.setdefault(node, {})[name] = value
                        changes.setdefault(node, {})[name] = value
            cursor += 1
        if changes or frame == 0:
            frames.append({"f": frame, "t_source": round(source_t, 3), "set": changes})

    commands = []
    for command in header.get("commands") or derive_commands(rows):
        start = _source_to_render(float(command["t_start"]), source, render)
        end = _source_to_render(float(command["t_end"]), source, render)
        device = command.get("device") or str(command.get("label") or "").split(" · ")[0] or None
        commands.append({"id": command["id"], "label": command.get("label"), "device": device,
                         "f_start": int(round(start * fps)), "f_end": int(round(end * fps)),
                         "t_source_start": command["t_start"], "t_source_end": command["t_end"]})

    timeline = {
        "schema": SCHEMA,
        "fps": fps,
        "policy": {"kind": policy, "idle_cap_s": idle_cap_s, **({"until_s": until_s} if until_s is not None else {})},
        "frame_count": frame_count,
        "duration_s": round(duration, 3),
        "source": {**(header.get("source") or {}), "sha256": sha256({"header": header, "rows": rows}),
                   "duration_s": header.get("duration_s")},
        "devices": {node: names for node, names in (header.get("devices") or {}).items()
                    if keep is None or node in keep},
        "commands": commands,
        "frames": frames,
    }
    timeline["timeline_sha256"] = sha256({key: value for key, value in timeline.items() if key != "timeline_sha256"})
    return timeline


def state_at(timeline: dict[str, Any], frame: int) -> dict[str, dict[str, Any]]:
    """第 ``frame`` 帧的完整状态（逐帧 delta 累积）。"""
    state: dict[str, dict[str, Any]] = {}
    for item in timeline["frames"]:
        if item["f"] > frame:
            break
        for node, values in item["set"].items():
            state.setdefault(node, {}).update(values)
    return state


def verify(timeline: dict[str, Any]) -> None:
    """时间线自洽：摘要对得上、帧号递增且在范围内。"""
    if timeline.get("schema") != SCHEMA:
        raise ValueError("不是冻结时间线")
    expected = sha256({key: value for key, value in timeline.items() if key != "timeline_sha256"})
    if timeline.get("timeline_sha256") != expected:
        raise ValueError("时间线摘要不符")
    frames = [item["f"] for item in timeline["frames"]]
    if frames != sorted(set(frames)) or (frames and (frames[0] != 0 or frames[-1] >= timeline["frame_count"])):
        raise ValueError("帧号不递增或越界")
