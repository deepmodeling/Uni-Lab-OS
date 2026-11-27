"""工站资源条件等待/失败分类的合同测试。"""

from typing import Never

import pytest

from unilabos.app.scheduler.inventory.station_resource import (
    StationResourceError,
    TransferResourceRequest,
)
from unilabos.app.scheduler.resource_wait_policy import (
    is_temporary_resource_condition,
)
from unilabos.app.scheduler.site_target import ResolvedSiteTarget
from unilabos.app.scheduler.transfer_resource_set import (
    TransferResourceSetError,
    resolve_transfer_resource_set,
)


def test_runtime_resource_conditions_remain_waiting() -> None:
    """并发作业可改变的条件不能把工作流节点永久标记失败。

    参数：无。返回：无；断言入口运输、目标占用、夹爪占用与来源暂时缺失均
    属于可重排条件。异常：断言失败表示调度器会错误终结正常竞争作业。
    """

    assert is_temporary_resource_condition("site_ingress_reserved")
    assert is_temporary_resource_condition("site_group_unavailable")
    assert is_temporary_resource_condition("gripper_site_occupied")
    assert is_temporary_resource_condition("transfer_source_site_missing")


def test_contract_and_deployment_errors_fail_closed() -> None:
    """不会随并发状态改变的合同错误必须立即失败关闭。

    参数：无。返回：无；断言缺少夹爪角色、非法选择器和未知错误不会无限排队。
    异常：断言失败表示部署错误会被隐藏为资源忙。
    """

    assert not is_temporary_resource_condition("gripper_site_role_invalid")
    assert not is_temporary_resource_condition("site_selector_missing")
    assert not is_temporary_resource_condition("unknown_condition")


def test_transfer_resource_error_preserves_inventory_condition_code() -> None:
    """转运资源适配层必须保留库存权威的稳定条件码。

    参数：无。返回：无；断言包装后的错误仍可被统一等待策略分类。异常：无。
    """

    error = TransferResourceSetError(
        "gripper_site_occupied",
        "机械臂夹爪库位已有物料",
    )

    assert error.code == "gripper_site_occupied"
    assert error.message == "机械臂夹爪库位已有物料"
    assert is_temporary_resource_condition(error.code)


def test_transfer_resource_adapter_preserves_authoritative_wait_resources() -> None:
    """转运适配层不得丢弃库存权威给出的实际阻塞资源。"""

    class WaitingInventory:
        def resolve_transfer_resources(
            self,
            _request: TransferResourceRequest,
        ) -> Never:
            raise StationResourceError(
                "transfer_source_site_missing",
                "待搬物料没有来源库位",
                resources=(
                    {"scope": "material", "material_uuid": "material-1"},
                ),
            )

    with pytest.raises(TransferResourceSetError) as raised:
        resolve_transfer_resource_set(
            WaitingInventory(),  # type: ignore[arg-type]
            resource_material_uuid="material-1",
            target=ResolvedSiteTarget(
                uuid="site-target",
                name="IN",
                owner_material_uuid="device-target",
            ),
        )

    assert raised.value.resources == (
        {"scope": "material", "material_uuid": "material-1"},
    )
