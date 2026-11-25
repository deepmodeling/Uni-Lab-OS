"""框架按类型注入的动作上下文（``ActionContext``）不属于动作接口。

注解为 ``ActionContext`` / ``Optional[ActionContext]`` / ``ActionContext | None`` 的参数，不论叫什么名字，都不进
goal schema、必填列表、``placeholder_keys``、生成的端口、物料锁参数与 ``goal_default``；AST 扫描、装饰器与运行时
签名推断同一规则。参数叫 ``action_context`` 却没有这个注解（已删除的按名字写法）是注册错误。
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any, Optional

import pytest

from unilabos.client.host.runtime.action import ActionContext
from unilabos.registry.action_contract_schema import parse_action_contract
from unilabos.registry.action_options import RemovedActionOptionError
from unilabos.registry.ast_registry_scanner import _parse_file
from unilabos.registry.decorators import action
from unilabos.registry.placeholder_type import ResourceSlot
from unilabos.registry.registry import Registry
from unilabos.utils.import_manager import default_manager

_HEADER = '''
from typing import Optional
from unilabos.client.host.runtime.action import ActionContext
from unilabos.registry.decorators import action, device
from unilabos.registry.placeholder_type import ResourceSlot
'''


def _driver(body: str, *, header: str = _HEADER) -> str:
    return textwrap.dedent(header) + textwrap.dedent(
        '''
        @device(id="ctx_driver", category=["test"])
        class Driver:
        '''
    ) + textwrap.indent(textwrap.dedent(body), "    ")


def _entry(tmp_path: Path, body: str, *, header: str = _HEADER) -> dict[str, Any]:
    path = tmp_path / "ctx_driver.py"
    path.write_text(_driver(body, header=header), encoding="utf-8")
    devices, _resources, _workflows = _parse_file(path, tmp_path)
    return Registry()._build_device_entry_from_ast("ctx_driver", devices[0])["class"]["action_value_mappings"]


_TYPED_ACTIONS = '''
@action(goal_default={"speed": 2.0})
def run(self, sample: ResourceSlot, speed: float, ctx: ActionContext) -> None:
    pass

@action()
def wait(self, sample: ResourceSlot, feedback: Optional[ActionContext], seconds: int = 1) -> None:
    pass

@action()
def cool(self, sample: ResourceSlot, handle: ActionContext | None = None) -> None:
    pass

def stir(self, sample: ResourceSlot, rpm: int, context: Optional[ActionContext] = None) -> None:
    pass
'''


@pytest.mark.parametrize(
    ("action_name", "context_name", "parameters"),
    [
        ("run", "ctx", ["sample", "speed"]),
        ("wait", "feedback", ["sample", "seconds"]),
        ("cool", "handle", ["sample"]),
        ("auto-stir", "context", ["sample", "rpm"]),
    ],
)
def test_ast_scan_hides_action_context_parameters_by_type(
    tmp_path: Path, action_name: str, context_name: str, parameters: list[str]
) -> None:
    entry = _entry(tmp_path, _TYPED_ACTIONS)[action_name]
    goal = entry["schema"]["properties"]["goal"]
    assert list(goal["properties"]) == parameters
    assert context_name not in goal["required"]
    assert entry["schema"]["x-unilabos-action-contract"]["input_order"] == parameters
    assert context_name not in entry["placeholder_keys"]
    assert context_name not in entry["goal_default"]
    assert entry["materials_need_lock"] == ["sample"]
    handle_keys = {handle["handler_key"] for handle in entry["handles"].get("input", [])}
    handle_keys |= {handle["handler_key"] for handle in entry["handles"].get("output", [])}
    assert context_name not in handle_keys
    assert "sample" in handle_keys


def test_ast_scan_metadata_params_skip_typed_context(tmp_path: Path) -> None:
    path = tmp_path / "ctx_driver.py"
    path.write_text(_driver(_TYPED_ACTIONS), encoding="utf-8")
    devices, _resources, _workflows = _parse_file(path, tmp_path)
    actions = devices[0]["actions"]
    assert [param["name"] for param in actions["run"]["params"]] == ["sample", "speed"]
    assert [param["name"] for param in actions["wait"]["params"]] == ["sample", "seconds"]
    assert [param["name"] for param in devices[0]["auto_methods"]["stir"]["params"]] == ["sample", "rpm"]


def test_ast_scan_recognises_module_import_and_string_annotations(tmp_path: Path) -> None:
    header = '''
    import typing
    import unilabos.client.host.runtime.action as runtime_action
    from unilabos.registry.decorators import action, device
    from unilabos.registry.placeholder_type import ResourceSlot
    '''
    entry = _entry(
        tmp_path,
        '''
        @action()
        def run(self, sample: ResourceSlot, ctx: typing.Optional[runtime_action.ActionContext]) -> None:
            pass

        @action()
        def later(self, sample: ResourceSlot, ctx: "runtime_action.ActionContext") -> None:
            pass
        ''',
        header=header,
    )
    for name in ("run", "later"):
        assert list(entry[name]["schema"]["properties"]["goal"]["properties"]) == ["sample"]


def test_parameter_named_action_context_without_the_type_is_a_registration_error(tmp_path: Path) -> None:
    for body in (
        '''
        @action()
        def run(self, sample: ResourceSlot, action_context=None) -> None:
            pass
        ''',
        '''
        def stir(self, sample: ResourceSlot, action_context: dict = None) -> None:
            pass
        ''',
    ):
        path = tmp_path / "ctx_driver.py"
        path.write_text(_driver(body), encoding="utf-8")
        with pytest.raises(RemovedActionOptionError, match=r"action_context.*Optional\[ActionContext\]"):
            _parse_file(path, tmp_path)


def test_typed_parameter_named_action_context_is_still_injected(tmp_path: Path) -> None:
    entry = _entry(
        tmp_path,
        '''
        @action()
        def run(self, sample: ResourceSlot, action_context: Optional[ActionContext] = None) -> None:
            pass
        ''',
    )
    assert list(entry["run"]["schema"]["properties"]["goal"]["properties"]) == ["sample"]


def test_decorator_rejects_untyped_action_context_and_hides_typed_ones() -> None:
    with pytest.raises(RemovedActionOptionError, match=r"Optional\[ActionContext\]"):

        @action()
        def legacy(self, sample: ResourceSlot, action_context=None) -> None:
            pass

    # 上下文参数不是动作参数：placeholder_keys 不能写它
    with pytest.raises(ValueError, match="ctx"):

        @action(placeholder_keys={"ctx": "unilabos_resources"})
        def bad(self, sample: ResourceSlot, ctx: Optional[ActionContext] = None) -> None:
            pass

    @action()
    def typed(self, sample: ResourceSlot, ctx: ActionContext | None = None) -> None:
        pass

    assert typed._action_registry_meta["placeholder_keys"] == {}


def test_runtime_signature_inference_skips_typed_context() -> None:
    class Driver:
        def run(self, sample: ResourceSlot, speed: float, ctx: Optional[ActionContext] = None) -> None:
            pass

        def legacy(self, speed: float, action_context=None) -> None:
            pass

    assert [arg["name"] for arg in default_manager._analyze_method_signature(Driver().run)["args"]] == [
        "sample",
        "speed",
    ]
    # 不带类型的同名参数不再特殊：当作普通参数（注册时会因上面的规则报错）
    assert [arg["name"] for arg in default_manager._analyze_method_signature(Driver().legacy)["args"]] == [
        "speed",
        "action_context",
    ]


def test_action_contract_parser_skips_typed_context() -> None:
    import ast

    source = textwrap.dedent(
        '''
        from typing import Optional
        from unilabos.client.host.runtime.action import ActionContext

        class Driver:
            def run(self, speed: float, ctx: Optional[ActionContext] = None) -> dict:
                return {}
        '''
    )
    module = ast.parse(source)
    function = module.body[2].body[0]
    contract = parse_action_contract(module, function, module_name="ctx_contract_module")
    assert [item["name"] for item in contract.to_dict()["input_contract"]["parameters"]] == ["speed"]
