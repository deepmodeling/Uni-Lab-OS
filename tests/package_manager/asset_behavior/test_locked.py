"""下载、构建、静态绑定、转发执行的离线回归。"""
import importlib
import json
import sys
from pathlib import Path

import pytest

from tests.package_manager.asset_fetch.test_asset_fetch import _make_package, _write
from unilabos.package_manager.package_distribution.adapters.asset_store import LocalMirrorStore
from unilabos.package_manager.package_distribution.asset_behavior import (
    build_locked,
    check_bindings,
    contract_digest,
    verify_generated,
)
from unilabos.package_manager.package_distribution.asset_fetch import fetch_lock, read_asset_lock
from unilabos.package_manager.package_distribution.asset_fetch import native_package as native
from unilabos.registry.ast_registry_scanner import _parse_file


def fixture(tmp_path, *, import_failure=False):
    mirror = tmp_path / 'mirror'
    root, _ = _make_package(mirror)
    meta = native.read_json(root / native.ADAPTER)
    source = ('from unilabos.registry.decorators import action\n'
              'class Motion:\n'
              '    @action()\n'
              '    def move(self, distance: float = 0.01) -> float:\n'
              '        self.position = distance\n'
              '        return self.position\n')
    if import_failure:
        source += '\nraise AssertionError("作者代码不应在构建时执行")\n'
    _write(root / 'simready_usd/behavior/motion.py', source.encode())
    meta['behavior_binding'] = {'kind': 'asset_python', 'files': ['motion.py'],
        'source_root': 'simready_usd/behavior', 'implementation': 'motion:Motion',
        'exports': {'motion': {'implementation': 'motion:Motion',
                              'declaration': {'id': 'asset_binding_test_motion', 'category': ['test']}}}}
    _write(root / native.ADAPTER, native.canonical(meta))
    items = [{'relative_path': p.relative_to(root).as_posix(), 'size': p.stat().st_size,
              'hash': {'sha256': native.sha256_file(p)}} for p in (root / 'simready_usd').rglob('*') if p.is_file()]
    _write(root / native.BOM, native.canonical({'format_version': '1.0', 'items': items}))
    manifest = native.read_json(root / native.MANIFEST)
    manifest['content_hash']['sha256'] = native.content_hash(items)
    for row in manifest['metadata']:
        row['hash']['sha256'] = native.sha256_file(root / '.metadata' / row['name'])
    manifest['package_hash']['sha256'] = native.package_hash(manifest)
    _write(root / native.MANIFEST, native.canonical(manifest))
    row = {'key': 'motion', 'asset_id': meta['asset_id'], 'package_digest': native.sha256_file(root / native.MANIFEST),
           'owner_registry_id': 'asset_binding_test_motion'}
    _write(mirror / 'library.json', native.canonical({'packages': [{**row, 'path': root.name}]}))
    lock = tmp_path / 'lock.json'
    _write(lock, native.canonical({'schema': 'unilab.asset-dependencies/1', 'dependencies': [row]}))
    return mirror, lock, row


def bound(tmp_path):
    mirror, lock, row = fixture(tmp_path)
    cache, project = tmp_path / 'cache', tmp_path / 'built'
    fetch_lock(LocalMirrorStore(mirror), lock, cache)
    result = build_locked(lock, cache, project, 'binding_test_package')
    assert result['generated_devices'] == 1
    declaration = next((project / 'binding_test_package').glob('device_*.py'))
    devices, _ = _parse_file(declaration, project)
    row['os_binding'] = {'definition_file': declaration.relative_to(project).as_posix(), 'class_name': 'Motion',
                        'contract_sha256': contract_digest(devices[0]),
                        'actions': {'move': {'implementation': 'motion:Motion', 'method': 'move', 'adapter': 'direct'}}}
    _write(lock, native.canonical({'schema': 'unilab.asset-dependencies/1', 'dependencies': [row]}))
    return lock, cache, project, row


def test_cold_fetch_build_scan_and_execute(tmp_path, monkeypatch):
    lock, cache, project, _ = bound(tmp_path)
    report = check_bindings(lock, cache, project, scan_file=_parse_file)
    assert report['status'] == 'passed'
    assert report['assets'][0]['execution'] == 'not_run'
    monkeypatch.syspath_prepend(str(project))
    module = 'binding_test_package.' + next((project / 'binding_test_package').glob('device_*.py')).stem
    try:
        device = importlib.import_module(module).Motion()
        assert device.move(0.012) == device.position == 0.012
    finally:
        for name in list(sys.modules):
            if name.startswith('binding_test_package'):
                sys.modules.pop(name)


@pytest.mark.parametrize('change', ['owner', 'action', 'method', 'class', 'contract'])
def test_binding_mismatch_rejected(tmp_path, change):
    lock, cache, project, row = bound(tmp_path)
    b = row['os_binding']
    if change == 'owner': row['owner_registry_id'] = 'wrong'
    elif change == 'action': b['actions']['missing'] = b['actions'].pop('move')
    elif change == 'method': b['actions']['move']['method'] = 'missing'
    elif change == 'class': b['class_name'] = 'Wrong'
    else: b['contract_sha256'] = '0' * 64
    _write(lock, native.canonical({'schema': 'unilab.asset-dependencies/1', 'dependencies': [row]}))
    assert check_bindings(lock, cache, project, scan_file=_parse_file)['status'] == 'failed'


def test_installation_and_contract_baseline(tmp_path):
    lock, cache, project, _ = bound(tmp_path)
    _write(project / 'graph.json', b'{"rotation": [0,0,90], "offset": [1,2,3]}')
    before = check_bindings(lock, cache, project, scan_file=_parse_file, preserve=('graph.json',))
    assert check_bindings(lock, cache, project, scan_file=_parse_file,
                          preserve=('graph.json',), baseline=before)['status'] == 'passed'
    _write(project / 'graph.json', b'{"rotation": [0,0,0], "offset": [1,2,3]}')
    assert check_bindings(lock, cache, project, scan_file=_parse_file,
                          preserve=('graph.json',), baseline=before)['status'] == 'failed'


def test_unbound_motion_is_incomplete(tmp_path):
    mirror, lock, _ = fixture(tmp_path)
    fetch_lock(LocalMirrorStore(mirror), lock, tmp_path / 'cache')
    report = check_bindings(lock, tmp_path / 'cache', tmp_path, scan_file=_parse_file)
    assert report['status'] == 'incomplete'


@pytest.mark.parametrize('tamper', [None, 'owner', 'class', 'contract', 'action'])
def test_passive_asset_binds_explicit_parent_actions(tmp_path, tamper):
    lock, cache, project, row = bound(tmp_path)
    original = dict(row['os_binding'])
    _write(project / 'rail.py', b'from unilabos.registry.decorators import device\n@device(id="passive_rail")\nclass Rail: pass\n')
    rail = _parse_file(project / 'rail.py', project)[0][0]
    row['owner_registry_id'] = 'passive_rail'
    row['os_binding'].update(definition_file='rail.py', class_name='Rail', contract_sha256=contract_digest(rail),
        action_source={'owner_registry_id': 'asset_binding_test_motion', 'definition_file': original['definition_file'],
                       'class_name': 'Motion', 'contract_sha256': original['contract_sha256']})
    source = row['os_binding']['action_source']
    if tamper == 'owner': source['owner_registry_id'] = 'wrong'
    if tamper == 'class': source['class_name'] = 'Wrong'
    if tamper == 'contract': source['contract_sha256'] = '0' * 64
    if tamper == 'action': row['os_binding']['actions']['absent'] = row['os_binding']['actions'].pop('move')
    _write(lock, native.canonical({'schema': 'unilab.asset-dependencies/1', 'dependencies': [row]}))
    report = check_bindings(lock, cache, project, scan_file=_parse_file)
    assert report['status'] == ('failed' if tamper else 'passed')
    if not tamper:
        assert report['assets'][0]['owner_registry_id'] == 'passive_rail'
        assert report['assets'][0]['contract']['actions'] == {}
        baseline = json.loads(json.dumps(report))
        baseline['assets'][0]['action_source_contract']['device_id'] = 'different'
        assert check_bindings(lock, cache, project, scan_file=_parse_file, baseline=baseline)['status'] == 'failed'


def test_no_overwrite(tmp_path):
    mirror, lock, _ = fixture(tmp_path)
    fetch_lock(LocalMirrorStore(mirror), lock, tmp_path / 'cache')
    with pytest.raises(ValueError, match='新目录'):
        build_locked(lock, tmp_path / 'cache', mirror, 'out')


def test_binding_evidence_drift_is_rejected(tmp_path):
    lock, cache, project, row = bound(tmp_path)
    evidence = project/'trajectory.json'
    evidence.write_text('{"origin":0}', encoding='utf8')
    row['os_binding']['evidence_files'] = {'trajectory.json': native.sha256_file(evidence)}
    _write(lock, native.canonical({'schema':'unilab.asset-dependencies/1','dependencies':[row]}))
    assert check_bindings(lock, cache, project, scan_file=_parse_file)['status']=='passed'
    evidence.write_text('{"origin":1}', encoding='utf8')
    assert check_bindings(lock, cache, project, scan_file=_parse_file)['status']=='failed'


def test_generated_device_declaration_tampering_is_rejected(tmp_path):
    _, cache, project, _ = bound(tmp_path)
    generated = json.loads((project / 'assets.lock.json').read_text('utf8'))
    verify_generated(project, generated, asset_root=cache)
    target = next((project / 'binding_test_package').glob('device_*.py'))
    target.write_text(target.read_text('utf8') + '\n# drift\n', encoding='utf8')
    with pytest.raises(ValueError, match='declaration differs'):
        verify_generated(project, generated, asset_root=cache)


def test_snapshot_includes_real_actions_outside_scene_owners(tmp_path):
    from tests.package_manager.asset_behavior.run_locked_batch import snapshot
    _write(tmp_path / 'real.py', b'from unilabos.registry.decorators import device, action\n@device(id="real")\nclass Real:\n @action()\n def run(self): pass\n')
    result = snapshot(tmp_path, {'display_only'})
    assert result['contracts']['real@real.py']['actions'] == ['run']


def test_build_does_not_execute_author_code(tmp_path):
    mirror, lock, _ = fixture(tmp_path, import_failure=True)
    fetch_lock(LocalMirrorStore(mirror), lock, tmp_path / 'cache')
    assert build_locked(lock, tmp_path / 'cache', tmp_path / 'out', 'build_only')['status'] == 'passed'


def test_direct_parameter_mapping_cannot_hide_signature_difference(tmp_path):
    lock, cache, project, row = bound(tmp_path)
    definition = project / row['os_binding']['definition_file']
    definition.write_text(definition.read_text('utf8').replace('distance: float=0.01', 'distance: float=0.02'), encoding='utf8')
    devices, _ = _parse_file(definition, project)
    row['os_binding']['contract_sha256'] = contract_digest(devices[0])
    _write(lock, native.canonical({'schema': 'unilab.asset-dependencies/1', 'dependencies': [row]}))
    report = check_bindings(lock, cache, project, scan_file=_parse_file)
    assert report['status'] == 'failed'
    assert 'direct' in report['assets'][0]['issues'][0]


def test_cli_build_and_binding_report(tmp_path, capsys):
    from unilabos.package_manager.cli import _cmd_assets
    lock, cache, project, _ = bound(tmp_path)
    report = tmp_path / 'report.json'
    _cmd_assets({'assets_action': 'check-bindings', 'lock': str(lock), 'cache': str(cache),
                 'project': str(project), 'output': str(report)})
    assert json.loads(report.read_text('utf8'))['status'] == 'passed'
    _cmd_assets({'assets_action': 'build', 'lock': str(lock), 'cache': str(cache),
                 'python_package': 'cli_build', 'output': str(tmp_path / 'cli-build')})
    assert (tmp_path / 'cli-build/assets.lock.json').is_file()


def test_cli_reports_unbound_as_non_success(tmp_path):
    from unilabos.package_manager.cli import _cmd_assets
    from unilabos.package_manager.package_distribution.errors import PackageCLIError
    mirror, lock, _ = fixture(tmp_path)
    cache = tmp_path / 'cache'
    fetch_lock(LocalMirrorStore(mirror), lock, cache)
    output = tmp_path / 'report.json'
    with pytest.raises(PackageCLIError, match='incomplete'):
        _cmd_assets({'assets_action': 'check-bindings', 'lock': str(lock), 'cache': str(cache),
                     'project': str(tmp_path), 'output': str(output)})
    assert json.loads(output.read_text('utf8'))['status'] == 'incomplete'


@pytest.mark.parametrize('rows', [[], [{'asset_id': 'a', 'package_digest': 'z'*64}],
                                  [{'asset_id': 'a', 'package_digest': '0'*64}]*2])
def test_invalid_lock_is_not_success(tmp_path, rows):
    lock = tmp_path / 'lock.json'
    _write(lock, native.canonical({'schema': 'unilab.asset-dependencies/1', 'dependencies': rows}))
    with pytest.raises(ValueError): read_asset_lock(lock)


def test_evidence_digest_ignores_checkout_line_endings(tmp_path):
    """同一内容在 Windows（CRLF 检出）与 macOS/Linux（LF）上的依据摘要都应通过。"""
    import hashlib

    from unilabos.package_manager.package_distribution.asset_behavior.paths import text_sha, text_sha_matches
    lf, crlf = tmp_path / 'lf.json', tmp_path / 'crlf.json'
    lf.write_bytes(b'{\n  "a": 1\n}\n')
    crlf.write_bytes(b'{\r\n  "a": 1\r\n}\r\n')
    assert text_sha(lf) == text_sha(crlf)
    windows_pin = hashlib.sha256(crlf.read_bytes()).hexdigest()
    # 旧锁在 Windows 上按 CRLF 字节写的摘要，在 LF 检出上仍认；新锁写行尾无关摘要。
    assert text_sha_matches(lf, windows_pin) and text_sha_matches(crlf, windows_pin)
    assert text_sha_matches(lf, text_sha(crlf)) and text_sha_matches(crlf, text_sha(lf))
    assert not text_sha_matches(lf, hashlib.sha256(b'other').hexdigest())


def test_build_generates_material_definitions_from_the_asset_block(tmp_path):
    """耗材资产带 parameters.material_definition：构建只读资产（不量几何、不导入作者代码）生成物料定义。"""
    mirror = tmp_path / 'mirror'
    root, _ = _make_package(mirror)
    meta = native.read_json(root / native.ADAPTER)
    meta['interfaces_data'] = {'units': 'mm', 'sites': [
        {'name': 'A1', 'position_mm': [14.0, 74.0, 40.0], 'opening_mm': [8.0, 8.0]},
        {'name': 'B1', 'position_mm': [14.0, 65.0, 40.0], 'opening_mm': [8.0, 8.0]}]}
    meta.setdefault('parameters', {})['material_definition'] = {
        'schema': 'unilab.material-definition/1', 'resource_id': 'demo_built_plate', 'kind': 'plate',
        'geometry': {'body_min_mm': [0.0, 0.0, 0.0], 'body_max_mm': [127.0, 85.0, 42.0], 'basis': '资产包围盒'},
        'inputs': {'well_depth': {'mm': 38.0, 'basis': 'CAD 截面'}}}
    _write(root / native.ADAPTER, native.canonical(meta))
    items = [{'relative_path': p.relative_to(root).as_posix(), 'size': p.stat().st_size,
              'hash': {'sha256': native.sha256_file(p)}} for p in (root / 'simready_usd').rglob('*') if p.is_file()]
    _write(root / native.BOM, native.canonical({'format_version': '1.0', 'items': items}))
    manifest = native.read_json(root / native.MANIFEST)
    manifest['content_hash']['sha256'] = native.content_hash(items)
    for row in manifest['metadata']:
        row['hash']['sha256'] = native.sha256_file(root / '.metadata' / row['name'])
    manifest['package_hash']['sha256'] = native.package_hash(manifest)
    _write(root / native.MANIFEST, native.canonical(manifest))
    row = {'key': 'plate', 'asset_id': meta['asset_id'], 'package_digest': native.sha256_file(root / native.MANIFEST)}
    _write(mirror / 'library.json', native.canonical({'packages': [{**row, 'path': root.name}]}))
    lock = tmp_path / 'lock.json'
    _write(lock, native.canonical({'schema': 'unilab.asset-dependencies/1', 'dependencies': [row]}))
    fetch_lock(LocalMirrorStore(mirror), lock, tmp_path / 'cache')
    result = build_locked(lock, tmp_path / 'cache', tmp_path / 'out', 'station_assets')
    assert result['generated_resources'] == 1
    built = json.loads((tmp_path / 'out/assets.lock.json').read_text('utf8'))
    (generated,) = built['generated_resources']
    assert generated['module'] == 'station_assets.resources.demo_built_plate:make_demo_built_plate'
    _, resources = _parse_file(tmp_path / 'out' / generated['file'], tmp_path / 'out')
    assert [r['resource_id'] for r in resources] == ['demo_built_plate']
    pytest.importorskip('pylabrobot')
    sys.path.insert(0, str(tmp_path / 'out'))
    try:
        module = importlib.import_module('station_assets.resources.demo_built_plate')
        plate = module.make_demo_built_plate('p', with_items=True)
        assert plate.get_item('A1').get_size_z() == 38.0 and len(module.make_demo_built_plate('g').children) == 0
    finally:
        sys.path.remove(str(tmp_path / 'out'))
        for name in [n for n in sys.modules if n.startswith('station_assets')]:
            del sys.modules[name]
    (tmp_path / 'out' / generated['file']).write_text('# changed\n', encoding='utf8')
    with pytest.raises(ValueError):
        verify_generated(tmp_path / 'out', built, asset_root=tmp_path / 'cache')
