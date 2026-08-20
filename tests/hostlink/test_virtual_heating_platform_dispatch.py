from __future__ import annotations

import threading
import time
import uuid

from unilabos.basic.runtime import BasicDriverSpec, BasicRuntime
from unilabos.client.materials import LocalMaterialsClient
from unilabos.config.config import BasicConfig
from unilabos.devices.virtual.heating_platform import VirtualHeatingPlatform
from unilabos.hostlink.backend import HostLinkBackendRuntime
from unilabos.hostlink.execution_adapter import HostLinkExecutionAdapter
from unilabos.legacy_support.websocket import QueueItem
from unilabos.server.scheduler.backend import JobExecutionBackend
from unilabos.server.scheduler.device_state import DeviceStateStore
from unilabos.server.scheduler.integration import set_materials_gateway
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


def test_test_mode_executes_only_explicit_virtual_simulator_and_records_status_history(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(BasicConfig, "test_mode", True)
    monkeypatch.setattr(BasicConfig, "is_host_mode", True)
    material_service = MaterialsService(tmp_path / "materials.db")
    set_materials_gateway(LocalMaterialsClient(material_service))
    device_state = DeviceStateStore(str(tmp_path / "device-state.db"))
    local = BasicRuntime("hostlink")
    physical_node = local.add_driver(
        BasicDriverSpec(
            device_id="physical-heater",
            driver_class=PhysicalHeater,
            config={},
            action_names=("heat",),
        )
    )
    local.add_driver(
        BasicDriverSpec(
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
    runtime = HostLinkBackendRuntime(local, is_slave=False)
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
        material_service.close()
