"""Bootstrap a browsable mirror from published OSS indexes, without model bodies."""
import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from core import inside, digest
from minimal_format import SUPPORTED_SCHEMAS, check_retired_revisions, retry


def sha(data):
    return hashlib.sha256(data).hexdigest()


def sync(lib, previews=False):
    lib.reload()
    old = lib.manifest or {}
    raw = retry(lambda: lib.store.get('library.json'))
    index = json.loads(raw)
    if index.get('schema') not in SUPPORTED_SCHEMAS:
        raise ValueError('Unsupported published library schema')
    check_retired_revisions(old, index)
    if old.get('local_catalog') and old.get('local_catalog') != index.get('local_catalog'):
        raise ValueError('Local catalog overlay differs; use a separate OSS consumer mirror')
    if set(old.get('retired_package_digests', [])) - set(index.get('retired_package_digests', [])):
        raise ValueError('Remote index would discard local retirement history; use a separate OSS consumer mirror')
    unified_raw = retry(lambda: lib.store.get('统一目录/library.json'))
    unified = json.loads(unified_raw)
    if unified.get('source_library_digest') != sha(raw):
        raise ValueError('Published engineering and unified indexes are not one snapshot; retry after publication completes')
    if unified.get('schema') != 'unilab.unified-catalog/1':
        raise ValueError('Unsupported published unified catalog')

    rows = {}
    def add(path, expected=None, size=None):
        inside(lib.root, path)  # validates remote paths before any download/write
        value = {'path':path, 'sha256':expected, 'bytes':size}
        if path in rows and rows[path] != value:
            raise ValueError('Conflicting catalog file declarations: ' + path)
        rows[path] = value
    for ref in unified['files'].values():
        add('统一目录/' + ref['path'], ref['sha256'])
    for name, value in unified.get('dictionaries', {}).items():
        if name not in ('category-index.json', 'scene-index.json', 'facets.json', 'manufacturers.json'):
            raise ValueError('Unknown dictionary filename: ' + name)
        add('统一目录/' + ('分类/' if name in ('category-index.json', 'scene-index.json') else '') + name, value)
    # Older catalogs keep these independently. Read only when the index requires them.
    if 'tag_definitions' not in unified:
        add('统一目录/tags.json')
    if unified.get('source_offsets'):
        add('统一目录/sources.jsonl')
    if unified.get('backfill_offsets'):
        add('统一目录/backfill-mappings.jsonl')
    if previews:
        metadata = {r['path']:r for r in index.get('metadata_files', [])}
        for value in index.get('presentation', {}).values():
            thumb = value.get('thumbnail', {})
            path = thumb.get('path')
            if path and path in metadata:
                row = metadata[path]
                if row['sha256'] != thumb['sha256']:
                    raise ValueError('Preview declaration mismatch')
                add(path, row['sha256'], row['bytes'])

    state_file = lib.state / 'catalog-download-state.json'
    prior = json.loads(state_file.read_text(encoding='utf8')) if state_file.exists() else {}
    managed = prior.get('files', {}) if prior.get('library_root') == str(lib.root) else {}
    previous = {r['path']:r['sha256'] for r in old.get('metadata_files', [])}
    previous.update(managed)
    previous['library.json'] = sha((lib.root/'library.json').read_bytes()) if (lib.root/'library.json').exists() else None
    old_unified = lib.root / '统一目录/library.json'
    if old_unified.exists():
        previous['统一目录/library.json'] = digest(old_unified)
        u = json.loads(old_unified.read_text(encoding='utf8'))
        for ref in u.get('files', {}).values():
            previous['统一目录/' + ref['path']] = ref['sha256']
        for name, value in u.get('dictionaries', {}).items():
            previous['统一目录/' + ('分类/' if name in ('category-index.json', 'scene-index.json') else '') + name] = value

    stage = lib.state / 'catalog-downloads' / sha(raw)
    stage.mkdir(parents=True, exist_ok=True)
    def download(row):
        target = inside(lib.root, row['path'])
        cached = inside(stage, row['path'])
        expected = row['sha256']
        if target.is_file() and expected and digest(target) == expected:
            return {**row, 'sha256':expected, 'bytes':target.stat().st_size, 'downloaded':False, 'cached':False}
        if target.exists() and (not previous.get(row['path']) or digest(target) != previous[row['path']]):
            raise ValueError('Locally modified catalog file: ' + row['path'])
        cache_hit = cached.is_file() and expected and digest(cached) == expected
        content = cached.read_bytes() if cache_hit else retry(lambda:lib.store.get(row['path']))
        if (expected and sha(content) != expected) or (row['bytes'] is not None and len(content) != row['bytes']):
            raise ValueError('Catalog hash/size mismatch: ' + row['path'])
        cached.parent.mkdir(parents=True, exist_ok=True)
        if not cache_hit:
            tmp = cached.with_name(cached.name + '.partial')
            tmp.write_bytes(content)
            os.replace(tmp, cached)
        return {**row, 'sha256':sha(content), 'bytes':len(content), 'downloaded':not cache_hit, 'cached':True,
                'integrity':'manifest_sha256' if expected else 'authenticated_get_recorded_sha256'}
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(download, rows.values()))
    # Indexes are switched only after every required catalog file has passed validation.
    for result in results:
        if result['cached']:
            target = inside(lib.root, result['path'])
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + '.catalog-partial')
            tmp.write_bytes(inside(stage, result['path']).read_bytes())
            os.replace(tmp, target)
    lib.root.mkdir(parents=True, exist_ok=True)
    for path, content in [('统一目录/library.json', unified_raw), ('library.json', raw)]:
        target = inside(lib.root, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + '.catalog-partial')
        tmp.write_bytes(content)
        os.replace(tmp, target)
    hashes = {r['path']:r['sha256'] for r in results}
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps({'library_root':str(lib.root), 'files':hashes,
                                     'snapshot_sha256':sha(raw)}, ensure_ascii=False, indent=2), encoding='utf8')
    lib.reload()
    lib.store._usage_index = index
    return {'status':'passed', 'mode':'catalog_and_previews' if previews else 'catalog_only',
            'engineering_records':len(index['records']), 'public_entries':len(unified['entries']),
            'downloaded_files':2 + sum(r['downloaded'] for r in results),
            'downloaded_bytes':len(raw)+len(unified_raw)+sum(r['bytes'] for r in results if r['downloaded']),
            'model_payload_files_downloaded':0, 'snapshot_sha256':sha(raw),
            'unified_snapshot_sha256':sha(unified_raw)}
