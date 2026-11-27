"""接入身份确定后装配库存桥；不推断物料已交付或签收。"""
from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TYPE_CHECKING
from uuid import UUID

from .config import CenterConfigError
from .handoff import configure_handoffs
from .material import configure_materials

if TYPE_CHECKING:
    from unilabos.app.scheduler.inventory.store import InventoryStore
    from unilabos.workflow.store import WorkflowStore

    from .handoff import HandoffBridge
    from .runtime import CenterRuntime


def configure_inventory(runtime: CenterRuntime, inventory_store: InventoryStore, workflow_store: WorkflowStore,
                        *, environ: Mapping[str, str] | None = None) -> HandoffBridge:
    env = os.environ if environ is None else environ
    configured_lab = env.get("CENTER_LAB_UUID", "")
    lab = getattr(runtime.config, "lab_uuid", None) or configured_lab
    if not lab:
        raise CenterConfigError("中心未返回实验室身份，请升级中心；旧部署可配置 CENTER_LAB_UUID")
    try:
        if str(UUID(lab)) != lab:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as error:
        raise CenterConfigError("CENTER_LAB_UUID 必须为规范 UUID") from error
    if configured_lab and configured_lab != lab:
        raise CenterConfigError("CENTER_LAB_UUID 与中心登记的实验室身份不一致，请检查工站配置")
    from .handoff_operations import HandoffOperations
    from .handoff_policy import InstalledHandoffPolicy
    policy_path = env.get("CENTER_HANDOFF_POLICY_FILE")
    policy = InstalledHandoffPolicy(policy_path) if policy_path else None
    bridge = configure_handoffs(runtime, inventory_store, lab_uuid=lab,
                                incoming_plans=policy.plans if policy else None)
    bridge.operation_driven = policy is not None
    bridge.authority.include_template_evidence = policy is not None
    source_ready = arrival = None
    if policy and policy.value.get("adapter_provider"):
        from importlib import import_module

        from .handoff_queries import InventoryHandoffQueries

        module, factory = policy.value["adapter_provider"].split(":", 1)
        adapters = getattr(import_module(module), factory)(
            InventoryHandoffQueries(inventory_store, bridge.authority.station_uuid)
        )
        source_ready, arrival = adapters.get("source_ready"), adapters.get("arrival")
    runtime.handoff_operations = HandoffOperations(bridge, source_ready=source_ready, arrival=arrival)
    configure_materials(runtime, inventory_store, workflow_store, handoff_authority=bridge.authority)
    from .transport_preparation import TransportPreparationBridge
    from .transport_routes import TransportRoutes
    route_path=env.get("CENTER_TRANSPORT_ROUTE_FILE")
    runtime.transport_preparations = TransportPreparationBridge(runtime, inventory_store,
        routes=TransportRoutes(route_path) if route_path else None)
    return bridge
