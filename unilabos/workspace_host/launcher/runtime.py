"""桌面父进程内复用主线 Host、公开操作队列及包内单实例身份。"""
from __future__ import annotations

import os
import threading
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ..client import WorkspaceHostClient
from ..discovery import WorkspaceHostLock, ensure_local_token
from ..host import WorkspaceHost, _handler_type
from ..model import WorkspaceHostError, WorkspacePaths

# 覆盖主线 Backend、Edge 两轮初始化及排空预算，不截断主线操作。
OPERATION_TIMEOUT = 1250.0


class DesktopSession:
    """只持有一个标准 Host；停止失败时调用者必须保留此对象。"""

    def __init__(self, workspace: Path, *, backend_port: int | None = None) -> None:
        self.paths = WorkspacePaths.resolve(workspace)
        self.lease = WorkspaceHostLock(self.paths)
        self.lease.acquire()
        self.host: WorkspaceHost | None = None
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.stop_operation: str | None = None
        try:
            token = ensure_local_token(self.paths)
            self.host = WorkspaceHost(self.paths, token, backend_port=backend_port)
            self.server = ThreadingHTTPServer(('127.0.0.1', 0), _handler_type(self.host))
            endpoint = f'http://127.0.0.1:{self.server.server_port}'
            self.host.publish_endpoint(endpoint)
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
            self.thread.start()
            self.client = WorkspaceHostClient(self.paths, endpoint, token)
        except BaseException:
            self.close()
            raise

    def submit_start(self, parameters: dict[str, object]) -> str:
        """在父进程内同步提交，确保停止不会越过尚未入队的启动请求。"""
        assert self.host is not None
        operation_id = str(uuid.uuid4())
        self.host.submit({'operationId': operation_id, 'command': 'workspace.start',
                          'parameters': parameters})
        return operation_id

    def start(self, parameters: dict[str, object], *, operation_id: str | None = None) -> str:
        self.client.execute('workspace.start', parameters=parameters, operation_id=operation_id, timeout=OPERATION_TIMEOUT)
        snapshot = self.client.snapshot()
        return str(snapshot['components']['backend']['address'])

    def stop(self) -> list[dict[str, str]]:
        # 超时后的重试继续等待同一操作，避免在串行队列里重复提交排空。
        if self.stop_operation is None:
            self.stop_operation = str(self.client.submit('workspace.stop')['operationId'])
        result = self.client.wait(self.stop_operation, timeout=OPERATION_TIMEOUT)
        if result['phase'] != 'succeeded':
            self.stop_operation = None
            failure = result.get('error') or {}
            raise WorkspaceHostError(str(failure.get('code', 'stop_failed')),
                                     str(failure.get('message', '停止未完成，可重试')))
        self.stop_operation = None
        return (result.get('result') or {}).get('warnings', [])

    def processes(self) -> list[dict[str, Any]]:
        snapshot = self.client.snapshot()
        rows = [{'name': 'Host', 'pid': os.getpid(), 'state': 'running', 'exitCode': None}]
        for name in ('backend', 'edge'):
            component = snapshot['components'][name]
            phase = component['phase']
            rows.append({'name': name, 'pid': component.get('pid'),
                         'state': 'running' if phase == 'ready' else
                                  'exited' if phase in {'failed', 'interrupted'} else
                                  'stopped' if phase == 'idle' else 'starting',
                         'exitCode': component.get('exitCode')})
        return rows

    def close(self) -> None:
        """仅在标准停止成功或没有启动组件时释放父进程资源。"""
        if self.server is not None:
            if self.thread is not None and self.thread.is_alive():
                self.server.shutdown()
                self.thread.join(timeout=2)
            self.server.server_close()
        if self.host is not None:
            self.host.close()
        self.lease.release()
