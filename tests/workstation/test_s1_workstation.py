from unilabos.devices.workstation.s1.s1_workstation import S1Workstation


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.responses = []

    def queue(self, response):
        self.responses.append(response)

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            return {"code": "200", "desc": "Succeed!", "data": {}}
        return self.responses.pop(0)


def test_s1_workstation_builds_configurable_base_url():
    transport = FakeTransport()

    device = S1Workstation(host="10.0.0.8", port=18055, api_prefix="/api/v1/", transport=transport)

    assert device.base_url == "http://10.0.0.8:18055/api/v1"


def test_s1_login_caches_token_and_reuses_authorization_header():
    transport = FakeTransport()
    transport.queue({"code": "200", "desc": "Succeed!", "data": "token-123"})
    device = S1Workstation(host="10.0.0.8", transport=transport)

    login_result = device.login(username="operator", password="secret")
    query_result = device.sync_materials(nameKey="mock", pageNum=2, pageSize=5)

    assert login_result["success"] is True
    assert query_result["success"] is True
    assert transport.calls[0]["method"] == "POST"
    assert transport.calls[0]["url"] == "http://10.0.0.8:8055/api/v1/auth/login"
    assert transport.calls[0]["json"] == {"username": "operator", "password": "secret"}
    assert transport.calls[1]["method"] == "GET"
    assert transport.calls[1]["url"] == "http://10.0.0.8:8055/api/v1/material/search"
    assert transport.calls[1]["headers"]["Authorization"] == "Bearer token-123"
    assert transport.calls[1]["params"] == {"nameKey": "mock", "pageNum": 2, "pageSize": 5}


def test_s1_scheduler_start_is_blocked_until_hardware_actions_are_allowed():
    transport = FakeTransport()
    device = S1Workstation(host="10.0.0.8", transport=transport, test_mode=True, allow_hardware_action=False)

    result = device.scheduler_start(experiment_ids=[2804265])

    assert result == {
        "success": False,
        "message": "Mock mode: experiment start rejected to avoid operating real hardware.",
        "data": {"requestedExperimentIds": [2804265]},
    }
    assert transport.calls == []


def test_s1_scheduler_stop_uses_get_when_hardware_actions_are_allowed():
    transport = FakeTransport()
    device = S1Workstation(
        host="10.0.0.8",
        transport=transport,
        test_mode=False,
        allow_hardware_action=True,
        token="token-123",
    )

    result = device.scheduler_stop(channel=2)

    assert result["success"] is True
    assert transport.calls == [
        {
            "method": "GET",
            "url": "http://10.0.0.8:8055/api/v1/manualControl/stop",
            "params": {"channel": 2},
            "json": None,
            "headers": {"Authorization": "Bearer token-123"},
            "timeout": 10.0,
        }
    ]
