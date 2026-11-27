"""Offline VC cached-unit normalization and explicit-feature solid diagnostics.

No VC library, native snapshot, source script or path in support_rules is read.
Computed geometry is a declared-feature diagnostic, never a native collider claim.
"""
import hashlib
import json
import math
from collections import Counter

import numpy as np

RULESET = 'vc-5.1-revolution-physics-static-20261104-v1'
BINARY_SHA256 = 'bcb4985eaa77d51f81d93d6e8720c3db57d388ebf198fc89af084a79a5e65135'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _value(field):
    if field.get('expression'):
        return field.get('derived_value_source_units')
    return field.get('value')


def _rules(support):
    if support is None:
        return False
    if support.get('rule_set_id') != RULESET or support.get('binary_sha256') != BINARY_SHA256:
        raise ValueError('Unsupported static physics evidence ruleset')
    expected = {'mass_value_to_kg': .001, 'density_value_to_kg_m3': 1.,
                'inertia_xyz_to_kg_m2': 1., 'com_translation_to_m': .001}
    if support.get('normalization_factors') != expected:
        raise ValueError('Static physics evidence factors do not match reviewed rules')
    if not support.get('rules') or not support.get('evidence'):
        raise ValueError('Missing static physics rule evidence index')
    return True


def solid_integrals(points_m, triangles):
    """Integrate an oriented, closed triangle solid at unit density.

    Reject open/nonmanifold/inconsistently oriented meshes; do not repair them.
    Exact duplicate point coordinates are welded only for edge incidence checks.
    A globally reversed solid is accepted with explicit signed-volume evidence.
    """
    p = np.asarray(points_m, dtype=float)
    raw_t = np.asarray(triangles)
    if p.ndim != 2 or p.shape[1] != 3 or not np.isfinite(p).all():
        raise ValueError('Invalid finite Nx3 points')
    if raw_t.ndim != 2 or raw_t.shape[1] != 3 or not len(raw_t):
        raise ValueError('Nonempty triangle table required')
    if not np.issubdtype(raw_t.dtype, np.integer):
        raise ValueError('Triangle indices must be integers')
    t = raw_t.astype(np.int64)
    if t.min() < 0 or t.max() >= len(p):
        raise ValueError('Triangle index outside point array')
    _, ids = np.unique(p, axis=0, return_inverse=True)
    topo = ids[t]
    undirected, directed = Counter(), Counter()
    edge_faces = {}
    for face_index, (a, b, c) in enumerate(topo):
        if len({int(a), int(b), int(c)}) != 3:
            raise ValueError('Degenerate triangle vertex indices')
        for x, y in ((a,b), (b,c), (c,a)):
            directed[(int(x), int(y))] += 1
            edge = tuple(sorted((int(x), int(y))))
            undirected[edge] += 1
            edge_faces.setdefault(edge, []).append(face_index)
    if any(n != 2 for n in undirected.values()):
        raise ValueError('Open or nonmanifold edge incidence')
    if any(directed[(a,b)] != 1 or directed[(b,a)] != 1 for a,b in undirected):
        raise ValueError('Inconsistent face winding or duplicate oriented faces')
    # Shift near the object to reduce cancellation for distant instances.
    origin = p.mean(axis=0)
    a,b,c = (p[t[:, i]] - origin for i in range(3))
    if np.any(np.linalg.norm(np.cross(b-a,c-a), axis=1) <= np.finfo(float).eps * max(np.ptp(p, axis=0).max()**2, 1e-30)):
        raise ValueError('Zero-area triangle')
    tetra = np.einsum('ij,ij->i', a, np.cross(b,c)) / 6.
    neighbors = [[] for _ in t]
    for first, second_face in edge_faces.values():
        neighbors[first].append(second_face);neighbors[second_face].append(first)
    pending = set(range(len(t)));component_volumes = []
    while pending:
        seed=pending.pop();stack=[seed];component=[seed]
        while stack:
            for neighbor in neighbors[stack.pop()]:
                if neighbor in pending:
                    pending.remove(neighbor);stack.append(neighbor);component.append(neighbor)
        component_volumes.append(float(tetra[component].sum()))
    if any(v == 0 for v in component_volumes) or (min(component_volumes)<0<max(component_volumes)):
        raise ValueError('Mixed/zero signed shell volumes; cavity containment and orientation unresolved')
    signed = float(tetra.sum())
    scale = max(float(np.prod(np.ptp(p, axis=0))), 1e-30)
    if abs(signed) <= scale * 1e-12:
        raise ValueError('Zero or cancellation-dominated signed solid volume')
    sign = 1 if signed > 0 else -1
    tetra *= sign
    volume = abs(signed)
    s = a+b+c
    center_local = np.einsum('i,ij->j', tetra, s) / (4 * volume)
    second = np.zeros((3,3))
    for v,aa,bb,cc,ss in zip(tetra,a,b,c,s):
        second += v / 20 * (np.outer(ss,ss)+np.outer(aa,aa)+np.outer(bb,bb)+np.outer(cc,cc))
    centered = second - volume * np.outer(center_local, center_local)
    inertia = np.trace(centered)*np.eye(3)-centered
    if np.linalg.eigvalsh(inertia).min() <= 0:
        raise ValueError('Nonpositive integrated inertia')
    return {'volume_m3': volume, 'center_of_mass_m': (center_local+origin).tolist(),
            'unit_density_inertia_kg_m2': inertia.tolist(), 'signed_volume_m3': signed,
            'topology': {'closed_manifold_edges':len(undirected),'triangles':len(t),
                         'globally_reversed_winding': sign < 0,
                         'connected_shells':len(component_volumes),
                         'self_intersection_checked':False}}


def _sum_solids(solids):
    volume = sum(s['volume_m3'] for s in solids)
    center = sum(s['volume_m3']*np.array(s['center_of_mass_m']) for s in solids)/volume
    inertia = np.zeros((3,3))
    for s in solids:
        delta = np.array(s['center_of_mass_m'])-center
        inertia += np.array(s['unit_density_inertia_kg_m2']) + s['volume_m3']*(np.dot(delta,delta)*np.eye(3)-np.outer(delta,delta))
    return volume,center,inertia


def recompute(geometry, physics, support_rules=None):
    """Return source-only sidecar; units require explicit reviewed static rules.

    No inferred/native body is emitted. Integration covers only geometry with an
    explicit PhysicsCollider property on the exact same RSC feature/node owner.
    Collider mode selection/cooking and generated/script-modified shapes remain
    unresolved; even complete diagnostics are not final native mass properties.
    """
    enabled = _rules(support_rules)
    source_hash = physics['source_sha256']
    if geometry.get('source',{}).get('archive_sha256') != source_hash:
        raise ValueError('Physics and geometry source archive mismatch')
    if geometry.get('coordinate_contract',{}).get('metres_per_source_unit') != .001:
        raise ValueError('Unsupported source geometry length unit')
    result = {'schema':'unilab.vcmx-offline-mass/1','source_sha256':source_hash,
              'geometry_contract_sha256':digest(geometry),'physics_contract_sha256':digest(physics),
              'support_rules_sha256':digest(support_rules) if enabled else None,
              'status':'normalized_with_declared_feature_diagnostics' if enabled else 'not_normalized_no_support_rules',
              'entities':[], 'policy':{'vc_started':False,'vc_dll_loaded':False,'embedded_python_executed':False,
              'historical_runtime_input':False,'mass_api_authorized':False,'native_parity':'pending'}}
    feature_status = {f['id']:f.get('status') for f in geometry.get('feature_coverage',[])}
    for entity in physics.get('entities',[]):
        owner = entity['owner']; fields = entity['fields']
        row = {'owner_id':owner['id'],'node_id':owner['node_id'],'source_owner':owner,
               'auto_mass_update':entity['auto_mass_update'],'normalized_cache':{},
               'computed_from_source':None,'gaps':[],'native_parity':'pending','mass_api_authorized':False}
        result['entities'].append(row)
        if not enabled:
            row['gaps'].append('Static unit evidence rules not supplied');continue
        cache = row['normalized_cache']
        mass = _value(fields.get('Mass',{}));density = _value(fields.get('Material Density',{}))
        inertia = _value(fields.get('Inertia',{}));com = _value(fields.get('CenterOfGravity',{}))
        mf = fields.get('Mass',{})
        if _number(mass) and mass >= 0 and mf.get('quantity') == 'Mass' and mf.get('magnitude') == 1000:
            cache['mass_kg'] = mass*.001
        else:row['gaps'].append('Mass missing/invalid or unsupported Quantity/Magnitude')
        if _number(density) and density >= 0:cache['density_kg_m3'] = density
        else:row['gaps'].append('Density missing/invalid')
        if isinstance(inertia,list) and len(inertia)==4 and all(_number(x) for x in inertia):
            cache['inertia_xyz_kg_m2'] = inertia[:3]
            cache['inertia_fourth_lane_uninterpreted'] = inertia[3]
            cache['inertia_basis'] = 'Serialized xyz consumed as native mass-space diagonal; auto-update path extracts tensor diagonal, not proven eigenvalues'
        if isinstance(com,list) and len(com)==16 and all(_number(x) for x in com):
            m = np.array(com).reshape(4,4)
            if np.allclose(m[:,3],[0,0,0,1],atol=1e-10,rtol=0):
                m[3,:3] *= .001;cache['com_matrix_m'] = m.tolist()
            else:row['gaps'].append('COM matrix is not source row-vector affine')
        row['cache_evidence'] = {'classification':'direct_serialized_with_statically_proven_unit_conversion',
            'not_recomputed':True,'not_measured':True,'rule_set_id':RULESET,
            'fields':{k:{'line':v.get('line'),'archive_member':v.get('archive_member'),'raw_block_sha256':v.get('raw_block_sha256')} for k,v in fields.items() if k in ('Mass','Material Density','Inertia','CenterOfGravity')}}
        solids=[];details=[];seen_features=set()
        selected = [c for c in physics.get('colliders',[]) if c['owner']['node_id']==owner['node_id']]
        for collider in selected:
            feature_id = collider['owner']['id'];mode = _value(collider['fields'].get('PhysicsCollider',{}))
            matches=[m for m in geometry.get('meshes',[]) if m.get('feature_id')==feature_id and m.get('node_id')==owner['node_id']]
            detail={'feature_id':feature_id,'source_mode_raw':mode,'source_line':collider['owner']['line'],'meshes':[]}
            details.append(detail)
            if feature_id in seen_features:
                detail['gap']='Duplicate explicit collider feature owner; not counted twice';continue
            seen_features.add(feature_id)
            if feature_status.get(feature_id) != 'evaluated' or not matches:
                detail['gap']='Explicit feature geometry unavailable or unevaluated';continue
            for mesh in matches:
                one={'mesh_id':mesh['id']};detail['meshes'].append(one)
                try:
                    transform=np.asarray(mesh['local_to_node_matrix'],dtype=float)
                    if transform.shape!=(4,4) or not np.isfinite(transform).all() or not np.allclose(transform[:,3],[0,0,0,1],rtol=0,atol=1e-10):
                        raise ValueError('Feature-to-node matrix must be finite row affine')
                    points=np.asarray(mesh['points_mm'],dtype=float)
                    transformed=(np.c_[points,np.ones(len(points))]@transform)[:,:3]*.001
                    integral=solid_integrals(transformed,mesh['triangles'])
                    one.update(integral=integral,status='integrated_declared_feature_solid');solids.append(integral)
                except (ValueError,TypeError,KeyError,IndexError,np.linalg.LinAlgError) as exc:
                    one.update(status='decoder_gap',gap=str(exc))
        row['explicit_feature_diagnostics']=details
        row['gaps'].extend(['Native collider mode selection, fallback bounds and cooking not reproduced',
                            'Embedded script geometry generation/modification not evaluated',
                            'Automatic last-edited mass/density mode is internal state not serialized in this contract'])
        if not solids:
            row['gaps'].append('No eligible closed explicit-feature solid');continue
        volume,center,tensor=_sum_solids(solids)
        candidate={'classification':'deterministic_from_file','scope':'sum of integrated explicit-feature solids only; overlapping solids count additively',
                   'native_equivalent':False,'complete_native_actor_shape_coverage':False,
                   'volume_m3':volume,'center_of_mass_node_m':center.tolist(),
                   'unit_density_inertia_node_kg_m2':tensor.tolist(),'candidates':{}}
        if 'density_kg_m3' in cache:
            rho=cache['density_kg_m3'];candidate['candidates']['density_driven']={'mass_kg':volume*rho,'density_kg_m3':rho,'inertia_node_kg_m2':(tensor*rho).tolist()}
        if cache.get('mass_kg',0)>0:
            rho=cache['mass_kg']/volume;candidate['candidates']['mass_driven']={'mass_kg':cache['mass_kg'],'density_kg_m3':rho,'inertia_node_kg_m2':(tensor*rho).tolist()}
        row['computed_from_source']=candidate
    return result
