"""框架按类型注入 ``ActionContext``：HostLink 本地执行、跨设备调用与 ROS 2（没有框架上下文）同一个判定。

注解为 ``ActionContext`` / ``Optional[ActionContext]`` / ``ActionContext | None`` 的参数都注入，名字随意；
叫 ``action_context`` 却没有这个注解的参数不再特殊。
"""

from __future__ import annotations

from typing import Any, Optional

from unilabos.client.host.hostlink.local_runtime import HostLinkDriverSpec, HostLinkLocalRuntime
from unilabos.client.host.runtime.action import ActionContext, bind_action_context


class ContextDriver:
    def __init__(self, device_id: Optional[str] = None, config: Optional[dict] = None) -> None:
        self.node: Any = None
        self.seen: dict[str, Any] = {}

    def post_init(self, node: Any) -> None:
        self.node = node

    def required(self, value: int, job: ActionContext) -> int:
        self.seen["required"] = job
        return value

    def optional(self, value: int, feedback: Optional[ActionContext] = None) -> int:
        self.seen["optional"] = feedback
        return value

    async def union(self, value: int, handle: ActionContext | None = None) -> int:
        self.seen["union"] = handle
        return value

    def untyped(self, value: int, action_context: Any = "unset") -> int:
        self.seen["untyped"] = action_context
        return value

    def relay(self, target: str) -> int:
        return self.node.call_device_action(target, "optional", {"value": 5})


def _runtime() -> HostLinkLocalRuntime:
    runtime = HostLinkLocalRuntime()
    for device_id in ("caller", "target"):
        runtime.add_driver(HostLinkDriverSpec(device_id, ContextDriver, {}))
    return runtime


def test_hostlink_injects_every_annotated_context_whatever_its_name() -> None:
    runtime = _runtime()
    runtime.start()
    try:
        driver = runtime.devices["caller"].driver
        context = ActionContext(action_id="job-1")
        assert runtime.call_action("caller", "required", value=1, action_context=context) == 1
        assert runtime.call_action("caller", "optional", value=2, action_context=context) == 2
        assert runtime.call_action("caller", "union", value=3, action_context=context) == 3
        assert driver.seen["required"] is context
        assert driver.seen["optional"] is context
        assert driver.seen["union"] is context
        # 调用方没给上下文：HostLink 执行仍有一个（取消、超时靠它），可空的参数同样拿到
        assert runtime.call_action("caller", "optional", value=4) == 4
        assert isinstance(driver.seen["optional"], ActionContext)
    finally:
        runtime.stop()


def test_untyped_action_context_name_is_no_longer_injected() -> None:
    runtime = _runtime()
    runtime.start()
    try:
        driver = runtime.devices["caller"].driver
        assert runtime.call_action("caller", "untyped", value=1, action_context=ActionContext()) == 1
        assert driver.seen["untyped"] == "unset"
    finally:
        runtime.stop()


def test_cross_device_call_injects_the_target_context_by_type() -> None:
    runtime = _runtime()
    runtime.start()
    try:
        assert runtime.call_action("caller", "relay", target="target") == 5
        assert isinstance(runtime.devices["target"].driver.seen["optional"], ActionContext)
    finally:
        runtime.stop()


def test_without_a_framework_context_optional_gets_none_and_required_gets_a_new_one() -> None:
    """ROS 2 执行没有框架上下文：可空的得到 None，不可空的总能拿到一个上下文。"""

    driver = ContextDriver()
    optional = bind_action_context(driver.optional, {"value": 1}, None)
    assert optional == {"value": 1, "feedback": None}
    required = bind_action_context(driver.required, {"value": 1}, None)
    assert isinstance(required["job"], ActionContext)
    assert bind_action_context(driver.untyped, {"value": 1}, None) == {"value": 1}
    # goal 里同名的值由框架的上下文覆盖
    context = ActionContext(action_id="job-2")
    assert bind_action_context(driver.union, {"value": 1, "handle": "forged"}, context)["handle"] is context
