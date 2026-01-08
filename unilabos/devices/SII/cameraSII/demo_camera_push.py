# run_camera_push.py
import time
from cameraWifi import CameraController  # 这里根据你的文件名调整

if __name__ == "__main__":
    controller = CameraController(
        host_id="demo-host",
        signal_backend_url="wss://sciol.ac.cn/api/realtime/signal/host",
        rtmp_url="rtmp://srs.sciol.ac.cn:4499/live/camera-01",
        webrtc_api="https://srs.sciol.ac.cn/rtc/v1/play/",
        webrtc_stream_url="webrtc://srs.sciol.ac.cn:4500/live/camera-01",
        camera_rtsp_url="rtsp://admin:admin123@192.168.31.164:554/stream1",
    )

    try:
        while True:
            status = controller.get_status()
            print(status)
            time.sleep(5)
    except KeyboardInterrupt:
        controller.stop()