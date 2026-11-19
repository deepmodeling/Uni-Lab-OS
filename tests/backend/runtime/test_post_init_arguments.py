"""驱动 ``post_init`` 的可选入参：sites / resources / site_resources 按签名注入。"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from unilabos.backend.hostlink.local_runtime import (
    HostLinkDriverSpec,
    HostLinkLocalRuntime,
)
from unilabos.resources.objects.site import ResourceSite
from unilabos.resources.resource_tracker import ResourceDict, ResourceDictInstance

TEMPLATE = "post_init_args_device"


class _SiteAwareDriver:
    def __init__(self, **_kwargs: Any) -> None:
        self.received: dict[str, Any] | None = None

    def post_init(self, node: Any, sites=None, resources=None, site_resources=None) -> None:
        self.received = {
            "node": node,
            "sites": sites,
            "resources": resources,
            "site_resources": site_resources,
        }


class _PlainDriver:
    def __init__(self, **_kwargs: Any) -> None:
        self.args: tuple[Any, ...] | None = None

    def post_init(self, node: Any) -> None:
        self.args = (node,)


class _KwargsDriver:
    def __init__(self, **_kwargs: Any) -> None:
        self.kwargs: dict[str, Any] | None = None

    def post_init(self, node: Any, **kwargs: Any) -> None:
        self.kwargs = dict(kwargs)


def _container(name: str, material_uuid: str, parent_uuid: str) -> ResourceDictInstance:
    return ResourceDictInstance(
        ResourceDict.model_validate(
            {
                "id": name,
                "uuid": material_uuid,
                "parent_uuid": parent_uuid,
                "name": name,
                "type": "container",
                "class": "Container",
                "template_name": "test-container",
                "config": {"type": "Container"},
                "data": {},
                "extra": {},
                "sites": [],
                "sites_initialized": True,
            }
        )
    )


def _device_with_holdings() -> tuple[ResourceDictInstance, str, str]:
    """权威优先装配之后的设备节点：两个位点（一个被板占用）、一块板、一个直接挂载的台面。"""

    device_uuid, plate_uuid, bench_uuid = str(uuid4()), str(uuid4()), str(uuid4())

    def site(index: int, label: str, occupant: str | None) -> dict[str, Any]:
        return ResourceSite.model_validate(
            {
                "schema_version": 1,
                "uuid": str(uuid4()),
                "template_name": TEMPLATE,
                "material_uuid": device_uuid,
                "index": index,
                "label": label,
                "occupied_material_uuid": occupant,
            }
        ).model_dump()

    device = ResourceDictInstance(
        ResourceDict.model_validate(
            {
                "id": "heater",
                "uuid": device_uuid,
                "name": "heater",
                "type": "device",
                "class": TEMPLATE,
                "template_name": TEMPLATE,
                "config": {},
                "data": {},
                "extra": {},
                "sites": [site(1, "slot_1", plate_uuid), site(2, "slot_2", None)],
                "sites_initialized": True,
            }
        )
    )
    for child in (
        _container("plate-1", plate_uuid, device_uuid),
        _container("bench", bench_uuid, device_uuid),
    ):
        child.res_content.parent = device.res_content
        device.children.append(child)
    return device, plate_uuid, bench_uuid


def _spec(driver_class: type, device_config: ResourceDictInstance) -> HostLinkDriverSpec:
    return HostLinkDriverSpec(
        device_id=device_config.res_content.id,
        driver_class=driver_class,
        config={},
        registry_name=TEMPLATE,
        resource_uuid=device_config.res_content.uuid,
        device_config=device_config,
    )


def test_post_init_receives_sites_and_held_materials_by_signature() -> None:
    device, plate_uuid, bench_uuid = _device_with_holdings()
    runtime = HostLinkLocalRuntime()
    node = runtime.add_driver(_spec(_SiteAwareDriver, device))
    runtime.start()
    try:
        received = node.driver.received
        assert received is not None and received["node"] is node

        sites = received["sites"]
        assert set(sites) == {"slot_1", "slot_2"}
        assert sites["slot_1"].uuid == device.res_content.sites[0].uuid
        assert sites["slot_1"].occupied_material_uuid == plate_uuid

        resources = received["resources"]
        assert set(resources) == {"plate-1", "bench"}
        assert resources["plate-1"].unilabos_uuid == plate_uuid
        assert resources["bench"].unilabos_uuid == bench_uuid
        # 就是 tracker 里的那份实例，不是另一份拷贝
        assert resources["plate-1"] is node.resource_tracker.uuid_to_resources[plate_uuid]

        site_resources = received["site_resources"]
        assert site_resources["slot_1"] is resources["plate-1"]
        assert site_resources["slot_2"] is None
    finally:
        runtime.stop()


def test_post_init_without_optional_parameters_is_unchanged() -> None:
    device, _, _ = _device_with_holdings()
    runtime = HostLinkLocalRuntime()
    plain = runtime.add_driver(_spec(_PlainDriver, device))
    runtime.start()
    try:
        assert plain.driver.args == (plain,)
    finally:
        runtime.stop()


def test_post_init_var_keyword_does_not_receive_implicit_injection() -> None:
    """只按显式参数名注入：``**kwargs`` 不会悄悄收到一堆物料对象。"""

    device, _, _ = _device_with_holdings()
    runtime = HostLinkLocalRuntime()
    node = runtime.add_driver(_spec(_KwargsDriver, device))
    runtime.start()
    try:
        assert node.driver.kwargs == {}
    finally:
        runtime.stop()


def test_post_init_kwargs_without_device_config_is_empty_but_safe() -> None:
    runtime = HostLinkLocalRuntime()
    node = runtime.add_driver(
        HostLinkDriverSpec(device_id="bare", driver_class=_SiteAwareDriver, config={})
    )
    runtime.start()
    try:
        received = node.driver.received
        assert received["sites"] == {}
        assert received["resources"] == {}
        assert received["site_resources"] == {}
    finally:
        runtime.stop()
