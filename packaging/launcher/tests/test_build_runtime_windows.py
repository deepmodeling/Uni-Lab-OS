"""Windows 环境复用不能把旧 Runtime 标记成新的运行包。"""
import importlib.util
import json
import os
from pathlib import Path

import pytest


def builder():
    spec = importlib.util.spec_from_file_location('windows_builder', Path(__file__).parents[1] / 'build-runtime-windows.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_environment_excludes_active_conda_dll_paths(tmp_path, monkeypatch):
    module = builder()
    foreign = tmp_path / 'base'
    prefix = tmp_path / 'runtime'
    system = tmp_path / 'Windows'
    monkeypatch.setenv('PATH', str(foreign / 'Library/bin'))
    monkeypatch.setenv('CONDA_PREFIX', str(foreign))
    monkeypatch.setenv('AMENT_PREFIX_PATH', str(foreign / 'Library'))
    monkeypatch.setenv('COLCON_PREFIX_PATH', str(foreign))
    monkeypatch.setenv('SystemRoot', str(system))
    environment = module.runtime_environment(prefix)
    assert environment['PATH'].split(os.pathsep) == list(map(str, [
        prefix, prefix / 'Library/bin', prefix / 'Scripts', system / 'System32']))
    assert environment['CONDA_PREFIX'] == str(prefix)
    assert environment['AMENT_PREFIX_PATH'] == str(prefix / 'Library')
    assert environment['COLCON_PREFIX_PATH'] == str(prefix)
    assert os.environ['CONDA_PREFIX'] == str(foreign)


def test_conda_identity_requires_exact_packages_and_hashes(tmp_path):
    module = builder()
    metadata = tmp_path / 'runtime/conda-meta'
    metadata.mkdir(parents=True)
    record = metadata / 'python.json'
    record.write_text(json.dumps({'url': 'https://repo/python.conda', 'md5': 'abc'}))
    lock = tmp_path / 'lock.txt'
    lock.write_text('# platform: win-64\n@EXPLICIT\nhttps://repo/python.conda#abc\n')
    module.common.verify_conda(metadata.parent, lock)
    record.write_text(json.dumps({'url': 'https://repo/python.conda', 'md5': 'changed'}))
    with pytest.raises(ValueError, match='精确锁'):
        module.common.verify_conda(metadata.parent, lock)
    record.unlink()
    with pytest.raises(ValueError, match='精确锁'):
        module.common.verify_conda(metadata.parent, lock)
