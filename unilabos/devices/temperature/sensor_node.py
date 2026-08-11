import json
import threading,time

# class TempSensorNode:
#     def __init__(self,port,warning,address):
#         self._value = 0.0
#         self.warning = warning
#         self.device_id = address

#         self.hardware_interface = port
#         # t = threading.Thread(target=self.read_temperature, daemon=True)
#         # t.start()

#     def send_command(self ,command):
#         print('send_command---------------------')
#         pass
    
#     @property
#     def value(self) -> float:
#         self._value = self.send_command(self.device_id)
#         return self._value
#     # def read_temperature(self):
#     #     while True:
#     #         self.value = self.send_command(self.device_id) 
#     #         print(self.value,'-----------')
#     #         time.sleep(1)

#     def set_warning(self, warning_temp):
#         self.warning = warning_temp
    


import serial
import struct

class TempSensorNode():
    def __init__(self,port,warning,address,baudrate=9600):
        self._value = 0.0
        self.warning = warning
        self.device_id = address
        self.success = False
    # 配置串口
        self.hardware_interface = serial.Serial(
            port=port,
            baudrate=baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=1
        )
        self.lock = threading.Lock()

    def calculate_crc(self,data):
        crc = 0xFFFF
        for pos in data:
            crc ^= pos
            for i in range(8):
                if (crc & 0x0001) != 0:
                    crc >>= 1
                    crc ^= 0xA001
                else:
                    crc >>= 1
        return crc

    def build_modbus_request(self, device_id, function_code, register_address, register_count):
        request = struct.pack('>BBHH', device_id, function_code, register_address, register_count)
        crc = self.calculate_crc(request)
        request += struct.pack('<H', crc)
        return request

    def read_modbus_response(self,response):
        if len(response) < 5:
            return None
        data = response[:-2]
        crc_received = struct.unpack('<H', response[-2:])[0]
        crc_calculated = self.calculate_crc(data)
        if crc_received == crc_calculated:
            return data[3:]
        return None

    def send_prototype_command(self ,command):
        # with self.lock:
            function_code = 0x04  # 读输入寄存器
            register_address = 0x0003  # 温度高位寄存器地址
            register_count = 0x0002  # 读取两个寄存器
        
            request = self.build_modbus_request(command, function_code, register_address, register_count)
            self.hardware_interface.write(request)
            response = self.hardware_interface.read(9)  # 读取9字节的响应
            data = self.read_modbus_response(response)
            if data is None:
                return None

            high_value = struct.unpack('>H', data[:2])[0]
            low_value = struct.unpack('>H', data[2:])[0]

            # 组合高位和低位并计算实际温度
            raw_temperature = (high_value << 16) | low_value
            if raw_temperature & 0x8000:  # 如果低位寄存器最高位为1，表示负值
                raw_temperature -= 0x10000  # 转换为正确的负数表示

            actual_temperature = raw_temperature / 10.0
            return actual_temperature
    
    @property
    def value(self) -> float:
        self._value = self.send_prototype_command(self.device_id)
        return self._value
    
    def set_warning(self, command):
        self.success = False
        temp = json.loads(command)["warning_temp"]
        self.warning = round(float(temp), 1)
        self.success = True
