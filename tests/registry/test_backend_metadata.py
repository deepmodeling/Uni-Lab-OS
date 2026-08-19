from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
import yaml

from unilabos.registry.ast_registry_scanner import (
    _CACHE_VERSION,
    load_scan_cache,
    scan_directory,
)
from unilabos.registry.decorators import device, get_device_meta
from unilabos.registry.registry import Registry


def test_device_decorator_keeps_supported_backends() -> None:
    @device(
        id="backend_metadata_runtime_test",
        category=["test"],
        supported_backends=["hostlink", "ros2"],
    )
    class RuntimeDriver:
        pass

    metadata = get_device_meta(RuntimeDriver)
    assert metadata is not None
    assert metadata["supported_backends"] == ["hostlink", "ros2"]


def test_device_decorator_defaults_ordinary_device_metadata() -> None:
    @device(id="backend_metadata_default_test", category=["test"])
    class RuntimeDriver:
        pass

    metadata = get_device_meta(RuntimeDriver)
    assert metadata is not None
    assert metadata["supported_backends"] == ["hostlink", "ros2"]
    assert metadata["available_sites"] == []


def test_device_decorator_rejects_internal_basic_runtime() -> None:
    with pytest.raises(ValueError, match="只允许 hostlink/ros2"):
        @device(
            id="backend_metadata_internal_basic_test",
            category=["test"],
            supported_backends=["basic", "ros2"],
        )
        class RuntimeDriver:
            pass


def test_ast_scanner_keeps_supported_backends(tmp_path) -> None:
    source = tmp_path / "driver.py"
    source.write_text(
        "\n".join(
            [
                "from unilabos.registry.decorators import device",
                "",
                "@device(",
                "    id='backend_metadata_ast_test',",
                "    category=['test'],",
                "    supported_backends=['hostlink', 'ros2'],",
                ")",
                "class Driver:",
                "    pass",
            ]
        ),
        encoding="utf-8",
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        result = scan_directory(
            tmp_path,
            python_path=tmp_path,
            executor=executor,
        )

    metadata = result["devices"]["backend_metadata_ast_test"]
    assert metadata["supported_backends"] == ["hostlink", "ros2"]


def test_ast_scanner_defaults_python_and_native_ros2_devices(tmp_path) -> None:
    source = tmp_path / "default_drivers.py"
    source.write_text(
        "\n".join(
            [
                "from rclpy.node import Node",
                "from unilabos.registry.decorators import device",
                "",
                "@device(id='ordinary_ast_device', category=['test'])",
                "class OrdinaryDriver:",
                "    pass",
                "",
                "@device(id='native_ros_ast_device', category=['test'])",
                "class NativeROSDriver(Node):",
                "    pass",
            ]
        ),
        encoding="utf-8",
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        result = scan_directory(
            tmp_path,
            python_path=tmp_path,
            executor=executor,
        )

    assert result["devices"]["ordinary_ast_device"]["supported_backends"] == [
        "hostlink",
        "ros2",
    ]
    assert result["devices"]["ordinary_ast_device"]["available_sites"] == []
    assert result["devices"]["native_ros_ast_device"]["supported_backends"] == [
        "ros2"
    ]


def test_device_decorator_applies_per_id_supported_backends() -> None:
    @device(
        ids=["backend_metadata_runtime_a", "backend_metadata_runtime_b"],
        id_meta={
            "backend_metadata_runtime_b": {
                "supported_backends": ["hostlink"],
            },
        },
        category=["test"],
        supported_backends=["hostlink", "ros2"],
    )
    class MultiRuntimeDriver:
        pass

    base = get_device_meta(MultiRuntimeDriver, "backend_metadata_runtime_a")
    override = get_device_meta(MultiRuntimeDriver, "backend_metadata_runtime_b")

    assert base is not None
    assert override is not None
    assert base["supported_backends"] == ["hostlink", "ros2"]
    assert override["supported_backends"] == ["hostlink"]


def test_ast_scanner_applies_per_id_supported_backends(tmp_path) -> None:
    source = tmp_path / "multi_driver.py"
    source.write_text(
        "\n".join(
            [
                "from unilabos.registry.decorators import device",
                "",
                "@device(",
                "    ids=['backend_metadata_ast_a', 'backend_metadata_ast_b'],",
                "    id_meta={",
                "        'backend_metadata_ast_b': {",
                "            'supported_backends': ['hostlink'],",
                "        },",
                "    },",
                "    category=['test'],",
                "    supported_backends=['hostlink', 'ros2'],",
                ")",
                "class Driver:",
                "    pass",
            ]
        ),
        encoding="utf-8",
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        result = scan_directory(
            tmp_path,
            python_path=tmp_path,
            executor=executor,
        )

    assert result["devices"]["backend_metadata_ast_a"]["supported_backends"] == [
        "hostlink",
        "ros2",
    ]
    assert result["devices"]["backend_metadata_ast_b"]["supported_backends"] == [
        "hostlink",
    ]


def test_registry_completion_publishes_backend_site_and_policy_defaults(
    monkeypatch,
) -> None:
    registry = Registry()
    monkeypatch.setattr(
        registry,
        "device_type_registry",
        {
            "ordinary": {
                "class": {
                    "type": "python",
                    "status_types": {},
                    "action_value_mappings": {"run": {"type": "", "schema": {}}},
                }
            },
            "native_ros": {
                "class": {
                    "type": "ros2",
                    "status_types": {},
                    "action_value_mappings": {"run": {"type": "", "schema": {}}},
                }
            },
        },
    )

    completion = {
        entry["id"]: entry for entry in registry.obtain_registry_device_info()
    }

    assert completion["ordinary"]["class"]["supported_backends"] == [
        "hostlink",
        "ros2",
    ]
    assert completion["native_ros"]["class"]["supported_backends"] == ["ros2"]
    assert completion["ordinary"]["available_sites"] == []
    assert completion["ordinary"]["class"]["status_policies"] == {}
    assert completion["ordinary"]["class"]["action_value_mappings"]["run"][
        "error_policy"
    ] == {}

    yaml_entry = yaml.safe_load(registry.get_yaml_output("ordinary"))["ordinary"]
    assert yaml_entry["available_sites"] == []
    assert yaml_entry["class"]["supported_backends"] == ["hostlink", "ros2"]
    assert yaml_entry["class"]["status_policies"] == {}
    assert yaml_entry["class"]["action_value_mappings"]["run"][
        "error_policy"
    ] == {}


def test_ast_cache_rejects_previous_metadata_version(tmp_path) -> None:
    cache_path = tmp_path / "ast_scan_cache.json"
    cache_path.write_text(
        '{"version": 7, "files": {"stale.py": {"devices": [{"device_id": "stale"}]}}}',
        encoding="utf-8",
    )

    cache = load_scan_cache(cache_path)

    assert _CACHE_VERSION == 12
    assert cache == {"version": _CACHE_VERSION, "files": {}}


def test_registry_run_ast_scan_invalidates_stale_scan_and_build_caches(
    monkeypatch,
) -> None:
    registry = Registry()
    stale_cache = {
        "_ast_scan": {
            "version": _CACHE_VERSION - 1,
            "files": {
                "stale.py": {
                    "devices": [{"device_id": "stale-device"}],
                    "resources": [],
                }
            },
        },
        "_build_results": {
            "devices": {"stale-device": {"stale": True}},
            "resources": {},
        },
    }
    saved_cache = {}
    built_devices = []

    def fake_scan_directory(*_args, cache, **_kwargs):
        assert cache == {"version": _CACHE_VERSION, "files": {}}
        cache["files"]["fresh.py"] = {
            "devices": [{"device_id": "fresh-device"}],
            "resources": [],
        }
        return {
            "devices": {
                "fresh-device": {
                    "device_id": "fresh-device",
                    "supported_backends": ["ros2"],
                }
            },
            "resources": {},
            # An all-hit result would reuse _build_results if the production
            # version transition had failed to invalidate it.
            "_cache_stats": {"hits": 1, "misses": 0, "total": 1},
        }

    def fake_build_device(device_id, ast_meta):
        built_devices.append((device_id, ast_meta))
        return {"device_id": device_id, "fresh": True}

    monkeypatch.setattr(registry, "_startup_executor", None)
    monkeypatch.setattr(registry, "device_type_registry", {})
    monkeypatch.setattr(registry, "resource_type_registry", {})
    monkeypatch.setattr(registry, "_load_config_cache", lambda: stale_cache)
    monkeypatch.setattr(
        registry,
        "_save_config_cache",
        lambda cache: saved_cache.update(cache),
    )
    monkeypatch.setattr(registry, "_build_device_entry_from_ast", fake_build_device)
    monkeypatch.setattr(
        "unilabos.registry.ast_registry_scanner.scan_directory",
        fake_scan_directory,
    )

    registry._run_ast_scan(devices_dirs=[])

    assert [device_id for device_id, _ in built_devices] == ["fresh-device"]
    assert registry.device_type_registry == {
        "fresh-device": {"device_id": "fresh-device", "fresh": True}
    }
    assert "stale-device" not in registry.device_type_registry
    assert saved_cache["_ast_scan"] == {
        "version": _CACHE_VERSION,
        "files": {
            "fresh.py": {
                "devices": [{"device_id": "fresh-device"}],
                "resources": [],
            }
        },
    }
    assert saved_cache["_build_results"] == {
        "devices": {
            "fresh-device": {"device_id": "fresh-device", "fresh": True}
        },
        "resources": {},
    }
