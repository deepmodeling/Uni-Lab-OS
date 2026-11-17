"""核心 modbus_plc 节点在核心允许的整个 pymodbus 范围（>=3.6.9,<4）上读写（FRAMEWORK_GAPS #35）。

pymodbus 的接口在这个范围里变过：从站地址关键字 3.6.9–3.9 叫 ``slave``、3.10 起叫 ``device_id``
（旧名直接 TypeError）；``convert_from_registers`` / ``convert_to_registers`` 3.8 之前没有 ``word_order``。
这里用当前安装的 pymodbus 的真实读写方法（``execute`` 不走网络），再用模仿各版本签名的替身客户端，
校验节点按客户端实际接受的名字传从站地址、按 pymodbus 3.9+ 的规则处理字序。
"""

from __future__ import annotations

import inspect
from typing import Any, Dict, List, Tuple

import pytest

pytest.importorskip("pymodbus")

from pymodbus.client.mixin import ModbusClientMixin  # noqa: E402

from unilabos.device_comms.modbus_plc.modbus import (  # noqa: E402
    Coil,
    DataType,
    DiscreteInputs,
    HoldRegister,
    InputRegister,
    WorderOrder,
    call_with_slave,
)

INSTALLED_UNIT_KEYWORD = next(
    name for name in ("device_id", "slave", "unit") if name in inspect.signature(ModbusClientMixin.read_coils).parameters
)
FLOAT32_10_BIG = [0x4120, 0x0000]  # 10.0，高字在前


class _Response:
    def __init__(self, bits=None, registers=None):
        self.bits = list(bits or [])
        self.registers = list(registers or [])

    @staticmethod
    def isError() -> bool:  # noqa: N802 - pymodbus 的接口名
        return False


class LoopbackClient(ModbusClientMixin):
    """当前安装的 pymodbus 的真实读写方法；``execute`` 记录请求 PDU 并给出固定应答。"""

    def __init__(self, registers: List[int] = ()):
        super().__init__()
        self.registers = list(registers)
        self.requests: List[Tuple[str, int, Any]] = []

    def execute(self, *args, **kwargs):  # 3.6.9: execute(request)；3.7+: execute(no_response_expected, request)
        request = args[-1] if args else kwargs["request"]
        unit = next(getattr(request, name) for name in ("dev_id", "slave_id", "unit_id") if hasattr(request, name))
        self.requests.append((type(request).__name__, request.address, unit))
        count = getattr(request, "count", 0) or 1
        return _Response(bits=[True] * count, registers=self.registers[:count])


def test_nodes_reach_the_installed_pymodbus_with_their_slave_address():
    client = LoopbackClient(registers=FLOAT32_10_BIG)

    assert Coil(client, "coil", 8, DataType.INT16).read(1, slave=3) == ([True], False)
    assert Coil(client, "coil", 8, DataType.INT16).write(True, slave=4) is False
    assert Coil(client, "coil", 8, DataType.INT16).write([True, False], slave=5) is False
    assert HoldRegister(client, "hold", 100, DataType.FLOAT32).read(2, slave=6) == (10.0, False)
    assert HoldRegister(client, "hold", 100, DataType.INT16).write(7, slave=7) is False
    assert HoldRegister(client, "hold", 100, DataType.FLOAT32).write(1.5, slave=8) is False
    assert InputRegister(client, "input", 200, DataType.FLOAT32).read(2, slave=9) == (10.0, False)
    assert DiscreteInputs(client, "discrete", 300, DataType.INT16).read(2, slave=10) == ([True, True], False)

    assert [unit for _, _, unit in client.requests] == [3, 4, 5, 6, 7, 8, 9, 10]
    assert [address for _, address, _ in client.requests] == [8, 8, 8, 100, 100, 100, 200, 300]


def test_default_slave_is_one():
    client = LoopbackClient(registers=[5])
    HoldRegister(client, "hold", 1, DataType.INT16).read(1)
    assert client.requests[-1][2] == 1


class DeviceIdClient:
    """pymodbus >= 3.10 的签名。"""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def read_coils(self, address, *, count=1, device_id=1, no_response_expected=False):
        self.calls.append({"address": address, "count": count, "device_id": device_id})
        return _Response(bits=[False] * count)


class SlaveClient:
    """pymodbus 3.6.9–3.9 的签名（3.6.9 还带 ``**kwargs``）。"""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def read_coils(self, address, count=1, slave=0, **kwargs):
        self.calls.append({"address": address, "count": count, "slave": slave, **kwargs})
        return _Response(bits=[False] * count)


class KwargsClient:
    """只收 ``**kwargs`` 的替身 / 模拟器：按当前安装的 pymodbus 的名字传。"""

    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    def read_coils(self, address, count=1, **unit):
        self.calls.append({"address": address, "count": count, **unit})
        return _Response(bits=[False] * count)


@pytest.mark.parametrize(
    "client_cls, keyword",
    [(DeviceIdClient, "device_id"), (SlaveClient, "slave"), (KwargsClient, INSTALLED_UNIT_KEYWORD)],
    ids=["pymodbus>=3.10", "pymodbus<3.10", "kwargs-only"],
)
def test_slave_address_follows_the_client_signature(client_cls, keyword):
    client = client_cls()
    Coil(client, "coil", 12, DataType.INT16).read(2, slave=7)
    assert client.calls == [{"address": 12, "count": 2, keyword: 7}]

    call_with_slave(client.read_coils, 13, count=1, slave=9)
    assert client.calls[-1] == {"address": 13, "count": 1, keyword: 9}


class LegacyConvertClient(LoopbackClient):
    """pymodbus 3.6.9–3.8.0 的 convert_*：没有 ``word_order``，总是高字在前。"""

    @staticmethod
    def convert_from_registers(registers, data_type):
        return ModbusClientMixin.convert_from_registers(list(registers), data_type)

    @staticmethod
    def convert_to_registers(value, data_type):
        return ModbusClientMixin.convert_to_registers(value, data_type)


@pytest.mark.parametrize("client_cls", [LoopbackClient, LegacyConvertClient], ids=["installed", "pymodbus<3.8"])
@pytest.mark.parametrize("word_order", [WorderOrder.LITTLE, "little"], ids=["enum", "str"])
def test_little_word_order_reads_and_writes_the_same_on_every_version(client_cls, word_order):
    little = list(reversed(FLOAT32_10_BIG))
    client = client_cls(registers=little)
    node = HoldRegister(client, "hold", 40, DataType.FLOAT32)

    assert node.read(2, word_order=word_order) == (10.0, False)

    written: List[List[int]] = []
    client.write_registers = lambda address, values, **unit: written.append(list(values)) or _Response()
    node.write(10.0, word_order=word_order)
    assert written == [little]
