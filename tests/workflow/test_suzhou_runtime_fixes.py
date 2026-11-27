"""苏州补料回归：建任务、旧式完成通道、取消与运行时来源重试的真实副作用。

全部经公开 HTTP、生产 WorkflowService / TaskSchedulerBridge / EdgeScheduler 与库存权威
执行，仅设备派发边界使用 RecordingDispatcher。覆盖：

- 建任务在事务内做调度编译：编译失败整笔回滚，不留下 pending Task/Job；
- 建任务失败透传计划层具体原因与错误码（如 material_shortage_unhandled）；
- 旧式完成回调（``on_job_finished`` / ``POST /jobs/{id}/finish``）同样结算声明用量；
- 结果未知的作业收到“未发送”取消证明时不释放占用；
- 补料分支给不出唯一消费容器时，派发前补做预留失败让节点失败，不越过设备边界；
- 运行时来源正式重试后，库存来源绑定记录当前 attempt；
- 运行时来源的匹配物料只是被其他任务占用时等待（task_material_claimed），不走补料分支；
- unload 等待其他任务仍要使用被卸物料的未完成作业；对方只是同样卸它时不等待。
"""

from __future__ import annotations

import json
import math
from contextlib import ExitStack
from copy import deepcopy
from typing import Any
from uuid import uuid4

import pytest

from tests.workflow.test_f05_material_source_direct_graph import _projection_handle
from tests.workflow.test_quantity_runtime_fixes import (
    HEADER,
    Harness,
    act,
    cond,
    h,  # noqa: F401  生产 HTTP 运行时夹具
    nid,
    src,
    sz48_graph,
)
from unilabos.app.scheduler.dispatch import CommittedJobOutcome
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.workflow.models import (
    WorkflowEdgeWrite,
    WorkflowInventoryRequirementWrite,
    WorkflowNodeWrite,
)
from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge
from unilabos.workflow.template_projection_store import RegistryTemplateProjectionStore


def _rows(h: Harness, table: str) -> list[dict[str, Any]]:
    with h.rt.workflow_store.transaction() as connection:
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table}").fetchall()]


def _install_without_task(h: Harness, body: str) -> str:
    """与 ``Harness.install`` 相同地编译并保存图，但不创建任务。"""

    wf = h.api("POST", "/workflows", {"name": "苏州回归", "tags": [], "meta_data": {}})["uuid"]
    text = HEADER.replace("WORKFLOW_UUID", wf) + body + "    return workflow_output()\n"
    applied = h.api("GET", f"/workflows/{wf}/graph")
    result = h.engine().compile(workflow_uuid=wf, workflow_revision=applied["workflow"]["revision"],
                                python_source=text, source_uri="memory://suzhou.py", applied_graph=applied)
    assert result.valid, result.diagnostics
    graph = deepcopy(result.graph)
    templates = {t["uuid"]: t for t in graph["node_templates"]}
    RegistryTemplateProjectionStore(h.rt.workflow_store).replace(
        authority_id="suzhou-runtime-fixes", node_templates=graph["node_templates"],
        handle_templates=[_projection_handle(
            handle, resource_template_uuid=templates[handle["workflow_node_template_uuid"]]["resource_template_uuid"],
            action_name=templates[handle["workflow_node_template_uuid"]]["name"]) for handle in graph["handle_templates"]])
    for node in graph["nodes"]:
        if node.get("action_name") == "process_plate":
            node["material_uuid"] = h.d["device"]
            node.setdefault("meta_data", {}).setdefault("unilab", {})["executor_binding"] = {
                "mode": "fixed", "device_id": "reactor-a"}
    h.rt.workflow_store.save_graph(
        wf, revision=applied["workflow"]["revision"],
        nodes=[WorkflowNodeWrite.model_validate(n) for n in graph["nodes"]],
        edges=[WorkflowEdgeWrite.model_validate(e) for e in graph["edges"]],
        inventory_requirements=[WorkflowInventoryRequirementWrite.model_validate(r)
                                for r in graph.get("inventory_requirements") or []],
        workflow_meta_data=graph["workflow"].get("meta_data", {}),
    )
    h.wf = wf
    return wf


def _post_task(h: Harness) -> dict[str, Any]:
    response = h.rt.client.post("/api/v1/workflow-tasks", json={"workflow_uuid": h.wf, "run_mode": "normal"})
    return response.json()


def _assert_no_task_side_effects(h: Harness) -> None:
    assert _rows(h, "workflow_task") == []
    assert _rows(h, "workflow_node_job") == []
    assert h.rt.dispatcher.dispatched == []
    assert h.rt.inventory.store.query_all("SELECT * FROM inventory_material_source_binding") == []
    assert h.rt.inventory.store.query_all("SELECT * FROM inventory_reservation WHERE status='active'") == []


# ---------------------------------------------------------------- 6. 建任务

def test_scheduler_compilation_failure_rolls_back_task_creation(h: Harness, monkeypatch) -> None:
    """调度编译在建任务事务内执行：失败时不留下永远 pending 的 Task/Job，并返回具体原因。"""

    from unilabos.workflow import workflow_spec_compiler
    from unilabos.workflow._workflow_spec_snapshot import WorkflowSpecCompilationError

    h.bottle("WORK", "WORK", 5.0)
    _install_without_task(h, src("plate", "plate", h.d["WORK"]) + act("used", "used", "plate"))

    def reject(self, task, jobs, *, inventory_admission):
        raise WorkflowSpecCompilationError("invalid_task_snapshot", "注入：冻结快照不能转换为调度输入")

    monkeypatch.setattr(workflow_spec_compiler.WorkflowSpecCompiler, "_compile", reject)
    body = _post_task(h)
    assert body["code"] != 0, body
    assert "注入：冻结快照不能转换为调度输入" in body["error"]["msg"], body
    _assert_no_task_side_effects(h)
    monkeypatch.undo()
    # 编译恢复后同一工作流可正常建任务，证明失败没有留下残留占用。
    created = _post_task(h)
    assert created["code"] == 0, created
    assert len(_rows(h, "workflow_task")) == 1


def test_task_creation_reports_plan_code_and_reason_for_legacy_saved_graph(h: Harness, monkeypatch) -> None:
    """旧版本已保存的不可执行图：建任务返回计划层错误码与具体原因，且零写入。"""

    import unilabos.workflow.graph_executability as graph_executability

    h.bottle("WORK", "WORK", 5.0)
    body = (src("original", "original", h.d["WORK"], support=True) + cond("cond", "original.sufficient")
            + act("used", "used", "original", 2) + "    else:\n"
            + src("child", "child", h.d["WORK"], level=2).replace(
                f'mount=resource_ref("{h.d["WORK"]}")', "mount=original")
            + act("alt", "alt", "child", 2))
    # 模拟修复前已持久化：编译与保存的可执行性检查失效。
    monkeypatch.setattr(graph_executability, "assert_graph_executable", lambda *a, **k: None)
    _install_without_task(h, body)
    monkeypatch.undo()
    response = _post_task(h)
    assert response["code"] == 1000, response
    assert "sufficient=true 分支作为挂载位" in response["error"]["msg"], response
    _assert_no_task_side_effects(h)


# ---------------------------------------------------------------- 9. 旧式完成通道结算

def _dose_graph(h: Harness) -> str:
    return (src("bottle", "bottle", h.d["WORK"], site=h.site("WORK")) + act("dosed", "dosed", "bottle")
            + '    quantity_requirement(requirement_key="dose", source=bottle, consume=dosed, '
            'quantity=0.1, quantity_unit="mL")\n')


@pytest.mark.parametrize("channel", ["scheduler_callback", "public_finish_route"])
def test_legacy_finish_settles_declared_quantity(h: Harness, channel: str) -> None:
    """旧四参数完成回调与公开 ``/jobs/{id}/finish`` 同样结算声明用量，不能当作释放。"""

    work = h.bottle("WORK", "WORK", 5.0)
    h.install(_dose_graph(h))
    h.pump(2)
    dosed = h.job("dosed")
    assert [c["job_id"] for c in h.rt.dispatcher.dispatched] == [dosed["uuid"]]
    if channel == "scheduler_callback":
        h.rt.scheduler.on_job_finished(dosed["uuid"], True, {})
    else:
        response = h.rt.client.post(f"/api/v1/jobs/{dosed['uuid']}/finish",
                                    json={"success": True, "ret_value": {}, "suc_type": "normal"})
        assert response.status_code == 200, response.text
    h.pump(2)
    assert h.api("GET", f"/workflow-tasks/{h.task}")["status"] == "succeeded"
    consumptions = [c for c in h.consumptions() if c["workflow_node_job_uuid"] == dosed["uuid"]]
    assert len(consumptions) == 1, h.consumptions()
    assert math.isclose(consumptions[0]["actual_quantity"], 0.1)
    assert consumptions[0]["inventory_uuid"] == work["content"]["uuid"]
    assert math.isclose(h.qty(work), 4.9, abs_tol=1e-9)
    assert [a["status"] for a in h.allocations()] == ["consumed"]
    assert h.active_quantity_reservations() == []


# ---------------------------------------------------------------- 9. 取消与结果未知

def test_no_send_cancel_proof_does_not_release_job_with_unknown_outcome(h: Harness) -> None:
    """已收到结果未知的作业不可能“未发送”：取消时保留来源占用，作业停在 cancel_requested。"""

    h.bottle("WORK", "WORK", 5.0)
    h.install(src("plate", "plate", h.d["WORK"]) + act("used", "used", "plate"))
    h.pump(2)
    used = h.job("used")
    assert [c["job_id"] for c in h.rt.dispatcher.dispatched] == [used["uuid"]]
    h.rt.scheduler.on_job_outcome(used["uuid"], CommittedJobOutcome(
        outcome="failed", return_info={}, error_info=[], unknown_command_ids=[str(uuid4())]))
    assert h.job("used")["uncertainty_reason"]
    held = h.rt.inventory.store.query_all(
        "SELECT material_uuid FROM inventory_material_source_binding WHERE status='active'")
    assert len(held) == 1
    material = held[0]["material_uuid"]
    response = h.rt.client.post(f"/api/v1/workflow-tasks/{h.task}/commands",
                                json={"type": "cancel", "idempotency_key": str(uuid4())})
    assert response.status_code < 500, response.text
    h.pump(3)
    job = h.job("used")
    assert job["status"] == "cancel_requested", job
    assert job["uncertainty_reason"]
    assert h.api("GET", f"/workflow-tasks/{h.task}")["status"] != "canceled"
    assert h.rt.inventory.store.query_all(
        "SELECT material_uuid FROM inventory_material_source_binding WHERE status='active'") == held
    instance = h.rt.inventory.store.query_one("SELECT status FROM material_instance WHERE edge_uuid=?", (material,))
    assert instance["status"] == "reserved"
    assert len(h.rt.dispatcher.dispatched) == 1


# ---------------------------------------------------------------- 9. 派发前补做补料分支预留

def test_shortage_branch_consumer_without_unique_container_fails_before_dispatch(h: Harness) -> None:
    """缺料分支给不出唯一消费容器：派发前补做预留失败，本节点失败，设备不动、库存不变。"""

    work = h.bottle("WORK", "WORK", 0.05)
    other = h.bottle("OTHER", "OTHER", 5.0)
    h.bottle("TARGET", "TARGET")
    h.install(sz48_graph(h.d, ambiguous_else=True))
    h.run(hold=frozenset({nid("dosed")}))
    assert h.job("cond")["return_info"]["selected_branch"] == "else"
    dosed = h.job("dosed")
    assert dosed["uuid"] not in [c["job_id"] for c in h.rt.dispatcher.dispatched]
    assert dosed["status"] == "failed", dosed
    assert "无法从实际执行的补料分支唯一确定" in str(dosed["error_info"])
    errors = h.api("GET", "/stations/local/error-handling")["errors"]
    assert [e["source_job_uuid"] for e in errors] == [dosed["uuid"]]
    assert h.consumptions() == []
    assert h.allocations() == [] and h.reservations() == []
    assert math.isclose(h.qty(work), 1.05) and math.isclose(h.qty(other), 5.0)


# ---------------------------------------------------------------- 11. 运行时来源重试的 attempt

def test_runtime_source_retry_binding_records_current_attempt(h: Harness, monkeypatch) -> None:
    """运行时来源首个尝试准入异常、正式重试后：来源绑定记录 attempt=2，不复用 attempt=1。"""

    h.bottle("WORK", "WORK", 5.0)
    real = h.rt.inventory.admit_task_materials
    calls: list[int] = []

    def flaky(task_uuid, requests, quantities, **options):
        calls.append(int(options.get("attempt", 1)))
        if len(calls) == 1:
            raise RuntimeError("库存查询超时（注入）")
        return real(task_uuid, requests, quantities, **options)

    monkeypatch.setattr(h.rt.inventory, "admit_task_materials", flaky)
    h.install(src("original", "original", h.d["WORK"], support=True) + cond("cond", "original.sufficient")
              + act("used", "used", "original", 2) + "    else:\n"
              + src("spare", "spare", h.d["SPARE"], level=2) + act("alt", "alt", "spare", 2))
    h.pump(2)
    failed = h.job("original")
    assert failed["attempt"] == 1 and failed["status"] == "failed", failed
    h.decision_retry(failed["uuid"])
    retried = h.job("original")
    assert retried["attempt"] == 2 and retried["status"] == "succeeded", retried
    assert retried["return_info"]["sufficient"] is True
    bindings = h.rt.inventory.store.query_all(
        "SELECT node_id, attempt, status FROM inventory_material_source_binding WHERE workflow_id=?", (h.task,))
    assert [(b["node_id"], b["attempt"]) for b in bindings] == [(nid("original"), 2)], bindings
    assert calls[-1] == 2


# ---------------------------------------------------------------- 跨任务占用：等待而非缺料

def _jobs(h: Harness, task_uuid: str) -> dict[str, dict[str, Any]]:
    return {job["workflow_node_uuid"]: job for job in h.api("GET", f"/workflow-tasks/{task_uuid}/jobs")}


def _deleted_at(h: Harness, material_uuid: str) -> Any:
    return h.rt.inventory.store.query_one("SELECT deleted_at FROM material WHERE uuid=?", (material_uuid,))["deleted_at"]


def _active_bindings(h: Harness, task_uuid: str) -> list[str]:
    return [row["material_uuid"] for row in h.rt.inventory.store.query_all(
        "SELECT material_uuid FROM inventory_material_source_binding WHERE workflow_id=? AND status='active'",
        (task_uuid,))]


def _unload(h: Harness, material_uuid: str, label: str) -> str:
    return (f"    # unilab:node_uuid={nid(label)}\n"
            f'    {label}_gone = material_io(mode="unload", resource_template=None, mount=resource_ref("{h.d["WORK"]}"), '
            f'site="T1", material=resource_ref("{material_uuid}"), disposition="soft_delete", '
            'scope="material_with_contents", timeout_seconds=600)\n')


def _confirmation(job: dict[str, Any]) -> str | None:
    return (job.get("manual_confirmation") or {}).get("status")


def test_runtime_source_waits_while_material_claimed_by_other_task_then_resumes(h: Harness) -> None:
    """匹配物料只是被其他任务占用：来源保持 pending 并写明原因，不输出 sufficient、不进补料分支；
    对方释放后下一轮重评，取得同一物料并走有料分支。"""

    work = h.bottle("WORK", "WORK", 5.0)["material"]
    spare = h.bottle("SPARE", "SPARE", 5.0)["material"]
    holder = h.install(src("plate", "plate", h.d["WORK"]) + act("used_a", "used_a", "plate"))
    h.pump(2)
    held = h.job("used_a")["uuid"]
    assert [c["job_id"] for c in h.rt.dispatcher.dispatched] == [held]
    waiter = h.install(src("original", "original", h.d["WORK"], support=True) + cond("cond", "original.sufficient")
                       + act("used", "used", "original", 2) + "    else:\n"
                       + src("spare", "spare", h.d["SPARE"], level=2) + act("alt", "alt", "spare", 2))
    h.pump(3)
    jobs = _jobs(h, waiter)
    source = jobs[nid("original")]
    assert source["status"] == "pending", source
    assert source["wait_reason"]["code"] == "task_material_claimed", source
    assert "sufficient" not in (source.get("return_info") or {})
    assert all(jobs[nid(label)]["status"] == "pending" for label in ("cond", "used", "spare", "alt"))
    waiting_since = source["wait_reason"]["waiting_since"]
    h.pump(3)
    # 原因只写一次：重复重排不改写等待事实，也不派发或绑定任何物料。
    assert _jobs(h, waiter)[nid("original")]["wait_reason"]["waiting_since"] == waiting_since
    assert [c["job_id"] for c in h.rt.dispatcher.dispatched] == [held]
    assert _active_bindings(h, waiter) == []
    assert _active_bindings(h, holder) == [work]
    assert _deleted_at(h, work) is None and _deleted_at(h, spare) is None

    h.outcome(held)
    h.pump(3)
    assert h.api("GET", f"/workflow-tasks/{holder}")["status"] == "succeeded"
    jobs = _jobs(h, waiter)
    source = jobs[nid("original")]
    assert source["status"] == "succeeded", source
    assert source["return_info"]["sufficient"] is True
    assert source["return_info"]["material"]["uuid"] == work
    assert jobs[nid("cond")]["return_info"]["selected_branch"] == "if"
    used = jobs[nid("used")]["uuid"]
    assert [c["job_id"] for c in h.rt.dispatcher.dispatched] == [held, used]
    h.outcome(used)
    h.pump(3)
    assert h.api("GET", f"/workflow-tasks/{waiter}")["status"] == "succeeded"
    assert _jobs(h, waiter)[nid("spare")]["status"] == "skipped"


def test_unload_waits_for_other_task_unfinished_use_then_opens_confirmation(h: Harness) -> None:
    """其他任务以 SHARED_SOURCE 绑定了物料、使用它的作业还未执行（无执行租约）：卸料等待
    （不开确认、不删除、不派发）；对方用完后卸料正常进入确认并卸下。"""

    work = h.bottle("WORK", "WORK", 5.0)["material"]
    h.bottle("TARGET", "TARGET", 1.0)
    h.bottle("OTHER", "OTHER", 1.0)
    h.install(src("busy", "busy", h.d["TARGET"]) + act("busy_use", "busy_use", "busy"))
    h.pump(2)
    busy = h.job("busy_use")["uuid"]
    user = h.install(src("other", "other", h.d["OTHER"]) + act("prep", "prep", "other")
                     + src("plate", "plate", h.d["WORK"]).replace("TASK_EXCLUSIVE", "SHARED_SOURCE")
                     + act("used_a", "used_a", "plate"))
    h.pump(2)
    prep = h.job("prep")["uuid"]
    queued = h.job("used_a")
    # 对方使用该物料的作业还在等上游，没有任何执行租约，只有参数引用了它。
    assert queued["status"] == "pending" and not queued.get("wait_reason")
    assert work in json.dumps(queued["param"])
    unloader = h.install(_unload(h, work, "unload_b"))
    h.pump(3)
    unload = _jobs(h, unloader)[nid("unload_b")]
    assert unload["status"] == "pending", unload
    assert unload["wait_reason"]["code"] == "task_material_claimed", unload
    assert _confirmation(unload) is None
    assert _deleted_at(h, work) is None
    assert [c["job_id"] for c in h.rt.dispatcher.dispatched] == [busy]

    h.outcome(busy)
    h.pump(2)
    h.outcome(prep)
    h.pump(2)
    assert [c["job_id"] for c in h.rt.dispatcher.dispatched] == [busy, prep, queued["uuid"]]
    assert _confirmation(_jobs(h, unloader)[nid("unload_b")]) is None
    assert _deleted_at(h, work) is None
    h.outcome(queued["uuid"])
    h.pump(3)
    assert h.api("GET", f"/workflow-tasks/{user}")["status"] == "succeeded"
    unload = _jobs(h, unloader)[nid("unload_b")]
    assert unload["status"] == "running" and _confirmation(unload) == "pending", unload
    h.api("POST", f"/workflow-node-jobs/{unload['uuid']}/manual-confirmation", {"action": "approve"})
    h.pump(2)
    assert h.api("GET", f"/workflow-tasks/{unloader}")["status"] == "succeeded"
    assert _deleted_at(h, work) is not None
    assert len(h.rt.dispatcher.dispatched) == 3


def test_serial_unload_allows_queued_material_user_to_finish_first(h: Harness) -> None:
    h.rt.scheduler._serial_queue = True
    work = h.bottle("WORK", "WORK", 5.0)["material"]
    h.bottle("TARGET", "TARGET", 1.0)
    h.install(src("busy", "busy", h.d["TARGET"]) + act("busy_use", "busy_use", "busy"))
    busy = h.job("busy_use")["uuid"]
    unloader = h.install(_unload(h, work, "unload_first"))
    user = h.install(
        src("plate", "plate", h.d["WORK"]).replace("TASK_EXCLUSIVE", "SHARED_SOURCE")
        + act("used_later", "used_later", "plate")
    )
    use = _jobs(h, user)[nid("used_later")]
    assert _active_bindings(h, user) == []
    assert [item["job_id"] for item in h.rt.dispatcher.dispatched] == [busy]

    h.outcome(busy)
    h.pump(2)
    unload = _jobs(h, unloader)[nid("unload_first")]
    assert unload["wait_reason"]["code"] == "task_material_claimed"
    assert _confirmation(unload) is None
    assert [item["job_id"] for item in h.rt.dispatcher.dispatched] == [busy, use["uuid"]]
    assert work in json.dumps(_jobs(h, user)[nid("used_later")]["param"])

    h.outcome(use["uuid"])
    h.pump(2)
    unload = _jobs(h, unloader)[nid("unload_first")]
    assert _confirmation(unload) == "pending"
    h.api("POST", f"/workflow-node-jobs/{unload['uuid']}/manual-confirmation", {"action": "approve"})
    assert h.api("GET", f"/workflow-tasks/{unloader}")["status"] == "succeeded"


@pytest.mark.parametrize("scenario", ["fresh", "recover_queued", "recover_legacy_claim"])
def test_serial_admission_leaves_stock_for_the_head_runtime_source(
    h: Harness, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    h.rt.scheduler._serial_queue = True
    h.bottle("TARGET", "TARGET", 1.0)
    work = h.bottle("WORK", "WORK", 1.0)["material"]
    h.bottle("SPARE", "SPARE", 1.0)
    head = h.install(
        src("prepare", "prepare", h.d["TARGET"]) + act("pre", "pre", "prepare")
        + src("original", "original", h.d["WORK"], support=True)
        + cond("check", "original.sufficient") + act("use", "use", "original", 2)
        + "    else:\n" + src("spare", "spare", h.d["SPARE"], level=2)
        + act("fallback", "fallback", "spare", 2)
    )
    legacy = scenario == "recover_legacy_claim"
    # 模拟旧开源版本在入队前已经写下的持久绑定。
    with monkeypatch.context() as old_release:
        if legacy:
            old_release.setattr(EdgeScheduler, "serial_queue_enabled", property(lambda _: False))
        later = h.install(src("held", "held", h.d["WORK"]) + act("use_later", "use_later", "held"))
    assert _active_bindings(h, later) == ([work] if legacy else [])
    assert h.api("GET", f"/workflow-tasks/{later}")["status"] == "pending"
    head_jobs = _jobs(h, head)
    pre_job = head_jobs[nid("pre")]["uuid"]
    later_job = _jobs(h, later)[nid("use_later")]["uuid"]
    with ExitStack() as cleanup:
        if scenario != "fresh":
            h.rt.scheduler.close()
            scheduler = EdgeScheduler(
                dispatcher=h.rt.dispatcher, inventory=h.rt.inventory, serial_queue=True,
            )
            bridge = TaskSchedulerBridge(h.rt.workflow_store, scheduler=scheduler)
            cleanup.callback(bridge.close)
            cleanup.callback(scheduler.close)
            monkeypatch.setattr(h.rt, "scheduler", scheduler)
            bridge.recover_active_tasks()
            assert [job["job_id"] for job in h.rt.dispatcher.dispatched] == [pre_job]

        h.outcome(pre_job)
        h.pump(2)
        if legacy:
            # 既有占用由原任务正常结算，队列不重放前置作业，也不抢先释放占用。
            assert [job["job_id"] for job in h.rt.dispatcher.dispatched] == [pre_job, later_job]
            h.outcome(later_job)
            h.pump(2)
        else:
            assert _active_bindings(h, later) == []
        assert _jobs(h, head)[nid("original")]["status"] == "succeeded"
        h.outcome(head_jobs[nid("use")]["uuid"])
        h.pump(2)
        assert h.api("GET", f"/workflow-tasks/{head}")["status"] == "succeeded"
        if not legacy:
            assert _active_bindings(h, later) == [work]
            assert _jobs(h, later)[nid("use_later")]["status"] == "running"


def test_serial_queue_resumes_admission_after_drain_and_waiting_task_cancel(h: Harness) -> None:
    h.rt.scheduler._serial_queue = True
    work = h.bottle("WORK", "WORK", 1.0)["material"]
    h.rt.scheduler.begin_drain()
    head = h.install(src("stock", "first_source", h.d["WORK"]) + act("first", "first", "stock"))
    later = h.install(src("stock", "later_source", h.d["WORK"]) + act("later", "later", "stock"))

    assert h.rt.dispatcher.dispatched == []
    assert _active_bindings(h, head) == _active_bindings(h, later) == []
    h.api("POST", f"/workflow-tasks/{head}/commands", {"type": "cancel", "idempotency_key": str(uuid4())})
    assert h.api("GET", f"/workflow-tasks/{head}")["status"] == "canceled"
    assert _active_bindings(h, later) == []

    h.rt.scheduler.resume_from_drain()
    assert _active_bindings(h, later) == [work]
    assert _jobs(h, later)[nid("later")]["status"] == "running"


def test_two_tasks_only_unloading_same_material_do_not_wait_for_each_other(h: Harness) -> None:
    """对照：另一任务未完成的作业只是同样卸下这件物料，不算“使用”，卸料直接进入确认。"""

    work = h.bottle("WORK", "WORK", 5.0)["material"]
    h.bottle("TARGET", "TARGET", 1.0)
    h.bottle("OTHER", "OTHER", 1.0)
    h.install(src("busy", "busy", h.d["TARGET"]) + act("busy_use", "busy_use", "busy"))
    h.pump(2)
    first = h.install(src("other", "other", h.d["OTHER"]) + act("prep", "prep", "other") + _unload(h, work, "unload_a"))
    h.pump(2)
    first_unload = _jobs(h, first)[nid("unload_a")]
    assert first_unload["status"] == "pending" and work in json.dumps(first_unload["param"])
    second = h.install(_unload(h, work, "unload_b"))
    h.pump(3)
    unload = _jobs(h, second)[nid("unload_b")]
    assert unload["status"] == "running" and _confirmation(unload) == "pending", unload
    assert (unload.get("wait_reason") or {}).get("code") != "task_material_claimed"
    h.api("POST", f"/workflow-node-jobs/{unload['uuid']}/manual-confirmation", {"action": "approve"})
    h.pump(2)
    assert h.api("GET", f"/workflow-tasks/{second}")["status"] == "succeeded"
    assert _deleted_at(h, work) is not None
