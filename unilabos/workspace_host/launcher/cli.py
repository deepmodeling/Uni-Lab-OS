"""桌面协议入口：一个 Host 父进程和主线 Backend、Edge。"""
from __future__ import annotations

import argparse
import contextlib
import ipaddress
import json
import queue
import signal
import sys
import threading
from pathlib import Path

from .environment import desktop_web_environment, prepare_environment
from .package import DesktopPackage
from .protocol import Protocol
from .runtime import OPERATION_TIMEOUT, DesktopSession


def run(protocol: Protocol) -> None:
    parser = argparse.ArgumentParser(description='UniLabOS 桌面工作区入口')
    parser.add_argument('--workspace', required=True, type=Path)
    parser.add_argument('--bundle-root', type=Path)
    parser.add_argument('--checkout', type=Path)
    parser.add_argument('--dist', type=Path)
    parser.add_argument('--describe', action='store_true')
    parser.add_argument('--bundled-runtime', action='store_true')
    parser.add_argument('--options-stdin', action='store_true')
    parser.add_argument('--control-stdin', action='store_true')
    parser.add_argument('--report-progress', action='store_true')
    parser.add_argument('--parent-process-group', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--web-host', type=lambda value: str(ipaddress.ip_address(value)), default='127.0.0.1')
    parser.add_argument('--web-port', type=int, default=8012)
    args = parser.parse_args()
    if not 0 <= args.web_port <= 65535:
        parser.error('Web 端口必须为 0–65535；0 表示分配可用端口')
    with contextlib.ExitStack() as cleanup:
        _run_session(args, protocol, cleanup)


def _run_session(args: argparse.Namespace, protocol: Protocol, cleanup: contextlib.ExitStack) -> None:
    try:
        options = json.loads(sys.stdin.readline(65537)) if args.options_stdin else {}
        if not isinstance(options, dict):
            raise ValueError('设备包配置必须是对象')
        package = DesktopPackage(args.workspace)
        if args.describe:
            protocol.emit('description', description=package.describe())
            return
        parameters = package.parameters(options)
        prepare_environment(args.checkout)
        dist = args.dist or (args.bundle_root or Path.cwd()) / 'developer-web'
        if not (dist / 'index.html').is_file() or not (dist / 'assets').is_dir():
            raise ValueError(f'缺少前端产物：{dist}')
        if args.bundled_runtime and not (Path(sys.prefix) / '.unilabos-runtime-read-only').is_file():
            raise ValueError('内置 Runtime 不完整')
        requests: queue.Queue[None] = queue.Queue()
        completed = threading.Event()
        protocol.emit('budget', startupTimeoutSeconds=int(OPERATION_TIMEOUT + 45),
                      shutdownTimeoutSeconds=int(OPERATION_TIMEOUT + 45))
        port = cleanup.enter_context(desktop_web_environment(args.web_host, args.web_port, dist))
        session = DesktopSession(package.workspace, backend_port=port)
        cleanup.callback(session.close)
    except Exception as error:
        # 尚未提交业务启动；明确清理完成，外壳可安全释放失败的启动进程。
        protocol.emit('error', message=str(error))
        protocol.emit('stopped')
        return
    startup_submitted = threading.Event()
    stop_warnings: list[dict[str, str]] = []

    def control() -> None:
        for line in sys.stdin:
            if line.strip() == 'shutdown':
                requests.put(None)
        # 管道断开只请求正常停止，排空失败也不强杀或伪造成功。
        requests.put(None)

    if args.control_stdin:
        threading.Thread(target=control, daemon=True).start()
    for name in ('SIGTERM', 'SIGINT'):
        signal.signal(getattr(signal, name), lambda *_: requests.put(None))

    def stop_requests() -> None:
        # 可以提前接收退出请求，但必须等同步启动提交结束后才能提交停止。
        startup_submitted.wait()
        while not completed.is_set():
            requests.get()
            try:
                warnings = session.stop() or []
                stop_warnings[:] = warnings
                for warning in warnings:
                    protocol.emit('progress', message=warning['message'])
                    print(warning['message'], file=sys.stderr, flush=True)
            except Exception as error:
                protocol.emit('stop_failed', message=str(error))
            else:
                completed.set()

    threading.Thread(target=stop_requests, daemon=True).start()

    def progress() -> None:
        while not completed.wait(1):
            try:
                protocol.emit('progress', processes=session.processes())
            except Exception as error:
                print(f'读取工作区状态失败：{error}', file=sys.stderr, flush=True)

    if args.report_progress:
        threading.Thread(target=progress, daemon=True).start()
    try:
        try:
            operation_id = session.submit_start(parameters)
        finally:
            startup_submitted.set()
        url = session.start(parameters, operation_id=operation_id)
        if not completed.is_set():
            protocol.emit('progress', processes=session.processes())
            protocol.emit('ready', url=url)
    except Exception as error:
        protocol.emit('error', message=str(error))
        requests.put(None)
    completed.wait()
    cleanup.close()
    protocol.emit('stopped', **({'warnings': stop_warnings} if stop_warnings else {}))


def main() -> int:
    protocol = Protocol(sys.stdout)
    try:
        # stdout 只发送协议，主线 Host 与 HTTP 服务日志写入 stderr。
        with contextlib.redirect_stdout(sys.stderr):
            run(protocol)
    except Exception as error:
        protocol.emit('error', message=str(error))
        return 1
    return 0
