from __future__ import annotations

import inspect
import json
from pathlib import Path

from unilabos.server.database.repositories.materials import MaterialsRepository
from unilabos.client.materials import LocalMaterialsClient
from unilabos.config.config import BasicConfig
from unilabos.device_runtime.action import ActionContext
from unilabos.devices.virtual.heating_platform import (
    SAMPLE_DISPLAY_COLORS,
    VirtualHeatingPlatform,
)
from unilabos.registry.ast_registry_scanner import _parse_file
from unilabos.registry.decorators import get_device_meta
from unilabos.resources.objects.resource import ResourceDict
from unilabos.server.scheduler.integration import set_materials_gateway
from unilabos.server.services.materials import MaterialsService


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
    devices, _resources = _parse_file(source, repository_root)
    assert devices[0]["device_id"] == "virtual_heating_platform"
    assert "serialized_state" in devices[0]["status_properties"]

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


def test_virtual_heating_platform_creates_places_and_heats_real_materials(
    tmp_path, monkeypatch
) -> None:
    service = MaterialsService(MaterialsRepository(tmp_path / "materials.db"))
    monkeypatch.setattr(BasicConfig, "is_host_mode", True)
    set_materials_gateway(LocalMaterialsClient(service))
    try:
        platform = VirtualHeatingPlatform("heater-demo", {"update_interval_s": 0.05})

        assert platform.initialize() is True
        root = service.get_material_by_resource_id("heater-demo")
        assert len(root.sites) == 3
        assert all(site.occupied_material_uuid for site in root.sites)
        assert len(service.list_materials()) == 4
        samples = [
            service.get_material(site.occupied_material_uuid)
            for site in root.sites
            if site.occupied_material_uuid
        ]
        assert [item.material.meta_data["display_color"] for item in samples] == list(
            SAMPLE_DISPLAY_COLORS
        )

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
        assert material.data.source_job_uuid == "demo-job-1"

        serialized = platform.serialize()
        assert serialized["platform_material_uuid"] == root.material.material_uuid
        assert [site["material_uuid"] for site in serialized["sites"]] == [
            site.occupied_material_uuid for site in root.sites
        ]
        assert serialized["sites"][1]["temperature_c"] == 72.5
    finally:
        set_materials_gateway(None)
        service.repository.close()


def test_material_temperature_is_latest_passive_device_projection(
    tmp_path, monkeypatch
) -> None:
    service = MaterialsService(MaterialsRepository(tmp_path / "materials.db"))
    monkeypatch.setattr(BasicConfig, "is_host_mode", True)
    set_materials_gateway(LocalMaterialsClient(service))
    try:
        platform = VirtualHeatingPlatform("heater-history", {"update_interval_s": 0.05})
        platform.initialize()
        for index in range(3):
            platform._write_material_temperature(
                1,
                temperature_c=25.0 + index,
                target_temperature_c=150.0,
                state="heating",
                progress=float(index),
                job_uuid="history-job",
            )
        material = service.get_material_by_resource_id("heater-history-sample-1")
        assert material.data.data["temperature_c"] == 27.0
        assert "temperature_history" not in material.data.data
        assert material.data.data["temperature_observed_at_ms"] > 0
        assert platform.site_1_temperature_c == 27.0
    finally:
        set_materials_gateway(None)
        service.repository.close()
