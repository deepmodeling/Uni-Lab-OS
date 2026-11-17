"""HostLink 工作站 protocol 编排器：XDL protocol 编排契约与端到端执行。

- protocol 步骤生成/资源展开回写是共享实现（runtime.workstation_protocol）；
- HostLink 设备节点对 protocol 名的动作解析路由到编排器（hostlink WorkstationNode）；
- 子设备动作经 backend 无关的 call_device_action_async 派发。
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path

import pytest
from pydantic import BaseModel

from unilabos.backend.hostlink.backend import HostLinkBackend
from unilabos.backend.hostlink.local_runtime import (
    HostLinkDriverSpec,
    HostLinkLocalRuntime,
)
from unilabos.devices.workstation.workstation_base import WorkstationBase
from unilabos.experiments import models as protocol_models
from unilabos.experiments.compile import action_protocol_generators


class FakeHeatProtocol(BaseModel):
    temp: float


def _generate_fake_heat_protocol(G=None, **kwargs):
    temp = kwargs["temp"]
    return [
        {"device_id": "heater-1", "action_name": "heat", "action_kwargs": {"temp": temp}},
        {"action_name": "wait", "action_kwargs": {"time": 0.01}, "device_id": ""},
        {
            "device_id": "",
            "action_name": "log_message",
            "action_kwargs": {"log_message": "step done"},
        },
    ]


class FakeWorkstationDriver(WorkstationBase):
    def __init__(self, protocol_type=None, **kwargs) -> None:
        super().__init__(deck=None)
        self.protocol_type = protocol_type


class HeaterDriver:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def heat(self, temp: float) -> dict[str, float]:
        self.calls.append(temp)
        return {"reached": temp}


@pytest.fixture
def fake_protocol(monkeypatch):
    monkeypatch.setattr(
        protocol_models, "FakeHeatProtocol", FakeHeatProtocol, raising=False
    )
    monkeypatch.setitem(action_protocol_generators, FakeHeatProtocol, _generate_fake_heat_protocol)
    yield
    action_protocol_generators.pop(FakeHeatProtocol, None)


def test_hostlink_workstation_runs_protocol_via_unified_orchestrator(fake_protocol) -> None:
    local = HostLinkLocalRuntime()
    ws_node = local.add_driver(
        HostLinkDriverSpec(
            device_id="ws-1",
            driver_class=FakeWorkstationDriver,
            config={"protocol_type": ["FakeHeatProtocol"]},
            action_names=("FakeHeatProtocol",),
            action_value_mappings={
                "FakeHeatProtocol": {"type": "FakeHeatProtocol", "placeholder_keys": {}}
            },
        )
    )
    heater_node = local.add_driver(
        HostLinkDriverSpec(
            device_id="heater-1",
            driver_class=HeaterDriver,
            config={},
            action_names=("heat",),
            action_value_mappings={"heat": {"type": "UniLabJsonCommand"}},
        )
    )
    runtime = HostLinkBackend(local, is_slave=False)
    local.start()
    try:
        orchestrator = ws_node.__dict__.get("_workstation_protocols")
        assert orchestrator is not None, "workstation 应在 add_driver 时挂上 protocol 编排器"
        assert orchestrator.protocol_names == ["FakeHeatProtocol"]
        assert orchestrator.protocol_action("not_a_protocol") is None

        result = asyncio.run(
            runtime.call_action_async("ws-1", "FakeHeatProtocol", temp=81.5)
        )

        assert heater_node.driver.calls == [81.5]
        assert result["protocol_name"] == "FakeHeatProtocol"
        assert result["steps_executed"] == 3
        step_actions = [
            step.get("action") for step in result["step_results"]
        ]
        assert step_actions == ["heat", "wait", "log_message"]
        # log_message 是日志占位步：记录信息、不失败
        assert "step done" in str(result["step_results"][2]["result"])
        assert result["step_results"][0]["result"] == {"reached": 81.5}
    finally:
        runtime.stop()


def test_workstation_shared_logic_is_single_source_across_backends() -> None:
    """workstation 编排按 backend 各一份，共享逻辑只在 runtime.workstation_protocol：

    - ros2 形态经 base_device_node 装配 ros2/presets/workstation.ROS2WorkstationNode；
    - hostlink 形态由 local_runtime 挂 hostlink/workstation.WorkstationNode
      并在动作解析时路由；
    - 协议名/模型解析与资源展开/回写是模块级共享函数，两个实现均引用之。
    """
    import unilabos.backend.hostlink.local_runtime as local_runtime_module
    import unilabos.backend.hostlink.workstation as hostlink_workstation_module
    from unilabos.backend.runtime import workstation_protocol

    # 验证装配关系时不导入 ROS 运行时，无 ROS 的 HostLink CI 也覆盖此契约。
    ros2_directory = Path(local_runtime_module.__file__).parents[1] / "ros2"
    base_source = (ros2_directory / "base_device_node.py").read_text(encoding="utf-8")
    assert "unilabos.backend.ros2.presets.workstation" in base_source

    runtime_source = inspect.getsource(local_runtime_module.HostLinkLocalRuntime.add_driver)
    assert "unilabos.backend.hostlink.workstation" in runtime_source
    assert '_workstation_protocols' in runtime_source

    resolve_source = inspect.getsource(
        local_runtime_module.HostLinkDeviceNode._resolve_action
    )
    assert "_workstation_protocols" in resolve_source
    assert "protocol_action" in resolve_source

    # 步骤生成/资源展开/回写是共享实现（模块级唯一），两个实现均从共享模块导入
    for name in (
        "expand_resource_value",
        "update_protocol_resources",
        "setup_protocol_names",
        "protocol_model",
    ):
        assert hasattr(workstation_protocol, name), name
    hostlink_source = inspect.getsource(hostlink_workstation_module)
    assert "unilabos.backend.runtime.workstation_protocol" in hostlink_source
    ros2_workstation_path = ros2_directory / "presets" / "workstation.py"
    assert ros2_workstation_path.is_file()
    assert "unilabos.backend.runtime.workstation_protocol" in ros2_workstation_path.read_text(
        encoding="utf-8"
    )


def test_expanded_protocol_vessel_uses_the_physical_graph_node_id(monkeypatch) -> None:
    """协议的容器入参按权威展开后，``id`` 要是物理拓扑图里的节点名。

    权威的 resource id 是带父级前缀的路径（``Station/flask``），协议编译器按 ``-g`` 图的节点
    id 查容器；展开后原样带路径时编译器报「容器不存在于系统中」，示例协议站全都跑不起来。
    """

    import uuid

    import networkx as nx

    from unilabos.backend.runtime import workstation_protocol
    from unilabos.resources.resource_tracker import ResourceTreeSet

    flask_uuid, station_uuid = str(uuid.uuid4()), str(uuid.uuid4())
    graph = nx.Graph()
    graph.add_node("Station", uuid=station_uuid, type="device")
    graph.add_node("flask", uuid=flask_uuid, type="container")
    monkeypatch.setattr(workstation_protocol, "_physical_setup_graph", graph)
    authority = ResourceTreeSet.from_raw_dict_list(
        [
            {
                "id": "Station/flask", "uuid": flask_uuid, "name": flask_uuid, "type": "container",
                "class": "RegularContainer", "template_name": "container",
                "config": {"type": "RegularContainer", "name": "flask"}, "data": {}, "extra": {},
            }
        ]
    )

    class Node:
        async def get_resource(self, uuids, with_children=True):
            assert uuids == [flask_uuid]
            return authority

    single = asyncio.run(
        workstation_protocol.expand_resource_value(Node(), {"id": "flask", "uuid": flask_uuid})
    )
    assert single["id"] == "flask" and single["uuid"] == flask_uuid
    many = asyncio.run(
        workstation_protocol.expand_resource_value(Node(), [{"id": "flask", "uuid": flask_uuid}])
    )
    assert many[0][0]["id"] == "flask"
    # 物理拓扑图里没有这个物料（运行期新建的）时保留权威 id
    monkeypatch.setattr(workstation_protocol, "_physical_setup_graph", nx.Graph())
    kept = asyncio.run(
        workstation_protocol.expand_resource_value(Node(), {"uuid": flask_uuid})
    )
    assert kept["id"] == "Station/flask"
