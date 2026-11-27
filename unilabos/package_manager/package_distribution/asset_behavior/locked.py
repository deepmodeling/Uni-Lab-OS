"""固定资产的离线构建与绑定检查；不导入作者代码、不修改设备声明或装配。

OS 扫描器由组合根注入。锁行可附 os_binding：definition_file、class_name、
contract_sha256、actions。actions 的值为显式 implementation 和 method；
检查只证明静态合同，实际执行资格必须由独立回归提供。
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Callable

from ..asset_fetch import read_asset_lock
from ..asset_fetch.native_package import MANIFEST, canonical, read_json
from .forwarder import write_forwarder
from .materialize import behavior_kind, materialize, verify_generated
from .package import verify_package
from .paths import inside, sha, text_sha_matches


def resolve_cached(lock: Path, cache: Path) -> tuple[list[dict], dict]:
    """按清单摘要解析已有缓存；不访问网络，不把显示名称作为身份。"""
    rows = read_asset_lock(lock)
    cache = Path(cache).resolve()
    index: dict[str, list[Path]] = {}
    for manifest in cache.rglob(MANIFEST):
        if '.staging' not in manifest.relative_to(cache).parts:
            index.setdefault(sha(manifest), []).append(manifest.parent)
    assets = {}
    for row in rows:
        if row.get('path') or not row.get('asset_id') or not row.get('package_digest'):
            raise ValueError('行为构建只接受资产库固定包（asset_id + package_digest），不接受本地路径行：'
                             + str(row.get('key') or row.get('path')))
        key = row.get('key') or row.get('owner_registry_id') or row['asset_id']
        if key in assets:
            raise ValueError('资产锁的选择键重复：' + key)
        matches = index.get(row['package_digest'], [])
        if len(matches) != 1:
            raise ValueError(f'固定包缓存必须唯一：{key}，找到 {len(matches)} 个')
        root = matches[0]
        metadata = verify_package(root, row['package_digest'])
        if metadata['asset_id'] != row['asset_id']:
            raise ValueError('缓存资产 ID 与锁不一致：' + key)
        assets[key] = {'package_path': root.relative_to(cache).as_posix(), 'metadata': metadata}
    return rows, assets


def contract_view(device: dict) -> dict:
    """冻结公开 device/action 合同；实现文件位置与模型路径不进入动作合同。"""
    keys = ('device_id', 'actions', 'status_properties', 'init_params', 'auto_methods', 'available_sites')
    return {key: device.get(key) for key in keys}


def contract_digest(device: dict) -> str:
    return hashlib.sha256(canonical(contract_view(device))).hexdigest()


def _methods(root: Path, spec: dict, entry: str) -> set[str]:
    module, symbol = entry.split(':')
    rel = module.replace('.', '/') + '.py'
    if rel not in spec['files']:
        raise ValueError('行为入口未在 files 中声明：' + entry)
    source = inside(root, spec['source_root'] + '/' + rel)
    tree = ast.parse(source.read_text('utf-8'))
    classes = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == symbol]
    if len(classes) != 1:
        raise ValueError('行为类不存在或不唯一：' + entry)
    return {n.name for n in classes[0].body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _signature(source: Path, class_name: str, method: str) -> tuple:
    tree = ast.parse(source.read_text('utf-8'))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == class_name)
    fn = next(n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == method)
    return (ast.dump(fn.args), ast.dump(fn.returns) if fn.returns else None, isinstance(fn, ast.AsyncFunctionDef))


def check_bindings(lock: Path, cache: Path, project: Path, *, scan_file: Callable,
                   preserve: tuple[str, ...] = (), baseline: dict | None = None) -> dict:
    """逐锁项核对固定包、显式设备/动作绑定及基线；未绑定报告 incomplete。"""
    rows, assets = resolve_cached(lock, cache)
    project = Path(project).resolve()
    report = {'schema': 'unilab.asset-binding-report/1', 'lock_sha256': sha(lock),
              'scope': 'static_binding_only', 'assets': [], 'preserved_files': {}, 'errors': []}
    for row, (key, asset) in zip(rows, assets.items()):
        spec = asset['metadata'].get('behavior_binding')
        item = {'key': key, 'asset_id': row['asset_id'], 'package_digest': row['package_digest'],
                'owner_registry_id': row.get('owner_registry_id'), 'status': 'incomplete',
                'execution': 'not_run', 'issues': []}
        report['assets'].append(item)
        try:
            kind = behavior_kind(spec)
            item['behavior_kind'] = kind
            binding = row.get('os_binding')
            if not binding:
                item['status'] = 'model_only' if kind is None else 'incomplete'
                item['issues'] = [] if kind is None else ['缺少显式 OS 动作绑定；源码存在不代表动作已接通']
                continue
            source = inside(project, binding['definition_file'])
            definitions, _ = scan_file(source, project)
            owner = row.get('owner_registry_id')
            matches = [d for d in definitions if d.get('device_id') == owner]
            if len(matches) != 1:
                raise ValueError('声明中找不到唯一 owner_registry_id：' + str(owner))
            device = matches[0]
            if device.get('class_name') != binding['class_name']:
                raise ValueError('owner_registry_id 与 class_name 不属于同一个声明')
            # 扫描器的 class_name 字段随版本存在差异，以 AST 精确核对声明类。
            tree = ast.parse(source.read_text('utf-8'))
            if binding['class_name'] not in {n.name for n in tree.body if isinstance(n, ast.ClassDef)}:
                raise ValueError('设备类名不存在')
            item['contract'] = contract_view(device)
            item['contract_sha256'] = contract_digest(device)
            if item['contract_sha256'] != binding['contract_sha256']:
                raise ValueError('device/action/状态/库位合同与显式绑定摘要不符')
            evidence_files = binding.get('evidence_files', {})
            for relative, expected in evidence_files.items():
                if not text_sha_matches(inside(project, relative), expected):
                    raise ValueError('绑定依据文件摘要不符：' + relative)
            if evidence_files:
                item['evidence_files'] = evidence_files
            if kind != 'asset_python':
                raise ValueError('动作必须绑定显式 asset_python owner；引用资产请绑定其所有者')
            # 被动子模组没有独立动作；显式固定驱动它的上级设备，保留模型 owner。
            action_device = device
            action_source = binding.get('action_source')
            if action_source:
                action_file = inside(project, action_source['definition_file'])
                action_definitions, _ = scan_file(action_file, project)
                candidates = [d for d in action_definitions
                              if d.get('device_id') == action_source['owner_registry_id']]
                if len(candidates) != 1:
                    raise ValueError('找不到唯一上级动作 owner')
                action_device = candidates[0]
                if (action_device.get('class_name') != action_source['class_name'] or
                        contract_digest(action_device) != action_source['contract_sha256']):
                    raise ValueError('上级动作声明或合同摘要不符')
                item['action_source'] = action_source
                item['action_source_contract'] = contract_view(action_device)
            else:
                action_file = source
            mappings = binding.get('actions', {})
            if not mappings:
                item['issues'].append('缺少显式 action → behavior 映射')
                continue
            exports = {spec.get('implementation')} | {
                v if isinstance(v, str) else v['implementation'] for v in spec.get('exports', {}).values()}
            root = inside(cache, asset['package_path'])
            for action, target in mappings.items():
                if action not in action_device.get('actions', {}):
                    raise ValueError('OS 动作不存在：' + action)
                if target['implementation'] not in exports:
                    raise ValueError('目标实现不是资产声明的导出：' + action)
                if target['method'] not in _methods(root, spec, target['implementation']):
                    raise ValueError('目标行为方法不存在：' + action)
                if not target.get('adapter'):
                    raise ValueError('必须显式标明动作到机构参数的 adapter（同签名转发可填 direct）')
                if target['adapter'] == 'direct':
                    module, cls = target['implementation'].split(':')
                    implementation = inside(root, spec['source_root'] + '/' + module.replace('.', '/') + '.py')
                    if _signature(action_file, action_device['class_name'], action) != _signature(implementation, cls, target['method']):
                        raise ValueError('direct 转发参数、默认值、返回类型或异步合同不一致：' + action)
                else:
                    adapter = inside(project, target['adapter'])
                    if not text_sha_matches(adapter, target.get('adapter_sha256')) or not target.get('argument_mapping'):
                        raise ValueError('参数适配必须固定源码摘要并声明 argument_mapping（含单位换算）')
            item['status'] = 'declared_binding'
            item['actions'] = mappings
        except (ValueError, KeyError, TypeError, OSError, SyntaxError, StopIteration) as error:
            item['status'] = 'failed'
            item['issues'].append(str(error))
    for relative in preserve:
        report['preserved_files'][relative] = sha(inside(project, relative))
    if baseline is not None:
        before = {v['key']: v for v in baseline['assets']}
        after = {v['key']: v for v in report['assets']}
        if before.keys() != after.keys():
            report['errors'].append('回归资产集合改变')
        for key in before.keys() & after.keys():
            for field in ('asset_id', 'package_digest', 'owner_registry_id', 'contract', 'actions',
                          'action_source', 'action_source_contract', 'evidence_files'):
                if before[key].get(field) != after[key].get(field):
                    report['errors'].append(f'{key}: {field} 改变')
        if report['preserved_files'] != baseline['preserved_files']:
            report['errors'].append('安装变换/声明快照文件或受保护文件集合改变')
    statuses = {v['status'] for v in report['assets']}
    report['status'] = ('failed' if report['errors'] or 'failed' in statuses else
                        'incomplete' if 'incomplete' in statuses else 'passed')
    return report


def build_locked(lock: Path, cache: Path, output: Path, package: str) -> dict:
    """将缓存的封存脚本固化到新目录；只为显式 declaration 生成转发类。

    未声明设备导出的机构控制器只生成源码，不猜测 device/action。模型仍由固定资产锁解析。
    """
    if not package.isidentifier():
        raise ValueError('输出必须是单层 Python 导入包名')
    output = Path(output).resolve()
    cache = Path(cache).resolve()
    if output.is_relative_to(cache) or cache.is_relative_to(output):
        raise ValueError('构建目录与固定缓存必须互不包含')
    if output.exists():
        raise ValueError('构建目标必须是不存在的新目录')
    rows, assets = resolve_cached(lock, cache)
    locks = copy.deepcopy(rows)
    for row, asset in zip(locks, assets.values()):
        row['path'] = asset['package_path']
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.asset-build-', dir=output.parent) as temporary:
        stage = Path(temporary) / 'output'
        (stage / package).mkdir(parents=True)
        (stage / package / '__init__.py').write_text('', encoding='utf-8')
        bindings, requires = materialize(stage, package, assets, locks, asset_root=cache)
        declarations = []
        registry_ids = set()
        for key, bound in bindings.items():
            exports = bound.get('exports') or {'default': bound}
            for role, export in exports.items():
                if not export.get('declaration'):
                    continue
                declaration = export['declaration']
                ids = declaration.get('ids', [declaration.get('id')])
                if not ids or len(ids) != len(set(ids)) or any(not isinstance(v, str) or not v for v in ids) or registry_ids.intersection(ids):
                    raise ValueError('设备导出 ID 缺失或重复')
                registry_ids.update(ids)
                module, symbol = export['implementation'].split(':')
                source = stage / (module.replace('.', '/') + '.py')
                # 模型路径需要消费者显式布置；不把源目录相对入口误写到新声明位置。
                if declaration.get('model', {}).get('entry'):
                    raise ValueError('模型 entry 须由领域构建器重定位后生成声明，不能直接复制')
                target = stage / package / ('device_' + bound['module_root'].split('.')[-1] + '_' + role + '.py')
                declarations.append(write_forwarder(target, source, module, symbol, package=stage / package,
                                                      declaration=declaration, origin=key))
        # 耗材资产带 parameters.material_definition 时，按资产生成物料定义（PLR + @resource 工厂），写进包内
        # resources/；内容只由资产决定，哪个设备包按锁构建都得到同样的文件。只用标准库，不量几何。
        from .resource_definition import facts_from_asset, generate_resource_definition
        resources = []
        for key, asset in assets.items():
            block = (asset['metadata'].get('parameters') or {}).get('material_definition')
            if not block:
                continue
            facts = facts_from_asset(asset['metadata'], Path(asset['package_path']).name)
            resource_id = facts.pop('resource_id')
            facts['evidence']['package_digest'] = next(r['package_digest'] for r in locks if (r.get('key') or r.get('owner_registry_id') or r['asset_id']) == key)
            if resource_id in {r['resource_id'] for r in resources}:
                raise ValueError('物料定义 resource_id 重复：' + resource_id)
            folder = stage / package / 'resources'
            if not folder.exists():
                folder.mkdir()
                (folder / '__init__.py').write_text('', encoding='utf-8')
            target = folder / (resource_id + '.py')
            # 显示模型挂在物料定义上：约定按锁物化到定义文件旁的 models/<资源 ID>/usd/，根文件名同资产入口。
            entry = Path(str((asset['metadata'].get('entrypoints') or {}).get('usd') or ''))
            model = ({'format': entry.suffix.lstrip('.').lower(), 'entry': f'models/{resource_id}/usd/{entry.name}'}
                     if entry.name else None)
            target.write_text(generate_resource_definition(resource_id, facts, displayname=str(block.get('displayname') or ''),
                                                           model=model), encoding='utf-8')
            resources.append({'key': key, 'asset_id': asset['metadata']['asset_id'], 'resource_id': resource_id,
                              'module': f'{package}.resources.{resource_id}:make_{resource_id}', 'kind': facts['kind'],
                              **({'model': model} if model else {}),
                              'file': target.relative_to(stage).as_posix(), 'sha256': sha(target)})
        result = {'schema': 'unilab.asset-dependencies/1', 'package': package, 'dependencies': locks,
                  'requires': requires, 'generated_declarations': declarations, 'generated_resources': resources}
        verify_generated(stage, result, asset_root=cache)
        (stage / 'assets.lock.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        stage.rename(output)
    return {'status': 'passed', 'output': str(output), 'packages': len(rows),
            'materialized_behaviors': len(bindings), 'generated_devices': len(declarations),
            'generated_resources': len(resources),
            'execution': 'not_run'}
