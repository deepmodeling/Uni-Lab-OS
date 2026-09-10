"""动作结果解析器（Action result parser）对伪造 AST 节点形状的失败关闭回归测试。"""

from __future__ import annotations

import ast
from collections.abc import Mapping
from types import MappingProxyType

import pytest

from unilabos.registry.action_result_schema import (
    ActionResultSchemaError,
    parse_action_result_declaration,
)

_ERROR_MESSAGE = "Action 结果声明不符合 Workflow 版本 1 合同"
_EMPTY_IMPORTS: Mapping[str, str] = MappingProxyType({})
_TYPED_DICT_IMPORTS: Mapping[str, str] = MappingProxyType(
    {"TypedDict": "typing:TypedDict"}
)


def _typed_dict_with_body(body: list[ast.stmt] | None) -> ast.ClassDef:
    return ast.ClassDef(
        name="Result",
        bases=[ast.Name(id="TypedDict", ctx=ast.Load())],
        keywords=[],
        body=body,
        decorator_list=[],
    )


_MALFORMED_DECLARATIONS = [
    pytest.param(
        ast.Dict(
            keys=None,
            values=[ast.Name(id="str", ctx=ast.Load())],
        ),
        _EMPTY_IMPORTS,
        "/return",
        id="dict-keys-container-none",
    ),
    pytest.param(
        ast.Dict(
            keys=[ast.Constant(value="value")],
            values=None,
        ),
        _EMPTY_IMPORTS,
        "/return",
        id="dict-values-container-none",
    ),
    pytest.param(
        _typed_dict_with_body(None),
        _TYPED_DICT_IMPORTS,
        "/return",
        id="class-body-container-none",
    ),
    pytest.param(
        _typed_dict_with_body([ast.AnnAssign()]),
        _TYPED_DICT_IMPORTS,
        "/return/body/0",
        id="annotated-assignment-missing-target",
    ),
    pytest.param(
        _typed_dict_with_body(
            [
                ast.AnnAssign(
                    target=ast.Name(id="value", ctx=ast.Store()),
                    value=None,
                    simple=1,
                )
            ]
        ),
        _TYPED_DICT_IMPORTS,
        "/return/body/0",
        id="annotated-assignment-missing-annotation",
    ),
]


@pytest.mark.parametrize(
    ("declaration", "imports", "path_prefix"),
    _MALFORMED_DECLARATIONS,
)
def test_forged_ast_shapes_fail_closed_with_stable_public_error(
    declaration: ast.expr | ast.ClassDef,
    imports: Mapping[str, str],
    path_prefix: str,
) -> None:
    signatures: list[tuple[str, str, str]] = []

    for _ in range(2):
        with pytest.raises(ActionResultSchemaError) as caught:
            parse_action_result_declaration(declaration, imports=imports)

        error = caught.value
        assert error.code == "invalid_action_result"
        assert error.path.startswith("/return")
        assert error.path.startswith(path_prefix)
        assert error.message == _ERROR_MESSAGE
        assert str(error) == _ERROR_MESSAGE
        signatures.append((error.code, error.path, error.message))

    assert signatures[0] == signatures[1]
