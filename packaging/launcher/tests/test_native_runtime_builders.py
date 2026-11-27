"""Windows/Linux 连续复用必须发布当前 wheel 和对应摘要。"""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import hashlib
import zipfile

import pytest


@pytest.mark.parametrize('target', ['windows', 'linux'])
def test_repeated_native_build_keeps_single_current_wheel(tmp_path, monkeypatch, target):
    spec = importlib.util.spec_from_file_location('native_builder', Path(__file__).parents[1] / f'build-runtime-{target}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / 'source'
    source.mkdir()
    for name in ('setup.py', 'setup.cfg', 'MANIFEST.in', 'LICENSE', 'NOTICE', 'README.md', 'README_zh.md'):
        (source / name).write_text('source')
    (source / 'unilabos').mkdir()
    (source / 'skills').mkdir()
    monkeypatch.setattr(module, 'ROOT', source)
    monkeypatch.setattr(module.sys, 'platform', 'win32' if target == 'windows' else 'linux')
    monkeypatch.setattr(module.platform, 'machine', lambda: 'x86_64')
    monkeypatch.setattr(module.subprocess, 'CREATE_NO_WINDOW', 0, raising=False)
    if target == 'linux':
        monkeypatch.setattr(module, 'running_in', lambda prefix: False)
    output = tmp_path / 'app/os'
    prefix = output / 'runtime'
    python = prefix / ('python.exe' if target == 'windows' else 'bin/python')
    python.parent.mkdir(parents=True)
    python.touch()
    metadata = prefix / 'conda-meta'
    metadata.mkdir()
    packages = ['python-3.11.14-test.conda']
    if target == 'windows':
        packages += ['unilabos-env-0.11.4-test.conda', 'libblas-3.11.0-test_openblas.conda',
                     'libopenblas-0.3.34-test.conda']
    for index, filename in enumerate(packages):
        (metadata / f'package-{index}.json').write_text(json.dumps({'url': 'https://repo/' + filename, 'md5': 'abc'}))
    lock = tmp_path / 'conda-lock.txt'
    lock.write_text('@EXPLICIT\n' + ''.join('https://repo/' + filename + '#abc\n' for filename in packages))
    dependency_lock = tmp_path / 'dependencies.txt'
    dependency_lock.write_text('dependency==1.0\n')
    installer = tmp_path / 'constructor'
    installer.write_bytes(b'constructor')
    info = tmp_path / 'constructor-info.json'
    info.write_text(json.dumps({'_platform': 'win-64', '_installer_hashes': {'sha256': hashlib.sha256(installer.read_bytes()).hexdigest()},
        '_urls': [['https://repo/' + name, 'abc'] for name in packages], '_max_relative_path_length': 209}))
    dependencies = tmp_path / 'dependencies'
    dependencies.mkdir()
    frontend = tmp_path / 'frontend'
    dist = frontend / 'apps/developer-web/dist'
    dist.mkdir(parents=True)
    (dist / 'index.html').write_text('web')
    common_spec = importlib.util.spec_from_file_location('runtime_common', Path(__file__).parents[1] / 'runtime_common.py')
    common = importlib.util.module_from_spec(common_spec)
    common_spec.loader.exec_module(common)
    (dist / '.launcher-build.json').write_text(json.dumps({'commit': 'frontend', 'sourceDirty': False,
        'artifactDigest': common.frontend_digest(dist)}))
    calls = []
    version = ['1.0']
    def run(command, **kwargs):
        calls.append(command)
        if 'download' in command:
            selected = Path(command[command.index('--dest') + 1])
            with zipfile.ZipFile(selected / 'dependency-1.0-py3-none-any.whl', 'w') as archive:
                archive.writestr('dependency/__init__.py', version[0])
        if 'wheel' in command:
            wheels = Path(command[command.index('--wheel-dir') + 1])
            with zipfile.ZipFile(wheels / f'unilabos-{version[0]}-py3-none-any.whl', 'w') as archive:
                archive.writestr('unilabos/__init__.py', version[0])
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(module.subprocess, 'run', run)
    monkeypatch.setattr(module.subprocess, 'check_output', lambda *a, **kw: 'commit' if kw.get('text') else b'')
    monkeypatch.setattr(sys, 'argv', ['build', '--output', str(output), '--runtime-installer', str(installer),
        '--conda-lock', str(lock), '--dependency-wheels', str(dependencies), '--dependency-lock', str(dependency_lock),
        '--frontend', str(frontend), '--reuse-runtime', '--skip-frontend-build',
        *(['--constructor-info', str(info)] if target == 'windows' else [])])
    for value in ('1.0', '2.0'):
        version[0] = value
        module.main()
        wheels = list((output / 'wheels').glob('unilabos-*.whl'))
        assert len(wheels) == 1
        with zipfile.ZipFile(wheels[0]) as archive:
            assert archive.read('unilabos/__init__.py') == value.encode()
        record = json.loads((output / 'runtime-manifest.json').read_text())
        assert record['wheelDigest'] == common.digest(wheels[0])
        for name, expected in record['installationSupport']['files'].items():
            assert (output / 'installation-support' / name).read_bytes() == (Path(__file__).parents[1] / name).read_bytes()
            assert common.digest(output / 'installation-support' / name) == expected
        assert record['dependencyLockDigest'] == common.digest(dependency_lock)
        dependency = output / 'dependency-wheels' / 'dependency-1.0-py3-none-any.whl'
        assert record['dependencyWheelDigests'] == {dependency.name: common.digest(dependency)}
        with zipfile.ZipFile(dependency) as archive:
            assert archive.read('dependency/__init__.py') == value.encode()
        assert (prefix / '.unilabos-runtime-read-only').is_file()
        install = next(command for command in reversed(calls) if '--force-reinstall' in command)
        assert install[-1] == str(wheels[0])
