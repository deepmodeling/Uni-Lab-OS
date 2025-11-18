import time
import csv
from datetime import datetime
from pymodbus.client import ModbusTcpClient
from dmqfengzhuang import scan_once
from raman_module import run_raman_test

# =================== 配置 ===================
PLC_IP = "192.168.1.88"
PLC_PORT = 502
ROBOT_IP = "192.168.1.200"
ROBOT_PORT = 502
SCAN_CSV_FILE = "scan_results.csv"

# =================== 通用函数 ===================
def ensure_connected(client, name, ip, port):
    if not client.is_socket_open():
        print(f"{name} 掉线，正在重连...")
        client.close()
        time.sleep(1)

        new_client = ModbusTcpClient(ip, port=port)
        if new_client.connect():
            print(f"{name} 重新连接成功 ({ip}:{port})")
            return new_client
        else:
            print(f"{name} 重连失败，稍后重试...")
            time.sleep(3)
            return None
    return client

def safe_read(client, name, func, *args, retries=3, delay=0.3, **kwargs):
    for _ in range(retries):
        try:
            res = func(*args, **kwargs)
            if res and not (hasattr(res, "isError") and res.isError()):
                return res
        except Exception as e:
            print(f"{name} 读异常: {e}")
        time.sleep(delay)
    print(f"{name} 连续读取失败 {retries} 次")
    return None

def safe_write(client, name, func, *args, retries=3, delay=0.3, **kwargs):
    for _ in range(retries):
        try:
            res = func(*args, **kwargs)
            if res and not (hasattr(res, "isError") and res.isError()):
                return True
        except Exception as e:
            print(f"{name} 写异常: {e}")
        time.sleep(delay)
    print(f"{name} 连续写入失败 {retries} 次")
    return False

def wait_with_quit_check(robot, seconds, addr_quit=270):
    for _ in range(int(seconds / 0.2)):
        rr = safe_read(robot, "机器人", robot.read_holding_registers,
                       address=addr_quit, count=1)
        if rr and rr.registers[0] == 1:
            print("检测到 R270=1，立即退出循环")
            return True
        time.sleep(0.2)
    return False

# =================== 初始化 ===================
plc = ModbusTcpClient(PLC_IP, port=PLC_PORT)
robot = ModbusTcpClient(ROBOT_IP, port=ROBOT_PORT)

if not plc.connect():
    print("无法连接 PLC")
    exit()
if not robot.connect():
    print("无法连接 机器人")
    plc.close()
    exit()

print("✅ PLC 与 机器人连接成功")
time.sleep(0.5)

# 伺服使能
if safe_write(plc, "PLC", plc.write_coil, address=10, value=True):
    print("✅ 伺服使能成功 (M10=True)")
else:
    print("⚠️ 伺服使能失败")

# 初始化扫码 CSV
with open(SCAN_CSV_FILE, "w", newline="", encoding="utf-8") as f:
    csv.writer(f).writerow(["Bottle_No", "Scan_Result", "Time"])

bottle_count = 0
print("🟢 等待机器人触发信号... (R260=1扫码 / R256=1拉曼 / R270=1退出)")

# =================== 主监听循环 ===================
while True:
    plc = ensure_connected(plc, "PLC", PLC_IP, PLC_PORT) or plc
    robot = ensure_connected(robot, "机器人", ROBOT_IP, ROBOT_PORT) or robot

    # 退出命令检测
    quit_signal = safe_read(robot, "机器人", robot.read_holding_registers,
                            address=270, count=1)
    if quit_signal and quit_signal.registers[0] == 1:
        print("🟥 检测到 R270=1，准备退出程序...")
        break

    # 读取关键寄存器
    rr = safe_read(robot, "机器人", robot.read_holding_registers,
                   address=256, count=5)
    if not rr:
        time.sleep(0.3)
        continue

    r256, _, r258, r259, r260 = rr.registers[:5]

    # ----------- 扫码部分 (R260=1) -----------
    if r260 == 1:
        bottle_count += 1
        print(f"📸 第 {bottle_count} 瓶触发扫码 (R260=1)")

        try:
            result = scan_once(ip="192.168.1.50", port_in=2001, port_out=2002)
            if result:
                print(f"✅ 扫码成功: {result}")
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                with open(SCAN_CSV_FILE, "a", newline="", encoding="utf-8") as f:
                    csv.writer(f).writerow([bottle_count, result, timestamp])
            else:
                print("⚠️ 扫码失败或无返回")
        except Exception as e:
            print(f"❌ 扫码异常: {e}")

        safe_write(robot, "机器人", robot.write_register, address=260, value=0)
        time.sleep(0.2)
        safe_write(robot, "机器人", robot.write_register, address=261, value=1)
        print("➡️ 扫码完成 (R260→0, R261→1)")

    # ----------- 拉曼 + 电机部分 (R256=1) -----------
    if r256 == 1:
        print("⚙️ 检测到 R256=1（放瓶完成）")

        # 电机右转
        safe_write(plc, "PLC", plc.write_register, address=1199, value=1)
        safe_write(plc, "PLC", plc.write_register, address=1200, value=1)
        print("➡️ 电机右转中...")
        if wait_with_quit_check(robot, 3):
            break
        safe_write(plc, "PLC", plc.write_register, address=1199, value=0)
        print("✅ 电机右转完成")

        # 拉曼测试
        print("🧪 开始拉曼测试...")
        try:
            success, file_prefix, df = run_raman_test(
                integration_time=5000,
                laser_power=200,
                save_csv=True,
                save_plot=True,
                normalize=True,
                norm_max=1.0
            )
            if success:
                print(f"✅ 拉曼完成：{file_prefix}.csv / .png")
            else:
                print("⚠️ 拉曼失败")
        except Exception as e:
            print(f"❌ 拉曼测试异常: {e}")

        # 电机左转
        safe_write(plc, "PLC", plc.write_register, address=1299, value=1)
        safe_write(plc, "PLC", plc.write_register, address=1300, value=1)
        print("⬅️ 电机左转中...")
        if wait_with_quit_check(robot, 3):
            break
        safe_write(plc, "PLC", plc.write_register, address=1299, value=0)
        print("✅ 电机左转完成")

        # 写入拉曼完成信号
        safe_write(robot, "机器人", robot.write_register, address=257, value=1)
        print("✅ 已写入 R257=1（拉曼完成）")

        # 延迟清零 R256
        print("⏳ 延迟4秒后清零 R256")
        if wait_with_quit_check(robot, 4):
            break
        safe_write(robot, "机器人", robot.write_register, address=256, value=0)
        print("✅ 已清零 R256")

        # 等待机器人清零 R257
        print("等待 R257 清零中...")
        while True:
            rr2 = safe_read(robot, "机器人", robot.read_holding_registers, address=257, count=1)
            if rr2 and rr2.registers[0] == 0:
                print("✅ 检测到 R257=0，准备下一循环")
                break
            if wait_with_quit_check(robot, 1):
                break
            time.sleep(0.2)

    time.sleep(0.2)

# =================== 程序退出清理 ===================
print("🧹 开始清理...")
safe_write(plc, "PLC", plc.write_coil, address=10, value=False)
for addr in [256, 257, 260, 261, 270]:
    safe_write(robot, "机器人", robot.write_register, address=addr, value=0)

plc.close()
robot.close()
print("✅ 程序已退出，设备全部复位。")
