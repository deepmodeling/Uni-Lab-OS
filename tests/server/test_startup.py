from __future__ import annotations

from types import SimpleNamespace

import pytest

from unilabos.config.config import BasicConfig, HTTPConfig
from unilabos.client.materials import LocalMaterialsClient
from unilabos.server.backend.composition import (
    get_materials_gateway,
    get_materials_service,
    get_scheduler,
    get_workflow_service,
    reset_for_test,
)
from unilabos.server.services.materials import MaterialsService
from unilabos.server.startup import setup_host_server_stack


class _Registry:
    @staticmethod
    def obtain_registry_device_info():
        return []

    @staticmethod
    def obtain_registry_resource_info():
        return [
            {
                "id": "startup-container",
                "display_name": "Startup Container",
                "class": {
                    "module": "pylabrobot.resources",
                    "type": "RegularContainer",
                },
                "config_info": [{"id": "root", "type": "container"}],
                "handles": [],
            }
        ]


@pytest.fixture(autouse=True)
def _reset_server_stack():
    previous = (
        BasicConfig.backend,
        BasicConfig.machine_name,
        BasicConfig.server_database_paths,
        HTTPConfig.material_microbackend_addr,
        HTTPConfig.remote_addr,
    )
    reset_for_test()
    BasicConfig.backend = "hostlink"
    BasicConfig.machine_name = "test-host"
    HTTPConfig.material_microbackend_addr = ""
    HTTPConfig.remote_addr = ""
    yield
    reset_for_test()
    (
        BasicConfig.backend,
        BasicConfig.machine_name,
        BasicConfig.server_database_paths,
        HTTPConfig.material_microbackend_addr,
        HTTPConfig.remote_addr,
    ) = previous


def _communication_client():
    return SimpleNamespace(message_processor=None, publish_runtime_events=None)


def test_host_stack_uses_embedded_materials_when_address_is_empty(tmp_path) -> None:
    stack = setup_host_server_stack(
        args={
            "backend": "hostlink",
            "server_database_root": str(tmp_path / "edge"),
            "material_microbackend_addr": "",
        },
        working_dir=tmp_path,
        registry=_Registry(),
        communication_client=_communication_client(),
    )

    assert isinstance(stack.materials_gateway, LocalMaterialsClient)
    assert stack.material_authority == str(stack.database_paths.materials_db)
    assert stack.template_count == 1
    assert get_materials_gateway() is stack.materials_gateway
    assert get_materials_service() is not None


def test_host_stack_uses_external_materials_as_the_only_authority(
    tmp_path, monkeypatch
) -> None:
    external_service = MaterialsService(tmp_path / "external-materials.db")
    external_client = LocalMaterialsClient(external_service)
    monkeypatch.setattr(
        "unilabos.client.materials.HTTPMaterialsClient",
        lambda _address: external_client,
    )
    try:
        stack = setup_host_server_stack(
            args={
                "backend": "hostlink",
                "server_database_root": str(tmp_path / "edge"),
                "material_microbackend_addr": "http://materials:8092/api/v1",
            },
            working_dir=tmp_path,
            registry=_Registry(),
            communication_client=_communication_client(),
        )

        assert stack.materials_gateway is external_client
        assert stack.material_authority == "http://materials:8092/api/v1"
        assert stack.template_count == 1
        assert get_materials_gateway() is external_client
        assert get_materials_service() is None
        assert len(external_client.list_templates()) == 1
    finally:
        external_service.close()


def test_host_stack_mounts_local_scheduler_by_default(tmp_path) -> None:
    """未配置云端地址时，本机是默认调度权威（本地 Scheduler + Workflow API）。"""

    setup_host_server_stack(
        args={
            "backend": "hostlink",
            "server_database_root": str(tmp_path / "edge"),
            "material_microbackend_addr": "",
        },
        working_dir=tmp_path,
        registry=_Registry(),
        communication_client=_communication_client(),
    )

    assert get_scheduler() is not None
    assert get_workflow_service() is not None


def test_host_stack_defers_scheduling_when_backend_address_configured(
    tmp_path,
) -> None:
    """显式配置云端地址后调度权威在远端 Backend，本机不挂本地调度器。"""

    HTTPConfig.remote_addr = "https://backend.example/api/v1"
    setup_host_server_stack(
        args={
            "backend": "hostlink",
            "server_database_root": str(tmp_path / "edge"),
            "material_microbackend_addr": "",
        },
        working_dir=tmp_path,
        registry=_Registry(),
        communication_client=_communication_client(),
    )

    assert get_scheduler() is None
    assert get_workflow_service() is None


def test_host_stack_closes_partial_services_when_template_sync_fails(tmp_path) -> None:
    bad_registry = SimpleNamespace(
        obtain_registry_device_info=lambda: [],
        obtain_registry_resource_info=lambda: [{"id": ""}],
    )

    with pytest.raises(ValueError, match="template id is required"):
        setup_host_server_stack(
            args={
                "backend": "hostlink",
                "server_database_root": str(tmp_path / "edge"),
                "material_microbackend_addr": "",
            },
            working_dir=tmp_path,
            registry=bad_registry,
            communication_client=_communication_client(),
        )

    assert get_materials_gateway() is None
    assert get_materials_service() is None
