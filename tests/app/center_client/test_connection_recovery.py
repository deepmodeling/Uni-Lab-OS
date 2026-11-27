"""恢复码换证后，真实连接管理器、运行时和客户端重新建立控制会话。"""
from datetime import timedelta
from uuid import uuid4

import pytest

from tests.app.center_client.test_center_client import INSTANCE, STATION
from tests.app.center_client.test_connection import Center as CertificateCenter
from tests.app.center_client.test_runtime import Center, Service
from unilabos.app.center_client.client import CenterClient, SessionFencedError
from unilabos.app.center_client.connection import CenterConnection
from unilabos.app.center_client.durable import DurableCenterStore
from unilabos.app.center_client.runtime import CenterRuntime
from unilabos.app.center_client.transport import CenterTransportError


class ControlCenter(Center):
    def __init__(self):
        super().__init__()
        self.handshakes = []
        self.online = False

    def reload_credentials(self, config):
        self.config = config

    def request(self, method, path, **kwargs):
        if path.endswith('/session'):
            body = kwargs['json_body']
            if self.handshakes:
                assert body['previous_session_uuid'] == self.session_uuid
                assert body['previous_session_epoch'] == len(self.handshakes)
                assert body['process_uuid'] == self.handshakes[-1]['process_uuid']
            self.handshakes.append(body.copy())
            self.session_uuid = str(uuid4())
            self.online = True
            return dict(session_uuid=self.session_uuid, session_epoch=len(self.handshakes),
                        station_uuid=STATION, instance_uuid=INSTANCE)
        return super().request(method, path, **kwargs)


def connected(tmp_path, monkeypatch):
    certificates, center, service = CertificateCenter(), ControlCenter(), Service()
    certificates.station, certificates.instance = STATION, INSTANCE
    # 由测试显式推进轮询，保留真实换证与持久存储调用链。
    monkeypatch.setattr(CenterRuntime, 'start', lambda self: None)

    def factory(config):
        store = DurableCenterStore(tmp_path / 'workflow.db', station_uuid=STATION, instance_uuid=INSTANCE)
        service.store = store
        return CenterRuntime(config, CenterClient(config, center), store, service)

    owner = CenterConnection(tmp_path / 'connection', factory, request=certificates,
                             clock=lambda: certificates.now.timestamp())
    owner.connect('https://center.test', 'a' * 64)
    owner.step()
    owner.runtime.poll_once()
    return owner, certificates, center


@pytest.mark.parametrize('observed_by', ['http', 'websocket', 'not_yet'])
def test_reenrollment_restores_closed_control_session(tmp_path, monkeypatch, observed_by):
    owner, certificates, center = connected(tmp_path, monkeypatch)
    try:
        runtime = owner.runtime
        old_session = runtime.client.session
        event_epoch = runtime.store.event_epoch_uuid
        center.online = False
        if observed_by == 'http':
            center.reject_control = True
            with pytest.raises(SessionFencedError):
                runtime.poll_once()
            center.reject_control = False
        elif observed_by == 'websocket':
            assert runtime.client.fence_session(old_session)

        owner.connect('https://center.test', 'b' * 64)
        owner.step()
        runtime.poll_once()

        assert center.online, '换证成功后仍未重新建立控制会话'
        assert len(center.handshakes) == 2
        assert runtime.client.session.session_uuid != old_session.session_uuid
        assert runtime.client.process_uuid == old_session.process_uuid
        assert runtime.store.event_epoch_uuid == event_epoch
        assert runtime.store.session() == runtime.client.session
        assert owner.runtime is runtime
        assert certificates.requests[-1][1]['expected_station_uuid'] == STATION
    finally:
        owner.close()


@pytest.mark.parametrize('fenced', [False, True])
def test_automatic_renewal_preserves_control_session_state(tmp_path, monkeypatch, fenced):
    owner, certificates, center = connected(tmp_path, monkeypatch)
    try:
        previous = owner.runtime.client.session
        if fenced:
            assert owner.runtime.client.fence_session(previous)
        certificates.now += timedelta(days=24)
        owner.step()
        if fenced:
            with pytest.raises(SessionFencedError):
                owner.runtime.poll_once()
        else:
            owner.runtime.poll_once()
            assert owner.runtime.client.session == previous
        assert len(center.handshakes) == 1
    finally:
        owner.close()


@pytest.mark.parametrize('rejected', [False, True])
def test_recovery_preserves_handshake_on_timeout_and_obeys_center_fencing(tmp_path, monkeypatch, rejected):
    owner, _, center = connected(tmp_path, monkeypatch)
    try:
        previous = owner.runtime.client.session
        assert owner.runtime.client.fence_session(previous)
        owner.connect('https://center.test', 'b' * 64)
        owner.step()
        request = center.request
        attempts = []

        def interrupted(method, path, **kwargs):
            if path.endswith('/session'):
                attempts.append(kwargs['json_body'].copy())
                if rejected or len(attempts) == 1:
                    raise CenterTransportError('握手被拒绝' if rejected else '连接中断',
                                               code='STALE_SESSION' if rejected else None,
                                               retryable=not rejected)
            return request(method, path, **kwargs)

        monkeypatch.setattr(center, 'request', interrupted)
        with pytest.raises(CenterTransportError):
            owner.runtime.poll_once()
        if rejected:
            assert owner.runtime.client.session is None
            assert len(center.handshakes) == 1
        else:
            owner.runtime.poll_once()
            assert attempts[0] == attempts[1]
            assert len(center.handshakes) == 2
        assert attempts[0]['process_uuid'] == previous.process_uuid
        assert attempts[0]['previous_session_uuid'] == previous.session_uuid
    finally:
        owner.close()
