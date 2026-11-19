"""Uni-Lab-OS 的 dora 通信中间件后端。

以 dora-rs（Apache Arrow + 共享内存）作为设备节点之间的通信中间件，
提供与 ROS2 后端等价的「设备状态发布 + 命令下发」能力，用于本机通信性能验证。

对外入口：
    - unilabos.backend.dora.main_dora_run.main / slave  —— 与 app/backend.py:start_backend 对接
"""
