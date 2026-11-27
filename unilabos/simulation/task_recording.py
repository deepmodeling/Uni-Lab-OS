"""一次工作流任务的录制：只用现有数据拼回放记录，不新增表。

数据来源（都是 OS 现有接口，背后是现有表）：

* 动作周期：``GET /workflow-tasks/{task}/jobs`` → ``workflow_node_job``（作业、执行设备
  ``material_uuid``、``started_at`` / ``finished_at``）。
* 设备轨迹：``GET /device-state/{device}/history?property=…`` → ``device_property_history``
  （按设备 id、属性、毫秒时间存的变化点）。每个设备取它所有作业的时间窗内的点，并补上
  窗口开始前的最后一个值作为初值。
* 物料状态：``GET /workflow-tasks/{task}/material-events`` → ``inventory_ledger``
  的 ``material.transfer``（搬运前后的所属物料与库位、所属作业）。

输出是 ``unilab.replay-bundle/1``（``replay_feed`` 可直接播放）：设备属性按原名回放，
物料写 ``parent`` / ``site``；表头 ``commands`` 是每个作业的时间区间，id 为作业 uuid，
label 为节点名或动作名。位姿不存：前端由库位、连杆锚点和关节值算出。

注意：属性表只在值变化时写一行，且默认每个属性只保留最近
``ULAB_DEVICE_PROPERTY_HISTORY_PER_KEY`` 条（默认 1000）；录制用的部署应调大或设为 0（不裁）。

    python -m unilabos.simulation.task_recording --url http://127.0.0.1:PORT/api/v1 --task <uuid> --out run.jsonl
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote, urlencode
from urllib.request import urlopen

TTL = 3600.0
MATERIAL_PROPERTIES = {
    "parent": {"semantic": "observed", "dtype": "string", "unit": "-", "frame": "material", "ttl_seconds": TTL},
    "site": {"semantic": "observed", "dtype": "string", "unit": "-", "frame": "material", "ttl_seconds": TTL},
}


def to_ms(value: Any) -> int | None:
    """ISO 时间串或数字（秒/毫秒）→ 毫秒。"""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return int(value if value > 1e11 else value * 1000)
    text = str(value).strip().replace("Z", "+00:00")
    return int(datetime.fromisoformat(text).timestamp() * 1000)


def build_bundle(
    jobs: Iterable[dict[str, Any]],
    material_events: Iterable[dict[str, Any]],
    *,
    node_id: Callable[[str], str | None],
    properties_of: Callable[[str], list[str]],
    history: Callable[[str, str, int], list[dict[str, Any]]],
    task_uuid: str = "",
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """``history(device, property, since_ms)`` 返回该属性自 since_ms 起的变化点（任意顺序，
    含 ``value`` 与 ``recorded_at`` 毫秒）；为补初值，调用方应能返回 since_ms 之前的最后一个点。"""
    windows: dict[str, list[tuple[int, int]]] = {}
    commands: list[dict[str, Any]] = []
    for job in jobs:
        start, end = to_ms(job.get("started_at")), to_ms(job.get("finished_at"))
        control = job.get("control_data") or {}
        executor = control.get("actual_executor") or {}
        payload = control.get("dispatch_payload") or {}
        device = node_id(str(job.get("material_uuid") or "")) or executor.get("local_device_id")
        action = payload.get("action")
        if start is None:
            continue
        end = end if end is not None else start
        commands.append({"id": str(job["uuid"]), "t_start_ms": start, "t_end_ms": end,
                         "label": " · ".join(part for part in (device, action) if part)
                         or str((job.get("meta_data") or {}).get("node_name") or job.get("workflow_node_uuid") or ""),
                         **({"device": device} if device else {})})
        if device:
            windows.setdefault(device, []).append((start, end))
    if not commands:
        raise ValueError("任务没有已开始的作业，无法确定录制时间范围")
    origin = min(item["t_start_ms"] for item in commands)

    samples: list[tuple[int, str, str, Any, str]] = []  # (ms, node, property, value, event)
    devices: dict[str, list[str]] = {}
    for device, spans in windows.items():
        first = min(start for start, _ in spans)
        for name in properties_of(device):
            points = sorted(history(device, name, 0), key=lambda point: int(point["recorded_at"]))
            before = [point for point in points if int(point["recorded_at"]) < first]
            inside = [point for point in points
                      if any(start <= int(point["recorded_at"]) <= end for start, end in spans)]
            picked = ([before[-1]] if before else []) + inside
            if not picked:
                continue
            devices.setdefault(device, []).append(name)
            for point in picked:
                samples.append((max(int(point["recorded_at"]), origin), device, name, point["value"], ""))

    descriptors: dict[str, dict[str, Any]] = {}
    for event in material_events:
        if event.get("kind") != "moved":
            continue
        node = node_id(str(event.get("material_uuid") or ""))
        when = to_ms(event.get("occurred_at"))
        if node is None or when is None:
            continue
        owner = event.get("to_owner_uuid")
        values = {"parent": node_id(str(owner)) if owner else None, "site": event.get("to_site")}
        for name, value in values.items():
            samples.append((max(when, origin), node, name, value, str(event.get("workflow_node_job_uuid") or "")))
        names = devices.setdefault(node, [])
        names.extend(name for name in MATERIAL_PROPERTIES if name not in names)
        descriptors[node] = {"version": "task-recording-1", "properties": dict(MATERIAL_PROPERTIES)}

    def event_at(ms: int) -> str:
        active = [item["id"] for item in commands if item["t_start_ms"] <= ms <= item["t_end_ms"]]
        return "+".join(active) if active else "idle"

    rows: list[dict[str, Any]] = []
    for ms, node, name, value, _ in sorted(samples, key=lambda item: item[0]):
        t = round((ms - origin) / 1000.0, 3)
        event = event_at(ms)
        if not rows or rows[-1]["t"] != t or rows[-1]["event"] != event:
            rows.append({"t": t, "event": event, "values": {}})
        rows[-1]["values"].setdefault(node, {})[name] = value

    header = {
        "schema": "unilab.replay-bundle/1", "select_by": "id", "devices": devices, "descriptors": descriptors,
        "commands": [{"id": item["id"], "t_start": round((item["t_start_ms"] - origin) / 1000.0, 3),
                      "t_end": round((item["t_end_ms"] - origin) / 1000.0, 3),
                      **({"label": item["label"]} if item["label"] else {})}
                     for item in sorted(commands, key=lambda item: item["t_start_ms"])],
        "source": {"kind": "os-task-recording", "workflow_task_uuid": task_uuid,
                   "tables": ["workflow_node_job", "device_property_history", "inventory_ledger"]},
        "duration_s": rows[-1]["t"] if rows else 0.0,
        "origin_ms": origin,
    }
    return header, rows


class OsClient:
    """读现有 HTTP 接口；只读。"""

    def __init__(self, url: str, timeout: float = 30.0):
        self.url = url.rstrip("/")
        self.timeout = timeout

    def get(self, path: str, **query) -> Any:
        target = self.url + path + (("?" + urlencode(query)) if query else "")
        with urlopen(target, timeout=self.timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        return body.get("data", body) if isinstance(body, dict) else body

    def node_ids(self) -> dict[str, str]:
        snapshot = self.get("/device-state/observations/snapshot")
        return {node["uuid"]: node["id"] for node in snapshot.get("nodes", []) if node.get("id")}

    def properties_of(self, device: str) -> list[str]:
        try:
            return sorted(self.get(f"/device-state/{quote(device, safe='')}").get("properties", {}))
        except Exception:
            return []

    def history(self, device: str, name: str, since_ms: int) -> list[dict[str, Any]]:
        body = self.get(f"/device-state/{quote(device, safe='')}/history", property=name, since_ms=since_ms, limit=5000)
        return body.get("entries", [])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", required=True, help="OS 接口根，如 http://127.0.0.1:PORT/api/v1")
    parser.add_argument("--task", required=True, help="workflow task uuid")
    parser.add_argument("--out", type=Path, required=True)
    arguments = parser.parse_args(argv)
    client = OsClient(arguments.url)
    ids = client.node_ids()
    header, rows = build_bundle(
        client.get(f"/workflow-tasks/{arguments.task}/jobs"),
        client.get(f"/workflow-tasks/{arguments.task}/material-events"),
        node_id=ids.get, properties_of=client.properties_of, history=client.history, task_uuid=arguments.task,
    )
    arguments.out.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in [header, *rows]) + "\n",
                             encoding="utf-8")
    print(f"{len(rows)} rows, {len(header['commands'])} jobs, {len(header['devices'])} nodes -> {arguments.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


def recordings_dir() -> Path:
    import os

    return Path(os.environ.get("ULAB_RECORDING_DIR") or Path.home() / ".unilabos" / "recordings").expanduser()


def list_recordings(directory: Path | None = None) -> list[dict[str, Any]]:
    """录制目录里的任务录制（``<task>.mcap`` 及其说明 ``<task>.json``），新的在前。"""
    directory = directory or recordings_dir()
    items = []
    for path in sorted(directory.glob("*.mcap"), key=lambda item: item.stat().st_mtime, reverse=True):
        side = path.with_suffix(".json")
        info = json.loads(side.read_text(encoding="utf-8")) if side.exists() else {}
        items.append({"task_uuid": path.stem, "file": path.name, "bytes": path.stat().st_size,
                      "recorded_at": path.stat().st_mtime, "messages": info.get("messages"),
                      "nodes": info.get("nodes"), "tasks": info.get("tasks")})
    return items


def build_state_in_process(task_uuids: list[str], observations, device_state, workflow_service) -> tuple[dict, list]:
    """后端进程内生成这些任务的低频状态（作业区间、物料搬运、属性变化点），不走 HTTP。"""
    ids = {node["uuid"]: node.get("id") for node in observations.snapshot()["nodes"] if node.get("id")}

    def properties_of(device: str) -> list[str]:
        return sorted(device_state.latest_for(device)) if device_state is not None else []

    def history(device: str, name: str, since_ms: int) -> list[dict[str, Any]]:
        return device_state.history(device, name, since_ms, 5000) if device_state is not None else []

    jobs, events = [], []
    for task in task_uuids:
        jobs.extend(workflow_service.list_workflow_node_jobs(task))
        events.extend(workflow_service.list_task_material_events(task))
    return build_bundle(jobs, events, node_id=ids.get, properties_of=properties_of, history=history,
                        task_uuid=",".join(task_uuids))


def find_recording(task_uuid: str, directory: Path | None = None) -> tuple[Path | None, list[str]]:
    """含该任务的录制文件与其中全部任务（连续运行的任务会录进同一个文件）。"""
    directory = directory or recordings_dir()
    own = directory / f"{task_uuid}.mcap"
    if own.exists():
        side = own.with_suffix(".json")
        tasks = json.loads(side.read_text(encoding="utf-8")).get("tasks") if side.exists() else None
        return own, tasks or [task_uuid]
    for side in directory.glob("*.json"):
        if side.name.endswith(".state.json"):
            continue
        info = json.loads(side.read_text(encoding="utf-8"))
        if task_uuid in (info.get("tasks") or []):
            return Path(info["path"]), info["tasks"]
    return None, [task_uuid]


def prepare_task_replay(task_uuid: str, observations, device_state, workflow_service,
                        directory: Path | None = None) -> str:
    """返回可交给 ``replay_feed.load`` 的回放源：``<task>.mcap`` 加由数据库生成的 ``<task>.state.jsonl``。"""
    import os

    directory = directory or recordings_dir()
    mcap, tasks = find_recording(task_uuid, directory)
    header, rows = build_state_in_process(tasks, observations, device_state, workflow_service)
    state = directory / f"{(mcap.stem if mcap else task_uuid)}.state.jsonl"
    state.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in [header, *rows]) + "\n",
                     encoding="utf-8")
    return os.pathsep.join(str(path) for path in ([mcap] if mcap is not None else []) + [state])
