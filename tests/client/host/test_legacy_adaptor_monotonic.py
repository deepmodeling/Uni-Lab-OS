"""旧 Backend 适配层只在本进程计时的时长：墙钟跳变不影响。

覆盖 running 状态防抖、job 幂等缓存的保留期，以及注册表 / 物料全量上报的耗时日志。
只改写墙钟（``time.time``），单调时钟照常走。
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

from unilabos.client.host.legacy_adaptor.legacy import sync as sync_module
from unilabos.client.host.legacy_adaptor.legacy.sync import LegacyMaterialMirror, upload_registry_snapshot
from unilabos.client.host.legacy_adaptor.legacy.ws import LegacyBackendWebSocketClient

_ITEM = SimpleNamespace(job_id="job-1", task_id="task-1", device_id="pump", notebook_id="", action_name="transfer")


def _jump_wall_clock(monkeypatch, offset_s: float) -> None:
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + offset_s)


def _client() -> LegacyBackendWebSocketClient:
    client = LegacyBackendWebSocketClient(
        "wss://legacy.test/api/v1/ws/schedule",
        execution_backend_getter=lambda: None,
        adapter_getter=lambda: None,
    )
    client._connected = True
    return client


def _drain(client: LegacyBackendWebSocketClient) -> list[dict]:
    out = []
    while not client._send_queue.empty():
        out.append(client._send_queue.get_nowait())
    return out


class _Logger:
    def __init__(self) -> None:
        self.infos: list[tuple[str, tuple[Any, ...]]] = []

    def info(self, message: str, *args: Any) -> None:
        self.infos.append((message, args))

    def warning(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    error = debug = warning


def test_running_status_debounce_ignores_forward_wall_clock_jump(monkeypatch) -> None:
    client = _client()
    client.publish_job_status({"progress": 1}, _ITEM, "running")

    _jump_wall_clock(monkeypatch, 13.0)
    client.publish_job_status({"progress": 1}, _ITEM, "running")

    assert [message["data"]["status"] for message in _drain(client)] == ["running"]


def test_job_result_cache_survives_forward_wall_clock_jump(monkeypatch) -> None:
    client = _client()
    client.publish_job_status({}, _ITEM, "success", {"suc": True})

    # 墙钟前跳超过 24 h 的缓存保留期：重复 job_start 仍要回放缓存的终态
    _jump_wall_clock(monkeypatch, 25 * 3600.0)
    with client._lock:
        client._prune_jobs_locked()

    assert client._jobs[("task-1", "job-1")].terminal_status == "success"


def test_registry_upload_duration_ignores_backward_wall_clock_step(monkeypatch) -> None:
    log = _Logger()
    monkeypatch.setattr(sync_module, "logger", log)
    registry = SimpleNamespace(
        obtain_registry_device_info=lambda: [],
        obtain_registry_resource_info=lambda: [{"id": "tube"}],
    )

    class _Http:
        def upload_registry(self, entries: list) -> dict:
            _jump_wall_clock(monkeypatch, -30.0)
            return {"skipped": False}

    assert upload_registry_snapshot(registry, _Http()) is not None

    durations = [args[-1] for message, args in log.infos if message.startswith("[LegacyRegistry]")]
    assert len(durations) == 1
    assert 0 <= durations[0] < 5


def test_material_full_upload_duration_ignores_backward_wall_clock_step(monkeypatch) -> None:
    log = _Logger()
    monkeypatch.setattr(sync_module, "logger", log)

    class _Http:
        def upload_material_tree(self, nodes: list, *, mount_uuid: str, first_add: bool) -> dict:
            _jump_wall_clock(monkeypatch, -30.0)
            return {}

    mirror = LegacyMaterialMirror(client=_Http(), gateway=SimpleNamespace(changes=lambda **_kwargs: []))
    monkeypatch.setattr(mirror, "_dump_root", lambda root_uuid: [{"id": root_uuid}])

    mirror.upload_full(["root-1"])

    durations = [args[2] for message, args in log.infos if "全量物料上报" in message]
    assert len(durations) == 1
    assert 0 <= durations[0] < 5
