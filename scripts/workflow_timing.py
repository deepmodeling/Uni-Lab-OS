"""临时 workflow 排程计时记录器。"""

from __future__ import annotations

import csv
import html
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _round_seconds(value: float) -> float:
    return round(value, 3)


_ROBOT_TRANSFER_METHOD = re.compile(r"^submit_(place_to|pick_from)_(s\d+)$", re.IGNORECASE)


def _station_transfer(step: dict[str, Any]) -> tuple[str, str] | None:
    match = _ROBOT_TRANSFER_METHOD.match(str(step.get("method", "")))
    if not match:
        return None
    return match.group(1).lower(), match.group(2).upper()


def _transfer_params_match(place: dict[str, Any], pick: dict[str, Any]) -> bool:
    for name in ("position", "product_type", "sample_id"):
        left = place.get("params", {}).get(name)
        right = pick.get("params", {}).get(name)
        if left not in (None, "") and right not in (None, "") and left != right:
            return False
    return True


class WorkflowTimingRecorder:
    """记录 workflow 总时长、逐节点时长和 OPC 等待明细。"""

    def __init__(self, run_id: str, workflow_name: str, output_dir: Path) -> None:
        self.run_id = run_id
        self.workflow_name = workflow_name
        self.output_dir = output_dir
        self.started_at = _now_iso()
        self._started_monotonic = time.monotonic()
        self._execution_started_monotonic: float | None = None
        self._active_step: dict[str, Any] | None = None
        self._active_step_started_monotonic: float | None = None
        self.steps: list[dict[str, Any]] = []
        self.report_path: Path | None = None
        self.summary_path: Path | None = None

    def mark_execution_started(self) -> None:
        if self._execution_started_monotonic is None:
            self._execution_started_monotonic = time.monotonic()

    def start_step(
        self,
        *,
        index: int,
        total: int,
        node_id: str,
        device_name: str,
        method: str,
        params: dict[str, Any],
    ) -> None:
        self._active_step_started_monotonic = time.monotonic()
        execution_origin = self._execution_started_monotonic or self._started_monotonic
        self._active_step = {
            "index": index,
            "total": total,
            "node_id": node_id,
            "device_name": device_name,
            "resource_type": "robot" if "robot" in device_name.lower() else "station",
            "method": method,
            "params": params,
            "status": "running",
            "started_at": _now_iso(),
            "finished_at": None,
            "start_offset_s": _round_seconds(self._active_step_started_monotonic - execution_origin),
            "end_offset_s": None,
            "duration_s": None,
            "opc_wait_events": [],
            "result": None,
            "error": None,
        }

    def observe_log(self, message: str, detail: dict[str, Any] | None) -> None:
        if self._active_step is None or not isinstance(detail, dict):
            return
        if detail.get("type") != "opc_wait" or detail.get("phase") != "finish":
            return
        self._active_step["opc_wait_events"].append(
            {
                "message": message,
                "wait_kind": detail.get("wait_kind"),
                "context": detail.get("context"),
                "variable": detail.get("variable"),
                "success": detail.get("success"),
                "elapsed_s": detail.get("elapsed"),
            }
        )

    def finish_step(self, *, result: Any = None, error: str | None = None) -> None:
        if self._active_step is None or self._active_step_started_monotonic is None:
            return
        finished_monotonic = time.monotonic()
        duration = finished_monotonic - self._active_step_started_monotonic
        execution_origin = self._execution_started_monotonic or self._started_monotonic
        self._active_step.update(
            {
                "status": "failed" if error else "success",
                "finished_at": _now_iso(),
                "end_offset_s": _round_seconds(finished_monotonic - execution_origin),
                "duration_s": _round_seconds(duration),
                "result": result,
                "error": error,
            }
        )
        self.steps.append(self._active_step)
        self._active_step = None
        self._active_step_started_monotonic = None

    def finish(self, *, status: str, error: str | None = None) -> Path:
        if self._active_step is not None:
            self.finish_step(error=error or f"workflow ended with status={status}")

        finished_monotonic = time.monotonic()
        execution_started = self._execution_started_monotonic
        setup_duration = (
            execution_started - self._started_monotonic
            if execution_started is not None
            else finished_monotonic - self._started_monotonic
        )
        execution_duration = (
            finished_monotonic - execution_started if execution_started is not None else 0.0
        )
        robot_duration = sum(
            float(step["duration_s"] or 0)
            for step in self.steps
            if step["resource_type"] == "robot"
        )
        station_duration = sum(
            float(step["duration_s"] or 0)
            for step in self.steps
            if step["resource_type"] == "station"
        )
        station_dwells = self._calculate_station_dwells()
        station_dwell_summary = self._summarize_station_dwells(station_dwells)
        report = {
            "schema": "unilabos.workflow_timing.v2",
            "run_id": self.run_id,
            "workflow_name": self.workflow_name,
            "status": status,
            "started_at": self.started_at,
            "finished_at": _now_iso(),
            "total_duration_s": _round_seconds(finished_monotonic - self._started_monotonic),
            "setup_duration_s": _round_seconds(setup_duration),
            "execution_duration_s": _round_seconds(execution_duration),
            "robot_total_duration_s": _round_seconds(robot_duration),
            "station_total_duration_s": _round_seconds(station_duration),
            "station_dwells": station_dwells,
            "station_dwell_summary": station_dwell_summary,
            "error": error,
            "steps": self.steps,
            "notes": [
                "duration_s 是节点方法从调用到返回的墙钟时长，适合排程。",
                "robot_total_duration_s 是机器人节点时长之和，不代表机器人内部纯运动时间。",
                "opc_wait_events 保留 PLC 等待明细；不同等待可能嵌套，不直接相加。",
                "station_dwells 从机器人放料完成到对应取料开始计算，表示物料占用工站的停留时间。",
                "时间轴按实际开始/结束偏移展示；当前本地 runner 顺序执行时不会产生步骤重叠。",
            ],
        }

        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.report_path = self.output_dir / f"{self.run_id}.json"
        self.report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        self._write_csv(self.output_dir / f"{self.run_id}.csv")
        self.summary_path = self.output_dir / f"{self.run_id}.html"
        self._write_html(self.summary_path, report)
        return self.report_path

    def _calculate_station_dwells(self) -> list[dict[str, Any]]:
        pending_places: list[dict[str, Any]] = []
        dwells: list[dict[str, Any]] = []
        for step in self.steps:
            transfer = _station_transfer(step)
            if transfer is None or step.get("status") != "success":
                continue
            operation, station = transfer
            if operation == "place_to":
                pending_places.append({"station": station, "step": step})
                continue
            match_index = next(
                (
                    index
                    for index, entry in enumerate(pending_places)
                    if entry["station"] == station and _transfer_params_match(entry["step"], step)
                ),
                None,
            )
            if match_index is None:
                continue
            place = pending_places.pop(match_index)["step"]
            start = float(place.get("end_offset_s") or 0)
            end = float(step.get("start_offset_s") or start)
            dwells.append(
                {
                    "station": station,
                    "position": place.get("params", {}).get("position"),
                    "product_type": place.get("params", {}).get("product_type"),
                    "sample_id": place.get("params", {}).get("sample_id"),
                    "place_step_index": place["index"],
                    "pick_step_index": step["index"],
                    "started_at": place["finished_at"],
                    "finished_at": step["started_at"],
                    "start_offset_s": _round_seconds(start),
                    "end_offset_s": _round_seconds(end),
                    "duration_s": _round_seconds(max(0.0, end - start)),
                    "status": "completed",
                }
            )
        for entry in pending_places:
            place = entry["step"]
            dwells.append(
                {
                    "station": entry["station"],
                    "position": place.get("params", {}).get("position"),
                    "product_type": place.get("params", {}).get("product_type"),
                    "sample_id": place.get("params", {}).get("sample_id"),
                    "place_step_index": place["index"],
                    "pick_step_index": None,
                    "started_at": place["finished_at"],
                    "finished_at": None,
                    "start_offset_s": place.get("end_offset_s"),
                    "end_offset_s": None,
                    "duration_s": None,
                    "status": "open",
                }
            )
        return dwells

    @staticmethod
    def _summarize_station_dwells(dwells: list[dict[str, Any]]) -> list[dict[str, Any]]:
        summary: dict[str, dict[str, Any]] = {}
        for dwell in dwells:
            if dwell["duration_s"] is None:
                continue
            item = summary.setdefault(
                dwell["station"],
                {"station": dwell["station"], "visit_count": 0, "total_duration_s": 0.0, "max_duration_s": 0.0},
            )
            duration = float(dwell["duration_s"])
            item["visit_count"] += 1
            item["total_duration_s"] += duration
            item["max_duration_s"] = max(item["max_duration_s"], duration)
        for item in summary.values():
            item["total_duration_s"] = _round_seconds(item["total_duration_s"])
            item["max_duration_s"] = _round_seconds(item["max_duration_s"])
            item["average_duration_s"] = _round_seconds(item["total_duration_s"] / item["visit_count"])
        return sorted(summary.values(), key=lambda item: item["station"])

    def _write_html(self, path: Path, report: dict[str, Any]) -> None:
        total = max(float(report["execution_duration_s"]), 0.001)
        lanes: dict[str, list[dict[str, Any]]] = {}
        for step in self.steps:
            lanes.setdefault(str(step["device_name"]), []).append(step)

        lane_rows = []
        for lane, steps in lanes.items():
            bars = []
            for step in steps:
                left = min(100.0, float(step.get("start_offset_s") or 0) / total * 100)
                width = max(0.5, float(step.get("duration_s") or 0) / total * 100)
                label = f'{step["index"]}. {step["method"]} · {step.get("duration_s", 0):.3f}s'
                bars.append(
                    f'<div class="bar {html.escape(str(step["status"]))}" '
                    f'style="left:{left:.4f}%;width:{min(width, 100-left):.4f}%" '
                    f'title="{html.escape(label)}">{html.escape(label)}</div>'
                )
            lane_rows.append(
                f'<div class="lane-label">{html.escape(lane)}</div><div class="lane">{"".join(bars)}</div>'
            )

        step_rows = "".join(
            "<tr>"
            f'<td>{step["index"]}</td><td>{html.escape(str(step["device_name"]))}</td>'
            f'<td>{html.escape(str(step["method"]))}</td><td>{float(step.get("start_offset_s") or 0):.3f}</td>'
            f'<td>{float(step.get("duration_s") or 0):.3f}</td><td>{html.escape(str(step["status"]))}</td>'
            "</tr>"
            for step in self.steps
        )
        dwell_rows = "".join(
            "<tr>"
            f'<td>{html.escape(str(item["station"]))}</td><td>{html.escape(str(item.get("position") or "—"))}</td>'
            f'<td>{item["place_step_index"]}</td><td>{item.get("pick_step_index") or "—"}</td>'
            f'<td>{item.get("duration_s") if item.get("duration_s") is not None else "未取出"}</td>'
            "</tr>"
            for item in report["station_dwells"]
        )
        path.write_text(
            f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{html.escape(self.workflow_name)} 排程计时</title>
<style>
body{{font:14px system-ui;margin:24px;color:#172033;background:#f7f8fa}}main{{max-width:1400px;margin:auto}}
h1{{font-size:24px}}h2{{margin-top:28px}}.metrics{{display:flex;gap:12px;flex-wrap:wrap}}
.metric{{background:white;border:1px solid #d9dee8;padding:12px 16px;min-width:150px}}
.metric strong{{display:block;font-size:22px}}.timeline{{display:grid;grid-template-columns:220px 1fr;gap:8px}}
.lane-label{{padding:10px;background:white;border:1px solid #d9dee8;overflow-wrap:anywhere}}
.lane{{position:relative;min-height:42px;background:white;border:1px solid #d9dee8;
background-image:linear-gradient(to right,#edf0f5 1px,transparent 1px);background-size:10% 100%}}
.bar{{position:absolute;top:6px;height:28px;box-sizing:border-box;padding:5px 7px;overflow:hidden;
white-space:nowrap;background:#2563eb;color:white;font-size:11px}}.bar.failed{{background:#b42318}}
table{{border-collapse:collapse;width:100%;background:white}}th,td{{border:1px solid #d9dee8;padding:8px;text-align:left}}
th{{background:#eef1f6}}.note{{color:#596579}}code{{background:#e9edf3;padding:2px 5px}}
</style></head><body><main>
<h1>{html.escape(self.workflow_name)} 排程计时</h1>
<p class="note">Run <code>{html.escape(self.run_id)}</code> · 横轴为执行开始后的实际时间，所有资源使用同一时间尺度，可直接识别并行与空闲窗口。</p>
<div class="metrics"><div class="metric"><strong>{report["total_duration_s"]:.3f}s</strong>总时长</div>
<div class="metric"><strong>{report["execution_duration_s"]:.3f}s</strong>执行时长</div>
<div class="metric"><strong>{report["robot_total_duration_s"]:.3f}s</strong>机器人累计</div>
<div class="metric"><strong>{len(self.steps)}</strong>步骤数</div></div>
<h2>资源并行时间轴</h2><div class="timeline">{"".join(lane_rows)}</div>
<p class="note">刻度：0–{total:.3f} 秒；当前 runner 若顺序执行，横条不会重叠。</p>
<h2>逐步骤排程数据</h2><table><thead><tr><th>#</th><th>资源</th><th>动作</th><th>开始偏移(s)</th><th>耗时(s)</th><th>状态</th></tr></thead><tbody>{step_rows}</tbody></table>
<h2>物料在工站停留时间</h2><table><thead><tr><th>工站</th><th>位置</th><th>放料步骤</th><th>取料步骤</th><th>停留(s)</th></tr></thead><tbody>{dwell_rows}</tbody></table>
</main></body></html>""",
            encoding="utf-8",
        )

    def _write_csv(self, path: Path) -> None:
        fields = [
            "index",
            "node_id",
            "device_name",
            "resource_type",
            "method",
            "status",
            "started_at",
            "finished_at",
            "duration_s",
            "start_offset_s",
            "end_offset_s",
            "opc_wait_event_count",
            "error",
        ]
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for step in self.steps:
                writer.writerow(
                    {
                        **{field: step.get(field) for field in fields},
                        "opc_wait_event_count": len(step["opc_wait_events"]),
                    }
                )
