"""后端统一执行接口：鉴权、读取冻结许可、接收证据并复用库存结算。"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Header, HTTPException

from .execution_authority import ExecutionAuthority
from .inventory.runtime_view import InventoryView


class BackendExecutionPort:
    """借用后端已打开的权威存储；不创建执行端自己的工作流或库存库。"""

    def __init__(self, workflow, inventory):
        self.workflow, self.inventory = workflow, inventory

    def call(self, operation, payload):
        if operation.startswith("materials."):
            view = InventoryView(self.inventory.store)
            if operation == "materials.index":
                return view.by_id
            if operation == "materials.snapshot":
                return view.snapshot(
                    payload["roles"], quantity_unit=payload.get("quantity_unit", "mL")
                )
            if operation == "materials.relations":
                return view.relations(payload["identifiers"])
        elif operation in {"authorize", "settle"}:
            # 绑定来自受信执行端，仍必须逐项匹配后端冻结的设备、动作和参数。
            gate = ExecutionAuthority(
                self.workflow, self.inventory, payload["bindings"]
            )
            if operation == "authorize":
                return gate.grant(payload["request"])
            return gate.settle(
                payload["request"], payload["proof"], grant=payload["grant"]
            )
        raise ValueError("unknown_execution_operation")


def create_runtime_execution_router(get_port, get_api_key):
    router = APIRouter(prefix="/runtime/execution", tags=["runtime"])

    @router.post("/{operation}")
    def call(
        operation: str, payload: dict, authorization: str | None = Header(default=None)
    ):
        key = get_api_key()
        if not key or not hmac.compare_digest(authorization or "", "Bearer " + key):
            raise HTTPException(401, "运行接口未通过工作区身份校验")
        port = get_port()
        if port is None:
            raise HTTPException(503, "工作流或库存权威尚未就绪")
        try:
            return {"code": 0, "data": port.call(operation, payload)}
        except (KeyError, TypeError, ValueError) as error:
            raise HTTPException(409, str(error)) from error

    return router
