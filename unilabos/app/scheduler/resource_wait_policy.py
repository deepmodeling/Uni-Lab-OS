"""把资源条件失败稳定分类为等待或不可恢复的合同错误。"""

from __future__ import annotations

_TEMPORARY_RESOURCE_CONDITIONS = frozenset(
    {
        "gripper_site_occupied",
        "operate_in_place_site_changed",
        "operate_in_place_site_missing",
        "site_claimed",
        "site_group_unavailable",
        "site_ingress_reserved",
        "site_occupied",
        "transfer_source_site_missing",
    }
)


def is_temporary_resource_condition(code: str) -> bool:
    """判断资源错误是否应保留作业等待下一次重排。

    参数：``code`` 是库存权威或库位解析器返回的稳定错误码。返回：条件会随
    其他作业、运输或物料移动而改变时为真；合同损坏和部署错误为假。异常：无，
    空值和未知值都按不可恢复处理，避免永久等待掩盖配置错误。
    """

    return str(code or "").strip() in _TEMPORARY_RESOURCE_CONDITIONS


__all__ = ["is_temporary_resource_condition"]
