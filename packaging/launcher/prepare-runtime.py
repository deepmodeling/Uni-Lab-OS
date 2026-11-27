"""在各平台最终安装前缀离线准备 OS；不承担后端运行生命周期。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile


class InvalidPayload(ValueError):
    """离线载荷校验失败，重新创建 Runtime 无法修复。"""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def verify_payload(app: Path, manifest: Path | None = None) -> dict:
    record = json.loads((manifest or app / 'payload-manifest.json').read_text(encoding='utf-8'))
    files = record.get('files') if isinstance(record, dict) else None
    if not isinstance(files, dict) or not files or record.get('schemaVersion') != 1:
        raise ValueError('安装载荷清单无效')
    for name, expected in files.items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or '..' in relative.parts or '\\' in name or ':' in name:
            raise ValueError(f'安装载荷路径无效：{name}')
        path = (app / relative).resolve()
        if not path.is_relative_to(app.resolve()) or sha256(path) != expected:
            raise ValueError(f'安装载荷摘要不一致：{name}')
    return record


def verify_conda(prefix: Path, lock: Path) -> None:
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
        raise ValueError('Constructor Runtime 与交付锁不一致')


def prepare(base: Path, platform_name: str = 'win32', *, reuse_runtime: bool = False) -> None:
    base = base.resolve()
    prefix = base / 'runtime'
    if platform_name not in {'win32', 'linux', 'darwin'}:
        raise ValueError('安装准备仅支持 Windows、Linux 或 macOS')
    windows = platform_name == 'win32'
    python = prefix / ('python.exe' if windows else 'bin/python')
    conda_lock = base / ('ros-win-64.explicit.txt' if windows else ('ros-osx-arm64.explicit.txt' if platform_name == 'darwin' else 'ros-linux-64.explicit.txt'))
    marker = prefix / '.unilabos-runtime-read-only'
    manifest_path = base / 'runtime-manifest.json'
    if not reuse_runtime:
        marker.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
    payload_root = base.parents[2] if platform_name == 'darwin' else base.parent
    payload_manifest = base.parent / 'payload-manifest.json'
    try:
        verify_payload(payload_root, payload_manifest)
        template = json.loads((base / 'runtime-manifest.template.json').read_text(encoding='utf-8'))
        wheels = list((base / 'wheels').glob('unilabos-*.whl'))
        if len(wheels) != 1 or sha256(wheels[0]) != template['wheelDigest']:
            raise ValueError('安装包必须包含唯一且摘要正确的 OS wheel')
        if (sha256(base / 'runtime-requirements.lock.txt') != template['dependencyLockDigest']
                or sha256(conda_lock) != template['condaLockDigest']):
            raise ValueError('安装包依赖锁摘要不一致')
    except (OSError, ValueError, KeyError, TypeError) as error:
        if reuse_runtime:
            raise InvalidPayload(str(error)) from error
        raise
    if reuse_runtime:
        marker.unlink(missing_ok=True)
        manifest_path.unlink(missing_ok=True)
    verify_conda(prefix, conda_lock)
    system = Path(os.environ.get('SystemRoot', r'C:\Windows'))
    with tempfile.TemporaryDirectory(prefix='i-', dir=base if windows else None) as home:
        environment = {key: os.environ[key] for key in ('SystemRoot', 'WINDIR', 'APPDATA', 'LOCALAPPDATA') if key in os.environ}
        environment.update(PATH=os.pathsep.join(map(str, [prefix, prefix / 'Library/bin', prefix / 'Scripts', system / 'System32'])),
            HOME=home, USERPROFILE=home, TEMP=home, TMP=home, CONDA_PREFIX=str(prefix),
            AMENT_PREFIX_PATH=str(prefix / 'Library'), CONDA_DLL_SEARCH_MODIFICATION_ENABLE='1',
            PYTHONUTF8='1', PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1')
        if platform_name == 'darwin':
            environment.update(TMPDIR=home, DYLD_FALLBACK_LIBRARY_PATH=str(prefix / 'lib'))
        if not windows:
            environment.update(PATH=f'{prefix / "bin"}:/usr/bin:/bin', AMENT_PREFIX_PATH=str(prefix))
        with (base.parent / 'runtime-install.log').open('a', encoding='utf-8') as log:
            def run(arguments: list[str]) -> None:
                subprocess.run([str(python), '-I', '-X', 'utf8', '-B', *arguments],
                    cwd=home, env=environment, stdout=log, stderr=subprocess.STDOUT, check=True)
            if not reuse_runtime:
                print('UNILABOS_STAGE: 2', flush=True)
                run(['-m', 'pip', 'install', '--no-index', '--only-binary=:all:', '--no-deps', '--no-warn-script-location',
                     '--find-links', str(base / 'dependency-wheels'), '-r', str(base / 'runtime-requirements.lock.txt')])
                print('UNILABOS_STAGE: 3', flush=True)
                run(['-m', 'pip', 'install', '--no-index', '--no-deps', '--no-warn-script-location', '--force-reinstall', str(wheels[0])])
            print('UNILABOS_STAGE: 4', flush=True)
            run(['-m', 'pip', 'check'])
            run(['-c', 'import rclpy; from unilabos.workspace_host.launcher import cli; print("OS public launcher ready")'])
    metadata_paths = ((prefix / 'Lib/site-packages').glob('unilabos-*.dist-info/direct_url.json') if windows
                      else (prefix / 'lib').glob('python*/site-packages/unilabos-*.dist-info/direct_url.json'))
    for metadata in metadata_paths:
        metadata.unlink()
    template['runtimePrefix'] = str(prefix)
    template['installation'] = {'payloadManifestDigest': sha256(payload_manifest)}
    temporary = manifest_path.with_suffix('.tmp')
    temporary.write_text(json.dumps(template, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(manifest_path)
    marker.write_text('launcher-runtime-v2\n', encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--reuse-runtime', action='store_true')
    args = parser.parse_args()
    try:
        prepare(Path(__file__).resolve().parent, platform_name=sys.platform, reuse_runtime=args.reuse_runtime)
    except Exception as error:
        print(f'Runtime 安装准备失败：{error}', file=sys.stderr, flush=True)
        sys.exit(2 if isinstance(error, InvalidPayload) else 1)
