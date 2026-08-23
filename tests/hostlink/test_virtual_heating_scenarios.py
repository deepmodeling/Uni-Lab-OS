"""三类加热场景在无 ROS HostLink 运行时中的真实 materials.v1 闭环。"""

from __future__ import annotations

import time
from uuid import uuid4

import pytest

from unilabos.client.materials import LocalMaterialsClient
from unilabos.config.config import BasicConfig
from unilabos.devices.virtual.heating_platform import VirtualHeatingPlatform
from unilabos.devices.virtual.workbench import VirtualWorkbench
from unilabos.hostlink.backend import HostLinkBackend
from unilabos.hostlink.execution_adapter import HostLinkExecutionAdapter
from unilabos.hostlink.local_runtime import HostLinkDriverSpec, HostLinkLocalRuntime
from unilabos.server.database.repositories.materials import MaterialsRepository
from unilabos.server.database.repositories.telemetry import TelemetryRepository
from unilabos.server.protocol.telemetry import TelemetryEventQuery
from unilabos.server.scheduler.backend import JobExecutionBackend
from unilabos.server.scheduler.integration import set_materials_gateway
from unilabos.server.scheduler.telemetry_state import TelemetryDeviceStateProjection
from unilabos.server.scheduler.workflow_execution import WorkflowTaskExecutor
from unilabos.server.services.materials import MaterialsService
from unilabos.server.services.telemetry import TelemetryService
from unilabos.server.workflow.service import WorkflowNodeWrite, WorkflowService
from unilabos.server.workflow.store import WorkflowStore


def _occupied_site_ids(service: MaterialsService, device_id: str) -> dict[int, str]:
    platform = service.get_material_by_resource_id(device_id)
    return {
        int(site.site_index): str(site.occupied_material_uuid or "")
        for site in platform.sites
    }


def test_all_heating_scenarios_reset_and_reach_expected_material_state(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(BasicConfig, "is_host_mode", True)
    service = MaterialsService(MaterialsRepository(tmp_path / "materials.db"))
    set_materials_gateway(LocalMaterialsClient(service))
    runtime = HostLinkLocalRuntime()
    runtime.add_driver(
        HostLinkDriverSpec(
            "virtual-workbench",
            VirtualWorkbench,
            {
                "arm_operation_time": 0,
                "heating_time": 0.01,
                "num_heating_stations": 3,
            },
            action_names=("call_peer",),
        )
    )
    heater = runtime.add_driver(
        HostLinkDriverSpec(
            "virtual-heater",
            VirtualHeatingPlatform,
            {"update_interval_s": 0.05},
            registry_name="virtual_heating_platform",
            action_names=(
                "heat_site",
                "reset_scenario",
                "transfer_site",
                "run_scenario",
            ),
            status_names=(
                "status",
                "site_1_temperature_c",
                "site_2_temperature_c",
                "site_3_temperature_c",
                "serialized_state",
            ),
        )
    )
    try:
        runtime.start()

        sequential = runtime.call_action(
            "virtual-heater",
            "run_scenario",
            scenario_id="single_sequential",
            target_temperature_c=70.0,
            duration_seconds=0.05,
        )
        assert sequential["success"] is True
        assert len(sequential["steps"]) == 3
        occupied = _occupied_site_ids(service, "virtual-heater")
        assert occupied[1]
        assert not occupied[2]
        assert not occupied[3]
        sample_1 = service.get_material_by_resource_id("virtual-heater-sample-1")
        assert sample_1.data.data["temperature_c"] == 70.0

        parallel = runtime.call_action(
            "virtual-heater",
            "run_scenario",
            scenario_id="parallel_three_site",
            target_temperature_c=80.0,
            duration_seconds=0.05,
        )
        assert parallel["success"] is True
        assert len(parallel["steps"]) == 4
        occupied = _occupied_site_ids(service, "virtual-heater")
        assert all(occupied.values())
        assert [
            service.get_material_by_resource_id(
                f"virtual-heater-sample-{index}"
            ).data.data["temperature_c"]
            for index in range(1, 4)
        ] == [70.0, 80.0, 90.0]

        transferred = runtime.call_action(
            "virtual-heater",
            "run_scenario",
            scenario_id="cross_device_transfer",
            target_temperature_c=90.0,
            duration_seconds=0.05,
        )
        assert transferred["success"] is True
        assert len(transferred["steps"]) == 4
        assert transferred["steps"][1]["target_device"] == "virtual-heater"
        assert transferred["steps"][1]["return_value"]["temperature_c"] == 57.5
        occupied = _occupied_site_ids(service, "virtual-heater")
        assert not occupied[1]
        assert not occupied[2]
        assert occupied[3] == sample_1.material.material_uuid
        final_sample = service.get_material(occupied[3])
        assert final_sample.data.data["temperature_c"] == 90.0
        assert final_sample.data.data["temperature_source"] == {
            "device_id": "virtual-heater",
            "property": "site_3_temperature_c",
        }
        assert heater.driver.serialize()["sites"][2]["material_uuid"] == occupied[3]
    finally:
        runtime.stop()
        set_materials_gateway(None)
        service.repository.close()


def test_workflow_authority_runs_scenarios_to_terminal_with_telemetry_v1(
    tmp_path,
    monkeypatch,
) -> None:
    """同一份场景参数贯穿 Authority、HostLink、materials 与 telemetry。"""

    monkeypatch.setattr(BasicConfig, "is_host_mode", True)
    materials = MaterialsService(MaterialsRepository(tmp_path / "materials.db"))
    materials_client = LocalMaterialsClient(materials)
    set_materials_gateway(materials_client)
    telemetry = TelemetryService(TelemetryRepository(tmp_path / "telemetry.db"))
    projection = TelemetryDeviceStateProjection(
        telemetry,
        endpoint_uuid="hostlink:scenario-test",
    )

    local = HostLinkLocalRuntime()
    local.add_driver(
        HostLinkDriverSpec(
            "virtual-workbench",
            VirtualWorkbench,
            {
                "arm_operation_time": 0,
                "heating_time": 0.01,
                "num_heating_stations": 3,
            },
            action_names=("call_peer",),
        )
    )
    local.add_driver(
        HostLinkDriverSpec(
            "virtual-heater",
            VirtualHeatingPlatform,
            {"update_interval_s": 0.05},
            registry_name="virtual_heating_platform",
            action_names=(
                "heat_site",
                "reset_scenario",
                "transfer_site",
                "run_scenario",
            ),
            status_names=(
                "status",
                "site_1_temperature_c",
                "site_2_temperature_c",
                "site_3_temperature_c",
                "serialized_state",
            ),
        )
    )
    runtime = HostLinkBackend(local, is_slave=False)
    backend = JobExecutionBackend(device_state_store=projection)
    adapter = HostLinkExecutionAdapter(
        runtime,
        devices_config=object(),
        resources_config=object(),
        bridges=[backend],
    )
    backend._host_node_getter = lambda: adapter
    authority = WorkflowService(WorkflowStore(":memory:"))
    executor = WorkflowTaskExecutor(
        authority,
        backend,
        materials_gateway=materials_client,
    )
    authority.set_task_submitter(executor.submit)

    try:
        runtime.start()
        backend.start()
        adapter.start()
        executor.start(recover=True)

        terminal: dict[str, tuple[dict, dict]] = {}
        for scenario_id, target in (
            ("single_sequential", 70.0),
            ("parallel_three_site", 80.0),
            ("cross_device_transfer", 90.0),
        ):
            workflow = authority.create_workflow(
                name=f"heating:{scenario_id}",
                tags=["demo", scenario_id],
                description="HostLink 三场景自动验收",
                meta_data={"scenario_id": scenario_id},
            )
            node_uuid = str(uuid4())
            authority.save_graph(
                workflow["uuid"],
                revision=workflow["revision"],
                nodes=[
                    WorkflowNodeWrite(
                        uuid=node_uuid,
                        name=scenario_id,
                        type="device_action",
                        material_uuid=str(uuid4()),
                        action_name="run_scenario",
                        action_type="UniLabJsonCommand",
                        param={
                            "scenario_id": scenario_id,
                            "target_temperature_c": target,
                            "duration_seconds": 0.05,
                        },
                        meta_data={"target_device_id": "virtual-heater"},
                    )
                ],
                edges=[],
            )
            task = authority.create_workflow_task(
                workflow_uuid=workflow["uuid"],
                run_mode="normal",
                target_node_uuid=None,
                input_value={},
                description=f"scenario={scenario_id}",
                meta_data={"scenario_id": scenario_id},
            )
            deadline = time.monotonic() + 5
            current = authority.get_workflow_task(task["uuid"])
            while current["status"] not in {"succeeded", "failed"}:
                if time.monotonic() >= deadline:
                    pytest.fail(f"scenario {scenario_id} did not reach terminal state")
                time.sleep(0.02)
                current = authority.get_workflow_task(task["uuid"])
            jobs = authority.list_workflow_node_jobs(task["uuid"])
            assert current["status"] == "succeeded"
            assert len(jobs) == 1
            assert jobs[0]["status"] == "succeeded"
            assert current["output"][node_uuid]["return_value"]["scenario_id"] == (
                scenario_id
            )
            terminal[scenario_id] = (current, jobs[0])

        assert set(terminal) == {
            "single_sequential",
            "parallel_three_site",
            "cross_device_transfer",
        }
        occupied = _occupied_site_ids(materials, "virtual-heater")
        assert not occupied[1]
        assert not occupied[2]
        assert occupied[3]
        sample = materials.get_material(occupied[3])
        assert sample.data.data["temperature_c"] == 90.0
        assert (
            sample.data.source_job_uuid == terminal["cross_device_transfer"][1]["uuid"]
        )

        deadline = time.monotonic() + 3
        current_state = telemetry.get_device_state(
            "hostlink:scenario-test",
            "virtual-heater",
        )
        while (
            current_state is None
            or current_state.properties.get("site_3_temperature_c") != 90.0
        ):
            if time.monotonic() >= deadline:
                pytest.fail("telemetry.v1 did not publish the final site temperature")
            time.sleep(0.05)
            current_state = telemetry.get_device_state(
                "hostlink:scenario-test",
                "virtual-heater",
            )
        events = telemetry.query_events(
            TelemetryEventQuery(
                endpoint_uuid="hostlink:scenario-test",
                device_uuid="virtual-heater",
                event_type="property_sample",
                event_key="site_3_temperature_c",
                order="desc",
                limit=50,
            )
        )
        assert events
        assert events[0].payload == {"value": 90.0}
    finally:
        authority.set_task_submitter(None)
        executor.stop()
        backend.stop()
        adapter.stop()
        runtime.stop()
        set_materials_gateway(None)
        telemetry.repository.close()
        materials.repository.close()
