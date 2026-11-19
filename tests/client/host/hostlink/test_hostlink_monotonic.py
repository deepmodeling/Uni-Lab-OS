"""HostLink 包内只在本进程计时的时长与期限：墙钟跳变不影响。

覆盖设备定时器、Workstation 协议编排耗时和 doctor 的运行时长。只改写墙钟
（``time.time`` / ``time.time_ns``），单调时钟照常走；doctor 的测试另把单调时钟换成
只随 ``time.sleep`` 前进的假时钟，循环跑几轮只取决于时长与周期，与 CI 机器快慢无关。
"""

from __future__ import annotations

import asyncio
import itertools
import sys
import time
import types
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from unilabos.client.host.hostlink import doctor
from unilabos.client.host.hostlink.backend import HostLinkBackend
from unilabos.client.host.hostlink.local_runtime import HostLinkDriverSpec, HostLinkLocalRuntime
from unilabos.client.host.hostlink.primitives import DeviceTimer
from unilabos.devices.workstation.workstation_base import WorkstationBase
from unilabos.experiments import models as protocol_models
from unilabos.experiments.compile import action_protocol_generators


def _jump_wall_clock(monkeypatch, offset_s: float) -> None:
    real_time, real_time_ns = time.time, time.time_ns
    monkeypatch.setattr(time, "time", lambda: real_time() + offset_s)
    monkeypatch.setattr(time, "time_ns", lambda: real_time_ns() + int(offset_s * 1e9))


def _jumping_wall_clock(monkeypatch, step_s: float) -> None:
    """每读一次墙钟，它都比上一次再前跳 ``step_s`` 秒。"""

    real_time = time.time
    reads = itertools.count(1)
    monkeypatch.setattr(time, "time", lambda: real_time() + step_s * next(reads))


def _sleep_driven_monotonic_clock(monkeypatch) -> None:
    """单调时钟只随 ``time.sleep`` 前进、不真睡。

    真睡时，慢的 CI 机器上 ``sleep(0.02)`` 常睡过头，0.3 s 内只跑得了两轮；
    换成这个时钟后轮数是确定的，墙钟照样按 :func:`_jumping_wall_clock` 跳。
    """

    now = [time.monotonic()]

    def fake_sleep(seconds: float) -> None:
        now[0] += max(float(seconds), 0.0)

    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", fake_sleep)


# ── 设备定时器 ───────────────────────────────────────────────


def test_device_timer_schedule_ignores_wall_clock_jump(monkeypatch) -> None:
    node = SimpleNamespace(create_task=lambda coroutine: coroutine.close())
    timer = DeviceTimer(node, 5.0, lambda: None)

    _jump_wall_clock(monkeypatch, 13.0)

    assert timer.is_ready() is False
    assert timer.time_until_next_call() > 4_000_000_000


# ── Workstation 协议编排耗时 ─────────────────────────────────


class ClockJumpHeatProtocol(BaseModel):
    temp: float


def _generate_heat_protocol(G=None, **kwargs):
    return [{"device_id": "heater-1", "action_name": "heat", "action_kwargs": {"temp": kwargs["temp"]}}]


#: 加热步骤执行期间要做的事（测试在这里回拨墙钟）
_DURING_HEAT: list = []


class _Workstation(WorkstationBase):
    def __init__(self, protocol_type=None, **kwargs) -> None:
        super().__init__(deck=None)
        self.protocol_type = protocol_type


class _Heater:
    def __init__(self) -> None:
        pass

    def heat(self, temp: float) -> dict:
        for hook in _DURING_HEAT:
            hook()
        return {"reached": temp}


def test_workstation_protocol_total_time_ignores_backward_wall_clock_step(monkeypatch) -> None:
    monkeypatch.setattr(protocol_models, "ClockJumpHeatProtocol", ClockJumpHeatProtocol, raising=False)
    monkeypatch.setitem(action_protocol_generators, ClockJumpHeatProtocol, _generate_heat_protocol)
    monkeypatch.setattr(sys.modules[__name__], "_DURING_HEAT", [lambda: _jump_wall_clock(monkeypatch, -30.0)])
    local = HostLinkLocalRuntime()
    local.add_driver(
        HostLinkDriverSpec(
            device_id="ws-1",
            driver_class=_Workstation,
            config={"protocol_type": ["ClockJumpHeatProtocol"]},
            action_names=("ClockJumpHeatProtocol",),
            action_value_mappings={
                "ClockJumpHeatProtocol": {"type": "ClockJumpHeatProtocol", "placeholder_keys": {}}
            },
        )
    )
    local.add_driver(
        HostLinkDriverSpec(
            device_id="heater-1",
            driver_class=_Heater,
            config={},
            action_names=("heat",),
            action_value_mappings={"heat": {"type": "UniLabJsonCommand"}},
        )
    )
    runtime = HostLinkBackend(local, is_slave=False)
    local.start()
    try:
        result = asyncio.run(runtime.call_action_async("ws-1", "ClockJumpHeatProtocol", temp=60.0))
    finally:
        runtime.stop()

    assert result["step_results"][0]["result"] == {"reached": 60.0}
    assert 0 <= result["total_time"] < 5


# ── doctor 运行时长 ──────────────────────────────────────────


class _FakeRos:
    """最小的 rclpy / std_msgs / unilabos_msgs 替身：记录发布条数与 spin 次数。"""

    def __init__(self, monkeypatch) -> None:
        self.published: list = []
        self.spins = 0
        fake = self

        class Node:
            def __init__(self, _name: str) -> None:
                pass

            def create_publisher(self, *_args):
                return SimpleNamespace(publish=fake.published.append)

            def create_subscription(self, *_args):
                return None

            def destroy_node(self) -> None:
                pass

        def spin_once(_node, timeout_sec: float = 0.0) -> None:
            fake.spins += 1
            time.sleep(0.01)

        rclpy = types.ModuleType("rclpy")
        rclpy.ok = lambda: True
        rclpy.spin_once = spin_once
        rclpy_node = types.ModuleType("rclpy.node")
        rclpy_node.Node = Node
        rclpy.node = rclpy_node
        std_msgs = types.ModuleType("std_msgs")
        std_msgs_msg = types.ModuleType("std_msgs.msg")
        std_msgs_msg.String = lambda data="": SimpleNamespace(data=data)
        std_msgs.msg = std_msgs_msg
        unilabos_msgs = types.ModuleType("unilabos_msgs")
        unilabos_msgs_srv = types.ModuleType("unilabos_msgs.srv")
        unilabos_msgs_srv.SerialCommand = object
        unilabos_msgs.srv = unilabos_msgs_srv
        modules = {
            "rclpy": rclpy,
            "rclpy.node": rclpy_node,
            "std_msgs": std_msgs,
            "std_msgs.msg": std_msgs_msg,
            "unilabos_msgs": unilabos_msgs,
            "unilabos_msgs.srv": unilabos_msgs_srv,
        }
        for name, module in modules.items():
            monkeypatch.setitem(sys.modules, name, module)
        monkeypatch.setattr(doctor, "apply_ros_network_env", lambda _info: {})


@pytest.mark.parametrize(
    "run, minimum",
    [
        (lambda: doctor.run_talker(None, "test", rate_hz=50.0, duration_s=0.3), 3),
        (
            lambda: doctor.run_fake_device(
                None, "test", device_id="dev-1", rate_hz=50.0, duration_s=0.3, check_host_services=False
            ),
            6,
        ),
    ],
    ids=["talker", "fake-device"],
)
def test_doctor_publish_duration_ignores_forward_wall_clock_jumps(monkeypatch, run, minimum) -> None:
    ros = _FakeRos(monkeypatch)
    _jumping_wall_clock(monkeypatch, 13.0)
    _sleep_driven_monotonic_clock(monkeypatch)

    run()

    assert len(ros.published) >= minimum


def test_doctor_listener_duration_ignores_forward_wall_clock_jumps(monkeypatch) -> None:
    ros = _FakeRos(monkeypatch)
    _jumping_wall_clock(monkeypatch, 13.0)
    _sleep_driven_monotonic_clock(monkeypatch)

    doctor.run_listener(None, "test", duration_s=0.3, quiet=True)

    assert ros.spins >= 3
