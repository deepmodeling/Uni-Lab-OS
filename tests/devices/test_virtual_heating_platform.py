from __future__ import annotations

import inspect
import json
from pathlib import Path
from uuid import uuid4

import pytest

from unilabos.client.host.hostlink.local_runtime import HostLinkDriverSpec, HostLinkLocalRuntime
from unilabos.client.host.runtime.action import ActionContext
from unilabos.client.host.runtime.resource import AuthorityResourceService
from unilabos.client.materials import LocalMaterialsClient
from unilabos.config.config import BasicConfig
from unilabos.devices.virtual.heating_platform import (
    SAMPLE_DISPLAY_COLORS,
    SAMPLE_TEMPLATE,
    VirtualHeatingPlatform,
    VirtualHeatingSample,
)
from unilabos.registry.ast_registry_scanner import _parse_file
from unilabos.registry.decorators import get_device_meta
from unilabos.resources.objects.resource import ResourceDict
from unilabos.resources.resource_tracker import ResourceDictInstance
from unilabos.server.execution_composition import set_materials_gateway
from unilabos.server.services.materials import MaterialsService
from tests.heating_fixtures import heating_device_config


def test_virtual_heating_platform_is_registry_discoverable_and_demo_graph_is_strict() -> (
    None
):
    metadata = get_device_meta(VirtualHeatingPlatform, "virtual_heating_platform")
    assert metadata is not None
    assert metadata["supported_backends"] == ["hostlink", "ros2"]
    assert [site["label"] for site in metadata["available_sites"]] == [
        "site_1",
        "site_2",
        "site_3",
    ]

    source = Path(inspect.getfile(VirtualHeatingPlatform)).resolve()
    repository_root = Path(__file__).resolve().parents[2]
    devices, resources, _workflows = _parse_file(source, repository_root)
    assert devices[0]["device_id"] == "virtual_heating_platform"
    assert "serialized_state" in devices[0]["status_properties"]
    # 样品是注册表里的 PLR 资源类：权威里的模板名就是这个资源 id
    sample_entry = next(item for item in resources if item["resource_id"] == SAMPLE_TEMPLATE)
    assert sample_entry["module"] == "unilabos.devices.virtual.heating_platform:VirtualHeatingSample"
    assert sample_entry["category"] == ["heating_sample"]

    graph_path = (
        repository_root
        / "unilabos"
        / "test"
        / "experiments"
        / "virtual_heating_platform_demo.json"
    )
    graph_payload = json.loads(graph_path.read_text(encoding="utf-8"))
    assert graph_payload["links"] == []
    device = ResourceDict.model_validate(graph_payload["nodes"][0])
    assert device.klass == "virtual_heating_platform"
    assert device.sites_initialized is True
    assert [site.label for site in device.sites or []] == ["site_1", "site_2", "site_3"]


def test_sample_state_round_trips_through_plr_serialization() -> None:
    sample = VirtualHeatingSample("probe")
    sample.record_temperature(
        site_id=2,
        source={"device_id": "heater", "property": "site_2_temperature_c"},
        temperature_c=61.23456,
        target_temperature_c=80.0,
        progress=50.0,
        state="heating",
        observed_at_ms=1234,
    )

    restored = VirtualHeatingSample.deserialize(sample.serialize())
    restored.load_state(sample.serialize_state())

    assert restored.serialize_state() == sample.serialize_state()
    assert restored.temperature_c == 61.235
    assert restored.state == "heating"


class _Heater:
    """一台跑在 HostLink 本地运行时上的加热平台：设备物料按注册表 available_sites 在权威里装配。"""

    def __init__(self, service: MaterialsService, device_id: str, config: dict | None = None) -> None:
        self.service = service
        self.gateway = LocalMaterialsClient(service)
        self.device_id = device_id
        self.config = dict(config or {"update_interval_s": 0.05})
        self.device_uuid = str(uuid4())
        self.runtime: HostLinkLocalRuntime | None = None
        self.node = None

    def _device_config(self) -> ResourceDictInstance:
        # 权威优先装配：已有设备时取权威的 Site 与下挂物料（重启时样品从这里挂回）
        return heating_device_config(self.device_id, self.gateway, device_uuid=self.device_uuid)

    def start(self) -> VirtualHeatingPlatform:
        self.runtime = HostLinkLocalRuntime()
        self.node = self.runtime.add_driver(
            HostLinkDriverSpec(
                self.device_id,
                VirtualHeatingPlatform,
                self.config,
                registry_name="virtual_heating_platform",
                resource_uuid=self.device_uuid,
                device_config=self._device_config(),
            )
        )
        self.runtime.set_resource_service(AuthorityResourceService(self.gateway))
        self.runtime.start()
        return self.node.driver

    def stop(self) -> None:
        if self.runtime is not None:
            self.runtime.stop()
            self.runtime = None

    def sites(self) -> dict[str, object]:
        return {site.label: site for site in self.service.get_material(self.device_uuid).sites}


@pytest.fixture
def heater(tmp_path, monkeypatch):
    service = MaterialsService(tmp_path / "materials.db")
    monkeypatch.setattr(BasicConfig, "is_host_mode", True)
    set_materials_gateway(LocalMaterialsClient(service))
    rig = _Heater(service, "heater-demo")
    try:
        yield rig
    finally:
        rig.stop()
        set_materials_gateway(None)
        service.close()


def test_heater_creates_plr_samples_on_its_own_sites_and_heats_them(heater) -> None:
    platform = heater.start()
    service = heater.service

    sites = heater.sites()
    assert len(sites) == 3 and all(site.occupied_material_uuid for site in sites.values())
    # 只有设备物料和三个样品；没有私有的"平台物料"
    assert len(service.list_materials()) == 4
    tracker = heater.node.resource_tracker
    for index, label in enumerate(("site_1", "site_2", "site_3"), start=1):
        sample = service.get_material(sites[label].occupied_material_uuid)
        assert sample.material.parent_material_uuid == heater.device_uuid
        assert sample.material.config["type"] == "VirtualHeatingSample"
        assert sample.material.template_name == SAMPLE_TEMPLATE
        assert sample.material.resource_id == f"heater-demo-sample-{index}"
        assert sample.material.display_name == f"加热样品 {index}"
        assert sample.material.barcode == f"OPENLAB-HEAT-{index}"
        assert sample.material.meta_data["display_color"] == SAMPLE_DISPLAY_COLORS[index - 1]
        # uuid → 实例唯一：驱动拿着的就是设备 tracker 登记的那一份
        instance = tracker.uuid_to_resources[sample.material.material_uuid]
        assert isinstance(instance, VirtualHeatingSample)
        assert platform._samples[index] is instance

    result = platform.heat_site(
        site_id=2,
        target_temperature_c=72.5,
        duration_seconds=0.1,
        action_context=ActionContext(action_id="demo-job-1"),
    )

    material = service.get_material(result["material_uuid"])
    assert material.data.data["temperature_c"] == 72.5
    assert material.data.data["serialized_state"] == {
        "site_id": 2,
        "temperature_c": 72.5,
        "target_temperature_c": 72.5,
        "progress": 100.0,
        "state": "completed",
        "observed_at_ms": material.data.data["serialized_state"]["observed_at_ms"],
    }
    assert "temperature_history" not in material.data.data
    assert material.data.data["temperature_source"] == {
        "device_id": "heater-demo",
        "property": "site_2_temperature_c",
    }

    serialized = platform.serialize()
    assert serialized["platform_material_uuid"] == heater.device_uuid
    assert [site["material_uuid"] for site in serialized["sites"]] == [
        sites[label].occupied_material_uuid for label in ("site_1", "site_2", "site_3")
    ]
    assert serialized["sites"][1]["temperature_c"] == 72.5
    assert serialized["sites"][1]["material_name"] == "加热样品 2"


def test_heater_restart_adopts_the_same_samples_with_their_temperature(heater) -> None:
    """二次启动：样品由框架从权威反序列化成 PLR 实例挂回设备，驱动接管同一批，不重复创建。"""

    first = heater.start()
    first.heat_site(
        site_id=1,
        target_temperature_c=60.0,
        duration_seconds=0.1,
        action_context=ActionContext(action_id="first-run"),
    )
    before = {label: site.occupied_material_uuid for label, site in heater.sites().items()}
    heater.stop()

    second = heater.start()

    assert {label: site.occupied_material_uuid for label, site in heater.sites().items()} == before
    assert len(heater.service.list_materials()) == 4
    sample = second._samples[1]
    assert isinstance(sample, VirtualHeatingSample)
    assert sample.unilabos_uuid == before["site_1"]
    assert heater.node.resource_tracker.uuid_to_resources[sample.unilabos_uuid] is sample
    assert sample.temperature_c == 60.0
    assert second.site_1_temperature_c == 60.0
    assert second.serialize()["sites"][0]["material_name"] == "加热样品 1"

    result = second.heat_site(
        site_id=1,
        target_temperature_c=70.0,
        duration_seconds=0.1,
        action_context=ActionContext(action_id="second-run"),
    )
    assert result["material_uuid"] == before["site_1"]
    assert heater.service.get_material(before["site_1"]).data.data["temperature_c"] == 70.0


def test_material_temperature_is_latest_passive_device_projection(heater) -> None:
    platform = heater.start()
    for index in range(3):
        platform._write_material_temperature(
            1,
            temperature_c=25.0 + index,
            target_temperature_c=150.0,
            state="heating",
            progress=float(index),
        )
    material = heater.service.get_material_by_resource_id("heater-demo-sample-1")
    assert material.data.data["temperature_c"] == 27.0
    assert "temperature_history" not in material.data.data
    assert material.data.data["temperature_observed_at_ms"] > 0
    assert platform.site_1_temperature_c == 27.0


def test_heater_needs_its_device_node() -> None:
    with pytest.raises(RuntimeError, match="post_init"):
        VirtualHeatingPlatform("orphan-heater").initialize()
