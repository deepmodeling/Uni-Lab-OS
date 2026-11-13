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


def test_ast_scanner_skips_runtime_injected_parameters(tmp_path) -> None:
    """``action_context`` / ``sample_uuids`` 由执行器注入，不得进入动作契约。"""

    source = tmp_path / "ctx_driver.py"
    source.write_text(
        "\n".join(
            [
                "from unilabos.registry.decorators import device, action",
                "from unilabos.backend.runtime.action import ActionContext",
                "",
                "@device(id='ctx_ast_test', category=['test'])",
                "class Driver:",
                "    @action()",
                "    def heat(self, site_id: int, duration_seconds: float, action_context: ActionContext, sample_uuids: dict = None) -> dict:",
                "        return {}",
            ]
        ),
        encoding="utf-8",
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        result = scan_directory(tmp_path, python_path=tmp_path, executor=executor)

    metadata = result["devices"]["ctx_ast_test"]
    heat = metadata["actions"]["heat"]
    names = [param["name"] for param in heat["params"]]
    assert names == ["site_id", "duration_seconds"]


def test_ast_cache_rejects_previous_metadata_version(tmp_path) -> None:
    cache_path = tmp_path / "ast_scan_cache.json"
    cache_path.write_text(
        '{"version": 18, "files": {"stale.py": {"devices": [{"device_id": "stale"}]}}}',
        encoding="utf-8",
    )

    cache = load_scan_cache(cache_path)

    assert _CACHE_VERSION == 19
    assert cache == {"version": _CACHE_VERSION, "files": {}}


def test_build_cache_slots_are_isolated_per_scan_configuration_and_not_rewritten_on_hit(
    monkeypatch,
) -> None:
    """受管 Slave（external_only）与权威（全量）共用一个 pkl：各自命中各自的槽，不互相覆盖；
    全部命中时不重写缓存文件。"""

    full_result = {
        "devices": {"pump": {"device_id": "pump"}},
        "resources": {},
        "_cache_stats": {"hits": 3, "misses": 0, "total": 3},
    }
    slave_result = {
        "devices": {"rack": {"device_id": "rack"}},
        "resources": {},
        "_cache_stats": {"hits": 1, "misses": 0, "total": 1},
    }

    def make_registry(cache: dict, scan_result: dict, built: list, saves: list) -> Registry:
        registry = Registry()
        monkeypatch.setattr(registry, "_startup_executor", None)
        monkeypatch.setattr(registry, "device_type_registry", {})
        monkeypatch.setattr(registry, "resource_type_registry", {})
        monkeypatch.setattr(registry, "_load_config_cache", lambda: cache)
        monkeypatch.setattr(registry, "_save_config_cache", lambda c: saves.append(dict(c)))
        monkeypatch.setattr(
            registry,
            "_build_device_entry_from_ast",
            lambda device_id, meta: built.append(device_id) or {"device_id": device_id, "built": True},
        )

        def fake_scan_directory(*_args, cache, include_files=None, **_kwargs):
            if include_files is not None and any(str(f).endswith("host_services.py") for f in include_files):
                return {"devices": {}, "resources": {}, "_cache_stats": {"hits": 0, "misses": 0, "total": 0}}
            return {k: (dict(v) if isinstance(v, dict) else v) for k, v in scan_result.items()}

        monkeypatch.setattr("unilabos.registry.ast_registry_scanner.scan_directory", fake_scan_directory)
        return registry

    shared_cache: dict = {"_ast_scan": {"version": _CACHE_VERSION, "files": {}}}

    # 1) 权威全量扫描：首次构建并落盘
    built, saves = [], []
    make_registry(shared_cache, full_result, built, saves)._run_ast_scan(devices_dirs=[])
    assert built == ["pump"] and len(saves) == 1
    shared_cache = saves[-1]

    # 2) 受管 Slave 的 external_only 扫描：构建自己的槽，不覆盖权威的槽
    built, saves = [], []
    make_registry(shared_cache, slave_result, built, saves)._run_ast_scan(devices_dirs=[], external_only=True)
    assert built == ["rack"] and len(saves) == 1
    shared_cache = saves[-1]
    assert len(shared_cache["_build_results"]) == 2

    # 3) 权威再次启动：全部命中，不重建、不重写缓存
    built, saves = [], []
    registry = make_registry(shared_cache, full_result, built, saves)
    registry._run_ast_scan(devices_dirs=[])
    assert built == [] and saves == []
    assert registry.device_type_registry == {"pump": {"device_id": "pump", "built": True}}


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

    def fake_scan_directory(*_args, cache, include_files=None, **_kwargs):
        # host_services.py 的单独扫描（include_files）返回空结果即可，
        # 不参与本测试对主扫描缓存失效的断言。
        if include_files is not None:
            return {
                "devices": {},
                "resources": {},
                "_cache_stats": {"hits": 0, "misses": 0, "total": 0},
            }
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
    # build 结果按扫描配置分槽（权威 / Host 全量扫描与 external_only 的受管 Slave 共用一个 pkl）
    build_slots = saved_cache["_build_results"]
    assert len(build_slots) == 1
    (build_key, built), = build_slots.items()
    assert '"external_only": false' in build_key
    assert built == {
        "devices": {
            "fresh-device": {"device_id": "fresh-device", "fresh": True}
        },
        "resources": {},
    }
