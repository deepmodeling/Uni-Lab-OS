import socket
import time
import csv
from datetime import datetime
import threading

csv_lock = threading.Lock()  # 防止多线程写CSV冲突


def scan_once(ip="192.168.1.50", port_in=2001, port_out=2002,
              csv_file="scan_results.csv", timeout=5, retries=3):
    """
    改进版扫码函数：
    - 自动重试
    - 全程超时保护
    - 更安全的socket关闭
    - 文件写入加锁
    """
    def save_result(qrcode):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with csv_lock:
            with open(csv_file, mode="a", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([timestamp, qrcode])
        print(f"✅ 已保存结果: {timestamp}, {qrcode}")

    result = None

    for attempt in range(1, retries + 1):
        print(f"\n🟡 扫码尝试 {attempt}/{retries} ...")

        try:
            # -------- Step 1: 触发拍照 --------
            with socket.create_connection((ip, port_in), timeout=2) as client_in:
                cmd = "start"
                client_in.sendall(cmd.encode("ascii"))  # 把字符串转为byte字节流规则是ascii码
                print(f"→ 已发送触发指令: {cmd}")

            # -------- Step 2: 等待识别结果 --------
            with socket.create_connection((ip, port_out), timeout=timeout) as client_out:
                print(f" 已连接相机输出端口 {port_out}，等待结果...")

                # recv最多阻塞timeout秒
                client_out.settimeout(timeout)
                data = client_out.recv(2048).decode("ascii", errors="ignore").strip()  # 结果输出为ascii字符串，遇到无法解析的字节则忽略
                # .strip()：去掉字符串头尾的空白字符（包括 \n, \r, 空格等），便于后续判断是否为空或写入 CSV。
                if data:
                    print(f"📷 识别结果: {data}")
                    save_result(data)  # 调用 save_result(data) 把时间戳 + 识别字符串写入 CSV（线程安全）。
                    result = data  # 把局部变量 result 设为 data，用于函数返回值
                    break  # 如果读取成功跳出重试循环（for attempt in ...），不再进行后续重试。
                else:
                    print("⚠️ 相机返回空数据，重试中...")

        except socket.timeout:
            print("⏰ 超时未收到识别结果，重试中...")
        except ConnectionRefusedError:
            print("❌ 无法连接到扫码器端口，请检查设备是否在线。")
        except OSError as e:
            print(f"⚠️ 网络错误: {e}")
        except Exception as e:
            print(f"❌ 未知异常: {e}")

        time.sleep(0.5)  # 两次扫描之间稍作延时

    # -------- Step 3: 返回最终结果 --------
    if result:
        print(f"✅ 扫码成功：{result}")
    else:
        print("❌ 多次尝试后仍未获取二维码结果")

    return result
