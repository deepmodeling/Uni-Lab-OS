"""本地仿真流传输；Unix 使用原生 socket，Windows 使用显式 loopback 端口文件。

只提供传输，不改变 RPC 帧、时钟或领域行为，不修改全局 socket 模块。
端点文件由调用方按既有生命周期管理；跨机传输不属于此入口。
"""
import os
import socket
from pathlib import Path


class _LoopbackStream(socket.socket):
    """Windows 的路径命名流；地址文件必须位于本轮独占工作目录。"""

    def __init__(self):
        super().__init__(socket.AF_INET, socket.SOCK_STREAM)
        self._endpoint_path = None

    def bind(self, address):
        """先绑定端口，listen 成功后才发布就绪地址，拒绝覆盖已有端点。"""
        path = Path(address)
        if path.exists():
            raise FileExistsError(path)
        super().bind(('127.0.0.1', 0))
        # 现有 worker 在 listen 前 chmod；独占创建占位，客户端拒绝空地址。
        with path.open('x', encoding='ascii'):
            pass
        self._endpoint_path = path

    def listen(self, backlog=4):
        """监听成功后原子发布端口，避免把尚未监听当作服务可用。"""
        super().listen(backlog)
        temporary = self._endpoint_path.with_suffix(self._endpoint_path.suffix + '.ready')
        with temporary.open('x', encoding='ascii') as stream:
            stream.write(str(self.getsockname()[1]))
        os.replace(temporary, self._endpoint_path)

    def connect(self, address):
        """端口只允许本机；不自动重发任何 RPC 请求。"""
        port = int(Path(address).read_text(encoding='ascii').strip())
        if not 1 <= port <= 65535:
            raise ValueError('invalid_local_endpoint_port')
        return super().connect(('127.0.0.1', port))


def local_stream_socket():
    """创建本地流；支持原生 AF_UNIX 的环境保持原行为。"""
    if hasattr(socket, 'AF_UNIX'):
        return socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    return _LoopbackStream()
