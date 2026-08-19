from __future__ import annotations

from types import SimpleNamespace

from unilabos.app.cli.router import run_client_command
from unilabos.app.main import parse_args


def test_material_list_uses_microbackend_without_legacy(monkeypatch, capsys) -> None:
    calls = []

    class _Client:
        def __init__(self, address: str):
            calls.append(("address", address))

        def list_materials(self, *, roots_only: bool):
            calls.append(("roots_only", roots_only))
            return [
                SimpleNamespace(
                    model_dump=lambda **_kwargs: {"material_uuid": "material-1"}
                )
            ]

    monkeypatch.setattr(
        "unilabos.app.cli.material.HTTPMaterialsClient",
        _Client,
    )
    parser = parse_args()
    args = parser.parse_args(
        [
            "--material_microbackend_addr",
            "http://materials:8092/api/v1",
            "material",
            "list",
            "--roots_only",
        ]
    )

    assert run_client_command(args, parser) is True
    assert calls == [
        ("address", "http://materials:8092/api/v1"),
        ("roots_only", True),
    ]
    assert "material-1" in capsys.readouterr().out
