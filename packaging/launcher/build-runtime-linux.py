#!/usr/bin/env python3
"""在 Linux x64 最终前缀安装 Constructor，并构建 OS 与浏览器运行包。"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def load(name: str, file: str):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def running_in(prefix: Path) -> bool:
    for process in Path('/proc').glob('[0-9]*/exe'):
        try:
            if process.resolve(strict=True).is_relative_to(prefix):
                return True
        except OSError:
            pass
    return False


def main() -> None:
    cli = argparse.ArgumentParser(description=__doc__)
    for key in ('output', 'runtime-installer', 'conda-lock', 'dependency-wheels', 'dependency-lock'):
        cli.add_argument('--' + key, type=Path, required=True)
    cli.add_argument('--frontend', type=Path, default=ROOT.parent / 'uni-lab-fe')
    cli.add_argument('--reuse-runtime', action='store_true')
    cli.add_argument('--skip-frontend-build', action='store_true')
    args = cli.parse_args()
    if sys.platform != 'linux' or platform.machine().lower() not in {'x86_64', 'amd64'}:
        cli.error('此入口要求 Linux x64，不能在其他系统交叉构建 Runtime')
    common = load('runtime_common', 'runtime_common.py')
    output = args.output.resolve()
    prefix = output / 'runtime'
    python = prefix / 'bin/python'
    for path in (args.runtime_installer, args.conda_lock, args.dependency_lock):
        if not path.is_file():
            cli.error(f'缺少构建输入：{path}')
    if not args.dependency_wheels.is_dir():
        cli.error('缺少 Linux 离线 wheel 目录')
    if running_in(prefix):
        cli.error('私有 Runtime 正在使用，请先停止后端')
    if args.reuse_runtime:
        if not python.is_file():
            cli.error('缺少已有私有 Runtime')
        common.verify_conda(prefix, args.conda_lock)
    elif prefix.exists():
        cli.error('Runtime 已存在，请使用 --reuse-runtime；不会自动删除环境')
    frontend = args.frontend.resolve()
    dist = frontend / 'apps/developer-web/dist'
    if args.skip_frontend_build:
        frontend_record = common.frontend_provenance(dist)
    output.mkdir(parents=True, exist_ok=True)
    marker = output / 'runtime-manifest.json'
    marker.unlink(missing_ok=True)
    (prefix / '.unilabos-runtime-read-only').unlink(missing_ok=True)
    environment = dict(os.environ, CONDA_PREFIX=str(prefix), AMENT_PREFIX_PATH=str(prefix), PYTHONDONTWRITEBYTECODE='1')
    def run(command: list[str], **kwargs) -> None:
        print('+', subprocess.list2cmdline(command), flush=True)
        subprocess.run(command, check=True, env=environment, **kwargs)
    if not args.reuse_runtime:
        run(['/bin/sh', str(args.runtime_installer.resolve()), '-b', '-p', str(prefix)])
        common.verify_conda(prefix, args.conda_lock)
    pip = [str(python), '-I', '-X', 'utf8', '-m', 'pip']
    wheelhouse = output / 'dependency-wheels'
    with tempfile.TemporaryDirectory(prefix='dependency-wheels-', dir=output) as selected:
        run(pip + ['download', '--no-index', '--only-binary=:all:', '--no-deps',
                   '--find-links', str(args.dependency_wheels.resolve()), '-r', str(args.dependency_lock.resolve()),
                   '--dest', selected])
        run(pip + ['install', '--no-index', '--only-binary=:all:', '--no-deps',
                   '--find-links', selected, '-r', str(args.dependency_lock.resolve())])
        if wheelhouse.exists():
            shutil.rmtree(wheelhouse)
        shutil.copytree(selected, wheelhouse)
    wheel = common.build_wheel(ROOT, output / 'wheels', pip, run)
    run(pip + ['install', '--no-index', '--no-deps', '--force-reinstall', str(wheel)])
    run(pip + ['check'])
    run([str(python), '-I', '-X', 'utf8', '-c', 'import rclpy; from unilabos.workspace_host.launcher import cli'])
    if not args.skip_frontend_build:
        pnpm = shutil.which('pnpm')
        if not pnpm:
            cli.error('缺少 pnpm')
        subprocess.run([pnpm, '--config.verify-deps-before-run=false', '--filter', '@unilab/developer-web', 'build'],
            check=True, cwd=frontend, env=dict(os.environ, VITE_UNILAB_API_URL='/'))
        frontend_record = {'commit': common.revision(frontend), 'sourceDirty': bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=frontend)),
                           'artifactDigest': common.frontend_digest(dist)}
        (dist / '.launcher-build.json').write_text(json.dumps(frontend_record, indent=2) + '\n', encoding='utf-8')
    destination = output / 'developer-web'
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(dist, destination)
    for source, name in ((args.conda_lock, 'ros-linux-64.explicit.txt'), (args.dependency_lock, 'runtime-requirements.lock.txt'), (ROOT / 'LICENSE', 'LICENSE-GPL-3.0.txt')):
        shutil.copy2(source, output / name)
    record = {'schemaVersion': 1, 'protocolVersion': 2, 'product': 'unilabos', 'platform': 'linux', 'architecture': 'x86_64',
        'entrypoint': {'kind': 'python-module', 'module': 'unilabos.workspace_host.launcher'}, 'runtimePrefix': str(prefix),
        'osCommit': common.revision(ROOT), 'sourceDirty': bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT)),
        'frontendCommit': frontend_record['commit'], 'frontendSourceDirty': frontend_record['sourceDirty'],
        'frontendArtifactDigest': frontend_record['artifactDigest'], 'wheelDigest': common.digest(wheel),
        'condaLockDigest': common.digest(args.conda_lock), 'dependencyLockDigest': common.digest(args.dependency_lock),
        'runtimeInstallerDigest': common.digest(args.runtime_installer),
        'dependencyWheelDigests': {item.name: common.digest(item) for item in sorted(wheelhouse.glob('*.whl'))}}
    for metadata in (prefix / 'lib').glob('python*/site-packages/unilabos-*.dist-info/direct_url.json'):
        metadata.unlink()
    (prefix / '.unilabos-runtime-read-only').write_text('launcher-runtime-v2\n', encoding='utf-8')
    record['installationSupport'] = common.stage_installation_support(output)
    record['installationSupport']['reuseRuntime'] = True
    marker.write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    print(f'Linux 运行包已生成：{output}', flush=True)


if __name__ == '__main__':
    main()
