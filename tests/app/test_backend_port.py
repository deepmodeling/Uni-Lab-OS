"""``--role backend`` 与默认权威用同一个管理端口设置（``--port`` / ``BasicConfig.port``，默认 8002）；
Host 没给 ``--address`` 时缺省连的也是它。旧配置里的 ``HTTPConfig.backend_port`` 已删除，静默忽略。
"""

from __future__ import annotations

import logging
from types import ModuleType

from unilabos.app.backend_main import authority_port
from unilabos.app.cli.parser import build_parser
from unilabos.config.config import BasicConfig, HTTPConfig
from unilabos.server.backend.legacy_adaptor.session import BaseBackendClient


def test_role_backend_defaults_to_the_default_authority_port(monkeypatch) -> None:
    monkeypatch.setattr(BasicConfig, "port", 8002)
    for argv in (["--role", "backend"], []):
        args = vars(build_parser().parse_args(argv))
        assert args["port_management"] is None
        assert authority_port(args) == 8002
    backend = vars(build_parser().parse_args(["--role", "backend", "--port", "9100"]))
    assert authority_port(backend) == 9100
    # 端口只有一个设置：配置文件改 BasicConfig.port 对两种权威同样生效
    monkeypatch.setattr(BasicConfig, "port", 9200)
    assert authority_port(vars(build_parser().parse_args(["--role", "backend"]))) == 9200


def test_host_without_address_connects_to_the_authority_port(monkeypatch) -> None:
    monkeypatch.setattr(HTTPConfig, "remote_addr", "")
    monkeypatch.setattr(BasicConfig, "port", 8002)
    assert BaseBackendClient.default_backend_url() == "http://127.0.0.1:8002"
    assert BaseBackendClient.backend_url() == "http://127.0.0.1:8002"
    assert BaseBackendClient.address_source() == "default"


def test_old_backend_port_in_config_or_env_is_ignored_quietly(monkeypatch, caplog) -> None:
    from unilabos.config import config as config_module

    assert not hasattr(HTTPConfig, "backend_port")
    module = ModuleType("lab_old_config")
    module.HTTPConfig = type("HTTPConfig", (), {"backend_port": 8081})
    config_module._update_config_from_module(module)
    assert not hasattr(HTTPConfig, "backend_port")

    monkeypatch.setenv("UNILABOS_HTTPCONFIG_BACKEND_PORT", "8081")
    with caplog.at_level(logging.INFO):
        config_module._update_config_from_env()
    assert not hasattr(HTTPConfig, "backend_port")
    assert "BACKEND_PORT" not in caplog.text.upper()
