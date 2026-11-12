"""自然语言复现的独立只读验收：不以模型自报成功代替设备/权威证据。"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

from tests.e2e.readme_demos import api_request


def _body_data(body):
    """MCP 回包 body：信封取 ``data``，其他形状原样返回。"""

    return body.get("data", body) if isinstance(body, dict) and "code" in body else body


def _events(output: Path) -> list[dict]:
    return [json.loads(line) for line in (output / "codex" / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.startswith("{")]


def _calls(output: Path) -> list[dict]:
    return [entry["item"] for entry in _events(output)
            if entry.get("type") == "item.completed" and entry.get("item", {}).get("type") == "mcp_tool_call"]


def _result(call: dict) -> dict:
    result = call.get("result") or {}
    if result.get("structured_content") is not None:
        return result["structured_content"]
    try:
        return json.loads(result["content"][0]["text"])
    except (KeyError, ValueError, IndexError, TypeError):
        return {}


def assert_closed_loops(spec, source: Path, evidence: dict, output: Path, port: int | None = None) -> list[dict]:
    sys.path.insert(0, str(source))
    try:
        # 只调用 demo 自带的 assert_*，绝不调用 run_smoke/run_workflow 一类写入函数。
        smoke = importlib.import_module(f"{spec.package}.smoke")
    finally:
        sys.path.remove(str(source))
    calls = _calls(output)
    observations = [json.loads(line) for line in (output / "observations.jsonl").read_text(encoding="utf-8").splitlines()]
    names = {item["uuid"]: item["name"] for item in evidence["workflows"]["items"]}
    proofs = {}
    for value in evidence["runs"].values():
        task = value["task"]
        proof = {**value, "workflow_name": names[task["workflow_uuid"]], "task_uuid": task["uuid"],
                 "task_status": task["status"], "task_error_info": task.get("error_info", [])}
        # 公开 node-run 仅存定义引用；与 demo smoke 一样按 workflow_node_uuid 关联图中的名称。
        node_names = {node["uuid"]: node["name"] for node in value["graph"]["nodes"]}
        proof["node_runs"] = [{**run, "name": node_names[run["workflow_node_uuid"]]} for run in value["node_runs"]]
        proofs[proof["workflow_name"]] = proof
    checks = []

    def check(name, action):
        try:
            action()
            checks.append({"check": name, "passed": True})
        except Exception as exc:
            checks.append({"check": name, "passed": False, "error": str(exc) or repr(exc)})

    def assert_trace():
        assert calls, "缺少 MCP tool-call 证据"
        assert any(call.get("tool") == "workflow_task_create" or
                   (call.get("tool") == "protocol_batch" and any(r.get("tool_name") == "workflow_task_create"
                    for r in call.get("arguments", {}).get("requests", []))) for call in calls)
        forbidden = [entry for entry in _events(output) if entry.get("item", {}).get("type") in
                     {"command_execution", "file_change", "web_search"}]
        assert not forbidden, "AI 绕过 MCP 使用 shell/文件修改/网络搜索"
        if spec.package != "materials_demo":
            allowed_writes = {"workflow_workflow_from_template", "workflow_workflow_create", "workflow_graph_save",
                              "workflow_task_create", "decisions_error_decisions_resolve"}
            from unilabos.server.mcp.catalog import OPERATIONS

            mutates = {operation.tool_name for operation in OPERATIONS if operation.mutates}
            invoked = []
            for call in calls:
                if call.get("tool") == "protocol_batch":
                    invoked += [r["tool_name"] for r in call.get("arguments", {}).get("requests", [])]
                else:
                    invoked.append(call.get("tool"))
            assert not (set(invoked) & mutates) - allowed_writes, "尝试了场景范围以外的业务写操作"
    check("所有业务操作由真实 Codex 经 MCP 完成", assert_trace)

    if spec.package == "workstation_demo":
        check("共享端点 PONG / slave 3、7 / 串口 1 / Modbus 4", lambda: smoke.assert_workflow_proof(proofs[spec.workflows[0].name], "hostlink"))
    elif spec.package == "lan_demo":
        check("远端 echo→stop→start 轮次严格递增", lambda: smoke.assert_workflow_proof(proofs[spec.workflows[0].name]))
        def device_proof():
            path = output / "initial-device-proofs.json"
            smoke.assert_smoke_proof(json.loads(path.read_text(encoding="utf-8"))["LAN_DEMO_PROOF_FILE"], "hostlink")
        check("跨设备订阅及远端 stop 闭环 proof", device_proof)
    elif spec.package == "complex_workflow_demo":
        functions = [smoke.assert_for_workflow, smoke.assert_wait_workflow, smoke.assert_until_workflow,
                     smoke.assert_nested_workflow, smoke.assert_complex_workflow]
        for expectation, function in zip(spec.workflows, functions):
            check(expectation.name + " 实值与各轮历史", lambda e=expectation, f=function: f(proofs[e.name]))
    elif spec.package == "exception_demo":
        for expectation, function in zip(spec.workflows, [smoke.assert_failure_workflow,
                                                         smoke.assert_recovery_workflow, smoke.assert_retry_workflow]):
            def verify(e=expectation, f=function):
                proof = proofs[e.name]
                matching = []
                for call in calls:
                    result = _result(call)
                    if call.get("tool") == "protocol_wait_task":
                        items = result.get("decisions", [])
                    elif call.get("tool") == "decisions_error_decisions_list":
                        body = result.get("body", {})
                        data = body.get("data", body)
                        items = data.get("items", [])
                    else:
                        continue
                    matching += [item for item in items if item.get("task_id") == proof["task_uuid"]]
                assert matching, "缺少处理前决策报文"
                proof["decision"] = {**matching[0], "selected_action": e.error_decision["action"]}
                # 当前协议的 options 是带 action/label/description 的对象，不是旧 smoke 的字符串。
                # 保留原始回包在 events.jsonl；这里仅投影 action 字段供原有语义断言使用。
                proof["decision"]["options"] = [option["action"] for option in matching[0]["options"]]
                f(proof)
            check(expectation.name + " 异常内容、决策和计数", verify)
    elif spec.package == "inventory_demo":
        totals = []
        for event in observations:
            if event["kind"] == "lots":
                totals += list(event["data"])
        for expectation, function, total in zip(spec.workflows,
                                                [smoke.assert_restock, smoke.assert_dispense_ok, smoke.assert_dispense_short, smoke.assert_audit],
                                                [100, 60, 60, 60]):
            def verify(e=expectation, f=function, expected_total=total):
                proof = proofs[e.name]
                lots = [lot for lot in totals if lot["lot_uuid"] == smoke.WATER_LOT_UUID
                        and lot["quantity_total"] == expected_total and lot["quantity_reserved"] == 0]
                assert lots, f"权威未观察到 {expected_total} / {expected_total} / 0 库存"
                proof["lot"] = lots[-1]
                f(proof)
            check(expectation.name + " 库存实值与未派发证据", verify)
    elif spec.package == "lock_demo":
        def group(names, assertion):
            queued = {}
            for name in names:
                proof = proofs[name]
                for event in observations:
                    if event["kind"] == "locks":
                        for request in event["data"].get("requests", []):
                            if request.get("task_uuid") == proof["task_uuid"] and request.get("status") == "waiting" and request.get("blockers"):
                                queued[name] = request
            assertion({"tasks": [proofs[name] for name in names], "queued": queued})
        check("动作锁排队及 always_free 同动作重叠", lambda: group(smoke.ACTION_LOCK_GROUP, smoke.assert_action_lock_group))
        check("跨设备同板锁排队", lambda: group(smoke.MATERIAL_LOCK_GROUP, smoke.assert_material_lock_group))
        check("设备账本四项独立审计", lambda: smoke.assert_audit(proofs[smoke.AUDIT_WORKFLOW_NAME]))
    elif spec.package == "materials_demo":
        functions = [lambda p: smoke.assert_site_loop_workflow(p, "hostlink"), smoke.assert_material_loop_workflow,
                     smoke.assert_site_tour_workflow, smoke.assert_material_flow_workflow]
        for expectation, function in zip(spec.workflows, functions):
            check(expectation.name + " 物料/孔位实值", lambda e=expectation, f=function: f(proofs[e.name]))
        check("第三阶段：先缺料原子回滚，再补料出库加液", lambda: assert_material_stage_three(evidence, observations, port, output))
    return checks


def assert_material_stage_three(evidence: dict, observations: list, port: int | None, output: Path):
    matching = []
    for value in evidence["runs"].values():
        nodes = value["graph"]["nodes"]
        if len(nodes) == 3 and {node.get("action_name") for node in nodes} == {
            "apply_deduct_resource", "fill_well", "bench_report"
        }:
            matching.append(value)
    assert len(matching) == 2, f"第三阶段应同一张三节点图执行两次，实际 {len(matching)}"
    statuses = [value["task"]["status"] for value in matching]
    assert sorted(statuses) == ["failed", "succeeded"], f"第三阶段预期缺料失败及补料成功，实际 {statuses}"
    failed = next(value for value in matching if value["task"]["status"] == "failed")
    succeeded = next(value for value in matching if value["task"]["status"] == "succeeded")
    assert failed["task"]["workflow_uuid"] == succeeded["task"]["workflow_uuid"]
    error = failed["task"]["error_info"][0]
    assert error["code"] == "plan_not_executable" and "short by 700" in error["message"], error
    assert [run["status"] for run in failed["node_runs"]] == ["canceled"] * 3
    assert all(run["return_info"] == {} for run in failed["node_runs"])
    assert all(run["status"] == "succeeded" for run in succeeded["node_runs"])
    by_action = {node["uuid"]: node["action_name"] for node in succeeded["graph"]["nodes"]}
    ordered = sorted(succeeded["node_runs"], key=lambda run: run["topological_index"])
    assert [by_action[run["workflow_node_uuid"]] for run in ordered] == [
        "apply_deduct_resource", "fill_well", "bench_report"
    ]
    values = {by_action[run["workflow_node_uuid"]]: run["return_info"]["return_value"] for run in succeeded["node_runs"]}
    fill, report = values["fill_well"], values["bench_report"]
    assert fill["volume"] == 1200 and fill["unit"] == "ul" and fill["well"] == "A1"
    assert fill["slot"] == "T1" and fill["fills"] == 1
    assert fill["substances"] == [["Water", 1200, "ul"]]
    assert report["sites"]["T1"] == fill["plate_name"] and report["sites"]["T2"] == ""
    assert report["sites"]["T3"] == "bench_plate_r1" and report["sites"]["T4"] == "bench_plate_r2"
    totals = [lot for event in observations if event["kind"] == "lots" for lot in event["data"]]
    for quantity in (500, 10500, 9300):
        assert any(lot["quantity_total"] == quantity and lot["quantity_available"] == quantity
                   and lot["quantity_reserved"] == 0 for lot in totals), f"未观察到 {quantity}/{quantity}/0"
    proof_path = output / "materials-authority.json"
    if port is not None:
        failed_reservations = api_request(port, f"/materials/reservations?task_uuid={failed['task']['uuid']}")
        succeeded_reservations = api_request(port, f"/materials/reservations?task_uuid={succeeded['task']['uuid']}")
        deck = api_request(port, "/materials/instances?name=bench_deck")[0]
        selected_uuid = next(site["occupied_material_uuid"] for site in deck["sites"] if site["label"] == "T1")
        selected = api_request(port, f"/materials/instances/{selected_uuid}/tree")
        proof = {"deck": deck, "selected_tree": selected, "failed_reservations": failed_reservations,
                 "succeeded_reservations": succeeded_reservations}
        # 先留存原始 HTTP 证据；实例停止后仍可离线复核同一份结果。
        proof_path.write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
        deck, selected = proof["deck"], proof["selected_tree"]
        failed_reservations = proof["failed_reservations"]
        succeeded_reservations = proof["succeeded_reservations"]
        selected_uuid = next(site["occupied_material_uuid"] for site in deck["sites"] if site["label"] == "T1")
    assert failed_reservations == []
    assert len(succeeded_reservations) == 2 and all(r["status"] == "consumed" for r in succeeded_reservations)
    lot_uuid = next(item["lot_uuid"] for reservation in succeeded_reservations
                    for item in reservation["items"] if item["kind"] == "lot")
    calls = _calls(output)
    failed_seen = []
    for index, call in enumerate(calls):
        if call.get("tool") not in {"protocol_wait_task", "workflow_task_get"}:
            continue
        body = _result(call).get("body", {})
        task = body.get("data", body)
        if task.get("uuid") == failed["task"]["uuid"] and task.get("status") == "failed":
            failed_seen.append(index)
    assert failed_seen, "缺少 AI 查询缺料终态的原始回包"
    topped_up = next(index for index, call in enumerate(calls) if index > failed_seen[0]
                    and call.get("tool") == "materials_v1_lots_inbound"
                    and call.get("arguments", {}).get("body", {}).get("payload", {}).get("quantity") == 10000)
    # MCP 回包 body 是统一信封 {code, data}：比对的是 data 里的物料 / 批次
    rollback_reads = [_body_data(_result(call).get("body", {})) for call in calls[failed_seen[0] + 1:topped_up]]
    plates = [body["material"] for body in rollback_reads if isinstance(body, dict)
              and body.get("material", {}).get("name") in {"flow_plate_01", "flow_plate_02"}]
    assert {plate["name"] for plate in plates} == {"flow_plate_01", "flow_plate_02"}
    assert all(plate["lifecycle_status"] == "active" and plate["parent_material_uuid"] is None for plate in plates)
    assert any(isinstance(body, dict) and body.get("lot_uuid") == lot_uuid
               and (body["quantity_total"], body["quantity_available"], body["quantity_reserved"]) == (500, 500, 0)
               for body in rollback_reads), "缺料失败后、补料前的 lot 必须仍为 500/500/0"
    root = next(node for node in selected["nodes"] if node["material"]["material_uuid"] == selected_uuid)
    assert root["material"]["lifecycle_status"] == "in_use"
    assert root["material"]["parent_material_uuid"] == deck["material"]["material_uuid"]
    assert root["material"]["name"] == fill["plate_name"]
    well_name = root["material"]["config"]["ordering"]["A1"]
    well = next(node for node in selected["nodes"] if node["material"]["name"] == well_name)
    assert well["material"]["parent_material_uuid"] == selected_uuid
    assert well["data"]["data"]["volume"] == 1200
    substances = well["data"]["substances"]
    assert len(substances) == 1
    assert (substances[0]["name"], substances[0]["quantity"], substances[0]["quantity_unit"]) == ("Water", 1200, "ul")
