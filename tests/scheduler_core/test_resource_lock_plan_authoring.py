"""资源计划作者语法和作用域快照的模块测试。"""

from __future__ import annotations

import pytest

from unilabos.workflow.authoring_ast import AuthoringSyntaxError, parse_authoring_source


def test_authoring_ast_preserves_root_and_lexical_resource_scopes() -> None:
    """作者 AST 应冻结根资源与 with resources 的硬边界。"""

    source = '''
from lab.devices import Reactor
from unilabos.workflow.authoring import device, workflow, workflow_output, resources

reactor: Reactor = device()

@workflow(
    workflow_uuid="00000000-0000-4000-8000-000000000011",
    displayname="resource scope",
    resources=("station:photo_scrape",),
)
def run():
    # unilab:node_uuid=00000000-0000-4000-8000-000000000012
    first = reactor.prepare()
    with resources("robot", "station:rail"):
        # unilab:node_uuid=00000000-0000-4000-8000-000000000013
        second = reactor.move()
    return workflow_output()
'''

    program = parse_authoring_source(
        python_source=source,
        expected_workflow_uuid="00000000-0000-4000-8000-000000000011",
    )

    assert program.root_resources == ("station:photo_scrape",)
    assert len(program.resource_scopes) == 1
    scope = program.resource_scopes[0]
    assert scope.resources == ("robot", "station:rail")
    assert scope.entry_node_uuid == "00000000-0000-4000-8000-000000000013"
    assert scope.exit_node_uuid == scope.entry_node_uuid


def test_authoring_ast_rejects_dynamic_resource_scope() -> None:
    """资源作用域不得由运行时变量决定。"""

    source = '''
from lab.devices import Reactor
from unilabos.workflow.authoring import device, workflow, workflow_output, resources

reactor: Reactor = device()

@workflow(
    workflow_uuid="00000000-0000-4000-8000-000000000021",
    displayname="invalid resource scope",
)
def run(*, resource_name: str):
    with resources(resource_name):
        # unilab:node_uuid=00000000-0000-4000-8000-000000000022
        action = reactor.move()
    return workflow_output()
'''

    with pytest.raises(AuthoringSyntaxError) as caught:
        parse_authoring_source(
            python_source=source,
            expected_workflow_uuid="00000000-0000-4000-8000-000000000021",
        )

    assert caught.value.code == "invalid_resource_scope"


def test_authoring_nested_scope_identity_survives_formatting() -> None:
    """嵌套作用域的身份与父链不应因源码行号变化而漂移。"""

    source = '''
from lab.devices import Reactor
from unilabos.workflow.authoring import device, workflow, workflow_output, resources

reactor: Reactor = device()

@workflow(
    workflow_uuid="00000000-0000-4000-8000-000000000031",
    displayname="nested resource scope",
)
def run():
    with resources("outer"):
        with resources("inner"):
            # unilab:node_uuid=00000000-0000-4000-8000-000000000032
            action = reactor.move()
    return workflow_output()
'''

    first = parse_authoring_source(
        python_source=source,
        expected_workflow_uuid="00000000-0000-4000-8000-000000000031",
    )
    formatted_source = source.replace("def run():\n", 'def run():\n    """说明。"""\n')
    second = parse_authoring_source(
        python_source=formatted_source,
        expected_workflow_uuid="00000000-0000-4000-8000-000000000031",
    )

    assert [scope.scope_id for scope in first.resource_scopes] == [
        scope.scope_id for scope in second.resource_scopes
    ]
    inner, outer = first.resource_scopes
    assert inner.parent_scope_id == outer.scope_id
    assert inner.node_uuids == outer.node_uuids == (
        "00000000-0000-4000-8000-000000000032",
    )
