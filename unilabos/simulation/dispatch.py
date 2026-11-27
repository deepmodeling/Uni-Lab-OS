"""现有 Task/Job 执行器的可选模式门；未知模拟路由不会回退真实设备。"""

from __future__ import annotations

from typing import Any, Callable

from unilabos.runtime.observations import Selector, StateError


class SourceDispatcher:
    def __init__(self, delegate, observations, sources):
        self.delegate, self.observations, self.sources = delegate, observations, sources
        self.routes: dict[str, Callable[[dict, dict], Any]] = {}
        self.validators = {}

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def register_simulation_route(self, provider: str, dispatch: Callable[[dict, dict], Any], *, validate=None):
        """仅由 OS 组合根登记可信 provider；观察 HTTP 不暴露此入口。"""
        if not provider or provider in self.routes:
            raise StateError("provider_conflict")
        self.routes[provider] = dispatch
        self.validators[provider] = validate

    def dispatch(self, payload):
        # 未纳入孪生组的既有设备继续使用既有派发链。
        snapshot = self.observations.snapshot()
        identifier = payload.get("device_id")
        nodes = [node for node in snapshot["nodes"] if node["id"] == identifier]
        groups = [
            (name, group)
            for name, group in snapshot["groups"].items()
            if identifier in group.get("member_ids", {}).values()
            or any(node["uuid"] in group["members"] for node in nodes)
        ]
        if not groups:
            return self.delegate.dispatch(payload)
        if len(nodes) != 1 or len(groups) != 1:
            raise StateError("identity_ambiguous")
        _, group = groups[0]
        if nodes[0]["uuid"] not in group["members"]:
            raise StateError("identity_changed")
        route = self.routes.get(group["provider"])
        if route is None:
            raise StateError("execution_route_unavailable")
        validate = self.validators.get(group["provider"])
        if validate is not None:
            validate(payload)
        admission = self.sources.execution_source(
            Selector(value=identifier),
            provider=group["provider"],
        )
        # 路由只选择执行来源；幂等和未知结果阻断由原生执行权威负责。
        return route(payload, admission)
