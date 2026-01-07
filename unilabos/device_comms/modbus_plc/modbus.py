# coding=utf-8
from enum import Enum
from abc import ABC, abstractmethod
from typing import Tuple, Union, Optional, TYPE_CHECKING
from pymodbus.payload import BinaryPayloadDecoder, BinaryPayloadBuilder
from pymodbus.constants import Endian

if TYPE_CHECKING:
    from pymodbus.client.sync import ModbusSerialClient, ModbusTcpClient

# Define DataType enum for pymodbus 2.5.3 compatibility
class DataType(Enum):
    INT16 = "int16"
    UINT16 = "uint16"
    INT32 = "int32"
    UINT32 = "uint32"
    INT64 = "int64"
    UINT64 = "uint64"
    FLOAT32 = "float32"
    FLOAT64 = "float64"
    STRING = "string"
    BOOL = "bool"


class WorderOrder(Enum):
    BIG = "big"
    LITTLE = "little"

class DeviceType(Enum):
    COIL = 'coil'
    DISCRETE_INPUTS = 'discrete_inputs'
    HOLD_REGISTER = 'hold_register'
    INPUT_REGISTER = 'input_register'


def _convert_from_registers(registers, data_type: DataType, word_order: str = 'big'):
    """Convert registers to a value using BinaryPayloadDecoder.
    
    Args:
        registers: List of register values
        data_type: DataType enum specifying the target data type
        word_order: 'big' or 'little' endian
        
    Returns:
        Converted value
    """
    # Determine byte and word order based on word_order parameter
    if word_order == 'little':
        byte_order = Endian.Little
        word_order_enum = Endian.Little
    else:
        byte_order = Endian.Big
        word_order_enum = Endian.Big
    
    decoder = BinaryPayloadDecoder.fromRegisters(registers, byteorder=byte_order, wordorder=word_order_enum)
    
    if data_type == DataType.INT16:
        return decoder.decode_16bit_int()
    elif data_type == DataType.UINT16:
        return decoder.decode_16bit_uint()
    elif data_type == DataType.INT32:
        return decoder.decode_32bit_int()
    elif data_type == DataType.UINT32:
        return decoder.decode_32bit_uint()
    elif data_type == DataType.INT64:
        return decoder.decode_64bit_int()
    elif data_type == DataType.UINT64:
        return decoder.decode_64bit_uint()
    elif data_type == DataType.FLOAT32:
        return decoder.decode_32bit_float()
    elif data_type == DataType.FLOAT64:
        return decoder.decode_64bit_float()
    elif data_type == DataType.STRING:
        return decoder.decode_string(len(registers) * 2)
    else:
        raise ValueError(f"Unsupported data type: {data_type}")


def _convert_to_registers(value, data_type: DataType, word_order: str = 'little'):
    """Convert a value to registers using BinaryPayloadBuilder.
    
    Args:
        value: Value to convert
        data_type: DataType enum specifying the source data type
        word_order: 'big' or 'little' endian
        
    Returns:
        List of register values
    """
    # Determine byte and word order based on word_order parameter
    if word_order == 'little':
        byte_order = Endian.Little
        word_order_enum = Endian.Little
    else:
        byte_order = Endian.Big
        word_order_enum = Endian.Big
    
    builder = BinaryPayloadBuilder(byteorder=byte_order, wordorder=word_order_enum)
    
    if data_type == DataType.INT16:
        builder.add_16bit_int(value)
    elif data_type == DataType.UINT16:
        builder.add_16bit_uint(value)
    elif data_type == DataType.INT32:
        builder.add_32bit_int(value)
    elif data_type == DataType.UINT32:
        builder.add_32bit_uint(value)
    elif data_type == DataType.INT64:
        builder.add_64bit_int(value)
    elif data_type == DataType.UINT64:
        builder.add_64bit_uint(value)
    elif data_type == DataType.FLOAT32:
        builder.add_32bit_float(value)
    elif data_type == DataType.FLOAT64:
        builder.add_64bit_float(value)
    elif data_type == DataType.STRING:
        builder.add_string(value)
    else:
        raise ValueError(f"Unsupported data type: {data_type}")
    
    return builder.to_registers()


class Base(ABC):
    def __init__(self, client, name: str, address: int, typ: DeviceType, data_type):
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
        resp =  self._client.read_coils(
                address = self.address,
                count = value,
                slave = slave)

        # 检查是否读取出错
        if resp.isError():
            return [], True
        
        return resp.bits, False

    def write(self,value: Union[int, float, bool, str, list[bool], list[int], list[float]], data_type: Optional[DataType ]= None, word_order: WorderOrder = WorderOrder.LITTLE, slave = 1) -> bool:
        if isinstance(value, list):
            for v in value:
                if not isinstance(v, bool):
                    raise ValueError(f'value invalidate: {value}')

            return self._client.write_coils(
                    address = self.address,
                    values = [bool(v) for v in value],
                    slave = slave).isError()
        else:
            return self._client.write_coil(
                    address = self.address,
                    value = bool(value),
                    slave = slave).isError()


class DiscreteInputs(Base):
    def __init__(self, client,name, address: int, data_type: DataType):
        super().__init__(client, name, address, DeviceType.COIL, data_type)

    def read(self, value, data_type: Optional[DataType] = None, word_order: WorderOrder = WorderOrder.BIG, slave = 1,) -> Tuple[Union[int, float, str, list[bool], list[int], list[float]], bool]:
        if not data_type and not self._data_type:
            raise ValueError('data type is required')
        if not data_type:
            data_type = self._data_type
        resp = self._client.read_discrete_inputs(
                self.address,
                count = value,
                slave = slave)

        # 检查是否读取出错
        if resp.isError():
            # 根据数据类型返回默认值
            if data_type in [DataType.FLOAT32, DataType.FLOAT64]:
                return 0.0, True
            elif data_type == DataType.STRING:
                return "", True
            else:
                return 0, True
        
        # noinspection PyTypeChecker
        return _convert_from_registers(resp.registers, data_type, word_order=word_order.value), False

    def write(self,value: Union[int, float, bool, str, list[bool], list[int], list[float]], data_type: Optional[DataType ]= None, word_order: WorderOrder = WorderOrder.LITTLE, slave = 1) -> bool:
        raise ValueError('discrete inputs only support read')

class HoldRegister(Base):
    def __init__(self, client,name, address: int, data_type: DataType):
        super().__init__(client, name, address, DeviceType.COIL, data_type)

    def read(self, value, data_type: Optional[DataType] = None, word_order: WorderOrder = WorderOrder.BIG, slave = 1,) -> Tuple[Union[int, float, str, list[bool], list[int], list[float]], bool]:
        if not data_type and not self._data_type:
            raise ValueError('data type is required')

        if not data_type:
            data_type = self._data_type

        resp = self._client.read_holding_registers(
                address = self.address,
                count = value,
                slave = slave)
        
        # 检查是否读取出错
        if resp.isError():
            # 根据数据类型返回默认值
            if data_type in [DataType.FLOAT32, DataType.FLOAT64]:
                return 0.0, True
            elif data_type == DataType.STRING:
                return "", True
            else:
                return 0, True
        
        # noinspection PyTypeChecker
        return _convert_from_registers(resp.registers, data_type, word_order=word_order.value), False


    def write(self,value: Union[int, float, bool, str, list[bool], list[int], list[float]], data_type: Optional[DataType ]= None, word_order: WorderOrder = WorderOrder.LITTLE, slave = 1) -> bool:
        if not data_type and not self._data_type:
            raise ValueError('data type is required')

        if not data_type:
            data_type = self._data_type

        if isinstance(value , bool):
            if value:
                return self._client.write_register(self.address, 1, slave= slave).isError()
            else:
                return self._client.write_register(self.address, 0, slave= slave).isError()
        elif isinstance(value, int):
            return self._client.write_register(self.address, value, slave= slave).isError()
        else:
            # noinspection PyTypeChecker
            encoder_resp = _convert_to_registers(value, data_type=data_type, word_order=word_order.value)
            return self._client.write_registers(self.address, encoder_resp, slave=slave).isError()



class InputRegister(Base):
    def __init__(self, client,name, address: int, data_type: DataType):
        super().__init__(client, name, address, DeviceType.COIL, data_type)


    def read(self, value, data_type: Optional[DataType] = None, word_order: WorderOrder = WorderOrder.BIG, slave = 1) -> Tuple[Union[int, float, str, list[bool], list[int], list[float]], bool]:
        if not data_type and not self._data_type:
            raise ValueError('data type is required')

        if not data_type:
            data_type = self._data_type

        resp = self._client.read_holding_registers(
                address = self.address,
                count = value,
                slave = slave)
        
        # 检查是否读取出错
        if resp.isError():
            # 根据数据类型返回默认值
            if data_type in [DataType.FLOAT32, DataType.FLOAT64]:
                return 0.0, True
            elif data_type == DataType.STRING:
                return "", True
            else:
                return 0, True
        
        # noinspection PyTypeChecker
        return _convert_from_registers(resp.registers, data_type, word_order=word_order.value), False

    def write(self,value: Union[int, float, bool, str, list[bool], list[int], list[float]], data_type: Optional[DataType ]= None, word_order: WorderOrder = WorderOrder.LITTLE, slave = 1) -> bool:
        raise ValueError('input register only support read')
    
