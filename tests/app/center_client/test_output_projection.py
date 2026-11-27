"""中心 ResourceSlot 值投影覆盖数组/可空值，不裁剪普通业务对象。"""

from copy import deepcopy
from uuid import uuid4

import pytest

from unilabos.workflow.center_output import project_public_workflow_output


def test_resource_slot_arrays_nullable_and_regular_values_keep_original_facts():
    first, second = str(uuid4()), str(uuid4())
    slot = {"$slot": "ResourceSlot"}
    nullable = {"anyOf": [slot, {"type": "null"}]}
    schemas = {
        "sample": slot,
        "samples": {"type": "array", "items": slot},
        "nullable_samples": {"anyOf": [{"type": "array", "items": slot}, {"type": "null"}]},
        "empty": nullable,
        "present": nullable,
        "ordinary": {"type": "object"},
        "number": {"type": "number"},
    }
    contract = {"version": 1, "outputs": [{"name": name, "schema": schema} for name, schema in schemas.items()]}
    material = {"uuid": first, "custody_policy": "task_exclusive", "resource_template_uuid": str(uuid4())}
    raw = {"sample": material, "samples": [material, {"uuid": second, "local_note": "保留本地"}],
           "nullable_samples": [material], "empty": None, "present": material,
           "ordinary": {"uuid": "业务文本", "custody_policy": "普通字段", "nested": {"uuid": first}},
           "number": 1.5, "unexpected_top_level": "保留供公开合同原校验拒绝"}
    before_contract, before_raw = deepcopy(contract), deepcopy(raw)
    result = project_public_workflow_output(contract, raw)
    assert result == {**raw, "sample": {"uuid": first}, "samples": [{"uuid": first}, {"uuid": second}],
                      "nullable_samples": [{"uuid": first}], "present": {"uuid": first}}
    assert contract == before_contract and raw == before_raw
    result["ordinary"]["nested"]["uuid"] = "修改副本"
    assert raw == before_raw


@pytest.mark.parametrize("invalid", [{}, {"uuid": "invalid"}, {"uuid": str(uuid4()).upper()}, None])
def test_invalid_resource_slot_is_not_given_a_fabricated_public_identity(invalid):
    contract = {"version": 1, "outputs": [{"name": "sample", "schema": {"$slot": "ResourceSlot"}}]}
    with pytest.raises(ValueError):
        project_public_workflow_output(contract, {"sample": invalid})
