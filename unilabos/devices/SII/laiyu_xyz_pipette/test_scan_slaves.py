import time
from laiyu_xyz_pipette.drivers.SharedRS485Bus import SharedRS485Bus
from laiyu_xyz_pipette.drivers.XYZModbus import XYZModbus, ModbusException  # 按你的模块名改

def try_read(slave):
    modbus = XYZModbus(bus)
    try:
        vals = modbus.read_regs(slave, 0x00, 6)
        print(f"slave={slave}: OK, vals={vals}")
    except Exception as e:
        print(f"slave={slave}: ERROR -> {e}")

if __name__ == "__main__":
    bus = SharedRS485Bus(port="/dev/ttyUSB0", baudrate=115200)  # 按实际修改
    bus.open()
    try:
        for sid in range(1, 10):
            try_read(sid)
            time.sleep(0.5)
    finally:
        bus.close()