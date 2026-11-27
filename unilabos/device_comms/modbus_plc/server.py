import modbus_tk.defines as cst
import modbus_tk.modbus_tcp as modbus_tcp

# 创建一个 Modbus TCP 服务器
server = modbus_tcp.TcpServer(
	address="127.0.0.1", port=5021, timeout_in_sec=1
)

# 添加一个从设备 (slave)
slave_id = 1
slave = server.add_slave(slave_id)

# 为从设备分配地址空间，例如保持寄存器 (holding registers)
# 假设地址范围为 7000 到 7100，对应客户端M01_idlepos_velocity_rw
slave.add_block('hr', cst.HOLDING_REGISTERS, 7000, 100)
slave.add_block('coil_block', cst.COILS, 56000, 1000)


# 初始化地址 56488 和 56432 的值为 True
slave.set_values('coil_block', 56488, [True])  # Coil 56488 设置为 True
slave.set_values('coil_block', 56432, [True])  # Coil 56432 设置为 True

slave.set_values('coil_block', 56496, [True])  # Coil 56488 设置为 True
slave.set_values('coil_block', 56432, [True])  # Coil 56432 设置为 True


# slave.add_block('hr', cst.COILS, 7000, 100)
server.start()
print("Modbus TCP server running on localhost:5021")

# 保持服务器运行，直到按下 Ctrl+C
try:
    while True:
        pass
except KeyboardInterrupt:
    server.stop()
    print("Server stopped.")
