#!/usr/bin/env python3
"""构建可脱离源码运行的 OS 交付物；macOS 在项目开发输出目录构建。"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
spec = importlib.util.spec_from_file_location('runtime_common', HERE / 'runtime_common.py')
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)
revision = common.revision
frontend_digest = common.frontend_digest
frontend_provenance = common.frontend_provenance


def run(command: list[str], **kwargs: object) -> None:
    print('+', ' '.join(command), flush=True)
    subprocess.run(command, check=True, **kwargs)


def conda_packages(explicit: str) -> set[str]:
    return {line.strip().replace('%21', '!') for line in explicit.splitlines()
            if line.strip() and not line.startswith(('#', '@'))}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--dependency-lock', type=Path)
    parser.add_argument('--dependency-wheels', type=Path, help='从本地依赖缓存离线构建 wheel')
    parser.add_argument('--minimum-macos', default=os.environ.get('MACOSX_DEPLOYMENT_TARGET'),
                        help='交付目标的 macOS 最低版本，默认读取 MACOSX_DEPLOYMENT_TARGET')
    parser.add_argument('--frontend', type=Path, default=ROOT.parent / 'uni-lab-fe')
    parser.add_argument('--seed-runtime', type=Path, help='使用已有环境导出的 Conda 精确包清单；仍由 Conda 在目标前缀安装')
    parser.add_argument('--reuse-runtime', action='store_true')
    parser.add_argument('--skip-frontend-build', action='store_true')
    args = parser.parse_args()
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        parser.error('首期运行包构建仅验收 macOS arm64')
    dependency_lock = (args.dependency_lock or HERE / 'runtime-requirements.lock.txt').expanduser().resolve()
    if not dependency_lock.is_file():
        parser.error(f'缺少依赖锁：{dependency_lock}')
    dependency_wheels = args.dependency_wheels.resolve() if args.dependency_wheels else None
    if dependency_wheels is not None and not dependency_wheels.is_dir():
        parser.error(f'缺少离线依赖缓存：{dependency_wheels}')
    minimum = re.fullmatch(r'(\d+)\.(\d+)', args.minimum_macos or '')
    if not minimum:
        parser.error('请通过 --minimum-macos 指定 macOS 最低系统版本，格式为主版本.次版本')
    macos_platform = f'macosx_{minimum[1]}_{minimum[2]}_arm64'
    output = args.output.expanduser().resolve()
    prefix = output / 'runtime'
    if subprocess.run(['/usr/bin/pgrep', '-f', '^' + re.escape(str(prefix)) + '/'], capture_output=True).returncode == 0:
        parser.error('私有 Runtime 正在运行，请先停止后端')
    python = prefix / 'bin/python'
    frontend = args.frontend.resolve()
    dist = frontend / 'apps/developer-web/dist'
    if args.reuse_runtime:
        if not python.is_file() or not (prefix / 'conda-meta').is_dir():
            parser.error('缺少私有 Conda Runtime')
    elif prefix.exists():
        parser.error('Runtime 已存在，请使用 --reuse-runtime；不会自动删除现有环境')
    explicit = (subprocess.check_output(['conda', 'list', '--prefix', str(args.seed_runtime.resolve()), '--explicit'], text=True)
                if args.seed_runtime else (HERE / 'ros-osx-arm64.explicit.txt').read_text())
    if args.reuse_runtime:
        actual = subprocess.check_output(['conda', 'list', '--prefix', str(prefix), '--explicit'], text=True)
        if conda_packages(actual) != conda_packages(explicit):
            parser.error('目标 Runtime 的 Conda 包与所选锁不一致，请使用匹配的 --seed-runtime 或重新构建环境')
    if args.skip_frontend_build:
        try:
            if not (dist / 'index.html').is_file():
                raise ValueError('缺少 developer-web 构建产物')
            frontend_record = frontend_provenance(dist)
        except (OSError, ValueError, KeyError) as error:
            parser.error(f'无法复用 Web 产物，请先去掉 --skip-frontend-build 重建：{error}')
    # 所有只读前置校验通过后，才撤销旧运行包的有效标记。
    output.mkdir(parents=True, exist_ok=True)
    marker = output / 'runtime-manifest.json'
    marker.unlink(missing_ok=True)
    (prefix / '.unilabos-runtime-read-only').unlink(missing_ok=True)
    if not args.reuse_runtime:
        # Conda 26 的 x264 归档要求 URL 文件名保留 epoch 中的感叹号。
        normalized = output / 'ros-osx-arm64.explicit.txt'
        normalized.write_text(explicit.replace('%21', '!'), encoding='utf-8', newline='\n')
        run(['conda', 'create', '--yes', '--json', '--copy', '--prefix', str(prefix), '--file', str(normalized)])
    # 从本次目标环境导出，不读取其他 output 共用的构建缓存。
    actual = subprocess.check_output(['conda', 'list', '--prefix', str(prefix), '--explicit'], text=True)
    conda_lock = output / 'ros-osx-arm64.explicit.txt'
    conda_lock.write_text(actual.replace('%21', '!'), encoding='utf-8', newline='\n')
    pip = [str(python), '-I', '-X', 'utf8', '-m', 'pip']
    build_env = dict(os.environ, MACOSX_DEPLOYMENT_TARGET=args.minimum_macos)
    def run_build(command: list[str], **kwargs: object) -> None:
        run(command, env=build_env, **kwargs)
    dependency_digests = common.freeze_dependency_wheels(
        output, dependency_lock, pip, run_build, cache=dependency_wheels, macos_platform=macos_platform)
    run(pip + ['install', '--no-index', '--only-binary=:all:', '--no-deps', '--force-reinstall',
               '--find-links', str(output / 'dependency-wheels'), '-r', str(dependency_lock)])
    wheel = common.build_wheel(ROOT, output / 'wheels', pip, run_build)
    run([str(python), '-m', 'pip', 'install', '--no-deps', '--force-reinstall', str(wheel)])
    run([str(python), '-m', 'pip', 'check'])
    run([str(python), '-I', '-c', 'import rclpy, unilabos; from unilabos.workspace_host.launcher import cli; print(unilabos.__file__)'])
    if not args.skip_frontend_build:
        run(['pnpm', 'install', '--frozen-lockfile'], cwd=frontend)
        run(['pnpm', '--filter', '@unilab/developer-web', 'build'], cwd=frontend,
            env={**os.environ, 'VITE_UNILAB_API_URL': '/'})
        frontend_record = {'commit': revision(frontend),
                           'sourceDirty': bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=frontend)),
                           'artifactDigest': frontend_digest(dist)}
        (dist / '.launcher-build.json').write_text(json.dumps(frontend_record, indent=2) + '\n')
    if not (dist / 'index.html').is_file():
        parser.error('缺少 developer-web 构建产物')
    destination = output / 'developer-web'
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(dist, destination)
    shutil.copy2(dependency_lock, output / 'runtime-requirements.lock.txt')
    # 运行实现只随 OS wheel 安装，不再交付第二份松散 Python 脚本。
    for obsolete in ['bridge.py', 'adapter_host.py', 'adapter_worker.py', 'web_proxy.py']:
        (output / obsolete).unlink(missing_ok=True)
    shutil.copy2(ROOT / 'LICENSE', output / 'LICENSE-GPL-3.0.txt')
    # 历史版本曾把 wheel 再复制到根目录；交付统一使用 wheels/。
    for obsolete in output.glob('unilabos-*.whl'):
        obsolete.unlink()
    (prefix / '.unilabos-runtime-read-only').write_text('launcher-runtime-v2\n')
    for metadata in (prefix / 'lib/python3.11/site-packages').glob('unilabos-*.dist-info/direct_url.json'):
        metadata.unlink()
    manifest = {'schemaVersion': 1, 'protocolVersion': 2, 'product': 'unilabos',
                'entrypoint': {'kind': 'python-module', 'module': 'unilabos.workspace_host.launcher'},
                'platform': 'darwin', 'architecture': 'arm64', 'runtimePrefix': str(prefix),
                'osCommit': revision(ROOT), 'frontendCommit': frontend_record['commit'], 'frontendSourceDirty': frontend_record['sourceDirty'],
                'frontendArtifactDigest': frontend_record['artifactDigest'],
                'sourceDirty': bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT)),
                'condaLockDigest': hashlib.sha256(conda_lock.read_bytes()).hexdigest(),
                'dependencyLockDigest': common.digest(dependency_lock),
                'wheelDigest': hashlib.sha256(wheel.read_bytes()).hexdigest(),
                'minimumMacOS': args.minimum_macos, 'dependencyWheelDigests': dependency_digests}
    manifest['installationSupport'] = common.stage_installation_support(output)
    marker.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    print(f'运行包已生成：{output}', flush=True)


if __name__ == '__main__':
    main()
