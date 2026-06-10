import modbus_tk.modbus_tcp as modbus_tcp
import modbus_tk.defines as cst
from modbus_tk.modbus import Slave

# 创建一个 Modbus TCP 服务器
server = modbus_tcp.TcpServer(
    address="localhost", port=5020, timeout_in_sec=1
)

# 设置服务器的地址和端口
# server.set_address("localhost", 5020)  # 监听在本地端口5020

# 添加一个从设备，Slave ID 设为 1
slave: Slave = server.add_slave(1)


# 向从设备添加一个保持寄存器块，假设从地址0开始，10个寄存器
# def add_block(self, block_name, block_type, starting_address, size)
# slave.add_block('0', cst.HOLDING_REGISTERS, 0, 10)

# 添加一个线圈
# 0 名字， 从 16 字节内存位置开始，分配连续两个字节的内存大小，注意地址只能是 8 的整数倍
slave.add_block('0', cst.COILS, 2*8, 2)

# 1 名字，100 起始地址， 8 是从 100 的位置分配 8 个字节内存， 两个线圈的量
slave.add_block('1', cst.HOLDING_REGISTERS, 100, 8)
slave.add_block('2', cst.HOLDING_REGISTERS, 200, 16)

# slave.add_block('2', cst.DISCRETE_INPUTS , 200, 2)
# slave.add_block('3', cst.ANALOG_INPUTS , 300, 2)
# 启动服务器
server.start()

print("Modbus TCP server running on localhost:5020")

# 保持服务器运行，直到按下 Ctrl+C
try:
    while True:
        pass
except KeyboardInterrupt:
    server.stop()
    print("Server stopped.")
