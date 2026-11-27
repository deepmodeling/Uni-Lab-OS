"""显式执行受信任固定资产的离线回归；不连接硬件、不写项目/资产源包。

python -m tests.package_manager.asset_behavior.run_locked_batch --lock ... --source-cache ...
  --project ... --output <新目录> --execute-behavior
输出分别记录冷拉取、固化、原有 device/action 与配置不变量、审阅设备运动及未绑定项。
"""
from __future__ import annotations

import argparse
import gc
import importlib
import json
import math
import sys
from pathlib import Path

from unilabos.package_manager.package_distribution.asset_behavior import (
    build_locked,
    check_bindings,
    contract_digest,
    resolve_cached,
    write_forwarder,
)
from unilabos.package_manager.package_distribution.asset_fetch import fetch_lock
from unilabos.package_manager.package_distribution.asset_fetch.native_package import (
    canonical,
    inside,
    read_json,
    sha256_file,
)
from unilabos.registry.ast_registry_scanner import _parse_file


class UnqualifiedMotion(ValueError):
    """行程没有足够依据，保留 incomplete 而不是虚构可用范围。"""


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')


def snapshot(project: Path, owners: set) -> dict:
    contracts, source_hashes = {}, {}
    # 只读取显式项目；不导入厂家驱动。
    for source in project.rglob('*.py'):
        if any(p.startswith('.') or p in ('__pycache__', 'tests') for p in source.relative_to(project).parts):
            continue
        definitions, _ = _parse_file(source, project)
        # 场景锁可能指向仅显示用的 owner；因此冻结领域包全部设备合同，不能只看场景模型类。
        selected = definitions
        if selected:
            source_hashes[source.relative_to(project).as_posix()] = sha256_file(source)
        for device in selected:
            key = device['device_id'] + '@' + source.relative_to(project).as_posix()
            contracts[key] = {'digest': contract_digest(device), 'actions': sorted(device.get('actions', {})),
                              'asset_owner': device['device_id'] in owners,
                              'class_name': device['class_name']}
    config = project / 'peptide/config'
    preserved = {p.relative_to(project).as_posix(): sha256_file(p) for p in config.rglob('*.json')}
    return {'contracts': contracts, 'definition_files': source_hashes, 'configuration_files': preserved}


def exercise(package_root: Path, meta: dict, source: Path, module: str, symbol: str, review_dir: Path, token: str):
    """审阅命名空间转发到真实封存行为，观测 USD 矩阵而不是只看返回值。"""
    from pxr import Gf, Usd, UsdGeom, UsdPhysics
    target = review_dir / ('review_' + token + '.py')
    write_forwarder(target, source, module, symbol, package=review_dir,
                    declaration={'id': 'asset_review_' + token, 'category': ['offline_review']})
    definitions, _ = _parse_file(target, review_dir.parent)
    assert len(definitions) == 1
    controller_class = getattr(importlib.import_module(review_dir.name + '.' + target.stem), symbol)
    stage = Usd.Stage.CreateInMemory()
    UsdGeom.SetStageMetersPerUnit(stage, 1)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    entry = package_root / meta['entrypoints']['usd']
    for name, offset in [('A', (0.12, 0.23, 0.34)), ('B', (1.12, -0.23, 0.54))]:
        root = UsdGeom.Xform.Define(stage, '/' + name)
        root.GetPrim().GetReferences().AddReference(str(entry))
        root.AddTransformOp(opSuffix='reviewInstallation').Set(
            Gf.Matrix4d(1).SetRotate(Gf.Rotation(Gf.Vec3d(0, 0, 1), 31)) * Gf.Matrix4d(1).SetTranslate(Gf.Vec3d(*offset)))
    stage.SetEditTarget(stage.GetSessionLayer())

    def matrices(usd_stage, root):
        cache = UsdGeom.XformCache()
        return {str(p.GetPath()): tuple(cache.GetLocalToWorldTransform(p)[i][j] for i in range(4) for j in range(4))
                for p in Usd.PrimRange(usd_stage.GetPrimAtPath(root)) if p.IsA(UsdGeom.Xformable)}

    before_b = matrices(stage, '/B')
    installation = matrices(stage, '/A')['/A']
    if symbol == 'AnqianDevice':
        implementation_module = importlib.import_module(module)
        # 资产已有 UsdMotionPort 指向模组根；设备总成引用该根。
        platforms = [p for p in Usd.PrimRange(stage.GetPrimAtPath('/A')) if p.GetName() == 'Platform'
                     and p.GetParent().GetName() == 'OrbitX']
        assert platforms, '未找到资产定义的平台'
        base = str(platforms[0].GetParent().GetParent().GetPath())
        port = implementation_module.UsdMotionPort(stage, base)
        controller = controller_class(implementation_module.AnqianModule(write_translation=port.write, read_translation=port.read))
        before = matrices(stage, '/A')
        controller.start_shaking(rpm=600, amplitude_peak_to_peak_mm=1, duration_s=2)
        controller.advance(.175)
        assert matrices(stage, '/A') != before
        controller.stop_shaking()
        joints_tested = 2
    elif symbol == 'JointMotion':
        controller = controller_class(stage, '/A')
        controller.reset()
        before = matrices(stage, '/A')
        positions = {}
        unbounded = []
        for name, (p, _, _) in controller.joints.items():
            low = float(p.GetAttribute('physics:lowerLimit').Get())
            high = float(p.GetAttribute('physics:upperLimit').Get())
            if not math.isfinite(low) or not math.isfinite(high):
                unbounded.append(name)
                continue
            value = high * .17 if high > 0 else low * .17
            positions[name] = math.radians(value) if p.IsA(UsdPhysics.RevoluteJoint) else value
        if unbounded:
            raise UnqualifiedMotion('非有限行程，未执行猜测目标：' + ', '.join(unbounded))
        controller.set_positions(positions)
        assert matrices(stage, '/A') != before, '无非零实际运动'
        stable = matrices(stage, '/A')
        try:
            controller.set_positions({next(iter(positions)): float('nan')})
        except (ValueError, TypeError): pass
        else: raise AssertionError('NaN 未被拒绝')
        assert matrices(stage, '/A') == stable, '拒绝输入后场景变化'
        controller.reset()
        joints_tested = len(positions)
    elif symbol == 'JointBehavior':
        controller = controller_class(stage, '/A')
        controller.home()
        before = matrices(stage, '/A')
        targets = {}
        for name, row in controller.rows.items():
            limits = row['limits']
            if limits is None: continue
            value = limits[1] * .17 if limits[1] > 0 else limits[0] * .17
            if value: targets[name] = value
        if not targets: raise ValueError('没有已确认的非零行程')
        controller.command(**targets)
        assert matrices(stage, '/A') != before
        controller.home()
        joints_tested = len(targets)
    else:
        raise ValueError('需要该行为的专用执行适配：' + symbol)
    after = matrices(stage, '/A')
    assert matrices(stage, '/B') == before_b, '第二实例被影响'
    assert after['/A'] == installation, '安装矩阵改变'
    assert all(max(abs(x-y) for x,y in zip(after[k], v)) < 1e-7 for k,v in before.items()), '复位未还原实际位姿'
    del controller, stage
    gc.collect()
    return {'status': 'passed', 'joints_tested': joints_tested, 'instance_isolation': 'passed',
            'installation_transform': 'passed', 'reset': 'passed',
            'registered_review_methods': sorted(set(definitions[0].get('actions', {})) | set(definitions[0].get('auto_methods', {}))),
            'qualification': 'offline_review_facade_only; not existing station device/action or physics'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ('lock', 'source-cache', 'project', 'output'):
        parser.add_argument('--' + key, required=True, type=Path)
    parser.add_argument('--execute-behavior', action='store_true')
    args = parser.parse_args()
    if args.output.exists(): raise ValueError('输出目录已存在')
    rows, assets = resolve_cached(args.lock, args.source_cache)
    args.output.mkdir(parents=True)
    owners = {r.get('owner_registry_id') for r in rows}
    before = snapshot(args.project, owners)
    save(args.output / 'before.json', before)
    # 镜像索引只投影已校验的固定缓存；原缓存与已发布索引不写入。
    class Mirror:
        def get(self, key):
            if key == 'library.json':
                return canonical({'packages': [{**row, 'path': asset['package_path']}
                    for row, asset in zip(rows, assets.values())]})
            return inside(args.source_cache, key).read_bytes()
    cold = args.output / 'cold-cache'
    fetched = fetch_lock(Mirror(), args.lock, cold)
    save(args.output / 'fetch.json', fetched)
    built = args.output / 'built'
    build = build_locked(args.lock, cold, built, 'asset_batch_build')
    save(args.output / 'build.json', build)
    check = check_bindings(args.lock, cold, args.project, scan_file=_parse_file)
    save(args.output / 'bindings.json', check)
    generated = read_json(built / 'assets.lock.json')
    sys.path.insert(0, str(built))
    review_dir = built / 'review_devices'
    review_dir.mkdir()
    (review_dir / '__init__.py').write_text('', encoding='utf-8')
    runtime = []
    for row, asset, generated_row in zip(rows, assets.values(), generated['dependencies']):
        result = {'key': row.get('key'), 'status': 'model_only'}
        spec = asset['metadata'].get('behavior_binding') or {}
        if spec.get('kind') == 'asset_python':
            if not args.execute_behavior:
                result['status'] = 'not_run'
            else:
                try:
                    module, symbol = spec['implementation'].split(':')
                    token = 'a_' + row['asset_id'].replace('-', '_') + '_' + row['package_digest'][:12]
                    module = 'asset_batch_build._asset_code.' + token + '.' + module
                    result.update(exercise(cold / asset['package_path'], asset['metadata'],
                        built / (module.replace('.', '/') + '.py'), module, symbol, review_dir, token))
                except UnqualifiedMotion as error:
                    result.update(status='incomplete', reason=str(error))
                except Exception as error:
                    result.update(status='failed', error=str(error))
        runtime.append(result)
        save(args.output / 'runtime.json', runtime)
        print(result['key'], result['status'], result.get('error', ''), flush=True)
    resolve_cached(args.lock, cold)  # 执行后再次验证所有封存字节，不能污染缓存。
    after = snapshot(args.project, owners)
    save(args.output / 'after.json', after)
    invariant = before == after
    report = {'packages': len(rows), 'cold_fetch': fetched['status'], 'build': build,
              'device_action_and_installation_invariants': 'passed' if invariant else 'failed',
              'existing_station_bindings': check['status'],
              'runtime_counts': {s: sum(r['status']==s for r in runtime) for s in sorted({r['status'] for r in runtime})}}
    save(args.output / 'report.json', report)
    print(json.dumps(report, ensure_ascii=False))
    if not invariant or any(r['status']=='failed' for r in runtime): return 1
    return 2 if check['status'] != 'passed' or any(r['status'] in ('incomplete', 'not_run') for r in runtime) else 0


if __name__ == '__main__':
    raise SystemExit(main())
