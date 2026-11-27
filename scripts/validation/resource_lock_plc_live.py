"""连接已部署 PLC Sim，验证持久调度、连续占用和实际完成事件。"""

from __future__ import annotations

import argparse
import json
import time
from contextlib import ExitStack
from pathlib import Path
from tempfile import mkdtemp
from typing import Any

from opcua import Client, ua

from tests.scheduler_core.conftest import build_core_runtime, stable_uuid
from tests.scheduler_core.test_resource_occupancy_intervals import _continuous_task
from tests.scheduler_core.test_shared_scope_runtime import submit_shared_scope
from unilabos.app.scheduler.dispatch import CallbackDispatcher
from unilabos.utils.tracing import TracingSettings, initialize_tracing, shutdown_tracing


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--otel-endpoint", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    output.write_text(json.dumps({"status": "running"}), encoding="utf-8")
    try:
        result = _run_validation(args.endpoint, args.otel_endpoint)
    except BaseException as error:
        output.write_text(
            json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        raise
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


def _flush_tracing() -> None:
    if not shutdown_tracing(5000):
        raise RuntimeError("OTel flush 失败")


def _run_validation(endpoint: str, otel_endpoint: str) -> dict[str, Any]:
    service = "unilab-resource-lock-plc-validation"
    if not initialize_tracing(
        TracingSettings(
            enabled=True,
            service_name=service,
            endpoint=otel_endpoint,
            logs_enabled=False,
            schedule_delay_ms=100,
        )
    ):
        raise RuntimeError("OTel 初始化失败")
    with ExitStack() as cleanup:
        cleanup.callback(_flush_tracing)
        client = Client(endpoint, timeout=5)
        client.connect()
        cleanup.callback(client.disconnect)
        prefix = "ns=4;s=上位机通讯|"

        def read(name: str) -> Any:
            return client.get_node(prefix + name).get_value()

        def write(name: str, value: Any) -> None:
            node = client.get_node(prefix + name)
            node.set_value(ua.DataValue(ua.Variant(value, node.get_data_type_as_variant_type())))

        def wait(name: str, expected: Any, timeout: float = 20) -> Any:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                actual = read(name)
                if actual == expected:
                    return actual
                time.sleep(0.05)
            raise AssertionError(f"PLC {name} 未返回 {expected}，最后值 {actual}")

        root = Path(mkdtemp(prefix="unilab-resource-lock-plc-"))
        runtime = build_core_runtime(root)
        cleanup.callback(runtime.close)
        events: list[dict[str, Any]] = []
        pending: list[str] = []

        channels: dict[str, tuple[str, str]] = {}

        def dispatch(payload: dict[str, Any]) -> None:
            prefix = "S07" if payload["device_id"] == "reactor-b" else "S06"
            done = prefix + ("工艺完成" if prefix == "S07" else "加工完成")
            assert read(prefix + "允许加工") is True
            assert not read(done)
            write(prefix + "工艺选择", 1)
            write(prefix + "参数写入完成", True)
            events.append(
                {
                    "event": "dispatch",
                    "job_id": payload["job_id"],
                    "channel": prefix,
                    "at": time.time(),
                }
            )
            channels[payload["job_id"]] = (prefix, done)
            pending.append(payload["job_id"])

        def complete(job_id: str) -> None:
            prefix, done = channels[job_id]
            wait(done, True)
            events.append(
                {
                    "event": "plc_completed",
                    "job_id": job_id,
                    "channel": prefix,
                    "at": time.time(),
                    "completion_evidence": True,
                }
            )
            write(prefix + "参数写入完成", False)
            write(prefix + "工艺选择", 0)
            wait(done, False)
            wait(prefix + "允许加工", True)
            runtime.scheduler.on_job_finished(job_id, True, {"plc_completion_evidence": True})

        runtime.scheduler._dispatcher = CallbackDispatcher(dispatch)
        assert read("S06准备信号") is True
        owner, jobs = _continuous_task(runtime, task_name="plc-owner", device_id="reactor-a")
        waiter = runtime.submit(task_name="plc-waiter", devices=["reactor-a"], priority="high")
        expected = [*jobs, stable_uuid("job:plc-waiter:0")]
        assert pending == [expected[0]], "其他任务在设备未完成时插入"
        for index, job_id in enumerate(expected):
            assert pending[index] == job_id
            complete(job_id)
            assert pending == expected[: min(index + 2, 3)], "连续区间被高优先级任务抢占"
        shared_owner, shared_jobs = submit_shared_scope(runtime)
        assert set(pending[3:]) == set(shared_jobs), "共同范围把不同 PLC 工站错误串行化"
        shared_waiter = runtime.submit(
            task_name="scope-waiter", devices=["warehouse-a"], priority="high"
        )
        assert len(pending) == 5
        complete(shared_jobs[1])
        assert len(pending) == 5, "首个分支完成就释放根范围"
        complete(shared_jobs[0])
        final_job = stable_uuid("job:scope-waiter:0")
        assert pending[-1] == final_job
        complete(final_job)
        statuses = {
            task["task"]["uuid"]: runtime.workflow_store.get_task(task["task"]["uuid"])["status"]
            for task in [owner, waiter, shared_owner, shared_waiter]
        }
        assert set(statuses.values()) == {"succeeded"}
        active = runtime.inventory_store.query_all(
            "SELECT claim_uuid FROM station_execution_claim WHERE state IN ('prepared','reserved','running','uncertain')"
        )
        assert active == [], "完成后仍有活动资源 Claim"
        result = {
            "status": "passed",
            "service_name": service,
            "endpoint": endpoint,
            "database_directory": str(root),
            "expected_order": expected,
            "events": events,
            "tasks": statuses,
            "active_claims_after_completion": len(active),
        }
        return result


if __name__ == "__main__":
    main()
