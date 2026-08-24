from __future__ import annotations

import threading
import time
import uuid

from unilabos.hostlink.local_runtime import HostLinkDriverSpec, HostLinkLocalRuntime
from unilabos.client.materials import LocalMaterialsClient
from unilabos.config.config import BasicConfig
from unilabos.devices.virtual.heating_platform import VirtualHeatingPlatform
from unilabos.hostlink.backend import HostLinkBackend
from unilabos.hostlink.execution_adapter import HostLinkExecutionAdapter
from unilabos.server.protocol.materials import (
    MaterialIdentityWrite,
    MaterialMove,
    MaterialNodeCreate,
    MaterialTransfer,
    MaterialTransferItem,
    MaterialTreeCreate,
)
from unilabos.server.scheduler.execution_queue import QueueItem
from unilabos.server.database.repositories.materials import MaterialsRepository
from unilabos.server.scheduler.backend import JobExecutionBackend
from unilabos.server.scheduler.device_state import DeviceStateStore
from unilabos.server.scheduler.integration import set_materials_gateway
from unilabos.server.services.heating_demo import HeatingDemoProvisionService
from unilabos.server.services.materials import MaterialsService


class PhysicalHeater:
    def __init__(self) -> None:
        self.calls = 0

    def heat(self, target_temperature_c: float) -> dict[str, float]:
        self.calls += 1
        return {"temperature_c": target_temperature_c}


class RecordingBridge:
    def __init__(self) -> None:
        self.statuses: list[tuple[str, str, dict]] = []
        self.finished = threading.Event()

    def publish_job_status(
        self,
        data: dict,
        item: QueueItem,
        status: str,
        return_info: dict | None = None,
    ) -> None:
        del return_info
        self.statuses.append((item.job_id, status, data))
        if status in {"success", "failed", "canceled"}:
            self.finished.set()


def _item(device_id: str, action_name: str) -> QueueItem:
    return QueueItem(
        task_type="job_call_back_status",
        device_id=device_id,
        action_name=action_name,
        task_id=str(uuid.uuid4()),
        job_id=str(uuid.uuid4()),
        notebook_id="heating-demo",
        device_action_key=f"/devices/{device_id}/{action_name}",
        node_id=str(uuid.uuid4()),
    )


def test_restart_preserves_authoritative_cross_device_transfer(tmp_path) -> None:
    service = MaterialsService(MaterialsRepository(tmp_path / "materials.db"))
    set_materials_gateway(LocalMaterialsClient(service))
    service.set_resource_sync_dispatcher(lambda command: {"success": True})
    source = VirtualHeatingPlatform(device_id="virtual-heater")
    target = VirtualHeatingPlatform(device_id="virtual-heater-target")
    try:
        assert source.initialize() is True
        assert target.initialize() is True

        environment = HeatingDemoProvisionService(service).reset(
            "cross_device_transfer",
            request_uuid=str(uuid.uuid4()),
            source_device_id="virtual-heater",
            target_device_id="virtual-heater-target",
        )
        source_sample = service.get_material_by_resource_id(
            "virtual-heater-sample-1"
        )
        service.transfer_material(
            VirtualHeatingPlatform._mutation(
                "transfer_material", f"test-cross-device-transfer:{uuid.uuid4()}"
            ),
            MaterialTransfer(
                source_device_id="virtual-heater",
                target_device_id="virtual-heater-target",
                items=[
                    MaterialTransferItem(
                        material_uuid=source_sample.material.material_uuid,
                        target_material_uuid=environment.target_platform_uuid,
                        target_site=environment.transfer_target_site_uuid,
                    )
                ],
            ),
        )

        restarted_source = VirtualHeatingPlatform(device_id="virtual-heater")
        restarted_target = VirtualHeatingPlatform(
            device_id="virtual-heater-target"
        )
        assert restarted_source.initialize() is True
        assert restarted_target.initialize() is True

        refreshed_source = service.get_material_by_resource_id("virtual-heater")
        refreshed_target = service.get_material_by_resource_id(
            "virtual-heater-target"
        )
        source_site_1 = next(
            site for site in refreshed_source.sites if int(site.site_index) == 1
        )
        target_site_3 = next(
            site for site in refreshed_target.sites if int(site.site_index) == 3
        )
        assert source_site_1.occupied_material_uuid is None
        assert target_site_3.occupied_material_uuid == (
            source_sample.material.material_uuid
        )
        assert (
            service.get_material_by_resource_id(
                "virtual-heater-sample-1"
            ).material.parent_material_uuid
            == refreshed_target.material.material_uuid
        )
        assert (
            service.get_material_by_resource_id(
                "virtual-heater-target-sample-3"
            ).material.parent_material_uuid
            is None
        )
        serialized_site_3 = restarted_target.serialize()["sites"][2]
        assert serialized_site_3["material_uuid"] == (
            source_sample.material.material_uuid
        )
        assert serialized_site_3["material_name"] == source_sample.material.name
    finally:
        set_materials_gateway(None)
        service.repository.close()


def test_restart_preserves_non_demo_occupant(tmp_path) -> None:
    service = MaterialsService(MaterialsRepository(tmp_path / "materials.db"))
    set_materials_gateway(LocalMaterialsClient(service))
    target = VirtualHeatingPlatform(device_id="virtual-heater-target")
    try:
        assert target.initialize() is True
        target_sample = service.get_material_by_resource_id(
            "virtual-heater-target-sample-3"
        )
        target_platform = service.get_material_by_resource_id(
            "virtual-heater-target"
        )
        target_site = next(
            site for site in target_platform.sites if int(site.site_index) == 3
        )
        service.move_material(
            VirtualHeatingPlatform._mutation(
                "move_material", f"test-unmount-target:{uuid.uuid4()}"
            ),
            MaterialMove(material_uuid=target_sample.material.material_uuid),
        )
        created = service.create_tree(
            VirtualHeatingPlatform._mutation(
                "create_material_tree", f"test-create-real-sample:{uuid.uuid4()}"
            ),
            MaterialTreeCreate(
                nodes=[
                    MaterialNodeCreate(
                        client_ref="real-sample",
                        identity=MaterialIdentityWrite(
                            resource_id="real-heating-sample",
                            name="真实装载样品",
                            resource_type="Resource",
                            class_name="Resource",
                            template_name="virtual_heating_sample",
                            meta_data={},
                        ),
                    )
                ]
            ),
        )
        real_sample_uuid = created.data.root_material_uuid
        service.move_material(
            VirtualHeatingPlatform._mutation(
                "move_material", f"test-load-real-sample:{uuid.uuid4()}"
            ),
            MaterialMove(
                material_uuid=real_sample_uuid,
                destination_site_uuid=target_site.site_uuid,
            ),
        )

        restarted_target = VirtualHeatingPlatform(
            device_id="virtual-heater-target"
        )
        assert restarted_target.initialize() is True

        refreshed_target = service.get_material_by_resource_id(
            "virtual-heater-target"
        )
        refreshed_site = next(
            site for site in refreshed_target.sites if int(site.site_index) == 3
        )
        assert refreshed_site.occupied_material_uuid == real_sample_uuid
        assert service.get_material(real_sample_uuid).material.parent_material_uuid == (
            refreshed_target.material.material_uuid
        )
        assert target_sample.material.material_uuid != real_sample_uuid
        assert restarted_target.serialize()["sites"][2][
            "material_uuid"
        ] == real_sample_uuid
    finally:
        set_materials_gateway(None)
        service.repository.close()


def test_test_mode_executes_only_explicit_virtual_simulator_and_records_status_history(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(BasicConfig, "test_mode", True)
    monkeypatch.setattr(BasicConfig, "is_host_mode", True)
    material_service = MaterialsService(MaterialsRepository(tmp_path / "materials.db"))
    set_materials_gateway(LocalMaterialsClient(material_service))
    device_state = DeviceStateStore(str(tmp_path / "device-state.db"))
    local = HostLinkLocalRuntime()
    physical_node = local.add_driver(
        HostLinkDriverSpec(
            device_id="physical-heater",
            driver_class=PhysicalHeater,
            config={},
            action_names=("heat",),
        )
    )
    local.add_driver(
        HostLinkDriverSpec(
            device_id="virtual-heater",
            driver_class=VirtualHeatingPlatform,
            config={"update_interval_s": 0.05},
            registry_name="virtual_heating_platform",
            action_names=("heat_site",),
            status_names=(
                "status",
                "site_1_temperature_c",
                "site_2_temperature_c",
                "site_3_temperature_c",
                "site_1_state",
                "site_2_state",
                "site_3_state",
                "serialized_state",
            ),
        )
    )
    runtime = HostLinkBackend(local, is_slave=False)
    recording = RecordingBridge()
    backend = JobExecutionBackend(
        device_state_store=device_state,
        result_bridges=[recording],
    )
    adapter = HostLinkExecutionAdapter(
        runtime,
        devices_config=object(),
        resources_config=object(),
        bridges=[backend],
    )
    backend._host_node_getter = lambda: adapter
    try:
        runtime.start()
        adapter.start()
        backend.start()

        physical = _item("physical-heater", "heat")
        backend.dispatch(
            {
                "job_id": physical.job_id,
                "task_id": physical.task_id,
                "node_id": physical.node_id,
                "device_id": physical.device_id,
                "action": physical.action_name,
                "action_type": "NativeAction",
                "action_args": {"target_temperature_c": 90.0},
            }
        )
        assert recording.finished.wait(2)
        assert physical_node.driver.calls == 0
        physical_result = next(
            item for item in recording.statuses if item[0] == physical.job_id
        )
        assert physical_result[1] == "success"
        assert physical_result[2]["return_value"]["test_mode"] is True

        recording.finished.clear()
        virtual = _item("virtual-heater", "heat_site")
        backend.dispatch(
            {
                "job_id": virtual.job_id,
                "task_id": virtual.task_id,
                "node_id": virtual.node_id,
                "device_id": virtual.device_id,
                "action": virtual.action_name,
                "action_type": "UniLabJsonCommand",
                "action_args": {
                    "site_id": 1,
                    "target_temperature_c": 81.0,
                    "duration_seconds": 0.8,
                },
            }
        )
        assert recording.finished.wait(4)
        virtual_result = next(
            item
            for item in reversed(recording.statuses)
            if item[0] == virtual.job_id and item[1] != "running"
        )
        assert virtual_result[1] == "success"
        assert virtual_result[2]["return_value"]["temperature_c"] == 81.0

        deadline = time.time() + 2
        history = []
        while time.time() < deadline:
            history = device_state.history(
                "virtual-heater", "site_1_temperature_c", limit=200
            )
            if len(history) >= 2 and history[0]["value"] == 81.0:
                break
            time.sleep(0.05)
        assert len(history) >= 2
        assert history[0]["value"] == 81.0

        material = material_service.get_material_by_resource_id(
            "virtual-heater-sample-1"
        )
        assert material.data.data["temperature_c"] == 81.0
        assert "temperature_history" not in material.data.data
        assert material.data.data["temperature_source"] == {
            "device_id": "virtual-heater",
            "property": "site_1_temperature_c",
        }
        assert material.data.source_job_uuid == virtual.job_id
    finally:
        backend.stop()
        adapter.stop()
        runtime.stop()
        device_state.close()
        set_materials_gateway(None)
        material_service.repository.close()
