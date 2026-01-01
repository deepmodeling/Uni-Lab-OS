#!/usr/bin/env python3
import time
import json

from cameraUSB import CameraController


def main():
    # 按你的实际情况改
    cfg = dict(
        host_id="demo-host",
        signal_backend_url="wss://sciol.ac.cn/api/realtime/signal/host",
        rtmp_url="rtmp://srs.sciol.ac.cn:4499/live/camera-01",
        webrtc_api="https://srs.sciol.ac.cn/rtc/v1/play/",
        webrtc_stream_url="webrtc://srs.sciol.ac.cn:4500/live/camera-01",
        video_device="/dev/video7",
        width=1280,
        height=720,
        fps=30,
        video_bitrate="1500k",
        audio_device=None,
    )

    c = CameraController(**cfg)

    # 可选：如果你不想依赖 __init__ 自动 start，可以这样显式调用：
    # c = CameraController(host_id=cfg["host_id"])
    # c.start(cfg)

    run_seconds = 30  # 测试运行时长
    t0 = time.time()

    try:
        while True:
            st = c.get_status()
            print(json.dumps(st, ensure_ascii=False, indent=2))

            if time.time() - t0 >= run_seconds:
                break

            time.sleep(2)
    except KeyboardInterrupt:
        print("Interrupted, stopping...")
    finally:
        print("Stopping controller...")
        c.stop()
        print("Done.")


if __name__ == "__main__":
    main()