from unilabos.app.cli.parser import build_parser


def test_material_startup_uses_address_as_the_only_authority_switch() -> None:
    parser = build_parser()

    assert parser.parse_args([]).material_microbackend_addr is None
    assert (
        parser.parse_args(
            ["--material_microbackend_addr", "http://materials:8092/api/v1"]
        ).material_microbackend_addr
        == "http://materials:8092/api/v1"
    )
    assert "--material_service_mode" not in parser._option_string_actions


def test_edge_has_no_direct_backend_material_switch_or_writes() -> None:
    parser = build_parser()
    assert "--material_source" not in parser._option_string_actions
    assert "--legacy" not in parser._option_string_actions
    assert "--upload_registry" not in parser._option_string_actions
