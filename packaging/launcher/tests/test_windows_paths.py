"""路径预算独立于宿主机注册表，覆盖缓存、wheel 安装布局和 UTF-16。"""
import hashlib
import importlib.util
import json
from pathlib import Path
import zipfile
import sys

import pytest


@pytest.fixture
def paths():
    spec = importlib.util.spec_from_file_location('windows_paths', Path(__file__).parents[1] / 'windows_paths.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_delivery_boundary_259_allowed_260_rejected(paths):
    assert paths.check_relative('x' * 232, 'fixture') == 232
    with pytest.raises(ValueError, match='交付路径过长'):
        paths.check_relative('x' * 233, 'fixture')


def test_delivery_budget_counts_utf16_units(paths):
    assert paths.check_relative('x' * 230 + '\U0001f680', 'unicode') == 232
    with pytest.raises(ValueError, match='交付路径过长'):
        paths.check_relative('x' * 231 + '\U0001f680', 'unicode')


def test_directory_budget_reserves_space_for_83_filename(paths):
    paths.check_relative('x' * 220, 'directory', is_directory=True)
    with pytest.raises(ValueError, match='交付路径过长'):
        paths.check_relative('x' * 221, 'directory', is_directory=True)
    with pytest.raises(ValueError, match='交付路径过长'):
        paths.check_relative('x' * 221 + '/file', 'file in deep directory')


@pytest.fixture
def constructor(tmp_path):
    exe = tmp_path / 'constructor.exe'
    exe.write_bytes(b'constructor')
    lock = tmp_path / 'lock.txt'
    lock.write_text('@EXPLICIT\nhttps://repo/python.conda#abc\n')
    data = {'_platform': 'win-64', '_installer_hashes': {'sha256': hashlib.sha256(exe.read_bytes()).hexdigest()},
            '_urls': [['https://repo/python.conda', 'abc']], '_max_relative_path_length': 209}
    info = tmp_path / 'info.json'
    info.write_text(json.dumps(data))
    return info, exe, lock, data


def test_constructor_cache_maximum_is_included(paths, constructor):
    info, exe, lock, _ = constructor
    policy = paths.constructor_policy(info, exe, lock)
    assert policy['constructorRelativeMax'] == 209
    assert policy['maxAppDirectoryChars'] == 26


@pytest.mark.parametrize('change', ['hash', 'lock', 'maximum', 'missing', 'platform'])
def test_untrusted_or_overlong_constructor_metadata_is_rejected(paths, constructor, change):
    info, exe, lock, data = constructor
    if change == 'hash':
        exe.write_bytes(b'other constructor')
    elif change == 'lock':
        lock.write_text('@EXPLICIT\nhttps://repo/other.conda#abc\n')
    elif change == 'maximum':
        data['_max_relative_path_length'] = 210
    elif change == 'missing':
        data.pop('_max_relative_path_length')
    else:
        data['_platform'] = 'linux-64'
    info.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        paths.constructor_policy(info, exe, lock)


def wheel(tmp_path, files):
    file = tmp_path / 'example-1.0-py3-none-any.whl'
    with zipfile.ZipFile(file, 'w') as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return file


def test_wheel_data_and_generated_bytecode_are_checked(paths, tmp_path):
    file = wheel(tmp_path, {'example.data/purelib/example/__init__.py': '',
        'example.data/scripts/tool.py': '', 'example-1.0.dist-info/entry_points.txt': '[console_scripts]\nhello = example:main\n'})
    report = paths.audit_wheels([file])
    assert report['entries'] >= 6
    assert '__pycache__' in report['longestRelativePath']


@pytest.mark.parametrize('file', ['../escape.py', '/absolute.py', 'C:/absolute.py', 'x' * 185 + '.py'])
def test_unsafe_or_overlong_wheel_paths_fail_before_install(paths, tmp_path, file):
    archive = wheel(tmp_path, {file: ''})
    with pytest.raises(ValueError):
        paths.audit_wheels([archive])


def test_long_generated_entrypoint_is_rejected(paths, tmp_path):
    archive = wheel(tmp_path, {'example-1.0.dist-info/entry_points.txt': '[console_scripts]\n' + 'x' * 210 + ' = example:main\n'})
    with pytest.raises(ValueError, match='交付路径过长'):
        paths.audit_wheels([archive])


def test_long_development_path_does_not_change_delivery_budget(paths, tmp_path):
    root = tmp_path
    while paths.path_chars(root) <= 270:
        root /= 'development-' * 4
    root.mkdir(parents=True)
    (root / 'module.py').write_text('')
    assert paths.audit_tree(root)['entries'] == 1
    paths.audit_wheels([wheel(root, {'example/__init__.py': ''})])
    (root / ('x' * 233)).write_text('')
    with pytest.raises(ValueError, match='交付路径过长'):
        paths.audit_tree(root)


def test_invalid_constructor_is_rejected_before_existing_runtime_changes(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location('windows_builder', Path(__file__).parents[1] / 'build-runtime-windows.py')
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    monkeypatch.setattr(builder.sys, 'platform', 'win32')
    monkeypatch.setattr(builder.platform, 'machine', lambda: 'AMD64')
    app = tmp_path / 'deep-existing-development-output'
    base = app / 'os'
    (base / 'runtime').mkdir(parents=True)
    manifest = base / 'runtime-manifest.json'
    marker = base / 'runtime/.unilabos-runtime-read-only'
    manifest.write_text('previous manifest')
    marker.write_text('previous marker')
    installer = tmp_path / 'constructor.exe'
    installer.touch()
    info = tmp_path / 'info.json'
    info.write_text('{}')
    lock = tmp_path / 'lock.txt'
    lock.write_text('@EXPLICIT\nhttps://repo/unilabos-env-0.11.4-test.conda#abc\n'
        'https://repo/libblas-3.11.0-test_openblas.conda#abc\nhttps://repo/libopenblas-0.3.34-test.conda#abc\n')
    wheels = tmp_path / 'wheels'
    wheels.mkdir()
    monkeypatch.setattr(sys, 'argv', ['build', '--output', str(base), '--runtime-installer', str(installer),
        '--conda-lock', str(lock), '--constructor-info', str(info), '--dependency-lock', str(lock),
        '--dependency-wheels', str(wheels), '--reuse-runtime'])
    with pytest.raises(SystemExit):
        builder.main()
    assert 'Constructor' in capsys.readouterr().err
    assert manifest.read_text() == 'previous manifest'
    assert marker.read_text() == 'previous marker'
