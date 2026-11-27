"""Build the usage bundle from an explicit source checkout; copy code, never machine state."""
import argparse
import ast
import hashlib
import json
import shutil
import sys
import zipfile
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]


def build(source):
    source = Path(source).resolve()
    tools = source / 'tools'
    dest = SKILL / 'assets/runtime/tools'
    pending = [tools / 'asset-square/server.py', tools / 'asset-browser/service.py']
    seen = set()
    external = set()
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        tree = ast.parse(path.read_text(encoding='utf-8-sig'))
        seen.add(path)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(n.name.split('.')[0] for n in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names.add(node.module.split('.')[0])
        for name in names:
            # library.py prepends asset-library; square's catalog is its own module.
            search = [path.parent, tools / 'asset-library', tools / 'asset-browser']
            if path.name == 'library.py' and name == 'catalog':
                search = [tools / 'asset-library']
            dep = next((p / (name + '.py') for p in search if (p / (name + '.py')).exists()), None)
            if dep:
                pending.append(dep)
            elif name not in sys.stdlib_module_names:
                external.add(name)
    files = sorted(seen)
    for folder in ['asset-square/web', 'asset-browser/web', 'asset-browser/blender_addon/unilab_asset_browser']:
        for p in (tools / folder).rglob('*'):
            if p.is_file() and p.suffix in ('.py', '.html', '.css', '.js', '.svg', '.png', '.woff2') and '__pycache__' not in p.parts:
                files.append(p)
    rows = []
    for p in sorted(set(files)):
        rel = p.relative_to(tools)
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, out)
        rows.append({'path':out.relative_to(SKILL).as_posix(), 'source':('tools' / rel).as_posix(),
                     'sha256':hashlib.sha256(out.read_bytes()).hexdigest(), 'bytes':out.stat().st_size})
    addon = dest / 'asset-browser/blender_addon/unilab_asset_browser'
    archive = SKILL / 'assets/unilab_asset_browser.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as z:
        for p in sorted(addon.glob('*.py')):
            z.write(p, 'unilab_asset_browser/' + p.name)
    rows.append({'path':archive.relative_to(SKILL).as_posix(), 'sha256':hashlib.sha256(archive.read_bytes()).hexdigest(), 'bytes':archive.stat().st_size})
    manifest = {'schema':'unilab.asset-library-usage-bundle/1', 'addon_version':'0.3.0',
                'external_imports_including_optional':sorted(external), 'files':rows}
    (SKILL / 'assets/runtime-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf8')
    return {'files':len(rows), 'bytes':sum(r['bytes'] for r in rows), 'external_imports':sorted(external)}


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source', required=True, help='Asset_pipeline checkout root')
    print(json.dumps(build(ap.parse_args().source), ensure_ascii=False))
