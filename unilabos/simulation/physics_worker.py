"""物理会话的公共 RPC 与扫描日志；求解器生命周期由调用者持有。"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Callable, TextIO


class PhysicsWorkerSession:
    """重复确认只回缓存；日志只记录新确认扫描，不记录传输重试。"""

    def __init__(self, session, trace: TextIO):
        self.session = session
        self.trace = trace
        self.running = True
        self.failed = False

    def handle(self, request: dict) -> dict:
        try:
            if not self.running:
                raise RuntimeError("worker_closed")
            operation = request["op"]
            if operation == "close":
                self.running = False
                result = {"closed": True}
            elif self.failed:
                raise RuntimeError("worker_result_unknown")
            elif operation == "observe":
                result = self.session.observe()
            elif operation == "command":
                result = self.session.controls.apply(request)
            elif operation == "step":
                prior = self.session.stepper.tick
                result = self.session.scan(request["token"])
                if self.session.stepper.tick != prior:
                    try:
                        self.trace.write(json.dumps(result, allow_nan=False) + "\n")
                        self.trace.flush()
                    except BaseException:
                        # 物理已推进但证据未确认，不能继续或伪造重试成功。
                        self.failed = True
                        raise
            else:
                raise ValueError("unsupported_operation")
            return {"ok": True, "result": result}
        except Exception as error:
            return {"ok": False, "error": str(error)}


def serve(session, path: Path, trace: TextIO, *, ready: Callable[[], None]) -> None:
    """单线程拥有会话；只清理本次成功绑定的 Unix socket。"""
    worker = PhysicsWorkerSession(session, trace)
    bound = False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(path))
            bound = True
            os.chmod(path, 0o600)
            server.listen(4)
            ready()
            while worker.running:
                connection, _ = server.accept()
                with connection:
                    connection.settimeout(120)
                    try:
                        with connection.makefile("rb") as stream:
                            line = stream.readline(65537)
                        if len(line) > 65536 or not line.endswith(b"\n"):
                            raise ValueError("invalid_request_frame")
                        response = worker.handle(json.loads(line))
                    except Exception as error:
                        response = {"ok": False, "error": str(error)}
                    # ACK 丢失不重新推进；客户端仍可用同一 token 重取缓存。
                    try:
                        connection.sendall(json.dumps(response, allow_nan=False).encode() + b"\n")
                    except OSError:
                        pass
    finally:
        if bound:
            path.unlink(missing_ok=True)
