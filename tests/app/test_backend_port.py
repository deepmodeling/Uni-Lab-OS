"""``--role backend`` 与默认权威用同一个管理端口设置（``--port`` / ``BasicConfig.port``，默认 8002）；
Host 没给 ``--address`` 时缺省连的也是它。旧配置里的 ``HTTPConfig.backend_port`` 已删除：值被忽略，
启动时警告一次并给出替代项。
"""

from __future__ import annotations

import logging
from types import ModuleType

import pytest

from unilabos.app.backend_main import authority_port
from unilabos.app.cli.parser import build_parser
from unilabos.config.config import BasicConfig, HTTPConfig
from unilabos.client.host.legacy_adaptor.session import BaseBackendClient


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


def _backend_port_warnings(caplog) -> list[str]:
    return [
        record.getMessage() for record in caplog.records
        if record.levelno == logging.WARNING and "backend_port" in record.getMessage()
    ]


@pytest.fixture
def config_module(monkeypatch):
    from unilabos.config import config as module

    # 每个用例都当作新进程启动：已提示集合清空
    monkeypatch.setattr(module, "_warned_removed_fields", set(), raising=False)
    monkeypatch.delenv("UNILABOS_HTTPCONFIG_BACKEND_PORT", raising=False)
    return module


def test_old_backend_port_is_ignored_with_one_startup_warning(config_module, monkeypatch, caplog) -> None:
    assert not hasattr(HTTPConfig, "backend_port")
    module = ModuleType("lab_old_config")
    module.HTTPConfig = type("HTTPConfig", (), {"backend_port": 8081})
    monkeypatch.setenv("UNILABOS_HTTPCONFIG_BACKEND_PORT", "8081")
    with caplog.at_level(logging.WARNING):
        config_module._update_config_from_module(module)
        config_module._update_config_from_env()
    assert not hasattr(HTTPConfig, "backend_port")
    # 配置文件与环境变量都写了也只提示一次，并给出替代项
    (warning,) = _backend_port_warnings(caplog)
    assert "HTTPConfig.backend_port 已删除" in warning and "配置文件" in warning
    assert "--port" in warning and "BasicConfig.port" in warning


def test_backend_port_only_in_env_also_warns(config_module, monkeypatch, caplog) -> None:
    monkeypatch.setenv("UNILABOS_HTTPCONFIG_BACKEND_PORT", "8081")
    with caplog.at_level(logging.WARNING):
        config_module._update_config_from_env()
    assert not hasattr(HTTPConfig, "backend_port")
    (warning,) = _backend_port_warnings(caplog)
    assert "UNILABOS_HTTPCONFIG_BACKEND_PORT" in warning and "--port" in warning


def test_no_warning_without_backend_port(config_module, caplog) -> None:
    module = ModuleType("lab_new_config")
    module.BasicConfig = type("BasicConfig", (), {"control_token": "left-over"})
    with caplog.at_level(logging.WARNING):
        config_module._update_config_from_module(module)
        config_module._update_config_from_env()
    assert _backend_port_warnings(caplog) == []
    assert not hasattr(BasicConfig, "control_token")  # 其余已删除字段仍静默忽略
