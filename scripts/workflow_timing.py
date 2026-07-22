"""临时 workflow 排程计时记录器。"""

from __future__ import annotations

import csv
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _round_seconds(value: float) -> float:
    return round(value, 3)


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
        duration = time.monotonic() - self._active_step_started_monotonic
        self._active_step.update(
            {
                "status": "failed" if error else "success",
                "finished_at": _now_iso(),
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
        report = {
            "schema": "unilabos.workflow_timing.v1",
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
            "error": error,
            "steps": self.steps,
            "notes": [
                "duration_s 是节点方法从调用到返回的墙钟时长，适合排程。",
                "robot_total_duration_s 是机器人节点时长之和，不代表机器人内部纯运动时间。",
                "opc_wait_events 保留 PLC 等待明细；不同等待可能嵌套，不直接相加。",
            ],
        }

        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.report_path = self.output_dir / f"{self.run_id}.json"
        self.report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        self._write_csv(self.output_dir / f"{self.run_id}.csv")
        return self.report_path

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
