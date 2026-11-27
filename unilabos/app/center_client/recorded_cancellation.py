"""核实历史人工取消报告与本地成功的差异；不改写任何原执行事实。"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any
from uuid import UUID, uuid5

from unilabos.app.center_client.transport import CenterTransportError
from unilabos.center_protocol.commands import validate_cancellation
from unilabos.center_protocol.digest import digest
from unilabos.center_protocol.events import validate_event
from unilabos.center_protocol.reconciliation import validate_receipt, validate_request, validate_snapshot
from unilabos.persistence import recovery_mode
from unilabos.workflow.center_boundary import _bind, cancellation_requested
from unilabos.workflow.material_ports import output_material_ids, ports_from_graph

KIND = "recorded_cancellation_reconciliation"
_STALE_CODE = "RECONCILIATION_SNAPSHOT_STALE"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _work_uuid(request: dict[str, Any], field: str, attempt: int = 0) -> str:
    label = "record-local-release" if field == "release_evidence_request" else "record-original-difference"
    if attempt:
        label += f":retry:{attempt}"
    return str(uuid5(UUID(request["settlement_uuid"]), label))


def _supersedes(previous: dict[str, Any], current: dict[str, Any]) -> bool:
    """只允许把明确未应用的旧请求追加为历史，不能跳过不确定 ACK。"""
    before, after = previous.get("superseded_requests", []), current.get("superseded_requests", [])
    if previous.get("receipt") is not None or len(after) != len(before) + 1 or after[:-1] != before:
        return False
    archived = after[-1]
    return (all(archived.get(key) == previous.get(key) for key in ("request_uuid", "body", "receipt"))
            and archived.get("error", {}).get("status_code") == 409
            and archived.get("error", {}).get("code") == _STALE_CODE
            and current.get("receipt") is None)


def recorded_cancellation_state(workflow: Any, invocation: str) -> dict[str, Any] | None:
    """只读独立历史核实状态，普通取消结算回执不可混入此类型。"""
    with workflow.read() as c:
        row = c.execute(
            "SELECT recorded_cancellation_json FROM center_outcome_evidence WHERE invocation_uuid=?",
            (invocation,),
        ).fetchone()
        return json.loads(row[0]) if row is not None and row[0] else None


def _save(workflow: Any, invocation: str, state: dict[str, Any]) -> None:
    """每个外部副作用前持久工作单；已经提交的阶段只能向前推进。"""
    with workflow.transaction() as c:
        row = c.execute(
            "SELECT cancel_settlement_json,recorded_cancellation_json FROM center_outcome_evidence WHERE invocation_uuid=?",
            (invocation,),
        ).fetchone()
        if row is None or row["cancel_settlement_json"]:
            raise ValueError("历史核实缺少原物料责任，或已进入普通取消结算")
        if row["recorded_cancellation_json"]:
            old = json.loads(row["recorded_cancellation_json"])
            if old["request"] != state["request"] or old.get("kind") != KIND:
                raise ValueError("历史核实的冻结事实、原因和证据不可改写")
            if old.get("completed") and old != state:
                raise ValueError("已完成历史核实不可回退")
            if old.get("resources_released") and (
                not state.get("resources_released")
                or old.get("inventory_release_proof") != state.get("inventory_release_proof")
            ):
                raise ValueError("历史核实的库存释放证明不可回退或替换")
            for field in ("evidence_request", "release_evidence_request"):
                previous, current = old.get(field), state.get(field)
                if previous:
                    if not current:
                        raise ValueError("历史核实必须重放原幂等请求")
                    changed = any(previous[k] != current[k] for k in ("request_uuid", "body"))
                    if changed:
                        if (not _supersedes(previous, current)
                                or current["request_uuid"] != _work_uuid(state["request"], field, len(current["superseded_requests"]))):
                            raise ValueError("只有中心明确拒绝的过期请求能保留历史后重新核实")
                    elif previous.get("superseded_requests", []) != current.get("superseded_requests", []):
                        raise ValueError("历史核实的过期请求历史不能改写")
                if previous and previous.get("receipt") is not None and previous["receipt"] != current.get("receipt"):
                    raise ValueError("历史核实的中心回执不可替换")
        c.execute(
            "UPDATE center_outcome_evidence SET recorded_cancellation_json=? WHERE invocation_uuid=?",
            (_json(state), invocation),
        )


def _local_facts(runtime: Any, invocation: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """验证原身份、原人工终态、真实本地结果和取消通知；不读取可变库存作冻结事实。"""
    if str(UUID(invocation)) != invocation:
        raise ValueError("历史核实调用 UUID 无效")
    workflow = runtime.materials.workflow
    with workflow.transaction() as c:
        inbox = c.execute("SELECT * FROM center_invocation_inbox WHERE invocation_uuid=?", (invocation,)).fetchone()
        if inbox is None or not inbox["local_task_uuid"] or inbox["terminal_recorded"] != 1:
            raise ValueError("历史核实必须绑定原已记录终态的本地任务")
        task, package = _bind(c, invocation_uuid=invocation, task_uuid=inbox["local_task_uuid"])
        if workflow.execution_held(task["uuid"]) or workflow.invocation_held(invocation) or runtime.store.invocation_held(invocation):
            raise ValueError("原任务或调用处于恢复保护，不能结清历史责任")
        if task["status"] != "succeeded" or task["cleanup_status"] not in {"none", "settled"}:
            raise ValueError("仅核实真实本地成功且物理清理已安全结束的历史差异")
        if not cancellation_requested(c, task["uuid"]):
            raise ValueError("必须有中心发给原调用的有效取消命令")
        payload = package["payload"]
        if any(payload[name] for name in ("material_refs", "container_refs", "handoff_uuids")) or "transport" in payload:
            raise ValueError("有跨站物料或搬运责任的调用不能使用历史取消核实")
        if c.execute("SELECT 1 FROM center_material_input WHERE invocation_uuid=?", (invocation,)).fetchone():
            raise ValueError("存在原输入占用责任，不能使用历史取消核实")
        evidence = c.execute("SELECT * FROM center_outcome_evidence WHERE invocation_uuid=?", (invocation,)).fetchone()
        if (evidence is None or evidence["local_task_uuid"] != task["uuid"]
                or evidence["pending_reason"] != "closed_center_experiment_canceled"
                or evidence["cancel_settlement_json"] or evidence["evidence_json"] is not None):
            raise ValueError("原调用不属于人工收尾取消留下的未补报历史物料结果")
        result = c.execute("SELECT result FROM workflow_result WHERE workflow_task_uuid=?", (task["uuid"],)).fetchone()
        if result is None:
            raise ValueError("历史核实不能丢失原成功结果")
        output = json.loads(result[0])
        if not isinstance(output, dict):
            raise ValueError("原成功结果不是完整输出对象")
        graph = json.loads(task["workflow_snapshot"])
        output_ids = sorted(output_material_ids(ports_from_graph(graph), output))
        if not output_ids:
            raise ValueError("历史物料核实必须有冻结端口声明的真实输出 UUID")
        outbox = c.execute("SELECT * FROM center_boundary_outbox WHERE invocation_uuid=? AND phase=1", (invocation,)).fetchone()
        if outbox is None or outbox["acknowledged"] != 1 or outbox["application"] != "applied":
            raise ValueError("原人工取消事件尚未获得中心 applied 确认")
        event = validate_event(json.loads(outbox["event_json"]))
        if any(outbox[key] != event[key] for key in ("event_uuid", "event_epoch_uuid", "event_sequence")):
            raise ValueError("原人工取消 outbox 索引与完整信封不一致")
        identity = {"station_invocation_uuid": invocation, "station_uuid": runtime.store.station_uuid,
                    "instance_uuid": runtime.store.instance_uuid, "event_epoch_uuid": runtime.store.event_epoch_uuid,
                    "local_task_uuid": task["uuid"]}
        if (any(event[key] != value for key, value in identity.items())
                or event["event_type"] != "outcome" or event["payload"]["status"] != "canceled"
                or event["payload"]["task_payload_digest"] != package["payload_digest"]
                or any(event["payload"].get(key) for key in ("material_refs", "container_refs", "artifact_refs"))):
            raise ValueError("原人工取消事件与原工站、实例、纪元或冻结任务不符")
        if payload["station_uuid"] != runtime.store.station_uuid or any(
            getattr(runtime.materials.authority, key) != getattr(runtime.store, key)
            for key in ("station_uuid", "instance_uuid", "event_epoch_uuid")
        ):
            raise ValueError("物料权威与原执行身份不一致")
        commands = []
        for row in c.execute(
            "SELECT envelope_json FROM center_command_inbox "
            "WHERE json_extract(envelope_json,'$.command.type')='task.cancel' "
            "AND json_extract(envelope_json,'$.command.payload.station_invocation_uuid')=? ORDER BY command_uuid",
            (invocation,),
        ).fetchall():
            envelope = json.loads(row[0])
            command = envelope["command"]
            if command.get("type") != "task.cancel" or envelope.get("disposition") != "active":
                continue
            try:
                cancel = validate_cancellation(command["payload"])
            except ValueError:
                continue
            if (cancel["station_invocation_uuid"] == invocation
                    and cancel["global_task_uuid"] == payload["global_task_uuid"]
                    and command["station_uuid"] == payload["station_uuid"]
                    and not recovery_mode.command_held(c, command)):
                commands.append(envelope)
        if not commands:
            raise ValueError("原调用有效取消命令已变化")
        facts = {**identity, "task_payload_digest": package["payload_digest"], "package": package,
                 "execution_status": task["status"], "cleanup_status": task["cleanup_status"],
                 "original_output_json": result[0], "output_material_uuids": output_ids,
                 "workflow_snapshot_digest": digest(graph), "original_canceled_event": event,
                 "cancellation_commands": commands, "pending_reason": evidence["pending_reason"]}
        state = json.loads(evidence["recorded_cancellation_json"]) if evidence["recorded_cancellation_json"] else None
        return facts, state


def _validate_center(snapshot: dict[str, Any], facts: dict[str, Any]) -> dict[str, Any]:
    snapshot = validate_snapshot(snapshot, facts["station_invocation_uuid"])
    payload, event = facts["package"]["payload"], facts["original_canceled_event"]
    if (any(snapshot[key] != payload[key] for key in ("station_invocation_uuid", "global_task_uuid", "station_uuid", "workflow_uuid"))
            or snapshot["payload_digest"] != facts["task_payload_digest"] or snapshot["recovery_blocked"]
            or snapshot["conflicts"] or snapshot["result_revision"] != 1):
        raise ValueError("中心冻结身份、结果修订或恢复冲突不满足历史核实条件")
    if any(item.get("application") in {"conflict", "conflict_needs_attention"}
           for collection in ("result_events", "event_receipts") for item in snapshot[collection]):
        raise ValueError("中心存在冲突结果或事件回执，不能结束历史物料责任")
    state = snapshot["state"]
    if (state["owner_instance_uuid"] != facts["instance_uuid"] or state["local_task_uuid"] != facts["local_task_uuid"]
            or state["execution_status"] != "canceled" or state["attention_state"] != "clear"):
        raise ValueError("中心原取消终态、owner、本地任务或待核实状态已变化")
    result_body: list[Any] = ["canceled", event["payload"]["output"]]
    if "finished_at" in event["payload"]:
        result_body.append({key: event["payload"][key] for key in (
            "material_refs", "container_refs", "artifact_refs", "started_at", "finished_at")})
    expected_result = digest(result_body)
    terminal = [item for item in snapshot["result_events"]
                if item.get("event_type") in {"succeeded", "failed", "canceled"} and item.get("application") == "applied"]
    if (len(terminal) != 1 or any(terminal[0].get(key) != value for key, value in {
        "event_uuid": event["event_uuid"], "event_type": "canceled", "owner_instance_uuid": facts["instance_uuid"],
        "local_task_uuid": facts["local_task_uuid"], "result_digest": expected_result,
    }.items()) or state["result_digest"] != expected_result):
        raise ValueError("中心原终态结果与本站人工取消信封不能精确对应")
    receipts = [item for item in snapshot["event_receipts"] if item.get("event_uuid") == event["event_uuid"]]
    expected_receipt = {key: event[key] for key in ("event_uuid", "event_digest", "event_epoch_uuid", "event_sequence")}
    expected_receipt["application"] = "applied"
    if receipts != [expected_receipt]:
        raise ValueError("中心原事件回执与本站完整人工取消信封不一致")
    return snapshot


def inspect_recorded_cancellation(runtime: Any, invocation: str) -> dict[str, Any]:
    """返回可供操作者审阅的原始事实和乐观并发摘要，不提交核实。"""
    facts, state = _local_facts(runtime, invocation)
    snapshot = _validate_center(runtime.client.reconciliation_state(invocation, runtime.store.event_epoch_uuid), facts)
    if state:
        _validate_frozen_state(state, facts)
    return {"local_facts_digest": digest(facts), "center_evidence_digest": snapshot["evidence_digest"],
            "local_facts": facts, "center_snapshot": snapshot, "state": state}


def _validate_frozen_state(state: dict[str, Any], facts: dict[str, Any]) -> None:
    """恢复时校验所有重复保存的身份，不能凭局部字段释放另一任务的占用。"""
    request = state["request"]
    expected = {"kind": KIND, "local_facts": facts, "local_facts_digest": digest(facts),
                "settlement_uuid": str(uuid5(UUID(facts["station_invocation_uuid"]), "recorded-cancellation-reconciliation")),
                "task_payload_digest": facts["task_payload_digest"], "local_task_uuid": facts["local_task_uuid"],
                "original_output_json": facts["original_output_json"]}
    if (state.get("kind") != KIND or state.get("center_terminal_corrected") is not False
            or "release_receipt" in state or any(request.get(key) != value for key, value in expected.items())):
        raise ValueError("历史核实冻结后原执行事实或永久身份已变化")
    _validate_center(request["initial_center_snapshot"], facts)


def _evidence_body(state: dict[str, Any], snapshot: dict[str, Any], *, released: bool) -> dict[str, Any]:
    request = state["request"]
    facts = request["local_facts"]
    prefix = "历史差异核实：原 canceled 为人工收尾报告；真实本地 Task 已 succeeded。中心原终态保留，本次不授予执行权限。"
    if released:
        prefix += "本站原物料责任已核实并安全结清；原输出和错误事件均保留。"
        proof = digest(state["inventory_release_proof"])
    else:
        prefix += "尚未宣告本站库存责任释放。"
        proof = request["local_facts_digest"]
    # 原用户全文及全部引用在冻结 request 中；中心引用其摘要，原因不超过协议字节上限。
    note = request["reason"].encode("utf-8")[:500].decode("utf-8", errors="ignore")
    reference = (f"/api/v1/center-material-settlements/{facts['station_invocation_uuid']}/recorded-cancellation"
                 f"#request={digest(request)}&proof={proof}")
    return validate_request({
        "expected_state_revision": snapshot["state_revision"], "expected_result_revision": snapshot["result_revision"],
        "expected_evidence_digest": snapshot["evidence_digest"], "reason": prefix + note,
        "evidence_refs": [reference, *request["evidence_refs"][:31]], "resolution": {"kind": "record_evidence"},
    })


def _send_evidence(runtime: Any, invocation: str, state: dict[str, Any], field: str) -> None:
    """已发出的工作单始终重放同一幂等键和正文，ACK 丢失也不另造请求。"""
    for attempt in range(3):
        work = state[field]
        if work["receipt"] is not None:
            receipt = work["receipt"]
            break
        try:
            receipt = runtime.client.reconcile(invocation, work["body"], idempotency_key=work["request_uuid"],
                                               event_epoch_uuid=runtime.store.event_epoch_uuid)
            break
        except CenterTransportError as error:
            # 只有中心明确表示这个正文未应用，才允许新版本；未知网络错误仍原样重传。
            if error.status_code != 409 or error.code != _STALE_CODE:
                raise
            work["last_error"] = {"status_code": 409, "code": _STALE_CODE, "message": str(error)[:500]}
            _save(runtime.materials.workflow, invocation, state)
            runtime.store.require_execution()
            facts, _ = _local_facts(runtime, invocation)
            if facts != state["request"]["local_facts"]:
                raise ValueError("中心证据变化期间本地原执行事实也发生变化") from error
            snapshot = _validate_center(runtime.client.reconciliation_state(invocation, runtime.store.event_epoch_uuid), facts)
            body = _evidence_body(state, snapshot, released=field == "release_evidence_request")
            if body == work["body"]:
                raise
            archived = {key: deepcopy(work[key]) for key in ("request_uuid", "body", "receipt")}
            archived["error"] = deepcopy(work["last_error"])
            history = [*work.get("superseded_requests", []), archived]
            state[field] = {"request_uuid": _work_uuid(state["request"], field, len(history)), "body": body,
                            "receipt": None, "superseded_requests": history}
            _save(runtime.materials.workflow, invocation, state)
            if attempt == 2:
                # 限制本次竞争重试；新请求已经持久化，下一次显式恢复继续相同正文。
                raise
    work = state[field]
    receipt = validate_receipt(receipt, invocation, work["body"], actor_instance_uuid=runtime.store.instance_uuid)
    _validate_center(receipt["snapshot"], state["request"]["local_facts"])
    work["receipt"] = receipt
    _save(runtime.materials.workflow, invocation, state)


def reconciliation_completed(state: dict[str, Any] | None) -> bool:
    """只有两次精确证据回执和本站释放证明齐全，才结束原补报异常。"""
    try:
        if (not state or state.get("kind") != KIND or state.get("completed") is not True
                or state.get("resources_released") is not True or state.get("center_terminal_corrected") is not False
                or "release_receipt" in state):
            return False
        request, proof = state["request"], state["inventory_release_proof"]
        facts = request["local_facts"]
        _validate_frozen_state(state, facts)
        if (request["kind"] != KIND or request["local_facts_digest"] != digest(facts)
                or proof["kind"] != KIND or proof["request_digest"] != digest(request)
                or proof["task_uuid"] != request["local_task_uuid"]
                or proof["evidence_refs"] != request["evidence_refs"] or proof["input_receipt"] is not None):
            return False
        checked = {item["uuid"] for item in proof["checked_materials"]}
        if not set(facts["output_material_uuids"]).issubset(checked):
            return False
        for field in ("evidence_request", "release_evidence_request"):
            work = state[field]
            released = field == "release_evidence_request"
            history = work.get("superseded_requests", [])
            if work["request_uuid"] != _work_uuid(request, field, len(history)):
                return False
            receipt = validate_receipt(work["receipt"], facts["station_invocation_uuid"], work["body"],
                                       actor_instance_uuid=facts["instance_uuid"])
            _validate_center(receipt["snapshot"], facts)
            expected = _evidence_body(state, receipt["snapshot"], released=released)
            if any(work["body"][key] != expected[key] for key in ("reason", "evidence_refs", "resolution")):
                return False
            for index, archived in enumerate(history):
                if (archived["request_uuid"] != _work_uuid(request, field, index)
                        or archived["receipt"] is not None or archived["error"]["status_code"] != 409
                        or archived["error"]["code"] != _STALE_CODE
                        or any(archived["body"][key] != expected[key] for key in ("reason", "evidence_refs", "resolution"))):
                    return False
        if state["evidence_request"]["receipt"]["reconciliation_uuid"] == state["release_evidence_request"]["receipt"]["reconciliation_uuid"]:
            return False
        return True
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def reconcile_recorded_cancellation(runtime: Any, invocation: str, *, reason: str, evidence_refs: list[str],
                                    expected_local_facts_digest: str, expected_center_evidence_digest: str) -> dict[str, Any]:
    """显式核实历史差异、原子结清本站责任，再追加释放证据；不重跑任务。"""
    from unilabos.app.center_client.material_settlement import _release_inventory

    runtime.store.require_execution()
    inspected = inspect_recorded_cancellation(runtime, invocation)
    facts, snapshot, state = inspected["local_facts"], inspected["center_snapshot"], inspected["state"]
    if expected_local_facts_digest != inspected["local_facts_digest"]:
        raise ValueError("本地事实摘要已变化，请重新审阅")
    # 校验用户原文，不能在保存时悄悄截断。
    validate_request({"expected_state_revision": snapshot["state_revision"], "expected_result_revision": snapshot["result_revision"],
                      "expected_evidence_digest": snapshot["evidence_digest"], "reason": reason,
                      "evidence_refs": evidence_refs, "resolution": {"kind": "record_evidence"}})
    if state is None:
        if expected_center_evidence_digest != snapshot["evidence_digest"]:
            raise ValueError("中心证据摘要已变化，请重新审阅")
        request = {"kind": KIND, "settlement_uuid": str(uuid5(UUID(invocation), "recorded-cancellation-reconciliation")),
                   "local_facts": facts, "local_facts_digest": inspected["local_facts_digest"],
                   "initial_center_snapshot": snapshot, "task_payload_digest": facts["task_payload_digest"],
                   "local_task_uuid": facts["local_task_uuid"], "original_output_json": facts["original_output_json"],
                   "reason": reason, "evidence_refs": list(evidence_refs)}
        state = {"kind": KIND, "request": request, "resources_released": False, "inventory_release_proof": None,
                 "evidence_request": None, "release_evidence_request": None, "completed": False,
                 "center_terminal_corrected": False}
        _save(runtime.materials.workflow, invocation, state)
    else:
        request = state["request"]
        if request["reason"] != reason or request["evidence_refs"] != evidence_refs:
            raise ValueError("恢复必须使用原历史核实的原因和证据")
        if expected_center_evidence_digest not in {snapshot["evidence_digest"], request["initial_center_snapshot"]["evidence_digest"]}:
            raise ValueError("恢复请求没有引用当前或原始中心证据摘要")
        if state["completed"]:
            if not reconciliation_completed(state):
                raise ValueError("历史核实完成记录缺少精确证明")
            return deepcopy(state)
    if state["evidence_request"] is None:
        state["evidence_request"] = {"request_uuid": _work_uuid(request, "evidence_request"),
                                     "body": _evidence_body(state, snapshot, released=False), "receipt": None,
                                     "superseded_requests": []}
        _save(runtime.materials.workflow, invocation, state)
    _send_evidence(runtime, invocation, state, "evidence_request")
    # 每次恢复仍核对原执行事实；库存事务已有证明时只重放，不碰后续任务的新占用。
    current, _ = _local_facts(runtime, invocation)
    if current != facts:
        raise ValueError("核实期间原本地执行事实已变化")
    proof = _release_inventory(runtime, invocation, request, facts["package"], None,
                               extra_materials=facts["output_material_uuids"], settlement_kind=KIND)
    if state["resources_released"] and state["inventory_release_proof"] != proof:
        raise ValueError("本站历史核实库存证明发生变化")
    state["resources_released"], state["inventory_release_proof"] = True, proof
    _save(runtime.materials.workflow, invocation, state)
    if state["release_evidence_request"] is None:
        snapshot = _validate_center(runtime.client.reconciliation_state(invocation, runtime.store.event_epoch_uuid), facts)
        state["release_evidence_request"] = {
            "request_uuid": _work_uuid(request, "release_evidence_request"),
            "body": _evidence_body(state, snapshot, released=True), "receipt": None, "superseded_requests": [],
        }
        _save(runtime.materials.workflow, invocation, state)
    _send_evidence(runtime, invocation, state, "release_evidence_request")
    state["completed"] = True
    if not reconciliation_completed(state):
        raise ValueError("历史核实尚未取得完整责任结清证明")
    _save(runtime.materials.workflow, invocation, state)
    return deepcopy(state)
