#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
测试 cameraDriver.py中的 PTZController 类，让摄像头按顺序运动几次
"""

import time

from cameraDriver import PTZController


def main():
    # 根据你的实际情况填 IP、端口、账号密码
    host = "192.168.31.164"
    port = 80
    user = "admin"
    password = "admin123"

    ptz = PTZController(host=host, port=port, user=user, password=password)

    # 1. 连接摄像头
    if not ptz.connect():
        print("连接 PTZ 失败，检查 IP/用户名/密码/端口。")
        return

    # 2. 依次测试几个动作
    # 每个动作之间 sleep 一下方便观察

    print("向下移动...")
    ptz.move_down(speed=0.5, duration=1.0)
    time.sleep(1)

    print("向上移动...")
    ptz.move_up(speed=0.5, duration=1.0)
    time.sleep(1)

    print("向左移动...")
    ptz.move_left(speed=0.5, duration=1.0)
    time.sleep(1)

    print("向右移动...")
    ptz.move_right(speed=0.5, duration=1.0)
    time.sleep(1)

    print("测试结束。")


if __name__ == "__main__":
    main()