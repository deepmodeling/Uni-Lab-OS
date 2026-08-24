from __future__ import annotations

from uuid import uuid4

from unilabos.client.materials import LocalMaterialsClient
from unilabos.devices.virtual.heating_platform import VirtualHeatingPlatform
from unilabos.registry.decorators import has_action_decorator
from unilabos.server.database.repositories.materials import MaterialsRepository
from unilabos.server.demo.heating_scenarios import build_heating_scenario_graph
from unilabos.server.protocol.materials import MaterialTransfer, MaterialTransferItem
from unilabos.server.scheduler.integration import set_materials_gateway
from unilabos.server.services.heating_demo import HeatingDemoProvisionService
from unilabos.server.services.materials import MaterialsService


def test_heating_driver_only_registers_atomic_production_action() -> None:
    assert has_action_decorator(VirtualHeatingPlatform.heat_site)
    assert not hasattr(VirtualHeatingPlatform, "run_scenario")
    assert not hasattr(VirtualHeatingPlatform, "reset_scenario")
    assert not hasattr(VirtualHeatingPlatform, "transfer_site")


def test_demo_provision_and_canonical_graphs_use_real_jobs(tmp_path) -> None:
    materials = MaterialsService(MaterialsRepository(tmp_path / "materials.db"))
    gateway = LocalMaterialsClient(materials)
    set_materials_gateway(gateway)
    sync_calls = []
    materials.set_resource_sync_dispatcher(
        lambda command: sync_calls.append(command) or {"success": True}
    )
    source = VirtualHeatingPlatform(
        device_id="virtual-heater",
        config={"update_interval_s": 0.05},
    )
    target = VirtualHeatingPlatform(
        device_id="virtual-heater-target",
        config={"update_interval_s": 0.05},
    )
    try:
        source.initialize()
        target.initialize()
        provision = HeatingDemoProvisionService(materials)
        environments = {
            scenario_id: provision.reset(
                scenario_id,
                request_uuid=str(uuid4()),
                source_device_id="virtual-heater",
                target_device_id="virtual-heater-target",
            )
            for scenario_id in (
                "single_sequential",
                "parallel_three_site",
                "cross_device_transfer",
            )
        }
        graphs = {
            scenario_id: build_heating_scenario_graph(
                scenario_id,
                revision=1,
                environment=environment,
                target_temperature_c=80,
                duration_seconds=0.1,
            )
            for scenario_id, environment in environments.items()
        }

        sequential = graphs["single_sequential"]
        assert len(sequential["nodes"]) == 2
        assert len(sequential["edges"]) == 1
        assert sequential["edges"][0]["source_node_uuid"] == sequential["nodes"][0][
            "uuid"
        ]
        assert sequential["edges"][0]["target_node_uuid"] == sequential["nodes"][1][
            "uuid"
        ]

        parallel = graphs["parallel_three_site"]
        assert len(parallel["nodes"]) == 3
        assert parallel["edges"] == []
        assert all(
            node["execution_policy"]["always_free"] is True
            for node in parallel["nodes"]
        )

        transfer = graphs["cross_device_transfer"]
        assert len(transfer["nodes"]) == 3
        assert len(transfer["edges"]) == 2
        assert [node["action_name"] for node in transfer["nodes"]] == [
            "heat_site",
            "materials.transfer",
            "heat_site",
        ]
        environment = environments["cross_device_transfer"]
        assert transfer["nodes"][1]["param"] == {
            "source_device_id": "virtual-heater",
            "target_device_id": "virtual-heater-target",
            "items": [
                {
                    "material_uuid": environment.transfer_material_uuid,
                    "target_material_uuid": environment.target_platform_uuid,
                    "target_site": environment.transfer_target_site_uuid,
                }
            ],
        }
        assert [call.action for call in sync_calls] == [
            "load",
            "load",
            "load",
            "load",
        ]
    finally:
        set_materials_gateway(None)
        materials.repository.close()


def test_only_explicit_demo_reset_restores_layout_after_transfer(tmp_path) -> None:
    materials = MaterialsService(MaterialsRepository(tmp_path / "materials.db"))
    set_materials_gateway(LocalMaterialsClient(materials))
    materials.set_resource_sync_dispatcher(lambda command: {"success": True})
    source = VirtualHeatingPlatform(device_id="virtual-heater")
    target = VirtualHeatingPlatform(device_id="virtual-heater-target")
    try:
        source.initialize()
        target.initialize()
        provision = HeatingDemoProvisionService(materials)
        before = provision.reset(
            "cross_device_transfer",
            request_uuid=str(uuid4()),
            source_device_id="virtual-heater",
            target_device_id="virtual-heater-target",
        )
        transfer_material_uuid = str(before.transfer_material_uuid)
        materials.transfer_material(
            VirtualHeatingPlatform._mutation(
                "transfer_material", f"explicit-transfer:{uuid4()}"
            ),
            MaterialTransfer(
                source_device_id="virtual-heater",
                target_device_id="virtual-heater-target",
                items=[
                    MaterialTransferItem(
                        material_uuid=transfer_material_uuid,
                        target_material_uuid=str(before.target_platform_uuid),
                        target_site=str(before.transfer_target_site_uuid),
                    )
                ],
            ),
        )
        transferred = materials.get_material(transfer_material_uuid)
        assert transferred.material.parent_material_uuid == (
            before.target_platform_uuid
        )

        after = provision.reset(
            "cross_device_transfer",
            request_uuid=str(uuid4()),
            source_device_id="virtual-heater",
            target_device_id="virtual-heater-target",
        )

        source_platform = materials.get_material_by_resource_id("virtual-heater")
        target_platform = materials.get_material_by_resource_id(
            "virtual-heater-target"
        )
        source_site_1 = next(
            site for site in source_platform.sites if int(site.site_index) == 1
        )
        target_site_3 = next(
            site for site in target_platform.sites if int(site.site_index) == 3
        )
        assert after.assignments[transfer_material_uuid] == source_site_1.site_uuid
        assert source_site_1.occupied_material_uuid == transfer_material_uuid
        assert target_site_3.occupied_material_uuid is None
        assert materials.get_material(
            transfer_material_uuid
        ).material.parent_material_uuid == source_platform.material.material_uuid
    finally:
        set_materials_gateway(None)
        materials.repository.close()
