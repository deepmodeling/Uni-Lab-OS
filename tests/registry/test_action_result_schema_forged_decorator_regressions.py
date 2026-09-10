"""动作结果解析器（Action result parser）对伪造 dataclass decorator 的失败关闭回归测试。"""

from __future__ import annotations

import ast
from types import MappingProxyType

import pytest

from unilabos.registry.action_result_schema import (
    ActionResultSchemaError,
    parse_action_result_declaration,
)

_ERROR_MESSAGE = "Action 结果声明不符合 Workflow 版本 1 合同"
_IMPORTS = MappingProxyType({"dataclass": "dataclasses:dataclass"})


def _frozen_dataclass_with_keyword_name(keyword_name: object) -> ast.ClassDef:
    return ast.ClassDef(
        name="Result",
        bases=[],
        keywords=[],
        body=[
            ast.AnnAssign(
                target=ast.Name(id="value", ctx=ast.Store()),
                annotation=ast.Name(id="str", ctx=ast.Load()),
                value=None,
                simple=1,
            )
        ],
        decorator_list=[
            ast.Call(
                func=ast.Name(id="dataclass", ctx=ast.Load()),
                args=[],
                keywords=[
                    ast.keyword(
                        arg="frozen",
                        value=ast.Constant(value=True),
                    ),
                    ast.keyword(
                        arg=keyword_name,
                        value=ast.Constant(value=True),
                    ),
                ],
            )
        ],
    )


@pytest.mark.parametrize(
    "keyword_name",
    [
        pytest.param([], id="list-keyword-name"),
        pytest.param({}, id="dict-keyword-name"),
    ],
)
def test_unhashable_dataclass_keyword_name_has_stable_public_error(
    keyword_name: object,
) -> None:
    declaration = _frozen_dataclass_with_keyword_name(keyword_name)
    signatures: list[tuple[str, str, str]] = []

    for _ in range(2):
        with pytest.raises(ActionResultSchemaError) as caught:
            parse_action_result_declaration(declaration, imports=_IMPORTS)

        error = caught.value
        assert error.code == "invalid_action_result"
        assert error.path == "/return/decorators/0"
        assert error.message == _ERROR_MESSAGE
        signatures.append((error.code, error.path, error.message))

    assert signatures[0] == signatures[1]
