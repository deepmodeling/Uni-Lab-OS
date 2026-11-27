"""目标 Edge 入口库位预留的 HTTP 适配器。"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from unilabos.app.scheduler.inventory.ingress import (
    IngressReservationError,
    StationIngressAuthority,
)
from unilabos.app.scheduler.inventory.store import InventoryStore


class _StrictModel(BaseModel):
    """拒绝未知字段的入口预留 DTO 基类。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class IngressReserveRequest(_StrictModel):
    """Backend 在 AGV 开始运输前提交的入口预留命令。"""

    idempotency_key: str
    carrier_material_uuid: str
    candidate_site_uuids: list[str] = Field(min_length=1)
    ttl_seconds: int = Field(strict=True, gt=0)
    backend_task_uuid: str | None = None
    invocation_key: str | None = None

    @field_validator("idempotency_key", "carrier_material_uuid")
    @classmethod
    def _required_text(cls, value: str) -> str:
        """规范必填文本；空值由 Pydantic 映射为 422。"""

        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized


class IngressCancelRequest(_StrictModel):
    """人工取消未完成入口预留的审计命令。"""

    reason: str

    @field_validator("reason")
    @classmethod
    def _reason(cls, value: str) -> str:
        """规范人工理由；空值由 Pydantic 映射为 422。"""

        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized


class IngressReservationResponse(_StrictModel):
    """入口预留的公共持久投影。"""

    uuid: str
    idempotency_key: str
    backend_task_uuid: str | None
    invocation_key: str | None
    carrier_material_uuid: str
    site_uuid: str
    state: Literal["reserved", "in_transit", "received", "expired", "canceled"]
    expires_at: str
    prepared_at: str | None
    transport_started_at: str | None
    received_at: str | None
    canceled_at: str | None
    cancel_reason: str | None
    revision: int
    create_time: str
    update_time: str


def create_ingress_router(
    store: InventoryStore,
    *,
    edge_id: str,
    lab_id: str,
) -> APIRouter:
    """创建只依赖入口深模块的薄 HTTP 路由。

    参数：库存 Store 与事件身份来自组合根。返回：挂载在库存路由下的 APIRouter。
    异常：构造不访问数据库；请求期领域冲突映射为 404/409，非法 DTO 由 FastAPI
    映射为 422。
    """

    authority = StationIngressAuthority(
        store,
        edge_id=edge_id,
        lab_id=lab_id,
    )
    router = APIRouter(prefix="/ingress-reservations")

    def call(operation: Any, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """调用入口权威并把稳定领域错误映射到 HTTP 边界。

        参数：``operation`` 是入口深模块方法，其余参数原样传递。返回：持久预留
        投影。异常：不存在映射为 404，其余领域冲突映射为 409；未知异常原样传播。
        """

        try:
            return operation(*args, **kwargs)
        except IngressReservationError as error:
            status = 404 if error.code == "ingress_reservation_not_found" else 409
            raise HTTPException(
                status_code=status,
                detail={"code": error.code, "message": error.message},
            ) from error

    @router.post("", response_model=IngressReservationResponse, status_code=201)
    def reserve(body: IngressReserveRequest) -> dict[str, Any]:
        """原子选择并预留一个目标入口库位。

        参数：``body`` 携带载体、等价候选组、TTL 和幂等身份。返回：``reserved``
        投影。异常：容量或重放冲突由统一适配器映射为 HTTP 409。
        """

        return call(authority.reserve, **body.model_dump())

    @router.get("/{reservation_uuid}", response_model=IngressReservationResponse)
    def get(reservation_uuid: str) -> dict[str, Any]:
        """读取入口预留并收敛运输前 TTL。

        参数：``reservation_uuid`` 是预留身份。返回：最新持久投影。异常：非法或
        不存在的身份分别映射为 409 或 404。
        """

        return call(authority.get, reservation_uuid)

    @router.post("/{reservation_uuid}/prepared", response_model=IngressReservationResponse)
    def protect_prepared(reservation_uuid: str) -> dict[str, Any]:
        """持久保护尚未过期的入口；保持 reserved，不表示已有发运许可。"""

        return call(authority.protect_prepared, reservation_uuid)

    @router.post(
        "/{reservation_uuid}/in-transit",
        response_model=IngressReservationResponse,
    )
    def mark_in_transit(reservation_uuid: str) -> dict[str, Any]:
        """确认 AGV 已装载，禁止后续自然过期。

        调用方须另行核对发运许可，本接口只记录已经发生的装载运输事实。
        参数：``reservation_uuid`` 是仍处于 ``reserved`` 的预留身份。返回：
        ``in_transit`` 投影。异常：非法状态转换映射为 HTTP 409。
        """

        return call(authority.mark_in_transit, reservation_uuid)

    @router.post(
        "/{reservation_uuid}/receive",
        response_model=IngressReservationResponse,
    )
    def receive(reservation_uuid: str) -> dict[str, Any]:
        """由目标 Edge 确认载体已进入预留库位。

        参数：``reservation_uuid`` 是运输中预留。返回：原子结算库存后的
        ``received`` 投影。异常：库位或载体位置冲突映射为 HTTP 409。
        """

        return call(authority.receive, reservation_uuid)

    @router.post(
        "/{reservation_uuid}/cancel",
        response_model=IngressReservationResponse,
    )
    def cancel(
        reservation_uuid: str,
        body: IngressCancelRequest,
    ) -> dict[str, Any]:
        """以人工理由取消尚未完成的入口预留。

        参数：``reservation_uuid`` 定位预留，``body.reason`` 保存人工审计理由。
        返回：``canceled`` 投影。异常：已接收或理由冲突映射为 HTTP 409。
        """

        return call(authority.cancel, reservation_uuid, reason=body.reason)

    return router


__all__ = ["create_ingress_router"]
