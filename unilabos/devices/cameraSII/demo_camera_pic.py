import cv2

# 推荐把 @ 进行 URL 编码：@ -> %40
RTSP_URL = "rtsp://admin:admin123@192.168.31.164:554/stream1"
OUTPUT_IMAGE = "rtsp_test_frame.jpg"

def main():
    print(f"尝试连接 RTSP 流: {RTSP_URL}")
    cap = cv2.VideoCapture(RTSP_URL)

    if not cap.isOpened():
        print("错误：无法打开 RTSP 流，请检查：")
        print("  1. IP/端口是否正确")
        print("  2. 账号密码（尤其是 @ 是否已转成 %40）是否正确")
        print("  3. 摄像头是否允许当前主机访问（同一网段、防火墙等）")
        return

    print("连接成功，开始读取一帧...")
    ret, frame = cap.read()

    if not ret or frame is None:
        print("错误：已连接但未能读取到帧数据（可能是码流未开启或网络抖动）")
        cap.release()
        return

    # 保存当前帧
    success = cv2.imwrite(OUTPUT_IMAGE, frame)
    cap.release()

    if success:
        print(f"成功截取一帧并保存为: {OUTPUT_IMAGE}")
    else:
        print("错误：写入图片失败，请检查磁盘权限/路径")

if __name__ == "__main__":
    main()