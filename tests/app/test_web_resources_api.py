from __future__ import annotations

from types import SimpleNamespace

from unilabos.app.web import api as api_module


def test_get_resources_flattens_resource_tree_set(monkeypatch) -> None:
    resources = SimpleNamespace(
        dump=lambda: [
            [{"id": "deck", "parent_uuid": ""}],
            [{"id": "device", "parent_uuid": ""}],
        ]
    )
    monkeypatch.setattr(api_module, "load_resources", lambda: (True, resources))

    response = api_module.get_resources()

    assert response.code == 0
    assert response.data == [
        {"id": "deck", "parent_uuid": ""},
        {"id": "device", "parent_uuid": ""},
    ]


def test_get_resources_reports_uninitialized_host(monkeypatch) -> None:
    monkeypatch.setattr(api_module, "load_resources", lambda: (False, "Host node not initialized"))

    response = api_module.get_resources()

    assert response.code != 0
    assert response.message == "Host node not initialized"
