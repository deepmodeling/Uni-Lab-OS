"""本地物料与库位内存忙碌键的层级互斥测试。"""

import pytest

from unilabos.app.scheduler.resource_lock import (
    conflicting_resource_lock_keys,
    material_lock_key,
    narrow_runtime_owner_lock_to_site,
    normalize_resource_lock_keys,
    site_lock_key,
)
from unilabos.workflow.resource_lock_key import device_lock_key

_MATERIAL_UUID = "11111111-1111-4111-8111-111111111111"
_SITE_UUID_A = "22222222-2222-4222-8222-222222222222"
_SITE_UUID_B = "33333333-3333-4333-8333-333333333333"


def test_whole_material_and_child_site_conflict_in_both_directions() -> None:
    """验证整父物料与任一子库位双向冲突。

    参数：无。返回：无。异常：若父子层级只在一个申请方向生效，断言失败，
    防止整物料动作与库位动作并行。
    """

    whole = material_lock_key(_MATERIAL_UUID)
    child = site_lock_key(_MATERIAL_UUID, _SITE_UUID_A)

    assert conflicting_resource_lock_keys({whole}, {child}) == {whole}
    assert conflicting_resource_lock_keys({child}, {whole}) == {child}


def test_distinct_sites_of_same_parent_do_not_conflict() -> None:
    """验证同一父物料下不同库位可并行。

    参数：无。返回：无。异常：若库位键被错误退化为整父物料互斥，断言失败。
    """

    first = site_lock_key(_MATERIAL_UUID, _SITE_UUID_A)
    second = site_lock_key(_MATERIAL_UUID, _SITE_UUID_B)

    assert conflicting_resource_lock_keys({first}, {second}) == set()


def test_whole_material_key_removes_redundant_child_site_key() -> None:
    """验证同一作业内整物料占用覆盖冗余子库位键。

    参数：无。返回：无。异常：若归一化保留重复层级键，断言失败。
    """

    whole = material_lock_key(_MATERIAL_UUID)
    child = site_lock_key(_MATERIAL_UUID, _SITE_UUID_A)

    assert normalize_resource_lock_keys({whole, child}) == {whole}


def test_runtime_owner_site_lock_matches_dispatch_after_narrowing() -> None:
    """运行时盒子在派发收成孔位锁后，不应再被算成计划外资源。"""

    whole = material_lock_key(_MATERIAL_UUID)
    child = site_lock_key(_MATERIAL_UUID, _SITE_UUID_A)
    runtime = {whole, child}
    requested = {child}
    assert normalize_resource_lock_keys(requested) - normalize_resource_lock_keys(runtime) == {child}
    narrow_runtime_owner_lock_to_site(
        runtime,
        owner_material_uuid=_MATERIAL_UUID,
        site_uuid=_SITE_UUID_A,
    )
    assert normalize_resource_lock_keys(requested) - normalize_resource_lock_keys(runtime) == set()


@pytest.mark.parametrize(
    ("builder", "identities"),
    [
        (device_lock_key, ("",)),
        (device_lock_key, (" reactor ",)),
        (device_lock_key, ("robot/arm",)),
        (material_lock_key, ("",)),
        (site_lock_key, (_MATERIAL_UUID, "site/one")),
    ],
)
def test_lock_key_builders_reject_noncanonical_identity_tokens(
    builder: object,
    identities: tuple[str, ...],
) -> None:
    """所有生产者共用同一语法，不能构造解析器随后拒绝的锁键。"""

    with pytest.raises(ValueError):
        builder(*identities)  # type: ignore[operator]
