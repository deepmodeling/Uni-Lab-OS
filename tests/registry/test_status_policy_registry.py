import ast
from concurrent.futures import ThreadPoolExecutor

import pytest

from unilabos.registry.ast_registry_scanner import (
    _collect_imports,
    _extract_class_body,
    scan_directory,
)
from unilabos.registry.decorators import get_topic_config, topic_config


def _registry():
    try:
        from unilabos.registry.registry import Registry
    except ImportError as exc:
        pytest.skip(f"当前环境未安装完整 unilabos_msgs: {exc}")
    return Registry()


def _policy():
    return {
        "normal_values": ["Idle", "Running"],
        "incidents": {
            "Error": {
                "code": "pump.mode.error",
                "severity": "critical",
                "message": "泵进入错误状态",
                "hold": True,
            }
        },
    }


def test_topic_config_exposes_a_normalized_status_policy() -> None:
    raw_policy = _policy()

    @topic_config(status_policy=raw_policy)
    def get_mode() -> str:
        return "Idle"

    raw_policy["normal_values"].append("MutatedAfterDecoration")
    config = get_topic_config(get_mode)

    assert config["status_policy"] == {
        "normal_values": ["Idle", "Running"],
        "incidents": [
            {
                "value": "Error",
                "code": "pump.mode.error",
                "severity": "critical",
                "message": "泵进入错误状态",
                "hold": True,
            }
        ],
    }


def test_topic_config_without_policy_exposes_empty_registry_object() -> None:
    @topic_config()
    def get_mode() -> str:
        return "Idle"

    assert get_topic_config(get_mode)["status_policy"] == {}


def test_ast_scanner_normalizes_status_policy_and_supplies_default() -> None:
    tree = ast.parse(
        """
from unilabos.registry.decorators import topic_config

class Driver:
    @topic_config(status_policy={"error_values": ["Error"]})
    def get_mode(self) -> str:
        return "Idle"

    @topic_config()
    def get_pressure(self) -> float:
        return 0.0
"""
    )
    class_node = next(
        node for node in tree.body if isinstance(node, ast.ClassDef)
    )

    extracted = _extract_class_body(class_node, _collect_imports(tree))

    assert extracted["status_properties"]["mode"]["topic_config"][
        "status_policy"
    ] == {
        "normal_values": [],
        "incidents": [
            {
                "value": "Error",
                "code": "status.Error",
                "severity": "error",
                "message": "device status changed to 'Error'",
                "hold": True,
            }
        ],
    }
    assert extracted["status_properties"]["pressure"]["topic_config"][
        "status_policy"
    ] == {}


def test_ast_registry_publishes_status_policy_metadata(tmp_path) -> None:
    source = tmp_path / "status_driver.py"
    source.write_text(
        """
from unilabos.registry.decorators import device, topic_config

@device(id="status_policy_test", category=["test"])
class Driver:
    @property
    @topic_config(name="operation_mode", status_policy={
        "normal_values": ["Idle"],
        "incidents": {
            "Error": {
                "code": "driver.mode.error",
                "severity": "error",
                "hold": True,
            },
        },
    })
    def mode(self) -> str:
        return "Idle"
""",
        encoding="utf-8",
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        scanned = scan_directory(
            tmp_path,
            python_path=tmp_path,
            executor=executor,
        )

    assert scanned["devices"]["status_policy_test"]["status_properties"][
        "mode"
    ]["topic_config"]["status_policy"]["incidents"][0]["value"] == "Error"

    registry = _registry()
    entry = registry._build_device_entry_from_ast(
        "status_policy_test",
        scanned["devices"]["status_policy_test"],
    )

    assert entry["class"]["status_policies"]["operation_mode"] == {
        "normal_values": ["Idle"],
        "incidents": [
            {
                "value": "Error",
                "code": "driver.mode.error",
                "severity": "error",
                "message": "device status changed to 'Error'",
                "hold": True,
            }
        ],
    }


def test_ast_registry_rejects_invalid_status_policy_with_context(tmp_path) -> None:
    source = tmp_path / "invalid_status_driver.py"
    source.write_text(
        """
from unilabos.registry.decorators import device, topic_config

@device(id="invalid_status_policy_test", category=["test"])
class Driver:
    @topic_config(status_policy={
        "incidents": {"Error": {"severity": "catastrophic"}},
    })
    def get_mode(self) -> str:
        return "Idle"
""",
        encoding="utf-8",
    )

    tree = ast.parse(source.read_text(encoding="utf-8"))
    class_node = next(
        node for node in tree.body if isinstance(node, ast.ClassDef)
    )
    with pytest.raises(ValueError, match="status_policy.*severity"):
        _extract_class_body(class_node, _collect_imports(tree))
