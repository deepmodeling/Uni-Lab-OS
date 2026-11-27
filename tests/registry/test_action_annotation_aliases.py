"""同模块 Annotated 输入别名的静态、失败关闭边界。"""

import ast

import pytest

from unilabos.registry.action_contract_schema import ActionContractError, parse_action_contract

HEADER = "from typing import Annotated, Literal\nfrom pydantic import Field\n"
VALUE = 'Annotated[Literal[11, 12], Field(title="夹持模式", json_schema_extra={"x-unilabos-enum-labels": ["长边", "短边"]})]'


def parse(source):
    module = ast.parse(source)
    action = next(n for n in ast.walk(module) if isinstance(n, ast.FunctionDef) and n.name == "move")
    return parse_action_contract(module, action, module_name="test.driver").to_dict()


def test_direct_alias_is_identical_to_inline_and_does_not_execute_module():
    direct = parse(HEADER + f"def move(mode: {VALUE} = 11) -> None: pass\n")
    aliased = parse(
        HEADER
        + f'Clamp = {VALUE}\nraise RuntimeError("must never execute")\ndef move(mode: Clamp = 11) -> None: pass\n'
    )
    assert aliased == direct


@pytest.mark.parametrize(
    "declarations",
    [
        f"Clamp = {VALUE}\nClamp = int",
        f"Clamp = {VALUE}\nif enabled:\n    Clamp = int",
        f"Clamp = {VALUE}\nfrom other import Clamp",
        f"Clamp = {VALUE}\nClamp = {VALUE}",
        "Clamp = Other\nOther = Clamp",
        "from other import Clamp",
        f"class Container:\n    Clamp = {VALUE}",
        f"Clamp = {VALUE}\ndel Clamp",
        "Clamp = Annotated[Literal[11,12], Field(title=make_title())]",
        f"Clamp = {VALUE}\nField = object",
    ],
)
def test_ambiguous_dynamic_imported_or_cyclic_alias_fails_closed(declarations):
    with pytest.raises(ActionContractError):
        parse(HEADER + declarations + "\ndef move(mode: Clamp) -> None: pass\n")
