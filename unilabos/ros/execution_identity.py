"""ROS 依赖调用的签名身份：绑定接收进程、Goal 和完整命令，执行前一次性消费。"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import threading
import time
import uuid
from collections.abc import Mapping
from typing import Any

PROOF_FIELD = "_unilab_execution_identity"
_TTL_SECONDS = 60.0
_SIGNING_DOMAIN = b"unilabos/ros-execution-identity/v1\x00"


class IdentityTransportError(ValueError):
    """身份配置缺失、证据非法或证据已经消费；不得降级成匿名执行。"""


def identity_key() -> bytes:
    from unilabos.config.config import EdgeControlConfig, ROSConfig

    value = str(ROSConfig.execution_identity_key or EdgeControlConfig.api_key or "")
    if len(value.encode()) < 32:
        raise IdentityTransportError("跨进程工作流身份需要至少 32 字节的共享运行时密钥")
    # 与同一工作区的 HTTP Bearer 用途隔离，ROS 线上只传签名。
    return hmac.digest(value.encode(), _SIGNING_DOMAIN, "sha256")


def _encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sign_command(command: dict[str, Any], identity: Mapping[str, str], *,
                 challenge: Mapping[str, Any], goal_id: str, target: str, key: bytes) -> dict[str, Any]:
    """只由 OS 传入当前执行上下文；业务参数不参与身份选取。"""
    claims = {
        "version": 1, "receiver": challenge["receiver"], "target": target,
        "issued_at": challenge["issued_at"], "goal_id": str(uuid.UUID(goal_id)),
        "node_job_uuid": str(uuid.UUID(identity["node_job_uuid"])),
        "task_uuid": str(uuid.UUID(identity["task_uuid"])),
        "command_digest": hashlib.sha256(_encode(command)).hexdigest(),
    }
    signature = hmac.digest(key, _SIGNING_DOMAIN + _encode(claims), "sha256").hex()
    return {**command, PROOF_FIELD: {"claims": claims, "signature": signature}}


class IdentityReceiver:
    """接收进程启动时生成新代次；旧进程的证据在重启后立即失效。"""

    def __init__(self, target: str) -> None:
        self.target = target
        self.receiver = str(uuid.uuid4())
        self._used: dict[str, float] = {}
        self._lock = threading.Lock()

    def challenge(self) -> dict[str, Any]:
        return {"version": 1, "receiver": self.receiver, "issued_at": time.time()}

    def consume(self, command: Mapping[str, Any], goal_id: str, *, key: bytes) -> dict[str, str]:
        """校验并消费证据；篡改、过期、跨设备/进程/Goal 重放都关闭式失败。"""
        try:
            proof = command[PROOF_FIELD]
            claims = proof["claims"]
            signature = proof["signature"]
            expected = hmac.digest(key, _SIGNING_DOMAIN + _encode(claims), "sha256").hex()
            if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
                raise IdentityTransportError("执行身份签名无效")
            if (claims["version"] != 1 or claims["receiver"] != self.receiver
                    or claims["target"] != self.target or claims["goal_id"] != goal_id):
                raise IdentityTransportError("执行身份与接收进程、设备或 Goal 不匹配")
            issued_at = float(claims["issued_at"])
            now = time.time()
            if not math.isfinite(issued_at) or not 0 <= now - issued_at <= _TTL_SECONDS:
                raise IdentityTransportError("执行身份已过期或时间无效")
            digest = hashlib.sha256(_encode({k: v for k, v in command.items() if k != PROOF_FIELD})).hexdigest()
            if not hmac.compare_digest(claims["command_digest"], digest):
                raise IdentityTransportError("执行命令与身份签名不匹配")
            identity = {name: str(uuid.UUID(claims[name])) for name in ("node_job_uuid", "task_uuid")}
            with self._lock:
                self._used = {goal: expiry for goal, expiry in self._used.items() if expiry >= now}
                if goal_id in self._used:
                    raise IdentityTransportError("执行身份证据已消费，禁止重放")
                if len(self._used) >= 10000:
                    raise IdentityTransportError("执行身份接收容量已满")
                self._used[goal_id] = issued_at + _TTL_SECONDS
            return identity
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            if isinstance(error, IdentityTransportError):
                raise
            raise IdentityTransportError("执行身份证据格式无效") from error
