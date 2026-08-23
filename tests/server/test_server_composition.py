"""四库组合根只创建新数据库并保持单 writer。"""

from __future__ import annotations

import pytest

from unilabos.server.composition import (
    configure_server_services,
    get_server_services,
    shutdown_server_services,
)
from unilabos.server.database import ServerDatabasePaths
from unilabos.server.database.repositories import (
    HistoryRepository,
    MaterialsRepository,
    RuntimeRepository,
    TelemetryRepository,
)
from unilabos.config.config import BasicConfig
from unilabos.server.scheduler.integration import (
    get_edge_backend,
    get_materials_service,
    reset_for_test,
    setup_job_execution_backend,
    setup_materials_service,
)
from unilabos.server.services import (
    HistoryService,
    MaterialsService,
    RuntimeService,
    TelemetryService,
)


def test_server_services_open_exactly_four_new_databases(tmp_path) -> None:
    paths = ServerDatabasePaths.resolve(tmp_path)
    try:
        services = configure_server_services(paths)

        assert get_server_services() is services
        assert services.runtime.repository.connection is not (
            services.materials.repository.connection
        )
        assert services.materials.repository.connection is not (
            services.telemetry.repository.connection
        )
        assert services.telemetry.repository.connection is not (
            services.history.repository.connection
        )
        assert isinstance(services.runtime.repository, RuntimeRepository)
        assert isinstance(services.materials.repository, MaterialsRepository)
        assert isinstance(services.telemetry.repository, TelemetryRepository)
        assert isinstance(services.history.repository, HistoryRepository)
        assert {path.name for path in tmp_path.glob("*.db")} == {
            "runtime.db",
            "materials.db",
            "telemetry.db",
            "history.db",
        }
    finally:
        shutdown_server_services()


@pytest.mark.parametrize(
    "service_type",
    (RuntimeService, MaterialsService, TelemetryService, HistoryService),
)
def test_services_require_an_explicit_repository(tmp_path, service_type) -> None:
    with pytest.raises(TypeError, match="Repository"):
        service_type(tmp_path / "service.db")


def test_server_services_reject_runtime_layout_switch(tmp_path) -> None:
    first = ServerDatabasePaths.resolve(tmp_path / "first")
    second = ServerDatabasePaths.resolve(tmp_path / "second")
    try:
        configure_server_services(first)
        with pytest.raises(RuntimeError, match="another database layout"):
            configure_server_services(second)
    finally:
        shutdown_server_services()


def test_host_startup_uses_four_new_writers_without_local_scheduler(
    tmp_path, monkeypatch
) -> None:
    paths = ServerDatabasePaths.resolve(tmp_path)
    monkeypatch.setattr(BasicConfig, "backend", "hostlink")
    monkeypatch.setattr(BasicConfig, "machine_name", "test-host")
    monkeypatch.setattr(BasicConfig, "server_database_paths", paths)
    try:
        materials = setup_materials_service(database_paths=paths)
        backend = setup_job_execution_backend(database_paths=paths)

        assert get_materials_service() is materials
        assert get_edge_backend() is backend
        assert backend.device_state.endpoint_uuid == "hostlink:test-host"
        assert {path.name for path in tmp_path.glob("*.db")} == {
            "runtime.db",
            "materials.db",
            "telemetry.db",
            "history.db",
        }
    finally:
        reset_for_test()
