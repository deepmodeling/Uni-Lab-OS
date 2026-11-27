"""实时录制：OS 接受的观测样本（回放来源除外）同步写进一个 MCAP 文件。

文件格式与 ``mcap_record`` 相同（每节点一个 ``/observations/<id>`` 通道），停止后可直接作为
回放源，或与数据库生成的作业区间、物料状态合并播放。一次只录一个文件；由接口或任务钩子
开始、停止。录制失败只记日志，不影响观测写入。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from unilabos.simulation.mcap_record import METADATA_NAME, FrameChannels, open_writer

logger = logging.getLogger(__name__)


class McapRecorder:
    def __init__(self, path: Path, *, task_uuid: str | None = None):
        self.path = Path(path)
        self.task_uuid = task_uuid
        self.messages = 0
        self.started_at = time.time()
        self._lock = threading.Lock()
        self._sequence = 0
        self.nodes: set[str] = set()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._stream = self.path.open("wb")
        self._writer, schemas = open_writer(self._stream)
        self._frames = FrameChannels(self._writer, schemas)

    def record_frame(self, sampled_at: float, event: str | None, values: dict[str, dict[str, Any]],
                     descriptors: dict[str, dict[str, Any] | None]) -> None:
        """一个采样时刻多个节点的值：数值进二进制帧，其余进状态通道。"""
        stamp = int(sampled_at * 1e9)
        with self._lock:
            if self._writer is None:
                return
            for node, properties in values.items():
                descriptor = descriptors.get(node)
                names = sorted(set((descriptor or {}).get("properties") or {}) | set(properties))
                self._frames.ensure(node, descriptor, names, properties)
                self.nodes.add(node)
            self.messages += self._frames.write(stamp, self._sequence, event or "", values)
            self._sequence += 1

    def record(self, node_id: str, descriptor: dict[str, Any] | None, sampled_at: float, event: str | None,
               values: dict[str, Any]) -> None:
        self.record_frame(sampled_at, event, {node_id: values}, {node_id: descriptor})

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if self._writer is not None:
                self._writer.add_metadata(METADATA_NAME, {"header": json.dumps({
                    "schema": "unilab.replay-bundle/1", "select_by": "id",
                    "source": {"kind": "os-observation-recording", "workflow_task_uuid": self.task_uuid,
                               "started_at": self.started_at, "stopped_at": time.time()}}, ensure_ascii=False)})
                self._writer.finish()
                self._stream.close()
                self._writer = None
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        return {"path": str(self.path), "sha256": digest, "messages": self.messages,
                "nodes": sorted(self.nodes), "task_uuid": self.task_uuid}

    @property
    def active(self) -> bool:
        return self._writer is not None


_current: McapRecorder | None = None
_guard = threading.Lock()


def current() -> McapRecorder | None:
    return _current if _current is not None and _current.active else None


def start(path: Path, *, task_uuid: str | None = None) -> McapRecorder:
    global _current
    with _guard:
        if current() is not None:
            raise RuntimeError("已有录制在进行，先停止")
        _current = McapRecorder(path, task_uuid=task_uuid)
        logger.info("[McapRecorder] recording to %s (task=%s)", path, task_uuid)
        return _current


def stop() -> dict[str, Any] | None:
    global _current
    with _guard:
        recorder, _current = _current, None
    if recorder is None:
        return None
    result = recorder.stop()
    logger.info("[McapRecorder] stopped: %s", result)
    return result


def tap(samples: list, nodes: dict[str, Any], descriptor_of) -> None:
    """``DeviceObservationService`` 写入成功后调用：按节点和采样时刻分组写入当前录制。"""
    recorder = current()
    if recorder is None:
        return
    try:
        frames: dict[tuple[float, str | None], dict[str, dict[str, Any]]] = {}
        descriptors: dict[str, dict[str, Any] | None] = {}
        for sample in samples:
            if sample.provider == "replay":
                continue
            node = nodes.get(sample.material_uuid)
            if node is None or not (node.get("id") or node.get("name")):
                continue
            key = node.get("id") or node["name"]
            if key not in descriptors:
                descriptor = descriptor_of(node)
                descriptors[key] = descriptor.model_dump() if descriptor is not None else None
            frames.setdefault((sample.sampled_at, sample.sample_group), {}).setdefault(key, {})[sample.property] = sample.value
        for (sampled_at, event), values in sorted(frames.items(), key=lambda item: item[0][0]):
            recorder.record_frame(sampled_at, event, values, descriptors)
    except Exception:  # noqa: BLE001 - 录制失败不影响观测写入
        logger.warning("[McapRecorder] write failed", exc_info=True)


class TaskRecordingWatcher:
    """按任务自动录制：有任务进入 running 时开一个 ``<目录>/<task_uuid>.mcap``，任务都结束后停止，
    并写旁路说明 ``<task_uuid>.json``（路径、sha256、消息数、期间运行过的任务）。

    不改工作流库：录制文件按任务 uuid 命名，回放时按 uuid 找到文件即可。并行任务共用一个文件，
    说明里列出全部任务。``list_running`` 返回当前 running 的任务 uuid 列表。
    """

    def __init__(self, list_running, directory: Path, *, interval_s: float = 1.0, grace_s: float = 3.0,
                 clock=time.monotonic):
        self.list_running = list_running
        # 任务结束后再录 grace_s 秒：设备的运动流通常比任务状态晚到一点。
        self.grace_s = grace_s
        self.clock = clock
        self._idle_since: float | None = None
        self.directory = Path(directory)
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._owner: str | None = None
        self._seen: set[str] = set()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="mcap-task-recording", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._finish()

    def poll(self) -> None:
        running = set(self.list_running())
        if running and self._owner is None and current() is None:
            self._owner = sorted(running)[0]
            self._seen = set(running)
            start(self.directory / f"{self._owner}.mcap", task_uuid=self._owner)
        elif self._owner is not None:
            self._seen |= running
            if running:
                self._idle_since = None
            elif self._idle_since is None:
                self._idle_since = self.clock()
            elif self.clock() - self._idle_since >= self.grace_s:
                self._finish()

    def _finish(self) -> None:
        if self._owner is None:
            return
        result = stop()
        if result is not None:
            result["tasks"] = sorted(self._seen)
            (self.directory / f"{self._owner}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                                                encoding="utf-8")
        self._owner, self._seen, self._idle_since = None, set(), None

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self.poll()
            except Exception:  # noqa: BLE001 - 录制失败不影响任务
                logger.warning("[McapRecorder] task watcher poll failed", exc_info=True)


_watcher: TaskRecordingWatcher | None = None


def start_task_watcher_from_environment() -> TaskRecordingWatcher | None:
    """``ULAB_RECORD_TASKS=true`` 时按任务自动录制到 ``ULAB_RECORDING_DIR``（默认 ~/.unilabos/recordings）。"""
    import os

    global _watcher
    if os.environ.get("ULAB_RECORD_TASKS", "").strip().lower() != "true":
        return None
    from unilabos.workflow.composition import get_workflow_service

    def list_running() -> list[str]:
        service = get_workflow_service()
        if service is None:
            return []
        page = service.list_workflow_tasks(status="running", page_size=100, projection="summary")
        return [str(item["uuid"]) for item in page.get("items", [])]

    directory = Path(os.environ.get("ULAB_RECORDING_DIR") or Path.home() / ".unilabos" / "recordings").expanduser()
    _watcher = TaskRecordingWatcher(list_running, directory)
    _watcher.start()
    logger.info("[McapRecorder] recording workflow tasks to %s", directory)
    return _watcher


def stop_task_watcher() -> None:
    global _watcher
    if _watcher is not None:
        _watcher.stop()
        _watcher = None
