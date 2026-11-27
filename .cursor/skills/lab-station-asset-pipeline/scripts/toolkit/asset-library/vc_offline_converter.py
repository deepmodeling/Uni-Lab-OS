"""VCMX -> source-derived USD without VC, embedded Python, or runtime captures.

Unknown features, missing units and runtime-generated products remain explicit
gaps. This is static configuration reconstruction, not native simulation parity.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

sys.dont_write_bytecode = True


def write(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf8')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def guard(source, output, libraries):
    """Fail closed on process/network/VC DLL calls or undeclared data reads."""
    exact = {Path(p).resolve() for p in [source, *libraries]}
    roots = [Path(__file__).resolve().parent, Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(), output]
    reads = set(); blocked = []
    def reject(event, detail):
        blocked.append({'event': event, 'detail': str(detail)})
        raise PermissionError('Offline conversion policy: '+event+' '+str(detail))
    def hook(event, args):
        if event.startswith('socket.') or event in ('subprocess.Popen', 'os.system', 'os.exec', 'os.spawn',
                'os.posix_spawn', 'os.fork', 'os.forkpty', 'os.startfile', 'os.startfile/2'):
            reject(event, 'process/network execution is forbidden')
        if event == 'ctypes.dlopen' and args[0] and any(s in str(args[0]).lower().replace(' ', '')
                for s in ('visualcomponents', 'revolution.dll')):
            reject(event, args[0])
        if event != 'open' or not isinstance(args[0], (str, bytes, os.PathLike)):
            return
        p = Path(os.fsdecode(args[0])).resolve()
        mode = args[1]; flags = args[2] if len(args) > 2 else 0
        writing = (isinstance(mode, str) and any(v in mode for v in 'wax+')) or bool(flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC))
        if writing:
            if not p.is_relative_to(output): reject(event, 'write outside output: '+str(p))
        else:
            if p not in exact and not any(p.is_relative_to(r) for r in roots):
                reject(event, 'undeclared input: '+str(p))
            reads.add(str(p))
    sys.addaudithook(hook)
    return {'reads': reads, 'blocked': blocked}


def assemble(geometry, physics, libraries, output, mass_report=None, binary_usd=False):
    import numpy as np
    from pxr import Gf, Sdf, Tf, Usd, UsdGeom, UsdShade, UsdPhysics, Vt
    from vc_offline_materials import resolve
    from vc_offline_kinematics import normalize, validate_poses, apply_kinematics, validate_usd_frames
    usd = output/('usd/asset.usdc' if binary_usd else 'usd/asset.usda'); usd.parent.mkdir()
    stage = Usd.Stage.CreateNew(str(usd)); root = UsdGeom.Xform.Define(stage, '/Asset')
    stage.SetDefaultPrim(root.GetPrim()); UsdGeom.SetStageMetersPerUnit(stage, 1.0); UsdGeom.SetStageUpAxis(stage, 'Z')
    root.GetPrim().SetCustomData({'source_sha256': geometry['source']['archive_sha256'],
        'conversion': 'VCMX offline static source reconstruction', 'native_equivalence': 'not_validated',
        'physics_scope': 'source declarations only; no final mass/inertia or runtime products inferred'})
    paths = {}; material_map = {}; material_rows = []; mesh_rows = []; omissions = []
    raw_file = output/'evidence/geometry/raw-meshes.json'
    raw_meshes = {r['id']: r for r in json.loads(raw_file.read_text(encoding='utf8'))['meshes']} if raw_file.exists() else {}
    def converted_matrix(value):
        m = np.asarray(value, dtype=float).copy()
        if m.shape != (4,4) or not np.isfinite(m).all() or not np.allclose(m[:,3],[0,0,0,1]):
            raise ValueError('Invalid row-vector source matrix')
        m[3,:3] *= .001
        return Gf.Matrix4d(m.tolist())
    for n in geometry['nodes']:
        parent = paths[n['parent_id']] if n['parent_id'] else '/Asset'
        path = parent+'/N_'+n['id'].split(':')[-1]; paths[n['id']] = path
        xf = UsdGeom.Xform.Define(stage, path); xf.AddTransformOp().Set(converted_matrix(n['local_matrix_source']))
        xf.GetPrim().SetDisplayName(n['name'])
        xf.GetPrim().SetCustomData({'source_node_id': n['id'], 'source_name': n['name'],
                                   'source_line': n['evidence']['line_start']})
        if n.get('visible') is False: xf.CreateVisibilityAttr('invisible')
    def material(name):
        key = str(name)
        if key in material_map: return material_map[key]
        row = resolve(name, geometry['materials'], libraries)
        if row['status'] == 'unresolved':
            material_rows.append(row); material_map[key] = None; return None
        path = '/Asset/Materials/M_'+str(len(material_map)); mat = UsdShade.Material.Define(stage, path)
        shader = UsdShade.Shader.Define(stage, path+'/Shader'); shader.CreateIdAttr('UsdPreviewSurface')
        for field, value in row['usd_preview_inputs'].items():
            if field == 'diffuseColor': shader.CreateInput(field, Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*value))
            else: shader.CreateInput(field, Sdf.ValueTypeNames.Float).Set(float(value))
        mat.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), 'surface')
        mat.GetPrim().SetCustomData({'source_name': str(name), 'provenance_json': json.dumps(row, ensure_ascii=False)})
        material_map[key] = mat; material_rows.append(dict(row, usd_path=path)); return mat
    for item in geometry['meshes']:
        p = np.asarray(item['points_mm'], dtype=float); f = np.asarray(item['triangles'], dtype=int)
        if not len(f):
            omissions.append({'source_mesh_id': item['id'], 'feature_id': item['feature_id'],
                              'status': 'serialized_configuration_empty_geometry',
                              'reason': 'no source-derived faces; no substitute solid or collider created'})
            continue
        if p.ndim != 2 or p.shape[1] != 3 or not len(p) or f.ndim != 2 or f.shape[1] != 3:
            raise ValueError('Invalid offline mesh array')
        if not np.isfinite(p).all() or f.min() < 0 or f.max() >= len(p): raise ValueError('Invalid mesh values')
        matrix = np.asarray(item['local_to_node_matrix'], dtype=float)
        converted_matrix(matrix)  # validate the affine contract before baking
        p = (np.c_[p, np.ones(len(p))] @ matrix)[:,:3]*.001
        # Bake feature transforms into node-local geometry, then correct reflected
        # winding. Do not also retain a negative scale on the USD mesh transform.
        if item['winding_flip_required']: f = f[:, [0,2,1]]
        path = paths[item['node_id']]+'/Mesh_'+item['id'].split(':')[-1]
        mesh = UsdGeom.Mesh.Define(stage, path)
        mesh.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(p.astype(np.float32)))
        mesh.CreateFaceVertexCountsAttr([3]*len(f)); mesh.CreateFaceVertexIndicesAttr(f.flatten().tolist())
        mesh.CreateSubdivisionSchemeAttr('none'); mesh.CreateExtentAttr([Gf.Vec3f(*p.min(0)), Gf.Vec3f(*p.max(0))])
        uv_status = 'not_present_in_source'
        raw = raw_meshes.get(item.get('raw_mesh_ref'), {})
        uv_sets = raw.get('uv_sets', [])
        if uv_sets:
            uv = np.asarray(uv_sets[0]['uv'], dtype=float)
            if len(uv_sets) == 1 and uv.size == 0:
                # A serialized 3DS mapping chunk may explicitly contain zero UVs.
                # Preserve that empty source table; it is not a failed decode.
                uv_status = 'not_present_in_source'
            elif len(uv_sets) == 1 and uv.shape == (len(p),2) and np.isfinite(uv).all():
                UsdGeom.PrimvarsAPI(mesh).CreatePrimvar('st', Sdf.ValueTypeNames.TexCoord2fArray, 'vertex').Set(Vt.Vec2fArray.FromNumpy(uv.astype(np.float32)))
                uv_status = 'source_vertex_uv_preserved'
            else:
                uv_status = 'decoder_gap_ambiguous_or_invalid_uv'
                omissions.append({'mesh': path, 'field': 'uv', 'reason': uv_status})
        normals = np.cross(p[f[:,1]]-p[f[:,0]], p[f[:,2]]-p[f[:,0]]); lengths = np.linalg.norm(normals, axis=1)
        degenerate = np.flatnonzero(lengths <= 1e-14).tolist()
        if not degenerate:
            mesh.CreateNormalsAttr(Vt.Vec3fArray.FromNumpy((normals/lengths[:,None]).astype(np.float32)))
            mesh.SetNormalsInterpolation('uniform')
        if not item['visible']: mesh.CreateVisibilityAttr('invisible')
        mesh.GetPrim().SetCustomData({'source_feature_id': item['feature_id'], 'source_mesh_id': item['id'],
            'source_evidence_json': json.dumps(item['evidence']), 'source_only': True,
            'baked_feature_matrix_json': json.dumps(matrix.tolist())})
        binding = item.get('material_id'); groups = item.get('raw_face_material_groups', [])
        if binding:
            mat = material(binding)
            if mat: UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(mat)
        elif groups:
            member = item['raw_mesh_ref'].rsplit('#',1)[0]; seen = set()
            for i, group in enumerate(groups):
                indices = group['face_indices']
                invalid=[v for v in indices if v < 0 or v >= len(f)]
                overlap=seen.intersection(indices)
                duplicate=len(indices)-len(set(indices))
                if invalid or overlap or duplicate:
                    raise ValueError('Invalid source face material group: '+json.dumps({'mesh':item['id'],'member':member,'group':i,
                        'byte_offset':group.get('byte_offset'),'face_count':len(f),'invalid_indices':invalid[:16],
                        'overlapping_indices':sorted(overlap)[:16],'duplicate_indices_count':duplicate,'repair':'withheld; no guessed reassignment'}))
                seen.update(indices); mat = material(member+'#material:'+group['material'])
                subset = UsdGeom.Subset.Define(stage, path+'/MaterialFaces_'+str(i))
                subset.CreateElementTypeAttr('face'); subset.CreateFamilyNameAttr('materialBind'); subset.CreateIndicesAttr(indices)
                if mat: UsdShade.MaterialBindingAPI.Apply(subset.GetPrim()).Bind(mat)
            if len(seen) != len(f): omissions.append({'mesh': path, 'field': 'material', 'unassigned_faces': len(f)-len(seen)})
        else: omissions.append({'mesh': path, 'field': 'material', 'reason': 'source binding not resolved'})
        mesh_rows.append({'path': path, 'mesh_id': item['id'], 'node_id': item['node_id'],
                          'vertices': len(p), 'triangles': len(f), 'degenerate_faces': degenerate,
                          'material_id': binding, 'uv_status': uv_status,
                          'source_point_sha256': hashlib.sha256(p.astype('<f8').tobytes()).hexdigest()})
    entity_rows = []
    for e in physics['entities']:
        plan = e['usd_integration_plan']; path = paths.get(plan['node_id'])
        if not path: raise ValueError('Physics owner absent from source node map')
        prim = stage.GetPrimAtPath(path)
        prim.SetCustomDataByKey('source_physics_json', json.dumps(e, ensure_ascii=False))
        if mass_report:
            candidates = [row for row in mass_report.get('entities', []) if row['owner_id'] == e['owner']['id']]
            if len(candidates) != 1:
                raise ValueError('Mass evidence must bind exactly one source entity')
            prim.SetCustomDataByKey('source_mass_analysis_json', json.dumps(candidates[0], ensure_ascii=False))
            cache = candidates[0].get('normalized_cache', {})
            for source_key, usd_name, value_type in (
                    ('mass_kg', 'cachedMassKg', Sdf.ValueTypeNames.Double),
                    ('density_kg_m3', 'cachedDensityKgM3', Sdf.ValueTypeNames.Double),
                    ('inertia_xyz_kg_m2', 'cachedInertiaXYZKgM2', Sdf.ValueTypeNames.Double3),
                    ('com_matrix_m', 'cachedCenterOfMassMatrixM', Sdf.ValueTypeNames.Matrix4d)):
                if source_key not in cache: continue
                value = cache[source_key]
                if source_key == 'inertia_xyz_kg_m2': value = Gf.Vec3d(*value)
                elif source_key == 'com_matrix_m': value = Gf.Matrix4d(value)
                attr = prim.CreateAttribute('unilab:sourceSI:'+usd_name, value_type, custom=True)
                attr.Set(value)
                attr.SetCustomDataByKey('scope', 'unit-normalized serialized cache; not final, recomputed, measured or MassAPI')
        for name, field in e['fields'].items():
            value = field.get('value'); kind = None
            if isinstance(value, bool): kind = Sdf.ValueTypeNames.Bool
            elif isinstance(value, (int,float)): kind = Sdf.ValueTypeNames.Double
            elif isinstance(value, str): kind = Sdf.ValueTypeNames.String
            elif isinstance(value, list) and len(value) in (4,16) and all(isinstance(v,(int,float)) for v in value):
                if len(value) == 4: kind = Sdf.ValueTypeNames.Double4; value = Gf.Vec4d(*value)
                else: kind = Sdf.ValueTypeNames.Matrix4d; value = Gf.Matrix4d(np.asarray(value).reshape(4,4).tolist())
            if kind:
                attr = prim.CreateAttribute('unilab:sourceRaw:'+Tf.MakeValidIdentifier(name), kind, custom=True)
                attr.Set(value); attr.SetCustomDataByKey('scope', 'raw serialized source value, not SI or evaluated runtime value')
                attr.SetCustomDataByKey('source_field_json', json.dumps(field, ensure_ascii=False))
        # Material coefficients have dimensionless source semantics. They are saved
        # unbound: the native collision shape-to-material relationship is unobserved.
        matpath = '/Asset/SourcePhysicsMaterials/E_'+e['owner']['id'].split(':')[-1]
        pm = UsdShade.Material.Define(stage, matpath); api = UsdPhysics.MaterialAPI.Apply(pm.GetPrim())
        for field, value in plan['material_dimensionless_candidates'].items():
            {'staticFriction': api.CreateStaticFrictionAttr, 'dynamicFriction': api.CreateDynamicFrictionAttr,
             'restitution': api.CreateRestitutionAttr}[field]().Set(value['value'])
        pm.GetPrim().SetCustomDataByKey('scope', 'serialized entity coefficients, not bound to an observed contact shape')
        entity_rows.append({'node_id': plan['node_id'], 'usd_path': path, 'owner': e['owner'],
                            'physics_material': matpath, 'mass_api_authored': False,
                            'rigidbody_api_authored': False, 'reason': plan['mass_properties_omitted_reasons']})
    for i, j in enumerate(physics['joints_and_drivers']):
        prim = UsdGeom.Scope.Define(stage, '/Asset/SourceJoints/J_'+str(i)).GetPrim()
        prim.SetCustomDataByKey('source_contract_json', json.dumps(j, ensure_ascii=False))
        # A VC controller Dof is not itself a PhysX constraint. Its exact axis,
        # bounds and expressions remain a reusable source contract.
        prim.SetCustomDataByKey('solver_constraint_authored', False)
    normalized = normalize(geometry, physics)
    poses = validate_poses(geometry, normalized)
    supported = normalized
    derived_joints = apply_kinematics(stage, supported, paths)
    # Give Blender a mapped transform for the organizational joint parent, rather
    # than an importer-created untracked Scope empty. This changes no joint frame.
    UsdGeom.Xform.Define(stage, '/Asset/SourceKinematics')
    write(output/'evidence/kinematics.json', normalized)
    write(output/'evidence/kinematic-poses.json', poses)
    write(output/'evidence/kinematic-usd-mapping.json', {'disabled_derived_joints': derived_joints,
          'mapping_gaps': [],
          'scope': 'source kinematic subset mapped as disabled USD constraints; not native runtime or dynamics'})
    stage.GetRootLayer().Save()
    reopened = Usd.Stage.Open(str(usd)); actual = {str(p.GetPath()): p for p in reopened.Traverse()}
    write(output/'evidence/source-to-usd-frames.json', validate_usd_frames(reopened, geometry, normalized, paths, derived_joints))
    for row in mesh_rows:
        m = UsdGeom.Mesh(actual[row['path']])
        if len(m.GetPointsAttr().Get()) != row['vertices'] or len(m.GetFaceVertexCountsAttr().Get()) != row['triangles']:
            raise ValueError('Reopened geometry differs')
    write(output/'evidence/usd-readback.json', {'status': 'passed', 'input_sha256': digest(usd),
          'nodes': paths, 'meshes': mesh_rows, 'physics_entities': entity_rows,
          'joint_contracts': len(physics['joints_and_drivers']), 'material_omissions': omissions,
          'scope': 'USD structural readback; not native geometry equivalence or dynamics'})
    write(output/'evidence/material-resolution.json', {'materials': material_rows, 'omissions': omissions})
    return usd, mesh_rows, material_rows, omissions


def convert(source, output, expected=None, material_libraries=(), physics_rules=None, compact=False):
    from vc_offline_geometry import evaluate_archive
    from vc_offline_physics import inspect
    from vc_offline_materials import load_catalog
    from vc_offline_mass import recompute
    source = Path(source).resolve(); output = Path(output).resolve(); output.mkdir(parents=True, exist_ok=False)
    import tempfile
    scratch = output/'scratch'; scratch.mkdir()
    tempfile.tempdir = str(scratch)
    os.environ['TMP'] = os.environ['TEMP'] = str(scratch)
    original_hash = digest(source)
    if expected and original_hash != expected: raise ValueError('Source hash differs')
    evaluate_archive(source, output/'evidence/geometry', original_hash)
    inspect(source, output/'evidence/physics', original_hash)
    # Consume the serialized module contracts, exactly as independent reviewers do.
    geometry = json.loads((output/'evidence/geometry/geometry.json').read_text(encoding='utf8'))
    physics = json.loads((output/'evidence/physics/physics-contract.json').read_text(encoding='utf8'))
    from vc_offline_dependencies import analyze
    write(output/'evidence/joint-dependencies.json',analyze(physics))
    rules = json.loads(Path(physics_rules).read_text(encoding='utf-8-sig')) if physics_rules else None
    mass_report = recompute(geometry, physics, rules)
    write(output/'evidence/mass-properties.json', mass_report)
    catalogs = [load_catalog(p) for p in material_libraries]
    (output/'sources').mkdir(); shutil.copyfile(source, output/'sources'/source.name)
    if physics_rules:
        shutil.copyfile(physics_rules, output/'sources/physics-rules.json')
    for i, catalog in enumerate(catalogs):
        dest = output/'sources'/('material-library-'+str(i)+'.xml'); shutil.copyfile(catalog['source'], dest)
        catalog['portable_source'] = str(dest.relative_to(output))
        for entry in catalog['entries'].values():
            entry['source'] = '../'+dest.relative_to(output).as_posix()
    usd, meshes, materials, omissions = assemble(geometry, physics, catalogs, output, mass_report, binary_usd=compact)
    for name in ('vc_offline_converter.py','vc_offline_geometry.py','vc_offline_expression.py',
                 'vc_offline_source.py','vc_offline_dof.py','vc_offline_dependencies.py','vc_offline_physics.py','vc_offline_materials.py','vc_offline_kinematics.py','vc_offline_mass.py',
                 'enrichment/vc_file_source.py'):
        src = Path(__file__).parent/name; dest = output/'evidence/code'/name
        dest.parent.mkdir(parents=True, exist_ok=True); shutil.copyfile(src, dest)
    gaps = [r for r in geometry['feature_coverage'] if r['status'] == 'decoder_gap']
    result = {'status': 'converted_static_with_explicit_gaps', 'source': str(source), 'source_sha256': original_hash,
        'usd': str(usd), 'usd_sha256': digest(usd), 'nodes': len(geometry['nodes']),
        'meshes': len(meshes), 'triangles': sum(m['triangles'] for m in meshes),
        'feature_count': len(geometry['feature_coverage']), 'feature_decoder_gaps': gaps,
        'archive_gaps': geometry.get('archive_gaps', []),
        'kinematic_decoder_gaps': json.loads((output/'evidence/kinematics.json').read_text(encoding='utf8'))['gaps'],
        'material_status_counts': {s: sum(m['status']==s for m in materials) for s in sorted({m['status'] for m in materials})},
        'source_entities': len(physics['entities']), 'source_joint_contracts': len(physics['joints_and_drivers']),
        'normalized_movable_joints': len(json.loads((output/'evidence/kinematics.json').read_text(encoding='utf8'))['joints']),
        'embedded_scripts_executed': False, 'historical_snapshot_inputs': False, 'vc_started': False,
        'native_equivalence': 'not_run', 'dynamics': 'not_run', 'mass_inertia_si_authored': False,
        'physics_rules': {'sha256': digest(physics_rules), 'portable_source': 'sources/physics-rules.json'} if physics_rules else None,
        'mass_analysis': {'evidence': 'evidence/mass-properties.json', 'status': mass_report.get('status'),
                          'scope': 'normalized serialized caches and bounded source calculations; no MassAPI or native dynamics claim'},
        'external_material_libraries': [{'path': c['source'], 'sha256': c['sha256'], 'portable_source': c['portable_source']} for c in catalogs]}
    if digest(source) != original_hash: raise ValueError('Source changed')
    if compact:
        import gzip
        compressed=[]
        # Generated evidence only; original archive and code snapshots stay intact.
        for path in sorted((output/'evidence').rglob('*.json')):
            if path.stat().st_size < 32768:continue
            raw=path.read_bytes();dest=path.with_suffix(path.suffix+'.gz')
            with dest.open('wb') as target:
                with gzip.GzipFile(filename='',mode='wb',fileobj=target,mtime=0,compresslevel=6) as stream:stream.write(raw)
            if gzip.decompress(dest.read_bytes())!=raw:raise ValueError('Evidence compression readback differs')
            compressed.append({'original':path.relative_to(output).as_posix(),'compressed':dest.relative_to(output).as_posix(),'uncompressed_sha256':hashlib.sha256(raw).hexdigest(),'compressed_sha256':digest(dest),'uncompressed_bytes':len(raw),'compressed_bytes':dest.stat().st_size})
            path.unlink()
        write(output/'evidence/compression.json',{'lossless':True,'entries':compressed})
        result['evidence_compression']='evidence/compression.json'
    write(output/'result.json', result); return result


def main():
    p = argparse.ArgumentParser(description=__doc__); p.add_argument('--source', required=True); p.add_argument('--output', required=True)
    p.add_argument('--expected-sha256'); p.add_argument('--material-library', action='append', default=[])
    p.add_argument('--physics-rules', help='Explicit static unit/algorithm evidence JSON; no runtime values')
    p.add_argument('--compact', action='store_true', help='Binary USD and losslessly gzipped large generated JSON evidence')
    p.add_argument('--native-delivery-name', help='Generate a NVIDIA-layout final package inside output/delivery')
    p.add_argument('--manufacturer'); p.add_argument('--thumbnail')
    a = p.parse_args(); output = Path(a.output).resolve()
    if output.exists():
        raise ValueError('Output exists; earlier evidence must remain unchanged')
    support_inputs = a.material_library + ([a.physics_rules] if a.physics_rules else [])
    policy = guard(a.source, output, support_inputs + ([a.thumbnail] if a.thumbnail else []))
    try:
        result = convert(a.source, output, a.expected_sha256, a.material_library, a.physics_rules, a.compact)
        if a.native_delivery_name:
            from deliver_conversion import deliver
            print(json.dumps(deliver(output,a.source,'vc',a.native_delivery_name,manufacturer=a.manufacturer,thumbnail=a.thumbnail)))
        print(json.dumps({k: result[k] for k in ('status','nodes','meshes','triangles','source_entities','source_joint_contracts')}))
    finally:
        write(output/'evidence/offline-policy.json', {'declared_asset_input': str(Path(a.source).resolve()),
              'declared_support_inputs': support_inputs, 'read_paths': sorted(policy['reads']),
              'blocked_operations': policy['blocked'], 'network_and_process_operations_forbidden': True,
              'scope': 'Python audit events and reviewed converter; not an OS-wide process trace'})


if __name__ == '__main__': main()
