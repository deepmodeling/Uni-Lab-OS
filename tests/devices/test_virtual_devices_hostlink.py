"""内置虚拟设备：注册合同、HostLink 真实执行（不走 ``--test_mode`` 占位）、示例图与代表协议。

驱动按装饰器注册表条目（AST 扫描，与运行时同一份）由 HostLink 本地运行时实例化、启动，
动作走运行时的同一条调用链（goal 映射、样品映射与 ActionContext 注入）。设备类的默认时间倍率在用例里
设为 0：逻辑与状态变化照常发生，只是不等待；时间倍率、进度与取消另有专门的用例。
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import networkx as nx
import pytest

from unilabos.backend.hostlink.local_runtime import HostLinkDriverSpec, HostLinkLocalRuntime
from unilabos.backend.runtime import workstation_protocol
from unilabos.backend.runtime.action import ActionCancelled, ActionContext
from unilabos.config.config import BasicConfig
from unilabos.devices.virtual.simulation import DEFAULT_TIME_SCALE, VirtualDevice
from unilabos.devices.workstation.workstation_base import WorkstationBase
from unilabos.registry.ast_registry_scanner import _parse_file
from unilabos.registry.registry import Registry
from unilabos.utils.import_manager import default_manager

REPO_ROOT = Path(__file__).resolve().parents[2]
VIRTUAL_DIR = REPO_ROOT / "unilabos" / "devices" / "virtual"
EXPERIMENTS = REPO_ROOT / "unilabos" / "test" / "experiments"
ROS_ACTIONS = REPO_ROOT / "unilabos_msgs" / "action"

#: 内置虚拟设备（注册表条目名不变）
VIRTUAL_DEVICE_IDS = {
    "virtual_centrifuge",
    "virtual_column",
    "virtual_filter",
    "virtual_gas_source",
    "virtual_heatchill",
    "virtual_heating_platform",
    "virtual_multiway_valve",
    "virtual_rotavap",
    "virtual_separator",
    "virtual_solenoid_valve",
    "virtual_solid_dispenser",
    "virtual_stirrer",
    "virtual_transfer_pump",
    "virtual_vacuum_pump",
}

#: 协议编译器调用的动作与它们的 ROS 2 原生动作类型（ROS 2 工作站只为原生类型建动作客户端）
COMPILER_ACTIONS = {
    ("virtual_centrifuge", "centrifuge"): "Centrifuge",
    ("virtual_column", "run_column"): "RunColumn",
    ("virtual_filter", "filter"): "Filter",
    ("virtual_gas_source", "set_status"): "StrSingleInput",
    ("virtual_heatchill", "heat_chill"): "HeatChill",
    ("virtual_heatchill", "heat_chill_start"): "HeatChillStart",
    ("virtual_heatchill", "heat_chill_stop"): "HeatChillStop",
    ("virtual_multiway_valve", "set_valve_position"): "SendCmd",
    ("virtual_multiway_valve", "set_position"): "SendCmd",
    ("virtual_rotavap", "evaporate"): "Evaporate",
    ("virtual_separator", "separate"): "Separate",
    ("virtual_separator", "valve_open"): "SendCmd",
    ("virtual_solenoid_valve", "set_valve_position"): "SendCmd",
    ("virtual_solenoid_valve", "set_status"): "StrSingleInput",
    ("virtual_solid_dispenser", "add_solid"): "Add",
    ("virtual_stirrer", "stir"): "Stir",
    ("virtual_stirrer", "start_stir"): "StartStir",
    ("virtual_stirrer", "stop_stir"): "StopStir",
    ("virtual_transfer_pump", "set_position"): "SetPumpPosition",
    ("virtual_transfer_pump", "set_valve_position"): "SendCmd",
    ("virtual_transfer_pump", "transfer"): "Transfer",
    ("virtual_vacuum_pump", "set_status"): "StrSingleInput",
}


@pytest.fixture(scope="module")
def registry() -> dict[str, Any]:
    """装饰器注册表条目：AST 扫描 ``unilabos/devices/virtual``，与运行时注册表同一套构建。"""

    builder = Registry.__wrapped__()
    entries: dict[str, Any] = {}
    for path in sorted(VIRTUAL_DIR.glob("*.py")):
        devices, _resources, _workflows = _parse_file(path, REPO_ROOT)
        for meta in devices:
            entries[meta["device_id"]] = builder._build_device_entry_from_ast(meta["device_id"], meta)
    return entries


@pytest.fixture(autouse=True)
def _instant_real_execution(monkeypatch):
    monkeypatch.setattr(BasicConfig, "test_mode", False)
    monkeypatch.setattr(VirtualDevice, "default_time_scale", 0.0)


def _spec(
    registry: dict[str, Any],
    device_id: str,
    name: str,
    config: dict[str, Any] | None = None,
    **extra: Any,
) -> HostLinkDriverSpec:
    class_config = registry[name]["class"]
    mappings = class_config["action_value_mappings"]
    return HostLinkDriverSpec(
        device_id=device_id,
        driver_class=default_manager.get_class(class_config["module"]),
        config=dict(config or {}),
        registry_name=name,
        action_names=tuple(mappings),
        action_value_mappings=mappings,
        status_names=tuple(class_config.get("status_types") or {}),
        **extra,
    )


@contextmanager
def _running(*specs: HostLinkDriverSpec) -> Iterator[HostLinkLocalRuntime]:
    runtime = HostLinkLocalRuntime()
    for spec in specs:
        runtime.add_driver(spec)
    runtime.start()
    try:
        yield runtime
    finally:
        runtime.stop()


def _driver(runtime: HostLinkLocalRuntime, device_id: str) -> Any:
    return runtime.devices[device_id].driver


def _ros_goal_fields(type_name: str) -> list[str]:
    text = (ROS_ACTIONS / f"{type_name}.action").read_text(encoding="utf-8").split("---", 1)[0]
    fields = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            fields.append(line.split()[1])
    return fields


# ---------------------------------------------------------------------------
# 注册合同
# ---------------------------------------------------------------------------


def test_virtual_devices_are_decorator_registered_for_both_backends(registry) -> None:
    assert set(registry) == VIRTUAL_DEVICE_IDS
    # 不再有 YAML 注册的虚拟设备
    assert not (REPO_ROOT / "unilabos" / "registry" / "devices" / "virtual_device.yaml").exists()
    for name, entry in registry.items():
        class_config = entry["class"]
        assert class_config["supported_backends"] == ["hostlink", "ros2"], name
        assert class_config["module"].startswith("unilabos.devices.virtual."), name
        driver_class = default_manager.get_class(class_config["module"])
        # 虚拟设备本身就是模拟器：--test_mode 下同样真实执行
        assert driver_class.run_in_test_mode is True, name
        for action_name, mapping in class_config["action_value_mappings"].items():
            assert mapping["display_name"], (name, action_name)


@pytest.mark.parametrize(("device", "action_name"), sorted(COMPILER_ACTIONS))
def test_compiler_actions_keep_their_ros_types_and_goal_fields(registry, device, action_name) -> None:
    """ROS 2 下工作站按原生动作类型建客户端、按 goal 字段转消息；HostLink 下 goal 字段即驱动参数。"""

    import inspect

    mapping = registry[device]["class"]["action_value_mappings"][action_name]
    type_name = COMPILER_ACTIONS[(device, action_name)]
    assert mapping["type"] == type_name
    method = getattr(default_manager.get_class(registry[device]["class"]["module"]), action_name)
    parameters = inspect.signature(method).parameters
    for field_name in _ros_goal_fields(type_name):
        assert mapping["goal"].get(field_name) == field_name, (device, action_name, field_name)
        assert field_name in parameters, (device, action_name, field_name)
    goal_schema = mapping["schema"]["properties"]["goal"]
    assert goal_schema["title"] == f"{type_name}_Goal"
    assert set(goal_schema["properties"]) >= set(_ros_goal_fields(type_name))


# ---------------------------------------------------------------------------
# HostLink 真实执行：每台设备的每个动作，失败一律抛异常
# ---------------------------------------------------------------------------


def test_process_devices_run_their_actions(registry) -> None:
    with _running(
        _spec(registry, "centrifuge", "virtual_centrifuge", {"max_speed": 12000.0}),
        _spec(registry, "column", "virtual_column"),
        _spec(registry, "filter", "virtual_filter"),
        _spec(registry, "heatchill", "virtual_heatchill"),
        _spec(registry, "rotavap", "virtual_rotavap"),
        _spec(registry, "separator", "virtual_separator"),
        _spec(registry, "stirrer", "virtual_stirrer"),
    ) as runtime:
        call = runtime.call_action
        for device_id in ("centrifuge", "column", "filter", "heatchill", "rotavap", "separator", "stirrer"):
            assert call(device_id, "auto-initialize") is True

        centrifuge = _driver(runtime, "centrifuge")
        assert centrifuge.max_speed == 12000.0
        spun = call("centrifuge", "centrifuge", vessel={"id": "reactor"}, speed=3000.0, time="2 min", temp=25.0)
        assert spun == {"success": True, "vessel": "reactor", "speed": 3000.0, "time": 120.0, "temp": 25.0}
        assert (centrifuge.centrifuge_state, centrifuge.progress, centrifuge.current_speed) == ("Completed", 100.0, 0.0)
        with pytest.raises(ValueError, match="离心速度"):
            call("centrifuge", "centrifuge", vessel="reactor", speed=99999.0, time=1.0)
        with pytest.raises(ValueError, match="温度"):
            call("centrifuge", "centrifuge", vessel="reactor", speed=3000.0, time=1.0, temp=80.0)

        ran = call("column", "run_column", from_vessel="a", to_vessel="b", column="silica", rf="0.3")
        assert ran["success"] is True and ran["column"] == "silica"
        assert _driver(runtime, "column").column_state == "Completed"

        filtered = call("filter", "filter", vessel={"id": "filter"}, filtrate_vessel={"id": "flask"}, volume=10.0)
        assert filtered["filtered_volume"] == pytest.approx(10.0)
        with pytest.raises(ValueError, match="过滤体积"):
            call("filter", "filter", vessel="reactor", volume=9999.0)

        heatchill = _driver(runtime, "heatchill")
        heated = call("heatchill", "heat_chill", vessel={"id": "reactor"}, temp=60.0, time="2 min",
                      stir=True, stir_speed=300.0, purpose="test")
        assert heated["current_temp"] == 60.0 and heatchill.operation_mode == "Completed"
        # 干燥 / 氢化协议不带 stir 参数
        assert call("heatchill", "heat_chill", vessel={"id": "reactor"}, temp=40.0, time=30.0, purpose="dry")["success"]
        assert call("heatchill", "heat_chill_start", vessel={"id": "reactor"}, temp=50.0, purpose="test")["temp"] == 50.0
        assert (heatchill.operation_mode, heatchill.remaining_time, heatchill.target_temp) == ("Heating", -1.0, 50.0)
        assert call("heatchill", "heat_chill_stop", vessel={"id": "reactor"})["previous_target_temp"] == 50.0
        assert heatchill.operation_mode == "Stopped" and heatchill.current_temp == 25.0
        with pytest.raises(ValueError, match="温度"):
            call("heatchill", "heat_chill", temp=900.0, time=10.0)
        with pytest.raises(ValueError, match="时长"):
            call("heatchill", "heat_chill", temp=60.0, time=0)

        evaporated = call("rotavap", "evaporate", vessel="reactor", pressure=0.1, temp=60.0, time="3 min",
                          stir_speed=100.0, solvent="ethanol")
        # 易挥发溶剂自动降温降压
        assert (evaporated["temp"], evaporated["pressure"], evaporated["time"]) == (50.0, 0.05, 180.0)
        with pytest.raises(ValueError, match="旋转速度"):
            call("rotavap", "evaporate", vessel="reactor", stir_speed=1.0)

        separated = call("separator", "separate", purpose="wash", product_phase="top", from_vessel="a",
                         separation_vessel="separator", to_vessel="b", repeats=2, stir_time=1.0, settling_time=1.0)
        assert separated["repeats"] == 2 and separated["phase_separation"] is True
        with pytest.raises(ValueError, match="产物相"):
            call("separator", "separate", purpose="wash", product_phase="middle", from_vessel="a",
                 separation_vessel="separator", to_vessel="b")
        # 分液协议的底阀放液
        assert call("separator", "valve_open", command="time > 60")["drain_time"] == 60.0
        assert call("separator", "valve_open", command="delta > 0.05")["success"] is True
        with pytest.raises(ValueError, match="放液条件"):
            call("separator", "valve_open", command="open please")

        stirrer = _driver(runtime, "stirrer")
        stirred = call("stirrer", "stir", stir_time=60.0, stir_speed=300.0, settling_time="1 min")
        assert stirred["settling_time"] == 60.0 and stirrer.operation_mode == "Completed"
        # 搅拌协议给的是带单位的 time、stir_time 为 0
        assert call("stirrer", "stir", vessel={"id": "reactor"}, time="5 min", stir_time=0, stir_speed=300.0,
                    settling_time=0)["stir_time"] == 300.0
        assert call("stirrer", "start_stir", vessel={"id": "reactor"}, stir_speed=300.0, purpose="test")["success"]
        assert stirrer.is_stirring is True and stirrer.current_vessel == "reactor"
        call("stirrer", "stop_stir", vessel={"id": "reactor"})
        assert stirrer.is_stirring is False and stirrer.operation_mode == "Stopped"
        with pytest.raises(ValueError, match="搅拌速度"):
            call("stirrer", "start_stir", vessel="reactor", stir_speed=5000.0)

        for device_id in ("centrifuge", "column", "filter", "heatchill", "rotavap", "separator", "stirrer"):
            assert call(device_id, "auto-cleanup") is True


def test_fluidic_devices_run_their_actions(registry) -> None:
    with _running(
        _spec(registry, "pump", "virtual_transfer_pump", {"max_volume": 25.0, "transfer_rate": 10.0}),
        _spec(registry, "valve", "virtual_multiway_valve", {"positions": 8}),
        _spec(registry, "solenoid", "virtual_solenoid_valve", {"response_time": 0.0}),
    ) as runtime:
        call = runtime.call_action

        pump = _driver(runtime, "pump")
        # 设备图把参数平铺在 config 里；transfer_rate 作为默认柱塞速度
        assert (pump.transfer_rate, pump.max_volume, pump.max_velocity) == (10.0, 25.0, 10.0)
        assert call("pump", "auto-initialize") is True
        assert call("pump", "auto-set_max_velocity", velocity=100.0)["max_velocity"] == 50.0
        assert call("pump", "auto-is_empty") is True
        call("pump", "auto-aspirate", volume=5.0)
        call("pump", "auto-dispense", volume=2.0)
        assert pump.current_volume == pytest.approx(3.0)
        call("pump", "auto-pull_plunger", volume=1.0)
        call("pump", "auto-push_plunger", volume=1.0)
        call("pump", "auto-fill_syringe")
        assert call("pump", "auto-is_full") is True
        with pytest.raises(ValueError, match="剩余容量"):
            call("pump", "auto-aspirate", volume=1.0)
        call("pump", "auto-empty_syringe")
        with pytest.raises(ValueError, match="现有"):
            call("pump", "auto-dispense", volume=1.0)
        moved = call("pump", "set_position", position=3.0, max_velocity=2.0)
        assert moved["success"] is True and pump.position == pytest.approx(3.0)
        with pytest.raises(ValueError, match="超出注射器范围"):
            call("pump", "set_position", position=30.0)
        # 超过注射器容量时分次转移
        transferred = call("pump", "transfer", volume=60.0)
        assert transferred["strokes"] == 3 and pump.position == 0.0
        # 泵阀一体：协议编译器在泵直连容器时按连线端口名切换泵头通道
        assert call("pump", "set_valve_position", command="outlet")["valve_position"] == "outlet"
        assert pump.valve_position == "outlet"
        assert call("pump", "auto-stop_operation")["stopped"] is False
        assert call("pump", "auto-cleanup") is True

        valve = _driver(runtime, "valve")
        call("valve", "auto-set_to_port", port_number=3)
        assert valve.current_position == 3
        assert call("valve", "auto-is_at_port", port_number=3) is True
        assert call("valve", "auto-is_at_position", position=3) is True
        call("valve", "auto-set_to_pump_position")
        assert call("valve", "auto-is_at_pump_position") is True
        call("valve", "auto-switch_between_pump_and_port", port_number=2)
        assert valve.current_position == 2
        call("valve", "set_position", command="4")
        call("valve", "set_valve_position", command=5)
        assert valve.current_position == 5
        # 接泵的连线端口名就是 0 号位
        call("valve", "set_valve_position", command="transferpump")
        assert valve.current_position == 0
        call("valve", "auto-set_to_port", port_number=6)
        assert call("valve", "auto-close")["valve_state"] == "Closed"
        assert valve.current_position == 6 and "关闭" in valve.flow_path
        call("valve", "auto-open")
        call("valve", "auto-set_to_port", port_number=7)
        call("valve", "auto-reset")
        assert valve.current_position == 0 and valve.valve_state == "Ready"
        # 无效阀位是失败，不当成功返回一段错误文字
        with pytest.raises(ValueError, match="阀位"):
            call("valve", "set_valve_position", command="9")
        with pytest.raises(ValueError, match="无法识别"):
            call("valve", "set_valve_position", command="inlet")
        with pytest.raises(ValueError, match="端口号"):
            call("valve", "auto-set_to_port", port_number=0)

        solenoid = _driver(runtime, "solenoid")
        assert call("solenoid", "auto-initialize") is True
        assert call("solenoid", "open")["valve_position"] == "OPEN"
        assert call("solenoid", "auto-is_closed") is False
        assert call("solenoid", "close")["valve_position"] == "CLOSED"
        assert call("solenoid", "auto-toggle")["valve_position"] == "OPEN"
        assert call("solenoid", "set_status", string="OFF")["valve_position"] == "CLOSED"
        assert call("solenoid", "set_valve_position", command="OPEN")["valve_position"] == "OPEN"
        assert call("solenoid", "auto-reset")["valve_position"] == "CLOSED"
        assert solenoid.is_open is False and solenoid.valve_state == "Closed"
        with pytest.raises(ValueError, match="电磁阀命令"):
            call("solenoid", "set_valve_position", command="HALF")
        assert call("solenoid", "auto-cleanup") is True


def test_gas_devices_switch_on_and_off(registry) -> None:
    with _running(
        _spec(registry, "gas", "virtual_gas_source", {"gas_type": "argon"}),
        _spec(registry, "vacuum", "virtual_vacuum_pump"),
    ) as runtime:
        call = runtime.call_action
        assert _driver(runtime, "gas").gas_type == "argon"
        for device_id in ("gas", "vacuum"):
            assert call(device_id, "auto-initialize") is True
            assert call(device_id, "auto-is_closed") is True
            assert call(device_id, "open")["status"] == "ON"
            assert call(device_id, "auto-is_open") is True
            assert call(device_id, "close")["status"] == "OFF"
            # 协议编译器发 ON / OFF
            call(device_id, "set_status", string="ON")
            assert _driver(runtime, device_id).status == "ON"
            call(device_id, "set_status", string="OFF")
            assert call(device_id, "auto-is_open") is False
            with pytest.raises(ValueError, match="开关命令"):
                call(device_id, "set_status", string="HALF")
            assert call(device_id, "auto-cleanup") is True


def test_solid_dispenser_runs_its_actions(registry) -> None:
    with _running(_spec(registry, "dispenser", "virtual_solid_dispenser", {"max_capacity": 50.0})) as runtime:
        call = runtime.call_action
        dispenser = _driver(runtime, "dispenser")
        assert dispenser.max_capacity == 50.0
        assert call("dispenser", "auto-initialize") is True
        assert call("dispenser", "auto-parse_mass_string", mass_str="2.5 g") == 2.5
        assert call("dispenser", "auto-parse_mass_string", mass_str="500 mg") == 0.5
        assert call("dispenser", "auto-parse_mol_string", mol_str="16 mmol") == pytest.approx(0.016)
        with pytest.raises(ValueError, match="无法解析"):
            call("dispenser", "auto-parse_mass_string", mass_str="a lot")
        assert call("dispenser", "auto-find_solid_reagent_bottle", reagent_name="NaCl") == "solid_bottle_NaCl"
        added = call("dispenser", "add_solid", vessel={"id": "reactor"}, reagent="sodium_chloride", mass="1 g")
        assert (added["dispensed_amount"], added["basis"]) == (1.0, "mass")
        by_mol = call("dispenser", "add_solid", vessel="reactor", reagent="x", mol="0.1 mol")
        assert (by_mol["dispensed_amount"], by_mol["basis"]) == (pytest.approx(10.0), "mol")
        assert dispenser.total_operations == 2 and dispenser.current_reagent == "x"
        with pytest.raises(ValueError, match="单次最大加样量"):
            call("dispenser", "add_solid", vessel="reactor", reagent="x", mass="80 g")
        with pytest.raises(ValueError, match="目标容器"):
            call("dispenser", "add_solid", vessel="", reagent="x", mass="1 g")
        assert call("dispenser", "auto-cleanup") is True


# ---------------------------------------------------------------------------
# 时间倍率、进度反馈与取消
# ---------------------------------------------------------------------------


def test_time_scale_comes_from_the_device_config_or_the_class_default(registry, monkeypatch) -> None:
    from unilabos.devices.virtual.heating_platform import VirtualHeatingPlatform
    from unilabos.devices.virtual.virtual_stirrer import VirtualStirrer

    # 没有全局设置：倍率只按设备配，缺省是设备类自带的短倍率
    assert not hasattr(BasicConfig, "virtual_time_scale")
    monkeypatch.undo()
    assert VirtualStirrer().time_scale == DEFAULT_TIME_SCALE == 0.001
    assert VirtualHeatingPlatform().time_scale == DEFAULT_TIME_SCALE
    assert VirtualStirrer(time_scale=1).time_scale == 1.0
    assert VirtualStirrer(time_scale=0).time_scale == 0.0
    # 图节点写成 {"config": {...}}（旧注册表的形状）时内层同样生效
    nested = VirtualStirrer(config={"time_scale": 0.5, "max_speed": 800})
    assert (nested.time_scale, nested.max_speed) == (0.5, 800.0)
    with pytest.raises(ValueError, match="time_scale"):
        VirtualStirrer(time_scale=-1)

    # 2 分钟的模拟搅拌按 0.002 倍约 0.24 秒跑完，并分步发布进度反馈
    monkeypatch.setattr(BasicConfig, "test_mode", False)
    feedback: list[dict[str, Any]] = []
    context = ActionContext(feedback_callback=lambda _id, data: feedback.append(data))
    with _running(_spec(registry, "stirrer", "virtual_stirrer", {"time_scale": 0.002})) as runtime:
        started = time.monotonic()
        runtime.call_action("stirrer", "stir", stir_time=60.0, stir_speed=300.0, settling_time=60.0,
                            action_context=context)
        elapsed = time.monotonic() - started
    assert 0.2 <= elapsed < 2.0, elapsed
    progress = [item["progress"] for item in feedback]
    assert len(progress) >= 2 and progress == sorted(progress) and progress[-1] == 100.0
    assert feedback[0]["status"] == "Stirring" and feedback[-1]["remaining_time"] == 0.0


def test_long_actions_can_be_cancelled(registry) -> None:
    with _running(
        _spec(registry, "heatchill", "virtual_heatchill", {"time_scale": 1.0}),
        _spec(registry, "pump", "virtual_transfer_pump", {"time_scale": 1.0, "max_velocity": 0.5}),
        _spec(registry, "stirrer", "virtual_stirrer", {"time_scale": 1.0}),
    ) as runtime:
        context = ActionContext()
        errors: list[BaseException] = []

        def heat() -> None:
            try:
                runtime.call_action("heatchill", "heat_chill", temp=80.0, time="1 h", action_context=context)
            except BaseException as exc:  # noqa: BLE001 - 断言取消异常
                errors.append(exc)

        worker = threading.Thread(target=heat)
        worker.start()
        heatchill = _driver(runtime, "heatchill")
        deadline = time.monotonic() + 5
        # 等升温走过第一步再取消：温度停在半路
        while heatchill.current_temp <= 25.0 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert heatchill.operation_mode == "Heating"
        context.request_cancel()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], ActionCancelled)
        assert (heatchill.status, heatchill.operation_mode, heatchill.stir_speed) == ("Idle", "Stopped", 0.0)
        assert 25.0 < heatchill.current_temp < 80.0

        # 泵的停止动作取消正在进行的柱塞移动，柱塞停在中途
        pump = _driver(runtime, "pump")
        moving: list[BaseException] = []

        def aspirate() -> None:
            try:
                runtime.call_action("pump", "set_position", position=20.0, action_context=ActionContext())
            except BaseException as exc:  # noqa: BLE001
                moving.append(exc)

        worker = threading.Thread(target=aspirate)
        worker.start()
        deadline = time.monotonic() + 5
        while pump.status != "Aspirating" and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.2)
        assert runtime.call_action("pump", "auto-stop_operation")["stopped"] is True
        worker.join(timeout=5)
        assert len(moving) == 1 and isinstance(moving[0], ActionCancelled)
        assert pump.status == "Idle" and 0.0 < pump.position < 20.0

        # 按真实时长跑的长动作（每个进度步 3 分钟）也在取消检查间隔内停下
        stirrer = _driver(runtime, "stirrer")
        long_context = ActionContext()
        stopped: list[BaseException] = []

        def stir() -> None:
            try:
                runtime.call_action("stirrer", "stir", stir_time=36000.0, stir_speed=300.0, settling_time=0,
                                    action_context=long_context)
            except BaseException as exc:  # noqa: BLE001
                stopped.append(exc)

        worker = threading.Thread(target=stir)
        worker.start()
        deadline = time.monotonic() + 5
        while not stirrer.is_stirring and time.monotonic() < deadline:
            time.sleep(0.02)
        cancelled_at = time.monotonic()
        long_context.request_cancel()
        worker.join(timeout=5)
        assert time.monotonic() - cancelled_at < 1.0
        assert len(stopped) == 1 and isinstance(stopped[0], ActionCancelled)
        assert (stirrer.status, stirrer.operation_mode, stirrer.is_stirring) == ("Idle", "Stopped", False)


# ---------------------------------------------------------------------------
# 容器：标签读 display_name，内容物走 PLR 体积跟踪器
# ---------------------------------------------------------------------------


class _Station(WorkstationBase):
    """只承载容器的工作站：子设备共用它的资源跟踪器。"""

    def __init__(self, **_kwargs: Any) -> None:
        super().__init__(deck=None)


def _container(node_id: str, parent_uuid: str, substances: list[list[Any]], max_volume: float = 50000.0):
    node_uuid = str(uuid.uuid4())
    return {
        "id": node_id, "uuid": node_uuid, "name": node_uuid, "display_name": f"{node_id} 瓶",
        "type": "container", "class": "RegularContainer", "template_name": "container",
        "parent_uuid": parent_uuid,
        "config": {"type": "RegularContainer", "max_volume": max_volume, "name": node_id},
        "data": {}, "extra": {}, "substances": substances, "pose": {"position": {"x": 0, "y": 0, "z": 0}},
    }


def test_devices_read_labels_and_write_contents_through_the_volume_tracker(registry, monkeypatch) -> None:
    from unilabos.resources.resource_tracker import ResourceTreeSet

    station_uuid = str(uuid.uuid4())
    source = _container("flask_a", station_uuid, [["ethanol", 8000.0, "ul"], ["water", 2000.0, "ul"]])
    target = _container("flask_b", station_uuid, [])
    vial = _container("vial", station_uuid, [], max_volume=1000.0)
    tree = ResourceTreeSet.from_raw_dict_list(
        [
            {"id": "station", "uuid": station_uuid, "name": station_uuid, "type": "device",
             "class": "workstation", "template_name": "workstation", "config": {}, "data": {}, "extra": {}},
            source,
            target,
            vial,
        ]
    )
    # 协议编译器按设备图节点 id 引用容器：物理拓扑图给出 uuid 与 display_name
    graph = nx.Graph()
    for node in (source, target, vial):
        graph.add_node(node["id"], uuid=node["uuid"], display_name=node["display_name"])
    monkeypatch.setattr(workstation_protocol, "_physical_setup_graph", graph)

    # 工作站的子设备共用工作站的资源跟踪器（与示例协议站一致）
    station = HostLinkDriverSpec(
        device_id="station", driver_class=_Station, config={}, device_config=tree.trees[0].root_node
    )

    def spec(device_id: str, name: str) -> HostLinkDriverSpec:
        return _spec(registry, device_id, name, parent_device_id="station")

    with _running(
        station,
        spec("pump", "virtual_transfer_pump"),
        spec("dispenser", "virtual_solid_dispenser"),
        spec("rotavap", "virtual_rotavap"),
        spec("column", "virtual_column"),
    ) as runtime:
        call = runtime.call_action
        tracker = runtime.devices["pump"].resource_tracker
        assert tracker is runtime.devices["rotavap"].resource_tracker
        flask_a = tracker.uuid_to_resources[source["uuid"]]
        flask_b = tracker.uuid_to_resources[target["uuid"]]

        moved = call("pump", "transfer", volume=5.0, from_vessel={"id": "flask_a"}, to_vessel={"uuid": target["uuid"]})
        assert (moved["from_vessel"], moved["to_vessel"]) == ("flask_a 瓶", "flask_b 瓶")
        assert flask_a.tracker.get_used_volume() == pytest.approx(5000.0)
        assert dict((name, volume) for name, volume, _unit in flask_b.tracker.get_liquids()) == {
            "ethanol": pytest.approx(4000.0), "water": pytest.approx(1000.0)
        }

        # 控温协议传的是再包一层的 {"id": {"id": ...}}
        added = call("dispenser", "add_solid", vessel={"id": {"id": "flask_b"}}, reagent="NaCl", mass="2 g")
        assert added["recorded_in_vessel"] is True and added["vessel"] == "flask_b 瓶"
        assert flask_b.tracker.get_solids() == [("NaCl", 2_000_000.0, "ug")]

        evaporated = call("rotavap", "evaporate", vessel={"id": "flask_b"}, solvent="ethanol", time=60)
        assert evaporated["evaporated_volume"] == pytest.approx(4.0)
        assert [name for name, _volume, _unit in flask_b.tracker.get_liquids()] == ["water"]
        assert flask_b.tracker.get_solids() == [("NaCl", 2_000_000.0, "ug")]

        # 收集瓶放不下的液体留在源容器（不凭空消失）
        collected = call("column", "run_column", from_vessel={"id": "flask_a"}, to_vessel={"id": "vial"}, column="c")
        assert collected["collected_volume"] == pytest.approx(1.0)
        assert flask_a.tracker.get_used_volume() == pytest.approx(4000.0)
        assert tracker.uuid_to_resources[vial["uuid"]].tracker.get_used_volume() == pytest.approx(1000.0)

        # 不在本设备可见范围内的容器只记标签，动作照常完成
        assert call("pump", "transfer", volume=1.0, from_vessel="elsewhere", to_vessel="nowhere")["moved_contents"] == []


# ---------------------------------------------------------------------------
# 示例图与代表协议
# ---------------------------------------------------------------------------


def _virtual_graphs() -> list[Path]:
    graphs = []
    for path in sorted(EXPERIMENTS.rglob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        nodes = payload.get("nodes") if isinstance(payload, dict) else None
        if not nodes:
            continue
        # comprehensive_slim 的根节点模板 workstation.example 不在注册表里，与 backend 无关，单独说明
        if path.name == "comprehensive_slim.json":
            continue
        if any("virtual" in str(node.get("class") or node.get("template_name") or "") for node in nodes):
            graphs.append(path)
    return graphs


def _load_graph(graph: Path, tmp_path: Path, monkeypatch) -> Any:
    from unilabos.app.main import _load_startup_graph

    monkeypatch.setattr(workstation_protocol, "_physical_setup_graph", None)
    monkeypatch.setattr(BasicConfig, "machine_name", "lab-pc")
    graph_file = tmp_path / graph.name
    shutil.copy(graph, graph_file)
    return _load_startup_graph(
        {"server_database_root": str(tmp_path), "graph": str(graph_file)}, str(tmp_path), is_slave=False
    )


@pytest.mark.parametrize("graph", _virtual_graphs(), ids=lambda path: path.relative_to(EXPERIMENTS).as_posix())
def test_example_graph_materials_build_as_their_template_class(graph: Path, tmp_path, monkeypatch, registry) -> None:
    """示例图经 legacy 适配与 Graph Authority 后，每个物料都能建成 PLR，且类名与模板一致；
    每台虚拟设备都能按图里的 config 构造出来。"""

    from unilabos.resources.resource_tracker import ResourceTreeInstance, ResourceTreeSet

    loaded = _load_graph(graph, tmp_path, monkeypatch)
    materials = [
        node for node in loaded.resource_tree_set.all_nodes
        if node.res_content.type != "device"
        and (node.res_content.parent is None or node.res_content.parent.type == "device")
    ]
    for node in materials:
        (resource,) = ResourceTreeSet([ResourceTreeInstance(node)]).to_plr_resources()
        assert node.res_content.klass == type(resource).__name__, node.res_content.id
        if node.res_content.type == "container":
            assert node.res_content.template_name == "container", node.res_content.id
    for node in loaded.resource_tree_set.all_nodes:
        name = node.res_content.template_name
        # 加热平台启动时向物料权威建物料，单独的用例覆盖
        if node.res_content.type == "device" and name in registry and name != "virtual_heating_platform":
            with _running(_spec(registry, node.res_content.id, name, dict(node.res_content.config or {}))):
                pass


def _vessel(node_id: str) -> dict[str, str]:
    return {"id": node_id}


#: 每张工作站示例图的代表协议（短参数）；容器按 {"id": ...} 传，与协议模型一致。
EXAMPLE_PROTOCOLS: dict[str, tuple[str, dict[str, Any]]] = {
    "Protocol_Test_Station/add_protocol_test_station.json": ("AddProtocol", {
        "vessel": _vessel("main_reactor"), "reagent": "DMF", "volume": 5.0, "mass": 0.0, "amount": "",
        "time": 0.0, "stir": False, "stir_speed": 0.0, "viscous": False, "purpose": "test"}),
    "Protocol_Test_Station/centrifuge_protocol_test_station.json": ("CentrifugeProtocol", {
        "vessel": _vessel("centrifuge_tube"), "speed": 3000.0, "time": 5.0, "temp": 25.0}),
    "Protocol_Test_Station/clean_vessel_protocol_test_station.json": ("CleanVesselProtocol", {
        "vessel": _vessel("main_reactor"), "solvent": "water", "volume": 5.0, "temp": 25.0, "repeats": 1}),
    "Protocol_Test_Station/dual_valve_pump_test_station.json": ("PumpTransferProtocol", {
        "from_vessel": _vessel("flask_DMF"), "to_vessel": _vessel("main_reactor"), "volume": 5.0}),
    "Protocol_Test_Station/evacuateandrefill_test_station.json": ("EvacuateAndRefillProtocol", {
        "vessel": _vessel("main_reactor"), "gas": "nitrogen"}),
    "Protocol_Test_Station/evaporate_protocol_test_station.json": ("EvaporateProtocol", {
        "vessel": _vessel("rotavap_flask"), "pressure": 0.1, "temp": 60.0, "time": 5.0,
        "stir_speed": 100.0, "solvent": "ethanol"}),
    "Protocol_Test_Station/filter_protocol_test_station.json": ("FilterProtocol", {
        "vessel": _vessel("filter_vessel"), "filtrate_vessel": _vessel("filtrate_vessel")}),
    "Protocol_Test_Station/heatchill_protocol_test_station.json": ("HeatChillProtocol", {
        "vessel": _vessel("main_reactor"), "temp": 60.0, "time": 5.0, "stir": True, "stir_speed": 300.0,
        "purpose": "test"}),
    "Protocol_Test_Station/pumptransfer_filterthrough_test_station.json": ("FilterThroughProtocol", {
        "from_vessel": _vessel("reaction_mixture"), "to_vessel": _vessel("collection_bottle_1"),
        "filter_through": "celite"}),
    "Protocol_Test_Station/pumptransfer_test_station.json": ("PumpTransferProtocol", {
        "from_vessel": _vessel("flask_DMF"), "to_vessel": _vessel("main_reactor"), "volume": 5.0}),
    "Protocol_Test_Station/run_column_protocol_test_station.json": ("RunColumnProtocol", {
        "from_vessel": _vessel("flask_sample"), "to_vessel": _vessel("collection_flask_1"), "column": "column_1"}),
    "Protocol_Test_Station/simple_stir_heatchill_test_station.json": ("StirProtocol", {
        "vessel": _vessel("main_reactor"), "time": "5 s"}),
    "comprehensive_protocol/comprehensive_station.json": ("PumpTransferProtocol", {
        "from_vessel": _vessel("reagent_bottle_1"), "to_vessel": _vessel("main_reactor"), "volume": 5.0}),
    "mock_protocol/addteststation.json": ("AddProtocol", {
        "vessel": _vessel("reactor"), "reagent": "reagent1", "volume": 5.0, "mass": 0.0, "amount": "",
        "time": 0.0, "stir": False, "stir_speed": 0.0, "viscous": False, "purpose": "test"}),
    "mock_protocol/centrifugeteststation.json": ("PumpTransferProtocol", {
        "from_vessel": _vessel("flask_1"), "to_vessel": _vessel("reactor"), "volume": 5.0}),
    "mock_protocol/cleanvesselteststation.json": ("CleanVesselProtocol", {
        "vessel": _vessel("reactor"), "solvent": "water", "volume": 5.0, "temp": 25.0, "repeats": 1}),
    "mock_protocol/dissolveteststation.json": ("DissolveProtocol", {
        "vessel": _vessel("reactor"), "solvent": "water", "volume": 5.0, "temp": 25.0, "time": 5.0,
        "stir_speed": 300.0}),
    "mock_protocol/filterteststation.json": ("FilterProtocol", {
        "vessel": _vessel("reactor"), "filtrate_vessel": _vessel("flask_filtrate")}),
    "mock_protocol/filterthroughteststation.json": ("FilterThroughProtocol", {
        "from_vessel": _vessel("reactor"), "to_vessel": _vessel("collection_flask"),
        "filter_through": "flask_celite"}),
    "mock_protocol/heatchillteststation.json": ("HeatChillProtocol", {
        "vessel": _vessel("reactor"), "temp": 60.0, "time": 5.0, "stir": False, "purpose": "test"}),
    "mock_protocol/runcolumnteststation.json": ("RunColumnProtocol", {
        "from_vessel": _vessel("flask_sample"), "to_vessel": _vessel("collection_flask_1"), "column": "column_1"}),
    "mock_protocol/stirteststation.json": ("StirProtocol", {"vessel": _vessel("reactor"), "time": "5 s"}),
    "mock_protocol/transferteststation.json": ("TransferProtocol", {
        "from_vessel": _vessel("flask_source1"), "to_vessel": _vessel("flask_target1"), "volume": 5.0}),
    "mock_protocol/washsolidteststation.json": ("WashSolidProtocol", {
        "vessel": _vessel("reactor"), "solvent": "ethanol", "volume": 5.0,
        "filtrate_vessel": _vessel("filtrate_flask"), "repeats": 1}),
    "mock_reactor.json": ("EvacuateAndRefillProtocol", {"vessel": _vessel("reactor"), "gas": "nitrogen"}),
}


def _flatten(steps: list[Any]) -> list[dict[str, Any]]:
    return [step for item in steps for step in (item if isinstance(item, list) else [item])]


@pytest.mark.parametrize("graph_name", sorted(EXAMPLE_PROTOCOLS))
def test_example_station_protocol_steps_run_on_the_virtual_devices(
    graph_name: str, tmp_path, monkeypatch, registry
) -> None:
    """示例协议站的代表协议按图编译，每一步都是图里某台虚拟设备注册过的动作（不回调工作站自身），
    参数都是驱动接受的参数，并且在按图配置构造的设备上真实执行成功。"""

    import inspect

    from unilabos.experiments import models as protocol_models
    from unilabos.experiments.compile import action_protocol_generators

    loaded = _load_graph(EXPERIMENTS / graph_name, tmp_path, monkeypatch)
    graph = loaded.physical_setup_graph
    protocol_name, kwargs = EXAMPLE_PROTOCOLS[graph_name]
    station = next(node for node, data in graph.nodes(data=True) if data.get("class") == "workstation")
    assert protocol_name in graph.nodes[station]["config"]["protocol_type"]

    generator = action_protocol_generators[getattr(protocol_models, protocol_name)]
    steps = list(generator(G=graph, **kwargs))
    assert steps
    flat = [step for step in _flatten(steps) if step["action_name"] not in ("wait", "log_message")]
    devices = {node for node, data in graph.nodes(data=True) if data.get("type") == "device"} - {station}
    used: dict[str, str] = {}
    for step in flat:
        assert step["device_id"] in devices, step
        template = graph.nodes[step["device_id"]]["template_name"]
        mapping = registry[template]["class"]["action_value_mappings"]
        assert step["action_name"] in mapping, (template, step)
        method = getattr(
            default_manager.get_class(registry[template]["class"]["module"]),
            step["action_name"].removeprefix("auto-"),
        )
        assert set(step["action_kwargs"]) <= set(inspect.signature(method).parameters), (template, step)
        used[step["device_id"]] = template

    # 协议在工作站进程里执行：设备看得到物理拓扑图（容器标签）
    monkeypatch.setattr(workstation_protocol, "_physical_setup_graph", graph)
    specs = [
        _spec(registry, device_id, template, dict(graph.nodes[device_id].get("config") or {}))
        for device_id, template in sorted(used.items())
    ]
    with _running(*specs) as runtime:
        for step in flat:
            result = runtime.call_action(step["device_id"], step["action_name"], **step["action_kwargs"])
            assert result is not False and (not isinstance(result, dict) or result.get("success", True)), step




@pytest.mark.slow
def test_protocol_station_example_runs_on_a_real_hostlink_backend(tmp_path: Path) -> None:
    """起真实 ``unilab``（HostLink，非 test_mode），按示例图跑代表协议与单设备动作：默认时间倍率下秒级完成。"""

    from tests.app.test_process_lifetime import _Unilab

    unilab = _Unilab(
        tmp_path,
        "station",
        ["-g", str(EXPERIMENTS / "Protocol_Test_Station" / "simple_stir_heatchill_test_station.json")],
        root=tmp_path / "db",
        builtin_devices=True,
    )

    def request(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        import urllib.request

        req = urllib.request.Request(
            f"http://127.0.0.1:{unilab.port}/api/v1{path}",
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Content-Type": "application/json"} if body is not None else {},
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            payload = json.load(response)
        assert payload["code"] == 0, payload
        return payload["data"]

    def run(device_id: str, action: str, action_type: str, args: dict[str, Any]) -> tuple[Any, float]:
        started = time.monotonic()
        job = request("POST", "/job/add", {"device_id": device_id, "action": action,
                                           "action_type": action_type, "action_args": args})["jobId"]

        def finished() -> dict[str, Any] | None:
            status = request("GET", f"/job/{job}/status")
            return status if status["status"] in (4, 5, 6) else None

        status = unilab.wait_for(finished, f"{device_id}/{action} 结束", timeout=60)
        assert status["status"] == 4, (status, unilab.text()[-4000:])
        return status["result"]["return_value"], time.monotonic() - started

    try:
        unilab.wait_for(lambda: "Host 已完全就绪" in unilab.text(), "Host 就绪")
        payload = request("GET", "/graphs/live/payload")
        # 子节点的 id 带父级前缀（Station/main_reactor）；协议的物料入参带 uuid，id 留给编译器查拓扑
        reactor = next(node for node in payload["nodes"] if str(node["id"]).split("/")[-1] == "main_reactor")
        vessel = {"id": "main_reactor", "uuid": reactor["uuid"]}

        # 半小时的模拟控温按默认倍率约 1.8 秒
        heated, elapsed = run("heatchill_1", "heat_chill", "HeatChill",
                              {"vessel": {"id": "main_reactor"}, "temp": 60.0, "time": "30 min", "purpose": "smoke"})
        assert heated["success"] is True and heated["current_temp"] == 60.0
        assert elapsed < 15, elapsed

        station = "SimpleStirHeatChillTestStation"
        result, _ = run(station, "HeatChillProtocol", "HeatChillProtocol",
                        {"vessel": vessel, "temp": 40.0, "time": "10 min", "stir": False, "purpose": "smoke"})
        assert result["protocol_name"] == "HeatChillProtocol"
        result, _ = run(station, "StirProtocol", "StirProtocol", {"vessel": vessel, "time": "5 min"})
        assert result["protocol_name"] == "StirProtocol"
        assert re.search(r"stir", json.dumps(result["step_results"]))
    finally:
        unilab.close()
