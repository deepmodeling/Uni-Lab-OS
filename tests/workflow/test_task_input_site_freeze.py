"""所属物料尚未选定时，提交任务不冻结库位。"""

import pytest

from unilabos.workflow.task_input import TaskInputError, _freeze_site_selections

OWNER = "00000000-0000-4000-8000-000000000001"
SITE = "00000000-0000-4000-8000-000000000002"


def _plan(*, owner):
    node = {
        "uuid": "action",
        "param": {"target_box": owner, "target_site": "hole"},
        "site_selectors": [{
            "parameter": "target_site",
            "owner_parameter": "target_box",
        }],
        "execution_policy": {},
    }
    job = {
        "workflow_node_uuid": "action",
        "param": {"target_box": owner, "target_site": "hole"},
        "execution_policy": {},
    }
    return {"nodes": [node]}, [job]


def test_missing_owner_leaves_site_name_for_dispatch():
    plan, jobs = _plan(owner=None)

    def resolver(_request):
        raise AssertionError("所属物料还没选定时不应解析库位")

    _freeze_site_selections(
        plan=plan, jobs=jobs, resolver=resolver, resolved_input={},
    )
    assert plan["nodes"][0]["param"]["target_site"] == "hole"
    assert jobs[0]["param"]["target_site"] == "hole"
    assert "target_site_group" not in plan["nodes"][0]["execution_policy"]


def test_concrete_owner_still_freezes_site_at_submit():
    plan, jobs = _plan(owner={"uuid": OWNER})

    def resolver(request):
        assert request["owner_material_uuid"] == OWNER
        assert request["exact_site_reference"] == "hole"
        return {"site_uuids": [SITE], "fingerprint": "fp"}

    _freeze_site_selections(
        plan=plan, jobs=jobs, resolver=resolver, resolved_input={},
    )
    assert "target_site" not in plan["nodes"][0]["param"]
    assert plan["nodes"][0]["execution_policy"]["target_site_group"] == [SITE]


def test_missing_owner_keeps_named_site_group_for_dispatch():
    plan, jobs = _plan(owner=None)
    plan["nodes"][0]["site_selectors"][0]["group_key"] = "tip_positions"
    plan["nodes"][0]["site_selectors"][0]["strategy"] = "sort_order"

    def resolver(_request):
        raise AssertionError("所属物料还没选定时不应解析库位组")

    _freeze_site_selections(
        plan=plan, jobs=jobs, resolver=resolver, resolved_input={},
    )
    assert "target_site" not in plan["nodes"][0]["param"]
    assert "target_site_group" not in plan["nodes"][0]["execution_policy"]
    assert plan["nodes"][0]["execution_policy"]["deferred_site_group"] == {
        "version": 1,
        "parameter": "target_site",
        "owner_parameter": "target_box",
        "group_key": "tip_positions",
        "strategy": "sort_order",
    }


def test_missing_owner_with_exact_site_is_rejected():
    plan, jobs = _plan(owner=None)
    plan["nodes"][0]["site_selectors"][0]["group_key"] = "tip_positions"
    plan["nodes"][0]["site_selectors"][0]["exact_parameter"] = "exact_site"

    with pytest.raises(TaskInputError, match="所属物料尚未确定时不能指定精确库位"):
        _freeze_site_selections(
            plan=plan, jobs=jobs, resolver=lambda _request: {}, resolved_input={},
        )


def test_present_owner_without_uuid_still_rejected():
    plan, jobs = _plan(owner={"name": "box"})
    with pytest.raises(TaskInputError, match="目标库位所属资源没有冻结 UUID"):
        _freeze_site_selections(
            plan=plan, jobs=jobs, resolver=lambda _request: {}, resolved_input={},
        )
