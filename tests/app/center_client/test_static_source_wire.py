"""与中心消费相同固定 JSON，保证新来源绑定及历史任务摘要跨端一致。"""
import json
from copy import deepcopy
from pathlib import Path

import pytest

from unilabos.center_protocol.digest import digest
from unilabos.center_protocol.task_package import TaskPackageError, validate_task_package


def package(name):
    return json.loads((Path(__file__).parent / "fixtures/static-source-wire.json").read_text())[name]


@pytest.mark.parametrize("name", ["legacy", "unseen", "retained"])
def test_frozen_source_wire_roundtrip_preserves_exact_package_and_digest(name):
    original = package(name)
    decoded = validate_task_package(deepcopy(original))
    assert decoded == original
    assert digest(decoded["payload"]) == original["payload_digest"]
    if name == "legacy":
        assert "material_source_bindings" not in decoded["payload"]


@pytest.mark.parametrize("level", ["binding", "lineage"])
@pytest.mark.parametrize("name", ["unseen", "retained"])
def test_source_wire_rejects_unknown_fields_even_with_matching_digest(level, name):
    changed = package(name)
    binding = changed["payload"]["material_source_bindings"][0]
    target = binding if level == "binding" else binding["lineage"]
    target["unexpected"] = True
    changed["payload_digest"] = digest(changed["payload"])
    with pytest.raises(TaskPackageError):
        validate_task_package(changed)


def test_source_identity_is_covered_by_frozen_task_digest():
    changed = package("unseen")
    changed["payload"]["material_source_bindings"][0]["inventory_snapshot"]["materials"][0]["barcode"] = "另一只样品"
    with pytest.raises(TaskPackageError):
        validate_task_package(changed)


def test_empty_source_field_cannot_change_legacy_package_serialization():
    changed = package("legacy")
    changed["payload"]["material_source_bindings"] = []
    changed["payload_digest"] = digest(changed["payload"])
    with pytest.raises(TaskPackageError):
        validate_task_package(changed)
