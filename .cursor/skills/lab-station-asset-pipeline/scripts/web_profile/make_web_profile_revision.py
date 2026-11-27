"""Make a front-end lightweight ("web") profile for asset-library packages, as new revisions.

For every source package: copy its simready_usd as a new revision (suffix, e.g. ``__R193web``), keep the
spatial entry byte-identical apart from the root file name, and add

    simready_usd/profiles/web/usd/...         same layer tree, meshes simplified (meshoptimizer), textures capped
    simready_usd/profiles/web/web.json        generator settings, report and checks
    simready_usd/profiles/web/dependency_closure.json
    unilab.json entrypoints.web_profile       -> profiles/web/usd/<root>
    unilab.json parameters.web_profile        lineage: source_library_path + source_package_digest, spatial_entry_unchanged

then re-seal it with the library's own native packager. Gate (the revision is not written unless all pass):
prim paths identical to the source, UsdPhysics joints identical, bounds within max(1 mm, 2·rel·diagonal),
both dependency closures complete and inside their package / profile folder, spatial geometry facts unchanged.

Publishing (admission, immutable upload, cold download check, index commit) is not done here; use the
library's publisher with the rows this script writes to ``<out>/built_assets.json``.

    python make_web_profile_revision.py --library-tools <dir with admit_reviewed_asset_batch.py> \
        --package "04_设备/unilab/X__r001" [--package ...] --suffix __R193web --revision R193 --out <work dir> \
        [--rel 1e-3] [--texmax 1024] [--min-triangles 200] [--meshopt-from <node_modules dir with meshoptimizer>]

Needs pxr, numpy, Pillow, node and the meshoptimizer npm package (MESHOPT_FROM or --meshopt-from).
"""
import argparse, json, os, re, shutil, sys, time, traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import webprofile as wp  # noqa: E402
from pxr import Usd, UsdGeom, UsdPhysics, UsdUtils  # noqa: E402

PROFILE = 'simready_usd/profiles/web'
IGNORE = shutil.ignore_patterns('__pycache__', '*.pyc')


def prim_paths(stage):
    return sorted(str(p.GetPath()) for p in stage.Traverse(Usd.TraverseInstanceProxies()))


def joints(stage):
    out = []
    for p in stage.Traverse(Usd.TraverseInstanceProxies()):
        if p.IsA(UsdPhysics.Joint):
            j = UsdPhysics.Joint(p)
            out.append((str(p.GetPath()), str(p.GetTypeName()), [str(t) for t in j.GetBody0Rel().GetTargets()], [str(t) for t in j.GetBody1Rel().GetTargets()]))
    return sorted(out)


def bounds(stage):
    r = UsdGeom.BBoxCache(Usd.TimeCode.Default(), ['default', 'render']).ComputeWorldBound(stage.GetPseudoRoot()).ComputeAlignedRange()
    return [list(r.GetMin()), list(r.GetMax())]


def triangles(stage):
    n = 0
    for p in stage.Traverse(Usd.TraverseInstanceProxies()):
        if p.IsA(UsdGeom.Mesh):
            n += sum(max(0, k - 2) for k in (UsdGeom.Mesh(p).GetFaceVertexCountsAttr().Get() or []))
    return n


def closure_inside(usd, root):
    layers, assets, missing = UsdUtils.ComputeAllDependencies(str(usd))
    root = Path(root).resolve()
    outside = [l.realPath for l in layers if not Path(l.realPath).resolve().is_relative_to(root)]
    outside += [a for a in assets if not Path(a).resolve().is_relative_to(root)]
    return layers, assets, list(missing), outside


def build(b, rel_path, a):
    src = b.LIB / rel_path
    entry = next(p for p in b.read(b.LIB / 'library.json')['packages'] if p['path'] == rel_path)
    b.native.verify(src, entry['package_digest'])
    body = b.read(src / b.native.ADAPTER)
    if body['entrypoints'].get('web_profile'):
        raise ValueError('source already has a web profile: ' + rel_path)
    dest = src.parent / (re.sub(r'__.*$', '', src.name) + a.suffix)
    if (dest / b.native.MANIFEST).exists():
        raise FileExistsError('already sealed: ' + str(dest))
    if dest.exists():
        shutil.rmtree(dest)
    old_rel = body['entrypoints']['usd']
    shutil.copytree(src / 'simready_usd', dest / 'simready_usd', ignore=IGNORE)
    new_name = dest.name + Path(old_rel).suffix
    new_rel = 'simready_usd/' + new_name
    os.replace(dest / old_rel, dest / new_rel)
    usd = dest / new_rel
    started = time.time()
    report = wp.build_profile(str(dest / 'simready_usd'), str(dest / PROFILE / 'usd'), rel=a.rel, texmax=a.texmax,
                              min_triangles=a.min_triangles, meshopt_from=a.meshopt_from)
    web_usd = dest / PROFILE / 'usd' / new_name
    src_stage, web_stage = Usd.Stage.Open(str(src / old_rel)), Usd.Stage.Open(str(web_usd))
    assert b.geometry_facts(usd) == b.geometry_facts(src / old_rel), 'spatial entry geometry changed'
    b0, b1 = bounds(src_stage), bounds(web_stage)
    diag = sum((b0[1][i] - b0[0][i]) ** 2 for i in range(3)) ** 0.5
    _, _, missing_main, outside_main = closure_inside(usd, dest)
    layers, assets, missing_web, outside_web = closure_inside(web_usd, dest / PROFILE / 'usd')
    checks = {'prim_paths_identical': prim_paths(web_stage) == prim_paths(src_stage), 'joints_identical': joints(web_stage) == joints(src_stage),
              'bounds_max_error_m': max(abs(b0[k][i] - b1[k][i]) for k in range(2) for i in range(3)),
              'bounds_tolerance_m': max(1e-3, 2 * a.rel * diag),
              'main_closure_missing': missing_main, 'main_closure_outside': outside_main,
              'web_closure_missing': missing_web, 'web_closure_outside': outside_web,
              'triangles_main': triangles(src_stage), 'triangles_web': triangles(web_stage)}
    ok = (checks['prim_paths_identical'] and checks['joints_identical'] and checks['bounds_max_error_m'] <= checks['bounds_tolerance_m']
          and not (missing_main or outside_main or missing_web or outside_web))
    checks['status'] = 'passed' if ok else 'failed'
    if not ok:
        shutil.rmtree(dest)
        raise AssertionError(json.dumps(checks, ensure_ascii=False)[:2000])
    b.write(dest / PROFILE / 'dependency_closure.json', {
        'usd_sha256': b.native.digest(web_usd), 'unresolved': [], 'self_contained_usd': True,
        'layers': sorted(Path(l.realPath).resolve().relative_to(dest.resolve()).as_posix() for l in layers),
        'asset_paths': sorted(Path(x).resolve().relative_to(dest.resolve()).as_posix() for x in assets)})
    b.write(dest / PROFILE / 'web.json', {
        'id': 'web', 'purpose': 'front-end lightweight display (three.js USD loader); not for physics or measurement',
        'usd': 'usd/' + new_name, 'source_spatial_entry': new_rel, 'source_package_digest': entry['package_digest'],
        'generator': {'tool': 'make_web_profile_revision.py / webprofile.py + meshoptimizer simplifyWithAttributes',
                      'flags': ['Sparse', 'Prune', 'Permissive'], 'relative_error': a.rel, 'error_reference': 'per-mesh extent',
                      'min_triangles': a.min_triangles, 'texture_max_px': a.texmax, 'texture_resample': 'PIL LANCZOS'},
        'preserved': ['prim hierarchy and names', 'UsdPhysics joints and limits', 'xform ops', 'material bindings', 'GeomSubset membership'],
        'report': {**report, 'seconds': round(time.time() - started, 1)}, 'checks': checks})
    body = json.loads(json.dumps(body, ensure_ascii=False).replace(old_rel, new_rel))
    body['entrypoints']['usd'] = new_rel
    body['entrypoints']['web_profile'] = PROFILE + '/usd/' + new_name
    body['revision'] = a.revision
    body.setdefault('parameters', {})['web_profile'] = {
        'profile': PROFILE + '/web.json', 'source_library_path': rel_path, 'source_package_digest': entry['package_digest'],
        'spatial_entry_unchanged': True, 'triangles_spatial': checks['triangles_main'], 'triangles_web': checks['triangles_web']}
    body['usd_dependencies'] = b.usd_closure(usd, dest)
    readiness = b.read(dest / b.native.READINESS)
    readiness.setdefault('binding', {}).update(source_usd_sha256=b.native.digest(usd), spatial_path=new_rel,
                                               usd_closure_sha256=b.native.sha(b.native.canonical(body['usd_dependencies'])))
    readiness.setdefault('checks', {})['web_profile'] = 'passed'
    b.write(dest / b.native.READINESS, readiness)
    verified = b.native.finalize(dest, body)
    return {'asset_id': entry['asset_id'], 'name': body.get('title') or dest.name, 'key': dest.name, 'package': str(dest), 'usd': str(usd),
            'web_usd': str(web_usd), 'target': b.target(dest), 'geometry': b.geometry_facts(usd), 'note': 'web profile added; spatial entry unchanged',
            'source': {'library_path': rel_path, 'package_digest': entry['package_digest']},
            'validation': {'status': 'passed', 'package': verified, 'web': checks}}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--library-tools', required=True, help='directory with the library tooling (admit_reviewed_asset_batch.py)')
    ap.add_argument('--package', action='append', required=True, help='library-relative package path; repeat')
    ap.add_argument('--suffix', required=True, help='new revision directory suffix, e.g. __R193web')
    ap.add_argument('--revision', required=True, help='revision label written into unilab.json')
    ap.add_argument('--out', type=Path, required=True, help='work directory for built_assets.json / build_failures.json')
    ap.add_argument('--rel', type=float, default=1e-3, help='simplification error relative to each mesh extent')
    ap.add_argument('--texmax', type=int, default=1024)
    ap.add_argument('--min-triangles', type=int, default=200)
    ap.add_argument('--meshopt-from', default=os.environ.get('MESHOPT_FROM'), help='node_modules directory that contains meshoptimizer')
    a = ap.parse_args()
    if not a.meshopt_from:
        ap.error('--meshopt-from or MESHOPT_FROM is required')
    sys.path.insert(0, a.library_tools)
    import admit_reviewed_asset_batch as b
    a.out.mkdir(parents=True, exist_ok=True)
    built, failures = [], []
    for rel_path in a.package:
        try:
            row = build(b, rel_path, a); built.append(row)
            w = row['validation']['web']
            print('BUILT', row['key'], w['triangles_main'], '->', w['triangles_web'], row['target']['package_digest'][:12], flush=True)
        except Exception as exc:
            failures.append({'package': rel_path, 'error': str(exc)[:2000], 'trace': traceback.format_exc()[-3000:]})
            print('FAILED', rel_path, str(exc)[:300], flush=True)
    b.write(a.out / 'built_assets.json', built); b.write(a.out / 'build_failures.json', failures)
    print('DONE built', len(built), 'failed', len(failures))
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
