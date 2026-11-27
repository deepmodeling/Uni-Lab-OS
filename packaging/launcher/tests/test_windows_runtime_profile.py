"""精简 Windows Runtime 不重复携带 Conda 业务包或 MKL。"""
import importlib.util
from pathlib import Path
import sys

import jinja2
import pytest
import yaml


HERE = Path(__file__).parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def lock(tmp_path):
    file = tmp_path / 'lock.txt'
    file.write_text('@EXPLICIT\nhttps://repo/python-3.11.14-test.conda#abc\n'
        'file:///D:/cache/unilabos-env-0.11.4-test.conda#abc\n'
        'https://repo/libblas-3.11.0-test_openblas.conda#abc\n'
        'https://repo/libopenblas-0.3.34-test.tar.bz2#abc\n')
    return file


def test_accepts_env_and_openblas_with_conda_and_tar_archives(lock):
    load('runtime_common', HERE / 'runtime_common.py').verify_windows_runtime_lock(lock)


@pytest.mark.parametrize('change', ['full', 'full-extra', 'missing-env', 'mkl', 'missing-openblas', 'wrong-blas', 'duplicate', 'invalid'])
def test_rejects_broad_or_inconsistent_runtime(lock, change):
    content = lock.read_text()
    if change in {'full', 'full-extra'}:
        content += f'https://repo/{"unilabos" if change == "full" else "unilabos-full"}-0.11.4-test.conda#abc\n'
    elif change == 'missing-env':
        content = '\n'.join(line for line in content.splitlines() if 'unilabos-env-' not in line)
    elif change == 'mkl':
        content += 'https://repo/mkl-2026.1.0-test.conda#abc\n'
    elif change == 'missing-openblas':
        content = '\n'.join(line for line in content.splitlines() if '/libopenblas-' not in line)
    elif change == 'wrong-blas':
        content = content.replace('test_openblas', 'test_mkl')
    elif change == 'duplicate':
        content += 'https://repo/libblas-3.11.0-other_openblas.conda#abc\n'
    else:
        content += 'https://repo/not-an-archive#abc\n'
    lock.write_text(content)
    with pytest.raises(ValueError):
        load('runtime_common', HERE / 'runtime_common.py').verify_windows_runtime_lock(lock)


def test_rejects_full_constructor_before_invalidating_existing_output(lock, tmp_path, monkeypatch):
    module = load('windows_builder', HERE / 'build-runtime-windows.py')
    monkeypatch.setattr(module.sys, 'platform', 'win32')
    monkeypatch.setattr(module.platform, 'machine', lambda: 'AMD64')
    lock.write_text(lock.read_text() + 'https://repo/unilabos-0.11.4-test.conda#abc\n')
    output = tmp_path / 'output'
    output.mkdir()
    manifest = output / 'runtime-manifest.json'
    manifest.write_text('previous')
    installer = tmp_path / 'constructor.exe'
    installer.touch()
    wheels = tmp_path / 'wheels'
    wheels.mkdir()
    monkeypatch.setattr(sys, 'argv', ['build', '--output', str(output), '--runtime-installer', str(installer),
        '--conda-lock', str(lock), '--dependency-lock', str(lock), '--dependency-wheels', str(wheels), '--constructor-info', str(lock)])
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **k: pytest.fail('拒绝输入前不能启动安装器'))
    with pytest.raises(SystemExit):
        module.main()
    assert manifest.read_text() == 'previous'


@pytest.mark.parametrize('private', [False, True])
def test_constructor_preserves_general_profile_and_restores_launcher_profile(private):
    template = (HERE.parents[1] / '.conda/constructor/construct.yaml').read_text(encoding='utf-8')
    env = {'UNILABOS_DESKTOP_RUNTIME': '1'} if private else {}
    rendered = yaml.safe_load(jinja2.Template(template).render(environ=env))
    specs = rendered['specs']
    if private:
        assert 'unilabos-env ==0.11.4' in specs
        assert 'pip' in specs and 'numpy 1.26.4' in specs
        assert 'libblas * *openblas' in specs
        assert not any(spec.startswith('conda ') for spec in specs)
        assert rendered['initialize_conda'] is False
    else:
        assert 'unilabos ==0.11.4' in specs
        assert 'conda >=25.5' in specs
        assert rendered['initialize_conda'] == 'condabin'
