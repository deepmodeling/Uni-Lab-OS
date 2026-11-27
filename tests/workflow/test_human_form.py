"""人工多字段判定经过真实编译、循环、持久回复和恢复边界。"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from fastapi.testclient import TestClient

from tests.scheduler_core.conftest import ManualClock
from tests.workflow.test_authoring_engine import WORKFLOW_UUID, _compile, _handle, _template
from tests.workflow.test_inventory_native_command import engine
from unilabos.app.scheduler.dispatch import RecordingDispatcher
from unilabos.app.scheduler.service import EdgeScheduler
from unilabos.app.workflow_api import create_workflow_app
from unilabos.workflow.authoring_engine import WorkflowAuthoringEngine
from unilabos.workflow.authoring_kernel import AuthoringCatalogSnapshot
from unilabos.workflow.execution_plan import ExecutionPlanBuilder
from unilabos.workflow.human_form import normalize_form_fields, validate_form_answers
from unilabos.workflow.service import WorkflowService
from unilabos.workflow.store import WorkflowStore
from unilabos.workflow.task_input import prepare_task_input
from unilabos.workflow.task_scheduler_bridge import TaskSchedulerBridge

FIELDS = [
    {
        "name": "outcome",
        "label": "结论",
        "kind": "choice",
        "options": ["qualified", "stir", "add_liquid"],
        "labels": ["合格", "继续搅拌", "补液后搅拌"],
    },
    {"name": "viscosity", "label": "黏度", "kind": "number", "minimum": 0, "maximum": 100},
    {"name": "has_clumps", "label": "是否有团块", "kind": "boolean"},
    {"name": "note", "label": "备注", "kind": "text", "required": False},
]
ANSWERS = {"outcome": "qualified", "viscosity": 0, "has_clumps": False}
FORM_NODE = "20000000-0000-4000-8000-000000000092"
SOURCE = f'''from unilabos.workflow.authoring import service, workflow, workflow_output
from unilabos.workflow.native_commands import HumanCommands
human: HumanCommands = service()
@workflow(workflow_uuid="{WORKFLOW_UUID}", displayname="人工判定")
def inspect():
    # unilab:node_uuid={FORM_NODE}
    decision = human.form(prompt="检查搅拌结果", fields={FIELDS!r}, timeout_seconds=30)
    return workflow_output()
'''
LOOP_SOURCE = (
    SOURCE.replace(
        "from unilabos.workflow.authoring import service, workflow, workflow_output",
        "from lab.devices import Reactor\nfrom unilabos.workflow.authoring import device, service, workflow, workflow_output, repeat_until, until",
    )
    .replace(
        "human: HumanCommands = service()",
        'human: HumanCommands = service()\nreactor: Reactor = device("60000000-0000-4000-8000-000000000001")',
    )
    .replace(
        f'    # unilab:node_uuid={FORM_NODE}\n    decision = human.form(prompt="检查搅拌结果", fields={FIELDS!r}, timeout_seconds=30)',
        f"""    # unilab:node_uuid=20000000-0000-4000-8000-000000000090
    with repeat_until(max_iterations=3, carry={{}}) as loop:
        # unilab:node_uuid=20000000-0000-4000-8000-000000000091
        mixed = reactor.stir()
        # unilab:node_uuid={FORM_NODE}
        decision = human.form(prompt="检查搅拌结果", fields={FIELDS!r}, timeout_seconds=30)
        # unilab:node_uuid=20000000-0000-4000-8000-000000000093
        if decision.answers["outcome"] == "add_liquid":
            # unilab:node_uuid=20000000-0000-4000-8000-000000000094
            added = reactor.add_liquid()
        until(decision.answers["outcome"] == "qualified" and decision.answers["viscosity"] <= 10 and not decision.answers["has_clumps"])""",
    )
)


def form_engine():
    base = engine()._catalog
    templates = [a.detached_template() for a in base.actions]
    handles = [h for a in base.actions for h in a.detached_handles()]
    for name in ("stir", "add_liquid"):
        identity = str(uuid5(NAMESPACE_URL, name))
        template, action_handles = _template(
            identity,
            name=name,
            handles=[
                _handle(
                    str(uuid5(NAMESPACE_URL, name + direction)),
                    node_template_uuid=identity,
                    key="ready",
                    io_type=direction,
                    value_type="any",
                    data_source="dependency",
                )
                for direction in ("source", "target")
            ],
        )
        template["node_type"] = "ILab"
        template["schema"] = {"type": "object", "properties": {}}
        template["meta_data"]["unilab"] = {
            "action_contract_schema": {
                "type": "object",
                "properties": {"goal": template["schema"]},
                "required": ["goal"],
            }
        }
        templates.append(template)
        handles.extend(action_handles)
    return WorkflowAuthoringEngine(catalog=AuthoringCatalogSnapshot.from_entities(templates, handles))


@contextmanager
def runtime(tmp_path, source=SOURCE):
    compiler = form_engine()
    compiled = _compile(compiler, source)
    assert compiled.valid, compiled.diagnostics
    rebuilt = _compile(compiler, compiled.normalized_python_source, graph=compiled.graph)
    assert rebuilt.valid and rebuilt.graph == compiled.graph, rebuilt.diagnostics
    plan, jobs = ExecutionPlanBuilder().build(compiled.graph, run_mode="normal", target_node_uuid=None)
    store = WorkflowStore(tmp_path / "form.db")
    store.create_workflow(workflow_uuid=WORKFLOW_UUID, name="判定", tags=[], description=None, meta_data={})
    task_uuid = str(uuid4())
    store.create_task_with_jobs(
        workflow_uuid=WORKFLOW_UUID,
        task_uuid=task_uuid,
        run_mode="normal",
        target_node_uuid=None,
        description=None,
        meta_data={},
        plan_builder=lambda _: prepare_task_input(graph=compiled.graph, execution_plan=plan, jobs=jobs, raw_input={}),
    )
    dispatcher, clock = RecordingDispatcher(), ManualClock()
    scheduler = EdgeScheduler(dispatcher=dispatcher)
    bridge = TaskSchedulerBridge(store, scheduler=scheduler, clock=clock.now, timer_factory=clock.timer)
    try:
        bridge.submit(store.get_task(task_uuid))
        yield store, task_uuid, dispatcher, scheduler, bridge, clock
    finally:
        bridge.close()
        store.close()


def pending(bridge, task_uuid):
    requests = [r for r in bridge.human_interactions.list(task_uuid) if r["status"] == "pending"]
    assert len(requests) == 1, requests
    return requests[0]


@pytest.mark.parametrize("answers", [ANSWERS, {**ANSWERS, "note": ""}, {**ANSWERS, "note": "现场确认"}])
def test_form_accepts_typed_zero_false_and_optional_fields(answers):
    validate_form_answers(FIELDS, answers)
    assert normalize_form_fields(normalize_form_fields(FIELDS)) == normalize_form_fields(FIELDS)


@pytest.mark.parametrize(
    "change",
    [
        {"outcome": "unknown"},
        {"viscosity": True},
        {"viscosity": "3"},
        {"viscosity": -1},
        {"viscosity": float("nan")},
        {"viscosity": float("inf")},
        {"has_clumps": 0},
        {"note": None},
        {"note": "x" * 4097},
        {"undeclared": "bad"},
    ],
)
def test_form_rejects_invalid_answers(change):
    with pytest.raises(ValueError):
        validate_form_answers(FIELDS, {**ANSWERS, **change})


@pytest.mark.parametrize(
    "fields",
    [
        [],
        FIELDS * 9,
        FIELDS + [FIELDS[0]],
        [{**FIELDS[0], "kind": "object"}],
        [{**FIELDS[1], "minimum": 101}],
        [{**FIELDS[1], "default": True}],
        [{**FIELDS[0], "options": ["x", "x"]}],
        [{**FIELDS[0], "labels": ["x"]}],
        [{**FIELDS[2], "required": "false"}],
        [{**FIELDS[2], "name": "__proto__"}],
        [{**FIELDS[2], "options": ["x"]}],
    ],
)
def test_invalid_field_contract_fails_at_authoring(fields):
    with pytest.raises(ValueError):
        normalize_form_fields(fields)
    compiled = _compile(form_engine(), SOURCE.replace(repr(FIELDS), repr(fields)))
    assert not compiled.valid
    assert compiled.diagnostics[0]["code"] == "invalid_human_form"


def test_form_reply_api_is_strict_and_idempotent(tmp_path):
    with runtime(tmp_path) as (store, task, dispatcher, scheduler, bridge, _clock):
        request = pending(bridge, task)
        with TestClient(create_workflow_app(WorkflowService(store, task_scheduler_bridge=bridge))) as client:
            path = f"/api/v1/workflow-human-interactions/{request['uuid']}/reply"
            for payload, status in [
                ({"answers": {**ANSWERS, "viscosity": "0"}}, 400),
                ({"answers": {**ANSWERS, "has_clumps": 0}}, 400),
                ({"answers": {**ANSWERS, "extra": True}}, 400),
                ({"answers": {"outcome": "qualified"}}, 400),
                ({"answers": ANSWERS, "choice": "qualified"}, 400),
                ({"answers": {**ANSWERS, "note": []}}, 422),
            ]:
                assert client.post(path, json=payload).status_code == status
            assert client.post(path, json={"answers": ANSWERS}).status_code == 200
            scheduler._wake_reconcile()
            assert client.post(path, json={"answers": ANSWERS}).status_code == 200
            assert client.post(path, json={"answers": {**ANSWERS, "outcome": "stir"}}).status_code == 409
        assert store.get_task(task)["status"] == "succeeded"
        assert store.get_job(request["job_uuid"])["return_info"] == {"answers": ANSWERS}
        assert dispatcher.dispatched == []


def test_stir_form_conditional_addition_and_repeat_use_distinct_jobs(tmp_path):
    with runtime(tmp_path, LOOP_SOURCE) as (store, task, dispatcher, scheduler, bridge, _clock):
        assert [d["action"] for d in dispatcher.dispatched] == ["stir"]
        seen = []
        for outcome in ("add_liquid", "stir", "qualified"):
            scheduler.on_job_finished(dispatcher.dispatched[-1]["job_id"], True, {})
            request = pending(bridge, task)
            assert request["job_uuid"] not in seen
            seen.append(request["job_uuid"])
            response = {"answers": {**ANSWERS, "outcome": outcome}}
            bridge.reply_human_interaction(request["uuid"], response)
            scheduler._wake_reconcile()
            dispatched_count = len(dispatcher.dispatched)
            bridge.reply_human_interaction(request["uuid"], response)
            scheduler._wake_reconcile()
            assert len(dispatcher.dispatched) == dispatched_count
            if outcome == "add_liquid":
                assert dispatcher.dispatched[-1]["action"] == "add_liquid"
                scheduler.on_job_finished(dispatcher.dispatched[-1]["job_id"], True, {})
            if outcome != "qualified":
                assert dispatcher.dispatched[-1]["action"] == "stir"
                with pytest.raises(RuntimeError):
                    bridge.reply_human_interaction(request["uuid"], {"answers": ANSWERS})
        assert [d["action"] for d in dispatcher.dispatched] == ["stir", "add_liquid", "stir", "stir"]
        assert store.get_task(task)["status"] == "succeeded"


@pytest.mark.parametrize("boundary", ["cancel", "timeout", "bridge", "execution"])
def test_form_recovery_cancel_timeout(tmp_path, boundary):
    with runtime(tmp_path) as (store, task, dispatcher, scheduler, bridge, clock):
        request = pending(bridge, task)
        if boundary == "cancel":
            bridge.cancel(task)
        elif boundary == "timeout":
            clock.advance(30)
            scheduler._wake_reconcile()
        else:
            bridge.close()
            scheduler = EdgeScheduler(dispatcher=dispatcher)
            bridge = TaskSchedulerBridge(store, scheduler=scheduler, clock=clock.now, timer_factory=clock.timer)
            try:
                bridge.recover_active_tasks(execution_process_restarted=boundary == "execution")
                if boundary == "bridge":
                    restored = pending(bridge, task)
                    assert restored["uuid"] == request["uuid"]
                    assert restored["deadline_at"] == request["deadline_at"]
                    bridge.reply_human_interaction(restored["uuid"], {"answers": ANSWERS})
                    scheduler._wake_reconcile()
                    assert store.get_task(task)["status"] == "succeeded"
                    return
            finally:
                bridge.close()
        with pytest.raises(RuntimeError):
            bridge.human_interactions.reply(request["uuid"], {"answers": ANSWERS}, now=clock.now())
        assert dispatcher.dispatched == []


def test_concurrent_form_replies_have_one_winner(tmp_path):
    with runtime(tmp_path) as (_store, task, _dispatcher, scheduler, bridge, clock):
        request = pending(bridge, task)

        def reply(outcome):
            try:
                return bridge.human_interactions.reply(
                    request["uuid"], {"answers": {**ANSWERS, "outcome": outcome}}, now=clock.now()
                )["result"]
            except RuntimeError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(reply, ("stir", "qualified")))
        assert outcomes.count("conflict") == 1
        scheduler._wake_reconcile()


def test_form_loop_respects_iteration_limit_and_combined_exit_condition(tmp_path):
    with runtime(tmp_path, LOOP_SOURCE) as (store, task, dispatcher, scheduler, bridge, _clock):
        for _ in range(3):
            scheduler.on_job_finished(dispatcher.dispatched[-1]["job_id"], True, {})
            request = pending(bridge, task)
            # 结论合格仍不足以结束：另外两个条件也必须同时成立。
            bridge.reply_human_interaction(request["uuid"], {"answers": {**ANSWERS, "viscosity": 20}})
            scheduler._wake_reconcile()
        assert [d["action"] for d in dispatcher.dispatched] == ["stir"] * 3
        assert store.get_task(task)["status"] != "succeeded"
        assert not [r for r in bridge.human_interactions.list(task) if r["status"] == "pending"]
        controller = next(job for job in store.list_jobs(task) if job["workflow_node_uuid"].endswith("000000000090"))
        assert controller["status"] == "failed"
        assert "循环达到最大迭代次数" in controller["error_info"][0]["message"]


def test_form_graph_from_editor_without_executor_binding_roundtrips():
    compiler = form_engine()
    compiled = _compile(compiler, SOURCE)
    graph = deepcopy(compiled.graph)
    for node in graph["nodes"]:
        node["meta_data"]["unilab"].pop("local_command", None)
        node["material_uuid"] = None
    generated = compiler.generate_python(
        workflow_uuid=WORKFLOW_UUID, workflow_revision=7, graph=graph, source_uri="memory://form.py"
    )
    assert generated.valid, generated.diagnostics
    rebuilt = _compile(compiler, generated.normalized_python_source)
    assert rebuilt.valid, rebuilt.diagnostics
    assert rebuilt.graph["nodes"][0]["meta_data"]["unilab"]["local_command"].endswith("HumanCommands.form")


def test_committed_form_reply_resumes_after_bridge_rebuild_without_new_request(tmp_path):
    with runtime(tmp_path) as (store, task, dispatcher, _scheduler, bridge, clock):
        request = pending(bridge, task)
        # 模拟回复已提交，但调度器尚未来得及消费时调度桥被销毁。
        bridge.human_interactions.reply(request["uuid"], {"answers": ANSWERS}, now=clock.now())
        bridge.close()
        scheduler = EdgeScheduler(dispatcher=dispatcher)
        restored = TaskSchedulerBridge(store, scheduler=scheduler, clock=clock.now, timer_factory=clock.timer)
        try:
            restored.recover_active_tasks(execution_process_restarted=False)
            scheduler._wake_reconcile()
            assert store.get_task(task)["status"] == "succeeded"
            assert store.get_job(request["job_uuid"])["return_info"] == {"answers": ANSWERS}
            assert len(restored.human_interactions.list(task)) == 1
            assert dispatcher.dispatched == []
        finally:
            restored.close()
