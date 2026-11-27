#!/usr/bin/env python3
"""从交付物冷启动空设备包，验证真实接口、浏览器入口和退出清理。"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import queue
import shutil
import socket
import threading
import signal
import subprocess
import tempfile
import time
from urllib.request import ProxyHandler, build_opener


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--package', type=Path, default=Path(__file__).parent / 'examples/device-package')
    args = parser.parse_args()
    root = args.runtime.resolve()
    python = root / ('runtime/python.exe' if os.name == 'nt' else 'runtime/bin/python')
    with tempfile.TemporaryDirectory(prefix='lab-os-smoke-') as temp:
        folder = Path(temp).resolve()
        package = folder / 'package'
        shutil.copytree(args.package.resolve(), package,
                        ignore=shutil.ignore_patterns('.git', '.unilabos', '__pycache__'))
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            web_port = listener.getsockname()[1]
        log_path = folder / 'runtime.log'
        environment = {key: os.environ[key] for key in [
            'HOME', 'TMPDIR', 'LANG', 'SystemRoot', 'WINDIR', 'TEMP', 'TMP',
            'USERPROFILE', 'APPDATA', 'LOCALAPPDATA'] if key in os.environ}
        environment.update(PATH=str(Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32')
                           if os.name == 'nt' else '/usr/bin:/bin',
                           PYTHONUNBUFFERED='1', PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
        with log_path.open('w', encoding='utf-8') as log:
            process = subprocess.Popen([str(python), '-X', 'utf8', '-I', '-B', '-m', 'unilabos.workspace_host.launcher', '--bundle-root', str(root),
                '--bundled-runtime', '--control-stdin', '--options-stdin', '--report-progress',
                *(['--parent-process-group'] if os.name != 'nt' else []), '--workspace', str(package), '--web-host', '127.0.0.1', '--web-port', str(web_port)],
                cwd=folder, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=log, text=True, encoding='utf-8', start_new_session=os.name != 'nt',
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            # Windows select 不接受匿名管道，统一使用读线程和有期限队列。
            messages = queue.Queue()
            def read_output():
                for line in process.stdout:
                    messages.put(line)
                messages.put(None)
            threading.Thread(target=read_output, daemon=True).start()
            try:
                process.stdin.write('{}\n'); process.stdin.flush()
                deadline = time.monotonic() + 240
                url = None
                while time.monotonic() < deadline:
                    try:
                        line = messages.get(timeout=1)
                    except queue.Empty:
                        line = ''
                    if line is None:
                        raise RuntimeError('运行入口提前退出')
                    if line:
                        if not line: raise RuntimeError('运行入口提前退出')
                        message = json.loads(line)
                        print(message, flush=True)
                        if message.get('type') == 'error': raise RuntimeError(message['message'])
                        assert message.get('protocolVersion') == 2
                        if 'url' in message: url = message['url']; break
                    if process.poll() is not None: raise RuntimeError(f'入口退出码：{process.returncode}')
                if not url: raise RuntimeError('等待运行入口就绪超时')
                opener = build_opener(ProxyHandler({}))
                for path in ['/', '/tasks?task=example', '/workflows', '/api/v1/devices', '/api/v1/workflows', '/api/v1/readiness']:
                    with opener.open(url + path, timeout=15) as response:
                        body = response.read()
                        if path.startswith('/api/'):
                            assert 'application/json' in response.headers['Content-Type']
                            json.loads(body)
                        print(f'{path}: {response.status}', flush=True)
                process.stdin.write('shutdown\n'); process.stdin.flush()
                assert process.wait(timeout=60) == 0
                print('冷启动、真实接口与正常清理通过', flush=True)
            except BaseException:
                print(log_path.read_text(encoding='utf-8', errors='replace')[-16000:], flush=True)
                raise
            finally:
                if process.poll() is None:
                    try:
                        process.stdin.write('shutdown\n'); process.stdin.flush(); process.wait(timeout=60)
                    except (OSError, subprocess.TimeoutExpired):
                        if os.name == 'nt':
                            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                                           capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                        else:
                            os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                process.stdin.close(); process.stdout.close()


if __name__ == '__main__':
    main()
