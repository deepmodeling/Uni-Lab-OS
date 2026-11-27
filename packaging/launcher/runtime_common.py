"""各平台共用的来源校验与隔离 wheel 构建，不承担平台安装流程。"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from urllib.parse import unquote, urlsplit

_spec = importlib.util.spec_from_file_location('windows_paths', Path(__file__).with_name('windows_paths.py'))
windows_paths = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(windows_paths)


def revision(root: Path) -> str:
    return subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()


def frontend_digest(dist: Path) -> str:
    """摘要包含相对路径和内容，排除来源记录自身。"""
    digest = hashlib.sha256()
    for path in sorted(dist.rglob('*')):
        if path.is_file() and path != dist / '.launcher-build.json':
            digest.update(path.relative_to(dist).as_posix().encode() + b'\0')
            digest.update(path.read_bytes() + b'\0')
    return digest.hexdigest()


def frontend_provenance(dist: Path) -> dict:
    record = json.loads((dist / '.launcher-build.json').read_text())
    if (not isinstance(record, dict) or not isinstance(record.get('commit'), str)
            or type(record.get('sourceDirty')) is not bool):
        raise ValueError('Web 来源记录不完整，请重新构建前端')
    if record['artifactDigest'] != frontend_digest(dist):
        raise ValueError('Web 产物与来源记录不一致，请重新构建前端')
    return record


def verify_conda(prefix: Path, lock: Path) -> None:
    """复用前逐项核对 Constructor 安装后的包身份，拒绝其他环境。"""
    expected: dict[str, str] = {}
    for line in lock.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line and not line.startswith(('#', '@')):
            url, _, checksum = line.replace('%21', '!').partition('#')
            expected[url] = checksum
    actual: dict[str, dict] = {}
    for path in (prefix / 'conda-meta').glob('*.json'):
        record = json.loads(path.read_text(encoding='utf-8'))
        actual[record['url'].replace('%21', '!')] = record
    if (not expected or expected.keys() != actual.keys()
            or any(checksum and checksum != actual[url].get('sha256' if len(checksum) == 64 else 'md5')
                   for url, checksum in expected.items())):
        raise ValueError('目标 Runtime 与所选 Conda 精确锁不一致')


def verify_windows_runtime_lock(lock: Path) -> None:
    """Windows 启动器仅接受环境包和 OpenBLAS，业务代码由 pip wheel 交付。"""
    packages: dict[str, tuple[str, str]] = {}
    for raw in lock.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith(('#', '@')):
            continue
        filename = unquote(urlsplit(line).path).rsplit('/', 1)[-1]
        if filename.endswith('.tar.bz2'):
            stem = filename[:-8]
        elif filename.endswith('.conda'):
            stem = filename[:-6]
        else:
            raise ValueError(f'Conda 精确锁中的包格式无效：{filename}')
        parts = stem.rsplit('-', 2)
        if len(parts) != 3 or parts[0] in packages:
            raise ValueError(f'Conda 精确锁中的包身份无效：{filename}')
        packages[parts[0]] = (parts[1], parts[2])
    if 'unilabos-env' not in packages or {'unilabos', 'unilabos-full'} & packages.keys():
        raise ValueError('Windows 启动器要求 unilabos-env；UniLabOS 业务代码必须通过 pip wheel 安装')
    if (any(name == 'mkl' or name.startswith(('mkl-', 'mkl_')) for name in packages)
            or 'libopenblas' not in packages or 'openblas' not in packages.get('libblas', ('', ''))[1]):
        raise ValueError('Windows 启动器要求 OpenBLAS 精确锁，不能包含 MKL')


def build_wheel(root: Path, destination: Path, pip: list[str], run: Callable[..., None], *, temporary_root: Path | None = None) -> Path:
    """只发布本次构建的唯一 wheel；构建失败不清理上次产物。"""
    with tempfile.TemporaryDirectory(prefix='lab-os-wheel-', dir=temporary_root) as temporary:
        stage = Path(temporary)
        source = stage / 'source'
        source.mkdir()
        for name in ('setup.py', 'setup.cfg', 'MANIFEST.in', 'LICENSE', 'NOTICE', 'README.md', 'README_zh.md'):
            shutil.copy2(root / name, source / name)
        shutil.copytree(root / 'unilabos', source / 'unilabos',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.ua'))
        shutil.copytree(root / 'skills', source / 'skills')
        wheels = stage / 'wheels'
        wheels.mkdir()
        run(pip + ['wheel', '--no-deps', '--no-build-isolation', '--wheel-dir', str(wheels), str(source)])
        built = list(wheels.glob('unilabos-*.whl'))
        if len(built) != 1:
            raise ValueError('本次构建必须生成唯一的 OS wheel')
        destination.mkdir(parents=True, exist_ok=True)
        wheel = destination / built[0].name
        shutil.copy2(built[0], wheel)
        for old in destination.glob('unilabos-*.whl'):
            if old != wheel:
                old.unlink()
        return wheel


def freeze_dependency_wheels(
    output: Path,
    lock: Path,
    pip: list[str],
    run: Callable[..., None],
    *,
    cache: Path | None = None,
    macos_platform: str | None = None,
) -> dict[str, str]:
    """构建离线依赖；有目标 macOS 版本时，先验证整份锁能在目标平台安装。"""
    destination = output / 'dependency-wheels'
    with tempfile.TemporaryDirectory(prefix='dependency-wheels-', dir=output) as temporary:
        built = Path(temporary) / 'built'
        source = ['--no-index', '--find-links', str(cache)] if cache is not None else []
        run(pip + ['wheel', '--no-deps', '--no-build-isolation', '--wheel-dir', str(built), *source, '-r', str(lock)])
        selected = built
        if macos_platform is not None:
            selected = Path(temporary) / 'selected'
            run(pip + ['download', '--no-index', '--only-binary=:all:', '--no-deps',
                       '--platform', macos_platform, '--find-links', str(built), '--dest', str(selected), '-r', str(lock)])
        if not selected.is_dir():
            raise ValueError('未生成离线依赖 wheel')
        hashes = {wheel.name: digest(wheel) for wheel in sorted(selected.glob('*.whl'))}
        if destination.exists():
            shutil.rmtree(destination)
        selected.rename(destination)
        return hashes


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(block)
    return result.hexdigest()


def stage_installation_support(output: Path) -> dict:
    """冻结与本次 OS 构建配套的安装脚本和封装校验工具，并登记内容摘要。"""
    names = ('prepare-runtime.py', 'runtime_common.py', 'windows_paths.py')
    destination = output / 'installation-support'
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir()
    for name in names:
        shutil.copy2(Path(__file__).with_name(name), destination / name)
    return {'schemaVersion': 1, 'files': {name: digest(destination / name) for name in names}}
