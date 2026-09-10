"""模块级静态名称解析的版本 1 合同。"""

from __future__ import annotations

import ast
import importlib
from typing import Any

import pytest

from unilabos.registry.action_result_schema import (
    ActionResultSchemaError,
    parse_action_result_declaration,
)
from unilabos.registry.annotation_schema import (
    NO_DEFAULT,
    AnnotationSchemaError,
    parse_parameter_annotation,
)

_MODULE_NAME = "unilabos.registry.module_scope"


def _api() -> Any:
    """延迟加载，让缺少 production seam 的基线产生逐用例 RED。"""

    return importlib.import_module(_MODULE_NAME)


def _resolve(
    source: str,
    *,
    module_name: str = "lab.workflows.transfer",
) -> tuple[ast.Module, Any]:
    tree = ast.parse(source)
    return tree, _api().resolve_module_scope(tree, module_name=module_name)


def _parse_workflow_value(source: str) -> Any:
    tree = ast.parse(source)
    scope = _api().resolve_module_scope(
        tree,
        module_name="lab.workflows.transfer",
    )
    function = scope.definitions["workflow"]
    assert isinstance(function, ast.FunctionDef)
    parameter = function.args.kwonlyargs[0]
    assert parameter.annotation is not None
    return parse_parameter_annotation(
        parameter.arg,
        parameter.annotation,
        default=NO_DEFAULT,
        imports=scope.annotation_bindings,
    )


def test_absolute_imports_follow_python_binding_names_and_static_identities() -> None:
    _, scope = _resolve(
        """
from __future__ import annotations
import package.submodule
import another.branch as branch_alias
import top_level
from contracts.results import Result as ImportedResult
from contracts.results import Other
"""
    )

    assert dict(scope.import_identities) == {
        "package": "package",
        "branch_alias": "another.branch",
        "top_level": "top_level",
        "ImportedResult": "contracts.results:Result",
        "Other": "contracts.results:Other",
    }
    assert dict(scope.annotation_bindings) == dict(scope.import_identities)
    assert "annotations" not in scope.import_identities
    assert dict(scope.definitions) == {}


def test_direct_top_level_definitions_are_exposed_by_local_binding_name() -> None:
    tree, scope = _resolve(
        '''"""module docstring"""

class Result:
    pass

def execute():
    pass

async def execute_async():
    pass
'''
    )

    result, execute, execute_async = tree.body[1:]
    assert dict(scope.import_identities) == {}
    assert dict(scope.definitions) == {
        "Result": result,
        "execute": execute,
        "execute_async": execute_async,
    }
    assert scope.definitions["Result"] is result
    assert scope.definitions["execute"] is execute
    assert scope.definitions["execute_async"] is execute_async


def test_module_name_is_preserved_for_unambiguous_local_identity_derivation() -> None:
    _, scope = _resolve(
        "class Result:\n    pass\n",
        module_name="vendor.devices.transfer",
    )

    assert scope.module_name == "vendor.devices.transfer"
    assert f"{scope.module_name}:Result" == "vendor.devices.transfer:Result"
    assert "Result" not in scope.import_identities
    assert "Result" in scope.definitions


def test_later_definitions_replace_import_proofs_and_each_other() -> None:
    tree, scope = _resolve(
        """
from first import Result, execute, execute_async

class Result:
    pass

def execute():
    pass

async def execute_async():
    pass
"""
    )

    assert dict(scope.import_identities) == {}
    assert dict(scope.definitions) == {
        "Result": tree.body[1],
        "execute": tree.body[2],
        "execute_async": tree.body[3],
    }


def test_later_import_replaces_a_local_definition_proof() -> None:
    _, scope = _resolve(
        """
class Result:
    pass

from final_contract import Result
"""
    )

    assert dict(scope.import_identities) == {"Result": "final_contract:Result"}
    assert dict(scope.definitions) == {}


@pytest.mark.parametrize(
    "shadowing_statement",
    [
        pytest.param("Token = value", id="assign"),
        pytest.param("left = Token = value", id="chained-assign"),
        pytest.param("Token: Contract", id="annotation-only-assign"),
        pytest.param("Token: Contract = value", id="annotated-assign"),
        pytest.param("Token += value", id="augmented-assign"),
        pytest.param("del Token", id="delete"),
        pytest.param("(Token := value)", id="named-expression"),
    ],
)
def test_direct_non_definition_binding_invalidates_an_older_import_proof(
    shadowing_statement: str,
) -> None:
    _, scope = _resolve(f"from trusted import Token\n{shadowing_statement}\n")

    assert "Token" not in scope.import_identities
    assert "Token" not in scope.definitions


def test_destructuring_assignment_invalidates_every_bound_local_name() -> None:
    _, scope = _resolve(
        """
from trusted import first, second, rest
(first, [second, *rest]) = values
"""
    )

    assert dict(scope.import_identities) == {}
    assert dict(scope.definitions) == {}


@pytest.mark.parametrize(
    "conditional_binding",
    [
        pytest.param("if flag:\n    Token = value", id="if-assign"),
        pytest.param("while flag:\n    Token = value", id="while-assign"),
        pytest.param("for Token in values:\n    pass", id="for-target"),
        pytest.param("with manager() as Token:\n    pass", id="with-target"),
        pytest.param(
            "try:\n    pass\nexcept Error as Token:\n    pass",
            id="except-target",
        ),
        pytest.param(
            "match value:\n    case Token:\n        pass",
            id="match-capture",
        ),
        pytest.param(
            "if flag:\n    from conditional import Token",
            id="conditional-import",
        ),
        pytest.param(
            "if flag:\n    class Token:\n        pass",
            id="conditional-class",
        ),
        pytest.param(
            "if flag:\n    def Token():\n        pass",
            id="conditional-function",
        ),
        pytest.param("if flag:\n    del Token", id="conditional-delete"),
    ],
)
def test_possible_compound_statement_binding_removes_an_unconditional_proof(
    conditional_binding: str,
) -> None:
    _, scope = _resolve(f"from trusted import Token\n{conditional_binding}\n")

    assert "Token" not in scope.import_identities
    assert "Token" not in scope.definitions
    assert "Token" in scope.annotation_bindings


def test_conditional_import_or_definition_never_establishes_a_proven_binding() -> None:
    _, scope = _resolve(
        """
if flag:
    from conditional import Imported
if other_flag:
    class Result:
        pass
if third_flag:
    async def execute():
        pass
"""
    )

    assert dict(scope.import_identities) == {}
    assert dict(scope.definitions) == {}


def test_later_unconditional_bindings_reestablish_proof_after_ambiguity() -> None:
    tree, scope = _resolve(
        """
from initial import Imported, Result
if flag:
    Imported = dynamic_value
if flag:
    Result = dynamic_value
from final import Imported
class Result:
    pass
"""
    )

    assert dict(scope.import_identities) == {"Imported": "final:Imported"}
    assert dict(scope.definitions) == {"Result": tree.body[-1]}
    assert scope.annotation_bindings["Imported"] == "final:Imported"
    assert "Result" in scope.annotation_bindings


def test_nested_lexical_scopes_cannot_supply_or_shadow_module_identities() -> None:
    _, scope = _resolve(
        """
from trusted import Token

def action():
    from nested_function import Token
    class FunctionLocal:
        pass
    global ModuleMutationIfCalled
    ModuleMutationIfCalled = Token

class Container:
    from nested_class import Token
    class ClassLocal:
        pass
"""
    )

    assert dict(scope.import_identities) == {"Token": "trusted:Token"}
    assert set(scope.definitions) == {"action", "Container"}
    assert "FunctionLocal" not in scope.definitions
    assert "ClassLocal" not in scope.definitions
    assert "ModuleMutationIfCalled" not in scope.definitions


def test_nested_import_deletion_case_rejects_the_old_ast_walk_answer() -> None:
    """旧 ``ast.walk`` 会错误地把函数内的 evil identity 覆盖到模块作用域。"""

    _, scope = _resolve(
        """
from trusted_contract import Result

def action():
    from evil_nested_contract import Result
    return Result
"""
    )

    assert scope.import_identities["Result"] == "trusted_contract:Result"
    assert "evil_nested_contract:Result" not in scope.import_identities.values()


def test_compound_statement_without_a_binding_preserves_existing_proof() -> None:
    _, scope = _resolve(
        """
from trusted import Token
if flag:
    pass
while other_flag:
    break
try:
    expression()
finally:
    cleanup()
"""
    )

    assert dict(scope.import_identities) == {"Token": "trusted:Token"}


@pytest.mark.parametrize(
    "shadowing_source",
    [
        pytest.param("list = attacker", id="assign"),
        pytest.param("list: object = attacker", id="annassign"),
        pytest.param("(list := attacker)", id="named-expression"),
        pytest.param("if flag:\n    list = attacker", id="conditional"),
        pytest.param("class list:\n    pass", id="class-definition"),
        pytest.param("def list():\n    pass", id="function-definition"),
    ],
)
def test_annotation_binding_view_blocks_shadowed_builtin_names(
    shadowing_source: str,
) -> None:
    source = f"{shadowing_source}\ndef workflow(*, value: list[int]):\n    pass\n"
    _, scope = _resolve(source)

    assert "list" not in scope.import_identities
    assert "list" in scope.annotation_bindings
    with pytest.raises(AnnotationSchemaError) as caught:
        _parse_workflow_value(source)

    assert caught.value.code == "invalid_annotation"


def test_unconditional_delete_restores_builtin_lookup_after_local_shadow() -> None:
    source = """
list = attacker
del list
def workflow(*, value: list[int]):
    pass
"""
    _, scope = _resolve(source)

    assert "list" not in scope.annotation_bindings
    assert _parse_workflow_value(source).to_dict()["schema"] == {
        "type": "array",
        "items": {"type": "integer"},
    }


@pytest.mark.parametrize(
    "delete_statement",
    [
        pytest.param("del container[(list := key)]", id="subscript"),
        pytest.param("del (list := container).member", id="attribute"),
        pytest.param(
            "del list, container[(list := key)]",
            id="mixed-shadow-wins",
        ),
    ],
)
def test_delete_target_evaluation_keeps_named_expression_builtin_shadow(
    delete_statement: str,
) -> None:
    source = f"{delete_statement}\ndef workflow(*, value: list[int]):\n    pass\n"
    _, scope = _resolve(source)

    assert "list" in scope.annotation_bindings
    with pytest.raises(AnnotationSchemaError):
        _parse_workflow_value(source)


def test_action_result_parser_uses_the_same_builtin_shadow_barrier() -> None:
    _, scope = _resolve(
        """
from typing import TypedDict
list = attacker
class Result(TypedDict):
    values: list[int]
"""
    )
    declaration = scope.definitions["Result"]
    assert isinstance(declaration, ast.ClassDef)

    with pytest.raises(ActionResultSchemaError) as caught:
        parse_action_result_declaration(
            declaration,
            imports=scope.annotation_bindings,
        )

    assert caught.value.code == "invalid_action_result"


@pytest.mark.parametrize(
    "class_body",
    [
        pytest.param('global Token\nToken = "evil"', id="assign"),
        pytest.param(
            "global Token\nfrom evil_contract import Token",
            id="import",
        ),
        pytest.param("global Token\ndel Token", id="delete"),
    ],
)
def test_class_body_global_binding_invalidates_module_import_proof(
    class_body: str,
) -> None:
    indented_body = "\n".join(f"    {line}" for line in class_body.splitlines())
    _, scope = _resolve(
        f"from trusted_contract import Token\nclass Container:\n{indented_body}\n"
    )

    assert "Token" not in scope.import_identities
    assert "Token" not in scope.definitions
    assert "Token" in scope.annotation_bindings


def test_function_body_global_binding_is_not_an_import_time_module_proof() -> None:
    _, scope = _resolve(
        """
from trusted_contract import Token
def mutate_if_called():
    global Token
    Token = "evil"
"""
    )

    assert scope.import_identities["Token"] == "trusted_contract:Token"
    assert scope.annotation_bindings["Token"] == "trusted_contract:Token"


@pytest.mark.parametrize(
    "nested_class_source",
    [
        pytest.param(
            """
class Outer:
    class Inner:
        global Token
        Token = "evil"
""",
            id="one-level",
        ),
        pytest.param(
            """
class Outer:
    class Middle:
        class Inner:
            global Token
            Token = "evil"
""",
            id="multiple-levels",
        ),
        pytest.param(
            """
class Outer:
    if flag:
        class Inner:
            global Token
            Token = "evil"
""",
            id="compound",
        ),
    ],
)
def test_nested_class_global_binding_invalidates_module_import_proof(
    nested_class_source: str,
) -> None:
    _, scope = _resolve(f"from trusted_contract import Token\n{nested_class_source}")

    assert "Token" not in scope.import_identities
    assert "Token" in scope.annotation_bindings


def test_nested_class_inside_function_body_does_not_run_at_module_import() -> None:
    _, scope = _resolve(
        """
from trusted_contract import Token
def mutate_if_called():
    class Inner:
        global Token
        Token = "evil"
"""
    )

    assert scope.import_identities["Token"] == "trusted_contract:Token"
    assert scope.annotation_bindings["Token"] == "trusted_contract:Token"
