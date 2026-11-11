"""驱动包 SDK：请求不可自动重发，等待超时保留操作身份。"""

import httpx
import pytest

from unilabos.client.runtime.driver_packages import DriverPackageWaitTimeout, HTTPDriverPackageClient
from unilabos.protocol.runtime.driver_packages import DriverPackageInstallRequest


def _transport(client, handler):
    client._http._client.close()
    client._http._client = httpx.Client(
        base_url=client._http.config.base_url, transport=httpx.MockTransport(handler),
    )


@pytest.mark.parametrize("action", ["install", "uninstall", "enable"])
def test_package_mutation_is_not_retried_after_response_failure(action):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(503, json={"detail": "响应丢失"})

    with HTTPDriverPackageClient("http://host:8002") as client:
        _transport(client, handler)
        with pytest.raises(httpx.HTTPStatusError):
            if action == "install":
                client.install(DriverPackageInstallRequest(spec="/driver"))
            elif action == "enable":
                client.set_enabled("driver", True)
            else:
                client.uninstall("driver")
    assert len(requests) == 1
    assert requests[0].url.path.startswith("/api/v1/driver-packages/")


def test_wait_timeout_does_not_resubmit_or_cancel_operation():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"operation_id": "active-op", "status": "running"})

    with HTTPDriverPackageClient("http://host:8002") as client:
        _transport(client, handler)
        with pytest.raises(DriverPackageWaitTimeout) as exc:
            client.wait_operation("active-op", timeout=0.01, poll_interval=0.02)
        assert exc.value.operation_id == "active-op"
    assert len(requests) == 1
    assert requests[0].method == "GET"


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
def test_invalid_wait_budget_sends_no_request(timeout):
    with HTTPDriverPackageClient("http://host:8002") as client:
        _transport(client, lambda request: pytest.fail("无效预算不得发请求"))
        with pytest.raises(ValueError):
            client.wait_operation("op", timeout=timeout)


@pytest.mark.parametrize("payload", [
    {"operation_id": "op", "status": "unexpected"},
    {"operation_id": "another-op", "status": "succeeded"},
    {"operation_id": "op", "status": ["succeeded"]},
    {"operation_id": "op"},
    [],
])
def test_bad_operation_response_is_not_a_success(payload):
    with HTTPDriverPackageClient("http://host:8002") as client:
        _transport(client, lambda request: httpx.Response(200, json=payload))
        with pytest.raises(ValueError, match="响应身份或状态无效"):
            client.wait_operation("op", timeout=1)
        with pytest.raises(ValueError, match="响应身份或状态无效"):
            client.operation("op")
