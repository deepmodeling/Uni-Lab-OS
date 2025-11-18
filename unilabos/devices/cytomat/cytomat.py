import serial
import time

ser = serial.Serial(
    port="COM18",         
    baudrate=9600,
    bytesize=serial.EIGHTBITS,
    parity=serial.PARITY_NONE,
    stopbits=serial.STOPBITS_ONE,
    timeout=15,

def send_cmd(cmd: str, wait: float = 1.0) -> str:
    """向 Cytomat 发送一行命令并打印/返回响应。"""
    print(f">>> {cmd}")
    ser.write((cmd + "\r").encode("ascii"))
    time.sleep(wait)
    resp = ser.read_all().decode("ascii", errors="ignore").strip()
    print(f"<<< {resp or '<no response>'}")
    return resp

def initialize():
    """设备初始化 (ll:in)。"""
    return send_cmd("ll:in")

def wp_to_storage(pos: int):
    """WP → 库位。pos: 1–9999 绝对地址。"""
    return send_cmd(f"mv:ws {pos:04d}")

def storage_to_tfs(stacker: int, level: int):
    """库位 → TFS1。"""
    return send_cmd(f"mv:st {stacker:02d} {level:02d}")

def get_basic_state():
    """查询 Basic State Register。"""
    return send_cmd("ch:bs")

def set_pitch(stacker: int, pitch_mm: int):
    """设置单个 stacker 的层间距（mm）。"""
    return send_cmd(f"se:cs {stacker:02d} {pitch_mm}")

def tfs_to_storage(stacker: int, level: int):
    """TFS1 → 库位。"""
    return send_cmd(f"mv:ts {stacker:02d} {level:02d}")

# ---------- 示例工作流 ----------
if __name__ == "__main__":
    try:
        if not ser.is_open:
            ser.open()
        initialize()
        wp_to_storage(10)
        storage_to_tfs(17, 3)
        get_basic_state()
        tfs_to_storage(7, 5)

    except Exception as exc:
        print("Error:", exc)
    finally:
        ser.close()
        print("Done.")

