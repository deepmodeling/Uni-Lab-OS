#!/usr/bin/env python3
"""用显式指定的离线 Constructor 和 wheel 缓存构建 Windows 启动器运行包。"""
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
spec = importlib.util.spec_from_file_location('runtime_builder', HERE / 'runtime_common.py')
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)


def run(command: list[str], **kwargs) -> None:
    print('+', subprocess.list2cmdline(command), flush=True)
    subprocess.run(command, check=True, **kwargs)


def runtime_environment(prefix: Path) -> dict[str, str]:
    """隔离私有 Runtime 的 DLL 搜索路径，避免继承开发环境的同名 DLL。"""
    system = Path(os.environ.get('SystemRoot', r'C:\Windows'))
    return dict(os.environ,
                PATH=os.pathsep.join(map(str, [prefix, prefix / 'Library/bin', prefix / 'Scripts', system / 'System32'])),
                CONDA_PREFIX=str(prefix), AMENT_PREFIX_PATH=str(prefix / 'Library'),
                COLCON_PREFIX_PATH=str(prefix), CONDA_DLL_SEARCH_MODIFICATION_ENABLE='1',
                PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--runtime-installer', type=Path, required=True)
    parser.add_argument('--conda-lock', type=Path, required=True)
    parser.add_argument('--constructor-info', type=Path, required=True)
    parser.add_argument('--dependency-wheels', type=Path, required=True)
    parser.add_argument('--dependency-lock', type=Path, required=True)
    parser.add_argument('--frontend', type=Path, default=ROOT.parent / 'uni-lab-fe')
    parser.add_argument('--reuse-runtime', action='store_true')
    parser.add_argument('--skip-frontend-build', action='store_true')
    args = parser.parse_args()
    if sys.platform != 'win32' or platform.machine().lower() not in {'amd64', 'x86_64'}:
        parser.error('此入口要求 Windows x64')
    output = args.output.resolve()
    prefix = output / 'runtime'
    python = prefix / 'python.exe'
    environment = runtime_environment(prefix)
    for file in [args.runtime_installer, args.conda_lock, args.dependency_lock]:
        if not file.is_file():
            parser.error(f'缺少构建输入：{file}')
    if not args.dependency_wheels.is_dir():
        parser.error('缺少离线 wheel 目录')
    try:
        common.verify_windows_runtime_lock(args.conda_lock)
        path_policy = common.windows_paths.constructor_policy(args.constructor_info, args.runtime_installer, args.conda_lock)
    except ValueError as error:
        parser.error(str(error))
    if args.reuse_runtime:
        if not python.is_file():
            parser.error('缺少私有 Runtime')
        common.verify_conda(prefix, args.conda_lock)
        # 只使用安装在该前缀的 psutil，不依赖系统 Python 包。
        probe = ('import psutil,sys; from pathlib import Path; p=Path(sys.prefix).resolve(); '
                 'sys.exit(any(Path(x.info["exe"]).is_relative_to(p) for x in '
                 'psutil.process_iter(["exe"]) if x.pid != __import__("os").getpid() and x.info["exe"]))')
        if subprocess.run([str(python), '-I', '-c', probe], env=environment).returncode:
            parser.error('私有 Runtime 正在使用或无法检查进程，请先停止后端')
    elif prefix.exists():
        parser.error('Runtime 已存在，请使用 --reuse-runtime；不会自动删除环境')
    frontend = args.frontend.resolve()
    dist = frontend / 'apps/developer-web/dist'
    if args.skip_frontend_build:
        frontend_record = common.frontend_provenance(dist)
    output.mkdir(parents=True, exist_ok=True)
    marker = output / 'runtime-manifest.json'
    marker.unlink(missing_ok=True)
    (prefix / '.unilabos-runtime-read-only').unlink(missing_ok=True)
    if not args.reuse_runtime:
        run([str(args.runtime_installer.resolve()), '/InstallationType=JustMe', '/AddToPath=0',
             '/RegisterPython=0', '/NoRegistry=1', '/NoShortcuts=1', '/S', f'/D={prefix}'], creationflags=subprocess.CREATE_NO_WINDOW)
        common.verify_conda(prefix, args.conda_lock)
    pip = [str(python), '-I', '-m', 'pip']
    wheelhouse = output / 'dependency-wheels'
    with tempfile.TemporaryDirectory(prefix='deps-', dir=output.parent) as selected:
        run(pip + ['download', '--no-index', '--only-binary=:all:', '--no-deps',
                   '--find-links', str(args.dependency_wheels.resolve()), '-r', str(args.dependency_lock.resolve()),
                   '--dest', selected], env=environment)
        path_policy['dependencyWheels'] = common.windows_paths.audit_wheels(sorted(Path(selected).glob('*.whl')))
        run(pip + ['install', '--no-index', '--only-binary=:all:', '--no-deps', '--find-links', selected,
                   '-r', str(args.dependency_lock.resolve())], env=environment)
        if wheelhouse.exists():
            shutil.rmtree(wheelhouse)
        shutil.copytree(selected, wheelhouse)
    wheel = common.build_wheel(ROOT, output / 'wheels', pip, lambda command: run(command, env=environment), temporary_root=output.parent)
    path_policy['businessWheel'] = common.windows_paths.audit_wheels([wheel])
    run(pip + ['install', '--no-index', '--no-deps', '--force-reinstall', str(wheel)], env=environment)
    run(pip + ['check'], env=environment)
    run([str(python), '-I', '-c', 'import rclpy; from unilabos.workspace_host.launcher import cli'], env=environment)
    if not args.skip_frontend_build:
        pnpm = shutil.which('pnpm.cmd')
        if not pnpm:
            parser.error('缺少 pnpm.cmd')
        # 新版 pnpm 默认在 run 前自动重装依赖；构建只使用开发机已准备的依赖。
        run([os.environ['COMSPEC'], '/d', '/s', '/c', subprocess.list2cmdline([
            pnpm, '--config.verify-deps-before-run=false', '--filter', '@unilab/developer-web', 'build'])],
            cwd=frontend, env=dict(os.environ, VITE_UNILAB_API_URL='/'))
        frontend_record = {'commit': common.revision(frontend), 'sourceDirty': bool(subprocess.check_output(
            ['git', 'status', '--porcelain'], cwd=frontend)), 'artifactDigest': common.frontend_digest(dist)}
        (dist / '.launcher-build.json').write_text(json.dumps(frontend_record, indent=2) + '\n', encoding='utf-8')
    destination = output / 'developer-web'
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(dist, destination)
    shutil.copy2(args.conda_lock, output / 'ros-win-64.explicit.txt')
    shutil.copy2(args.constructor_info, output / 'constructor-info.json')
    shutil.copy2(args.dependency_lock, output / 'runtime-requirements.lock.txt')
    shutil.copy2(ROOT / 'LICENSE', output / 'LICENSE-GPL-3.0.txt')
    path_policy['runtimeFiles'] = common.windows_paths.audit_tree(prefix, 'os/runtime/')
    path_policy['osFiles'] = common.windows_paths.audit_tree(output, 'os/')
    manifest = {'schemaVersion': 1, 'protocolVersion': 2, 'product': 'unilabos',
                'windowsPathPolicy': path_policy, 'constructorInfoDigest': common.digest(args.constructor_info),
                'runtimeProfile': 'unilabos-env-openblas-pip',
                'entrypoint': {'kind': 'python-module', 'module': 'unilabos.workspace_host.launcher'},
                'platform': 'win32', 'architecture': 'amd64', 'runtimePrefix': str(prefix),
                'osCommit': common.revision(ROOT), 'sourceDirty': bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT)),
                'frontendCommit': frontend_record['commit'], 'frontendSourceDirty': frontend_record['sourceDirty'],
                'frontendArtifactDigest': frontend_record['artifactDigest'],
                'condaLockDigest': common.digest(args.conda_lock), 'dependencyLockDigest': common.digest(args.dependency_lock),
                'runtimeInstallerDigest': common.digest(args.runtime_installer), 'wheelDigest': common.digest(wheel),
                'dependencyWheelDigests': {item.name: common.digest(item) for item in sorted(wheelhouse.glob('*.whl'))}}
    for metadata in (prefix / 'Lib/site-packages').glob('unilabos-*.dist-info/direct_url.json'):
        metadata.unlink()
    (prefix / '.unilabos-runtime-read-only').write_text('launcher-runtime-v2\n', encoding='utf-8')
    manifest['installationSupport'] = common.stage_installation_support(output)
    marker.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'Windows 运行包已生成：{output}', flush=True)


if __name__ == '__main__':
    main()
