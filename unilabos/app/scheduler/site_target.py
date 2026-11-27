"""转运动作的库位（Site）稳定身份解析。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from unilabos.app.scheduler.inventory.station_resource import (
    StationResourceError,
    StationResourceInventory,
    TargetSiteRequest,
)


class SiteTargetResolutionError(ValueError):
    """目标库位无法安全解析或当前不可使用。

    参数：``code`` 是稳定诊断码，``message`` 是中文原因。返回：异常对象。
    异常：构造过程不再抛出其他异常。
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        resources: Sequence[Mapping[str, str]] = (),
    ) -> None:
        """保存稳定错误码和中文诊断。

        参数：``code`` 用于日志和测试判断，``message`` 供调用方展示；
        ``resources`` 是库存权威给出的实际阻塞资源。返回：无。异常：不主动抛出
        其他异常。
        """

        super().__init__(message)
        self.code = code
        self.message = message
        self.resources = tuple(dict(resource) for resource in resources)


@dataclass(frozen=True, slots=True)
class ResolvedSiteTarget:
    """已经由本地库存权威确认的目标库位。

    参数：``uuid`` 是库位稳定身份，``name`` 是设备执行所需名称，
    ``owner_material_uuid`` 是拥有该库位的父物料身份。返回：不可变值对象。
    异常：构造本身不校验，统一由 ``resolve_site_target`` 校验。
    """

    uuid: str
    name: str
    owner_material_uuid: str


def resolve_site_target(
    inventory: StationResourceInventory,
    *,
    owner_material_uuid: str,
    site_uuid: str = "",
    site_name: str = "",
    site_uuids: Sequence[str] = (),
    occupant_material_uuid: str = "",
    unavailable_site_uuids: Sequence[str] = (),
    require_available: bool = True,
) -> ResolvedSiteTarget:
    """按稳定身份优先、名称兜底解析一个可用目标库位。

    参数：``inventory`` 是不暴露 Store/SQL 的工站资源库存接口；
    ``owner_material_uuid`` 是 ``mount_resource.uuid``；``site_uuid`` 是优先使用
    的库位稳定身份；``site_name`` 是旧工作流兼容名称；``site_uuids`` 是显式
    等价库位组，按库存 ``sort_order`` 选择首个可用位置；
    ``occupant_material_uuid`` 是本次准备放入的物料身份；
    ``unavailable_site_uuids`` 是本轮已经被其他作业执行占用（Claim）选中的位置。
    ``require_available`` 为假时只解析身份和模板兼容性，把占用与 Claim 留给同一
    库存事务内的 Gate 7 复验。
    返回：数据库中的规范库位 UUID、名称和拥有者。异常：库位不存在、归属错误、
    UUID 与名称不一致，或全部候选已占用/申领时抛
    ``SiteTargetResolutionError``，不得退化执行。
    """

    try:
        target = inventory.resolve_target_site(
            TargetSiteRequest(
                owner_material_uuid=owner_material_uuid,
                site_uuid=site_uuid,
                site_name=site_name,
                equivalent_site_uuids=tuple(site_uuids),
                occupant_material_uuid=occupant_material_uuid,
                unavailable_site_uuids=tuple(unavailable_site_uuids),
                require_available=require_available,
            )
        )
    except StationResourceError as error:
        raise SiteTargetResolutionError(
            error.code,
            error.message,
            resources=error.resources,
        ) from error
    return ResolvedSiteTarget(
        uuid=target.uuid,
        name=target.name,
        owner_material_uuid=target.owner_material_uuid,
    )


__all__ = [
    "ResolvedSiteTarget",
    "SiteTargetResolutionError",
    "resolve_site_target",
]
