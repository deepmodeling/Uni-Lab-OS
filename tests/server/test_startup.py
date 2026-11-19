from __future__ import annotations

from types import SimpleNamespace

import pytest

from unilabos.config.config import BasicConfig, HTTPConfig
from unilabos.client.materials import LocalMaterialsClient
from unilabos.server.execution_composition import (
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
    tmp_path, monkeypatch
) -> None:
    """显式配置云端地址后调度权威在远端 Backend，本机不挂本地调度器。"""

    authority = MaterialsService(tmp_path / "authority-materials.db")
    monkeypatch.setattr(
        "unilabos.client.materials.HTTPMaterialsClient",
        lambda _address: LocalMaterialsClient(authority),
    )
    HTTPConfig.remote_addr = "https://backend.example/api/v1"
    try:
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
    finally:
        authority.close()


def test_host_with_backend_address_uses_it_as_materials_authority(tmp_path, monkeypatch) -> None:
    """``--address`` 不带物料地址的 Host 与默认拓扑的 Host 子进程一样，把物料交给调度权威，
    不在本机另起一个物料库（否则权威的设备目录里看不到这个 Host 的设备）。"""

    authority = MaterialsService(tmp_path / "authority-materials.db")
    addresses: list[str] = []

    def http_client(address):
        addresses.append(address)
        return LocalMaterialsClient(authority)

    monkeypatch.setattr("unilabos.client.materials.HTTPMaterialsClient", http_client)
    HTTPConfig.remote_addr = "http://127.0.0.1:18210"
    try:
        stack = setup_host_server_stack(
            args={"backend": "hostlink", "server_database_root": str(tmp_path / "edge")},
            working_dir=tmp_path,
            registry=_Registry(),
            communication_client=_communication_client(),
        )

        assert addresses == ["http://127.0.0.1:18210"]
        assert stack.material_authority == "http://127.0.0.1:18210"
        assert get_materials_service() is None
        assert len(LocalMaterialsClient(authority).list_templates()) == 1
    finally:
        authority.close()


@pytest.mark.parametrize(
    ("explicit", "protocol", "expected"),
    [
        ("http://materials:8092/api/v1", "", "http://materials:8092/api/v1"),
        (None, "legacy", None),
    ],
)
def test_explicit_materials_address_or_legacy_backend_keep_their_authority(
    tmp_path, monkeypatch, explicit, protocol, expected
) -> None:
    """显式 --material_microbackend_addr 优先；显式声明为旧云端协议（没有 materials.v1）时仍用本机物料库。"""

    external = MaterialsService(tmp_path / "external-materials.db")
    addresses: list[str] = []

    def http_client(address):
        addresses.append(address)
        return LocalMaterialsClient(external)

    monkeypatch.setattr("unilabos.client.materials.HTTPMaterialsClient", http_client)
    monkeypatch.setattr(HTTPConfig, "backend_protocol", protocol)
    HTTPConfig.remote_addr = "https://backend.example/api/v1"
    try:
        stack = setup_host_server_stack(
            args={
                "backend": "hostlink",
                "server_database_root": str(tmp_path / "edge"),
                "material_microbackend_addr": explicit,
            },
            working_dir=tmp_path,
            registry=_Registry(),
            communication_client=_communication_client(),
        )
        if expected is None:
            assert addresses == []
            assert isinstance(stack.materials_gateway, LocalMaterialsClient)
            assert stack.material_authority == str(stack.database_paths.materials_db)
        else:
            assert addresses == [expected]
            assert stack.material_authority == expected
    finally:
        external.close()


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
