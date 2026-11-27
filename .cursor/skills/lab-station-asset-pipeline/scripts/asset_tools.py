"""Run the skill's pinned conversion/validation tools without a source checkout."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parent / 'toolkit'


def verified_manifest():
    manifest = json.loads((ROOT / 'manifest.json').read_text(encoding='utf-8'))
    if manifest['schema'] != 'unilab.skill-toolkit/1':
        raise ValueError('Unknown toolkit manifest')
    for relative, record in manifest['files'].items():
        path = (ROOT / relative).resolve()
        if not path.is_relative_to(ROOT.resolve()) or not path.is_file():
            raise ValueError('Missing or external tool: ' + relative)
        if hashlib.sha256(path.read_bytes()).hexdigest() != record['sha256']:
            raise ValueError('Tool digest differs: ' + relative)
    return manifest


def api(command, arguments):
    parser = argparse.ArgumentParser(prog='asset_tools run ' + command)
    if command == 'native-verify':
        parser.add_argument('package'); parser.add_argument('--digest')
    else:
        parser.add_argument('before'); parser.add_argument('after')
    args = parser.parse_args(arguments)
    record = verified_manifest()['commands'][command]
    file = ROOT / record['path']
    sys.path.insert(0, str(file.parent))
    spec = importlib.util.spec_from_file_location('_skill_tool_api', file)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    result = (module.verify(args.package, args.digest) if command == 'native-verify'
              else module.compare(args.before, args.after))
    print(json.dumps(result, ensure_ascii=False))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('list'); sub.add_parser('verify-toolkit')
    run = sub.add_parser('run')
    run.add_argument('command'); run.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    manifest = verified_manifest()
    if args.action == 'verify-toolkit':
        print(json.dumps({'status': 'passed', 'files': len(manifest['files'])})); return 0
    if args.action == 'list':
        print(json.dumps(manifest['commands'], ensure_ascii=False, indent=2)); return 0
    if args.command not in manifest['commands']:
        parser.error('Unknown command; use list')
    arguments = args.arguments[1:] if args.arguments[:1] == ['--'] else args.arguments
    record = manifest['commands'][args.command]
    if record['kind'] == 'api':
        return api(args.command, arguments)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    return subprocess.call([sys.executable, '-B', str(ROOT / record['path']), *arguments], env=env)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError, ImportError) as error:
        print(type(error).__name__ + ': ' + str(error), file=sys.stderr)
        raise SystemExit(1)
