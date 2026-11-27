"""只读显示消费与隔离 Blender 进程；模型转换由资产所有者完成。"""

from __future__ import annotations

import hashlib
import json
import queue
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Literal
from urllib.request import urlopen

from pydantic import Field, model_validator

from unilabos.runtime.observations import Contract, Descriptor, StateError


class Channel(Contract):
    """相对于显式资产根的对象绑定；轴/矩阵分量由设备包声明。"""

    object: str = Field(min_length=1)
    kind: Literal["translation", "world_translation", "rotation", "world_matrix"]
    properties: list[str] = Field(min_length=1)
    axis: tuple[float, float, float] = (1, 0, 0)
    offset: float = 0

    @model_validator(mode="after")
    def shape(self):
        if len(self.properties) not in ((1, 16) if self.kind == "world_matrix" else (1,)):
            raise ValueError("矩阵需要16个具名分量，其余绑定需要一个分量")
        if self.kind != "world_matrix" and abs(sum(x * x for x in self.axis) - 1) > 1e-6:
            raise ValueError("绑定轴必须为单位向量")
        return self


class DisplayAsset(Contract):
    """消费已发布的 blend 子树，外部文件逐项核验，不运行资产修补。"""

    version: str = Field(min_length=1)
    file: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    root: str
    dependencies: dict[str, str] = Field(default_factory=dict)
    channels: list[Channel] = Field(default_factory=list)
    hidden: list[str] = Field(default_factory=list)
    probes: dict[str, str] = Field(default_factory=dict)
    frame_camera: bool = True

    @model_validator(mode="after")
    def unique_writers(self):
        for i, first in enumerate(self.channels):
            for second in self.channels[i + 1 :]:
                if first.object != second.object:
                    continue
                if first.kind != second.kind or first.kind not in ("translation", "world_translation"):
                    raise ValueError("同一对象的变换写入冲突")
                if abs(sum(a * b for a, b in zip(first.axis, second.axis))) > 1e-6:
                    raise ValueError("同一对象的平移绑定轴必须正交")
        return self

    def validate_files(self, directory: Path) -> dict[str, Any]:
        base = directory.resolve()
        paths = {self.file: self.sha256, **self.dependencies}
        for name, expected in paths.items():
            path = (base / name).resolve(strict=True)
            if not path.is_relative_to(base):
                raise StateError("asset_path_outside_root")
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != expected:
                raise StateError("asset_digest_mismatch", name)
        return {
            **self.model_dump(),
            "file": str((base / self.file).resolve()),
            "dependency_files": [str((base / name).resolve()) for name in self.dependencies],
        }


class SceneMirror:
    """从一致 OS 快照生成显示帧；坏质量保留诊断，不归零、不使用目标代替观测。"""

    def __init__(self, assets: dict[str, DisplayAsset], directory: Path, *, instances: set[str] | None = None):
        self.assets = {key: asset.validate_files(directory) for key, asset in assets.items()}
        self.revision = 0
        self.instances = instances

    def frame(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        entities = []
        for node in snapshot["nodes"]:
            if self.instances is not None and node["uuid"] not in self.instances:
                continue
            asset = self.assets.get(node["resource_template_uuid"])
            if asset is None:
                continue
            if node["descriptor"] is None and asset["channels"]:
                raise StateError("descriptor_missing")
            descriptor = Descriptor.model_validate(node["descriptor"]) if node["descriptor"] else None
            channels = []
            for channel in asset["channels"]:
                names = channel["properties"]
                if any(name not in descriptor.properties for name in names):
                    raise StateError("property_missing")
                if any(descriptor.properties[name].semantic != "observed" for name in names):
                    raise StateError("display_requires_observation")
                values = [node["properties"][name] for name in names]
                valid = all(value["quality"] == "good" and value["value"] is not None for value in values)
                if valid and len(values) > 1:
                    samples = [value["sample"] for value in values]
                    # 多分量矩阵须来自同一有证据的采样组，不拼接不同时刻的坐标。
                    valid = (
                        bool(samples[0].get("sample_group"))
                        and len(
                            {
                                (s["sample_group"], s["sampled_at"], s["clock"], s["source_session"], s["epoch"])
                                for s in samples
                            }
                        )
                        == 1
                    )
                output = [value["value"] for value in values]
                if channel["kind"] == "world_matrix" and len(values) == 1:
                    if descriptor.properties[names[0]].dtype != "matrix4":
                        raise StateError("version_incompatible")
                    output = output[0] if output[0] is not None else []
                channels.append(
                    {**channel, "apply": valid, "values": output, "qualities": [value["quality"] for value in values]}
                )
            entities.append(
                {
                    "uuid": node["uuid"],
                    "generation": node["generation"],
                    "parent_uuid": node["parent_uuid"],
                    "asset": asset,
                    "channels": channels,
                }
            )
        self.revision += 1
        return {
            "revision": self.revision,
            "runtime_session": snapshot["runtime_session"],
            "graph_version": snapshot["graph_version"],
            "cursor": snapshot["cursor"],
            "entities": entities,
        }


class StateClient:
    """标准只读 HTTP 入口；每次读取一致基线也支持 worker 离线期间发生的结构变化。"""

    def __init__(self, url: str, timeout: float = 10):
        self.url, self.timeout = url.rstrip("/"), timeout

    def snapshot(self):
        with urlopen(self.url + "/snapshot", timeout=self.timeout) as response:
            body = json.load(response)
        if body.get("code") != 0:
            raise StateError(body.get("error", {}).get("reason", "state_read_failed"))
        return body["data"]


class BlenderWorker:
    """每次调用等待实际应用读回；渲染是独立命令，不在属性流上自动执行。"""

    def __init__(self, executable: str, log: Path, timeout: float = 120):
        self.timeout = timeout
        self._lock = threading.Lock()
        self._responses = queue.Queue()
        self._log = log.open("w")
        self.process = subprocess.Popen(
            [
                executable,
                "--background",
                "--factory-startup",
                "--python",
                str(Path(__file__).with_name("blender_worker.py")),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        self._next_id = 0
        self._closed = False

    def _read(self):
        for line in self.process.stdout:
            if line.startswith("UNILAB_TWIN="):
                self._responses.put(json.loads(line.removeprefix("UNILAB_TWIN=")))
            else:
                self._log.write(line)
                self._log.flush()
        self._responses.put({"ok": False, "error": "worker_disconnected"})

    def call(self, operation: str, **payload):
        with self._lock:
            if self._closed:
                raise StateError("worker_disconnected")
            self._next_id += 1
            started = time.monotonic()
            try:
                self.process.stdin.write(
                    json.dumps({"id": self._next_id, "operation": operation, **payload}, allow_nan=False) + "\n"
                )
                self.process.stdin.flush()
                response = self._responses.get(timeout=self.timeout)
            except (BrokenPipeError, queue.Empty) as error:
                self.close()
                raise StateError("worker_result_unknown") from error
            if response.get("id") != self._next_id:
                self.close()
                raise StateError("worker_result_unknown")
            if not response["ok"]:
                self.close()
                raise StateError("worker_apply_failed", response["error"])
            return {**response["result"], "roundtrip_seconds": time.monotonic() - started}

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self._reader.join(timeout=5)
        self.process.stdout.close()
        self._log.close()
