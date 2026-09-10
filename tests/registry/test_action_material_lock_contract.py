"""动作（Action）物料锁（Material Lock）编译协议的公共行为测试。"""

from __future__ import annotations

import ast
import textwrap
from typing import Any

import pytest

from unilabos.registry.action_contract_schema import (
    ActionContractError,
    parse_action_contract,
)
from unilabos.registry.annotation_schema import (
    NO_DEFAULT,
    AnnotationSchemaError,
    parse_parameter_annotation,
)

_MODULE_NAME = "lab.devices.material_lock"


def _parse(source: str, *, action_name: str = "action") -> Any:
    module = ast.parse(textwrap.dedent(source))
    action = next(
        node
        for node in ast.walk(module)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == action_name
    )
    return parse_action_contract(module, action, module_name=_MODULE_NAME)


def _assert_action_error(
    source: str,
    *,
    code: str = "invalid_annotation",
) -> None:
    with pytest.raises(ActionContractError) as caught:
        _parse(source)
    assert caught.value.code == code
    assert caught.value.path.startswith("/parameters/") or caught.value.path.startswith(
        "/return"
    )


def _material_reference_schema(
    *,
    locked: bool | None,
    nullable: bool = False,
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": ["object", "null"] if nullable else "object",
        "properties": {
            "uuid": {
                "type": "string",
                "format": "uuid",
            }
        },
        "required": ["uuid"],
        "additionalProperties": False,
    }
    if locked is not None:
        schema["x-unilabos-material-lock"] = locked
    return schema


def test_action_input_resource_slots_default_to_locked_and_free_is_explicit() -> None:
    contract = _parse(
        """
        from typing import Annotated, TypedDict
        from unilabos.registry.annotations import MaterialLock
        from unilabos.registry.placeholder_type import ResourceSlot

        class Result(TypedDict):
            material: ResourceSlot

        def action(
            required_material: ResourceSlot,
            optional_material: ResourceSlot | None = None,
            materials: list[ResourceSlot] = [],
            free_material: Annotated[
                ResourceSlot | None,
                MaterialLock(free=True),
            ] = None,
        ) -> Result:
            pass
        """
    )

    schema = contract.to_action_schema(action_name="action")
    goal = schema["properties"]["goal"]

    assert schema["x-unilabos-action-contract"]["version"] == 2
    assert goal["required"] == ["required_material"]
    assert goal["properties"]["required_material"] == _material_reference_schema(
        locked=True
    )
    assert goal["properties"]["optional_material"] == {
        **_material_reference_schema(locked=True, nullable=True),
        "default": None,
    }
    assert goal["properties"]["materials"] == {
        "type": "array",
        "items": _material_reference_schema(locked=True),
        "default": [],
    }
    assert goal["properties"]["free_material"] == {
        **_material_reference_schema(locked=False, nullable=True),
        "default": None,
    }

    result_material = schema["properties"]["result"]["properties"]["material"]
    assert result_material == _material_reference_schema(locked=None)
    assert "x-unilabos-material-lock" not in result_material


def test_material_lock_metadata_does_not_change_canonical_workflow_value() -> None:
    contract = _parse(
        """
        from typing import Annotated
        from unilabos.registry.annotations import MaterialLock
        from unilabos.registry.placeholder_type import ResourceSlot

        def action(
            material: Annotated[ResourceSlot, MaterialLock(free=True)],
        ) -> None:
            pass
        """
    )

    assert contract.to_dict()["input_contract"]["parameters"] == [
        {
            "name": "material",
            "schema": {"$slot": "ResourceSlot"},
            "required": True,
        }
    ]


def test_material_lock_metadata_is_rejected_outside_action_input_scope() -> None:
    module = ast.parse(
        textwrap.dedent(
            """
            from typing import Annotated
            from unilabos.registry.annotations import MaterialLock
            from unilabos.registry.placeholder_type import ResourceSlot

            def workflow(
                material: Annotated[ResourceSlot, MaterialLock(free=True)],
            ):
                pass
            """
        )
    )
    function = next(node for node in module.body if isinstance(node, ast.FunctionDef))
    imports = {
        alias.asname or alias.name: f"{node.module}:{alias.name}"
        for node in module.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }

    with pytest.raises(AnnotationSchemaError) as caught:
        parse_parameter_annotation(
            "material",
            function.args.args[0].annotation,
            default=NO_DEFAULT,
            imports=imports,
        )
    assert caught.value.code == "invalid_annotation"


@pytest.mark.parametrize(
    "annotation",
    [
        pytest.param(
            "Annotated[str, MaterialLock(free=True)]",
            id="non-material-value",
        ),
        pytest.param(
            "Annotated[ResourceSlot, MaterialLock(True)]",
            id="positional-free-flag",
        ),
        pytest.param(
            "Annotated[ResourceSlot, MaterialLock(free=False)]",
            id="redundant-false-flag",
        ),
        pytest.param(
            "Annotated[ResourceSlot, MaterialLock(free=decision)]",
            id="dynamic-free-flag",
        ),
        pytest.param(
            "Annotated[ResourceSlot, MaterialLock(free=True), MaterialLock(free=True)]",
            id="duplicate-metadata",
        ),
    ],
)
def test_free_metadata_is_a_closed_fail_safe_contract(annotation: str) -> None:
    _assert_action_error(
        f"""
        from typing import Annotated
        from unilabos.registry.annotations import MaterialLock
        from unilabos.registry.placeholder_type import ResourceSlot

        decision = True

        def action(material: {annotation}) -> None:
            pass
        """
    )


def test_action_result_cannot_declare_material_lock_metadata() -> None:
    _assert_action_error(
        """
        from typing import Annotated, TypedDict
        from unilabos.registry.annotations import MaterialLock
        from unilabos.registry.placeholder_type import ResourceSlot

        class Result(TypedDict):
            material: Annotated[ResourceSlot, MaterialLock(free=True)]

        def action() -> Result:
            pass
        """,
        code="invalid_action_result",
    )


def test_forged_material_lock_import_is_rejected() -> None:
    _assert_action_error(
        """
        from typing import Annotated
        from malicious.annotations import MaterialLock
        from unilabos.registry.placeholder_type import ResourceSlot

        def action(
            material: Annotated[ResourceSlot, MaterialLock(free=True)],
        ) -> None:
            pass
        """
    )


def test_runtime_annotation_helper_only_accepts_explicit_free_true() -> None:
    from unilabos.registry.annotations import MaterialLock

    assert MaterialLock(free=True).free is True
    with pytest.raises(ValueError, match="free=True"):
        MaterialLock(free=False)
