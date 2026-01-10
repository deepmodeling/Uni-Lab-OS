# coding=utf-8
from enum import Enum
from abc import ABC, abstractmethod

from pymodbus.client import ModbusBaseSyncClient
from pymodbus.client.mixin import ModbusClientMixin 
from typing import Tuple, Union, Optional

DataType = ModbusClientMixin.DATATYPE

class WorderOrder(Enum):
    BIG = "big"
    LITTLE = "little"

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
        resp =  self._client.read_coils(
                address = self.address,
                count = value,
                slave = slave)

        return resp.bits, resp.isError()

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

        # noinspection PyTypeChecker
        return self._client.convert_from_registers(resp.registers, data_type, word_order=word_order.value), resp.isError()

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
        # noinspection PyTypeChecker
        return self._client.convert_from_registers(resp.registers, data_type, word_order=word_order.value), resp.isError()


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
            encoder_resp = self._client.convert_to_registers(value, data_type=data_type, word_order=word_order.value)
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
        # noinspection PyTypeChecker
        return self._client.convert_from_registers(resp.registers, data_type, word_order=word_order.value), resp.isError()

    def write(self,value: Union[int, float, bool, str, list[bool], list[int], list[float]], data_type: Optional[DataType ]= None, word_order: WorderOrder = WorderOrder.LITTLE, slave = 1) -> bool:
        raise ValueError('input register only support read')
    
