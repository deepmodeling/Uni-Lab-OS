"""工作流物料输入的允许模板写在装饰器上：``@workflow(allowed_templates={输入名: [注册表资源 id, ...]})``。

输入本身照常注解为 ``ResourceSlot`` / ``List[ResourceSlot]``；键不是这样的输入时注册失败。发布时（模板载荷 / 作者编译）
按物料权威把注册表资源 id 解析成资源模板 uuid（未知 id 发布失败），写进输入 schema 的
``allowed_resource_template_uuids``——动作 / 组合工作流已在用的同一个扩展，前端据此筛选。
``Annotated[ResourceSlot, AllowedResourceTemplates(...)]`` 写在工作流输入上仍报错，并指向新写法。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import List, Optional
from uuid import uuid4

import pytest

from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.registry.workflow_ast import WorkflowSourceError, compile_workflow_source
from unilabos.registry.workflows import (
    build_workflow_template_payload,
    clear_registered_workflows,
    get_registered_workflows,
    workflow,
)

DEEP_WELL = str(uuid4())
COLLECTION = str(uuid4())
TROUGH = str(uuid4())
TEMPLATES = {
    "community.demo_pkg.deep_well_plate": DEEP_WELL,
    "community.demo_pkg.collection_plate": COLLECTION,
    "community.demo_pkg.reagent_trough": TROUGH,
    "community.other_pkg.reagent_trough": str(uuid4()),
}


class _Gateway:
    def list_templates(self, **_kwargs):
        return [SimpleNamespace(name=name, template_uuid=identity) for name, identity in TEMPLATES.items()]


@pytest.fixture(autouse=True)
def _materials(monkeypatch):
    from unilabos.server import execution_composition

    monkeypatch.setattr(execution_composition, "get_materials_gateway", lambda: _Gateway())
    snapshot = get_registered_workflows()
    clear_registered_workflows()
    yield
    clear_registered_workflows()
    from unilabos.registry.workflows import _registered_workflows

    _registered_workflows.update(snapshot)


def _single_definition():
    [definition] = get_registered_workflows().values()
    return definition


def _inputs(payload) -> dict:
    return {item["name"]: item["schema"] for item in payload["io"]["input_contract"]["parameters"]}


def test_allowed_templates_resolve_into_the_input_schema_extension() -> None:
    @workflow(
        "环化",
        allowed_templates={
            "plate_1": ["deep_well_plate", "community.demo_pkg.collection_plate"],
            "plates": "deep_well_plate",
        },
    )
    def cyclize(ctx, *, plate_1: ResourceSlot, plates: List[ResourceSlot], spare: Optional[ResourceSlot] = None,
                volume: float = 1.0):
        ctx.run("handler/couple", {"plate": plate_1, "plates": plates})

    definition = _single_definition()
    assert definition.allowed_templates == {
        "plate_1": ["deep_well_plate", "community.demo_pkg.collection_plate"],
        "plates": ["deep_well_plate"],
    }
    payload = build_workflow_template_payload(definition)

    inputs = _inputs(payload)
    assert inputs["plate_1"] == {"$slot": "ResourceSlot", "allowed_resource_template_uuids": [DEEP_WELL, COLLECTION]}
    assert inputs["plates"]["items"] == {"$slot": "ResourceSlot", "allowed_resource_template_uuids": [DEEP_WELL]}
    assert "allowed_resource_template_uuids" not in str(inputs["spare"])
    # 同名的隐式输出（物料原样传出）带同一份允许模板
    implicit = {item["name"]: item["schema"] for item in payload["io"]["output_contract"]["outputs"]}
    assert implicit["plate_1"] == inputs["plate_1"]

    # 写进去的就是工作流输入合同认的扩展（存库 / 详情接口按它规范化）
    from unilabos.server.services.runtime.workflow.workflow_io import normalize_input_contract

    normalized = normalize_input_contract(payload["io"]["input_contract"])
    assert normalized["parameters"][0]["schema"]["allowed_resource_template_uuids"] == [DEEP_WELL, COLLECTION]


@pytest.mark.parametrize(
    "allowed, message",
    [
        ({"missing": ["deep_well_plate"]}, "不是工作流输入"),
        ({"volume": ["deep_well_plate"]}, "必须是 ResourceSlot / List[ResourceSlot] 输入"),
        ({"plate_1": []}, "非空"),
        ({"plate_1": [3]}, "不是注册表资源 id"),
    ],
)
def test_keys_must_be_resource_slot_inputs(allowed, message) -> None:
    with pytest.raises(ValueError, match=message.replace("[", r"\[").replace("]", r"\]")):

        @workflow("环化", allowed_templates=allowed)
        def cyclize(ctx, *, plate_1: ResourceSlot, volume: float = 1.0):
            ctx.run("handler/couple", {"plate": plate_1})

    assert get_registered_workflows() == {}


@pytest.mark.parametrize(
    "identity, message",
    [("no_such_plate", "注册表里没有的资源"), ("reagent_trough", "匹配到多个注册表资源")],
)
def test_unknown_or_ambiguous_ids_fail_publishing(identity, message) -> None:
    @workflow("环化", allowed_templates={"plate_1": [identity]})
    def cyclize(ctx, *, plate_1: ResourceSlot):
        ctx.run("handler/couple", {"plate": plate_1})

    with pytest.raises(ValueError, match=message):
        build_workflow_template_payload(_single_definition())


def test_static_compilation_reads_the_literal() -> None:
    program = compile_workflow_source(
        """
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.registry.workflows import workflow

@workflow("环化", allowed_templates={"plate_1": ["deep_well_plate", "collection_plate"]})
def cyclize(ctx, *, plate_1: ResourceSlot):
    ctx.run("handler/couple", {"plate": plate_1})
"""
    )
    assert program.allowed_templates == {"plate_1": ["deep_well_plate", "collection_plate"]}
    with pytest.raises(WorkflowSourceError, match="不是工作流输入") as caught:
        compile_workflow_source(
            """
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.registry.workflows import workflow

@workflow("环化",
          allowed_templates={"plate_9": ["deep_well_plate"]})
def cyclize(ctx, *, plate_1: ResourceSlot):
    ctx.run("handler/couple", {"plate": plate_1})
"""
        )
    assert caught.value.line == 6


def test_annotated_allowed_resource_templates_on_a_workflow_input_points_to_the_decorator() -> None:
    source = """
from typing import Annotated
from unilabos.registry.annotations import AllowedResourceTemplates
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.registry.workflows import workflow
from lab.plates import DeepWellPlate

@workflow("环化")
def cyclize(ctx, *, plate_1: Annotated[ResourceSlot, AllowedResourceTemplates(DeepWellPlate)]):
    ctx.run("handler/couple", {"plate": plate_1})
"""
    with pytest.raises(WorkflowSourceError) as caught:
        compile_workflow_source(source)
    message = str(caught.value)
    assert "plate_1" in message and "@workflow(" in message and "allowed_templates=" in message
