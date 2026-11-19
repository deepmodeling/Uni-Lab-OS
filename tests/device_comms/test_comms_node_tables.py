"""核心 Modbus / OPC UA 客户端的节点表、函数表、工作流表每个实例一份（FRAMEWORK_GAPS #36）。

原来这些表声明为类属性字典，经 ``self`` 原地修改，所有客户端共用一份：第二个客户端注册同名节点时
沿用第一个客户端（连着另一台 PLC / 服务器）的节点，还能用到它根本没注册的节点。
"""

from __future__ import annotations

import pytest

pytest.importorskip("pymodbus")

from unilabos.device_comms.modbus_plc.client import ModbusNode, TCPClient  # noqa: E402
from unilabos.device_comms.modbus_plc.modbus import DataType, DeviceType  # noqa: E402


def _modbus_pair():
    # 构造不连接 PLC
    return TCPClient("127.0.0.1", 1502), TCPClient("127.0.0.1", 1503)


def test_modbus_clients_keep_their_own_node_tables():
    first, second = _modbus_pair()
    first.register_node_list([ModbusNode(name="start", device_type=DeviceType.COIL, address=8)])
    second.register_node_list(
        [ModbusNode(name="speed", device_type=DeviceType.HOLD_REGISTER, address=100, data_type=DataType.FLOAT32)]
    )

    assert set(first.get_node_registry()) == {"start"}
    assert set(second.get_node_registry()) == {"speed"}
    with pytest.raises(ValueError, match="not registered"):
        first.use_node("speed")

    # 同名节点：各自指向自己的连接
    second.register_node_list([ModbusNode(name="start", device_type=DeviceType.COIL, address=8)])
    assert first.use_node("start")._client is first.client
    assert second.use_node("start")._client is second.client


def test_modbus_function_and_workflow_tables_are_per_client():
    first, second = _modbus_pair()
    first.register_node_list([ModbusNode(name="start", device_type=DeviceType.COIL, address=8)])
    first.create_address_function("start_on", "start", "write", True)
    first.create_workflow_from_json([{"name": "boot", "action": []}])

    assert "start_on" in first.function_name and "start_on" not in second.function_name
    assert "boot" in first.workflow_name and "boot" not in second.workflow_name


def test_modbus_nodes_report_their_own_type_so_registering_twice_is_idempotent():
    """寄存器节点原来都报 DeviceType.COIL：同一节点第二次注册（或第二个客户端注册同名节点）报类型冲突。"""

    client, _ = _modbus_pair()
    nodes = [
        ModbusNode(name="speed", device_type=DeviceType.HOLD_REGISTER, address=100, data_type=DataType.FLOAT32),
        ModbusNode(name="temperature", device_type=DeviceType.INPUT_REGISTER, address=200),
        ModbusNode(name="door", device_type=DeviceType.DISCRETE_INPUTS, address=300),
        ModbusNode(name="start", device_type=DeviceType.COIL, address=8),
    ]
    client.register_node_list(nodes)
    client.register_node_list(nodes)

    assert {name: node.type for name, node in client.get_node_registry().items()} == {
        node.name: node.device_type for node in nodes
    }


def test_opcua_clients_keep_their_own_tables():
    pytest.importorskip("opcua")
    from unilabos.device_comms.opcua_client.client import BaseClient, OpcUaNode
    from unilabos.device_comms.opcua_client.node.uniopcua import DataType as OpcDataType, NodeType

    first, second = BaseClient(), BaseClient()
    # 未连接时只登记到待查找表，连接后按浏览名查找
    first.register_node_list([OpcUaNode(name="温度", node_type=NodeType.VARIABLE, data_type=OpcDataType.FLOAT)])
    first.create_node_function("read_temperature", "温度", "read")
    first.create_workflow_from_json([{"name": "boot", "action": []}])

    assert "温度" in first._variables_to_find and "温度" not in second._variables_to_find
    assert first.get_node_registry() is not second.get_node_registry()
    assert "read_temperature" in first.function_name and "read_temperature" not in second.function_name
    assert "boot" in first.workflow_name and "boot" not in second.workflow_name


def test_opcua_node_attributes_belong_to_the_instance():
    """_register_nodes_as_attributes 原来把节点属性 setattr 到 OpcUaClient 类上，所有实例（及子类）都带上它。"""

    pytest.importorskip("opcua")
    from unilabos.device_comms.opcua_client.client import BaseClient, OpcUaClient

    def offline_client(values):
        # 不连服务器：只建 BaseClient 部分，再放入已缓存的节点值
        client = OpcUaClient.__new__(OpcUaClient)
        BaseClient.__init__(client)
        client._node_values = dict(values)
        for name in values:
            client._node_registry[name] = object()
        return client

    first = offline_client({"温度": 25.0})
    first._reverse_mapping["温度"] = "temperature"
    first._register_nodes_as_attributes()
    second = offline_client({"pressure": 1.5})
    second._register_nodes_as_attributes()

    assert first.temperature == 25.0
    assert second.pressure == 1.5
    assert not hasattr(OpcUaClient, "temperature") and not hasattr(OpcUaClient, "pressure")
    assert not hasattr(second, "temperature") and not hasattr(first, "pressure")
