"""估时规则文件的装载、校验和原子替换契约。"""

from __future__ import annotations

import json

import pytest

from unilabos.app.scheduler.estimation import DurationEstimator
from unilabos.app.scheduler.timing_config import load_timing_rules


def _write_json(tmp_path, value: object, name: str = "timing.json"):
    path = tmp_path / name
    path.write_text(json.dumps(value, allow_nan=False), encoding="utf-8")
    return path


def test_missing_path_is_a_noop() -> None:
    estimator = DurationEstimator()
    assert load_timing_rules(estimator, environ={}) == 0
    assert estimator.timing_specs == {}


def test_loads_rules_from_environment_and_expands_user_path(tmp_path, monkeypatch) -> None:
    path = _write_json(
        tmp_path,
        {
            "/devices/spinner/run": {
                "version": "cycles-v2",
                "formula": "constant_speed_cycles",
                "feature_paths": ["cycles", "rpm"],
            }
        },
    )
    estimator = DurationEstimator()

    assert load_timing_rules(
        estimator, environ={"UNILABOS_TIMING_RULES_FILE": str(path)}
    ) == 1
    spec = estimator.timing_specs["/devices/spinner/run"]
    assert spec.version == "cycles-v2"
    assert spec.formula is not None


@pytest.mark.parametrize(
    "raw, message",
    [
        ([{"version": "not-a-map"}], "顶层"),
        ({"": {}}, "动作键"),
        ({"/device/run": []}, "规则对象"),
        ({"/device/run": {"formula": "eval"}}, "允许清单"),
        ({"/device/run": {"default_s": 0}}, "默认"),
    ],
)
def test_invalid_rule_documents_fail_before_registration(tmp_path, raw, message: str) -> None:
    estimator = DurationEstimator()
    path = _write_json(tmp_path, raw)

    with pytest.raises(ValueError, match=message):
        load_timing_rules(estimator, str(path))
    assert estimator.timing_specs == {}


def test_duplicate_json_keys_and_nonfinite_constants_are_rejected(tmp_path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"/device/run": {"version": "1"}, "/device/run": {"version": "2"}}',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="重复键"):
        load_timing_rules(DurationEstimator(), str(duplicate))

    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text('{"/device/run": {"default_s": NaN}}', encoding="utf-8")
    with pytest.raises(ValueError, match="非法 JSON 数值"):
        load_timing_rules(DurationEstimator(), str(nonfinite))


def test_existing_version_replacement_is_rejected_atomically(tmp_path) -> None:
    estimator = DurationEstimator()
    estimator.register_timing_spec("/device/old", {"version": "v1"})
    path = _write_json(
        tmp_path,
        {
            "/device/old": {"version": "v1"},
            "/device/new": {"version": "v1"},
        },
    )

    with pytest.raises(ValueError, match="新的 version"):
        load_timing_rules(estimator, str(path))
    assert set(estimator.timing_specs) == {"/device/old"}
    assert estimator.timing_specs["/device/old"].version == "v1"


def test_new_versions_are_registered_as_one_batch(tmp_path) -> None:
    estimator = DurationEstimator()
    path = _write_json(
        tmp_path,
        {
            "/device/one": {"version": "v2", "default_s": 2},
            "/device/two": {"version": "v3", "default_s": 3},
        },
    )

    assert load_timing_rules(estimator, str(path)) == 2
    assert estimator.timing_specs["/device/one"].default_s == 2.0
    assert estimator.timing_specs["/device/two"].default_s == 3.0

