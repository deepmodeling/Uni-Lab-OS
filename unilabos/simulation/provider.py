"""显式配置的模拟 provider 控制面；观察 HTTP 不提供动作或模式写入口。"""

from __future__ import annotations

import hmac
from typing import Literal

from fastapi import APIRouter, Header, HTTPException
from pydantic import Field, model_validator

from unilabos.runtime.observations import Contract, Sample, Selector, StateError


class ProviderConfig(Contract):
    group: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    versions: dict[str, str] = Field(min_length=1)
    actions: dict[str, list[str]] = Field(min_length=1)

    @model_validator(mode="after")
    def members(self):
        if not set(self.actions) <= set(self.versions) or any(not v for v in self.actions.values()):
            raise ValueError("provider actions must belong to configured members")
        return self


class Connect(Contract):
    operation_id: str = Field(min_length=1)
    source_session: str = Field(min_length=1)
    mode: Literal["simulation"] = "simulation"
    versions: dict[str, str]


class Lease(Contract):
    runtime_session: str
    source_session: str
    epoch: int


class Observations(Lease):
    samples: list[Sample] = Field(min_length=1, max_length=1000)


class Settlement(Lease):
    job_id: str
    receipt_id: str
    quiescent: Literal[True]


class Recovery(Lease):
    operation_id: str = Field(min_length=1)
    receipts: dict[str, str] = Field(default_factory=dict)
    quiescent: Literal[True]


class ProviderControl:
    """组合根必须提供独立模拟路由；配置白名单本身不能授权真实 Edge 派发。"""

    def __init__(self, observations, dispatcher, config, *, sources, simulation_dispatch=None):
        if not callable(simulation_dispatch):
            raise StateError("simulation_route_required", "必须显式注入模拟执行路由，不能回退到真实 Edge")
        self.observations = observations
        self.sources = sources
        self.config = ProviderConfig.model_validate(config)
        dispatcher.register_simulation_route(
            self.config.provider,
            simulation_dispatch,
            validate=self.validate_action,
        )

    def validate_action(self, payload):
        if payload.get("action") not in self.config.actions.get(payload.get("device_id"), []):
            raise StateError("execution_action_not_allowed")

    def connect(self, request: Connect):
        if request.versions != self.config.versions:
            raise StateError("version_incompatible")
        nodes = {n["id"]: n for n in self.observations.snapshot()["nodes"] if n["id"] in self.config.versions}
        if set(nodes) != set(self.config.versions) or any(
            not n["descriptor"] or n["descriptor"]["version"] != self.config.versions[key] for key, n in nodes.items()
        ):
            raise StateError("version_incompatible")
        self.sources.configure_group(self.config.group, [Selector(value=key) for key in self.config.versions])
        operation = self.sources.switch(
            self.config.group,
            operation_id=request.operation_id,
            mode=request.mode,
            provider=self.config.provider,
            source_session=request.source_session,
            prepare=lambda: None,
            quiescent=lambda: True,
        )
        # 这里只提交操作；worker 完成整个世界的准备后再单独 ACK。
        return {"operation": operation, "nodes": nodes}

    def check(self, lease: Lease):
        group = self.observations.snapshot()["groups"].get(self.config.group)
        if (
            not group
            or group["provider"] != self.config.provider
            or any(group[key] != getattr(lease, key) for key in ("runtime_session", "source_session", "epoch"))
        ):
            raise StateError("stale_source")
        return group

    def ready(self, lease: Lease):
        group = self.check(lease)
        for member in group["members"]:
            self.sources.ready(self.config.group, member, lease.epoch, lease.source_session)
        return self.observations.snapshot()["groups"][self.config.group]

    def disconnect(self, lease: Lease):
        self.check(lease)
        self.sources.disconnect(self.config.group)
        return {"degraded": True}

    def publish(self, request: Observations):
        self.check(request)
        if any(
            sample.group != self.config.group
            or sample.provider != self.config.provider
            or any(
                getattr(sample, key) != getattr(request, key) for key in ("runtime_session", "source_session", "epoch")
            )
            for sample in request.samples
        ):
            raise StateError("stale_source")
        return self.observations.publish_batch(request.samples)

    def settle(self, request: Settlement):
        self.check(request)
        action = self.sources.legacy_action(request.job_id)
        if action["group"] != self.config.group or any(
            action[key] != getattr(request, key) for key in ("runtime_session", "source_session", "epoch")
        ):
            raise StateError("stale_source")
        if not self.sources.context.inventory.has_receipt(request.job_id, request.receipt_id):
            raise StateError("canonical_inventory_receipt_missing")
        return self.sources.reconcile_legacy_action(
            request.job_id, evidence={"receipt_id": request.receipt_id}, quiescent=lambda: request.quiescent
        )

    def recover(self, request: Recovery):
        evidence = request.model_dump()
        try:
            receipt = self.sources.operation(request.operation_id)["receipt"]
        except StateError as error:
            if error.reason != "operation_missing":
                raise
        else:
            if (receipt.get("kind") != "recovery" or receipt.get("group") != self.config.group
                    or receipt.get("recovery_evidence") != evidence):
                raise StateError("operation_conflict")
            return receipt
        group = self.check(request)
        if not group["recovery_required"]:
            raise StateError("recovery_not_required")
        if set(request.receipts) != set(group["pending"]):
            raise StateError("reconciliation_required")
        # 重启后的 action 仍保留旧运行会话；仅用规范台账逐项确认，绝不重发动作。
        for job_id, receipt_id in request.receipts.items():
            action = self.sources.legacy_action(job_id)
            if action["group"] != self.config.group or any(
                action[key] != group[key] for key in ("provider", "source_session", "epoch")
            ):
                raise StateError("stale_source")
            if not self.sources.context.inventory.has_receipt(job_id, receipt_id):
                raise StateError("canonical_inventory_receipt_missing")
        for job_id, receipt_id in request.receipts.items():
            self.sources.reconcile_legacy_action(job_id, evidence={"receipt_id": receipt_id}, quiescent=lambda: request.quiescent)
        return self.sources.recover(self.config.group, quiescent=lambda: request.quiescent,
                                    operation_id=request.operation_id, evidence=evidence)


def create_provider_router(get_control, *, get_api_key=None, prefix="/device-state/observations/provider"):
    """挂在既有 Edge API，受同一部署访问控制；未显式配置时禁用。"""
    router = APIRouter(prefix=prefix, tags=["edge-scheduler"])

    def call(method, value, authorization):
        if get_api_key is not None:
            key = get_api_key()
            if not key or not hmac.compare_digest(authorization or "", "Bearer " + key):
                raise HTTPException(401, "workspace authentication required")
        control = get_control()
        if control is None:
            raise HTTPException(503, "twin provider not configured")
        try:
            return {"code": 0, "data": getattr(control, method)(value)}
        except StateError as error:
            raise HTTPException(409, {"reason": error.reason, "msg": str(error)}) from error

    @router.post("/connect")
    def connect(body: Connect, authorization: str | None = Header(default=None)):
        return call("connect", body, authorization)

    @router.post("/ready")
    def ready(body: Lease, authorization: str | None = Header(default=None)):
        return call("ready", body, authorization)

    @router.post("/disconnect")
    def disconnect(body: Lease, authorization: str | None = Header(default=None)):
        return call("disconnect", body, authorization)

    @router.post("/samples")
    def samples(body: Observations, authorization: str | None = Header(default=None)):
        return call("publish", body, authorization)

    @router.post("/settle")
    def settle(body: Settlement, authorization: str | None = Header(default=None)):
        return call("settle", body, authorization)

    @router.post("/recover")
    def recover(body: Recovery, authorization: str | None = Header(default=None)):
        return call("recover", body, authorization)

    return router
