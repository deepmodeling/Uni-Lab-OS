"""在真实 Windows IOCP 完成回调中注入错误，验证监听和待接入 socket 的生命周期。"""

from __future__ import annotations

import asyncio
import errno
import socket
import sys

import pytest
from fastapi import FastAPI, WebSocket


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows IOCP 专用回归")


class _Echo(asyncio.Protocol):
    def connection_made(self, transport) -> None:
        self.transport = transport

    def data_received(self, data: bytes) -> None:
        self.transport.write(data)


def _inject_accept_error(monkeypatch, loop, *, winerror=64, failures=1, armed=None):
    """真实 AcceptEx 完成后报错；不伪造整个 accept，以覆盖 socket 回收。"""
    proactor = loop._proactor
    register = proactor._register
    get_socket = proactor._get_accept_socket
    fired = asyncio.Event()
    sockets: list[socket.socket] = []

    def tracked_socket(family):
        conn = get_socket(family)
        sockets.append(conn)
        return conn

    def fault_register(ov, obj, callback):
        if isinstance(obj, socket.socket) and obj.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN):
            finish_accept = callback

            def callback(transferred, key, completed):
                nonlocal failures
                if failures and (armed is None or armed.is_set()):
                    completed.getresult()
                    failures -= 1
                    fired.set()
                    if winerror is None:
                        raise OSError(errno.EBADF, "注入非 Windows 接收错误")
                    raise OSError(22, "注入 AcceptEx 完成错误", None, winerror)
                return finish_accept(transferred, key, completed)

        return register(ov, obj, callback)

    monkeypatch.setattr(proactor, "_get_accept_socket", tracked_socket)
    monkeypatch.setattr(proactor, "_register", fault_register)
    return fired, sockets


@pytest.mark.parametrize("disconnects", [1, 3])
def test_accept_disconnect_keeps_listener_and_closes_failed_client(monkeypatch, disconnects) -> None:
    from unilabos.server.windows_event_loop import create_event_loop

    with asyncio.Runner(loop_factory=create_event_loop) as runner:
        loop = runner.get_loop()
        errors: list[dict] = []
        loop.set_exception_handler(lambda _loop, context: errors.append(context))
        fired, sockets = _inject_accept_error(monkeypatch, loop, failures=disconnects)

        async def scenario() -> None:
            server = await loop.create_server(_Echo, "127.0.0.1", 0)
            listener = server.sockets[0]
            port = listener.getsockname()[1]
            try:
                for index in range(disconnects):
                    fired.clear()
                    with socket.socket() as client:
                        client.setblocking(False)
                        await asyncio.wait_for(loop.sock_connect(client, ("127.0.0.1", port)), 2)
                        await asyncio.wait_for(fired.wait(), 2)
                        await asyncio.sleep(0.1)
                    assert listener.fileno() != -1, "单个客户端的 WinError 64 关闭了 HTTP 监听 socket"
                    assert sockets[index].fileno() == -1, "接入失败的客户端 socket 未释放"
                reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 2)
                try:
                    writer.write(b"still serving")
                    await writer.drain()
                    assert await asyncio.wait_for(reader.readexactly(13), 2) == b"still serving"
                finally:
                    writer.close()
                    await writer.wait_closed()
                assert not errors, errors
            finally:
                server.close()
                await server.wait_closed()
                # 负对照的原生 Proactor 会泄漏失败 socket；测试自身不能把它遗留给后续用例。
                for conn in sockets:
                    conn.close()

        runner.run(scenario())


@pytest.mark.parametrize("winerror", [5, 10048, None])
def test_unrelated_accept_error_is_not_silenced(monkeypatch, winerror) -> None:
    from unilabos.server.windows_event_loop import create_event_loop

    with asyncio.Runner(loop_factory=create_event_loop) as runner:
        loop = runner.get_loop()
        errors: list[dict] = []
        loop.set_exception_handler(lambda _loop, context: errors.append(context))
        fired, sockets = _inject_accept_error(monkeypatch, loop, winerror=winerror)

        async def scenario() -> None:
            server = await loop.create_server(_Echo, "127.0.0.1", 0)
            listener = server.sockets[0]
            try:
                with socket.socket() as client:
                    client.setblocking(False)
                    await loop.sock_connect(client, listener.getsockname())
                    await asyncio.wait_for(fired.wait(), 2)
                    await asyncio.sleep(0.1)
                assert listener.fileno() == -1
                assert len(errors) == 1, errors
                assert errors[0]["message"] == "Accept failed on a socket"
                assert getattr(errors[0]["exception"], "winerror", None) == winerror
                assert all(conn.fileno() == -1 for conn in sockets)
            finally:
                server.close()
                await server.wait_closed()

        runner.run(scenario())


@pytest.mark.parametrize("during_retry", [False, True])
def test_shutdown_cancels_accept_and_retry_without_leaks(monkeypatch, during_retry) -> None:
    from unilabos.server import windows_event_loop

    monkeypatch.setattr(windows_event_loop, "_ACCEPT_RETRY_DELAY_S", 60)
    with asyncio.Runner(loop_factory=windows_event_loop.create_event_loop) as runner:
        loop = runner.get_loop()
        errors: list[dict] = []
        loop.set_exception_handler(lambda _loop, context: errors.append(context))
        fired, sockets = _inject_accept_error(monkeypatch, loop)

        async def scenario() -> None:
            server = await loop.create_server(_Echo, "127.0.0.1", 0)
            try:
                if during_retry:
                    with socket.socket() as client:
                        client.setblocking(False)
                        await loop.sock_connect(client, server.sockets[0].getsockname())
                        await asyncio.wait_for(fired.wait(), 2)
                await asyncio.sleep(0.01)
                assert len(sockets) == 1
                pending_accepts = set(asyncio.all_tasks()) - {asyncio.current_task()}
                assert len(pending_accepts) == 1
            finally:
                server.close()
                await server.wait_closed()
            # 即使处于 60 秒退避，也必须立即取消；不能在停机后重新接收。
            await asyncio.wait_for(asyncio.gather(*pending_accepts, return_exceptions=True), 1)
            assert all(task.cancelled() for task in pending_accepts)
            assert all(conn.fileno() == -1 for conn in sockets)
            assert not errors, errors
            assert not loop._accept_futures

        runner.run(scenario())


def test_loop_retains_async_subprocess_pipes_and_does_not_patch_global_policy() -> None:
    from asyncio.windows_events import IocpProactor
    from unilabos.server.windows_event_loop import create_event_loop

    policy = asyncio.get_event_loop_policy()
    original_accept = IocpProactor.accept
    with asyncio.Runner(loop_factory=create_event_loop) as runner:
        assert isinstance(runner.get_loop(), asyncio.ProactorEventLoop)

        async def scenario() -> None:
            child = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-c", "print('child-ok')",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(child.communicate(), 10)
                assert child.returncode == 0, stderr
                assert stdout.strip() == b"child-ok"
            finally:
                if child.returncode is None:
                    child.kill()
                    await child.wait()

        runner.run(scenario())
    assert asyncio.get_event_loop_policy() is policy
    assert IocpProactor.accept is original_accept
    normal_loop = asyncio.ProactorEventLoop()
    try:
        assert type(normal_loop._proactor) is IocpProactor
    finally:
        normal_loop.close()


def test_http_websocket_and_sse_survive_an_accept_disconnect(monkeypatch) -> None:
    import httpx
    import uvicorn
    from starlette.responses import StreamingResponse
    from websockets.legacy.client import connect
    from unilabos.server.windows_event_loop import create_event_loop

    with asyncio.Runner(loop_factory=create_event_loop) as runner:
        loop = runner.get_loop()
        errors: list[dict] = []
        loop.set_exception_handler(lambda _loop, context: errors.append(context))
        armed = asyncio.Event()
        fired, sockets = _inject_accept_error(monkeypatch, loop, armed=armed)
        continue_stream = asyncio.Event()
        application = FastAPI()

        @application.get("/health")
        async def health():
            return {"status": "ok"}

        @application.websocket("/ws")
        async def echo(websocket: WebSocket):
            await websocket.accept()
            await websocket.send_text(await websocket.receive_text())
            await websocket.close()

        @application.get("/events")
        async def events():
            async def stream():
                yield "data: before\n\n"
                await continue_stream.wait()
                yield "data: after\n\n"

            return StreamingResponse(stream(), media_type="text/event-stream")

        async def scenario() -> None:
            server = uvicorn.Server(uvicorn.Config(
                application, host="127.0.0.1", port=0,
                log_config=None, access_log=False, timeout_graceful_shutdown=1,
            ))
            serving = asyncio.create_task(server.serve())
            try:
                async with asyncio.timeout(5):
                    while not server.started:
                        if serving.done():
                            await serving
                            pytest.fail("HTTP 服务在就绪前退出")
                        await asyncio.sleep(0.01)
                port = server.servers[0].sockets[0].getsockname()[1]
                async with httpx.AsyncClient(
                    base_url=f"http://127.0.0.1:{port}", trust_env=False,
                    limits=httpx.Limits(max_keepalive_connections=0),
                ) as client:
                    assert (await client.get("/health")).json() == {"status": "ok"}
                    async with connect(f"ws://127.0.0.1:{port}/ws") as websocket:
                        async with client.stream("GET", "/events") as response:
                            lines = response.aiter_lines()
                            assert await anext(lines) == "data: before"
                            assert await anext(lines) == ""
                            armed.set()
                            with socket.socket() as broken:
                                broken.setblocking(False)
                                await loop.sock_connect(broken, ("127.0.0.1", port))
                                await asyncio.wait_for(fired.wait(), 2)
                            # 禁用 keep-alive，确保健康检查确实建立新连接。
                            assert (await client.get("/health")).json() == {"status": "ok"}
                            await websocket.send("still connected")
                            assert await asyncio.wait_for(websocket.recv(), 2) == "still connected"
                            continue_stream.set()
                            assert await asyncio.wait_for(anext(lines), 2) == "data: after"
                    # 错误之后的新 WebSocket 也必须能接入。
                    async with connect(f"ws://127.0.0.1:{port}/ws") as websocket:
                        await websocket.send("new connection")
                        assert await asyncio.wait_for(websocket.recv(), 2) == "new connection"
            finally:
                continue_stream.set()
                server.should_exit = True
                await asyncio.wait_for(serving, 5)
            assert not errors, errors
            assert all(conn.fileno() == -1 for conn in sockets)

        runner.run(scenario())
