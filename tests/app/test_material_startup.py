from unilabos.app.main import (
    parse_args,
)
from unilabos.app.web.client import HTTPClient


def test_material_startup_uses_address_as_the_only_authority_switch() -> None:
    parser = parse_args()

    assert parser.parse_args([]).material_microbackend_addr is None
    assert (
        parser.parse_args(
            ["--material_microbackend_addr", "http://materials:8092/api/v1"]
        ).material_microbackend_addr
        == "http://materials:8092/api/v1"
    )
    assert "--material_service_mode" not in parser._option_string_actions


def test_edge_has_no_direct_backend_material_switch_or_writes() -> None:
    parser = parse_args()
    assert "--material_source" not in parser._option_string_actions
    assert "--legacy" in parser._option_string_actions
    for method_name in (
        "resource_add",
        "resource_edge_add",
        "resource_tree_add",
        "resource_update",
    ):
        assert not hasattr(HTTPClient, method_name)
