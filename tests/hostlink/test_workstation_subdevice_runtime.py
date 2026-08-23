from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from unilabos.devices.workstation.workstation_base import WorkstationBase
from unilabos.hostlink import main_hostlink_run
from unilabos.hostlink.local_runtime import (
    HostLinkDriverSpec,
    HostLinkLocalRuntime,
)
from unilabos.resources.resource_tracker import ResourceDictInstance


class _AddService:
    class Request:
        def __init__(self, left: int = 0, right: int = 0) -> None:
            self.left = left
            self.right = right

    class Response:
        def __init__(self) -> None:
            self.total = 0


class _Workstation(WorkstationBase):
    def __init__(
        self,
        deck: Any = None,
        children: Any = None,
        protocol_type: Any = None,
        **_kwargs: Any,
    ) -> None:
        super().__init__(deck)
        self.constructed_children = list(children or [])
        self.protocol_type = list(protocol_type or [])
        self.child_started_during_post_init = False

    def post_init(self, node: Any) -> None:
        super().post_init(node)
        child = node.sub_devices.get("static-child")
        self.child_started_during_post_init = bool(
            child is None or child._started
        )


class _ServiceStatusDriver:
    def __init__(self, device_id: str, **_kwargs: Any) -> None:
        self.device_id = device_id
        self.value = 0

    def post_init(self, node: Any) -> None:
        self.node = node
        node.create_service(_AddService, "add", self.add)

    @staticmethod
    def add(request: _AddService.Request, response: _AddService.Response) -> Any:
        response.total = request.left + request.right
        return response

    def set_value(self, value: int) -> dict[str, int]:
        self.value = value
        return {"value": value}


def _device_config(
    device_id: str,
    registry_name: str,
) -> ResourceDictInstance:
    return ResourceDictInstance.get_resource_instance_from_dict(
        {
            "id": device_id,
            "uuid": f"{device_id}-uuid",
            "name": device_id,
            "type": "device",
            "class": registry_name,
        }
    )


def _definition(
    device_id: str,
    device_config: ResourceDictInstance,
    *,
    backend_name: str | None = None,
) -> SimpleNamespace:
    assert backend_name == "hostlink"
    is_station = device_config.res_content.klass == "test.workstation"
    return SimpleNamespace(
        driver_class=_Workstation if is_station else _ServiceStatusDriver,
        runtime_config={"protocol_type": []} if is_station else {},
        registry_name=str(device_config.res_content.klass),
        display_name=device_id,
        action_value_mappings=(
            {}
            if is_station
            else {"set_value": {"type": "SetValue", "goal": {"value": "value"}}}
        ),
        status_types={} if is_station else {"value": int},
        hardware_interface={},
        resource_uuid=device_config.res_content.uuid,
    )


def test_hostlink_graph_starts_subdevice_before_workstation_post_init(
    monkeypatch,
) -> None:
    station_config = _device_config("station", "test.workstation")
    child_config = _device_config("static-child", "test.child")
    station_config.children.append(child_config)
    monkeypatch.setattr(main_hostlink_run, "resolve_device_definition", _definition)

    runtime = main_hostlink_run.build_runtime(
        SimpleNamespace(root_nodes=[station_config])
    )
    station = runtime.devices["station"]
    child = runtime.devices["static-child"]

    assert station.sub_devices == {"static-child": child}
    assert child.resource_tracker is station.resource_tracker
    assert child.driver_instance is child.driver
    assert child.ros_node_instance is child

    runtime.start()
    try:
        assert child._started is True
        assert station.driver.child_started_during_post_init is True
        assert "/devices/static-child/add" in child.service_names()
    finally:
        runtime.stop()


def test_workstation_dynamic_subdevice_exposes_action_service_and_status(
    monkeypatch,
) -> None:
    from unilabos.device_runtime import definition as definition_module

    monkeypatch.setattr(definition_module, "resolve_device_definition", _definition)
    runtime = HostLinkLocalRuntime()
    station = runtime.add_driver(
        HostLinkDriverSpec(
            "station",
            _Workstation,
            {"protocol_type": []},
        )
    )
    changes: list[tuple[str, str]] = []
    runtime.add_device_change_listener(
        lambda event, node: changes.append((event, node.device_id))
    )
    runtime.start()
    try:
        dynamic_config = _device_config("dynamic-child", "test.child")
        created = station.create_device("dynamic-child", dynamic_config)

        assert created == {
            "success": True,
            "device_id": "dynamic-child",
            "registry_name": "test.child",
        }
        child = station.sub_devices["dynamic-child"]
        assert child is runtime.devices["dynamic-child"]
        assert child.resource_tracker is station.resource_tracker
        assert child._started is True
        assert dynamic_config in station.children
        assert changes[-2:] == [
            ("added", "dynamic-child"),
            ("updated", "dynamic-child"),
        ]

        assert runtime.call_action(
            "dynamic-child",
            "set_value",
            value=7,
        ) == {"value": 7}
        service_client = station.create_client(
            _AddService,
            "/devices/dynamic-child/add",
        )
        response = asyncio.run(
            service_client.call_async(_AddService.Request(left=4, right=5))
        )
        assert response.total == 9
        assert runtime.snapshot_states()["dynamic-child"] == {"value": 7}

        descriptor = next(
            item
            for item in runtime.descriptors()
            if item["id"] == "dynamic-child"
        )
        assert descriptor["actions"] == ["set_value"]
        assert descriptor["services"] == ["/devices/dynamic-child/add"]
        assert descriptor["status_fields"] == ["value"]

        assert station.destroy_device("dynamic-child") == {
            "success": True,
            "device_id": "dynamic-child",
        }
        assert "dynamic-child" not in runtime.devices
        assert "dynamic-child" not in station.sub_devices
        assert dynamic_config not in station.children
        assert not runtime.service_bus.has_service("/devices/dynamic-child/add")
        assert changes[-1] == ("removed", "dynamic-child")
    finally:
        runtime.stop()
