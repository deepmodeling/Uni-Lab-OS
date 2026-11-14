"""``ctx.lock(...)`` 是 ``ctx.resource_scope(...)`` 的简写（W01）：两种编译方式下结果逐项相同。"""

from __future__ import annotations

import textwrap

import pytest

from unilabos.registry.workflow_ast import WorkflowSourceError, collect_static_workflow, compile_workflow_source
from unilabos.registry.workflows import WorkflowBuildContext


def _shape(ctx: WorkflowBuildContext) -> tuple:
    steps = [(step.kind, step.target, step.action, step.params, step.parent, step.key, step.dependencies)
             for step in ctx.steps]
    return steps, ctx.resource_scopes


def _with_lock(ctx):
    with ctx.lock("reactor", "pump", name="reaction"):
        ctx.run("reactor/load", {})
        with ctx.loop_for(2):
            with ctx.lock("reactor", name="round"):
                ctx.run("pump/dose", {"volume": 5})
        ctx.run("reactor/unload", {})


def _with_resource_scope(ctx):
    with ctx.resource_scope("reactor", "pump", name="reaction"):
        ctx.run("reactor/load", {})
        with ctx.loop_for(2):
            with ctx.resource_scope("reactor", name="round"):
                ctx.run("pump/dose", {"volume": 5})
        ctx.run("reactor/unload", {})


def _builder(fn) -> WorkflowBuildContext:
    ctx = WorkflowBuildContext()
    fn(ctx)
    return ctx


def test_lock_compiles_exactly_like_resource_scope_in_both_compilers() -> None:
    expected = _shape(_builder(_with_resource_scope))
    assert _shape(_builder(_with_lock)) == expected
    assert _shape(collect_static_workflow(_with_lock)) == expected
    assert _shape(collect_static_workflow(_with_resource_scope)) == expected
    [outer, inner] = expected[1]
    assert (outer["scope_id"], outer["resources"], inner["parent_scope_id"]) == ("reaction", ["reactor", "pump"], "reaction")
    assert len(outer["node_keys"]) == 4 and len(inner["node_keys"]) == 1  # 装料、循环、加液、卸料


def test_lock_inside_a_helper_is_expanded_by_the_ast_compiler() -> None:
    compiled = compile_workflow_source(textwrap.dedent('''
        def _hold_and_stir(ctx, seconds):
            with ctx.lock("reactor"):
                ctx.run("reactor/stir", {"duration": seconds})

        def flow(ctx):
            ctx.run("reactor/load", {})
            _hold_and_stir(ctx, 30)
    '''), function_name="flow").ctx
    [scope] = compiled.resource_scopes
    assert scope["resources"] == ["reactor"]
    assert [step.params for step in compiled.steps if step.key in scope["node_keys"]] == [{"duration": 30}]


@pytest.mark.parametrize(("device_ids", "match"), [((), "lock 需要非空设备 ID"), (("a", "a"), "lock 的设备 ID 不能重复")])
def test_lock_errors_name_the_form_the_author_wrote(device_ids, match) -> None:
    ctx = WorkflowBuildContext()
    with pytest.raises(ValueError, match=match):
        with ctx.lock(*device_ids):
            ctx.run("a/x", {})
    with pytest.raises(ValueError, match="资源作用域不能是空块"):
        with ctx.lock("a"):
            pass


def test_ast_rejects_binding_a_lock_with_as() -> None:
    with pytest.raises(WorkflowSourceError, match="lock / resource_scope"):
        compile_workflow_source(textwrap.dedent('''
            def flow(ctx):
                with ctx.lock("reactor") as held:
                    ctx.run("reactor/stir", {})
        '''), function_name="flow")
