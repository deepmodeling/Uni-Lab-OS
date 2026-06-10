import time
from pymodbus.client import ModbusTcpClient
from unilabos.device_comms.modbus_plc.modbus import Coil, HoldRegister
from pymodbus.payload import BinaryPayloadDecoder
from pymodbus.constants import Endian

# client = ModbusTcpClient('localhost', port=5020)
client = ModbusTcpClient('192.168.3.2', port=502)

client.connect()


coil1 = Coil(client=client, name='coil_test1', data_type=bool, address=4502*8)
coil1.write(True)
time.sleep(3)
coil1.write(False)


coil1 = Coil(client=client, name='coil_test1', data_type=bool, address=4503*8)
coil1.write(True)
time.sleep(3)
coil1.write(False)


exit(0)


register1 = HoldRegister(client=client, name="test-1", address=7040)


coil1 = Coil(client=client, name='coil_test1', address=7002*8)


coil1.write(True)

while True:
    # result = client.read_holding_registers(address=7040, count=2, slave=1)  # unit=1 是从站地址
    result = register1.read(2, slave=1)
    if result.isError():
        print("读取失败")
    else:
        print("读取成功:", result.registers)
        decoder = BinaryPayloadDecoder.fromRegisters(
            result.registers, byteorder=Endian.BIG, wordorder=Endian.LITTLE
        )
        real_value = decoder.decode_32bit_float()
        print("这里的值是: ", real_value)
        if real_value > 42:
            coil1.write(False)
            break
    time.sleep(1)


# # 创建 Modbus TCP 客户端，连接到本地模拟的服务器
# client = ModbusClient('localhost', port=5020)

# # 连接到服务器

# # 读取保持寄存器（地址 0，读取 10 个寄存器）
# # address: int,
# # *,
# # count: int = 1,
# # slave: int = 1,
# response = client.read_holding_registers(
# 	address=0, count=10, slave=1
# )

# response = coil1.read(2, slave=1)
#
# if response.isError():
#     print(f"Error reading registers: {response}")
# else:
#     print(f"Read holding registers: {response.bits}")
#
# coil1.write(1, slave=1)
# print("Wrote value 1234 to holding register 0")
#
# response = coil1.read(2, slave=1)
# if response.isError():
#     print(f"Error reading registers: {response}")
# else:
#     print(f"Read holding registers after write: {response.bits}")
#
#
# if response.isError():
#     print(f"Error reading registers: {response}")
# else:
    # print(f"Read holding registers after write: {response.bits}")

client.close()

# # 写入保持寄存器（地址 0，值为 1234）
# client.write_register(0, 1234, slave=1)
# print("Wrote value 1234 to holding register 0")

# # 再次读取寄存器，确认写入成功
# response = client.read_holding_registers(address=0, count=10, slave=1)
# if response.isError():
#     print(f"Error reading registers: {response}")
# else:
#     print(f"Read holding registers after write: {response.registers}")

# # 关闭连接
# client.close()
