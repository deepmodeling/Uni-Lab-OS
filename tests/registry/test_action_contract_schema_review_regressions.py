"""动作合同（ActionContract）门面审查发现的公共接缝（seam）回归测试。"""

from __future__ import annotations

import ast
import textwrap

import pytest

from unilabos.registry.action_contract_schema import (
    ActionContractError,
    parse_action_contract,
)

_MODULE_NAME = "lab.devices.review_regression"


def _module_and_action(source: str) -> tuple[ast.Module, ast.FunctionDef]:
    module = ast.parse(textwrap.dedent(source))
    actions = [node for node in ast.walk(module) if isinstance(node, ast.FunctionDef)]
    assert len(actions) == 1
    return module, actions[0]


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            """
            class Device:
                def action(self, /, self: int) -> None:
                    pass
            """,
            id="receiver-positional-only-and-ordinary",
        ),
        pytest.param(
            """
            class Device:
                def action(
                    self,
                    sample_uuids,
                    /,
                    sample_uuids: int,
                ) -> None:
                    pass
            """,
            id="framework-positional-only-and-ordinary",
        ),
        pytest.param(
            """
            class Device:
                def action(
                    self,
                    sample_uuids,
                    *,
                    sample_uuids: int,
                ) -> None:
                    pass
            """,
            id="framework-ordinary-and-keyword-only",
        ),
        pytest.param(
            """
            class Device:
                def action(
                    self,
                    value: int,
                    /,
                    *,
                    value: int,
                ) -> None:
                    pass
            """,
            id="business-positional-only-and-keyword-only",
        ),
    ],
)
def test_duplicate_source_parameter_names_fail_before_contract_filtering(
    source: str,
) -> None:
    module, action = _module_and_action(source)

    with pytest.raises(ActionContractError) as caught:
        parse_action_contract(module, action, module_name=_MODULE_NAME)

    error = caught.value
    assert error.code == "invalid_action_contract"
    assert error.path.startswith("/parameters")
    assert error.message


def test_deep_left_associative_annotation_has_one_repeatable_action_error() -> None:
    annotation = " | ".join(["int"] * 512)
    module, action = _module_and_action(
        f"def action(value: {annotation}) -> None:\n    pass\n"
    )
    observed: list[tuple[str, str, str]] = []

    for _ in range(3):
        with pytest.raises(ActionContractError) as caught:
            parse_action_contract(module, action, module_name=_MODULE_NAME)

        error = caught.value
        assert error.code
        assert error.path
        assert error.message
        observed.append((error.code, error.path, error.message))

    assert observed == [observed[0], observed[0], observed[0]]
