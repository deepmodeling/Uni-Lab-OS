#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
使用 CameraController 来测试 PTZ：
让摄像头按顺序向下、向上、向左、向右运动几次。
"""

import time
import sys

# 根据你的工程结构修改导入路径：
# 假设 CameraController 定义在 cameraController.py 里
from cameraDriver import CameraController


def main():
    # === 根据你的实际情况填 IP、端口、账号密码 ===
    ptz_host = "192.168.31.164"
    ptz_port = 2020          # 注意要和你单独测试 PTZController 时保持一致
    ptz_user = "admin"
    ptz_password = "admin123"

    # 1. 创建 CameraController 实例
    cam = CameraController(
        # 其他摄像机相关参数按你类的 __init__ 来补充
        ptz_host=ptz_host,
        ptz_port=ptz_port,
        ptz_user=ptz_user,
        ptz_password=ptz_password,
    )

    # 2. 启动 / 初始化（如果你的 CameraController 有 start(config) 之类的接口）
    #    这里给一个最小的 config，重点是 PTZ 相关字段
    config = {
        "ptz_host": ptz_host,
        "ptz_port": ptz_port,
        "ptz_user": ptz_user,
        "ptz_password": ptz_password,
    }

    try:
        cam.start(config)
    except Exception as e:
        print(f"[TEST] CameraController start() 失败: {e}", file=sys.stderr)
        return

    # 这里可以判断一下内部 _ptz 是否初始化成功（如果你对 CameraController 做了封装）
    if getattr(cam, "_ptz", None) is None:
        print("[TEST] CameraController 内部 PTZ 未初始化成功，请检查 ptz_host/port/user/password 配置。", file=sys.stderr)
        return

    # 3. 依次调用 CameraController 的 PTZ 方法
    #    这里假设你在 CameraController 中提供了这几个对外方法：
    #    ptz_move_down / ptz_move_up / ptz_move_left / ptz_move_right
    #    如果你命名不一样，把下面调用名改成你的即可。

    print("向下移动（通过 CameraController）...")
    cam.ptz_move_down(speed=0.5, duration=1.0)
    time.sleep(1)

    print("向上移动（通过 CameraController）...")
    cam.ptz_move_up(speed=0.5, duration=1.0)
    time.sleep(1)

    print("向左移动（通过 CameraController）...")
    cam.ptz_move_left(speed=0.5, duration=1.0)
    time.sleep(1)

    print("向右移动（通过 CameraController）...")
    cam.ptz_move_right(speed=0.5, duration=1.0)
    time.sleep(1)

    print("测试结束。")


if __name__ == "__main__":
    main()