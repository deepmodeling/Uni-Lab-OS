"""动作预计时长（A01，``@action(estimate_duration_fixed / estimate_duration_express)``）。"""

from __future__ import annotations

import textwrap

import pytest

from unilabos.registry.action_duration import (
    DEFAULT_ESTIMATE_DURATION_S,
    DurationExpressionError,
    normalize_estimate_duration_express,
    normalize_estimate_duration_fixed,
    resolve_action_estimate,
)
from unilabos.registry.action_timeout import normalize_execution_timeout
from unilabos.registry.ast_registry_scanner import _parse_file
from unilabos.registry.decorators import action, get_action_meta
from unilabos.registry.registry import _apply_action_timeouts


def test_expression_accepts_plain_and_v1_braced_parameter_names() -> None:
    assert normalize_estimate_duration_express("{cycles} * 2", action_parameter_names=["cycles"]) == "cycles * 2"
    assert normalize_estimate_duration_express("cycles*2 + 10", action_parameter_names=["self", "cycles"]) == "cycles * 2 + 10"
    assert normalize_estimate_duration_express("", action_parameter_names=["cycles"]) == ""
    assert normalize_estimate_duration_express(None) == ""
    # 软超时同一套语法，也认花括号
    assert normalize_execution_timeout("{duration} * 1.5", action_parameter_names=["duration"]) == "duration * 1.5"


@pytest.mark.parametrize(("value", "error", "match"), [
    ("volume ** 2", DurationExpressionError, r"只支持 \+ - \* /"),
    ("volume(", DurationExpressionError, "语法错误"),
    ("unknown * 2", ValueError, "非动作入参: unknown"),
    ("'5'", DurationExpressionError, "只允许数字字面量"),
    (5, TypeError, "必须是字符串"),
])
def test_expression_rejects_anything_outside_the_whitelist(value, error, match) -> None:
    with pytest.raises(error, match=match):
        normalize_estimate_duration_express(value, action_parameter_names=["volume"])


@pytest.mark.parametrize(("value", "error"), [(-1, ValueError), (True, TypeError), ("60", TypeError), (float("inf"), ValueError)])
def test_fixed_fallback_must_be_a_finite_non_negative_number(value, error) -> None:
    with pytest.raises(error):
        normalize_estimate_duration_fixed(value)
    assert normalize_estimate_duration_fixed(0) == 0.0
    assert normalize_estimate_duration_fixed(None) is None


def test_resolution_order_expression_then_fixed_then_default() -> None:
    entry = {"estimate_duration_express": "cycles * 2 + 10", "estimate_duration_fixed": 30.0, "goal_default": {"cycles": 5}}
    assert resolve_action_estimate(entry, {}) == {"estimated_s": 20.0, "estimate_source": "expression", "error": None}
    assert resolve_action_estimate(entry, {"cycles": "7"})["estimated_s"] == 24.0  # 字符串数字也能算

    degraded = resolve_action_estimate(entry, {"cycles": "fast"})
    assert (degraded["estimated_s"], degraded["estimate_source"]) == (30.0, "fixed")
    assert "不是数字" in degraded["error"]
    negative = resolve_action_estimate(entry, {"cycles": -20})
    assert negative["estimate_source"] == "fixed" and "不小于 0" in negative["error"]

    assert resolve_action_estimate({}, {}) == {
        "estimated_s": DEFAULT_ESTIMATE_DURATION_S, "estimate_source": "default", "error": None}
    assert resolve_action_estimate(None, None)["estimate_source"] == "default"
    # 显式 None：不提供兜底
    assert resolve_action_estimate({"estimate_duration_fixed": None}, {}) == {
        "estimated_s": None, "estimate_source": None, "error": None}
    missing = resolve_action_estimate({"estimate_duration_express": "cycles * 2", "estimate_duration_fixed": None}, {})
    assert missing["estimated_s"] is None and "缺少参数 'cycles'" in missing["error"]


def test_decorator_records_only_declared_fields() -> None:
    @action(estimate_duration_fixed=30, estimate_duration_express="{cycles} * 2")
    def spin(self, cycles: int = 1):
        return cycles

    @action()
    def idle(self):
        return None

    @action(estimate_duration_fixed=None)
    def manual(self):
        return None

    meta = get_action_meta(spin)
    assert meta["estimate_duration_fixed"] == 30.0 and meta["estimate_duration_express"] == "cycles * 2"
    assert "estimate_duration_fixed" not in get_action_meta(idle) and "estimate_duration_express" not in get_action_meta(idle)
    assert get_action_meta(manual)["estimate_duration_fixed"] is None
    with pytest.raises(ValueError, match="非动作入参: speed"):
        action(estimate_duration_express="speed * 2")(lambda self, cycles=1: cycles)


def test_ast_scanner_and_registry_entries_carry_the_estimate(tmp_path) -> None:
    source = tmp_path / "spinner.py"
    source.write_text(textwrap.dedent('''
        from unilabos.registry.decorators import action, device

        @device(id="spinner_demo", category=["demo"])
        class Spinner:
            @action(estimate_duration_fixed=30, estimate_duration_express="{cycles} * 2")
            def spin(self, cycles: int = 1):
                return cycles

            @action()
            def idle(self):
                return None
    '''), encoding="utf-8")
    devices, _resources, _workflows = _parse_file(source, tmp_path)
    actions = devices[0]["actions"]
    spin_args = actions["spin"]["action_args"]
    assert spin_args["estimate_duration_fixed"] == 30.0 and spin_args["estimate_duration_express"] == "cycles * 2"
    assert "estimate_duration_fixed" not in actions["idle"]["action_args"]

    entry: dict = {}
    _apply_action_timeouts(entry, spin_args, action_parameter_names=["cycles"], action_name="spin")
    assert entry == {"estimate_duration_fixed": 30.0, "estimate_duration_express": "cycles * 2"}
    untouched: dict = {}
    _apply_action_timeouts(untouched, actions["idle"]["action_args"], action_parameter_names=[], action_name="idle")
    assert untouched == {}
