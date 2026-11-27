"""构建期固化资产源码；按原 BOM 来源校验，不导入、不执行资产代码，也不做插件发现。

behavior_binding 的取值按资产库现有合同逐类处理，未知形态一律失败，不静默跳过：
- 无 behavior_binding，或没有 kind 且 implementation 为 null（库内 geometry_only 等）：无行为；
- kind=asset_python：原样复制声明文件到 <package>/_asset_code/<token>/ 并改写入口；
- kind=asset_reference：行为由另一个资产提供，被引用 owner 必须同时选中并锁定。
"""
import ast
import json
from pathlib import Path
from shutil import copyfile

from .paths import inside, sha

BEHAVIOR_KINDS = ('asset_python', 'asset_reference')


def behavior_kind(spec):
    """返回 None（无行为）或已支持的 kind；其余形态抛 ValueError。"""
    if not spec:
        return None
    if not isinstance(spec, dict):
        raise ValueError('behavior_binding must be an object')
    kind = spec.get('kind')
    if kind is None:
        if spec.get('implementation') is None:
            return None
        raise ValueError('behavior_binding has an implementation but no kind')
    if kind not in BEHAVIOR_KINDS:
        raise ValueError('Unsupported behavior_binding kind: ' + repr(kind))
    return kind


def _registers(node):
    return isinstance(node, ast.ClassDef) and any(
        isinstance(d, ast.Call) and (
            isinstance(d.func, ast.Name) and d.func.id in ('device', 'resource') or
            isinstance(d.func, ast.Attribute) and d.func.attr in ('device', 'resource'))
        for d in node.decorator_list)


def materialize(root, package, assets, locks, *, asset_root=None):
    """原样复制显式源文件，返回 {资产键: 改写后的 binding} 与合并后的 requires。

    参数：root 为输出工作区根；package 为领域包导入名；assets 为 {键: {package_path, metadata}}，
    metadata 即 unilab.json；locks 为 assets.lock 依赖行（会就地写入 generated_code）。
    """
    root = Path(root).resolve()
    asset_root = Path(asset_root) if asset_root is not None else root
    if not package or not all(part.isidentifier() for part in package.split('.')):
        raise ValueError('Invalid import package name')
    results, requirements = {}, set()
    selected_ids = {d['asset_id'] for d in locks}
    kinds = {key: behavior_kind(asset['metadata'].get('behavior_binding')) for key, asset in assets.items()}
    for key, asset in assets.items():
        if kinds[key] == 'asset_reference':
            ref = asset['metadata']['behavior_binding']
            if ref.get('asset_id') not in selected_ids:
                raise ValueError('Referenced behavior owner must be selected and pinned: ' + str(ref.get('asset_id')))
    for key, asset in assets.items():
        if kinds[key] != 'asset_python':
            continue
        spec = asset['metadata']['behavior_binding']
        dependency = next((d for d in locks if d['path'] == asset['package_path']), None)
        if dependency is None:
            raise ValueError('Asset behavior has no lock entry: ' + asset['package_path'])
        token = 'a_' + dependency['asset_id'].replace('-', '_') + '_' + dependency['package_digest'][:12]
        relative = f'{package}/_asset_code/{token}'
        source_root = inside(asset_root, asset['package_path'] + '/' + spec['source_root'])
        target_root = inside(root, relative)
        target_root.mkdir(parents=True, exist_ok=True)
        (target_root.parent / '__init__.py').write_text('', encoding='utf8')
        declared = spec['files']
        if not declared or len(declared) != len(set(declared)):
            raise ValueError('Asset code requires nonempty unique files')
        bom = json.loads((asset_root / asset['package_path'] / '.metadata/com.nvidia.simready.packaging.bom.json').read_text('utf8'))
        sealed = {r['relative_path']: r['hash']['sha256'] for r in bom['items']}
        copied = []
        for path in declared:
            source = inside(source_root, path)
            if source.suffix != '.py':
                raise ValueError('Only explicit Python modules may be materialized')
            tree = ast.parse(source.read_text('utf8'))
            if any(_registers(node) for node in ast.walk(tree)):
                raise ValueError('Asset implementation cannot register device/resource classes')
            source_rel = spec['source_root'] + '/' + path
            digest = sha(source)
            if sealed.get(source_rel) != digest:
                raise ValueError('Unsealed behavior source: ' + source_rel)
            target = inside(target_root, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            copyfile(source, target)
            copied.append({'source': source_rel, 'path': target.relative_to(root).as_posix(), 'sha256': digest})
        module = package + '._asset_code.' + token

        def entry(value):
            mod, symbol = value.split(':')
            if not all(x.isidentifier() for x in [*mod.split('.'), symbol]) or mod.replace('.', '/') + '.py' not in declared:
                raise ValueError('Behavior entry is not a declared module')
            return module + '.' + mod + ':' + symbol

        binding = {**spec, 'module_root': module, 'code_path': relative}
        for field in ('implementation', 'runtime', 'rejected'):
            if field in spec:
                binding[field] = entry(spec[field])
        if 'exports' in spec:
            binding['exports'] = {}
            for role, export in spec['exports'].items():
                if not role.isidentifier():
                    raise ValueError('Invalid behavior export name')
                # 已发布的纯机构包允许 role → module:Class；不为其猜测 @device 声明。
                export = {'implementation': export} if isinstance(export, str) else export
                bound = {**export, 'implementation': entry(export['implementation'])}
                # 模型 provider 必须是同一封存资产里的显式符号。
                declaration = dict(export.get('declaration') or {})
                if export.get('model_provider'):
                    declaration['model'] = {**declaration['model'], 'provider': entry(export['model_provider'])}
                bound['declaration'] = declaration
                binding['exports'][role] = bound
        if not binding.get('exports') and 'implementation' not in binding:
            raise ValueError('Asset behavior has no implementation exports')
        dependency['generated_code'] = copied
        results[key] = binding
        requirements.update(spec.get('requires', []))
    return results, sorted(requirements)


def verify_generated(root, lock, *, asset_root=None):
    """校验派生源码与锁定资产原件字节相同；_asset_code 下不得有未声明的模块。"""
    root = Path(root).resolve()
    asset_root = Path(asset_root).resolve() if asset_root is not None else root
    allowed = set()
    for dep in lock['dependencies']:
        for row in dep.get('generated_code', []):
            target = inside(root, row['path'])
            source = inside(asset_root, dep['path'] + '/' + row['source'])
            if sha(target) != row['sha256'] or sha(source) != row['sha256']:
                raise ValueError('Generated asset behavior differs: ' + row['path'])
            allowed.add(target)
    for folder in {p for p in root.glob('*/_asset_code')}:
        if (folder / '__init__.py').read_bytes() != b'':
            raise ValueError('Generated module root changed')
        actual = {p.resolve() for p in folder.rglob('*.py') if p != folder / '__init__.py'}
        if actual != {p for p in allowed if p.is_relative_to(folder)}:
            raise ValueError('Unexpected generated behavior module')
    for row in lock.get('generated_resources', []):
        if sha(inside(root, row['file'])) != row['sha256']:
            raise ValueError('Generated material definition differs: ' + row['file'])
    for row in lock.get('generated_declarations', []):
        module = row['project_class'].split(':')[0]
        target = inside(root, module.replace('.', '/') + '.py')
        if sha(target) != row['generated_sha256']:
            raise ValueError('Generated device declaration differs: ' + module)
