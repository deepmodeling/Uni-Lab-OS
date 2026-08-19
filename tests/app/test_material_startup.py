import pytest

from unilabos.app.main import (
    configure_material_startup,
    parse_args,
    should_start_embedded_material_service,
)
from unilabos.config.config import HTTPConfig
from unilabos.app.web.client import HTTPClient


@pytest.fixture(autouse=True)
def _restore_material_microbackend_address():
    previous = HTTPConfig.material_microbackend_addr
    try:
        yield
    finally:
        HTTPConfig.material_microbackend_addr = previous


def test_material_startup_selects_only_embedded_or_external_microbackend() -> None:
    embedded = {"material_service_mode": "embedded"}
    assert configure_material_startup(embedded) == "embedded"
    assert HTTPConfig.material_microbackend_addr == ""
    assert should_start_embedded_material_service(
        embedded, is_host_mode=True
    )

    external = {"material_service_mode": "external"}
    assert configure_material_startup(external) == "external"
    assert HTTPConfig.material_microbackend_addr == "http://127.0.0.1:8092/api/v1"
    assert not should_start_embedded_material_service(
        external, is_host_mode=True
    )


def test_edge_has_no_direct_backend_material_switch_or_writes() -> None:
    parser = parse_args()
    assert "--material_source" not in parser._option_string_actions
    for method_name in (
        "resource_add",
        "resource_edge_add",
        "resource_tree_add",
        "resource_update",
    ):
        assert not hasattr(HTTPClient, method_name)
