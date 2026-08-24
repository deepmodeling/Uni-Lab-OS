from __future__ import annotations

from unilabos.app.cli.parser import build_parser
from unilabos.app.cli.router import _prepare_command_session, run_cli_command


def test_parser_accepts_dash_and_underscore_aliases() -> None:
    parser = build_parser()

    dashed = parser.parse_args(
        ["--working-dir", "edge", "material", "list", "--roots-only"]
    )
    underscored = parser.parse_args(
        ["--working_dir", "edge", "material", "list", "--roots_only"]
    )

    assert dashed.working_dir == underscored.working_dir == "edge"
    assert dashed.roots_only is True
    assert underscored.roots_only is True


def test_parser_uses_one_address_for_top_level_and_client_subcommands() -> None:
    parser = build_parser()

    top_level = parser.parse_args(
        ["--address", "test", "material", "list"]
    )
    nested = parser.parse_args(
        ["material", "list", "--addr", "test"]
    )

    assert top_level.address == nested.address == "test"
    _prepare_command_session(top_level, parser)
    _prepare_command_session(nested, parser)
    assert top_level.address_resolved == (
        "https://leap-lab.test.bohrium.com/api/v1"
    )
    assert nested.address_resolved == top_level.address_resolved
    assert "--schedule_addr" not in parser._option_string_actions
    assert "--schedule-address" not in parser._option_string_actions


def test_unified_router_dispatches_package_without_runtime(monkeypatch, tmp_path) -> None:
    calls = []
    monkeypatch.setattr(
        "unilabos.app.cli.router.run_package_command",
        lambda values, **kwargs: calls.append((values, kwargs)) or True,
    )
    parser = build_parser()
    args = parser.parse_args(
        [
            "--working-dir",
            str(tmp_path),
            "package",
            "inspect",
            "--path",
            ".",
        ]
    )

    assert run_cli_command(args, parser) is True
    assert calls[0][0]["package_action"] == "inspect"
    assert calls[0][1]["args_namespace"] is args
