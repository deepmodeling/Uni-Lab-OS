import serial
import time
import pandas as pd


class Laiyu:
    @property
    def status(self) -> str:
        return ""

    def __init__(self, port, baudrate=115200, timeout=0.5):
        """
        初始化串口参数，默认波特率115200，8位数据位、1位停止位、无校验
        """
        self.ser = serial.Serial(port, baudrate=baudrate, timeout=timeout)

    def calculate_crc(self, data: bytes) -> bytes:
        """
        计算Modbus CRC-16，返回低字节和高字节（little-endian）
        """
        crc = 0xFFFF
        for pos in data:
            crc ^= pos
            for _ in range(8):
                if crc & 0x0001:
                    crc = (crc >> 1) ^ 0xA001
                else:
                    crc >>= 1
        return crc.to_bytes(2, byteorder='little')

    def send_command(self, command: bytes) -> bytes:
        """
        构造完整指令帧（加上CRC校验），发送指令后一直等待设备响应，直至响应结束或超时（最大3分钟）
        """
        crc = self.calculate_crc(command)
        full_command = command + crc
        # 清空接收缓存
        self.ser.reset_input_buffer()
        self.ser.write(full_command)
        print("发送指令：", full_command.hex().upper())  # 打印发送的指令帧

        # 持续等待响应，直到连续0.5秒没有新数据或超时（3分钟）
        start_time = time.time()
        last_data_time = time.time()
        response = bytearray()
        while True:
            if self.ser.in_waiting > 0:
                new_data = self.ser.read(self.ser.in_waiting)
                response.extend(new_data)
                last_data_time = time.time()
            # 如果已有数据，并且0.5秒内无新数据，则认为响应结束
            if response and (time.time() - last_data_time) > 0.5:
                break
            # 超过最大等待时间，退出循环
            if time.time() - start_time > 180:
                break
            time.sleep(0.1)
        return bytes(response)

    def pick_powder_tube(self, int_input: int) -> bytes:
        """
        拿取粉筒指令：
         - 功能码06
         - 寄存器地址0x0037（取粉筒）
         - 数据：粉筒编号（如1代表A，2代表B，以此类推）
        示例：拿取A粉筒指令帧：01 06 00 37 00 01 + CRC
        """
        slave_addr = 0x01
        function_code = 0x06
        register_addr = 0x0037
        # 数据部分：粉筒编号转换为2字节大端
        data = int_input.to_bytes(2, byteorder='big')
        command = bytes([slave_addr, function_code]) + register_addr.to_bytes(2, byteorder='big') + data
        return self.send_command(command)

    def put_powder_tube(self, int_input: int) -> bytes:
        """
        放回粉筒指令：
         - 功能码06
         - 寄存器地址0x0038（放回粉筒）
         - 数据：粉筒编号
        示例：放回A粉筒指令帧：01 06 00 38 00 01 + CRC
        """
        slave_addr = 0x01
        function_code = 0x06
        register_addr = 0x0038
        data = int_input.to_bytes(2, byteorder='big')
        command = bytes([slave_addr, function_code]) + register_addr.to_bytes(2, byteorder='big') + data
        return self.send_command(command)

    def reset(self) -> bytes:
        """
        重置指令：
         - 功能码   0x06
         - 寄存器地址 0x0042 （示例中用了 00 42）
         - 数据     0x0001
        示例发送：01 06 00 42 00 01 E8 1E
        """
        slave_addr = 0x01
        function_code = 0x06
        register_addr = 0x0042               # 对应示例中的 00 42
        payload = (0x0001).to_bytes(2, 'big')  # 重置命令

        cmd = (
            bytes([slave_addr, function_code])
            + register_addr.to_bytes(2, 'big')
            + payload
        )
        return self.send_command(cmd)

    def move_to_xyz(self, x: float, y: float, z: float) -> bytes:
        """
        移动到指定位置指令：
         - 功能码10（写多个寄存器）
         - 寄存器起始地址0x0030
         - 寄存器数量：3个（x,y,z）
         - 字节计数：6
         - 数据：x,y,z各2字节，单位为0.1mm（例如1mm对应数值10）
        示例帧：01 10 00 30 00 03 06 00C8 02BC 02EE + CRC
        """
        slave_addr = 0x01
        function_code = 0x10
        register_addr = 0x0030
        num_registers = 3
        byte_count = num_registers * 2  # 6字节

        # 将mm转换为0.1mm单位（乘以10），转换为2字节大端表示
        x_val = int(x * 10)
        y_val = int(y * 10)
        z_val = int(z * 10)
        data = x_val.to_bytes(2, 'big') + y_val.to_bytes(2, 'big') + z_val.to_bytes(2, 'big')

        command = (bytes([slave_addr, function_code]) +
                   register_addr.to_bytes(2, 'big') +
                   num_registers.to_bytes(2, 'big') +
                   byte_count.to_bytes(1, 'big') +
                   data)
        return self.send_command(command)

    def discharge(self, float_in: float) -> bytes:
        """
        出料指令：
        - 使用写多个寄存器命令（功能码 0x10）
        - 寄存器起始地址设为 0x0039
        - 寄存器数量为 0x0002（两个寄存器：出料质量和误差范围）
        - 字节计数为 0x04（每个寄存器2字节，共4字节）
        - 数据：出料质量（单位0.1mg，例如10mg对应100，即0x0064）、误差范围固定为0x0005
        示例发送帧：01 10 00 39 0002 04 00640005 + CRC
        """
        mass = float_in
        slave_addr = 0x01
        function_code = 0x10            # 修改为写多个寄存器的功能码
        start_register = 0x0039         # 寄存器起始地址
        quantity = 0x0002               # 寄存器数量
        byte_count = 0x04               # 字节数：2寄存器*2字节=4
        mass_val = int(mass * 10)       # 质量转换，单位0.1mg
        error_margin = 5                # 固定误差范围，0x0005

        command = (bytes([slave_addr, function_code]) +
                   start_register.to_bytes(2, 'big') +
                   quantity.to_bytes(2, 'big') +
                   byte_count.to_bytes(1, 'big') +
                   mass_val.to_bytes(2, 'big') +
                   error_margin.to_bytes(2, 'big'))
        return self.send_command(command)

    '''
    示例：这个是标智96孔板的坐标转换，但是不同96孔板的坐标可能不同
    所以需要根据实际情况进行修改
    '''

    def move_to_plate(self, string):
        # 只接受两位数的str，比如a1，a2，b1，b2
        # 解析位置字符串
        if len(string) != 2 and len(string) != 3:
            raise ValueError("Invalid plate position")
        if not string[0].isalpha() or not string[1:].isdigit():
            raise ValueError("Invalid plate position")
        a = string[0]  # 字母部分s
        b = string[1:]  # 数字部分

        if a.isalpha():
            a = ord(a.lower()) - ord('a') + 1
        else:
            print('1')
            raise ValueError("Invalid plate position")
        a = int(a)
        b = int(b)
        # max a = 8, max b = 12, 否则报错
        if a > 8 or b > 12:
            print('2')
            raise ValueError("Invalid plate position")
        # 计算移动到指定位置的坐标
        #  a=1, x=3.0; a=12, x=220.0
        #  b=1, y=62.0; b=8, y=201.0
        # z = 110.0
        x = float((b-1) * (220-4.0)/11 + 4.0)
        y = float((a-1) * (201.0-62.0)/7 + 62.0)
        z = 110.0
        # 移动到指定位置
        resp_move = self.move_to_xyz(x, y, z)
        print("移动位置响应：", resp_move.hex().upper())
        # 打印移动到指定位置的坐标
        print(f"移动到位置：{string}，坐标：x={x:.2f}, y={y:.2f}, z={z:.2f}")
        return resp_move

    def add_powder_tube(self, powder_tube_number, target_tube_position, compound_mass):
        # 拿取粉筒
        resp_pick = self.pick_powder_tube(powder_tube_number)
        print("拿取粉筒响应：", resp_pick.hex().upper())
        time.sleep(1)
        # 移动到指定位置
        self.move_to_plate(target_tube_position)
        time.sleep(1)
        # 出料，设定质量
        resp_discharge = self.discharge(compound_mass)
        print("出料响应：", resp_discharge.hex().upper())
        # 使用modbus协议读取实际出料质量
        # 样例 01 06 00 40 00 64 89 F5，其中 00 64 是实际出料质量，换算为十进制为100，代表10 mg
        # 从resp_discharge读取实际出料质量
        # 提取字节4和字节5的两个字节
        actual_mass_raw = int.from_bytes(resp_discharge[4:6], byteorder='big')
        # 根据说明，将读取到的数据转换为实际出料质量（mg），这里除以10，例如：0x0064 = 100，转换后为10 mg
        actual_mass_mg = actual_mass_raw / 10
        print(f"孔位{target_tube_position}，实际出料质量：{actual_mass_mg}mg")
        time.sleep(1)
        # 放回粉筒
        resp_put = self.put_powder_tube(powder_tube_number)
        print("放回粉筒响应：", resp_put.hex().upper())
        print(f"放回粉筒{powder_tube_number}")
        resp_reset = self.reset()
        return actual_mass_mg


if __name__ == "__main__":

    '''
    样例：对单个粉筒进行称量
    '''

    modbus = Laiyu(port="COM25")

    mass_test = modbus.add_powder_tube(1, 'h12', 6.0)
    print(f"实际出料质量：{mass_test}mg")

    '''
    样例: 对一份excel文件记录的化合物进行称量
    '''

    excel_file = r"C:\auto\laiyu\test1.xlsx"
    # 定义输出文件路径，用于记录实际加样多少
    output_file = r"C:\auto\laiyu\test_output.xlsx"

    # 定义物料名称和料筒位置关系
    compound_positions = {
        'XPhos': '1',
        'Cu(OTf)2': '2',
        'CuSO4': '3',
        'PPh3': '4',
    }

    # read excel file
    # excel_file = r"C:\auto\laiyu\test.xlsx"
    df = pd.read_excel(excel_file, sheet_name='Sheet1')
    # 读取Excel文件中的数据
    # 遍历每一行数据
    for index, row in df.iterrows():
        # 获取物料名称和质量
        copper_name = row['copper']
        copper_mass = row['copper_mass']
        ligand_name = row['ligand']
        ligand_mass = row['ligand_mass']
        target_tube_position = row['position']
        # 获取物料位置 from compound_positions
        copper_position = compound_positions.get(copper_name)
        ligand_position = compound_positions.get(ligand_name)
        # 判断物料位置是否存在
        if copper_position is None:
            print(f"物料位置不存在：{copper_name}")
            continue
        if ligand_position is None:
            print(f"物料位置不存在：{ligand_name}")
            continue
        # 加铜
        copper_actual_mass = modbus.add_powder_tube(int(copper_position), target_tube_position, copper_mass)
        time.sleep(1)
        # 加配体
        ligand_actual_mass = modbus.add_powder_tube(int(ligand_position), target_tube_position, ligand_mass)
        time.sleep(1)
        # 保存至df
        df.at[index, 'copper_actual_mass'] = copper_actual_mass
        df.at[index, 'ligand_actual_mass'] = ligand_actual_mass

    # 保存修改后的数据到新的Excel文件
    df.to_excel(output_file, index=False)
    print(f"已保存到文件：{output_file}")

    # 关闭串口
    modbus.ser.close()
    print("串口已关闭")
