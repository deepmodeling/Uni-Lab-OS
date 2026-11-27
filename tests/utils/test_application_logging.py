"""应用日志出口、逐次诊断门禁和 HTTP 降噪的行为测试。"""

from __future__ import annotations

import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from unilabos.config import config
from unilabos.utils import log
from unilabos.utils.fastapi.log_adapter import PollingAccessFilter, UvicornToIlabosHandler, setup_fastapi_logging
from unilabos.utils.log_storage import LogPolicy, SessionRotatingFileHandler


@pytest.fixture(autouse=True)
def isolated_loggers(monkeypatch):
    """保存测试框架的出口，避免配置测试关闭 pytest 捕获 handler。"""
    names = ["", "unilabos.comm", "websockets", "uvicorn", "uvicorn.access", "uvicorn.error", "fastapi"]
    snapshots = []
    for name in names:
        target = logging.getLogger(name)
        snapshots.append((target, target.handlers[:], target.level, target.propagate))
        target.handlers = []
    monkeypatch.setattr(log, "_comm_file_handler", None)
    monkeypatch.setattr(log, "_comm_ws_handler", None)
    monkeypatch.setattr(log, "_detailed_logging_enabled", False)
    yield
    created = set()
    for target, handlers, level, propagate in snapshots:
        created.update(target.handlers)
        target.handlers = handlers
        target.setLevel(level)
        target.propagate = propagate
    for handler in created:
        handler.close()


def test_default_file_level_and_explicit_detailed_trace(tmp_path):
    normal_path = Path(log.configure_logger("ERROR", tmp_path))
    log.logger.debug("不应写入普通文件")
    log.logger.info("常规运行事件")
    log.logger.error("业务失败仍可见")
    text = normal_path.read_text()
    assert "常规运行事件" in text and "业务失败仍可见" in text
    assert "不应写入普通文件" not in text

    detailed_path = Path(log.configure_logger("ERROR", tmp_path, log_detailed=True))
    log.logger.trace("详细跟踪事件")
    assert "详细跟踪事件" in detailed_path.read_text()
    assert detailed_path != normal_path
    assert log.is_detailed_logging_enabled()


def test_reconfiguration_closes_file_but_preserves_otel(tmp_path):
    otel = logging.Handler()
    otel._unilabos_otel_handler = True
    otel.close = Mock()
    logging.getLogger().addHandler(otel)
    first = log.configure_logger(working_dir=tmp_path)
    previous = next(h for h in logging.getLogger().handlers if isinstance(h, SessionRotatingFileHandler))
    second = log.configure_logger(working_dir=tmp_path)
    assert first != second
    assert previous.stream is None and previous._closed
    assert otel in logging.getLogger().handlers
    otel.close.assert_not_called()


def test_protocol_log_is_written_once_and_none_reconfiguration_releases_file(tmp_path):
    main_path = Path(log.configure_logger("ERROR", tmp_path))
    comm_path = Path(log.configure_comm_logger(tmp_path, "ERROR"))
    old_handler = log._comm_file_handler
    protocol_logger = logging.getLogger("websockets.client")
    protocol_logger.warning("唯一协议记录")
    assert comm_path.read_text().count("唯一协议记录") == 1
    assert "唯一协议记录" not in main_path.read_text()
    before = comm_path.stat().st_size
    log.configure_comm_logger(None, "ERROR")
    protocol_logger.warning("重配后的协议记录")
    assert old_handler.stream is None and old_handler._closed
    assert old_handler not in logging.getLogger("websockets").handlers
    assert comm_path.stat().st_size == before


def test_uvicorn_configuration_does_not_close_existing_session(tmp_path):
    uvicorn = pytest.importorskip("uvicorn")
    path = Path(log.configure_logger("ERROR", tmp_path))
    handler = next(h for h in logging.getLogger().handlers if isinstance(h, SessionRotatingFileHandler))
    uvicorn.Config(app=lambda scope, receive, send: None, log_config=setup_fastapi_logging())
    assert not handler._closed
    logging.getLogger("uvicorn.error").error("服务器启动诊断")
    assert path.read_text().count("服务器启动诊断") == 1


@pytest.mark.parametrize("args, expected", [
    (("local", "GET", "/api/v1/health", "1.1", 200), False),
    (("local", "GET", "/api/v1/readiness?probe=1", "1.1", 200), False),
    (("local", "GET", "/api/v1/health", "1.1", 503), True),
    (("local", "GET", "/api/v1/health/unknown", "1.1", 200), True),
    (("local", "GET", "/api/v1/health", "1.1", 307), True),
    (("local", "POST", "/api/v1/health", "1.1", 200), True),
    (("local", "GET", "/api/v1/workflow-tasks", "1.1", 200), True),
    (("未知格式",), True),
])
def test_access_filter_is_exact_and_keeps_failed_requests(args, expected):
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "%s", args, None)
    assert PollingAccessFilter().filter(record) is expected


def test_detailed_mode_and_warning_access_records_are_preserved(monkeypatch):
    args = ("local", "GET", "/api/v1/health", "1.1", 200)
    record = logging.LogRecord("uvicorn.access", logging.WARNING, __file__, 1, "%s", args, None)
    assert PollingAccessFilter().filter(record)
    monkeypatch.setattr(log, "_detailed_logging_enabled", True)
    record.levelno = logging.INFO
    assert PollingAccessFilter().filter(record)


def test_access_handler_filters_before_creating_application_record(monkeypatch):
    info = Mock()
    handler = UvicornToIlabosHandler()
    handler.level_map[logging.INFO] = info
    args = ("local", "GET", "/api/v1/health", "1.1", 200)
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d', args, None)
    handler.handle(record)
    info.assert_not_called()


def _load_ros_class(name: str):
    """直接执行被测类，避免无 ROS 环境导入整套硬件依赖。"""
    source = Path(__file__).parents[2] / "unilabos/ros/nodes/base_device_node.py"
    tree = ast.parse(source.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name)
    namespace = {"asyncio": asyncio, "is_detailed_logging_enabled": log.is_detailed_logging_enabled}
    namespace.update({level: getattr(log, level) for level in ["trace", "debug", "info", "warning", "error", "critical"]})
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), namespace)
    return namespace[name]


@pytest.mark.parametrize("detailed", [False, True])
def test_property_trace_gate_precedes_python_records_and_ros_forwarding(tmp_path, detailed):
    log.configure_logger("TRACE", tmp_path, file_log_level="TRACE", log_detailed=detailed)
    ros = Mock()
    adapter = _load_ros_class("ROSLoggerAdapter")(ros, "device")
    publisher_type = _load_ros_class("PropertyPublisher")
    publisher = object.__new__(publisher_type)
    publisher.node = SimpleNamespace(lab_logger=lambda: adapter)
    publisher.name = "温度"
    publisher.get_method = lambda: 42
    records = []
    recorder = logging.Handler()
    recorder.emit = records.append
    logging.getLogger().addHandler(recorder)
    assert publisher.get_property() == 42
    assert bool(records) is detailed
    assert bool(ros.debug.call_count) is detailed


@pytest.mark.parametrize("detailed", [False, True])
def test_async_property_trace_gate_keeps_error(tmp_path, detailed):
    log.configure_logger("ERROR", tmp_path, log_detailed=detailed)
    ros = Mock()
    adapter = _load_ros_class("ROSLoggerAdapter")(ros, "device")
    publisher_type = _load_ros_class("PropertyPublisher")
    publisher = object.__new__(publisher_type)
    publisher.node = SimpleNamespace(lab_logger=lambda: adapter)
    publisher.name = "温度"

    async def success():
        return 43

    publisher.get_method = success
    asyncio.run(publisher.get_property_async())
    assert publisher._value == 43
    assert bool(ros.debug.call_count) is detailed
    ros.reset_mock()

    async def failure():
        raise RuntimeError("传感器失败")

    publisher.get_method = failure
    asyncio.run(publisher.get_property_async())
    assert any("传感器失败" in call.args[0] for call in ros.debug.call_args_list)


@pytest.fixture
def workstation_http():
    return pytest.importorskip(
        "unilabos.devices.workstation.workstation_http_service", exc_type=ModuleNotFoundError
    )


@pytest.mark.parametrize("endpoint", ["/health", "/status?probe=1"])
def test_workstation_successful_polling_does_not_write_raw_report(endpoint, workstation_http):
    handler = object.__new__(workstation_http.WorkstationHTTPHandler)
    handler.path = endpoint
    handler._save_raw_request = Mock()
    handler._send_response = Mock()
    handler._handle_status_check = lambda: workstation_http.HttpResponse(success=True, message="健康")
    handler.do_GET()
    handler._save_raw_request.assert_not_called()
    assert handler._send_response.call_args.args[0].success


def test_workstation_failed_status_keeps_bounded_diagnostic(monkeypatch, workstation_http):
    handler = object.__new__(workstation_http.WorkstationHTTPHandler)
    handler.path = "/status"
    handler._save_raw_request = Mock()
    handler._send_response = Mock()
    handler._handle_status_check = lambda: workstation_http.HttpResponse(success=False, message="状态失败")
    warning = Mock()
    monkeypatch.setattr(log.logger, "warning", warning)
    handler.do_GET()
    handler._save_raw_request.assert_not_called()
    warning.assert_called_once_with("工作站查询失败: %s - %s", "/status", "状态失败")
    assert not handler._send_response.call_args.args[0].success


@pytest.mark.parametrize("field, value", [
    ("log_max_bytes", "bad"), ("log_backup_count", "0"),
    ("log_retention_days", "-1"), ("log_total_max_bytes", "1.2"),
    ("log_cleanup_interval_seconds", "0"), ("log_detailed", "maybe"),
    ("file_log_level", "nonsense"),
])
def test_invalid_env_log_configuration_is_rejected(monkeypatch, field, value):
    monkeypatch.setenv(f"UNILABOS_BASICCONFIG_{field.upper()}", value)
    monkeypatch.setattr(config.BasicConfig, field, getattr(config.BasicConfig, field))
    with pytest.raises(ValueError):
        config._update_config_from_env()
        config._validate_log_config()


@pytest.mark.parametrize("detailed", [False, True])
def test_device_discovery_gates_unchanged_polling_and_keeps_offline_transition(tmp_path, detailed):
    log.configure_logger("TRACE", tmp_path, file_log_level="TRACE", log_detailed=detailed)
    ros = Mock()
    adapter = _load_ros_class("ROSLoggerAdapter")(ros, "host")
    source = Path(__file__).parents[2] / "unilabos/ros/nodes/presets/host_node.py"
    tree = ast.parse(source.read_text())
    host = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "HostNode")
    discovery = next(n for n in host.body if isinstance(n, ast.FunctionDef) and n.name == "_discover_devices")
    namespace = {"is_detailed_logging_enabled": log.is_detailed_logging_enabled}
    exec(compile(ast.Module(body=[discovery], type_ignores=[]), str(source), "exec"), namespace)
    discover = namespace["_discover_devices"]
    device_key = "/devices/sensor/sensor"
    device = SimpleNamespace(
        lab_logger=lambda: adapter,
        device_id="host",
        get_node_names_and_namespaces=lambda: [("sensor", "/devices/sensor")],
        _simulated_device_keys=set(),
        _online_devices={device_key},
        devices_names={"sensor": "/devices/sensor"},
        _action_clients={},
        _has_ready_action_client=lambda clients: False,
    )
    discover(device)
    assert device._online_devices == {device_key}
    assert bool(ros.debug.call_count) is detailed
    ros.reset_mock()
    device.get_node_names_and_namespaces = lambda: []
    discover(device)
    assert device._online_devices == set()
    assert any("Device offline:" in call.args[0] for call in ros.debug.call_args_list)


@pytest.mark.parametrize("activate_before_comm", [False, True])
def test_real_otel_handler_survives_reconfiguration_and_receives_protocol_once(tmp_path, activate_before_comm):
    from unilabos.utils import tracing

    log.configure_logger("ERROR", tmp_path)
    if not activate_before_comm:
        log.configure_comm_logger(tmp_path, "ERROR")
    records = []
    target = logging.Handler()
    target.emit = records.append
    handler = tracing._attach_otel_log_handler(logging.getLogger(), target)
    try:
        tracing._activate_otel_log_handler(handler, logging.getLogger())
        if activate_before_comm:
            log.configure_comm_logger(tmp_path, "ERROR")
        log.configure_logger("ERROR", tmp_path)
        log.configure_comm_logger(tmp_path, "ERROR")
        records.clear()
        log.logger.warning("主日志观测事件")
        log.get_comm_logger().warning("通信观测事件")
        logging.getLogger("websockets.client").warning("协议观测事件")
        assert [record.getMessage() for record in records] == [
            "主日志观测事件", "通信观测事件", "协议观测事件",
        ]
        assert handler in logging.getLogger().handlers
        assert handler in log.get_comm_logger().handlers
        assert not handler._closed
    finally:
        tracing._deactivate_otel_log_handler(handler)
        handler.close()


def test_env_log_policy_overrides_file_values_with_positive_durations(monkeypatch):
    monkeypatch.setattr(config.BasicConfig, "log_max_bytes", 20)
    monkeypatch.setattr(config.BasicConfig, "log_retention_days", 7)
    monkeypatch.setattr(config.BasicConfig, "log_cleanup_interval_seconds", 600)
    monkeypatch.setenv("UNILABOS_BASICCONFIG_LOG_MAX_BYTES", "1024")
    monkeypatch.setenv("UNILABOS_BASICCONFIG_LOG_RETENTION_DAYS", "0.5")
    monkeypatch.setenv("UNILABOS_BASICCONFIG_LOG_CLEANUP_INTERVAL_SECONDS", "0.2")
    config._update_config_from_env()
    config._validate_log_config()
    policy = LogPolicy.from_config(config.BasicConfig)
    assert policy.max_bytes == 1024
    assert policy.retention_days == 0.5
    assert policy.cleanup_interval_seconds == 0.2


def test_workstation_business_post_keeps_full_raw_report(workstation_http):
    import io
    import json

    payload = {"token": "report-session", "request_time": "2026-09-09", "data": {"sampleId": "sample-1"}}
    body = json.dumps(payload).encode()
    handler = object.__new__(workstation_http.WorkstationHTTPHandler)
    handler.path = "/report/step_finish"
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = io.BytesIO(body)
    handler.workstation = SimpleNamespace(_reports_received_count=0)
    handler._save_raw_request = Mock()
    handler._send_response = Mock()
    handler._handle_step_finish_report = Mock(return_value=workstation_http.HttpResponse(success=True, message="已报送"))
    handler.do_POST()
    handler._save_raw_request.assert_called_once_with("/report/step_finish", {"method": "POST", **payload})
    handler._handle_step_finish_report.assert_called_once_with(payload)
    assert handler.workstation._reports_received_count == 1


def test_missing_async_property_loop_keeps_error_and_skips_polling_trace(tmp_path):
    log.configure_logger("ERROR", tmp_path)
    ros = Mock()
    adapter = _load_ros_class("ROSLoggerAdapter")(ros, "device")
    publisher = object.__new__(_load_ros_class("PropertyPublisher"))
    publisher.node = SimpleNamespace(lab_logger=lambda: adapter)
    publisher.name = "温度"
    publisher._PropertyPublisher__loop = None

    async def read_sensor():
        return 42

    publisher.get_method = read_sensor
    assert publisher.get_property() is None
    assert ros.debug.call_count == 1
    assert "事件循环未初始化" in ros.debug.call_args.args[0]


def test_workstation_get_exception_does_not_grow_business_raw_file(monkeypatch, workstation_http):
    handler = object.__new__(workstation_http.WorkstationHTTPHandler)
    handler.path = "/status?probe=1"
    handler._save_raw_request = Mock()
    handler._send_response = Mock()
    failure = RuntimeError("查询失败")
    handler._handle_status_check = Mock(side_effect=failure)
    error = Mock()
    monkeypatch.setattr(log.logger, "error", error)
    handler.do_GET()
    handler._save_raw_request.assert_not_called()
    error.assert_called_once_with("GET请求处理失败: %s - %s", handler.path, failure)
    assert not handler._send_response.call_args.args[0].success
