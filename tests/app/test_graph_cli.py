"""``unilab graph`` CLI：参数契约、子命令分发与 --remote 语义。"""

from __future__ import annotations

import json

import pytest

from unilabos.app.cli.graph import _create_graph_client, cmd_graph_command
from unilabos.app.cli.parser import build_parser
from unilabos.client import SessionManager

PAYLOAD = {"nodes": [{"id": "pump"}], "links": []}


class TestParserContract:
    def test_graph_subcommands_parse(self) -> None:
        parser = build_parser()

        upload = parser.parse_args(
            ["graph", "upload", "-f", "lab.json", "-n", "lab", "--tags", "a", "b"]
        )
        assert upload.command == "graph"
        assert upload.graph_command == "upload"
        assert upload.graph_file == "lab.json"
        assert upload.graph_name == "lab"
        assert upload.tags == ["a", "b"]
        assert upload.remote is False

        listing = parser.parse_args(["graph", "list", "--remote", "--name", "lan"])
        assert listing.remote is True and listing.name == "lan"

        create = parser.parse_args(
            ["graph", "create", "--devices", "pkg", "-o", "out.json"]
        )
        assert create.devices == ["pkg"] and create.output == "out.json"

        download = parser.parse_args(["graph", "download", "lan", "-o", "d.json"])
        assert download.identity == "lan" and download.output == "d.json"


class _RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def upsert_graph(self, **kwargs):
        self.calls.append(("upsert", kwargs))
        return {"uuid": "u-1", "name": kwargs["name"], "revision": 1}

    def list_graphs(self, **kwargs):
        self.calls.append(("list", kwargs))
        return {"items": [], "total": 0, "page": 1, "page_size": 100}

    def get_graph(self, identity):
        self.calls.append(("get", identity))
        return {"uuid": "u-1", "name": identity}

    def download_graph(self, identity):
        self.calls.append(("download", identity))
        return PAYLOAD

    def delete_graph(self, identity):
        self.calls.append(("delete", identity))
        return {}

    def close(self) -> None:
        self.calls.append(("close",))


@pytest.fixture()
def recording_client(monkeypatch):
    client = _RecordingClient()
    monkeypatch.setattr(
        "unilabos.app.cli.graph._create_graph_client",
        lambda args, session_manager: client,
    )
    return client


def _args(**values):
    parser = build_parser()
    argv = values.pop("argv")
    args = parser.parse_args(argv)
    for key, value in values.items():
        setattr(args, key, value)
    return args


class TestCommandDispatch:
    def test_upload_defaults_name_to_file_stem(
        self, tmp_path, recording_client
    ) -> None:
        graph_file = tmp_path / "my_lab.json"
        graph_file.write_text(json.dumps(PAYLOAD), encoding="utf-8")
        args = _args(argv=["graph", "upload", "-f", str(graph_file)])

        cmd_graph_command(args, session_manager=None)

        action, kwargs = recording_client.calls[0]
        assert action == "upsert"
        assert kwargs["name"] == "my_lab"
        assert kwargs["payload"] == PAYLOAD
        assert recording_client.calls[-1] == ("close",)

    def test_upload_rejects_invalid_graph_file(
        self, tmp_path, recording_client
    ) -> None:
        bad_file = tmp_path / "bad.json"
        bad_file.write_text(json.dumps({"nodes": "not-a-list"}), encoding="utf-8")
        args = _args(argv=["graph", "upload", "-f", str(bad_file)])

        with pytest.raises(SystemExit):
            cmd_graph_command(args, session_manager=None)
        assert all(call[0] != "upsert" for call in recording_client.calls)

    def test_download_writes_payload_file(self, tmp_path, recording_client) -> None:
        output = tmp_path / "out" / "lan.json"
        args = _args(argv=["graph", "download", "lan", "-o", str(output)])

        cmd_graph_command(args, session_manager=None)

        assert ("download", "lan") in recording_client.calls
        with output.open(encoding="utf-8") as stream:
            assert json.load(stream) == PAYLOAD

    def test_get_and_delete_pass_identity(self, recording_client) -> None:
        cmd_graph_command(_args(argv=["graph", "get", "u-1"]), session_manager=None)
        cmd_graph_command(_args(argv=["graph", "delete", "u-1"]), session_manager=None)
        actions = [call[0] for call in recording_client.calls]
        assert "get" in actions and "delete" in actions


class TestRemoteResolution:
    def test_remote_without_base_url_fails_fast(self, tmp_path) -> None:
        args = _args(argv=["graph", "list", "--remote"])
        session_manager = SessionManager(working_dir=str(tmp_path))

        with pytest.raises(SystemExit):
            _create_graph_client(args, session_manager)

    def test_local_default_targets_loopback(self, tmp_path) -> None:
        args = _args(argv=["graph", "list"])
        session_manager = SessionManager(working_dir=str(tmp_path))

        client = _create_graph_client(args, session_manager)
        try:
            assert client.base_url.startswith("http://127.0.0.1:")
        finally:
            client.close()
