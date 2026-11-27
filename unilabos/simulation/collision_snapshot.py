"""Read-only export of effective USD collision geometry for a motion planner.

Call after the complete consumer scene is built, before assisted mode removes
CollisionAPI. This exports authored geometry and its approximation metadata,
not PhysX's private cooked hulls. Runtime body poses must come from Isaac views.
"""
import hashlib
import json
from pathlib import Path


def export_collision_snapshot(stage, output, *, identities=None, roles=None, excluded_roots=(),
                              static_visual_roots=(), moving_visual_roots=()):
    """Write a JSON body/shape manifest plus lossless local-geometry NPZ arrays.

    ``paths`` is the port's identity -> prim path mapping. ``output`` is a JSON
    filename (or a directory, receiving planning-geometry.json). Matrices use
    column vectors, metres and the stage's recorded up axis. Moving collider
    points are baked into a rigid body's local frame; static points use world.
    Holes, convex partitions and original triangle winding are retained.
    static_visual_roots 显式增加指定子树内的可见静态 CAD 网格用于规划；
    moving_visual_roots 必须另行显式声明，并为所属刚体提供 roles。
    不修改 USD 碰撞属性，不覆盖禁用碰撞声明；附加形状标为 planning_only_*
    并保留 body-local 变换，不能冒充物理碰撞体。
    """
    import numpy as np
    from pxr import Gf, Usd, UsdGeom, UsdPhysics

    output = Path(output)
    if output.suffix != '.json':
        output = output / 'planning-geometry.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    array_file = output.with_suffix('.npz')
    units = float(UsdGeom.GetStageMetersPerUnit(stage))
    if abs(units - 1.) > 1e-12:
        raise ValueError('Planning exporter requires the metre-normalized consumer stage')
    identities = dict(identities or {})
    roles = dict(roles or {})
    static_visual_roots = tuple(str(root).rstrip("/") for root in static_visual_roots)
    moving_visual_roots = tuple(str(root).rstrip("/") for root in moving_visual_roots)
    if any(not root.startswith("/") or root == "" for root in static_visual_roots + moving_visual_roots):
        raise ValueError("Planning visual roots must be absolute USD paths")
    cache = UsdGeom.XformCache()
    bodies, arrays, excluded, unsupported = {}, {}, [], []
    count = 0
    roundtrip_max = 0.

    def identity(prim):
        result = {}
        while prim and not prim.IsPseudoRoot():
            for attr in prim.GetAttributes():
                if attr.GetName().startswith('lab:') and attr.HasValue():
                    value = attr.Get()
                    if isinstance(value, (str, int, float, bool)):
                        result.setdefault(attr.GetName(), value)
            prim = prim.GetParent()
        return result

    def apply(points, matrix):
        # Explicit affine arithmetic avoids BLAS for tiny transforms on Windows.
        m = np.asarray(matrix)
        return sum(points[:, i:i+1] * m[i:i+1, :3] for i in range(3)) + m[3, :3]

    for prim in Usd.PrimRange.Stage(stage, Usd.TraverseInstanceProxies()):
        authored_collider = prim.HasAPI(UsdPhysics.CollisionAPI)
        path = str(prim.GetPath())
        planning_visual = (not authored_collider and prim.IsA(UsdGeom.Mesh)
            and any(path == root or path.startswith(root + '/') for root in static_visual_roots + moving_visual_roots))
        if not authored_collider and not planning_visual:
            continue
        reason = None
        if prim.GetAttribute('physics:collisionEnabled').Get() is False:
            reason = 'collision_disabled'
        elif any(path == base or path.startswith(base + '/')
                 for base in excluded_roots):
            reason = 'robot_provider_URDF'
        elif prim.GetAttribute('lab:collisionRole').Get() in {
                'optical_path_visual_candidate', 'optical_ray_visual', 'flexible_cable_visual'}:
            reason = 'nonphysical_optical_or_cable_visual'
        if reason:
            excluded.append({'prim': path, 'reason': reason})
            continue
        body = prim
        while body and not body.IsPseudoRoot():
            if body.HasAPI(UsdPhysics.RigidBodyAPI) and body.GetAttribute('physics:rigidBodyEnabled').Get() is not False:
                break
            body = body.GetParent()
        moving = bool(body and not body.IsPseudoRoot())
        if planning_visual:
            declared_roots = moving_visual_roots if moving else static_visual_roots
            if not any(path == root or path.startswith(root + '/') for root in declared_roots):
                excluded.append({'prim': path, 'reason': 'moving_visual_requires_explicit_declaration' if moving
                                 else 'static_visual_requires_explicit_declaration'})
                continue
            if UsdGeom.Imageable(prim).ComputeVisibility() == 'invisible':
                excluded.append({'prim': path, 'reason': 'invisible_planning_visual'})
                continue
        body_path = str(body.GetPath()) if moving else '/__static_world__'
        original_world = cache.GetLocalToWorldTransform(body) if moving else Gf.Matrix4d(1)
        # Body scale is baked into shape geometry, not passed as a runtime pose.
        world_from_body = Gf.Matrix4d().SetRotate(original_world.ExtractRotationQuat())
        world_from_body.SetTranslateOnly(original_world.ExtractTranslation())
        shape_world = cache.GetLocalToWorldTransform(prim)
        body_from_shape = shape_world * world_from_body.GetInverse()
        if body_path not in bodies:
            name = identities.get(body_path)
            if not moving:
                role = 'static_world'
            else:
                if body_path not in roles:
                    raise ValueError("Moving collision body requires an explicit role: " + body_path)
                role = roles[body_path]
            bodies[body_path] = {'path': body_path, 'identity': name, 'role': role,
                                 'source_identity': identity(body) if moving else {},
                                 'world_from_body_initial': np.asarray(world_from_body).T.tolist(),
                                 'usd_world_initial': np.asarray(original_world).T.tolist(),
                                 'pose_authority': 'initial_USD_only; replace with current OS scene body pose before every request' if moving else 'static_world',
                                 'kinematic': bool(body.GetAttribute('physics:kinematicEnabled').Get()) if moving else False,
                                 'shapes': []}
        shape = {'prim': path, 'type': prim.GetTypeName(), 'source_identity': identity(prim),
                 'body_from_shape': np.asarray(body_from_shape).T.tolist(),
                 'world_from_shape_initial': np.asarray(shape_world).T.tolist(),
                 'approximation': prim.GetAttribute('physics:approximation').Get() or 'none',
                 'cooked_geometry': 'not_exported', 'collision_enabled': bool(authored_collider),
                 'geometry_source': 'authored_collision' if authored_collider else ('planning_only_moving_visual' if moving else 'planning_only_static_visual'),
                 'contact_offset_m': prim.GetAttribute('physxCollision:contactOffset').Get(),
                 'rest_offset_m': prim.GetAttribute('physxCollision:restOffset').Get()}
        if prim.IsA(UsdGeom.Mesh):
            mesh = UsdGeom.Mesh(prim)
            points = np.asarray(mesh.GetPointsAttr().Get(), dtype=np.float64)
            counts = np.asarray(mesh.GetFaceVertexCountsAttr().Get(), dtype=np.int32)
            indices = np.asarray(mesh.GetFaceVertexIndicesAttr().Get(), dtype=np.int32)
            holes = set(mesh.GetHoleIndicesAttr().Get() or [])
            if not np.isfinite(points).all() or sum(counts) != len(indices):
                raise ValueError('Invalid collision mesh: ' + path)
            # Preserve polygon topology too. Current generated CAD is triangular;
            # non-triangle faces are not silently fan-filled across concavities.
            if any(n != 3 for i, n in enumerate(counts) if i not in holes):
                unsupported.append({'prim': path, 'reason': 'nontriangular_collision_faces_require_exact_tessellation'})
            body_points = apply(points, body_from_shape)
            reconstructed = apply(body_points, world_from_body)
            error = float(np.max(np.abs(reconstructed - apply(points, shape_world)))) if len(points) else 0.
            roundtrip_max = max(roundtrip_max, error)
            prefix = 's' + str(count)
            arrays[prefix + '_vertices'] = body_points
            arrays[prefix + '_counts'] = counts
            arrays[prefix + '_indices'] = indices
            shape.update(vertices=prefix + '_vertices', face_counts=prefix + '_counts',
                         face_indices=prefix + '_indices', hole_faces=sorted(holes),
                         point_frame='body_local', points=len(points), faces=len(counts),
                         orientation=mesh.GetOrientationAttr().Get(), world_roundtrip_error_m=error)
        elif prim.GetTypeName() in ('Cube', 'Sphere', 'Cylinder', 'Capsule', 'Cone', 'Plane'):
            shape['parameters'] = {name: prim.GetAttribute(name).Get()
                                   for name in ('size', 'radius', 'height', 'axis', 'width', 'length')
                                   if prim.GetAttribute(name) and prim.GetAttribute(name).HasValue()}
            shape['point_frame'] = 'primitive_shape_local_with_body_from_shape'
        else:
            unsupported.append({'prim': path, 'reason': 'unsupported_shape_type:' + prim.GetTypeName()})
        bodies[body_path]['shapes'].append(shape)
        count += 1
    if not count:
        raise ValueError('No enabled collision geometry; export before assisted collision removal')
    temporary_arrays = array_file.with_suffix('.npz.tmp')
    with temporary_arrays.open('wb') as stream:
        np.savez_compressed(stream, **arrays)
    temporary_arrays.replace(array_file)
    report = {'schema': 'unilab.collision-snapshot/v1', 'status': 'exported' if not unsupported else 'incomplete',
              'units': 'metres', 'up_axis': str(UsdGeom.GetStageUpAxis(stage)), 'matrix_convention': 'column_vectors',
              'arrays': array_file.name, 'arrays_sha256': hashlib.sha256(array_file.read_bytes()).hexdigest(),
              'bodies': list(bodies.values()), 'shape_count': count, 'excluded': excluded,
              'unsupported': unsupported, 'world_roundtrip_max_error_m': roundtrip_max,
              'inactive_prims': 'Omitted by USD active traversal, including replaced CAD occurrences',
              'geometry_authority': 'Effective authored colliders plus explicitly selected planning-only CAD; private PhysX cooked geometry not exported',
              'static_visual_roots': list(static_visual_roots),
              'moving_visual_roots': list(moving_visual_roots),
              'planning_only_moving_visual_shapes': sum(1 for b in bodies.values() for s in b['shapes']
                  if s['geometry_source'] == 'planning_only_moving_visual'),
              'planning_only_static_visual_shapes': sum(1 for b in bodies.values() for s in b['shapes']
                  if s['geometry_source'] == 'planning_only_static_visual'),
              'path_planning_validation': 'not_run', 'hardware': False}
    temporary = output.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(output)
    return report


def read_collision_body_poses(stage,metadata):
    """读取 OS 当前运动学状态；物理执行器应传自己的实测视图状态。"""
    import numpy as np
    from pxr import Gf, UsdGeom
    if abs(UsdGeom.GetStageMetersPerUnit(stage)-1)>1e-12:
        raise ValueError("Current collision state requires metres")
    cache=UsdGeom.XformCache();result={}
    for body in metadata["bodies"]:
        if body["role"]=="static_world":continue
        prim=stage.GetPrimAtPath(body["path"])
        if not prim or not prim.IsActive():raise ValueError("Missing current body: "+body["path"])
        matrix=cache.GetLocalToWorldTransform(prim)
        current=np.asarray(matrix).T
        initial=np.asarray(body["usd_world_initial"])
        if not np.allclose(current[:3,:3].T@current[:3,:3],initial[:3,:3].T@initial[:3,:3],atol=1e-8,rtol=1e-8):
            raise ValueError("Body scale/shear changed; re-export collision geometry")
        rigid=Gf.Matrix4d().SetRotate(matrix.ExtractRotationQuat())
        rigid.SetTranslateOnly(matrix.ExtractTranslation())
        result[body["path"]]=np.asarray(rigid).T.tolist()
    return result
