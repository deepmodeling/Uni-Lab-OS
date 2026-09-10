"""注册表强制初始化参数合同。"""

import pytest

from unilabos.registry.init_enforce import (
    merge_init_param_enforce,
    validate_init_param_enforce,
)


def test_merge_init_param_enforce_preserves_runtime_values_outside_registry_boundary():
    runtime_config = {
        "host": "10.0.0.9",
        "port": 7001,
        "channels": 8,
        "transport": {"timeout": 5, "retries": 2},
    }
    registry_config = {
        "channels": 96,
        "transport": {"retries": 4},
    }

    merged = merge_init_param_enforce(runtime_config, registry_config)

    assert merged == {
        "host": "10.0.0.9",
        "port": 7001,
        "channels": 96,
        "transport": {"timeout": 5, "retries": 4},
    }
    assert runtime_config["channels"] == 8
    assert registry_config["transport"]["retries"] == 4


def test_validate_init_param_enforce_normalizes_missing_value():
    assert validate_init_param_enforce("vendor.device", None, None) == {}


@pytest.mark.parametrize(
    "legacy_value",
    [
        {"backend": {"factory": "vendor.driver:Backend"}},
        {"args": ["${config.host}"]},
        {"kwargs": {"host": "127.0.0.1"}},
        {"name": {"value": "constant"}},
        {"host": "${config.host}"},
    ],
)
def test_validate_init_param_enforce_rejects_legacy_object_factory_dsl(legacy_value):
    with pytest.raises(ValueError, match="init_param_enforce"):
        validate_init_param_enforce("vendor.device", None, legacy_value)
