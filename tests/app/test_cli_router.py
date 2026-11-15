from __future__ import annotations

import json
import socket

import pytest

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
        ["--address", "http://backend:8002/api/v1/", "material", "list"]
    )
    nested = parser.parse_args(
        ["material", "list", "--addr", "http://backend:8002/api/v1/"]
    )

    assert top_level.address == nested.address == "http://backend:8002/api/v1/"
    _prepare_command_session(top_level, parser)
    _prepare_command_session(nested, parser)
    assert top_level.address_resolved == "http://backend:8002/api/v1"
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


def test_config_path_and_explicit_working_dir_select_the_matching_token(monkeypatch, tmp_path):
    from unilabos.config.config import BasicConfig
    from unilabos.config.control_token import TOKEN_ENV, client_token

    monkeypatch.setattr(BasicConfig, "control_token", "")
    monkeypatch.setattr(BasicConfig, "working_dir", "stale-directory")
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    config_dir = tmp_path / "configured"
    data_dir = config_dir / "unilabos_data"
    data_dir.mkdir(parents=True)
    config = config_dir / "site.py"
    config.write_text("# 管理命令只解析路径，无需执行此文件\n", encoding="utf-8")
    (data_dir / "control_token").write_text("configured-token", encoding="utf-8")
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    (explicit / "control_token").write_text("explicit-token", encoding="utf-8")
    parser = build_parser()

    args = parser.parse_args(["--config", str(config), "runtime", "status"])
    session = _prepare_command_session(args, parser)
    assert session.working_dir == data_dir
    assert client_token() == "configured-token"
    args = parser.parse_args(["--config", str(config), "--working-dir", str(explicit), "package", "list"])
    assert _prepare_command_session(args, parser).working_dir == explicit
    assert client_token() == "explicit-token"


def test_doctor_subcommands_parse_and_keep_top_level_ros_domain() -> None:
    parser = build_parser()

    net = parser.parse_args(["doctor", "net", "--hostlink-addr", "10.0.0.2:7302", "--count", "3"])
    assert (net.command, net.doctor_command, net.hostlink_addr, net.count) == ("doctor", "net", "10.0.0.2:7302", 3)
    # 顶层 --ros_domain_id 不被子命令的缺省值覆盖；写在子命令后面的同名参数照常生效
    inherited = parser.parse_args(["--ros_domain_id", "5", "doctor", "talker", "--peer", "10.0.0.3"])
    assert (inherited.ros_domain_id, inherited.peer, inherited.discovery) == (5, "10.0.0.3", "")
    local = parser.parse_args(["doctor", "listener", "--ros-domain-id", "7", "--quiet", "--duration", "2"])
    assert (local.ros_domain_id, local.quiet, local.duration) == (7, True, 2.0)
    fake = parser.parse_args(["doctor", "fake-device", "--device-id", "probe", "--no-service-check"])
    assert (fake.doctor_command, fake.device_id, fake.no_service_check) == ("fake-device", "probe", True)
    with pytest.raises(SystemExit):
        parser.parse_args(["doctor"])


def test_doctor_is_dispatched_without_session_or_runtime(monkeypatch) -> None:
    calls = []

    def no_session(*_args):
        raise AssertionError("doctor 不应读取会话或工作目录")

    monkeypatch.setattr("unilabos.app.cli.router._prepare_command_session", no_session)
    monkeypatch.setattr(
        "unilabos.backend.hostlink.doctor.run_doctor", lambda values: calls.append(dict(values)) or 0,
    )
    parser = build_parser()

    assert run_cli_command(parser.parse_args(["doctor", "net", "--hostlink-addr", "10.0.0.2"]), parser) is True
    assert calls[0]["doctor_command"] == "net" and calls[0]["hostlink_addr"] == "10.0.0.2"
    monkeypatch.setattr("unilabos.backend.hostlink.doctor.run_doctor", lambda _values: 3)
    with pytest.raises(SystemExit) as exited:
        run_cli_command(parser.parse_args(["doctor", "fake-device"]), parser)
    assert exited.value.code == 3


def _report(output: str) -> dict:
    return json.loads(output[output.index("{"): output.rindex("}") + 1])


def test_doctor_net_probes_a_real_hostlink_server(capsys) -> None:
    from unilabos.backend.hostlink.server import HostLinkServer

    parser = build_parser()
    server = HostLinkServer("127.0.0.1", 0).start()
    try:
        args = parser.parse_args(["doctor", "net", "--hostlink-addr", f"127.0.0.1:{server.port}", "--count", "2"])
        assert run_cli_command(args, parser) is True
    finally:
        server.stop()
    report = _report(capsys.readouterr().out)
    assert report["verdict"] == "ok"
    assert report["hello"]["ok"] is True and report["ping"]["ok"] == 2

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    args = parser.parse_args(["doctor", "net", "--hostlink-addr", f"127.0.0.1:{closed_port}", "--count", "1"])
    with pytest.raises(SystemExit) as exited:
        run_cli_command(args, parser)
    assert exited.value.code == 2
    assert _report(capsys.readouterr().out)["verdict"] == "tcp_fail"
