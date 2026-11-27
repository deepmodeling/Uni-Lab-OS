"""耗时模型的输入边界、历史分组和剩余时间契约。"""

from __future__ import annotations

import math
from collections import deque

import pytest

from unilabos.app.scheduler import timing as timing_module
from unilabos.app.scheduler.timing import (
    ActionTimingSpec,
    TimingModel,
    constant_speed_cycles,
    finite_seconds,
)


def _spec(**kwargs: object) -> ActionTimingSpec:
    return ActionTimingSpec(feature_paths=(), **kwargs)


@pytest.mark.parametrize(
    ("value", "allow_zero", "expected"),
    [
        (2, False, 2.0),
        ("2.5", False, 2.5),
        (0, False, None),
        (0, True, 0.0),
        (True, True, None),
        (None, True, None),
        (float("nan"), True, None),
        (float("inf"), True, None),
    ],
)
def test_finite_seconds_accepts_only_finite_numeric_values(
    value: object, allow_zero: bool, expected: float | None
) -> None:
    assert finite_seconds(value, allow_zero=allow_zero) == expected


def test_constant_speed_cycles_uses_seconds_and_rejects_invalid_inputs() -> None:
    assert constant_speed_cycles({"cycles": 2, "rpm": 60}) == 2.0

    for params in (
        {"cycles": 0, "rpm": 60},
        {"cycles": 2, "rpm": 0},
        {"cycles": "bad", "rpm": 60},
        {"cycles": 2, "rpm": float("inf")},
    ):
        with pytest.raises(ValueError):
            constant_speed_cycles(params)

    with pytest.raises(ValueError, match="无效时长"):
        constant_speed_cycles({"cycles": 1e308, "rpm": 1})


def test_timing_spec_from_dict_only_allows_registered_formula() -> None:
    spec = ActionTimingSpec.from_dict(
        {
            "version": "cycles-v1",
            "formula": "constant_speed_cycles",
            "feature_paths": [],
        }
    )
    assert spec.formula is constant_speed_cycles

    with pytest.raises(ValueError, match="允许清单"):
        ActionTimingSpec.from_dict({"formula": "__import__('os').system"})


@pytest.mark.parametrize(
    "factory",
    [
        lambda: ActionTimingSpec(version=""),
        lambda: ActionTimingSpec(timing_boundary=""),
        lambda: ActionTimingSpec(end_mode=""),
        lambda: ActionTimingSpec(formula=object()),
        lambda: ActionTimingSpec(feature_paths="cycles"),
        lambda: ActionTimingSpec(feature_paths=(1,)),
        lambda: ActionTimingSpec(parameter_bounds={"cycles": (2, 1)}),
        lambda: ActionTimingSpec(parameter_bounds={"cycles": (1,)}),
        lambda: ActionTimingSpec(parameter_bounds={"cycles": (True, 2)}),
        lambda: ActionTimingSpec(parameter_bounds={"cycles": (0, float("inf"))}),
        lambda: ActionTimingSpec(default_s=0),
    ],
)
def test_timing_spec_rejects_invalid_contracts(factory) -> None:
    with pytest.raises(ValueError):
        factory()


def test_timing_spec_freezes_paths_and_bounds() -> None:
    spec = ActionTimingSpec(
        feature_paths=["cycles"],
        calibration_paths=["rpm"],
        parameter_bounds={"cycles": (1, 5)},
    )

    assert spec.feature_paths == ("cycles",)
    assert spec.calibration_paths == ("rpm",)
    assert spec.parameter_bounds["cycles"] == (1.0, 5.0)
    with pytest.raises(TypeError):
        spec.parameter_bounds["new"] = (0.0, 1.0)  # type: ignore[index]


def test_model_constructor_checks_quantile_and_sample_limits() -> None:
    with pytest.raises(ValueError):
        TimingModel(quantile=0.4)
    with pytest.raises(ValueError):
        TimingModel(quantile=float("nan"))
    with pytest.raises(ValueError):
        TimingModel(min_samples=0)
    with pytest.raises(ValueError):
        TimingModel(sample_limit=0)
    with pytest.raises(ValueError):
        TimingModel(min_samples=3, sample_limit=2)


def test_estimate_reports_unknown_and_default_without_fabricating_zero() -> None:
    unknown = TimingModel().estimate("/device/run", {"x": 1}, spec=_spec())
    assert unknown.point_s is None
    assert not unknown.known
    assert unknown.source == "unknown"

    default = TimingModel().estimate(
        "/device/run", {}, spec=_spec(default_s=3.0), fallback_s=7.0
    )
    assert (default.point_s, default.planned_s, default.source) == (3.0, 3.0, "default")


def test_estimate_validates_feature_paths_and_marks_tentative_parameters() -> None:
    model = TimingModel()
    estimate = model.estimate(
        "/device/run",
        {},
        spec=ActionTimingSpec(feature_paths=("cycles",), default_s=2.0),
        parameters_final=False,
    )

    assert estimate.point_s is None
    assert estimate.source == "unknown"
    assert "缺少估时参数" in estimate.reason

    tentative = model.estimate(
        "/device/run", {}, spec=_spec(default_s=2.0), parameters_final=False
    )
    assert tentative.confidence == "tentative"
    assert tentative.parameters_final is False


def test_parameter_bounds_and_formula_fail_closed() -> None:
    bounded = ActionTimingSpec(feature_paths=(), parameter_bounds={"load": (1, 3)})
    model = TimingModel()
    for params in ({}, {"load": "bad"}, {"load": True}, {"load": 4}, {"load": float("nan")}):
        estimate = model.estimate("/device/run", params, spec=bounded, fallback_s=2.0)
        assert estimate.point_s is None
        assert estimate.source == "unknown"

    bad_formula = ActionTimingSpec(feature_paths=(), formula=lambda _params: 0.0)
    estimate = model.estimate("/device/run", {}, spec=bad_formula)
    assert estimate.point_s is None
    assert "非正" in estimate.reason

    assert model.estimate(
        "/device/run", {}, spec=_spec(), declared_s=0.0, fallback_s=2.0
    ).point_s is None
    assert not model.observe(
        "/device/run",
        2.0,
        {},
        spec=ActionTimingSpec(feature_paths=("missing",)),
    )


def test_historical_estimates_are_grouped_and_quantiled() -> None:
    model = TimingModel(quantile=0.9, min_samples=2)
    spec = ActionTimingSpec(feature_paths=("size",))

    assert model.observe("/device/run", 2.0, {"size": 1}, spec=spec, observation_id="a")
    assert model.observe("/device/run", 6.0, {"size": 1}, spec=spec, observation_id="b")
    estimate = model.estimate("/device/run", {"size": 1}, spec=spec)

    assert estimate.source == "historical_group"
    assert estimate.samples == 2
    assert estimate.point_s == 4.0
    assert estimate.planned_s == 6.0

    other_group = model.estimate("/device/run", {"size": 2}, spec=spec)
    assert other_group.source == "unknown"


def test_formula_model_uses_residuals_and_calibration_margin() -> None:
    model = TimingModel(quantile=0.9, min_samples=1)
    spec = ActionTimingSpec(
        version="cycles-v1",
        feature_paths=("cycles", "rpm"),
        calibration_paths=(),
        formula=constant_speed_cycles,
    )
    params = {"cycles": 1, "rpm": 60}

    assert model.observe("/device/spin", 3.0, params, spec=spec, observation_id="fit")
    assert model.observe("/device/spin", 5.0, params, spec=spec, observation_id="fit-2")
    estimate = model.estimate("/device/spin", params, spec=spec)
    assert estimate.source == "formula_calibrated"
    assert estimate.point_s == 4.0
    assert estimate.planned_s == 5.0

    assert model.observe(
        "/device/spin", 4.0, params, spec=spec, calibration=True, observation_id="cal"
    )
    calibrated = model.estimate("/device/spin", params, spec=spec)
    assert calibrated.confidence == "empirical_calibrated"
    assert calibrated.calibration_samples == 1
    assert calibrated.planned_s == calibrated.point_s


def test_observation_contract_rejects_failures_and_conflicting_ids() -> None:
    model = TimingModel()
    spec = _spec()

    assert not model.observe("/device/run", 2.0, {}, spec=spec, outcome="failed")
    assert not model.observe("/device/run", 0.0, {}, spec=spec)
    assert model.observe("/device/run", 2.0, {}, spec=spec, observation_id="same")
    assert not model.observe("/device/run", 2.0, {}, spec=spec, observation_id="same")
    with pytest.raises(ValueError, match="不同内容"):
        model.observe("/device/run", 3.0, {}, spec=spec, observation_id="same")

    calibration_model = TimingModel()
    assert not calibration_model.observe(
        "/device/run", 2.0, {}, spec=ActionTimingSpec(default_s=1.0), calibration=True
    )


def test_training_observation_invalidates_old_calibration_samples() -> None:
    model = TimingModel(min_samples=1)
    spec = ActionTimingSpec(
        feature_paths=(), calibration_paths=(), formula=lambda _params: 1.0
    )
    assert model.observe("/device/run", 2.0, {}, spec=spec, observation_id="fit")
    assert model.observe(
        "/device/run", 3.0, {}, spec=spec, calibration=True, observation_id="cal"
    )
    assert model._groups  # calibration is retained until the next fit sample.
    assert model.observe("/device/run", 4.0, {}, spec=spec, observation_id="fit-2")
    assert all(not group.calibration_errors for group in model._groups.values())


def test_remaining_time_requires_running_evidence_and_supports_progress() -> None:
    model = TimingModel(min_samples=1, quantile=0.9)
    spec = _spec()
    for observation_id, actual in (("a", 10.0), ("b", 20.0), ("c", 30.0)):
        assert model.observe("/device/run", actual, {}, spec=spec, observation_id=observation_id)

    conditional = model.remaining(
        "/device/run", {}, spec=spec, elapsed_s=15.0, still_running=True
    )
    assert conditional.source == "conditional_history"
    assert conditional.point_s == 10.0
    assert conditional.planned_s == 15.0

    completed = model.remaining(
        "/device/run", {}, spec=spec, elapsed_s=15.0, completed=True
    )
    assert completed.point_s == completed.planned_s == 0.0
    assert completed.source == "completed"

    unverified = model.remaining(
        "/device/run", {}, spec=spec, elapsed_s=15.0, still_running=False
    )
    assert unverified.point_s is None
    assert "核实物理状态" in unverified.reason

    progress = model.remaining(
        "/device/run", {}, spec=spec, elapsed_s=15.0, progress_remaining_s=4.0
    )
    assert (progress.point_s, progress.source) == (4.0, "progress")

    invalid_progress = model.remaining(
        "/device/run", {}, spec=spec, elapsed_s=15.0, progress_remaining_s=0.0
    )
    assert invalid_progress.point_s is None
    assert "零不能证明完成" in invalid_progress.reason


def test_remaining_time_handles_invalid_elapsed_and_cross_group_formula() -> None:
    model = TimingModel()
    spec = _spec(default_s=3.0)
    invalid = model.remaining("/device/run", {}, spec=spec, elapsed_s=-1.0)
    assert invalid.point_s is None
    assert "非负数" in invalid.reason

    formula_spec = ActionTimingSpec(
        feature_paths=("cycles",),
        calibration_paths=("rpm",),
        formula=constant_speed_cycles,
    )
    result = model.remaining(
        "/device/spin",
        {"cycles": 1, "rpm": 60},
        spec=formula_spec,
        elapsed_s=1.0,
    )
    assert result.point_s is None
    assert "跨参数残差组" in result.reason


def test_estimate_rejects_nonfinite_values_after_model_calibration() -> None:
    """损坏的历史状态也不能把无穷时长送入排程器。"""

    model = TimingModel()
    spec = ActionTimingSpec(feature_paths=(), formula=lambda _params: 1e308)
    key, _ = model._context("/device/run", {}, spec, "", "")
    model._groups[key] = timing_module._Samples(
        deque([1.0]), deque([1e308]), deque()
    )
    overflow = model.estimate("/device/run", {}, spec=spec)
    assert overflow.point_s is None
    assert "校准后" in overflow.reason

    finite_spec = ActionTimingSpec(feature_paths=(), formula=lambda _params: 1.0)
    finite_key, _ = model._context("/device/calibrated", {}, finite_spec, "", "")
    model._groups[finite_key] = timing_module._Samples(
        deque([1.0]), deque([0.0]), deque([float("inf")])
    )
    invalid_plan = model.estimate("/device/calibrated", {}, spec=finite_spec)
    assert invalid_plan.point_s is None
    assert "计划时长" in invalid_plan.reason
