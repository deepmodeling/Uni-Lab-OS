"""用两个冻结工作流验证资源范围互斥与 PLC 工站并行。"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from opcua import Client, ua

from tests.scheduler_core.conftest import build_core_runtime, stable_uuid
from unilabos.app.scheduler.dispatch import CallbackDispatcher
from unilabos.workflow.resource_lock_plan import (
    bind_station_resource_plan,
    compile_template_resource_plan,
    serialize_resource_plan,
)


def frozen_workflow(runtime: Any, source: dict[str, Any], region: str, name: str) -> dict[str, Any]:
    """编译作者图并绑定本次独立库存的范围身份。"""
    graph = source["graph"]
    bindings = {
        alias: {"canonical_key": f"/devices/{identity}", "kind": "device"}
        for alias, identity in runtime.device_materials.items()
    }
    bindings["region"] = bindings[region]
    plan = serialize_resource_plan(bind_station_resource_plan(compile_template_resource_plan(graph), bindings))
    nodes, jobs = [], []
    for i, item in enumerate(graph["nodes"]):
        node_id, device = item["uuid"], source["device_id"]
        nodes.append({
            "uuid": node_id, "kind": "device_action", "device_id": device,
            "material_uuid": runtime.device_materials[device],
            "action_name": "run", "action_type": "UniLabJsonCommand", "param": {},
            "param_schema": {"type": "object", "properties": {"goal": {"type": "object", "properties": {}}}},
            "execution_policy": {}, "resource_plan_id": plan["plan_id"],
            "resource_interval_ids": [v["interval_id"] for v in plan["intervals"] if node_id in v["node_uuids"]],
            "resource_acquire_set_id": next((v["acquire_set_id"] for v in plan["acquire_sets"] if v["node_uuid"] == node_id), ""),
        })
        jobs.append({"uuid": stable_uuid(f"{name}:job:{i}"), "workflow_node_uuid": node_id,
                     "topological_index": i, "executor_kind": "device_action", "execution_policy": {},
                     "execution_timeout_seconds": 0, "param": {}})
    edges = [{**e, "uuid": stable_uuid(f"{name}:edge:{i}"), "source_handle_uuid": "", "target_handle_uuid": "",
              "dependency_only": True, "source_data_key": "", "target_data_key": "", "source_type": "", "target_type": ""}
             for i, e in enumerate(graph["edges"])]
    return {"task_name": name, "jobs": jobs, "execution_plan": {
        "version": 1, "run_mode": "normal", "target_node_uuid": None, "nodes": nodes,
        "edges": edges, "handles": [], "capabilities": plan["capabilities"], "resource_plan": plan,
    }}


def run_case(
    client: Client, output: Path, sources: list[dict[str, Any]], same_region: bool, reverse: bool,
) -> dict[str, Any]:
    """同时提交两个任务，独立记录 PLC 完成后才向调度器结算。"""
    name = f"{'same' if same_region else 'different'}-{'BA' if reverse else 'AB'}"
    directory = output / name
    directory.mkdir(parents=True, exist_ok=False)
    runtime = build_core_runtime(directory, device_ids=("reactor-a", "reactor-b", "region-a", "region-b"))
    events: list[dict[str, Any]] = []
    pending: dict[str, str] = {}
    peak = 0
    overlap_samples: list[dict[str, Any]] = []

    def read(key: str) -> Any:
        return client.get_node("ns=4;s=上位机通讯|" + key).get_value()

    def write(key: str, value: Any) -> None:
        node = client.get_node("ns=4;s=上位机通讯|" + key)
        node.set_value(ua.DataValue(ua.Variant(value, node.get_data_type_as_variant_type())))

    def done_key(channel: str) -> str:
        return channel + ("加工完成" if channel == "S06" else "工艺完成")

    def dispatch(payload: dict[str, Any]) -> None:
        nonlocal peak
        channel = "S06" if payload["device_id"] == "reactor-a" else "S07"
        assert channel not in pending.values(), "同一工站重复派发"
        assert read(channel + "允许加工") and not read(done_key(channel)), "PLC 工站非空闲"
        write(channel + "工艺选择", 1)
        write(channel + "参数写入完成", True)
        pending[payload["job_id"]] = channel
        peak = max(peak, len(pending))
        events.append({"event": "dispatch", "job_id": payload["job_id"], "channel": channel,
                       "monotonic": time.monotonic(), "epoch": time.time()})

    runtime.scheduler._dispatcher = CallbackDispatcher(dispatch)
    try:
        specs = [frozen_workflow(runtime, s, "region-a" if same_region or i == 0 else "region-b", f"{name}-{i}")
                 for i, s in enumerate(sources)]
        (directory / "frozen-workflows.json").write_text(json.dumps(specs, ensure_ascii=False, indent=2))
        order = [1, 0] if reverse else [0, 1]
        tasks = [runtime.submit_frozen(**specs[i]) for i in order]
        assert len(pending) == (1 if same_region else 2), "首次并发数不符合区域绑定"
        deadline = time.monotonic() + 90
        while sum(e["event"] == "completed" for e in events) < 4:
            assert time.monotonic() < deadline, "工作流未在时限内完成"
            # 同时观察两通道已接受参数且尚未完成，证明 PLC 命令在途重叠。
            states = {c: {"parameters_written": read(c + "参数写入完成"), "done": read(done_key(c)),
                          "process": read(c + "工艺选择")} for c in set(pending.values())}
            if len(states) == 2 and all(s["parameters_written"] and not s["done"] and s["process"] == 1 for s in states.values()):
                overlap_samples.append({"monotonic": time.monotonic(), "states": states})
            for job, channel in list(pending.items()):
                if not read(done_key(channel)):
                    continue
                events.append({"event": "completed", "job_id": job, "channel": channel,
                               "monotonic": time.monotonic(), "epoch": time.time(), "plc_done": True})
                write(channel + "参数写入完成", False)
                write(channel + "工艺选择", 0)
                reset_deadline = time.monotonic() + 10
                while read(done_key(channel)) or not read(channel + "允许加工"):
                    assert time.monotonic() < reset_deadline, "PLC 完成信号未复位"
                    time.sleep(.02)
                del pending[job]
                runtime.scheduler.on_job_finished(job, True, {"plc_completion_evidence": True})
            time.sleep(.02)
        dispatched = [e["job_id"] for e in events if e["event"] == "dispatch"]
        expected = [j["uuid"] for i in order for j in specs[i]["jobs"]]
        assert len(dispatched) == len(set(dispatched)) == 4
        if same_region:
            assert dispatched == expected and peak == 1, "同一区域跨动作被插入"
        else:
            assert peak == 2 and overlap_samples, "不同区域没有 PLC 在途重叠证据"
        states = {t["task"]["uuid"]: runtime.workflow_store.get_task(t["task"]["uuid"])["status"] for t in tasks}
        assert set(states.values()) == {"succeeded"}, states
        claims = runtime.inventory_store.query_all("SELECT claim_uuid FROM station_execution_claim WHERE state IN ('prepared','reserved','running','uncertain')")
        assert not claims, "任务完成后仍有活动占用"
        with runtime.workflow_store.read() as conn:
            leases = conn.execute("SELECT uuid FROM execution_lock_lease WHERE state IN ('reserved','running','uncertain')").fetchall()
        assert not leases, "任务完成后仍有软件租约"
        return {"case": name, "status": "passed", "tasks": states, "peak_in_flight": peak,
                "plc_overlap_samples": overlap_samples, "events": events, "active_claims": 0, "active_leases": 0}
    finally:
        (directory / "events.json").write_text(json.dumps(events, ensure_ascii=False, indent=2))
        runtime.close()


def main() -> None:
    """执行四种绑定/顺序组合；失败保留已采集证据，不强行清锁。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--workflows", type=Path, required=True,
                        help="外部输入目录，包含 workflow-a.json 和 workflow-b.json")
    parser.add_argument("--output", type=Path, required=True,
                        help="新建的验证结果目录，须与输入目录分离")
    args = parser.parse_args()
    args.workflows = args.workflows.expanduser().resolve()
    args.output = args.output.expanduser().resolve()
    if not args.workflows.is_dir():
        parser.error(f"工作流输入目录不存在或不是目录：{args.workflows}")
    if args.output.is_relative_to(args.workflows) or args.workflows.is_relative_to(args.output):
        parser.error("输入与输出目录不能相同或相互包含")
    sources: list[dict[str, Any]] = []
    for letter in ("a", "b"):
        path = args.workflows / f"workflow-{letter}.json"
        try:
            source = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            parser.error(f"无法读取工作流输入 {path}：{error}")
        if not isinstance(source, dict):
            parser.error(f"工作流输入必须是 JSON 对象：{path}")
        sources.append(source)
    args.output.mkdir(parents=True, exist_ok=False)
    results: dict[str, Any] = {"status": "running", "endpoint": args.endpoint, "cases": []}
    client = Client(args.endpoint, timeout=5)
    try:
        client.connect()
        for same in (True, False):
            for reverse in (False, True):
                case = run_case(client, args.output, sources, same, reverse)
                results["cases"].append(case)
                print(f"{case['case']}: passed, peak={case['peak_in_flight']}", flush=True)
        results["status"] = "passed"
    except BaseException as error:
        results.update(status="failed", error=str(error))
        raise
    finally:
        try:
            client.disconnect()
        finally:
            (args.output / "result.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
