"""Windows 管理服务专用 IOCP 循环；不修改进程全局策略或标准库。

CPython #93821：AcceptEx 遇到客户端断开（WinError 64）会让原生 Proactor
关闭监听 socket，进程却继续存活。这里仅接管 accept：释放失败连接并重新接收，
保留 Proactor 的管道 / 子进程能力，避免切到 Selector 的 512 socket 限制。

此模块仅在 Windows 启动 HTTP 服务时导入。所用 IOCP 扩展点为 CPython 私有接口，
升级 Python 时需继续运行 test_windows_event_loop 的真实 socket / 取消回归。
上游问题：https://github.com/python/cpython/issues/93821
"""

from __future__ import annotations

import asyncio
from asyncio.windows_events import IocpProactor
import logging
import socket
import struct
import _overlapped


_logger = logging.getLogger(__name__)
_ACCEPT_RETRY_DELAY_S = 0.05


class _AcceptRetryProactor(IocpProactor):
    def accept(self, listener: socket.socket) -> asyncio.Task:
        # 一个任务负责重试和 socket 所有权；原生实现的旁路 accept_coro 在异常时
        # 既不关闭失败 socket，也会额外留下 Task exception was never retrieved。
        return self._loop.create_task(self._accept(listener))

    async def _accept(self, listener: socket.socket) -> tuple[socket.socket, tuple]:
        while True:
            conn = self._get_accept_socket(listener.family)
            accepted = False
            try:
                result = await self._accept_once(listener, conn)
                accepted = True
                return result
            except OSError as exc:
                if (
                    getattr(exc, "winerror", None) != _overlapped.ERROR_NETNAME_DELETED
                    or listener.fileno() == -1
                    or listener in self._stopped_serving
                ):
                    raise
                _logger.debug("接入中的客户端已断开（WinError 64），保留监听并重新接收")
            finally:
                # 错误 / 取消 / 停机都回收本次 socket；成功才移交给事件循环的 transport。
                if not accepted:
                    conn.close()
            # 连续异常不能忙转；server.close() 会取消整个 accept 任务，也会打断此等待。
            await asyncio.sleep(_ACCEPT_RETRY_DELAY_S)

    def _accept_once(self, listener: socket.socket, conn: socket.socket) -> asyncio.Future:
        self._register_with_iocp(listener)
        operation = _overlapped.Overlapped(0)
        operation.AcceptEx(listener.fileno(), conn.fileno())

        def finish_accept(_transferred, _key, completed):
            completed.getresult()
            conn.setsockopt(
                socket.SOL_SOCKET,
                _overlapped.SO_UPDATE_ACCEPT_CONTEXT,
                struct.pack("@P", listener.fileno()),
            )
            conn.settimeout(listener.gettimeout())
            return conn, conn.getpeername()

        return self._register(operation, listener, finish_accept)


def create_event_loop() -> asyncio.ProactorEventLoop:
    """只供管理 API 的 Runner 使用；其他线程 / Host / Slave 仍使用原有循环。"""
    return asyncio.ProactorEventLoop(_AcceptRetryProactor())
