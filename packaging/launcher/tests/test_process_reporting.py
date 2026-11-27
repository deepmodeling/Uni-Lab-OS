"""进程状态扩展保持协议兼容，停止结果必须显式回复。"""
import io
import json
from unilabos.workspace_host.launcher.protocol import Protocol


def test_process_reporting_and_retryable_stop_events():
    output = io.StringIO()
    protocol = Protocol(output)
    protocol.emit('progress', processes=[{'name':'Host','pid':1,'state':'running','exitCode':None}])
    protocol.emit('stop_failed', message='排空失败')
    protocol.emit('stopped')
    messages = [json.loads(line) for line in output.getvalue().splitlines()]
    assert all(m['protocolVersion'] == 2 for m in messages)
    assert messages[0]['message']
    assert messages[1]['type'] == 'stop_failed'
    assert messages[2]['type'] == 'stopped'
