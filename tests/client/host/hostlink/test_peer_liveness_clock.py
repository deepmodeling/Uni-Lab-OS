"""墙钟跳变（NTP 步进校时、Windows/WSL 对时、休眠唤醒）下 Slave 在线判定的回归。

只改写墙钟（``time.time``），单调时钟照常走：在线与否只能由心跳之后真实经过的时间决定。
"""

import time

from unilabos.client.host.hostlink.protocol import ActionType
from unilabos.client.host.hostlink.server import HostLinkServer


def _jump_wall_clock(monkeypatch, offset_s: float) -> None:
    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time() + offset_s)


def _slave(heartbeat_timeout: float) -> HostLinkServer:
    server = HostLinkServer("127.0.0.1", 0, heartbeat_timeout=heartbeat_timeout)
    server._touch_peer(
        "connection-a",
        ActionType.HELLO,
        {"machine_name": "worker", "device_ids": ["pump"]},
    )
    return server


def test_forward_wall_clock_jump_keeps_live_slave_online(monkeypatch) -> None:
    server = _slave(heartbeat_timeout=1.0)
    # 实测：墙钟每 31 s 先回拨约 12 s、再前跳约 13 s；前跳不代表心跳间隔真的变长
    _jump_wall_clock(monkeypatch, 13.0)

    peer = server.peers()[0]
    assert peer["online"] is True
    assert server.has_device("pump")


def test_backward_wall_clock_jump_still_expires_silent_slave(monkeypatch) -> None:
    server = _slave(heartbeat_timeout=0.2)
    _jump_wall_clock(monkeypatch, -12.0)
    # 单调时钟越过心跳超时：回拨的墙钟不能让没有心跳的 Slave 继续在线
    time.sleep(0.3)

    assert server.peers()[0]["online"] is False
    assert not server.has_device("pump")


def test_heartbeat_after_wall_clock_jump_refreshes_liveness(monkeypatch) -> None:
    server = _slave(heartbeat_timeout=0.5)
    _jump_wall_clock(monkeypatch, -12.0)
    time.sleep(0.6)
    assert server.peers()[0]["online"] is False

    server._touch_peer("connection-a", ActionType.PING, {})
    peer = server.peers()[0]
    assert peer["online"] is True
    # 墙钟时间戳仍按当前（已回拨的）墙钟记录，只用于展示
    assert abs(peer["last_seen"] - time.time()) < 5.0
