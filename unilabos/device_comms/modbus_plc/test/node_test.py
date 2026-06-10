# coding=utf-8
from pymodbus.client import ModbusTcpClient
from unilabos.device_comms.modbus_plc.modbus import Coil
import time


client = ModbusTcpClient('192.168.3.2', port=502)
client.connect()

coil1 = Coil(client=client, name='0', address=7012*8)

coil2 = Coil(client=client, name='0', address=7062*8)
coil3 = Coil(client=client, name='0', address=7054*8)


while True:
    time.sleep(1)
    resp, isError = coil2.read(1)
    resp1, isError = coil3.read(1)
    print(resp[0], resp1[0])


# hr = HoldRegister(client, '1', 100)
# resp = hr.write([666.3, 777.4], data_type=DATATYPE.FLOAT32, word_order=WORDORDER.BIG)
# print('write ===== hr1', resp)
# time.sleep(1)
# h_resp = hr.read(4, data_type=DATATYPE.FLOAT32, word_order=WORDORDER.BIG)
# print('=======hr1', h_resp)
#
#
# resp = hr.write([666, 777], data_type=DATATYPE.INT32, word_order=WORDORDER.BIG)
# print('write ===== hr1', resp)
# time.sleep(1)
# h_resp = hr.read(4, data_type=DATATYPE.INT32, word_order=WORDORDER.BIG)
# print('=======hr1', h_resp)
#
#
# resp = hr.write('hello world!', data_type=DATATYPE.STRING, word_order=WORDORDER.BIG)
# print('write ===== hr1', resp)
# time.sleep(1)
# h_resp = hr.read(12, data_type=DATATYPE.STRING, word_order=WORDORDER.BIG)
# print('=======hr1', h_resp)
