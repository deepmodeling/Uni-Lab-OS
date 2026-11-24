"""设备持有的物料建不成 PLR 实例时，这台设备进入维护模式：不启动，Host 与其余设备照常运行。

权威只核对 ``config.type`` 的格式；类是否存在、能否反序列化只有 Host 知道。建不成（缺 / 写错
``config.type``、类不存在、反序列化失败）不再跳过那棵物料让设备照常起来，而是这台设备不启动，原因
（设备、物料 uuid、``config.type``）登记为不可用设备，随能力快照到 ``GET /api/v1/devices``，
并出现在 ``/api/v1/health`` 里。
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from unilabos.client.host.hostlink.local_runtime import HostLinkLocalRuntime
from unilabos.client.host.hostlink.main_hostlink_run import populate_runtime
from unilabos.client.host.runtime import definition as definition_module
from unilabos.client.host.runtime.driver_creator import build_device_materials
from unilabos.client.host.runtime.exception import DeviceMaterialError
from unilabos.registry.registry import lab_registry
from unilabos.resources.resource_tracker import ResourceTreeSet
from unilabos.server.services.materials.graph import graph_resource_tree
from tests.plr_fixtures import plate

MODULE = __name__


class PlainDriver:
    def __init__(self, device_id: str = "", **_kwargs) -> None:
        self.device_id = device_id


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setitem(lab_registry.device_type_registry, "test_holder", {
        "class": {"module": f"{MODULE}:PlainDriver", "type": "python"}})
    # 不接物料权威：设备的子树就是图里给的
    monkeypatch.setattr(definition_module, "apply_device_authority_state", lambda *args, **kwargs: None)


def _device(device_id: str, parent: str | None = None) -> dict:
    return {"id": device_id, "uuid": str(uuid4()), "type": "device", "class": "test_holder",
            "template_name": "test_holder", "parent": parent, "config": {}}


def _plate_nodes(name: str, device: dict, *, break_with=None) -> list[dict]:
    nodes = ResourceTreeSet.from_plr_resources([plate(name)], known_random_uuid=True).dump()[0]
    nodes[0]["parent_uuid"] = device["uuid"]
    if break_with is not None:
        break_with(nodes)
    return nodes


def _unknown_class(nodes: list[dict]) -> None:
    nodes[0]["config"]["type"] = "NotAPlrClass"


def _missing_type_on_a_well(nodes: list[dict]) -> None:
    nodes[5]["config"].pop("type")


def _well_without_location(nodes: list[dict]) -> None:
    nodes[5]["pose"]["position"] = None


def _graph(*groups: list[dict]):
    return graph_resource_tree([node for group in groups for node in group])


@pytest.mark.parametrize(
    "break_with, named_node, shown_type",
    [(_unknown_class, 0, "NotAPlrClass"), (_missing_type_on_a_well, 5, None), (_well_without_location, 0, "Plate")],
)
def test_only_the_device_with_a_bad_material_stays_down(registry, break_with, named_node, shown_type) -> None:
    bad, good = _device("heater_bad"), _device("heater_ok")
    child = _device("heater_bad_probe", parent="heater_bad")
    bad_nodes = _plate_nodes("bad_plate", bad, break_with=break_with)
    devices = _graph([bad, child, good], bad_nodes, _plate_nodes("good_plate", good))

    runtime = populate_runtime(HostLinkLocalRuntime(), devices)
    runtime.start()
    try:
        assert set(runtime.devices) == {"heater_ok"}
        assert [res.name for res in runtime.devices["heater_ok"].resource_tracker.resources] == ["good_plate"]
        record = runtime.unavailable_devices["heater_bad"]
        assert record["maintenance"] is True
        assert record["material_uuid"] == bad_nodes[named_node]["uuid"]
        assert record["material_type"] == shown_type
        assert "heater_bad" in record["reason"] and bad_nodes[named_node]["uuid"] in record["reason"]
        # 子设备没有宿主，一并不启动
        assert runtime.unavailable_devices["heater_bad_probe"]["parent_device_id"] == "heater_bad"
    finally:
        runtime.stop()


def test_build_names_the_material_and_its_type() -> None:
    device = _device("holder")
    tree = _graph([device], _plate_nodes("p", device, break_with=_unknown_class)).trees[0]

    with pytest.raises(DeviceMaterialError) as caught:
        build_device_materials(tree.root_node.children)

    assert caught.value.material_type == "NotAPlrClass"
    assert "NotAPlrClass" in str(caught.value)


def test_the_maintenance_record_reaches_the_device_catalog_route() -> None:
    from unilabos.client.host.capabilities import build_endpoint_capabilities

    error = DeviceMaterialError(material_uuid="m-1", material_type="Typo", reason="类不存在", device_id="heater")
    adapter = SimpleNamespace(
        devices_names={}, _action_value_mappings={},
        unavailable_devices={"heater": {"device_id": "heater", "registry_name": "test_holder", **error.unavailable_record()}},
    )

    routes, _capabilities = build_endpoint_capabilities(adapter, observed_at_ms=1)

    route = next(route for route in routes if route.device_uuid == "heater")
    assert route.enabled is False
    assert route.config["unavailable"]["maintenance"] is True
    assert route.config["unavailable"]["material_uuid"] == "m-1"
    assert "Typo" in route.config["unavailable"]["reason"]


def test_health_lists_the_device_in_maintenance(registry, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from unilabos.client.host.hostlink import main_hostlink_run
    from unilabos.server.api import edge_proxy
    from unilabos.server.api.runtime.diagnostics import create_backend_app

    bad = _device("heater_bad")
    runtime = populate_runtime(HostLinkLocalRuntime(), _graph([bad], _plate_nodes("p", bad, break_with=_unknown_class)))
    monkeypatch.setattr(main_hostlink_run, "_runtime", SimpleNamespace(local=runtime))
    edge_proxy.configure_edge_proxy(False)
    client = TestClient(create_backend_app(get_scheduler=lambda: object()))

    [record] = client.get("/api/v1/health").json()["data"]["unavailable_devices"]
    assert record["device_id"] == "heater_bad" and record["maintenance"] is True
    assert record["material_type"] == "NotAPlrClass"
