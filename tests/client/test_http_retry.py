"""HTTPClient 重试：Host 子进程启动中的 5001（带 Retry-After）与 5xx 一样按退避重试，其它业务码不重试。"""

import httpx
import pytest

from unilabos.client.http import HTTPClient, HTTPClientConfig
from unilabos.client.utils.envelope import EnvelopeError


def _client(responses):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return responses[min(len(calls), len(responses)) - 1]

    client = HTTPClient(HTTPClientConfig(base_url="http://test", max_retries=3, retry_backoff=0))
    client._client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(handler))
    return client, calls


def _unavailable(retry_after=True):
    headers = {"Retry-After": "3"} if retry_after else {}
    body = {"code": 5001, "error": {"msg": "Host 执行进程暂不可用（启动或重启中），请稍后重试"}}
    return httpx.Response(200, json=body, headers=headers)


def test_host_starting_5001_is_retried_until_ready():
    client, calls = _client([_unavailable(), _unavailable(), httpx.Response(200, json={"code": 0, "data": [1]})])
    assert client.get("/runtime/endpoints") == [1]
    assert len(calls) == 3


def test_host_starting_5001_surfaces_after_retries_run_out():
    client, calls = _client([_unavailable()])
    with pytest.raises(EnvelopeError) as excinfo:
        client.get("/runtime/endpoints")
    assert excinfo.value.code == 5001
    assert len(calls) == 4


def test_5001_without_retry_after_is_not_retried():
    client, calls = _client([_unavailable(retry_after=False)])
    with pytest.raises(EnvelopeError):
        client.get("/scheduler/time-plan")
    assert len(calls) == 1


def test_other_business_errors_are_not_retried():
    client, calls = _client([httpx.Response(200, json={"code": 4001, "error": {"msg": "不存在"}}, headers={"Retry-After": "3"})])
    with pytest.raises(EnvelopeError) as excinfo:
        client.get("/workflows/x")
    assert excinfo.value.code == 4001
    assert len(calls) == 1
