"""物理后端共用的步确认边界；包回调只提供控制与观测语义。"""

from __future__ import annotations

import math
import threading
from copy import deepcopy
from typing import Callable


class ConfirmedStepper:
    """OS 是唯一调用者；相同 token 重试只返回缓存，未知结果永久阻断后续步。"""

    def __init__(
        self, world_id: str, session_id: str, *, epoch: int = 0, substeps: int = 12, step_seconds: float = 1 / 240
    ):
        if (
            not isinstance(world_id, str)
            or not world_id
            or not isinstance(session_id, str)
            or not session_id
            or type(epoch) is not int
            or epoch < 0
            or type(substeps) is not int
            or substeps < 1
            or type(step_seconds) not in (float, int)
            or not math.isfinite(step_seconds)
            or step_seconds <= 0
        ):
            raise ValueError("非法世界或步长")
        self.world_id, self.session_id = world_id, session_id
        self.epoch = epoch
        self.substeps, self.step_seconds = substeps, step_seconds
        self.tick = 0
        self.pending = None
        self.last = None
        self.faulted = False
        self.owner = threading.get_ident()

    def step(self, token: dict, advance: Callable[[int, float, float], None], observe: Callable[[], dict]):
        if threading.get_ident() != self.owner or self.pending is not None and not self.faulted:
            raise RuntimeError("step_owner_conflict")
        if not isinstance(token, dict) or any(type(token.get(key)) is not int for key in ("epoch", "tick", "dt_ns")):
            raise ValueError("step_identity_mismatch")
        expected = {
            "world_id": self.world_id,
            "session_id": self.session_id,
            "epoch": self.epoch,
            "tick": self.tick + 1,
            "dt_ns": round(self.substeps * self.step_seconds * 1e9),
        }
        if self.faulted:
            raise RuntimeError("step_result_unknown")
        if self.last is not None and token == self.last["token"]:
            return deepcopy(self.last)
        if token != expected:
            raise ValueError("step_identity_mismatch")
        self.pending = deepcopy(token)
        try:
            for index in range(self.substeps):
                advance(index, self.step_seconds, (self.tick * self.substeps + index) * self.step_seconds)
            result = {"token": deepcopy(token), "observation": observe(), "substeps": self.substeps}
        except BaseException:
            self.faulted = True
            raise
        self.tick += 1
        self.last, self.pending = deepcopy(result), None
        return result


class ModelControls:
    """同一世界内幂等执行包的控制请求；结果不明时禁止继续推进。"""

    def __init__(self, stepper, command):
        self.stepper, self.command = stepper, command
        self.results = {}

    def apply(self, request):
        import json

        identity = {"world_id": self.stepper.world_id, "session_id": self.stepper.session_id,
                    "epoch": self.stepper.epoch}
        if any(request.get(key) != value for key, value in identity.items()):
            raise ValueError("control_identity_mismatch")
        key = request.get("control_id")
        if not isinstance(key, str) or not key:
            raise ValueError("control_id_required")
        payload = {"name": request["name"], "parameters": request["parameters"]}
        encoded = json.dumps(payload, sort_keys=True, allow_nan=False)
        if self.stepper.faulted:
            raise RuntimeError("step_result_unknown")
        if key in self.results:
            original, result = self.results[key]
            if original != encoded:
                raise ValueError("control_payload_mismatch")
            return deepcopy(result)
        if type(request.get("tick")) is not int or request["tick"] != self.stepper.tick:
            raise ValueError("control_tick_mismatch")
        try:
            result = self.command(payload["name"], deepcopy(payload["parameters"]))
            result = json.loads(json.dumps(result, allow_nan=False))
        except BaseException:
            self.stepper.faulted = True
            raise
        self.results[key] = (encoded, deepcopy(result))
        return result


class ModelSession:
    """统一包模型的扫描钩子；每个确认步只推进一次物理世界。"""

    def __init__(self, model, world_id, session_id, *, epoch=0, advance_substep=None):
        self.model = model
        self.advance_substep = advance_substep or model.advance_substep
        self.stepper = ConfirmedStepper(world_id, session_id, epoch=epoch)
        self.controls = ModelControls(self.stepper, model.command)

    def scan(self, token):
        def advance(index, duration, at):
            if index == 0:
                self.model.prepare_scan()
            self.model.before_substep(index)
            self.advance_substep(index, duration, at)
            self.model.after_substep(index)

        return self.stepper.step(token, advance, self.model.finish_scan)

    def observe(self):
        return {
            "world_id": self.stepper.world_id,
            "session_id": self.stepper.session_id,
            "epoch": self.stepper.epoch,
            "tick": self.stepper.tick,
            "observation": deepcopy(self.model.state),
        }
