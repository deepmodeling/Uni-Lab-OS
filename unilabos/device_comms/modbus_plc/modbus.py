# coding=utf-8
"""Modbus 寄存器 / 线圈节点，兼容核心允许的整个 pymodbus 范围（>=3.6.9,<4）。

pymodbus 在这个范围里改过两处接口，节点读写统一经过本模块的兼容函数：

- 从站地址关键字：3.6.9–3.9 叫 ``slave``，3.10 起叫 ``device_id``（旧名直接 TypeError）。
  :func:`call_with_slave` 按方法签名选名字；只收 ``**kwargs`` 的替身 / 模拟器按已安装的 pymodbus 选。
- ``convert_from_registers`` / ``convert_to_registers`` 的 ``word_order``：3.8 之前没有该参数，
  此时按 pymodbus 3.9+ 的规则（每个值的寄存器顺序反转）自行交换字序。
"""
import inspect
from enum import Enum
from abc import ABC, abstractmethod
from functools import lru_cache

from pymodbus.client.mixin import ModbusClientMixin
from typing import Any, Callable, List, Optional, Tuple, Union

try:
    from pymodbus.client import ModbusBaseSyncClient
except ImportError:  # pymodbus < 3.8 只在 pymodbus.client.base 里定义
    from pymodbus.client.base import ModbusBaseSyncClient

DataType = ModbusClientMixin.DATATYPE

#: 各版本 pymodbus 对从站地址的叫法，新的在前
_SLAVE_KEYWORDS = ("device_id", "slave", "unit")


def _installed_slave_keyword() -> str:
    parameters = inspect.signature(ModbusClientMixin.read_coils).parameters
    return next((name for name in _SLAVE_KEYWORDS if name in parameters), "slave")


#: 已安装的 pymodbus 用的从站地址关键字（只收 ``**kwargs`` 的客户端按它传）
INSTALLED_SLAVE_KEYWORD = _installed_slave_keyword()


@lru_cache(maxsize=None)
def _slave_keyword_of(func: Callable) -> Optional[str]:
    try:
        parameters = inspect.signature(func).parameters
    except (TypeError, ValueError):
        return INSTALLED_SLAVE_KEYWORD
    named = {
        name
        for name, parameter in parameters.items()
        if parameter.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    }
    for name in _SLAVE_KEYWORDS:
        if name in named:
            return name
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()):
        return INSTALLED_SLAVE_KEYWORD
    return None


def slave_keyword(method: Callable) -> Optional[str]:
    """读写方法接收从站地址的关键字名；方法不接收从站地址时返回 None。"""

    func = getattr(method, "__func__", method)
    try:
        return _slave_keyword_of(func)
    except TypeError:  # 不可哈希的可调用对象，不缓存
        return _slave_keyword_of.__wrapped__(func)


def call_with_slave(method: Callable, *args: Any, slave: int = 1, **kwargs: Any) -> Any:
    """调用 pymodbus 客户端的读写方法，按该方法接受的名字传从站地址。

    例如 ``call_with_slave(client.read_holding_registers, 100, count=2, slave=3)``：
    pymodbus >= 3.10 收到 ``device_id=3``，3.6.9–3.9 收到 ``slave=3``。
    """

    keyword = slave_keyword(method)
    if keyword is not None:
        kwargs[keyword] = slave
    return method(*args, **kwargs)


def _accepts_keyword(method: Callable, name: str) -> bool:
    try:
        parameters = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return True
    return name in parameters or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())


def _swap_word_order(registers: List[int], data_type: DataType) -> List[int]:
    """大端字序与小端字序互换：定长类型按每个值的寄存器反转，字符串 / 位串整体反转（同 pymodbus 3.9+）。"""

    registers = list(registers)
    size = data_type.value[1]
    if not size:
        return registers[::-1]
    swapped: List[int] = []
    for start in range(0, len(registers), size):
        swapped.extend(reversed(registers[start : start + size]))
    return swapped


def convert_from_registers(client: Any, registers: List[int], data_type: DataType, word_order: str = "big") -> Any:
    """``client.convert_from_registers``，老版本 pymodbus 没有 ``word_order`` 时自行交换字序。"""

    convert = client.convert_from_registers
    if word_order == "big":
        return convert(list(registers), data_type)
    if _accepts_keyword(convert, "word_order"):
        return convert(list(registers), data_type, word_order=word_order)
    return convert(_swap_word_order(registers, data_type), data_type)


def convert_to_registers(client: Any, value: Any, data_type: DataType, word_order: str = "big") -> List[int]:
    """``client.convert_to_registers``，老版本 pymodbus 没有 ``word_order`` 时自行交换字序。"""

    convert = client.convert_to_registers
    if word_order == "big":
        return convert(value, data_type)
    if _accepts_keyword(convert, "word_order"):
        return convert(value, data_type, word_order=word_order)
    return _swap_word_order(convert(value, data_type), data_type)


class WorderOrder(Enum):
    BIG = "big"
    LITTLE = "little"


def _word_order_value(word_order: Union["WorderOrder", str]) -> str:
    # 工作流 JSON 里给的是字符串 "big" / "little"
    return WorderOrder(word_order).value


class DeviceType(Enum):
    COIL = 'coil'
    DISCRETE_INPUTS = 'discrete_inputs'
    HOLD_REGISTER = 'hold_register'
    INPUT_REGISTER = 'input_register'


class Base(ABC):
    def __init__(self, client: ModbusBaseSyncClient, name: str, address: int, typ: DeviceType, data_type: DataType):
        self._address: int = address
        self._client = client
        self._name = name
        self._type = typ
        self._data_type = data_type

    @abstractmethod
    def read(self, value, data_type: Optional[DataType] = None, word_order: WorderOrder = WorderOrder.BIG, slave = 1,) -> Tuple[Union[int, float, str, list[bool], list[int], list[float]], bool]:
        pass

    @abstractmethod
    def write(self, value: Union[int, float, bool, str, list[bool], list[int], list[float]], data_type: Optional[DataType]= None, word_order: WorderOrder = WorderOrder.LITTLE, slave = 1) -> bool:
        pass

    @property
    def type(self) -> DeviceType:
        return self._type

    @property
    def address(self) -> int:
        return self._address

    @property
    def name(self) -> str:
        return self._name


class Coil(Base):
    def __init__(self, client,name, address: int, data_type: DataType):
        super().__init__(client, name, address, DeviceType.COIL, data_type)

    def read(self, value, data_type: Optional[DataType] = None, word_order: WorderOrder = WorderOrder.BIG, slave = 1,) -> Tuple[Union[int, float, str, list[bool], list[int], list[float]], bool]:
        resp = call_with_slave(self._client.read_coils, address=self.address, count=value, slave=slave)

        return resp.bits, resp.isError()

    def write(self,value: Union[int, float, bool, str, list[bool], list[int], list[float]], data_type: Optional[DataType ]= None, word_order: WorderOrder = WorderOrder.LITTLE, slave = 1) -> bool:
        if isinstance(value, list):
            for v in value:
                if not isinstance(v, bool):
                    raise ValueError(f'value invalidate: {value}')

            return call_with_slave(
                self._client.write_coils, address=self.address, values=[bool(v) for v in value], slave=slave
            ).isError()
        else:
            return call_with_slave(self._client.write_coil, address=self.address, value=bool(value), slave=slave).isError()


class DiscreteInputs(Base):
    def __init__(self, client,name, address: int, data_type: DataType):
        # 原来线圈以外的节点也报 COIL，同名节点再次注册时被判成类型冲突
        super().__init__(client, name, address, DeviceType.DISCRETE_INPUTS, data_type)

    def read(self, value, data_type: Optional[DataType] = None, word_order: WorderOrder = WorderOrder.BIG, slave = 1,) -> Tuple[Union[int, float, str, list[bool], list[int], list[float]], bool]:
        # 离散输入是位（与线圈相同），应答在 bits 里；原实现按寄存器解码，永远得到空结果
        resp = call_with_slave(self._client.read_discrete_inputs, self.address, count=value, slave=slave)
        return resp.bits, resp.isError()

    def write(self,value: Union[int, float, bool, str, list[bool], list[int], list[float]], data_type: Optional[DataType ]= None, word_order: WorderOrder = WorderOrder.LITTLE, slave = 1) -> bool:
        raise ValueError('discrete inputs only support read')

class HoldRegister(Base):
    def __init__(self, client,name, address: int, data_type: DataType):
        # 原来线圈以外的节点也报 COIL，同名节点再次注册时被判成类型冲突
        super().__init__(client, name, address, DeviceType.HOLD_REGISTER, data_type)

    def read(self, value, data_type: Optional[DataType] = None, word_order: WorderOrder = WorderOrder.BIG, slave = 1,) -> Tuple[Union[int, float, str, list[bool], list[int], list[float]], bool]:
        if not data_type and not self._data_type:
            raise ValueError('data type is required')

        if not data_type:
            data_type = self._data_type

        resp = call_with_slave(self._client.read_holding_registers, address=self.address, count=value, slave=slave)
        # noinspection PyTypeChecker
        return convert_from_registers(self._client, resp.registers, data_type, _word_order_value(word_order)), resp.isError()


    def write(self,value: Union[int, float, bool, str, list[bool], list[int], list[float]], data_type: Optional[DataType ]= None, word_order: WorderOrder = WorderOrder.LITTLE, slave = 1) -> bool:
        if not data_type and not self._data_type:
            raise ValueError('data type is required')

        if not data_type:
            data_type = self._data_type

        if isinstance(value , bool):
            if value:
                return call_with_slave(self._client.write_register, self.address, 1, slave=slave).isError()
            else:
                return call_with_slave(self._client.write_register, self.address, 0, slave=slave).isError()
        elif isinstance(value, int):
            return call_with_slave(self._client.write_register, self.address, value, slave=slave).isError()
        else:
            # noinspection PyTypeChecker
            encoder_resp = convert_to_registers(self._client, value, data_type, _word_order_value(word_order))
            return call_with_slave(self._client.write_registers, self.address, encoder_resp, slave=slave).isError()



class InputRegister(Base):
    def __init__(self, client,name, address: int, data_type: DataType):
        # 原来线圈以外的节点也报 COIL，同名节点再次注册时被判成类型冲突
        super().__init__(client, name, address, DeviceType.INPUT_REGISTER, data_type)


    def read(self, value, data_type: Optional[DataType] = None, word_order: WorderOrder = WorderOrder.BIG, slave = 1) -> Tuple[Union[int, float, str, list[bool], list[int], list[float]], bool]:
        if not data_type and not self._data_type:
            raise ValueError('data type is required')

        if not data_type:
            data_type = self._data_type

        resp = call_with_slave(self._client.read_holding_registers, address=self.address, count=value, slave=slave)
        # noinspection PyTypeChecker
        return convert_from_registers(self._client, resp.registers, data_type, _word_order_value(word_order)), resp.isError()

    def write(self,value: Union[int, float, bool, str, list[bool], list[int], list[float]], data_type: Optional[DataType ]= None, word_order: WorderOrder = WorderOrder.LITTLE, slave = 1) -> bool:
        raise ValueError('input register only support read')

