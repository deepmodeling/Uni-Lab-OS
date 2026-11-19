"""同机空设备握手后动态注册的连接隔离回归。"""

import pytest

from unilabos.backend.hostlink.client import HostLinkClient
from unilabos.backend.hostlink.backend import HostLinkBackend
from unilabos.backend.hostlink.local_runtime import HostLinkLocalRuntime
from unilabos.backend.hostlink.topic import TopicEvent
from unilabos.backend.hostlink.protocol import ActionType, RemoteError
from unilabos.backend.hostlink.server import HostLinkServer
from unilabos.client.materials.core import MaterialsHTTPError


def test_empty_connections_register_independently():
    server = HostLinkServer("127.0.0.1", 0).start()

    backend = HostLinkBackend(HostLinkLocalRuntime(), is_slave=False)

    def event(device_id):
        return {"event": TopicEvent.create("/test/heartbeat", 1, publisher_device_id=device_id).to_wire()}

    server.register_handler(ActionType.TOPIC_PUBLISH, backend._handle_topic_publish)
    clients = [HostLinkClient("127.0.0.1", server.port, machine_name=f"worker-{i}",
                              heartbeat_interval=60) for i in range(2)]
    assert clients[0].node_id != clients[1].node_id
    try:
        for client in clients:
            assert client.connect_blocking(timeout=2)
        assert len(server.peers()) == 2
        identities = [client.node_id for client in clients]
        for client, device_id in zip(clients, ["material_bench", "sample_rack"]):
            client.configure_device_descriptors([{"id": device_id}])
            client.heartbeat_payload_provider = lambda c=client: {"devices": c.device_descriptors}
            client.heartbeat_now()
        assert [client.node_id for client in clients] == identities
        for client, device_id in zip(clients, ["material_bench", "sample_rack"]):
            assert client.request(ActionType.TOPIC_PUBLISH, event(device_id))["accepted"]
        with pytest.raises(RemoteError):
            clients[0].request(ActionType.TOPIC_PUBLISH, event("sample_rack"))
        clients[1].close()
        assert clients[0].request(ActionType.TOPIC_PUBLISH, event("material_bench"))["accepted"]
    finally:
        for client in clients:
            client.close()
        server.stop()
        backend.stop()


def test_material_http_missing_is_distinct_from_failure():
    assert MaterialsHTTPError(404, "material root not found").code == "not_found"
    assert MaterialsHTTPError(500, "database failure").code != "not_found"


def test_duplicate_machine_name_rejected_without_overwriting():
    server = HostLinkServer("127.0.0.1", 0)
    first = server._touch_peer("connection-a", ActionType.HELLO, {"machine_name": "worker", "device_ids": []})
    with pytest.raises(ValueError, match="machine_name"):
        server._touch_peer("connection-b", ActionType.HELLO, {"machine_name": "worker", "device_ids": ["other"]})
    assert server._peers["worker"]["addr"] == first["addr"]
    server.mark_disconnected("connection-a")
    restored = server._touch_peer("connection-b", ActionType.HELLO, {"machine_name": "worker", "device_ids": ["new-device"]})
    assert restored["node_id"] == "worker"
