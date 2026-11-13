"""AST 模式的辅助函数展开：只读源码、不执行作者代码，展开结果与手写步骤一致。"""

from __future__ import annotations

import textwrap

import pytest

from unilabos.registry.workflow_ast import WorkflowSourceError, collect_static_workflow, compile_workflow_source
from unilabos.registry.workflows import WorkflowBuildContext

WATER_LOT = "11111111-1111-4111-8111-111111111111"
RESET_PARAMS = {"target_c": 20}


def _steps(ctx: WorkflowBuildContext) -> list[tuple]:
    return [(step.kind, step.target, step.action, step.params, step.name, step.inventory,
             step.parent, step.key, step.dependencies, step.guards) for step in ctx.steps]


def _reset(ctx):
    """复位：每条流程都从同一状态出发。"""
    ctx.run("reactor/reset", RESET_PARAMS, name="复位")


def _measure(ctx, label="测量"):
    result = ctx.run("reactor/measure", {}, name=label)
    return result


def _water(quantity, unit="ml"):
    return {"key": "water", "kind": "lot", "lot_uuid": WATER_LOT, "quantity": quantity, "unit": unit}


def _reset_and_measure(ctx):
    _reset(ctx)
    return ctx.run("reactor/measure", {}, name="测量")


def test_helpers_expand_to_the_same_steps_as_hand_written_code() -> None:
    def with_helpers(ctx):
        _reset(ctx)
        measured = _measure(ctx, label="首测")
        if measured.purity > 0.9:
            ctx.run("reactor/collect", {"purity": measured.purity}, inventory=[_water(500)])
        else:
            ctx.run("reactor/repurify", {}, inventory=[_water(quantity=20, unit="ul")])

    def by_hand(ctx):
        ctx.run("reactor/reset", {"target_c": 20}, name="复位")
        measured = ctx.run("reactor/measure", {}, name="首测")
        if measured.purity > 0.9:
            ctx.run("reactor/collect", {"purity": measured.purity},
                    inventory=[{"key": "water", "kind": "lot", "lot_uuid": WATER_LOT, "quantity": 500, "unit": "ml"}])
        else:
            ctx.run("reactor/repurify", {},
                    inventory=[{"key": "water", "kind": "lot", "lot_uuid": WATER_LOT, "quantity": 20, "unit": "ul"}])

    assert _steps(collect_static_workflow(with_helpers)) == _steps(collect_static_workflow(by_hand))


def test_nested_helpers_and_returned_steps() -> None:
    def nested(ctx):
        first = _reset_and_measure(ctx)
        ctx.run("reactor/report", {"value": first.value})

    steps = collect_static_workflow(nested).steps
    assert [(step.target, step.action) for step in steps] == [
        ("reactor", "reset"), ("reactor", "measure"), ("reactor", "report")]
    assert steps[2].params == {"value": {"kind": "node_output", "node_key": steps[1].key, "data_key": "value"}}


def test_helper_locals_and_constants_do_not_clash_with_the_caller() -> None:
    RESET_PARAMS = {"target_c": 99}  # noqa: N806 - 与辅助函数模块里的同名常量值不同

    def clashing(ctx):
        result = ctx.run("reactor/start", RESET_PARAMS)  # 与 _measure 里的局部名 result 同名
        _reset(ctx)
        measured = _measure(ctx)
        ctx.run("reactor/report", {"a": result.ok, "b": measured.ok})

    steps = collect_static_workflow(clashing).steps
    assert steps[0].params == {"target_c": 99}
    assert steps[1].params == {"target_c": 20}  # 辅助函数用的是它自己模块里的常量
    assert steps[3].params == {"a": {"kind": "node_output", "node_key": steps[0].key, "data_key": "ok"},
                               "b": {"kind": "node_output", "node_key": steps[2].key, "data_key": "ok"}}


def test_source_text_entry_inlines_helpers_from_the_same_file() -> None:
    source = textwrap.dedent('''
        from unilabos.registry.workflows import workflow

        LOT = "22222222-2222-4222-8222-222222222222"

        def _prepare(ctx, volume):
            ctx.run("pump/prime", {"ml": volume})

        def _lot(quantity):
            return {"key": "w", "kind": "lot", "lot_uuid": LOT, "quantity": quantity, "unit": "ml"}

        @workflow(display_name="源码入口")
        def flow(ctx):
            _prepare(ctx, 5)
            ctx.run("pump/dispense", {}, inventory=[_lot(3)])
    ''')
    compiled = compile_workflow_source(source)
    assert [(step.action, step.params) for step in compiled.ctx.steps] == [("prime", {"ml": 5}), ("dispense", {})]
    assert compiled.ctx.steps[1].inventory[0]["lot_uuid"] == "22222222-2222-4222-8222-222222222222"
    # 展开出的步骤源码范围记在模板里的调用行
    assert compiled.step_ranges[compiled.ctx.steps[0].key][0] == source.splitlines().index("    _prepare(ctx, 5)") + 1


def test_inlined_steps_do_not_inherit_the_callers_node_anchor() -> None:
    source = textwrap.dedent('''
        def _two(ctx):
            ctx.run("a/x", {})
            ctx.run("a/y", {})

        @workflow(display_name="锚点")
        def flow(ctx):
            # unilab:node_uuid 33333333-3333-4333-8333-333333333333
            _two(ctx)
    ''')
    compiled = compile_workflow_source(source)
    assert len(compiled.ctx.steps) == 2 and compiled.anchors == {}


def test_helpers_can_live_in_another_module(tmp_path, monkeypatch) -> None:
    package = tmp_path / "helper_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "common.py").write_text(textwrap.dedent('''
        SPEED = 300

        def spin(ctx, seconds):
            ctx.run("centrifuge/spin", {"rpm": SPEED, "seconds": seconds})
    '''), encoding="utf-8")
    (package / "flows.py").write_text(textwrap.dedent('''
        from helper_pkg.common import spin

        SPEED = 1  # 与 common.SPEED 不同

        def flow(ctx):
            spin(ctx, 30)
            ctx.run("centrifuge/stop", {"rpm": SPEED})
    '''), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    from helper_pkg.flows import flow

    assert [step.params for step in collect_static_workflow(flow).steps] == [{"rpm": 300, "seconds": 30}, {"rpm": 1}]


def _loop_a(ctx):
    _loop_b(ctx)


def _loop_b(ctx):
    _loop_a(ctx)


def _two_statements(quantity):
    total = quantity * 2
    return total


def _needs_volume(ctx, volume):
    ctx.run("pump/dispense", {"ml": volume})


def _flow_recursion(ctx):
    _loop_a(ctx)


def _flow_value_helper_with_statements(ctx):
    ctx.run("a/b", {"v": _two_statements(1)})


def _flow_missing_argument(ctx):
    _needs_volume(ctx)


@pytest.mark.parametrize(("flow", "message"), [
    (_flow_recursion, "递归"),
    (_flow_value_helper_with_statements, "一句 return"),
    (_flow_missing_argument, "缺少实参 volume"),
], ids=["recursion", "value-helper-with-statements", "missing-argument"])
def test_unsupported_helpers_fail_at_the_call_site(flow, message) -> None:
    with pytest.raises(WorkflowSourceError, match=message) as raised:
        collect_static_workflow(flow)
    assert raised.value.line is not None
