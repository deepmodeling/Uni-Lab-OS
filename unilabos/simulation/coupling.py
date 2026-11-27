"""OS 拥有调用时序，复用 PLC-Sim 的确认屏障与控制器写入授权。"""

from __future__ import annotations

import json
import socket
from dataclasses import asdict
from pathlib import Path


class IsaacSocketPort:
    """将隔离 Isaac RPC 接到 PLC-Sim 正式 PhysicsPort。

    launch(token) 必须创建全新世界，并返回其 socket 路径；stop 只清理该世界。
    decode 由设备包定义，返回命名传感器值。任何超时交由 CoupledSession
    保持 faulted；这里没有自动重试或真实设备回退。
    """

    def __init__(self, launch, stop, decode, *, timeout=30.0):
        self.launch, self.stop, self.decode = launch, stop, decode
        self.timeout = timeout
        self.path: Path | None = None

    def _call(self, request):
        if self.path is None:
            raise RuntimeError("physics_not_started")
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(self.timeout)
            client.connect(str(self.path))
            client.sendall(json.dumps(request, allow_nan=False).encode() + b"\n")
            with client.makefile("rb") as stream:
                response = json.loads(stream.readline(16 * 1024 * 1024))
        if response.get("ok") is not True:
            raise RuntimeError(response.get("error", "invalid_physics_response"))
        return response["result"]

    def _frame(self, token, result, channels, elapsed):
        from plc_sim.cosimulation import PhysicsFrame, Sample

        values = self.decode(result["observation"])
        return PhysicsFrame(
            token,
            elapsed,
            {name: Sample(token.tick, token.tick, values.get(name), values.get(name) is not None) for name in channels},
        )

    def reset(self, token, sample_channels):
        if token.dt_ns != 50_000_000 or token.tick != 0:
            raise ValueError("unsupported_physics_clock")
        self.close()
        self.path = Path(self.launch(token))
        result = self._call({"op": "observe"})
        if any(result.get(key) != value for key, value in asdict(token).items() if key != "dt_ns"):
            raise ValueError("physics_reset_identity_mismatch")
        return self._frame(token, result, sample_channels, 0)

    def step(self, token, commands, sample_channels):
        # 控制 RPC 尚未由设备模型接入时必须拒绝，不能静默丢弃控制量。
        if commands:
            raise ValueError("physics_control_model_missing")
        result = self._call({"op": "step", "token": asdict(token)})
        if result.get("token") != asdict(token) or result.get("substeps") != 12:
            raise ValueError("physics_step_identity_mismatch")
        return self._frame(token, result, sample_channels, token.dt_ns)

    def close(self):
        if self.path is not None:
            self.stop(self.path)
            self.path = None


class CoupledWorld:
    """唯一同步推进入口；动作提交控制意图，由此处扫描，不自行推进世界。"""

    def __init__(self, port, *, channels, writers, record, world_id=None):
        from plc_sim.cosimulation import CoupledSession

        self.session = CoupledSession(port, dt_ns=50_000_000, channels=channels, writers=writers, world_id=world_id)
        self.record = record

    def register_controller(self, name, scan, *, reset):
        self.session.register_controller(name, 1, scan, reset=reset)

    def start(self):
        self.session.reset()

    def scan(self):
        self.session.step(single=True)
        frame = {
            "token": asdict(self.session.token),
            "samples": {
                name: asdict(value) if (value := self.session.observation(name)) is not None else None
                for name in self.session.channels
            },
        }
        try:
            self.record(frame)
        except BaseException:
            # 物理步已完成，记录失败不能让上层重复扫描。
            self.session.state = "faulted"
            raise
        return frame
