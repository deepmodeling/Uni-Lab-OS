"""安装载荷校验必须拒绝损坏与越界路径。"""
import importlib.util
import json
from pathlib import Path

import pytest


def helper():
    spec = importlib.util.spec_from_file_location('prepare_windows', Path(__file__).parents[1] / 'prepare-runtime.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_payload_digest_detects_corruption(tmp_path):
    module = helper()
    binary = tmp_path / 'launcher.exe'
    binary.write_bytes(b'expected')
    (tmp_path / 'payload-manifest.json').write_text(json.dumps({
        'schemaVersion': 1, 'files': {'launcher.exe': module.sha256(binary)}}))
    module.verify_payload(tmp_path)
    binary.write_bytes(b'changed')
    with pytest.raises(ValueError, match='摘要'):
        module.verify_payload(tmp_path)


@pytest.mark.parametrize('path', ['../outside', '/absolute', 'C:/outside', 'os\\..\\outside'])
def test_payload_never_accepts_external_paths(tmp_path, path):
    module = helper()
    (tmp_path / 'payload-manifest.json').write_text(json.dumps({'schemaVersion': 1, 'files': {path: 'hash'}}))
    with pytest.raises(ValueError, match='路径'):
        module.verify_payload(tmp_path)


def test_failed_prepare_removes_all_ready_markers(tmp_path):
    module = helper()
    base = tmp_path / 'os'
    (base / 'runtime').mkdir(parents=True)
    marker = base / 'runtime/.unilabos-runtime-read-only'
    manifest = base / 'runtime-manifest.json'
    marker.touch()
    manifest.write_text('{}')
    with pytest.raises(FileNotFoundError):
        module.prepare(base)
    assert not marker.exists()
    assert not manifest.exists()


@pytest.mark.parametrize('platform_name', ['win32', 'linux', 'darwin'])
def test_success_binds_manifest_to_final_install_directory(tmp_path, monkeypatch, platform_name):
    module = helper()
    base = tmp_path / ('安装目录 with spaces/Lab OS Launcher.app/Contents/Resources/os' if platform_name == 'darwin' else '安装目录 with spaces/os')
    (base / 'runtime/conda-meta').mkdir(parents=True)
    (base / 'runtime/conda-meta/python.json').write_text(json.dumps({'url': 'https://repo/python.conda', 'md5': 'abc'}))
    lock = base / ('ros-win-64.explicit.txt' if platform_name == 'win32' else ('ros-osx-arm64.explicit.txt' if platform_name == 'darwin' else 'ros-linux-64.explicit.txt'))
    lock.write_text('@EXPLICIT\nhttps://repo/python.conda#abc\n')
    dependency = base / 'runtime-requirements.lock.txt'
    dependency.write_text('example==1\n')
    (base / 'wheels').mkdir()
    wheel = base / 'wheels/unilabos-0.0.1.whl'
    wheel.write_bytes(b'wheel')
    template = {'wheelDigest': module.sha256(wheel), 'dependencyLockDigest': module.sha256(dependency),
                'condaLockDigest': module.sha256(lock), 'protocolVersion': 2}
    (base / 'runtime-manifest.template.json').write_text(json.dumps(template))
    payload_root = base.parents[2] if platform_name == 'darwin' else base.parent
    files = {path.relative_to(payload_root).as_posix(): module.sha256(path)
             for path in base.rglob('*') if path.is_file()}
    (base.parent / 'payload-manifest.json').write_text(json.dumps({'schemaVersion': 1, 'files': files}))
    calls = []
    monkeypatch.setattr(module.subprocess, 'run', lambda command, **kwargs: calls.append((command, kwargs)))
    module.prepare(base, platform_name=platform_name)
    result = json.loads((base / 'runtime-manifest.json').read_text(encoding='utf-8'))
    assert result['runtimePrefix'] == str(base / 'runtime')
    assert (base / 'runtime/.unilabos-runtime-read-only').is_file()
    assert len(calls) == 4
    assert '--no-index' in calls[0][0] and '--only-binary=:all:' in calls[0][0]
    expected_python = base / ('runtime/python.exe' if platform_name == 'win32' else 'runtime/bin/python')
    assert all(command[0] == str(expected_python) for command, _ in calls)
    assert all(kwargs['check'] for _, kwargs in calls)


@pytest.mark.parametrize('module_name', ['prepare-runtime.py', 'runtime_common.py'])
@pytest.mark.parametrize('checksum', ['', 'abc', 'f' * 64])
def test_conda_identity_supports_native_explicit_locks_and_epoch_urls(tmp_path, module_name, checksum):
    path = Path(__file__).parents[1] / module_name
    spec = importlib.util.spec_from_file_location('identity_check', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    prefix = tmp_path / 'runtime'
    (prefix / 'conda-meta').mkdir(parents=True)
    (prefix / 'conda-meta/pkg.json').write_text(json.dumps({
        'url': 'https://repo/x264-1!1.conda', 'md5': 'abc', 'sha256': 'f' * 64}))
    lock = tmp_path / 'explicit.txt'
    lock.write_text('@EXPLICIT\nhttps://repo/x264-1%211.conda' + ('#' + checksum if checksum else '') + '\n')
    module.verify_conda(prefix, lock)
    lock.write_text('@EXPLICIT\nhttps://repo/x264-1%211.conda#bad\n')
    with pytest.raises(ValueError, match='不一致'):
        module.verify_conda(prefix, lock)


@pytest.mark.parametrize('bad_payload', [None, '[]', '{"schemaVersion": 1, "files": ["file"]}'])
def test_reuse_with_invalid_payload_keeps_runtime_ready_markers(tmp_path, bad_payload):
    module = helper()
    base = tmp_path / 'os'
    (base / 'runtime').mkdir(parents=True)
    marker = base / 'runtime/.unilabos-runtime-read-only'
    manifest = base / 'runtime-manifest.json'
    marker.touch()
    manifest.write_text('{}')
    if bad_payload is not None:
        (tmp_path / 'payload-manifest.json').write_text(bad_payload)
    with pytest.raises(module.InvalidPayload):
        module.prepare(base, platform_name='linux', reuse_runtime=True)
    assert marker.is_file()
    assert manifest.read_text() == '{}'
