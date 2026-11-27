"""只读 App 运行时不能被二级 Python/ROS 命令生成的缓存改写。"""
import os
import subprocess
import sys

from unilabos.workspace_host.launcher.environment import prepare_environment


def test_launcher_environment_disables_bytecode_in_secondary_python(tmp_path, monkeypatch):
    # -B 只作用于当前解释器，ROS 命令等另起的 Python 必须通过环境继承。
    monkeypatch.delenv('PYTHONDONTWRITEBYTECODE', raising=False)
    for key in ('PATH', 'CONDA_PREFIX', 'AMENT_PREFIX_PATH', 'COLCON_PREFIX_PATH'):
        monkeypatch.setenv(key, os.environ.get(key, ''))
    (tmp_path / 'probe.py').write_text('VALUE = 1\n')
    prepare_environment(None)
    subprocess.run([sys.executable, '-c', 'import probe'], cwd=tmp_path, check=True)
    assert not list(tmp_path.rglob('*.pyc')), '二级解释器改写了只读运行资源'
