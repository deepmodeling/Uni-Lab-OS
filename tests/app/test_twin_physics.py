"""共享物理会话的时序、重发和故障边界；不授予求解器资格。"""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from unilabos.simulation.physics import ModelSession


def model():
    return SimpleNamespace(state={"tick": 0}, command=Mock(return_value={"accepted": True}),
        prepare_scan=Mock(), before_substep=Mock(), advance_substep=Mock(),
        after_substep=Mock(), finish_scan=Mock(return_value={"tick": 1}))


def token(**changes):
    return {"world_id": "w", "session_id": "s", "epoch": 0,
            "tick": 1, "dt_ns": 50_000_000, **changes}


def test_shared_session_duplicate_and_invalid_identity_never_repeat_hooks():
    device = model()
    session = ModelSession(device, "w", "s")
    with pytest.raises(ValueError, match="step_identity_mismatch"):
        session.scan(token(session_id="old"))
    assert not device.advance_substep.called
    first = session.scan(token())
    first["observation"]["tick"] = 999
    assert session.scan(token())["observation"]["tick"] == 1
    assert device.advance_substep.call_count == 12
    device.prepare_scan.assert_called_once()
    device.finish_scan.assert_called_once()
    assert session.observe()["tick"] == 1


@pytest.mark.parametrize("hook", ["prepare_scan", "before_substep", "advance_substep", "after_substep", "finish_scan"])
def test_any_failed_scan_hook_requires_recovery(hook):
    device = model()
    getattr(device, hook).side_effect = OSError("lost feedback")
    session = ModelSession(device, "w", "s")
    with pytest.raises(OSError):
        session.scan(token())
    count = device.advance_substep.call_count
    with pytest.raises(RuntimeError, match="step_result_unknown"):
        session.scan(token())
    with pytest.raises(RuntimeError, match="step_result_unknown"):
        session.controls.apply({**token(tick=0), "control_id": "next", "name": "begin", "parameters": {}})
    assert device.advance_substep.call_count == count
    assert not device.command.called


def test_worker_retry_has_one_trace_and_control_side_effect():
    import io
    import json

    from unilabos.simulation.physics_worker import PhysicsWorkerSession
    device = model()
    trace = io.StringIO()
    worker = PhysicsWorkerSession(ModelSession(device, "w", "s"), trace)
    command = {**token(tick=0), "op": "command", "control_id": "one", "name": "begin", "parameters": {}}
    assert worker.handle(command) == worker.handle(command)
    device.command.assert_called_once()
    request = {"op": "step", "token": token()}
    assert worker.handle(request) == worker.handle(request)
    assert len(trace.getvalue().splitlines()) == 1
    assert json.loads(trace.getvalue())["token"]["tick"] == 1
    assert device.advance_substep.call_count == 12
    assert worker.handle({"op": "step", "token": token(session_id="old")})["ok"] is False
    assert worker.handle({"op": "close"})["result"]["closed"]
    assert worker.handle(request)["error"] == "worker_closed"


def test_worker_evidence_failure_blocks_further_physics():
    from unilabos.simulation.physics_worker import PhysicsWorkerSession
    device = model()
    trace = Mock()
    trace.flush.side_effect = OSError("disk full")
    worker = PhysicsWorkerSession(ModelSession(device, "w", "s"), trace)
    assert worker.handle({"op": "step", "token": token()})["error"] == "disk full"
    assert worker.handle({"op": "step", "token": token(tick=2)})["error"] == "worker_result_unknown"
    assert device.advance_substep.call_count == 12
    assert worker.handle({"op": "close"})["ok"]


def test_shared_rpc_survives_invalid_frame_and_duplicate_ack(tmp_path):
    import io
    import json
    import socket
    import threading
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from unilabos.simulation.physics_worker import serve
    directory = TemporaryDirectory(prefix="rpc-", dir="/tmp")
    path = Path(directory.name) / "physics.sock"
    ready = threading.Event()
    device = model()
    trace = io.StringIO()
    errors = []
    def run():
        try:
            session = ModelSession(device, "w", "s", advance_substep=device.advance_substep)
            serve(session, path, trace, ready=ready.set)
        except BaseException as error:
            errors.append(error)
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    assert ready.wait(5)
    def send(data):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect(str(path)); client.sendall(data)
            return json.loads(client.makefile("rb").readline())
    try:
        assert not send(b"not-json\n")["ok"]
        request = json.dumps({"op": "step", "token": token()}).encode() + b"\n"
        first = send(request)
        assert first["ok"] and send(request) == first
        assert device.advance_substep.call_count == 12
        assert len(trace.getvalue().splitlines()) == 1
    finally:
        send(b'{"op":"close"}\n')
        thread.join(5)
    assert not thread.is_alive() and not errors
    assert not path.exists()
    directory.cleanup()
