from __future__ import annotations

import base64

from unilabos.app.cli import package as package_cli
from unilabos.app.cli.parser import build_parser
from unilabos.app.cli.router import run_cli_command
from unilabos.legacy_support import configure_legacy_support


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


def test_package_upload_resolves_cli_auth_before_runtime(
    monkeypatch,
    tmp_path,
) -> None:
    clients = []
    commands = []

    class _LegacyClient:
        def __init__(self, *, remote_addr: str, auth: str) -> None:
            self.remote_addr = remote_addr
            self.auth = auth
            clients.append(self)

    monkeypatch.setattr(
        "unilabos.legacy_support.http.LegacyHTTPClient",
        _LegacyClient,
    )
    monkeypatch.setattr(
        package_cli,
        "cmd_package",
        lambda values, http_client=None: commands.append((values, http_client)),
    )
    configure_legacy_support(True)
    try:
        parser = build_parser()
        args = parser.parse_args(
            [
                "--legacy",
                "--working-dir",
                str(tmp_path),
                "--ak",
                "access",
                "--sk",
                "secret",
                "--addr",
                "test",
                "package",
                "upload",
                "--path",
                ".",
            ]
        )

        assert run_cli_command(args, parser) is True
    finally:
        configure_legacy_support(False)

    assert clients[0].remote_addr == "https://leap-lab.test.bohrium.com/api/v1"
    assert clients[0].auth == base64.b64encode(b"access:secret").decode()
    assert commands[0][1] is clients[0]
