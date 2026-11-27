"""把中心调用绑定和边界事件纳入工作流事实事务，不创建或调度任务。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import UUID, uuid4

from unilabos.center_protocol.commands import validate_cancellation
from unilabos.center_protocol.digest import DigestError, canonical_json
from unilabos.center_protocol.events import (
    EventEnvelopeError,
    utc_timestamp_from_ms,
    validate_event,
    validate_outcome_refs,
)
from unilabos.center_protocol.task_package import TaskPackageError, validate_task_package
from unilabos.persistence import center_events, recovery_mode
from unilabos.persistence.database import Database
from unilabos.persistence.inspection import table_exists
from unilabos.workflow.center_output import project_public_workflow_output
from unilabos.workflow.material_ports import MaterialPortError, output_material_ids, ports_from_graph
from unilabos.workflow.models import WorkflowTaskPriority

Connection = Database | sqlite3.Connection
_CENTER_TABLES = ("center_runtime_state", "center_invocation_inbox", "center_boundary_outbox")


def cancellation_requested(connection: Connection, task_uuid: str) -> bool:
    """已落盘取消阻止原 Task 首次派发；正式取消仍由有效控制会话执行。"""
    if not all(table_exists(connection, name) for name in ("center_invocation_inbox", "center_command_inbox")):
        return False
    invocation = connection.execute(
        "SELECT invocation_uuid,package_json FROM center_invocation_inbox WHERE local_task_uuid=?", (task_uuid,)
    ).fetchone()
    if invocation is None:
        return False
    package = validate_task_package(_load(invocation["package_json"], label="冻结任务包"))["payload"]
    rows = connection.execute(
        """SELECT envelope_json FROM center_command_inbox WHERE 1=1
           AND json_extract(envelope_json,'$.command.type')='task.cancel'
           AND json_extract(envelope_json,'$.command.payload.station_invocation_uuid')=?""",
        (invocation["invocation_uuid"],),
    ).fetchall()
    for row in rows:
        envelope = _load(row["envelope_json"], label="取消命令")
        command = envelope["command"]
        try:
            payload = validate_cancellation(command["payload"])
        except ValueError:
            continue
        if (envelope["disposition"] == "active"
                and command["station_uuid"] == package["station_uuid"]
                and payload["global_task_uuid"] == package["global_task_uuid"]
                and not recovery_mode.command_held(connection, command)):
            return True
    return False



def _conflict(message: str) -> RuntimeError:
    # Store 会调用本模块；延迟导入避免初始化环，也不为本地任务载入客户端依赖。
    from unilabos.workflow.store import StoreConflict

    return StoreConflict(message)


def _uuid(value: Any) -> None:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise _conflict("中心调用持久身份不是规范 UUID") from None


def _load(value: Any, *, label: str) -> Any:
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        raise _conflict(f"中心调用的{label}不是有效 JSON") from None


def _fact_time(value: Any) -> str:
    """把已有持久时间截取到毫秒，不用观测时间代替缺失事实。"""
    try:
        if not isinstance(value, str):
            raise ValueError
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        elapsed = parsed - datetime(1970, 1, 1, tzinfo=timezone.utc)
        return utc_timestamp_from_ms(elapsed // timedelta(milliseconds=1))
    except (ValueError, OverflowError):
        raise _conflict("中心调用缺少合法的持久任务时间") from None


def _intent(connection: Connection, invocation_uuid: str) -> tuple[Any, dict[str, Any], Any]:
    """从当前事务验证已领取 intent 和事件纪元，绝不借配置重建事实。"""
    if not connection.in_transaction:
        raise _conflict("中心调用必须在工作流写事务内绑定或恢复")
    if not all(table_exists(connection, name) for name in _CENTER_TABLES):
        raise _conflict("中心调用缺少已初始化的持久账本")
    _uuid(invocation_uuid)
    if (table_exists(connection, "center_transport_revocation")
            and connection.execute("SELECT 1 FROM center_transport_revocation WHERE invocation_uuid=?", (invocation_uuid,)).fetchone() is not None):
        raise _conflict("原搬运调用已持久撤销，禁止再次创建任务")
    inbox = connection.execute(
        "SELECT * FROM center_invocation_inbox WHERE invocation_uuid=?", (invocation_uuid,),
    ).fetchone()
    if inbox is None:
        raise _conflict("中心调用缺少已确认领取的 intent")
    if "acceptance_version" not in inbox.keys() or inbox["acceptance_version"] not in (0, 1):
        raise _conflict("中心调用受理账本版本无效")
    if inbox["local_task_uuid"] is not None:
        _uuid(inbox["local_task_uuid"])
    elif inbox["acceptance_version"] == 1 or inbox["terminal_recorded"]:
        raise _conflict("中心调用已受理但持久任务映射缺失")
    rows = connection.execute("SELECT * FROM center_runtime_state").fetchall()
    if len(rows) != 1 or rows[0]["singleton"] != "runtime":
        raise _conflict("中心调用的实例及纪元账本缺失或不唯一")
    runtime = rows[0]
    for name in ("station_uuid", "instance_uuid", "event_epoch_uuid"):
        _uuid(runtime[name])
    try:
        body = validate_task_package(_load(inbox["package_json"], label="冻结任务包"))
    except TaskPackageError as error:
        raise _conflict(str(error)) from error
    payload = body["payload"]
    if (payload["station_invocation_uuid"] != invocation_uuid
            or payload["station_uuid"] != runtime["station_uuid"]):
        raise _conflict("中心调用的任务包身份与持久账本不匹配")
    if inbox["claim_confirmed"] != 1 or ("reconciliation_uuid" in inbox.keys() and inbox["reconciliation_uuid"] is not None):
        validate_reconciliation_binding(connection, inbox=inbox, package=body, runtime=runtime)
    return inbox, body, runtime


def claimed_transport_context(connection: Connection, invocation_uuid: str) -> dict[str, Any] | None:
    """返回已领取原任务包的搬运许可；沿用实例、纪元和恢复账本校验。"""
    _, package, _ = _intent(connection, invocation_uuid)
    return package["payload"].get("transport")


def claimed_transport_binding(connection: Connection, invocation_uuid: str) -> dict[str, Any] | None:
    """仅从已正常领取的原任务包读取车辆绑定，不接受工作流参数替换。"""
    _, package, _ = _intent(connection, invocation_uuid)
    return package["payload"].get("transport_binding")


def validate_task_material_source_context(
    connection: Connection, task_uuid: str, plan: Mapping[str, Any],
) -> None:
    """准入前按持久调用账本核对原上下文，不访问中心或实时库存。"""
    from unilabos.workflow.material_source_context import validate_source_context

    rows = (connection.execute(
        "SELECT invocation_uuid FROM center_invocation_inbox WHERE local_task_uuid=?",
        (task_uuid,),
    ).fetchall() if table_exists(connection, "center_invocation_inbox") else [])
    if len(rows) > 1:
        raise _conflict("同一本地任务对应多个中心调用")
    payload = None
    if rows:
        inbox, package, _runtime = _intent(connection, rows[0]["invocation_uuid"])
        if inbox["acceptance_version"] != 1 or inbox["local_task_uuid"] != task_uuid:
            raise _conflict("中心物料来源缺少已受理的原任务绑定")
        payload = package["payload"]
    try:
        validate_source_context(plan, payload)
    except (ValueError, DigestError) as error:
        raise _conflict("中心物料来源上下文无效：" + str(error)) from error


def inspect_existing_task(
    connection: Connection, package: dict[str, Any], *, local_task_uuid: str | None = None,
    allow_terminal: bool = False,
) -> Any:
    """只读核对永久调用、原输入和已存在 Task，绝不从合同重建任务。"""
    try:
        package = validate_task_package(package)
        submission = _submission(package)
    except (TaskPackageError, ValueError, TypeError) as error:
        raise _conflict("核实冻结任务包无效") from error
    rows = connection.execute(
        "SELECT * FROM workflow_task WHERE backend_task_uuid=? AND invocation_key=?",
        (submission.backend_task_uuid, submission.invocation_key),
    ).fetchall()
    if len(rows) != 1:
        raise _conflict("恢复只能核实唯一现存 Task，缺失时禁止创建")
    task = rows[0]
    if (task["workflow_uuid"] != submission.workflow_id
            or task["revision_fingerprint"] != submission.revision_fingerprint
            or task["request_fingerprint"] != submission.request_fingerprint
            or (local_task_uuid is not None and task["uuid"] != local_task_uuid)):
        raise _conflict("核实与 Task 的永久调用、输入请求或执行指纹不匹配")
    if task["status"] not in ("pending", "running", "canceling", "succeeded", "failed", "canceled", "timeout"):
        raise _conflict("现存 Task 状态不能核实")
    if not allow_terminal and (task["status"] not in ("pending", "running", "canceling") or task["deleted_at"] is not None):
        raise _conflict("已终态或已删除 Task 不得请求 resume，必须核实已记录结果")
    from unilabos.workflow.schema import normalize_value, parse_input_contract, parse_value_schema

    try:
        graph = _load(task["workflow_snapshot"], label="冻结工作流图")
        parameters = parse_input_contract(graph["workflow"]["meta_data"]["unilab"]["input_contract"]).to_dict()["parameters"]
        if set(submission.input_value) - {parameter["name"] for parameter in parameters}:
            raise ValueError("输入包含未知字段")
        resolved = {parameter["name"]: normalize_value(
            parse_value_schema(parameter["schema"]),
            submission.input_value[parameter["name"]] if parameter["name"] in submission.input_value else parameter["default"],
        ) for parameter in parameters}
        from unilabos.center_protocol.digest import canonical_json

        if canonical_json(resolved) != canonical_json(_load(task["input"], label="当前 Task 输入")):
            raise ValueError("输入已变化")
    except (KeyError, TypeError, ValueError) as error:
        raise _conflict("现存 Task 输入或冻结输入合同与核实请求不一致") from error
    return task


def validate_reconciliation_binding(connection: Connection, *, inbox: Any, package: dict[str, Any], runtime: Any) -> None:
    """恢复凭据独立于普通 claim，必须绑定当前账本、原实例与同一现存 Task。"""
    from unilabos.center_protocol.reconciliation import ReconciliationError, validate_receipt

    identity = inbox["reconciliation_uuid"] if "reconciliation_uuid" in inbox.keys() else None
    if identity is None or not table_exists(connection, "center_reconciliation_request"):
        raise _conflict("中心调用缺少已确认领取或独立恢复核实")
    row = connection.execute("SELECT * FROM center_reconciliation_request WHERE reconciliation_uuid=?", (identity,)).fetchone()
    if row is None or not row["receipt_json"] or row["invocation_uuid"] != inbox["invocation_uuid"]:
        raise _conflict("中心调用恢复回执缺失或身份不匹配")
    body = _load(row["body_json"], label="核实请求")
    try:
        receipt = validate_receipt(_load(row["receipt_json"], label="核实回执"), inbox["invocation_uuid"], body,
                                   actor_instance_uuid=runtime["instance_uuid"])
    except ReconciliationError as error:
        raise _conflict(str(error)) from error
    resolution = body["resolution"]
    if (receipt["reconciliation_uuid"] != identity or resolution["kind"] != "resume_original_task"
            or resolution["event_epoch_uuid"] != runtime["event_epoch_uuid"]
            or row["event_epoch_uuid"] != runtime["event_epoch_uuid"]
            or row["station_uuid"] != runtime["station_uuid"] or row["instance_uuid"] != runtime["instance_uuid"]
            or receipt["snapshot"]["state"]["owner_instance_uuid"] != runtime["instance_uuid"]
            or any(receipt["snapshot"][name] != package["payload"][name]
                   for name in ("station_invocation_uuid", "global_task_uuid", "station_uuid", "workflow_uuid"))
            or resolution["local_task_uuid"] != inbox["local_task_uuid"]
            or resolution["payload_digest"] != package["payload_digest"]):
        raise _conflict("恢复回执不属于当前账本纪元、原 owner 或本地 Task")
    inspect_existing_task(connection, package, local_task_uuid=inbox["local_task_uuid"], allow_terminal=True)


def apply_reconciliation(connection: Connection, request_uuid: str) -> str:
    """在调用方事务内修复现存 Task 的收件绑定和事实，零 Task/Job 创建。"""
    if not connection.in_transaction:
        raise _conflict("恢复核实必须在工作流事务内提交")
    from unilabos.persistence.recovery_mode import require_execution

    require_execution(connection)
    row = connection.execute("SELECT * FROM center_reconciliation_request WHERE request_uuid=?", (request_uuid,)).fetchone()
    if row is None or row["receipt_json"] is None or row["package_json"] is None or row["reconciliation_uuid"] is None:
        raise _conflict("恢复核实尚无精确回执或冻结任务包")
    package = validate_task_package(_load(row["package_json"], label="核实任务包"))
    invocation = package["payload"]["station_invocation_uuid"]
    require_execution(connection, invocation_uuid=invocation, task_uuid=row["local_task_uuid"])
    if invocation != row["invocation_uuid"]:
        raise _conflict("核实记录与任务包调用不匹配")
    task = inspect_existing_task(connection, package, local_task_uuid=row["local_task_uuid"], allow_terminal=True)
    runtime = connection.execute("SELECT * FROM center_runtime_state WHERE singleton='runtime'").fetchone()
    if runtime is None:
        raise _conflict("恢复核实的本地身份账本缺失")
    existing = connection.execute("SELECT * FROM center_invocation_inbox WHERE invocation_uuid=?", (invocation,)).fetchone()
    if existing is not None:
        old = validate_task_package(_load(existing["package_json"], label="原任务包"))
        if (old["payload_digest"] != package["payload_digest"]
                or existing["local_task_uuid"] not in (None, task["uuid"])):
            raise _conflict("恢复不能覆盖已有调用包或本地 Task 绑定")
    else:
        connection.execute(
            "INSERT INTO center_invocation_inbox(invocation_uuid,package_json) VALUES (?,?)",
            (invocation, row["package_json"]),
        )
    connection.execute(
        "UPDATE center_invocation_inbox SET local_task_uuid=?,reconciliation_uuid=? WHERE invocation_uuid=?",
        (task["uuid"], row["reconciliation_uuid"], invocation),
    )
    inbox = connection.execute("SELECT * FROM center_invocation_inbox WHERE invocation_uuid=?", (invocation,)).fetchone()
    validate_reconciliation_binding(connection, inbox=inbox, package=package, runtime=runtime)
    recovered = recover_center_invocation(connection, invocation_uuid=invocation)
    if recovered != task["uuid"]:
        raise _conflict("恢复核实不得创建或更换现存 Task")
    connection.execute("UPDATE center_reconciliation_request SET applied=1 WHERE request_uuid=?", (request_uuid,))
    return task["uuid"]


def _submission(package: dict[str, Any]) -> Any:
    from unilabos.workflow.station_workflow_submission import (
        StationWorkflowSubmissionInvalid,
        prepare_station_workflow_submission,
    )

    payload = package["payload"]
    try:
        return prepare_station_workflow_submission(
            backend_task_uuid=payload["global_task_uuid"], invocation_key=payload["invocation_key"],
            workflow_name=None, workflow_id=payload["workflow_uuid"],
            revision_fingerprint=payload["revision_fingerprint"], input_value=payload["input"],
            priority=WorkflowTaskPriority.NORMAL, deadline=None, inventory_bindings=None, description=None, meta_data=None,
        )
    except StationWorkflowSubmissionInvalid as error:
        raise _conflict("中心调用不能构造固定的工作流请求身份") from error


def validate_claimed_submission(
    connection: Connection, invocation_uuid: str, *, workflow_uuid: str,
    revision_fingerprint: str | None, request_fingerprint: str,
) -> None:
    """允许历史冻结合同进入创建前，验证已领取意图与完整公共提交身份。"""
    from unilabos.persistence.recovery_mode import require_execution

    require_execution(connection, invocation_uuid=invocation_uuid)
    _inbox, package, _runtime = _intent(connection, invocation_uuid)
    submission = _submission(package)
    if (submission.workflow_id != workflow_uuid
            or submission.revision_fingerprint != revision_fingerprint
            or submission.request_fingerprint != request_fingerprint):
        raise _conflict("中心历史调用与已领取的冻结合同不匹配")


def material_input_task_uuid(connection: Connection, invocation_uuid: str, *, existing_terminal: bool = False) -> str | None:
    """物料调用必须采用库存先行预留的唯一 Task；在 Task 写事务中再次核实。"""
    from unilabos.center_protocol.material_input import validate_input_receipt

    inbox, package, _runtime = _intent(connection, invocation_uuid)
    if (inbox["local_task_uuid"] is None and table_exists(connection, "center_material_input_issue")
            and connection.execute("SELECT 1 FROM center_material_input_issue WHERE invocation_uuid=?",
                                   (invocation_uuid,)).fetchone() is not None):
        raise _conflict("原物料输入已报告异常，禁止新建 Task")
    from unilabos.center_protocol.material_source import requires_material_input
    if not requires_material_input(package["payload"]):
        return None
    row = (connection.execute("SELECT * FROM center_material_input WHERE invocation_uuid=?", (invocation_uuid,)).fetchone()
           if table_exists(connection, "center_material_input") else None)
    if row is None or (row["state"] != "claimed" and not (
        existing_terminal and row["state"] == "released" and inbox["terminal_recorded"]
    )):
        raise _conflict("中心物料调用尚无已领取的持久库存输入占用")
    try:
        receipt = validate_input_receipt(_load(row["receipt_json"], label="库存占用回执"), package)
    except ValueError as error:
        raise _conflict(str(error)) from error
    if receipt["local_task_uuid"] != row["local_task_uuid"]:
        raise _conflict("中心物料输入的固定任务身份发生变化")
    return receipt["local_task_uuid"]


def _append(connection: Connection, invocation_uuid: str, *, terminal: bool, body: dict[str, Any]) -> None:
    try:
        center_events.append_event_in_transaction(connection, invocation_uuid, terminal=terminal, body=body)
    except center_events.CenterEventConflict as error:
        raise _conflict(str(error)) from error


def _bind(connection: Connection, *, invocation_uuid: str, task_uuid: str) -> tuple[Any, dict[str, Any]]:
    inbox, package, _runtime = _intent(connection, invocation_uuid)
    _uuid(task_uuid)
    task = connection.execute("SELECT * FROM workflow_task WHERE uuid=?", (task_uuid,)).fetchone()
    if task is None:
        raise _conflict("中心调用关联的持久任务不存在")
    planned = material_input_task_uuid(connection, invocation_uuid,
        existing_terminal=task["status"] in ("succeeded", "failed", "canceled", "timeout"))
    if planned is not None and planned != task_uuid:
        raise _conflict("物料调用只能创建库存占用固定的同一本地 Task")
    submission = _submission(package)
    expected = {
        "backend_task_uuid": submission.backend_task_uuid, "invocation_key": submission.invocation_key,
        "request_fingerprint": submission.request_fingerprint, "workflow_uuid": submission.workflow_id,
        "revision_fingerprint": submission.revision_fingerprint,
    }
    if any(task[name] != value for name, value in expected.items()):
        raise _conflict("中心调用与持久任务的永久身份或冻结请求不匹配")
    if task["status"] not in ("pending", "running", "canceling", "succeeded", "failed", "canceled", "timeout"):
        raise _conflict("中心调用持久任务状态无效")
    if inbox["terminal_recorded"] and task["status"] not in ("succeeded", "failed", "canceled", "timeout"):
        raise _conflict("中心调用已有终态证据但任务状态回退")
    if inbox["local_task_uuid"] is not None and inbox["local_task_uuid"] != task_uuid:
        raise _conflict("同一中心调用不能更换本地任务")
    other = connection.execute(
        "SELECT invocation_uuid FROM center_invocation_inbox WHERE local_task_uuid=? AND invocation_uuid!=?",
        (task_uuid, invocation_uuid),
    ).fetchone()
    if other is not None:
        raise _conflict("同一本地任务不能关联多个中心调用")
    if inbox["acceptance_version"] == 1 and connection.execute(
        "SELECT 1 FROM center_boundary_outbox WHERE invocation_uuid=? AND phase=0", (invocation_uuid,),
    ).fetchone() is None:
        raise _conflict("中心调用已受理但原 accepted 事件缺失，禁止重建身份")
    accepted_at = _fact_time(task["create_time"])
    connection.execute(
        "UPDATE center_invocation_inbox SET local_task_uuid=?,acceptance_version=1 WHERE invocation_uuid=?",
        (task_uuid, invocation_uuid),
    )
    _append(connection, invocation_uuid, terminal=False, body={
        "event_uuid": str(uuid4()), "local_task_uuid": task_uuid, "event_type": "accepted",
        "payload": {"task_payload_digest": package["payload_digest"], "accepted_at": accepted_at,
                    "local_task_uuid": task_uuid},
        "occurred_at": accepted_at,
    })
    return task, package


def bind_center_invocation(connection: Connection, *, invocation_uuid: str, task_uuid: str) -> None:
    """在创建 Task 的现有事务内冻结调用映射和 accepted；失败回滚全部事实。"""
    _bind(connection, invocation_uuid=invocation_uuid, task_uuid=task_uuid)


def record_center_task_outcome(connection: Connection, *, task_uuid: str) -> None:
    """只从同事务真实终态与 workflow_result 形成 outcome；普通本地任务无操作。"""
    if not table_exists(connection, "center_invocation_inbox"):
        return
    rows = connection.execute(
        "SELECT invocation_uuid FROM center_invocation_inbox WHERE local_task_uuid=?", (task_uuid,),
    ).fetchall()
    if not rows:
        return
    if len(rows) != 1:
        raise _conflict("同一本地任务存在多个中心调用绑定")
    invocation_uuid = rows[0]["invocation_uuid"]
    task, package = _bind(connection, invocation_uuid=invocation_uuid, task_uuid=task_uuid)
    status = task["status"]
    if status not in ("succeeded", "failed", "canceled", "timeout"):
        return
    occurred_at = _fact_time(task["finished_at"])
    if status == "succeeded":
        result = connection.execute(
            "SELECT result FROM workflow_result WHERE workflow_task_uuid=?", (task_uuid,),
        ).fetchone()
        if result is None:
            raise _conflict("中心调用成功但缺少真实 workflow_result")
        output = _load(result["result"], label="工作流结果")
        if not isinstance(output, dict):
            raise _conflict("中心调用真实工作流结果不是对象")
    else:
        errors = _load(task["error_info"], label="任务错误事实")
        if not isinstance(errors, list):
            raise _conflict("中心调用任务错误事实不是数组")
        output = {"status": status, "error_info": errors}
        if status == "timeout":
            output["failure_code"] = "timeout"
    payload = {"task_payload_digest": package["payload_digest"],
               "status": "failed" if status == "timeout" else status, "output": output}
    previous = connection.execute(
        "SELECT event_json FROM center_boundary_outbox WHERE invocation_uuid=? AND phase=1", (invocation_uuid,),
    ).fetchone()
    # 已发布旧事件只能校对真实结果，不能补字段后重算历史摘要。
    if previous is not None:
        old = validate_event(_load(previous["event_json"], label="原终态事件"))
        old_output = canonical_json(old["payload"]["output"])
        try:
            raw_output = canonical_json(output)
        except DigestError:
            raw_output = None
        if old_output != raw_output and status == "succeeded":
            # 新事件公开 ResourceSlot，而旧事件可能保存完整本地值。两种既有
            # wire 都须能追溯真实结果；恢复只复用原值，不重写信封或摘要。
            graph = _load(task["workflow_snapshot"], label="冻结工作流图")
            try:
                projected = project_public_workflow_output(
                    graph["workflow"]["meta_data"]["unilab"]["output_contract"], output,
                )
                projected_output = canonical_json(projected)
            except (DigestError, ValueError, KeyError, TypeError):
                raise _conflict("原终态公开输出与冻结合同不一致") from None
            if old_output != projected_output:
                raise _conflict("原终态公开输出与真实工作流结果不一致")
            payload["output"] = old["payload"]["output"]
        if set(old["payload"]) == set(payload):
            _append(connection, invocation_uuid, terminal=True, body={
                "event_uuid": old["event_uuid"], "local_task_uuid": task_uuid,
                "event_type": "outcome", "payload": payload, "occurred_at": old["occurred_at"],
            })
            return
    if previous is None:
        try:
            if status == "succeeded":
                graph = _load(task["workflow_snapshot"], label="冻结工作流图")
                payload["output"] = project_public_workflow_output(
                    graph["workflow"]["meta_data"]["unilab"]["output_contract"], output,
                )
            canonical_json(payload["output"])
        except (DigestError, ValueError, KeyError, TypeError):
            # 本地可保存的精确数值不一定能进入中心 JCS 信封；保留完成事实，
            # 公开值投影失败也沿用待核实记录，不改结果或重派已执行动作。
            connection.execute(
                """INSERT INTO center_outcome_evidence(invocation_uuid,local_task_uuid,pending_reason)
                   VALUES (?,?,?) ON CONFLICT(invocation_uuid) DO UPDATE SET pending_reason=excluded.pending_reason""",
                (invocation_uuid, task_uuid, "output_not_exportable"),
            )
            return
    graph = _load(task["workflow_snapshot"], label="冻结工作流图")
    try:
        ports = ports_from_graph(graph)
        expected = output_material_ids(ports, output) if status == "succeeded" else set()
    except MaterialPortError:
        # 声明是 UUID 而设备实际返回了其他字符串时保留物理事实，等待人工修订结果。
        # 不能因导出失败回滚完成记录，让已执行动作被调度器再次执行。
        connection.execute(
            """INSERT INTO center_outcome_evidence(invocation_uuid,local_task_uuid,pending_reason)
               VALUES (?,?,?) ON CONFLICT(invocation_uuid) DO UPDATE SET pending_reason=excluded.pending_reason""",
            (invocation_uuid, task_uuid, "material_output_invalid"),
        )
        return
    evidence = connection.execute(
        "SELECT * FROM center_outcome_evidence WHERE invocation_uuid=?", (invocation_uuid,),
    ).fetchone()
    if evidence is not None and dict(evidence).get('cancel_settlement_json'):
        # 取消历史结算与普通物料结果相互独立，不能再生成正常出站结果。
        return
    if status == "succeeded" and any(port["direction"] == "output" for port in ports) and evidence is None:
        # 物理完成事实继续提交；等待补报只阻止中心 outcome，不回滚 Job 或重新派发。
        connection.execute(
            "INSERT INTO center_outcome_evidence(invocation_uuid,local_task_uuid,pending_reason) VALUES (?,?,?)",
            (invocation_uuid, task_uuid, "material_evidence_required"),
        )
        return
    if evidence is not None and evidence["evidence_json"] is None:
        return
    refs = (_load(evidence["evidence_json"], label="结果引用证据") if evidence is not None else
            {"material_refs": [], "container_refs": [], "artifact_refs": []})
    refs = validate_outcome_refs(refs, station_uuid=package["payload"]["station_uuid"])
    if {ref["local_material_uuid"] for ref in refs["material_refs"]} != expected:
        raise _conflict("结果物料证据与冻结输出端口不匹配")
    payload.update(refs)
    payload.update(started_at=_fact_time(task["started_at"]) if task["started_at"] is not None else None,
                   finished_at=occurred_at)
    _append(connection, invocation_uuid, terminal=True, body={
        "event_uuid": str(uuid4()), "local_task_uuid": task_uuid, "event_type": "outcome",
        "payload": payload,
        "occurred_at": occurred_at,
    })


def record_center_outcome_evidence(connection: Connection, *, invocation_uuid: str, task_uuid: str,
                                   owner_instance_uuid: str, task_payload_digest: str,
                                   evidence: dict[str, Any]) -> None:
    """仅工站受信任集成调用：真实终态之后补齐一次证据，与 outcome 同事务冻结。

    调用者提供库存/制品权威已经落盘的公开引用；本模块不跨库偷读库存、不生成谱系。
    这里不是网络认证入口，实例参数用于防止迟到适配器误写其他 owner 的任务。
    """
    inbox, package, runtime = _intent(connection, invocation_uuid)
    if (runtime["instance_uuid"] != owner_instance_uuid or package["payload_digest"] != task_payload_digest
            or inbox["local_task_uuid"] != task_uuid):
        raise _conflict("结果证据的 owner、调用、任务或摘要不匹配")
    task, _ = _bind(connection, invocation_uuid=invocation_uuid, task_uuid=task_uuid)
    if task["status"] not in ("succeeded", "failed", "canceled", "timeout"):
        raise _conflict("结果证据只能绑定已持久终态 Task")
    try:
        refs = validate_outcome_refs(evidence, station_uuid=runtime["station_uuid"])
        graph = _load(task["workflow_snapshot"], label="冻结工作流图")
        output = connection.execute("SELECT result FROM workflow_result WHERE workflow_task_uuid=?", (task_uuid,)).fetchone()
        expected = (output_material_ids(ports_from_graph(graph), _load(output["result"], label="真实结果"))
                    if task["status"] == "succeeded" and output is not None else set())
        if {ref["local_material_uuid"] for ref in refs["material_refs"]} != expected:
            raise MaterialPortError("结果物料证据与冻结输出端口不匹配")
    except (EventEnvelopeError, MaterialPortError) as error:
        raise _conflict(str(error)) from error
    row = connection.execute("SELECT * FROM center_outcome_evidence WHERE invocation_uuid=?", (invocation_uuid,)).fetchone()
    if row is not None and row["evidence_json"] is not None:
        if canonical_json(_load(row["evidence_json"], label="原结果证据")) != canonical_json(refs):
            raise _conflict("同一调用的结果引用证据已冻结，禁止改写")
    else:
        if inbox["terminal_recorded"]:
            raise _conflict("已发出终态的调用禁止事后添加引用证据")
        if row is None:
            connection.execute("INSERT INTO center_outcome_evidence(invocation_uuid,local_task_uuid) VALUES (?,?)",
                               (invocation_uuid, task_uuid))
        connection.execute("UPDATE center_outcome_evidence SET evidence_json=?,pending_reason=NULL WHERE invocation_uuid=?",
                           (json.dumps(refs, allow_nan=False, ensure_ascii=False), invocation_uuid))
    record_center_task_outcome(connection, task_uuid=task_uuid)


def recover_center_invocation(connection: Connection, *, invocation_uuid: str) -> str | None:
    """按永久调用身份找回已有 Task（含软删除）；只绑定与补证据，不创建或调度。"""
    inbox, package, _runtime = _intent(connection, invocation_uuid)
    submission = _submission(package)
    tasks = connection.execute(
        "SELECT uuid FROM workflow_task WHERE backend_task_uuid=? AND invocation_key=?",
        (submission.backend_task_uuid, submission.invocation_key),
    ).fetchall()
    if not tasks:
        if inbox["local_task_uuid"] is not None:
            raise _conflict("中心调用已绑定的永久任务事实缺失")
        return None
    if len(tasks) != 1:
        raise _conflict("中心调用的永久身份对应多个任务")
    task_uuid = tasks[0]["uuid"]
    bind_center_invocation(connection, invocation_uuid=invocation_uuid, task_uuid=task_uuid)
    record_center_task_outcome(connection, task_uuid=task_uuid)
    return task_uuid


def list_material_exports(connection: Connection, *, limit: int = 100, after_invocation_uuid: str = "") -> list[dict[str, Any]]:
    """在一个工作流事务中校验真实 Task、已领取包和冻结端口，再输出最小公共事实。"""
    from unilabos.workflow.material_ports import output_material_items

    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("物料证据导出批量必须为 1 至 100")
    if after_invocation_uuid:
        _uuid(after_invocation_uuid)
    if not table_exists(connection, "center_outcome_evidence"):
        return []
    rows = connection.execute("""SELECT e.invocation_uuid FROM center_outcome_evidence e
        JOIN center_invocation_inbox i ON i.invocation_uuid=e.invocation_uuid
        WHERE i.terminal_recorded=0 AND e.evidence_json IS NULL AND e.pending_reason='material_evidence_required' AND e.cancel_settlement_json IS NULL
          AND e.invocation_uuid>? ORDER BY e.invocation_uuid LIMIT ?""", (after_invocation_uuid, limit)).fetchall()
    result = []
    for row in rows:
        invocation = row["invocation_uuid"]
        inbox, package, runtime = _intent(connection, invocation)
        task, _ = _bind(connection, invocation_uuid=invocation, task_uuid=inbox["local_task_uuid"])
        if task["status"] != "succeeded":
            continue
        output = connection.execute("SELECT result FROM workflow_result WHERE workflow_task_uuid=?", (task["uuid"],)).fetchone()
        if output is None:
            raise _conflict("物料出口缺少真实 workflow_result")
        graph = _load(task["workflow_snapshot"], label="冻结工作流图")
        try:
            outputs = output_material_items(ports_from_graph(graph), _load(output["result"], label="真实结果"))
        except MaterialPortError:
            # 老导出路径曾把重复 UUID 去重；升级扫描时隔离这条真实终态，
            # 保留物理事实，不能使一个坏输出阻塞其他 Task 或已有库存补报意图。
            connection.execute(
                "UPDATE center_outcome_evidence SET pending_reason='material_output_invalid' WHERE invocation_uuid=? AND evidence_json IS NULL",
                (invocation,),
            )
            continue
        result.append({"invocation_uuid": invocation, "local_task_uuid": task["uuid"],
                       "station_uuid": runtime["station_uuid"], "owner_instance_uuid": runtime["instance_uuid"],
                       "event_epoch_uuid": runtime["event_epoch_uuid"], "task_payload_digest": package["payload_digest"],
                       "input_material_refs": package["payload"]["material_refs"], "outputs": outputs})
    return result
