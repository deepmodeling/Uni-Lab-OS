"""重复构建仅发布本次 wheel，不按旧文件时间猜测产物。"""
import importlib.util
import os
from pathlib import Path
import subprocess

import pytest


@pytest.fixture
def common():
    spec = importlib.util.spec_from_file_location('runtime_common', Path(__file__).parents[1] / 'runtime_common.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def source(tmp_path):
    root = tmp_path / 'source with spaces'
    root.mkdir()
    for name in ('setup.py', 'setup.cfg', 'MANIFEST.in', 'LICENSE', 'NOTICE', 'README.md', 'README_zh.md'):
        (root / name).write_text('source')
    (root / 'unilabos').mkdir()
    (root / 'unilabos/__init__.py').write_text('')
    skill = root / 'skills/unilab-domain-repo-builder/SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('bundled skill')
    (root / 'build/lib').mkdir(parents=True)
    (root / 'build/lib/stale.py').write_text('stale')
    return root


@pytest.mark.parametrize('version', ['1.0', '2.0'])
def test_rebuild_replaces_same_or_old_version_without_mtime_selection(common, source, tmp_path, version):
    destination = tmp_path / 'output/wheels'
    destination.mkdir(parents=True)
    old = destination / 'unilabos-1.0-py3-none-any.whl'
    old.write_bytes(b'old')
    os.utime(old, (2208988800, 2208988800))
    unrelated = destination / 'dependency.whl'
    unrelated.write_bytes(b'keep')
    def run(command):
        stage = Path(command[-1])
        assert not (stage / 'build').exists()
        assert (stage / 'unilabos/__init__.py').is_file()
        assert (stage / 'skills/unilab-domain-repo-builder/SKILL.md').read_text() == 'bundled skill'
        wheels = Path(command[command.index('--wheel-dir') + 1])
        assert list(wheels.iterdir()) == []
        (wheels / f'unilabos-{version}-py3-none-any.whl').write_bytes(b'new')
    wheel = common.build_wheel(source, destination, ['python', '-m', 'pip'], run)
    assert list(destination.glob('unilabos-*.whl')) == [wheel]
    assert wheel.name == f'unilabos-{version}-py3-none-any.whl'
    assert wheel.read_bytes() == b'new'
    assert unrelated.read_bytes() == b'keep'


@pytest.mark.parametrize('failure', ['command', 'missing', 'ambiguous'])
def test_failed_or_ambiguous_build_preserves_previous_wheel(common, source, tmp_path, failure):
    destination = tmp_path / 'wheels'
    destination.mkdir()
    old = destination / 'unilabos-old.whl'
    old.write_bytes(b'old')
    def run(command):
        if failure == 'command':
            raise subprocess.CalledProcessError(1, command)
        if failure == 'ambiguous':
            wheels = Path(command[command.index('--wheel-dir') + 1])
            for version in ('1', '2'):
                (wheels / f'unilabos-{version}.whl').write_bytes(b'new')
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        common.build_wheel(source, destination, ['python', '-m', 'pip'], run)
    assert list(destination.iterdir()) == [old]
    assert old.read_bytes() == b'old'
