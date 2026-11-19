"""``@action(timeout=..., execution_timeout=...)`` 注册表合同测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from unilabos.registry.action_timeout import (
    TimeoutExpressionError,
    evaluate_execution_timeout,
    execution_timeout_parameter_names,
    normalize_action_timeout,
    normalize_execution_timeout,
)
from unilabos.registry.ast_registry_scanner import _parse_file
from unilabos.registry.decorators import action, get_action_meta
from unilabos.registry.registry import Registry
from unilabos.server.backend.legacy_adaptor.sync.templates import _action_definition


# ── 归一化与求值 ────────────────────────────────────────────────


def test_hard_timeout_must_be_positive_seconds() -> None:
    assert normalize_action_timeout(None) is None
    assert normalize_action_timeout(600) == 600.0
    with pytest.raises(ValueError, match="有限正数"):
        normalize_action_timeout(0)
    with pytest.raises(TypeError, match="秒数"):
        normalize_action_timeout("600")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        normalize_action_timeout(True)


def test_execution_timeout_accepts_numbers_and_parameter_expressions() -> None:
    assert normalize_execution_timeout(90) == 90.0
    # 表达式归一化为规范字符串（空白 / 括号统一）
    assert (
        normalize_execution_timeout(
            "duration*1.5+ (30)", action_parameter_names=["self", "duration"]
        )
        == "duration * 1.5 + 30"
    )
    # 纯常量表达式直接折叠成秒数
    assert normalize_execution_timeout("(60 + 30) / 2") == 45.0
    assert execution_timeout_parameter_names("a * 2 + b / c - a") == ["a", "b", "c"]
    assert execution_timeout_parameter_names(42) == []


@pytest.mark.parametrize(
    "expression, message",
    [
        ("duration ** 2", "只支持 \\+ - \\* /"),
        ("max(duration, 1)", "只允许入参名"),
        ("duration.real", "只允许入参名"),
        ("'60'", "只允许数字字面量"),
        ("duration +", "语法错误"),
        ("", "不能为空"),
    ],
)
def test_execution_timeout_rejects_unsupported_expressions(
    expression: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        normalize_execution_timeout(expression, action_parameter_names=["duration"])


def test_execution_timeout_rejects_unknown_parameter_names() -> None:
    with pytest.raises(ValueError, match="非动作入参.*volume"):
        normalize_execution_timeout(
            "volume / 2", action_parameter_names=["self", "duration"]
        )


def test_evaluate_execution_timeout_with_real_arguments() -> None:
    spec = normalize_execution_timeout("duration * 1.5 + 30", action_parameter_names=["duration"])
    assert evaluate_execution_timeout(spec, {"duration": 60}) == 120.0
    # 字符串数字（HTTP 表单）可以参与运算
    assert evaluate_execution_timeout(spec, {"duration": "10"}) == 45.0
    assert evaluate_execution_timeout(15, {}) == 15.0
    assert evaluate_execution_timeout(None, {}) is None
    with pytest.raises(TimeoutExpressionError, match="缺少参数 'duration'"):
        evaluate_execution_timeout(spec, {})
    with pytest.raises(TimeoutExpressionError, match="不是数字"):
        evaluate_execution_timeout(spec, {"duration": "fast"})
    with pytest.raises(TimeoutExpressionError, match="除以零"):
        evaluate_execution_timeout("60 / n", {"n": 0})
    with pytest.raises(TimeoutExpressionError, match="有限正数"):
        evaluate_execution_timeout("duration - 100", {"duration": 10})


# ── 装饰器 ──────────────────────────────────────────────────────


def test_action_decorator_records_timeouts_only_when_declared() -> None:
    @action(timeout=600, execution_timeout="duration * 2 + 10")
    def heat(duration: float = 30.0) -> None:
        del duration

    meta = get_action_meta(heat)
    assert meta["timeout"] == 600.0
    assert meta["execution_timeout"] == "duration * 2 + 10"

    @action()
    def plain(duration: float = 30.0) -> None:
        del duration

    plain_meta = get_action_meta(plain)
    assert "timeout" not in plain_meta
    assert "execution_timeout" not in plain_meta

    with pytest.raises(ValueError, match="非动作入参.*volume"):

        @action(execution_timeout="volume * 2")
        def invalid(duration: float) -> None:
            del duration


# ── AST 扫描 → 注册表 → 上报定义 ────────────────────────────────


def _scan_entry(tmp_path: Path, source: str) -> tuple[dict, dict]:
    module_path = tmp_path / "timeout_driver.py"
    module_path.write_text(source, encoding="utf-8")
    devices, _resources, _workflows = _parse_file(module_path, tmp_path)
    ast_meta = devices[0]
    entry = Registry()._build_device_entry_from_ast("timeout_test", ast_meta)
    return ast_meta, entry


def test_ast_registry_roundtrip_publishes_timeout_contract(tmp_path: Path) -> None:
    ast_meta, entry = _scan_entry(
        tmp_path,
        '''
from unilabos.registry.decorators import action, device

@device(id="timeout_test", category=["test"])
class Driver:
    @action(timeout=600, execution_timeout="duration*1.5+30")
    def heat(self, duration: float = 60.0, ramp: float = 5.0) -> None:
        pass

    @action(execution_timeout=90)
    def stir(self, speed: int = 300) -> None:
        pass

    @action()
    def inspect(self) -> None:
        pass
''',
    )

    heat_args = ast_meta["actions"]["heat"]["action_args"]
    assert heat_args["timeout"] == 600.0
    assert heat_args["execution_timeout"] == "duration * 1.5 + 30"
    assert ast_meta["actions"]["inspect"]["action_args"]["timeout"] is None

    mappings = entry["class"]["action_value_mappings"]
    assert mappings["heat"]["timeout"] == 600.0
    assert mappings["heat"]["execution_timeout"] == "duration * 1.5 + 30"
    assert mappings["stir"]["execution_timeout"] == 90.0
    assert "timeout" not in mappings["stir"]
    assert "timeout" not in mappings["inspect"]
    assert "execution_timeout" not in mappings["inspect"]

    restored = json.loads(json.dumps(entry, default=str))
    assert restored["class"]["action_value_mappings"]["heat"]["execution_timeout"] == (
        "duration * 1.5 + 30"
    )

    # 注册表上报给调度权威 / 前端的动作定义同样带上超时声明
    definition = _action_definition(mappings["heat"])
    assert definition["timeout"] == 600.0
    assert definition["execution_timeout"] == "duration * 1.5 + 30"
    assert "timeout" not in _action_definition(mappings["inspect"])


def test_ast_rejects_expression_over_unknown_parameter(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="非动作入参.*volume"):
        _scan_entry(
            tmp_path,
            '''
from unilabos.registry.decorators import action, device

@device(id="timeout_test", category=["test"])
class Driver:
    @action(execution_timeout="volume * 2")
    def heat(self, duration: float = 60.0) -> None:
        pass
''',
        )
