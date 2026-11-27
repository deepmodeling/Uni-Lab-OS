"""声明式作业资源策略和静态设备托管顺序合同测试。"""

from __future__ import annotations

import pytest

from unilabos.workflow.execution_resource_policy import (
    ExecutionResourcePolicyError,
    merge_action_resource_policy,
    resolve_execution_resource_policy,
    validate_static_device_tenancy_order,
)

MATERIAL_UUID = "91000000-0000-4000-8000-000000000001"
DEVICE_A_UUID = "92000000-0000-4000-8000-000000000001"
DEVICE_B_UUID = "92000000-0000-4000-8000-000000000002"


def _reference(resource_uuid: str) -> dict[str, str]:
    """构造一个最终动作参数中的稳定资源引用。

    参数：``resource_uuid`` 是物料或设备的稳定 UUID。返回：符合 ResourceSlot
    运行形状的字典。异常：无；测试常量保证 UUID 合法。
    """

    return {"uuid": resource_uuid}


def _tenancy_policy(
    *,
    acquire: str = "",
    release: str = "",
) -> dict[str, object]:
    """构造装载期间任务托管的冻结节点执行策略。

    参数：``acquire`` 和 ``release`` 是可选设备参数名。返回：以主物料参数
    ``sample`` 为锚点的声明式策略。异常：无；至少一端由调用用例保证。
    """

    return {
        "device_tenancy": {
            "mode": "task_while_loaded",
            "material_param": "sample",
            "acquire_device_param": acquire or None,
            "release_device_param": release or None,
        }
    }


def _planned_node(
    *,
    policy: dict[str, object],
    **devices: str,
) -> dict[str, object]:
    """构造静态检查使用的一个拓扑有序计划节点。

    参数：``policy`` 是冻结资源策略，``devices`` 把动作设备参数映射到设备 UUID。
    返回：包含主物料和设备引用的最小计划节点。异常：无。
    """

    param = {"sample": _reference(MATERIAL_UUID)}
    param.update({name: _reference(identity) for name, identity in devices.items()})
    return {"execution_policy": policy, "param": param}


def test_resource_policy_resolves_device_keys_site_group_and_tenancy() -> None:
    """运行解析应一次形成附加设备键、等价库位组和托管转换。

    参数：无。返回：无。断言资源参数只按稳定 UUID 生成设备键，目标库位组保持
    冻结顺序，且主物料托管取得/释放身份完整。异常：解析失败即测试失败。
    """

    site_uuid = "93000000-0000-4000-8000-000000000001"
    policy = {
        "required_device_params": ["camera"],
        "target_site_group": [site_uuid],
        **_tenancy_policy(acquire="target", release="source"),
    }
    resolved = resolve_execution_resource_policy(
        policy,
        {
            "sample": _reference(MATERIAL_UUID),
            "camera": _reference("92000000-0000-4000-8000-000000000003"),
            "source": _reference(DEVICE_A_UUID),
            "target": _reference(DEVICE_B_UUID),
        },
    )

    assert resolved.target_site_uuids == (site_uuid,)
    assert resolved.device_lock_keys == (
        f"/devices/{DEVICE_A_UUID}",
        f"/devices/{DEVICE_B_UUID}",
        "/devices/92000000-0000-4000-8000-000000000003",
    )
    assert resolved.device_tenancy == {
        "mode": "task_while_loaded",
        "material_uuid": MATERIAL_UUID,
        "acquire_device_lock_key": f"/devices/{DEVICE_B_UUID}",
        "release_device_lock_key": f"/devices/{DEVICE_A_UUID}",
    }


def test_action_contract_owns_tenancy_while_workflow_selects_site_group() -> None:
    """动作资源合同拥有托管语义，工作流实例只可补充等价库位组。

    参数：无。返回：无；断言 AST 合同字段进入冻结执行策略，部署时库位组仍能
    按工作流选择。异常：合并失败即测试失败。
    """

    site_uuid = "93000000-0000-4000-8000-000000000001"
    contract = {
        "version": 1,
        "required_device_params": ["camera"],
        **_tenancy_policy(acquire="target"),
    }

    merged = merge_action_resource_policy(
        contract,
        {"target_site_group": [site_uuid]},
    )

    assert merged["required_device_params"] == ["camera"]
    assert merged["target_site_group"] == [site_uuid]
    assert merged["device_tenancy"]["acquire_device_param"] == "target"


def test_workflow_cannot_override_action_device_tenancy() -> None:
    """工作流节点不得把动作声明的设备托管改成另一种释放关系。

    参数：无。返回：无；断言资源语义漂移在执行计划冻结前被拒绝。异常：稳定
    ``ExecutionResourcePolicyError`` 是预期结果。
    """

    with pytest.raises(ExecutionResourcePolicyError, match="不得覆盖"):
        merge_action_resource_policy(
            {"version": 1, **_tenancy_policy(acquire="target")},
            _tenancy_policy(release="source"),
        )


def test_static_tenancy_allows_ordered_transfer_and_final_unload() -> None:
    """按全站稳定 UUID 顺序转运并最终卸载应通过静态无环证明。

    参数：无。返回：无。断言 A 装载、A→B 转运和 B 卸载形成的资源取得关系
    严格递增且没有遗留设备托管。异常：静态检查误报时测试失败。
    """

    validate_static_device_tenancy_order(
        [
            _planned_node(
                policy=_tenancy_policy(acquire="target"),
                target=DEVICE_A_UUID,
            ),
            _planned_node(
                policy=_tenancy_policy(acquire="target", release="source"),
                source=DEVICE_A_UUID,
                target=DEVICE_B_UUID,
            ),
            _planned_node(
                policy=_tenancy_policy(release="source"),
                source=DEVICE_B_UUID,
            ),
        ]
    )


def test_static_tenancy_rejects_reverse_device_acquisition() -> None:
    """持有高序设备再申请低序设备必须在派发前拒绝。

    参数：无。返回：无。断言逆序 H→N 不能进入运行时，以全站稳定资源顺序消除
    两任务 A→B 与 B→A 的循环等待。异常：错误码文案漂移由匹配断言暴露。
    """

    with pytest.raises(ExecutionResourcePolicyError, match="静态死锁"):
        validate_static_device_tenancy_order(
            [
                _planned_node(
                    policy=_tenancy_policy(acquire="target"),
                    target=DEVICE_B_UUID,
                ),
                _planned_node(
                    policy=_tenancy_policy(acquire="target", release="source"),
                    source=DEVICE_B_UUID,
                    target=DEVICE_A_UUID,
                ),
                _planned_node(
                    policy=_tenancy_policy(release="source"),
                    source=DEVICE_A_UUID,
                ),
            ]
        )


def test_static_tenancy_rejects_reverse_acquisition_before_separate_release() -> None:
    """分开的装载与卸载节点也必须遵守全站稳定资源取得顺序。

    参数：无。返回：无；断言主物料先托管高序设备 B、下一节点再托管低序设备
    A，即使后续分别卸载，也会在冻结执行计划时被拒绝。异常：预期抛出
    ``ExecutionResourcePolicyError``，防止两个任务形成 A→B 与 B→A 循环等待。
    """

    with pytest.raises(ExecutionResourcePolicyError, match="静态死锁"):
        validate_static_device_tenancy_order(
            [
                _planned_node(
                    policy=_tenancy_policy(acquire="target"),
                    target=DEVICE_B_UUID,
                ),
                _planned_node(
                    policy=_tenancy_policy(acquire="target"),
                    target=DEVICE_A_UUID,
                ),
                _planned_node(
                    policy=_tenancy_policy(release="source"),
                    source=DEVICE_A_UUID,
                ),
                _planned_node(
                    policy=_tenancy_policy(release="source"),
                    source=DEVICE_B_UUID,
                ),
            ]
        )


def test_static_tenancy_rejects_workflow_that_finishes_loaded() -> None:
    """完整工作流结束时仍有活动设备托管必须失败关闭。

    参数：无。返回：无。断言只装载不卸载的计划不会在业务成功后遗留设备；
    工作流作者必须增加明确卸载和物理结算节点。异常：诊断缺失即测试失败。
    """

    with pytest.raises(ExecutionResourcePolicyError, match="未在工作流结束前释放"):
        validate_static_device_tenancy_order(
            [
                _planned_node(
                    policy=_tenancy_policy(acquire="target"),
                    target=DEVICE_A_UUID,
                )
            ]
        )


@pytest.mark.parametrize("role", ["device", "motion", "tool"])
def test_v2_roles_resolve_final_device_identity(role):
    contract = {"version": 2, "resource_params": [{"param": "rail", "role": role}]}
    policy = merge_action_resource_policy(contract, {})
    assert resolve_execution_resource_policy(
        policy, {"rail": _reference(DEVICE_B_UUID)}
    ).device_lock_keys == (f"/devices/{DEVICE_B_UUID}",)
    with pytest.raises(ExecutionResourcePolicyError):
        resolve_execution_resource_policy(policy, {})


def test_transfer_static_motion_alias_is_not_a_required_goal_parameter():
    contract = {
        "version": 2,
        "transfer": {
            "material_param": "sample",
            "target_owner_param": "target",
            "target_site_uuid_param": "target_site_uuid",
            "target_site_name_param": "target_site_name",
            "gripper_site_role": "gripper",
            "motion_resource_roles": ["station:rail"],
        },
    }
    policy = merge_action_resource_policy(contract, {})
    assert resolve_execution_resource_policy(policy, {}).device_lock_keys == ()
