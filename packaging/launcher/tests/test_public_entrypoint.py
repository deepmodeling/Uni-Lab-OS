"""描述读取不创建工作区、适配器或外部服务。"""
import json
from pathlib import Path
import subprocess
import sys


def test_describe_is_pure_and_uses_existing_protocol():
    package = Path(__file__).parents[1] / 'examples/device-package'
    result = subprocess.run([sys.executable, '-B', '-m', 'unilabos.workspace_host.launcher',
                             '--describe', '--workspace', str(package)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload['protocolVersion'] == 2
    assert payload['type'] == 'description'
    assert not (package / '.unilabos').exists()
