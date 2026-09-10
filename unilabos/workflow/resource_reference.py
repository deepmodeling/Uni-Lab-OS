"""可信工作流创作中的 ``resource_ref`` 关闭式身份解析。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from unilabos.workflow.models import validate_uuid

ResourceReferenceResolver = Callable[[str], Mapping[str, Any] | None]


class ResourceReferenceResolutionError(ValueError):
    """``resource_ref`` 不能解析为库存权威中的实际物料身份。"""


def resolve_resource_reference(
    resource_id: str,
    resolver: ResourceReferenceResolver | None,
) -> dict[str, str | None]:
    """把部署资源 ID 解析为实际物料（Material）与资源模板身份。

    参数：``resource_id`` 是作者源码中的非空静态业务 ID 或兼容 UUID；
    ``resolver`` 是组合根注入的只读库存权威（Inventory Authority）端口。
    返回：包含规范 ``uuid`` 及可选 ``resource_template_uuid`` 的分离字典。
    异常：业务 ID 缺少解析端口、解析器失败、身份不存在或回执非法时抛出
    ``ResourceReferenceResolutionError``；进程控制异常不被吞掉。
    """

    if (
        not isinstance(resource_id, str)
        or not resource_id.strip()
        or resource_id != resource_id.strip()
    ):
        raise ResourceReferenceResolutionError("resource_ref 必须使用非空静态资源 ID")
    if resolver is None:
        try:
            # ``legacy_uuid`` 仅维持已发布 UUID 源码的纯编译兼容；业务 ID 绝不
            # 经过此路径，也不能被原样写进物料 UUID 字段。
            legacy_uuid = validate_uuid(resource_id)
        except (TypeError, ValueError):
            raise ResourceReferenceResolutionError(
                "resource_ref 业务资源 ID 缺少库存权威解析器"
            ) from None
        if legacy_uuid != resource_id:
            raise ResourceReferenceResolutionError(
                "resource_ref 兼容 UUID 必须使用规范小写形式"
            )
        return {"uuid": legacy_uuid, "resource_template_uuid": None}
    try:
        # ``resolved`` 必须是库存权威返回的实际身份摘要，不接受名称或条码回退。
        resolved = resolver(resource_id)
    except Exception as error:
        raise ResourceReferenceResolutionError(
            "resource_ref 无法从库存权威解析实际物料身份"
        ) from error
    if not isinstance(resolved, Mapping):
        raise ResourceReferenceResolutionError(
            "resource_ref 未命中唯一活动物料身份"
        )
    try:
        # ``material_uuid`` 是唯一允许进入候选图参数的实际物料稳定身份。
        material_uuid = validate_uuid(resolved.get("uuid"))
        # ``resource_template_uuid`` 用于证明动作物料占位符（ResourceSlot）兼容性。
        resource_template_uuid = validate_uuid(
            resolved.get("resource_template_uuid")
        )
    except (TypeError, ValueError):
        raise ResourceReferenceResolutionError(
            "resource_ref 库存回执缺少规范物料或资源模板 UUID"
        ) from None
    return {
        "uuid": material_uuid,
        "resource_template_uuid": resource_template_uuid,
    }


__all__ = [
    "ResourceReferenceResolutionError",
    "ResourceReferenceResolver",
    "resolve_resource_reference",
]
