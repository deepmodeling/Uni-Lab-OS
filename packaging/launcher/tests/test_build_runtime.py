"""构建入口在拒绝参数和复用产物时保留真实来源。"""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest

SCRIPT = Path(__file__).parents[1] / 'build-runtime.py'


@pytest.fixture
def builder(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('runtime_builder', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / 'source'
    here = source / 'packaging/launcher'
    here.mkdir(parents=True)
    for name in ['ros-osx-arm64.explicit.txt', 'runtime-requirements.lock.txt']:
        (here / name).write_text('@EXPLICIT\nhttps://repo/package-a.conda\n')
    for name in ('setup.py', 'setup.cfg', 'MANIFEST.in', 'LICENSE', 'NOTICE', 'README.md', 'README_zh.md'):
        (source / name).write_text('source')
    (source / 'unilabos').mkdir()
    (source / 'skills').mkdir()
    module.HERE, module.ROOT = here, source
    monkeypatch.setattr(module.sys, 'platform', 'darwin')
    monkeypatch.setattr(module.platform, 'machine', lambda: 'arm64')
    monkeypatch.setenv('MACOSX_DEPLOYMENT_TARGET', '15.0')
    monkeypatch.setattr(module.subprocess, 'run', Mock(return_value=subprocess.CompletedProcess([], 1)))
    monkeypatch.setattr(module, 'revision', lambda root: 'CURRENT_HEAD')
    def export(command, **kwargs):
        if command[0] == 'conda':
            return '@EXPLICIT\nhttps://repo/package-a.conda\n'
        return b'' if not kwargs.get('text') else ''
    monkeypatch.setattr(module.subprocess, 'check_output', export)
    def run(command, **kwargs):
        if 'wheel' in command:
            wheels = Path(command[command.index('--wheel-dir') + 1])
            wheels.mkdir(parents=True, exist_ok=True)
            name = 'dependency-1.2.3-py3-none-any.whl' if '-r' in command else 'unilabos-0.0.1.whl'
            (wheels / name).write_bytes(b'wheel')
        if 'download' in command:
            wheels = Path(command[command.index('--dest') + 1])
            wheels.mkdir()
            (wheels / 'dependency-1.2.3-py3-none-any.whl').write_bytes(b'wheel')
    monkeypatch.setattr(module, 'run', run)
    return module


def existing(tmp_path):
    output = tmp_path / 'output'
    (output / 'runtime/bin').mkdir(parents=True)
    (output / 'runtime/bin/python').touch()
    (output / 'runtime/conda-meta').mkdir()
    marker = output / 'runtime-manifest.json'
    marker.write_text('{"valid":true}')
    frontend = tmp_path / 'frontend'
    dist = frontend / 'apps/developer-web/dist'
    dist.mkdir(parents=True)
    (dist / 'index.html').write_text('old frontend')
    return output, marker, frontend, dist


def test_rejected_rebuild_keeps_valid_manifest(builder, tmp_path, monkeypatch):
    output, marker, frontend, _ = existing(tmp_path)
    monkeypatch.setattr(sys, 'argv', ['build', '--output', str(output)])
    with pytest.raises(SystemExit):
        builder.main()
    assert marker.read_text() == '{"valid":true}'


def test_reuse_records_target_conda_and_original_frontend(builder, tmp_path, monkeypatch):
    output, marker, frontend, dist = existing(tmp_path)
    # 新代码使用产物摘要校验旧 dist；旧代码忽略元数据，错误地标为当前 HEAD。
    digest = hashlib.sha256(b'index.html\0old frontend\0').hexdigest()
    (dist / '.launcher-build.json').write_text(json.dumps({
        'commit': 'FRONTEND_BUILD_A', 'sourceDirty': False, 'artifactDigest': digest}))
    (builder.HERE / 'build').mkdir()
    (builder.HERE / 'build/runtime.explicit.txt').write_text('OTHER_OUTPUT')
    monkeypatch.setattr(sys, 'argv', ['build', '--output', str(output), '--frontend', str(frontend),
        '--reuse-runtime', '--skip-frontend-build'])
    (output / 'unilabos-old.whl').write_bytes(b'legacy')
    builder.main()
    assert not list(output.glob('unilabos-*.whl'))
    assert len(list((output / 'wheels').glob('unilabos-*.whl'))) == 1
    result = json.loads(marker.read_text())
    lock = (output / 'ros-osx-arm64.explicit.txt').read_bytes()
    assert result['condaLockDigest'] == hashlib.sha256(lock).hexdigest()
    assert result['frontendCommit'] == 'FRONTEND_BUILD_A'
    assert result['minimumMacOS'] == '15.0'
    dependency = output / 'dependency-wheels/dependency-1.2.3-py3-none-any.whl'
    assert result['dependencyWheelDigests'] == {dependency.name: builder.common.digest(dependency)}
    for name, expected in result['installationSupport']['files'].items():
        assert builder.common.digest(output / 'installation-support' / name) == expected


def test_reuse_rejects_changed_conda_before_mutation(builder, tmp_path, monkeypatch):
    output, marker, frontend, _ = existing(tmp_path)
    (builder.HERE / 'ros-osx-arm64.explicit.txt').write_text('@EXPLICIT\nhttps://repo/package-b.conda\n')
    monkeypatch.setattr(sys, 'argv', ['build', '--output', str(output), '--reuse-runtime'])
    with pytest.raises(SystemExit):
        builder.main()
    assert marker.read_text() == '{"valid":true}'


def test_skip_rejects_missing_or_modified_frontend_before_mutation(builder, tmp_path, monkeypatch):
    output, marker, frontend, dist = existing(tmp_path)
    monkeypatch.setattr(sys, 'argv', ['build', '--output', str(output), '--frontend', str(frontend),
        '--reuse-runtime', '--skip-frontend-build'])
    with pytest.raises(SystemExit):
        builder.main()
    assert marker.read_text() == '{"valid":true}'
    (dist / '.launcher-build.json').write_text(json.dumps({
        'commit': 'A', 'sourceDirty': False, 'artifactDigest': 'tampered'}))
    with pytest.raises(SystemExit):
        builder.main()
    assert marker.read_text() == '{"valid":true}'


def test_seed_lock_and_actual_environment_are_output_specific(builder, tmp_path, monkeypatch):
    output, marker, frontend, dist = existing(tmp_path)
    (dist / '.launcher-build.json').write_text(json.dumps({
        'commit': 'A', 'sourceDirty': True, 'artifactDigest': builder.frontend_digest(dist)}))
    actual = '@EXPLICIT\nhttps://seed/package-c.conda\n'
    original = builder.subprocess.check_output
    monkeypatch.setattr(builder.subprocess, 'check_output',
        lambda cmd, **kwargs: actual if cmd[0] == 'conda' else original(cmd, **kwargs))
    monkeypatch.setattr(sys, 'argv', ['build', '--output', str(output), '--frontend', str(frontend),
        '--reuse-runtime', '--seed-runtime', str(tmp_path / 'seed'), '--skip-frontend-build'])
    builder.main()
    assert (output / 'ros-osx-arm64.explicit.txt').read_text() == actual
    assert json.loads(marker.read_text())['condaLockDigest'] == hashlib.sha256(actual.encode()).hexdigest()


def test_custom_dependency_lock_is_installed_copied_and_recorded(builder, tmp_path, monkeypatch):
    output, marker, frontend, dist = existing(tmp_path)
    (dist / '.launcher-build.json').write_text(json.dumps({
        'commit': 'A', 'sourceDirty': False, 'artifactDigest': builder.frontend_digest(dist)}))
    lock = tmp_path / 'custom dependency lock.txt'
    lock.write_text('custom-package==1.2.3\n')
    wheels = tmp_path / '离线缓存'
    wheels.mkdir()
    calls = []
    original = builder.run
    def capture(command, **kwargs):
        calls.append(command)
        original(command, **kwargs)
    monkeypatch.setattr(builder, 'run', capture)
    monkeypatch.setattr(sys, 'argv', ['build', '--output', str(output), '--frontend', str(frontend),
        '--reuse-runtime', '--skip-frontend-build', '--dependency-lock', str(lock),
        '--dependency-wheels', str(wheels), '--minimum-macos', '15.0'])
    builder.main()
    build = next(command for command in calls if 'wheel' in command and '-r' in command)
    assert build[build.index('--find-links') + 1] == str(wheels)
    assert build[build.index('-r') + 1] == str(lock)
    download = next(command for command in calls if 'download' in command)
    assert download[download.index('--platform') + 1] == 'macosx_15_0_arm64'
    install = next(command for command in calls if '--only-binary=:all:' in command and 'install' in command)
    assert install[install.index('--find-links') + 1] == str(output / 'dependency-wheels')
    assert (output / 'runtime-requirements.lock.txt').read_bytes() == lock.read_bytes()
    assert json.loads(marker.read_text())['dependencyLockDigest'] == hashlib.sha256(lock.read_bytes()).hexdigest()


def test_missing_custom_lock_preserves_ready_manifest(builder, tmp_path, monkeypatch):
    output, marker, _, _ = existing(tmp_path)
    monkeypatch.setattr(sys, 'argv', ['build', '--output', str(output), '--dependency-lock', str(tmp_path / 'missing')])
    with pytest.raises(SystemExit):
        builder.main()
    assert marker.read_text() == '{"valid":true}'


def test_failed_update_revokes_both_ready_markers(builder, tmp_path, monkeypatch):
    output, manifest, frontend, dist = existing(tmp_path)
    ready = output / 'runtime/.unilabos-runtime-read-only'
    ready.write_text('launcher-runtime-v2\n')
    (dist / '.launcher-build.json').write_text(json.dumps({
        'commit': 'A', 'sourceDirty': False, 'artifactDigest': builder.frontend_digest(dist)}))
    def failed_install(command, **kwargs):
        raise RuntimeError('dependency installation failed')
    monkeypatch.setattr(builder, 'run', failed_install)
    monkeypatch.setattr(sys, 'argv', ['build', '--output', str(output), '--frontend', str(frontend),
        '--reuse-runtime', '--skip-frontend-build'])
    with pytest.raises(RuntimeError, match='dependency installation failed'):
        builder.main()
    assert not manifest.exists()
    assert not ready.exists()
