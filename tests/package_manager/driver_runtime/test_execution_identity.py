"""跨进程执行身份不能来自业务字段，证据不能被重放或转移。"""

from copy import deepcopy
from uuid import uuid4

import pytest

from unilabos.ros.execution_identity import IdentityReceiver, IdentityTransportError, sign_command


def invocation():
    receiver = IdentityReceiver("robot")
    identity = {"node_job_uuid": str(uuid4()), "task_uuid": str(uuid4())}
    goal = str(uuid4())
    command = {"function_name": "pick", "function_args": {"site": "A"}, "unilabos_param": {}}
    signed = sign_command(command, identity, challenge=receiver.challenge(), goal_id=goal, target="robot", key=b"test-key")
    return receiver, identity, goal, signed


def test_identity_is_bound_to_command_and_consumed_once():
    receiver, identity, goal, signed = invocation()
    assert receiver.consume(signed, goal, key=b"test-key") == identity
    with pytest.raises(IdentityTransportError, match="重放"):
        receiver.consume(signed, goal, key=b"test-key")


@pytest.mark.parametrize("tamper", ["arguments", "identity", "goal", "key", "receiver"])
def test_tampered_or_redirected_proof_never_authenticates(tamper):
    receiver, identity, goal, signed = invocation()
    original = deepcopy(signed)
    key = b"test-key"
    if tamper == "arguments":
        signed["function_args"]["site"] = "B"
    elif tamper == "identity":
        signed["_unilab_execution_identity"]["claims"]["node_job_uuid"] = str(uuid4())
    elif tamper == "goal":
        goal = str(uuid4())
    elif tamper == "key":
        key = b"wrong-key"
    else:
        receiver = IdentityReceiver("robot")
    with pytest.raises(IdentityTransportError):
        receiver.consume(signed, goal, key=key)
    if tamper in {"arguments", "identity", "key"}:
        assert receiver.consume(original, goal, key=b"test-key") == identity


def test_expired_proof_and_previous_receiver_generation_fail(monkeypatch):
    receiver, _, goal, signed = invocation()
    issued_at = signed["_unilab_execution_identity"]["claims"]["issued_at"]
    monkeypatch.setattr("unilabos.ros.execution_identity.time.time", lambda: issued_at + 61)
    with pytest.raises(IdentityTransportError, match="过期"):
        receiver.consume(signed, goal, key=b"test-key")


def test_unsigned_business_identity_is_not_a_proof():
    receiver = IdentityReceiver("robot")
    with pytest.raises(IdentityTransportError):
        receiver.consume({"node_job_uuid": str(uuid4()), "task_uuid": str(uuid4())}, str(uuid4()), key=b"test-key")
