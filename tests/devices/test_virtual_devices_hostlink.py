"""内置虚拟设备在 HostLink 上真实执行（不走 ``--test_mode`` 占位）。

驱动由 HostLink 本地运行时按注册表条目实例化、启动，动作经运行时的同一条调用链
（参数映射、ResourceSlot 解析、样品映射注入）执行；只把 ``node.sleep`` 的模拟耗时
压到 0，驱动逻辑与状态变化照常发生。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest
import yaml

from unilabos.backend.hostlink.local_runtime import (
    HostLinkDeviceNode,
    HostLinkDriverSpec,
    HostLinkLocalRuntime,
)
from unilabos.config.config import BasicConfig
from unilabos.utils.import_manager import default_manager

REPO_ROOT = Path(__file__).resolve().parents[2]
VIRTUAL_YAML = REPO_ROOT / "unilabos" / "registry" / "devices" / "virtual_device.yaml"
EXPERIMENTS = REPO_ROOT / "unilabos" / "test" / "experiments"


@pytest.fixture(scope="module")
def registry() -> dict[str, Any]:
    return yaml.safe_load(VIRTUAL_YAML.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _real_execution_without_waiting(monkeypatch):
    async def no_wait(self, rel_time: float, callback_group: Any = None) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(BasicConfig, "test_mode", False)
    monkeypatch.setattr(HostLinkDeviceNode, "sleep", no_wait)


def _yaml_spec(
    registry: dict[str, Any], device_id: str, name: str, config: dict[str, Any] | None = None
) -> HostLinkDriverSpec:
    class_config = registry[name]["class"]
    assert "hostlink" in class_config["supported_backends"], name
    mappings = class_config["action_value_mappings"]
    return HostLinkDriverSpec(
        device_id=device_id,
        driver_class=default_manager.get_class(class_config["module"]),
        config=dict(config or {}),
        registry_name=name,
        action_names=tuple(mappings),
        action_value_mappings=mappings,
        status_names=tuple(class_config.get("status_types") or {}),
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


def test_every_yaml_virtual_device_declares_both_backends(registry) -> None:
    for name, entry in registry.items():
        assert entry["class"]["module"].startswith("unilabos.devices.virtual."), name
        assert entry["class"]["supported_backends"] == ["hostlink", "ros2"], name


def test_process_devices_run_their_actions(registry) -> None:
    with _running(
        _yaml_spec(registry, "centrifuge", "virtual_centrifuge", {"max_speed": 12000.0}),
        _yaml_spec(registry, "column", "virtual_column"),
        _yaml_spec(registry, "filter", "virtual_filter"),
        _yaml_spec(registry, "heatchill", "virtual_heatchill"),
        _yaml_spec(registry, "rotavap", "virtual_rotavap"),
        _yaml_spec(registry, "separator", "virtual_separator"),
        _yaml_spec(registry, "stirrer", "virtual_stirrer"),
    ) as runtime:
        call = runtime.call_action
        for device_id in ("centrifuge", "column", "filter", "heatchill", "rotavap", "separator", "stirrer"):
            assert call(device_id, "auto-initialize") is True

        assert call("centrifuge", "centrifuge", vessel="reactor", speed=3000.0, time=2.0, temp=25.0) is True
        centrifuge = _driver(runtime, "centrifuge")
        assert centrifuge.data["centrifuge_state"] == "Completed" and centrifuge.max_speed == 12000.0
        # 超出量程是失败而不是成功
        assert call("centrifuge", "centrifuge", vessel="reactor", speed=99999.0, time=1.0) is False

        assert call("column", "run_column", from_vessel="a", to_vessel="b", column="column") is True
        assert _driver(runtime, "column").data["progress"] == 100.0

        assert call("filter", "filter", vessel="reactor", filtrate_vessel="flask", volume=10.0) is True
        assert call("heatchill", "heat_chill", vessel={"id": "reactor"}, temp=60.0, time=2.0,
                    stir=True, stir_speed=300.0, purpose="test") is True
        assert call("heatchill", "heat_chill_start", vessel={"id": "reactor"}, temp=50.0, purpose="test") is True
        assert _driver(runtime, "heatchill").operation_mode != "Idle"
        assert call("heatchill", "heat_chill_stop", vessel={"id": "reactor"}) is True

        assert call("rotavap", "evaporate", vessel="reactor", pressure=0.1, temp=60.0, time=2.0,
                    stir_speed=100.0, solvent="ethanol") is True

        assert call("separator", "separate", purpose="wash", product_phase="top", from_vessel="a",
                    separation_vessel="separator", to_vessel="b", stir_time=1.0, settling_time=1.0) is True
        assert call("separator", "separate", purpose="wash", product_phase="middle", from_vessel="a",
                    separation_vessel="separator", to_vessel="b") is False

        assert call("stirrer", "stir", stir_time=2.0, stir_speed=300.0, settling_time=1.0) is True
        assert call("stirrer", "start_stir", vessel="reactor", stir_speed=300.0, purpose="test") is True
        assert _driver(runtime, "stirrer").is_stirring is True
        assert call("stirrer", "stop_stir", vessel="reactor") is True
        assert _driver(runtime, "stirrer").is_stirring is False

        for device_id in ("centrifuge", "column", "filter", "heatchill", "rotavap", "separator", "stirrer"):
            assert call(device_id, "auto-cleanup") is True


def test_fluidic_devices_run_their_actions(registry) -> None:
    with _running(
        _yaml_spec(registry, "pump", "virtual_transfer_pump", {"max_volume": 25.0, "transfer_rate": 10.0}),
        _yaml_spec(registry, "valve", "virtual_multiway_valve", {"positions": 8}),
        _yaml_spec(registry, "solenoid", "virtual_solenoid_valve", {"response_time": 0.0}),
    ) as runtime:
        call = runtime.call_action

        pump = _driver(runtime, "pump")
        # 设备图把参数写在 config 里（HostLink 交给驱动 config），transfer_rate 不能丢
        assert pump.transfer_rate == 10.0 and pump.max_volume == 25.0
        assert call("pump", "auto-initialize") is True
        call("pump", "auto-set_max_velocity", velocity=10.0)
        assert pump.max_velocity == 10.0
        assert call("pump", "auto-is_empty") is True
        call("pump", "auto-aspirate", volume=5.0)
        call("pump", "auto-dispense", volume=2.0)
        assert pump.current_volume == pytest.approx(3.0)
        call("pump", "auto-pull_plunger", volume=1.0)
        call("pump", "auto-push_plunger", volume=1.0)
        call("pump", "auto-fill_syringe")
        assert call("pump", "auto-is_full") is True
        call("pump", "auto-empty_syringe")
        result = call("pump", "set_position", position=3.0)
        assert result["success"] is True and pump.position == pytest.approx(3.0)
        call("pump", "transfer", volume=2.0)
        # 泵阀一体：协议编译器在泵直连容器时按连线端口名切换泵头通道
        assert "outlet" in call("pump", "set_valve_position", command="outlet")
        assert pump.valve_position == "outlet"
        call("pump", "auto-stop_operation")
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
        call("valve", "set_valve_position", command="5")
        assert valve.current_position == 5
        call("valve", "auto-open")
        call("valve", "auto-reset")
        assert valve.current_position == 0
        assert "关闭" in call("valve", "auto-close")
        # 无效阀位是失败，不再当成功返回一段错误文字
        with pytest.raises(ValueError):
            call("valve", "set_valve_position", command="9")

        solenoid = _driver(runtime, "solenoid")
        assert call("solenoid", "auto-initialize") is True
        assert call("solenoid", "open")["valve_position"] == "OPEN"
        assert call("solenoid", "auto-is_closed") is False
        assert call("solenoid", "close")["valve_position"] == "CLOSED"
        assert call("solenoid", "auto-toggle")["valve_position"] == "OPEN"
        assert call("solenoid", "set_status", string="OFF")["valve_position"] == "CLOSED"
        assert call("solenoid", "set_valve_position", command="OPEN")["valve_position"] == "OPEN"
        assert call("solenoid", "auto-reset")["valve_position"] == "CLOSED"
        assert solenoid.is_open is False
        assert call("solenoid", "auto-cleanup") is True


def test_gas_devices_report_open_and_closed_as_bool(registry, monkeypatch) -> None:
    from unilabos.devices.virtual import virtual_gas_source, virtual_vacuum_pump

    # set_status 模拟 5 秒切换：只跳过等待
    fast_time = SimpleNamespace(sleep=lambda _seconds: None, time=time.time)
    monkeypatch.setattr(virtual_gas_source, "time", fast_time)
    monkeypatch.setattr(virtual_vacuum_pump, "time", fast_time)

    with _running(
        _yaml_spec(registry, "gas", "virtual_gas_source"),
        _yaml_spec(registry, "vacuum", "virtual_vacuum_pump"),
    ) as runtime:
        for device_id in ("gas", "vacuum"):
            call = runtime.call_action
            assert call(device_id, "auto-initialize") is True
            call(device_id, "close")
            assert call(device_id, "auto-is_closed") is True
            assert call(device_id, "auto-is_open") is False
            call(device_id, "open")
            assert call(device_id, "auto-is_open") is True
            # 协议编译器发 ON / OFF
            call(device_id, "set_status", string="OFF")
            assert call(device_id, "auto-is_closed") is True
            call(device_id, "set_status", string="ON")
            assert call(device_id, "auto-is_open") is True
            assert call(device_id, "auto-cleanup") is True


def test_solid_dispenser_and_sample_demo_run_their_actions(registry) -> None:
    with _running(
        _yaml_spec(registry, "dispenser", "virtual_solid_dispenser", {"max_capacity": 50.0}),
        _yaml_spec(registry, "samples", "virtual_sample_demo"),
    ) as runtime:
        call = runtime.call_action
        assert _driver(runtime, "dispenser").max_capacity == 50.0
        assert call("dispenser", "auto-initialize") is True
        assert call("dispenser", "auto-parse_mass_string", mass_str="2.5 g") == 2.5
        assert call("dispenser", "auto-parse_mol_string", mol_str="0.1 mol") == 0.1
        assert call("dispenser", "auto-find_solid_reagent_bottle", reagent_name="sodium_chloride")
        added = call("dispenser", "add_solid", vessel="reactor", reagent="sodium_chloride", mass="1 g")
        assert added["success"] is True and added["dispensed_amount"] == pytest.approx(1.0)
        assert call("dispenser", "auto-cleanup") is True

        measured = call("samples", "measure_samples", concentrations=[1.0, 2.0])
        assert len(measured["absorbance"]) == 2
        split = call("samples", "split_and_measure", volumes=[3.0, 6.0], split_count=2)
        assert split["unilabos_samples"] == [0, 0, 1, 1]
        analyzed = call("samples", "analyze_readings", readings=[0.5, 0.2], samples=[0, 1])
        assert analyzed["passed"] == [True, False]


def _workbench_spec(device_id: str, config: dict[str, Any]) -> HostLinkDriverSpec:
    from unilabos.devices.virtual.workbench import VirtualWorkbench

    return HostLinkDriverSpec(
        device_id=device_id,
        driver_class=VirtualWorkbench,
        config=config,
        registry_name="virtual_workbench",
        action_names=(
            "auto-prepare_materials",
            "auto-move_to_heating_station",
            "auto-start_heating",
            "auto-move_to_output",
            "call_peer",
        ),
    )


def test_workbench_runs_its_flow_from_the_example_graph_config(registry) -> None:
    """virtual_bench.json 的嵌套 config 到达驱动；四步流程不带样品也能跑；失败要抛出。"""

    bench = json.loads((EXPERIMENTS / "virtual_bench.json").read_text(encoding="utf-8"))
    node_config = next(node for node in bench["nodes"] if node["id"] == "workbench_1")["config"]
    # 示例图的耗时配置原样到达驱动，再缩短以便测试
    with _running(_workbench_spec("probe", node_config)) as runtime:
        assert _driver(runtime, "probe").HEATING_TIME == 10.0
        assert _driver(runtime, "probe").ARM_OPERATION_TIME == 3.0

    fast = {"config": {"arm_operation_time": 0.0, "heating_time": 0.05, "num_heating_stations": 2}}
    with _running(_workbench_spec("workbench_1", fast), _yaml_spec(registry, "stirrer_1", "virtual_stirrer")) as runtime:
        call = runtime.call_action
        prepared = call("workbench_1", "auto-prepare_materials", count=2)
        assert (prepared["material_1"], prepared["material_2"], prepared["unilabos_samples"]) == (1, 2, [])
        moved = call("workbench_1", "auto-move_to_heating_station", material_number=1)
        assert moved["station_id"] == 1
        heated = call("workbench_1", "auto-start_heating", station_id=1, material_number=1)
        assert heated["message"] == "加热台1加热完成"
        done = call("workbench_1", "auto-move_to_output", station_id=1, material_number=1)
        assert done["output_position"] == "C1"

        with pytest.raises(RuntimeError, match="没有物料"):
            call("workbench_1", "auto-move_to_output", station_id=2, material_number=2)
        with pytest.raises(ValueError, match="无效的加热台ID"):
            call("workbench_1", "auto-start_heating", station_id=9, material_number=1)

        # 跨设备调用：调另一台设备成功，回调自己被拒绝
        peer = call(
            "workbench_1",
            "call_peer",
            target_device="stirrer_1",
            function_name="stir",
            function_args=json.dumps({"stir_time": 1, "stir_speed": 200, "settling_time": 0}),
        )
        assert peer == {"success": True, "target_device": "stirrer_1", "function_name": "stir", "return_value": True}
        with pytest.raises(ValueError, match="不能回调当前设备自身"):
            call("workbench_1", "call_peer", target_device="workbench_1", function_name="auto-prepare_materials")


def _container(node_id: str, parent_uuid: str, substances: list[list[Any]]) -> dict[str, Any]:
    node_uuid = str(uuid.uuid4())
    return {
        "id": node_id, "uuid": node_uuid, "name": node_uuid, "display_name": node_id,
        "type": "container", "class": "RegularContainer", "template_name": "container",
        "parent_uuid": parent_uuid,
        "config": {"type": "RegularContainer", "max_volume": 100.0, "name": node_id},
        "data": {}, "extra": {}, "substances": substances, "pose": {"position": {"x": 0, "y": 0, "z": 0}},
    }


def test_test_action_writes_contents_through_the_volume_tracker() -> None:
    from unilabos.devices.virtual.lctest_action import TestActionDevice
    from unilabos.registry.decorators import get_device_meta
    from unilabos.resources.resource_tracker import ResourceTreeSet

    assert get_device_meta(TestActionDevice, "test_action")["supported_backends"] == ["hostlink", "ros2"]
    device_uuid = str(uuid.uuid4())
    first = _container("echo_sample_1", device_uuid, [["echo_liquid_1", 10.0, "ul"]])
    second = _container("echo_sample_2", device_uuid, [["echo_liquid_2", 20.0, "ul"]])
    tree = ResourceTreeSet.from_raw_dict_list(
        [
            {"id": "test_action", "uuid": device_uuid, "name": device_uuid, "type": "device",
             "class": "test_action", "template_name": "test_action", "config": {}, "data": {}, "extra": {}},
            first,
            second,
        ]
    )
    spec = HostLinkDriverSpec(
        device_id="test_action",
        driver_class=TestActionDevice,
        config={},
        registry_name="test_action",
        action_names=("test_liquid", "test_check"),
        device_config=tree.trees[0].root_node,
    )
    with _running(spec) as runtime:
        call = runtime.call_action
        replaced = call("test_action", "test_liquid", resources=[{"uuid": first["uuid"]}],
                        liquid_name="water", volume=5.0)
        assert replaced["success"] is True
        assert replaced["resource_summary"][0]["uuid"] == first["uuid"]
        assert replaced["resource_summary"][0]["liquids"] == [{"name": "water", "volume": 5.0, "unit": "ul"}]
        # 输出给下游 handle 的资源树带新的内容物
        (output_root, *_rest) = replaced["resource_tree"][0]
        assert [tuple(item) for item in output_root["substances"]] == [("water", 5.0, "ul")]

        appended = call("test_action", "test_liquid", resources=[{"uuid": second["uuid"]}],
                        liquid_name="ethanol", volume=2.0, mode="append")
        assert appended["total_volume"] == 22.0

        checked = call("test_action", "test_check", expected_resource_count=2, fail_on_check_failed=True,
                       resources=[{"uuid": first["uuid"]}, {"uuid": second["uuid"]}], devices=["stirrer_1"])
        assert checked["success"] is True and checked["total_volume"] == 27.0
        with pytest.raises(ValueError, match="资源检查失败"):
            call("test_action", "test_check", expected_resource_count=3, fail_on_check_failed=True,
                 resources=[{"uuid": first["uuid"]}])


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
        if any("virtual" in str(node.get("class") or node.get("template_name") or "") or
               str(node.get("class") or "") == "test_action" for node in nodes):
            graphs.append(path)
    return graphs


@pytest.mark.parametrize("graph", _virtual_graphs(), ids=lambda path: path.relative_to(EXPERIMENTS).as_posix())
def test_example_graph_materials_build_as_their_template_class(graph: Path, tmp_path, monkeypatch) -> None:
    """示例图经 legacy 适配与 Graph Authority 后，每个物料都能建成 PLR，且类名与模板一致。

    开机对齐物料权威时，权威按模板校验 class_name；旧形状容器（class 为空或写成注册表名、
    没有 config.type）在这里就会失败，Host 起不来。
    """

    import shutil

    from unilabos.app.main import _load_startup_graph
    from unilabos.backend.runtime import workstation_protocol
    from unilabos.resources.resource_tracker import ResourceTreeInstance, ResourceTreeSet

    monkeypatch.setattr(workstation_protocol, "_physical_setup_graph", None)
    monkeypatch.setattr(BasicConfig, "machine_name", "lab-pc")
    graph_file = tmp_path / graph.name
    shutil.copy(graph, graph_file)
    loaded = _load_startup_graph(
        {"server_database_root": str(tmp_path), "graph": str(graph_file)}, str(tmp_path), is_slave=False
    )

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


@pytest.mark.slow
def test_virtual_bench_example_runs_on_a_real_hostlink_backend(tmp_path: Path) -> None:
    """起真实 ``unilab``（HostLink，非 test_mode），按示例图跑工作台流程与跨设备调用。"""

    from tests.app.test_process_lifetime import _Unilab

    unilab = _Unilab(
        tmp_path,
        "bench",
        ["-g", str(EXPERIMENTS / "virtual_bench.json")],
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

    def run(device_id: str, action: str, action_type: str, args: dict[str, Any]) -> dict[str, Any]:
        job = request("POST", "/job/add", {"device_id": device_id, "action": action,
                                           "action_type": action_type, "action_args": args})["jobId"]

        def finished() -> dict[str, Any] | None:
            status = request("GET", f"/job/{job}/status")
            return status if status["status"] in (4, 5, 6) else None

        status = unilab.wait_for(finished, f"{device_id}/{action} 结束", timeout=60)
        assert status["status"] == 4, (status, unilab.text()[-4000:])
        return status["result"]["return_value"]

    try:
        unilab.wait_for(lambda: "Host 已完全就绪" in unilab.text(), "Host 就绪")
        # 图里嵌套的 config 到达驱动
        assert "机械臂操作时间: 3.0s | 加热时间: 10.0s | 加热台数量: 3" in unilab.text()
        command = "UniLabJsonCommand"
        prepared = run("workbench_1", "auto-prepare_materials", command, {"count": 1})
        assert prepared["material_1"] == 1
        moved = run("workbench_1", "auto-move_to_heating_station", command, {"material_number": 1})
        assert moved["station_id"] == 1
        peer = run("workbench_1", "call_peer", command, {
            "target_device": "stirrer_1",
            "function_name": "stir",
            "function_args": json.dumps({"stir_time": 1, "stir_speed": 200, "settling_time": 0}),
        })
        assert peer["return_value"] is True
    finally:
        unilab.close()


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


@pytest.mark.parametrize("graph_name", sorted(EXAMPLE_PROTOCOLS))
def test_example_station_protocol_compiles_to_registered_device_steps(
    graph_name: str, tmp_path, monkeypatch, registry
) -> None:
    """示例协议站的代表协议能按图编译，每一步都是图里某台虚拟设备注册过的动作（不回调工作站自身）。"""

    import shutil

    from unilabos.app.main import _load_startup_graph
    from unilabos.backend.runtime import workstation_protocol
    from unilabos.experiments import models as protocol_models
    from unilabos.experiments.compile import action_protocol_generators

    monkeypatch.setattr(workstation_protocol, "_physical_setup_graph", None)
    monkeypatch.setattr(BasicConfig, "machine_name", "lab-pc")
    source = EXPERIMENTS / graph_name
    shutil.copy(source, tmp_path / source.name)
    loaded = _load_startup_graph(
        {"server_database_root": str(tmp_path), "graph": str(tmp_path / source.name)}, str(tmp_path), is_slave=False
    )
    graph = loaded.physical_setup_graph
    protocol_name, kwargs = EXAMPLE_PROTOCOLS[graph_name]
    station = next(
        node for node, data in graph.nodes(data=True) if data.get("class") == "workstation"
    )
    assert protocol_name in graph.nodes[station]["config"]["protocol_type"]

    generator = action_protocol_generators[getattr(protocol_models, protocol_name)]
    steps = list(generator(G=graph, **kwargs))

    assert steps
    flat = [step for item in steps for step in (item if isinstance(item, list) else [item])]
    devices = {node for node, data in graph.nodes(data=True) if data.get("type") == "device"} - {station}
    for step in flat:
        if step["action_name"] in ("wait", "log_message"):
            continue
        assert step["device_id"] in devices, step
        template = graph.nodes[step["device_id"]]["template_name"]
        assert step["action_name"] in registry[template]["class"]["action_value_mappings"], (template, step)
