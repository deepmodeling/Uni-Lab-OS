"""桌面必须共享主线目录、锁、操作队列与停止失败语义。"""
import json
import os
from pathlib import Path

import pytest

from unilabos.workspace_host.client import WorkspaceHostClient
from unilabos.workspace_host.launcher.manifest import load_descriptor
from unilabos.workspace_host.launcher.package import DesktopPackage
from unilabos.workspace_host.launcher.runtime import DesktopSession
from unilabos.workspace_host.model import WorkspaceHostError, WorkspacePaths


@pytest.fixture
def package(tmp_path):
    root = tmp_path / 'package'
    (root / 'deployment/graphs').mkdir(parents=True)
    (root / 'deployment/graphs/local-sim.json').write_text('{"nodes":[],"links":[]}')
    (root / 'deployment/local_config.py').write_text('')
    (root / 'unilab-app.toml').write_text('schema_version=2\npackage_id="demo"\ndisplay_name="示例"\n[runtime]\nfile="app.json"\n')
    (root / 'app.json').write_text(json.dumps({'schemaVersion':2,'startup':{
        'graph':'deployment/graphs/local-sim.json','actionMode':'real'}}))
    return root


def test_external_services_are_rejected(package):
    path = package / 'app.json'
    spec = json.loads(path.read_text())
    spec['services'] = [{'id':'modbus'}]
    path.write_text(json.dumps(spec))
    with pytest.raises(ValueError, match='外部服务'):
        load_descriptor(path)


def test_package_maps_to_mainline_options_without_rewriting_graph(package):
    before = (package / 'deployment/graphs/local-sim.json').read_bytes()
    values = DesktopPackage(package).parameters({})
    assert values == {'graphPath': str(package/'deployment/graphs/local-sim.json'), 'runtimeMode':'normal'}
    assert before == (package / 'deployment/graphs/local-sim.json').read_bytes()


def test_desktop_stop_returns_cleanup_warning(package, monkeypatch):
    """软件停止成功时仍向桌面调用方返回物理状态未知告警。"""
    session = DesktopSession(package)
    warning = {'code': 'scheduler_exited_before_drain', 'message': '无法确认设备作业状态，请人工核对设备。'}
    monkeypatch.setattr(session.host, '_stop_workspace', lambda: {'warnings': [warning]})
    try:
        assert session.stop() == [warning]
        assert session.stop_operation is None
    finally:
        session.close()


def test_desktop_uses_cli_host_identity_and_failed_stop_remains_retryable(package, monkeypatch):
    session = DesktopSession(package)
    try:
        assert session.paths == WorkspacePaths.resolve(package)
        assert session.paths.runtime.is_relative_to(package)
        discovered = WorkspaceHostClient.discover(package)
        assert discovered.snapshot()['host']['pid'] == os.getpid()
        with pytest.raises(WorkspaceHostError, match='已有'):
            DesktopSession(package)
        calls = []
        def stop():
            calls.append(True)
            if len(calls) == 1:
                raise WorkspaceHostError('drain_failed','排空失败')
            return session.host.snapshot()
        monkeypatch.setattr(session.host, '_stop_workspace', stop)
        with pytest.raises(WorkspaceHostError, match='排空失败'):
            session.stop()
        assert discovered.snapshot()['host']['phase'] == 'ready'
        assert session.lease._stream is not None
        session.stop()
        assert len(calls) == 2
        assert [p['name'] for p in session.processes()] == ['Host','backend','edge']
    finally:
        session.close()
    replacement = DesktopSession(package)
    replacement.close()


def test_launcher_publishes_bound_console_port_before_start(package, tmp_path, monkeypatch):
    """两个启动实例使用各自实际端口，退出后不污染父进程环境。"""
    import signal
    import sys

    from unilabos.app.center_client.console_address import LAUNCHER_CONSOLE_URL_ENV
    from unilabos.workspace_host.launcher import cli

    dist = tmp_path / 'web'
    dist.mkdir()
    (dist / 'assets').mkdir()
    (dist / 'index.html').write_text('页面')
    monkeypatch.setattr(sys, 'argv', ['launcher', '--workspace', str(package), '--dist', str(dist),
                                    '--web-host', '127.0.0.1', '--web-port', '0'])
    monkeypatch.setattr(cli, 'prepare_environment', lambda _: None)
    monkeypatch.setenv(LAUNCHER_CONSOLE_URL_ENV, 'http://previous.test:9999')
    handlers, inherited, ready = {}, [], []
    monkeypatch.setattr(cli.signal, 'signal', lambda name, handler: handlers.update({name: handler}))

    class Session:
        def __init__(self, _, *, backend_port):
            self.url = f'http://127.0.0.1:{backend_port}'

        def submit_start(self, _):
            inherited.append(os.environ[LAUNCHER_CONSOLE_URL_ENV])
            return 'operation'

        def start(self, *_args, **_kwargs):
            return self.url

        def processes(self):
            return []

        def stop(self):
            pass

        def close(self):
            pass

    class Protocol:
        def emit(self, event, **payload):
            if event == 'ready':
                ready.append(payload['url'])
                handlers[signal.SIGTERM](None, None)

    monkeypatch.setattr(cli, 'DesktopSession', Session)
    cli.run(Protocol())
    cli.run(Protocol())
    assert inherited == ready and len(ready) == 2
    assert all(url.startswith('http://127.0.0.1:') and not url.endswith(':0') for url in ready)
    assert os.environ[LAUNCHER_CONSOLE_URL_ENV] == 'http://previous.test:9999'


def test_file_picker_absolute_graph_is_equivalent_and_cannot_escape(package, tmp_path):
    path = package / 'app.json'
    spec = json.loads(path.read_text())
    spec['startup']['graph'] = '${options.graph}'
    spec['configuration'] = [{'key':'graph','label':'设备图','type':'file','required':True,
                              'extensions':['json']}]
    path.write_text(json.dumps(spec))
    desktop = DesktopPackage(package)
    relative = desktop.parameters({'graph':'deployment/graphs/local-sim.json'})
    assert desktop.parameters({'graph':str(package/'deployment/graphs/local-sim.json')}) == relative
    external = tmp_path/'outside.json'
    external.write_text('{}')
    with pytest.raises(ValueError, match='包内'):
        desktop.parameters({'graph':str(external)})


def test_stop_during_start_waits_for_start_and_does_not_restart_after_stop(package, monkeypatch):
    session = DesktopSession(package)
    calls = []
    try:
        def dispatch(command, parameters):
            calls.append(command)
            return session.host.snapshot()
        monkeypatch.setattr(session.host, '_dispatch', dispatch)
        operation = session.client.submit('workspace.start', parameters={})
        # 模拟控制线程先收到立即停止，主线程随后才等待启动结果。
        session.stop()
        session.start({}, operation_id=operation['operationId'])
        assert calls == ['workspace.start', 'workspace.stop']
    finally:
        session.close()


def test_rejected_startup_confirms_no_owned_runtime_and_exits(tmp_path):
    import subprocess
    import sys
    result = subprocess.run([sys.executable, '-B', '-m', 'unilabos.workspace_host.launcher',
                             '--workspace', str(tmp_path/'missing')],
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0
    messages = [json.loads(line) for line in result.stdout.splitlines()]
    assert [message['type'] for message in messages] == ['error','stopped']
    assert not (tmp_path/'missing').exists()


@pytest.mark.parametrize('failure_stage', ['before_submit', 'initial_submit', 'wait_submit'])
@pytest.mark.parametrize('drain_fails_once', [False, True])
def test_launcher_submission_failure_stops_before_releasing_host(
    package, tmp_path, monkeypatch, failure_stage, drain_fails_once,
):
    """提交已接受但调用失败时排空；停止失败仍保留 Host 并支持重试。"""
    import signal
    import sys

    from unilabos.workspace_host.launcher import cli

    dist = tmp_path / 'web'
    dist.mkdir()
    (dist / 'assets').mkdir()
    (dist / 'index.html').write_text('页面')
    monkeypatch.setattr(sys, 'argv', ['launcher', '--workspace', str(package), '--dist', str(dist), '--web-port', '0'])
    monkeypatch.setattr(cli, 'prepare_environment', lambda _: None)
    handlers = {}
    monkeypatch.setattr(cli.signal, 'signal', lambda name, handler: handlers.update({name: handler}))
    calls = []
    sessions = []

    def create_session(workspace, *, backend_port):
        session = DesktopSession(workspace, backend_port=backend_port)
        sessions.append(session)

        def dispatch(command, parameters):
            calls.append(command)
            if command == 'workspace.stop' and drain_fails_once and calls.count(command) == 1:
                raise WorkspaceHostError('drain_failed', '排空失败')
            return session.host.snapshot()

        monkeypatch.setattr(session.host, '_dispatch', dispatch)
        if failure_stage in {'before_submit', 'initial_submit'}:
            original = session.host.submit

            def submit(payload):
                if payload['command'] == 'workspace.start':
                    if failure_stage == 'before_submit':
                        raise TimeoutError('启动尚未入队')
                    # 提交期间到达的停止信号也不能越过启动操作。
                    handlers[signal.SIGTERM](None, None)
                result = original(payload)
                if payload['command'] == 'workspace.start':
                    # 同步提交即使在入队后抛错，停止也必须排在它之后。
                    raise TimeoutError('启动提交结果未知')
                return result

            monkeypatch.setattr(session.host, 'submit', submit)
        else:
            original = session.client.submit

            def submit(command, **kwargs):
                result = original(command, **kwargs)
                if command == 'workspace.start':
                    raise TimeoutError('启动响应超时')
                return result

            monkeypatch.setattr(session.client, 'submit', submit)
        return session

    monkeypatch.setattr(cli, 'DesktopSession', create_session)
    events = []

    class Protocol:
        def emit(self, event, **payload):
            events.append(event)
            if event == 'stop_failed':
                assert sessions[0].lease._stream is not None
                assert sessions[0].client.snapshot()['host']['phase'] == 'ready'
                handlers[signal.SIGTERM](None, None)

    cli.run(Protocol())
    starts = [] if failure_stage == 'before_submit' else ['workspace.start']
    assert calls == starts + ['workspace.stop'] * (2 if drain_fails_once else 1)
    assert 'error' in events and 'ready' not in events
    assert events[-1] == 'stopped'
    assert events.count('stop_failed') == int(drain_fails_once)
    assert sessions[0].lease._stream is None
    replacement = DesktopSession(package)
    replacement.close()


def test_desktop_rejects_busy_port_without_starting_or_releasing_its_owner(tmp_path):
    import socket

    from unilabos.workspace_host.launcher.environment import desktop_web_environment

    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        port = listener.getsockname()[1]
        with pytest.raises(OSError), desktop_web_environment('127.0.0.1', port, tmp_path):
            pytest.fail('已占用的端口不能用于本次启动')
        with socket.create_connection(('127.0.0.1', port), timeout=1):
            pass
