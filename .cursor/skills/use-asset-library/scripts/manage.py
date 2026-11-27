"""Configure, start and check the bundled local asset-library services."""
import argparse
import hashlib
import json
import os
import runpy
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
TOOLS = SKILL / 'assets/runtime/tools'
sys.path.insert(0, str(TOOLS/'asset-browser'))
from access_guidance import AccessSetupError, GUIDE, safe_error


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write_new(path, value):
    with Path(path).open('x', encoding='utf8') as f:
        json.dump(value, f, ensure_ascii=False, indent=2)


def verify_bundle():
    manifest = read(SKILL / 'assets/runtime-manifest.json')
    for row in manifest['files']:
        p = SKILL / row['path']
        if hashlib.sha256(p.read_bytes()).hexdigest() != row['sha256']:
            raise ValueError('Bundled file differs: ' + row['path'])
    return {'status':'passed', 'files':len(manifest['files'])}


def configure(args):
    root = Path(args.library_root).resolve()
    if not args.oss_config:
        for rel in ('library.json', '统一目录/library.json'):
            if not (root / rel).is_file():
                raise AccessSetupError('oss_not_configured','本地还没有资产目录。先取得 OSS 配置，再使用 configure --oss-config、doctor --check-oss、bootstrap。'+GUIDE)
    elif root.exists() and any(root.iterdir()) and not (root/'library.json').is_file():
        raise ValueError('Select an empty mirror directory, or an existing indexed library')
    if args.bridge_port == args.square_port:
        raise ValueError('Bridge and square need distinct ports')
    for p in (args.bridge_port, args.square_port):
        if not 1024 <= p <= 65535:
            raise ValueError('Use ports in 1024..65535')
    output = Path(args.output).resolve()
    names = ['service.json', 'runtime.json', 'services.json', 'oss.json' if args.oss_config else 'oss-local-only.json']
    if any((output / n).exists() for n in names):
        raise ValueError('Configuration already exists; use it or select a new output folder')
    oss = output / ('oss.json' if args.oss_config else 'oss-local-only.json')
    if args.oss_config:
        data = read(Path(args.oss_config).resolve())
        for k in ('endpoint', 'region', 'bucket', 'prefix'):
            if not data.get(k):
                raise ValueError('Missing OSS configuration field: ' + k)
        if not data['prefix'].endswith('/'):
            raise ValueError('OSS prefix must end with /')
        if data.get('layout') != 'nvidia-usd/1':
            raise ValueError('Cloud bootstrap requires a published nvidia-usd/1 library')
    state = Path(args.state_root).resolve()
    if state == root or root in state.parents:
        raise ValueError('Keep cache/state outside the asset library')
    output.mkdir(parents=True, exist_ok=True)
    if not args.oss_config:
        write_new(oss, {'layout':'nvidia-usd/1', 'mode':'local_only'})
    else:
        # Copy only connection coordinates, never AK/SK or arbitrary source configuration.
        write_new(oss, {**{k:data[k] for k in ('endpoint','region','bucket','prefix','layout')}, 'read_only':True})
    bridge = {'library_root':str(root), 'state_root':str(state), 'oss_config':str(oss),
              'port':args.bridge_port, 'square_port':args.square_port, 'local_candidates':not bool(args.oss_config),
              'catalog_sync':'square', 'download_previews':True,
              'addon_zip':str(SKILL / 'assets/unilab_asset_browser.zip')}
    write_new(output / 'service.json', bridge)
    runtime = {'python':sys.executable, 'script':str(TOOLS / 'asset-browser/service.py'),
               'config':str(output / 'service.json'), 'square_python':sys.executable,
               'square_script':str(TOOLS / 'asset-square/server.py'), 'library_root':str(root),
               'square_port':args.square_port}
    write_new(output / 'runtime.json', runtime)
    services = {'library_root':str(root), 'square_port':args.square_port, 'bridge_port':args.bridge_port}
    write_new(output / 'services.json', services)
    return {'config_dir':str(output), 'mode':'oss' if args.oss_config else 'local_only', **services}


def cloud_action(command, folder, args):
    config = read(folder/'service.json')
    if config.get('local_candidates') and command == 'bootstrap':
        raise ValueError('Local-only configuration has no OSS source')
    sys.path.insert(0, str(TOOLS/'asset-browser'))
    from library import Library
    lib = Library(config['library_root'], config['oss_config'], config['state_root'])
    if command == 'bootstrap':
        from catalog_sync import sync
        result = sync(lib, previews=not args.skip_previews)
    else:
        if not (lib.root/'library.json').is_file():
            raise ValueError('Run bootstrap first')
        catalog_id = args.catalog_id
        if args.entry_id:
            # Keep square's catalog.py isolated from asset-library/catalog.py.
            root = lib.root/'统一目录'
            index = read(root/'library.json')
            key = index.get('aliases',{}).get(args.entry_id,args.entry_id)
            row = next((r for r in index['entries'] if r['id']==key),None)
            if not row:
                raise ValueError('Public entry not found')
            catalog_id = row.get('asset_id') or row['id']
        if catalog_id not in lib.records:
            raise ValueError('Entry has no mapped engineering model; select a 3D model entry')
        result = lib.prepare(catalog_id)
    out = folder/('last-'+command+'.json')
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    return result


def doctor(folder=None, check_oss=False):
    if folder is None or not (folder/'service.json').is_file():
        return {'status':'needs_setup','code':'oss_not_configured','message':'尚未生成资产库配置。先运行 configure --oss-config <连接配置>。'+GUIDE}
    config=read(folder/'service.json')
    local_ready=all((Path(config['library_root'])/p).is_file() for p in ('library.json','统一目录/library.json'))
    if config.get('local_candidates'):
        return {'status':'local_only','local_catalog_ready':local_ready,'message':'当前配置只使用本地镜像。需要同步或下载缺失模型时配置 OSS。'+GUIDE}
    try:
        from access_guidance import validate_config
        from credentials import load_credentials
        validate_config(config.get('oss_config'))
        load_credentials()
        if check_oss:
            from library import Library
            lib=Library(config['library_root'],config['oss_config'],config['state_root'])
            for key in ('library.json','统一目录/library.json'):
                json.loads(lib.store.get(key))
        return {'status':'ready' if check_oss else 'configured_unverified','local_catalog_ready':local_ready,
                'message':'两个目录入口读取成功；其他模型的权限在按需下载时验证。' if check_oss else '已找到地址和凭据，尚未验证权限。运行 doctor --config-dir <配置目录> --check-oss。'}
    except Exception as exc:
        return {'status':'needs_setup','local_catalog_ready':local_ready,**safe_error(exc)}


def health(role, cfg):
    url = f"http://127.0.0.1:{cfg[role + '_port']}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            data = json.load(response)
    except urllib.error.URLError as e:
        if isinstance(e, urllib.error.HTTPError):
            raise ValueError(f'{role} port is occupied by an unexpected HTTP service ({e.code})')
        return None
    if data.get('status') != 'ready' or not data.get('root'):
        raise ValueError(f'{role}: unexpected health response')
    if role == 'square' and data.get('service') != 'asset-square':
        raise ValueError('Square port is used by another service')
    if role == 'bridge' and (data.get('service') == 'asset-square' or not data.get('version')):
        raise ValueError('Bridge port is used by another service')
    if Path(data['root']).resolve() != Path(cfg['library_root']).resolve():
        raise ValueError(f'{role} serves a different library: ' + data['root'])
    return {'url':url, 'root':data['root'], 'status':'ready'}


def serve(role, folder, cfg):
    if role == 'square':
        script = TOOLS / 'asset-square/server.py'
        argv = ['--root', cfg['library_root'], '--port', str(cfg['square_port'])]
    else:
        script = TOOLS / 'asset-browser/service.py'
        argv = ['--config', str(folder / 'service.json')]
    sys.path.insert(0, str(script.parent))
    sys.argv = [str(script), *argv]
    runpy.run_path(str(script), run_name='__main__')


def start(folder, cfg):
    verify_bundle()
    for rel in ('library.json','统一目录/library.json'):
        if not (Path(cfg['library_root'])/rel).is_file():
            raise ValueError('Run bootstrap before starting the services')
    # Check both ports before launching anything; do not interfere with another installation.
    current = {role:health(role, cfg) for role in ('square', 'bridge')}
    started = []
    try:
        for role, state in current.items():
            if state:
                continue
            with (folder / (role + '.log')).open('ab') as log:
                proc = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), 'serve', role,
                                         '--config-dir', str(folder)], stdout=log, stderr=log,
                                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            started.append((role, proc))
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError(role + ' exited; inspect ' + str(folder / (role + '.log')))
                if health(role, cfg):
                    break
                time.sleep(.25)
            else:
                raise RuntimeError(role + ' did not become ready within 25 seconds')
    except Exception:
        for _, proc in started:
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=10)
        raise
    result = {'services':{role:health(role, cfg) for role in current},
              'started':[{'role':role, 'pid':proc.pid} for role, proc in started]}
    with (folder / 'last-start.json').open('w', encoding='utf8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return result


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest='command', required=True)
    sub.add_parser('verify-bundle')
    d=sub.add_parser('doctor',help='检查首次使用配置；--check-oss 实际读取目录入口')
    d.add_argument('--config-dir')
    d.add_argument('--check-oss',action='store_true')
    c = sub.add_parser('configure')
    c.add_argument('--library-root', required=True)
    c.add_argument('--output', required=True)
    c.add_argument('--state-root', required=True)
    c.add_argument('--oss-config')
    c.add_argument('--square-port', type=int, default=8766)
    c.add_argument('--bridge-port', type=int, default=8765)
    for name in ('start', 'check', 'serve', 'bootstrap', 'prepare'):
        parser = sub.add_parser(name)
        parser.add_argument('--config-dir', required=True)
        if name == 'serve':
            parser.add_argument('role', choices=('square', 'bridge'))
        if name == 'bootstrap':
            parser.add_argument('--skip-previews', action='store_true', help='Download catalog only, leaving cover images optional')
        if name == 'prepare':
            ids=parser.add_mutually_exclusive_group(required=True)
            ids.add_argument('--catalog-id')
            ids.add_argument('--entry-id', help='Public UUID mapped to an engineering catalog ID')
    a = ap.parse_args()
    if a.command == 'doctor':
        result=doctor(Path(a.config_dir).resolve() if a.config_dir else None,a.check_oss)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        if result['status']=='needs_setup':raise SystemExit(1)
        return
    if a.command == 'verify-bundle':
        result = verify_bundle()
    elif a.command == 'configure':
        result = configure(a)
    else:
        folder = Path(a.config_dir).resolve()
        cfg = read(folder / 'services.json')
        if a.command in ('bootstrap','prepare'):
            result=cloud_action(a.command,folder,a)
            print(json.dumps(result,ensure_ascii=False,indent=2))
            return
        if a.command == 'serve':
            return serve(a.role, folder, cfg)
        result = start(folder, cfg) if a.command == 'start' else {role:health(role, cfg) for role in ('square', 'bridge')}
        if a.command == 'check' and any(v is None for v in result.values()):
            print(json.dumps(result, ensure_ascii=False))
            raise SystemExit(1)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:main()
    except Exception as exc:
        print(json.dumps({'status':'failed',**safe_error(exc)},ensure_ascii=False,indent=2))
        raise SystemExit(1) from None
