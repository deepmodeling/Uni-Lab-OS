"""Bounded offline RoboDK MDH -> source-colored USD conversion.

No vendor API. Supports statically traced serial mechanism types 1/6/8 with
6 revolute axes and seven visual frames, plus the traced generic 1-4-axis
R/P serial subset (source-empty links are preserved).
Native solver equivalence is a separate
qualification: validation here checks source field transport, joint frames,
multi-pose assembly, mesh/material fidelity and persisted USD semantics.
"""
import argparse, hashlib, json, math, shutil
from pathlib import Path
import numpy as np
from pxr import Gf, Sdf, Usd, UsdGeom, UsdPhysics, UsdShade, Vt
from robodk_offline import read

def write(path, value):
    Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')

def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

NATIVE_KINEMATICS_PROOF={'artifact':'RoboDK.exe','sha256':'83fa63bb2d41719e27ae7e07664a48de004e5dc1f1d5fe7818a2df60252efc06',
    'authority':'static PE instructions and bounded Ghidra decompilation; no vendor execution',
    'link_pose_dispatch':'0x1406033f0','type1_and_type8_core':'0x1405b0c10','type6_core':'0x140622ed0',
    'xyz_rx_ry_rz':'0x1406455a0','joint_input_mapping':'0x14055e6a0',
    'base_binding':'0x140337691: member0x868=model+0x20800=header+180*8',
    'base_application':'First returned graphical link pose is the base transform; subsequent poses postmultiply MDH',
    'units':'source XYZ millimeters and Rx/Ry/Rz radians; joints degrees'}

def source_directions(h):
    """Diagonal signs of the traced header[61] controller-to-MDH mapping.

    Do not use header[64:70] for every mode: modes 0/1 ignore those fields;
    mode12 takes axes2/3 from header71/72 and a cross-axis term from header73.
    Off-diagonal terms are carried by controller_rows, never discarded.
    """
    mode=float(h[61])
    if mode not in (0.,1.,2.,3.,4.,5.,6.,7.,8.,9.,11.,12.):raise ValueError('Unsupported coupled or unverified controller-to-MDH mapping mode '+str(mode))
    if mode in (0.,1.,4.):d=np.ones(6)
    elif mode in (2.,3.):d=np.array([1.,1.,-1.,-1.,-1.,-1.])
    elif mode in (5.,9.):d=np.array([-1.,1.,1.,-1.,1.,-1.])
    elif mode==8.:d=np.array([-1.,1.,-1.,-1.,1.,1.])
    elif mode==11.:d=np.array([-1.,1.,-1.,-1.,1.,-1.])
    elif mode==6.:d=np.array([1.,1.,-1.,1.,1.,1.])
    elif mode==7.:d=np.asarray(h[64:70])
    else:
        d=np.array([h[64],h[71],h[72],h[67],h[68],h[69]])
    if not np.all(np.isin(d,[-1,1])):raise ValueError('Unsupported non-unit source joint scale or invalid direction')
    return d


def controller_rows(h,directions):
    """Coordinates before signed USD frames, traced from PE function 0x14055e6a0.

    Native mode2/8/11: MDH3=-q2-q3; mode4/9: MDH3=q3-q2;
    mode12: MDH3=h73*q2+h72*q3. Do not collapse this into signs.
    """
    rows=np.eye(6);mode=int(h[61])
    if mode in (2,8,11):rows[2,1]=1
    elif mode in (4,9):rows[2,1]=-1
    elif mode==12:rows[2,1]=h[73]/directions[2]
    if not np.isfinite(rows).all():raise ValueError('Nonfinite controller coupling')
    return rows


def joint_coordinates(spec,q):
    if 'controller_coordinate_matrix' in spec:return np.asarray(spec['controller_coordinate_matrix'])@q
    return np.asarray([q[i] for i in spec.get('joint_source_indices',range(len(q)))])


def controller_coordinates(spec,coordinates):
    """Invert the USD coordinate map and enforce the original coupled domain."""
    q=np.array(coordinates,dtype=float,copy=True)
    if q.shape!=(len(spec['lower']),) or not np.isfinite(q).all():raise ValueError('Invalid joint target')
    if 'controller_coordinate_matrix' in spec:q=np.linalg.solve(np.asarray(spec['controller_coordinate_matrix']),q)
    elif 'joint_source_indices' in spec:
        original=q.copy()
        for i,k in enumerate(spec['joint_source_indices']):q[k]=original[i]
    if np.any(q<np.asarray(spec['lower'])-1e-10) or np.any(q>np.asarray(spec['upper'])+1e-10):raise ValueError('Target outside source controller domain')
    return q


def joint_bounds(spec,i):
    if 'controller_coordinate_matrix' not in spec:
        k=spec.get('joint_source_indices',list(range(len(spec['mdh']))))[i]
        return spec['lower'][k],spec['upper'][k]
    r=np.asarray(spec['controller_coordinate_matrix'])[i];lo=np.asarray(spec['lower']);hi=np.asarray(spec['upper'])
    return float(np.where(r>=0,r*lo,r*hi).sum()),float(np.where(r>=0,r*hi,r*lo).sum())

def xyz_rx_ry_rz(values):
    x,y,z,a,b,c=map(float,values);ca,sa=math.cos(a),math.sin(a);cb,sb=math.cos(b),math.sin(b);cc,sc=math.cos(c),math.sin(c)
    return np.array([[cb*cc,-cb*sc,sb,x],[ca*sc+sa*sb*cc,ca*cc-sa*sb*sc,-sa*cb,y],
                     [sa*sc-ca*sb*cc,sa*cc+ca*sb*sc,ca*cb,z],[0,0,0,1]],dtype=float)

def source_spec(parsed):
    h=parsed['header']
    if len(h)==4000 and h[20] in (0.,2.):return generic_source_spec(parsed)
    if len(h)==4000 and abs(h[20]) in (3.,23.):return seven_source_spec(parsed)
    auxiliary={}
    if len(h)==4000 and abs(h[20])==1 and tuple(h[21:23])==(6,9) and tuple(h[1060:1062])==(8,9):
        if h[1080]!=0 or h[1081]!=0:raise ValueError('Additional four-bar branch requires separate closure decoding')
        if not np.isfinite(h[1062:1066]).all():raise ValueError('Nonfinite auxiliary anchors')
        auxiliary={'auxiliary_linkage':{'kind':'robodk_two_anchor_slide','output_links':[7,8],
            'anchor_links':[1,2],'anchors_m':[[h[1062]*.001,0,h[1063]*.001],[h[1064]*.001,h[1065]*.001,0]],
            'orientation_link':1,'orientation_column':1,'native_function':'0x1405b0c10:0x1405b12e0..0x1405b17f4',
            'binary_sha256':NATIVE_KINEMATICS_PROOF['sha256'],
            'scope':'source graphical closed linkage pose dependency; not physical constraint or actuator model'}}
    if len(h)!=4000 or abs(h[20]) not in (1.,6.,8.) or (tuple(h[21:23])!=(6,7) and not auxiliary):
        raise ValueError('Unsupported mechanism: require statically traced type1/6/8, six axes, seven links')
    if not any(l['surfaces'] for l in parsed['links']):raise ValueError('Source robot contains no visual geometry')
    params=h[200:320].reshape(6,20)
    if np.any(params[:,4:]):raise ValueError('Uninterpreted MDH extension fields')
    directions=source_directions(h)
    lo,hi,home=h[600:606],h[620:626],h[680:686]
    if np.any(lo>=hi) or np.any(home<lo) or np.any(home>hi):raise ValueError('Invalid/inconsistent source joint limits/home')
    mapping=controller_rows(h,directions)
    coupling={} if np.array_equal(mapping,np.eye(6)) else {'controller_coordinate_matrix':mapping.tolist(),
        'controller_constraint':{'kind':'invertible_linear_coordinate_map','formula':'usd_joint_degrees = matrix @ source_controller_degrees; signed axes encoded in joint frames',
            'source_lower':lo.tolist(),'source_upper':hi.tolist(),
            'limits_scope':'USD limits are per-axis enclosure only. Coupled domain requires inverse(matrix) @ usd_coordinates within original source limits; dynamics remains disabled',
            'native_function':'0x14055e6a0','binary_sha256':NATIVE_KINEMATICS_PROOF['sha256']}}
    return {**coupling,**auxiliary,'mechanism_type':int(h[20]),'mdh':params[:,:4].tolist(),'joint_directions':directions.tolist(),'raw_joint_direction_field':h[64:70].tolist(),'lower':lo.tolist(),'upper':hi.tolist(),
        'home':home.tolist(),'base_xyz_rx_ry_rz':h[180:186].tolist(),'flange_xyz_rx_ry_rz':h[560:566].tolist(),
        'flange_translation_mm':h[560:563].tolist(),'flange_z_radians':float(h[565]),
        'controller_to_mdh_mode':int(h[61]),'raw_controller_mapping_fields':h[60:74].tolist(),'native_static_provenance':NATIVE_KINEMATICS_PROOF,
        'field_indices':{'controller_mapping_mode':61,'mode7_joint_directions':64,'mode12_axis2_axis3_coupling':[71,72,73],'base':180,'mdh_rows':[200+20*i for i in range(6)],'lower':600,'upper':620,'home':680,'flange':560},
        'units':'MDH alpha/theta radians, a/d millimeters; input joints degrees'}

def seven_source_spec(parsed):
    """Native 5b7280 with independent/permuted coordinates from 55c4d0."""
    h=parsed['header']
    if tuple(h[21:23])!=(7,8):raise ValueError('Seven-axis core requires exactly seven axes/eight visual links')
    if not any(l['surfaces'] for l in parsed['links']):raise ValueError('Source robot contains no visual geometry')
    rows=h[200:340].reshape(7,20)
    if np.any(rows[:,4:]):raise ValueError('Uninterpreted seven-axis MDH extension fields')
    mode=h[61]
    if mode not in (0.,1.,2.,7.):raise ValueError('Unverified seven-axis controller mapping mode '+str(mode))
    indices=[0,1,6,2,3,4,5] if mode in (1.,2.) else list(range(7))
    directions=h[64:71][indices] if mode in (2.,7.) else np.ones(7)
    if not np.all(np.isin(directions,[-1.,1.])):raise ValueError('Unsupported seven-axis non-unit source joint scale')
    lo,hi,home=h[600:607],h[620:627],h[680:687]
    if np.any(lo>=hi) or np.any(home<lo) or np.any(home>hi):raise ValueError('Invalid/inconsistent source joint limits/home')
    proof={**NATIVE_KINEMATICS_PROOF,'seven_axis_core':'0x1405b7280','joint_input_mapping':'0x14055c4d0',
        'dispatch':'abs(header20) in {3,23}; both branches call 0x1405b7280',
        'source_index_order':'mode0/7 [0,1,2,3,4,5,6]; mode1/2 [0,1,6,2,3,4,5]',
        'mapping_scales':'mode0/1 identity; mode2/7 scale by h64+source_index before permutation'}
    return {'mechanism_type':int(h[20]),'mdh':rows[:,:4].tolist(),'joint_types':['revolute']*7,
        'joint_source_indices':indices,'joint_directions':directions.tolist(),'raw_joint_direction_field':h[64:71].tolist(),
        'lower':lo.tolist(),'upper':hi.tolist(),'home':home.tolist(),'q_coordinate_space':'original source controller order; joint_source_indices maps chain slots to controller indices',
        'base_xyz_rx_ry_rz':h[180:186].tolist(),'flange_xyz_rx_ry_rz':h[560:566].tolist(),
        'flange_translation_mm':h[560:563].tolist(),'flange_z_radians':float(h[565]),'controller_to_mdh_mode':int(mode),
        'raw_controller_mapping_fields':h[60:74].tolist(),'native_static_provenance':proof,
        'field_indices':{'controller_mapping_mode':61,'seven_axis_directions':64,'base':180,'mdh_rows':[200+20*i for i in range(7)],'lower':600,'upper':620,'home':680,'flange':560},
        'joint_input_units':['degree']*7,'units':'MDH alpha/theta radians, a/d millimeters; original controller input joints degrees'}

def generic_source_spec(parsed):
    """Narrow native fallback: 1-4-axis positioners and four-axis SCARA.

    G_LinkPoses -> 6033f0 default -> 5dafb0 -> 5dbe20. Five-axis
    coordinate swapping and alternate visual-link constructions are excluded.
    """
    h=parsed['header'];n=int(h[21])
    if not ((h[20]==0 and h[21] in (1,2,3,4,7)) or (h[20]==2 and h[21]==4)) or h[22]!=n+1:
        raise ValueError('Unsupported generic mechanism topology; require type0 axes1..4/7 or type2 four axes with n+1 links')
    if not any(l['surfaces'] for l in parsed['links']):raise ValueError('Source mechanism contains no visual geometry')
    params=h[200:200+20*n].reshape(n,20)
    if np.any(params[:,5:]) or not np.all(np.isin(params[:,4],[0.,1.])):raise ValueError('Uninterpreted generic MDH extension or joint type')
    mode=h[61]
    if n==7 and mode not in (0.,1.,7.):raise ValueError('Seven-axis generic domain restricted to identity or explicit per-axis scales')
    if mode in (0.,1.,4.,8.):directions=np.ones(n)
    elif mode in (2.,3.,5.,6.,9.,11.):
        table={2:[1,1,-1,-1,-1,-1],3:[1,1,-1,-1,-1,-1],5:[-1,1,1,-1,1,-1],6:[1,1,-1,1,1,1],9:[-1,1,1,-1,1,-1],11:[-1,1,1,-1,1,-1]}
        directions=np.array(table[int(mode)][:n],dtype=float)
    elif mode in (7.,10.,12.):
        # Native thresholds: 5 <= n+4 (mode7), 5 <= n+4+1 (10/12).
        # Every supported positive n therefore reads h64, not h60.
        directions=np.asarray(h[64:64+n])
    else:raise ValueError('Unverified generic controller mapping mode '+str(mode))
    if not np.all(np.isin(directions,[-1.,1.])):raise ValueError('Unsupported generic non-unit source joint scale')
    lo,hi,home=h[600:600+n],h[620:620+n],h[680:680+n]
    if np.any(lo>=hi) or np.any(home<lo) or np.any(home>hi):raise ValueError('Invalid/inconsistent source joint limits/home')
    kinds=['prismatic' if flag else 'revolute' for flag in params[:,4]]
    proof={**NATIVE_KINEMATICS_PROOF,'generic_fallback':'0x1405dafb0','generic_direction_mapping':'0x1405dbe20',
        'units':'source XYZ and prismatic input millimeters; Rx/Ry/Rz and MDH alpha/theta radians; revolute input degrees',
        'joint_type':'MDH row[4]==0 revolute; row[4]==1 prismatic; other nonzero values conservatively rejected',
        'excluded':'five-axis swap, links!=axes+1, unrecognized joint-type/extensions and non-unit scale'}
    return {'mechanism_type':int(h[20]),'mdh':params[:,:4].tolist(),'joint_types':kinds,'joint_directions':directions.tolist(),
        'raw_joint_direction_field':h[64:64+n].tolist(),'lower':lo.tolist(),'upper':hi.tolist(),'home':home.tolist(),
        'base_xyz_rx_ry_rz':h[180:186].tolist(),'flange_xyz_rx_ry_rz':h[560:566].tolist(),
        'flange_translation_mm':h[560:563].tolist(),'flange_z_radians':float(h[565]),
        'controller_to_mdh_mode':int(mode),'raw_controller_mapping_fields':h[60:74].tolist(),'native_static_provenance':proof,
        'field_indices':{'controller_mapping_mode':61,'generic_axis_directions':64,'base':180,'mdh_rows':[200+20*i for i in range(n)],'lower':600,'upper':620,'home':680,'flange':560},
        'joint_input_units':['mm' if k=='prismatic' else 'degree' for k in kinds],
        'units':'MDH alpha/theta radians, a/d millimeters; per-axis joint_input_units'}

def mdh_matrix(row,q,kind='revolute'):
    # Direct closed-form source formula, distinct from USD quaternion reconstruction.
    alpha,a,offset,d=row
    t=offset+math.radians(q) if kind=='revolute' else offset
    if kind=='prismatic':d+=q
    ca,sa=math.cos(alpha),math.sin(alpha);ct,st=math.cos(t),math.sin(t)
    return np.array([[ct,-st,0,a],[st*ca,ct*ca,-sa,-d*sa],[st*sa,ct*sa,ca,d*ca],[0,0,0,1]],dtype=float)

def source_chain(spec,q):
    poses=[xyz_rx_ry_rz(spec.get('base_xyz_rx_ry_rz',[0]*6))]
    values=joint_coordinates(spec,q)
    for row,value,direction,kind in zip(spec['mdh'],values,spec['joint_directions'],spec.get('joint_types',['revolute']*len(q))):poses.append(poses[-1]@mdh_matrix(row,value*direction,kind))
    for m in poses:m[:3,3]*=.001
    if 'auxiliary_linkage' in spec:
        aux=spec['auxiliary_linkage'];a,b=[(poses[n]@np.r_[p,1])[:3] for n,p in zip(aux['anchor_links'],aux['anchors_m'])]
        x=b-a
        if np.linalg.norm(x)<1e-12:raise ValueError('Singular auxiliary anchor distance')
        x/=np.linalg.norm(x);z=np.cross(x,poses[aux['orientation_link']][:3,aux['orientation_column']])
        if np.linalg.norm(z)<1e-12:raise ValueError('Singular auxiliary orientation')
        z/=np.linalg.norm(z);y=np.cross(z,x)
        for p in (a,b):
            m=np.eye(4);m[:3,:3]=np.column_stack((x,y,z));m[:3,3]=p;poses.append(m)
    return poses

def quat(rotation):return Gf.Quatf(Gf.Matrix3d(rotation.T.tolist()).ExtractRotation().GetQuat())

def local_frame(joint,side):
    p=getattr(joint,'GetLocalPos'+str(side)+'Attr')().Get()
    q=getattr(joint,'GetLocalRot'+str(side)+'Attr')().Get()
    m=np.eye(4);m[:3,:3]=np.asarray(Gf.Matrix3d(Gf.Rotation(q))).T;m[:3,3]=p
    return m

def target_chain(stage,q):
    result=[np.asarray(UsdGeom.XformCache().GetLocalToWorldTransform(stage.GetPrimAtPath('/Asset/link_0'))).T]
    for i,angle in enumerate(q):
        prim=stage.GetPrimAtPath('/Asset/Joints/axis_'+str(i+1));linear=prim.IsA(UsdPhysics.PrismaticJoint)
        source_index=prim.GetAttribute('unilab:sourceControllerIndex').Get()
        if source_index is not None:angle=q[int(source_index)]
        coefficients=prim.GetAttribute('unilab:sourceControllerCoefficients').Get()
        if coefficients is not None:angle=float(np.dot(coefficients,q))
        j=(UsdPhysics.PrismaticJoint if linear else UsdPhysics.RevoluteJoint)(prim)
        if j.GetBody0Rel().GetTargets()!=[Sdf.Path('/Asset/link_'+str(i))] or j.GetBody1Rel().GetTargets()!=[Sdf.Path('/Asset/link_'+str(i+1))]:
            raise ValueError('Joint parent/child target mismatch')
        if j.GetAxisAttr().Get()!='Z':raise ValueError('Source axis changed')
        motion=np.eye(4)
        if linear:motion[2,3]=float(angle)*.001
        else:motion[:3,:3]=np.asarray(Gf.Matrix3d(Gf.Rotation(Gf.Vec3d(0,0,1),float(angle)))).T
        result.append(result[-1]@local_frame(j,0)@motion@np.linalg.inv(local_frame(j,1)))
    persisted=stage.GetDefaultPrim().GetAttribute('unilab:auxiliaryLinkage').Get()
    if persisted:
        from robot_behavior.linkage import auxiliary_poses
        result.extend(np.asarray(p) for p in auxiliary_poses(json.loads(persisted),[p.tolist() for p in result]))
    return result

def samples(spec):
    rows=[('home',spec['home'])]
    for i in range(len(spec['mdh'])):
        for name,val in [('perturb',min(spec['upper'][i],spec['home'][i]+7.5)),('lower',spec['lower'][i]),('upper',spec['upper'][i])]:
            q=spec['home'].copy();q[i]=val;rows.append((f'axis_{i+1}_{name}',q))
    if 'controller_coordinate_matrix' in spec:
        for q2 in (spec['lower'][1],spec['upper'][1]):
            for q3 in (spec['lower'][2],spec['upper'][2]):
                q=spec['home'].copy();q[1]=q2;q[2]=q3;rows.append((f'coupled_corner_{q2}_{q3}',q))
    return rows

def validate(entry,source):
    parsed=read(source);spec=source_spec(parsed);stage=Usd.Stage.Open(str(entry));cache=UsdGeom.XformCache()
    if not stage:raise ValueError('USD reopen failed')
    if 'auxiliary_linkage' in spec:
        persisted=stage.GetDefaultPrim().GetAttribute('unilab:auxiliaryLinkage').Get()
        if not persisted or json.loads(persisted)!=spec['auxiliary_linkage']:raise ValueError('Lost auxiliary linkage dependency')
    if 'controller_constraint' in spec:
        persisted=stage.GetDefaultPrim().GetAttribute('unilab:controllerConstraint').Get()
        if not persisted or json.loads(persisted)!=spec['controller_constraint']:raise ValueError('Lost source controller domain constraint')
    n=len(spec['mdh']);kinds=spec.get('joint_types',['revolute']*n)
    if sum(p.IsA(UsdPhysics.Joint) for p in stage.Traverse())!=n:raise ValueError('Joint count mismatch')
    meshes=[p for p in stage.Traverse() if p.IsA(UsdGeom.Mesh)]
    if len(meshes)!=sum(bool(l['surfaces']) for l in parsed['links']):raise ValueError('Unmapped or omitted source link geometry')
    exact=[]
    for link in parsed['links']:
        i=link['index'];mesh=UsdGeom.Mesh(stage.GetPrimAtPath(f'/Asset/link_{i}/Geometry'))
        body=stage.GetPrimAtPath(f'/Asset/link_{i}')
        if not body:raise ValueError('Source link frame omitted')
        m=np.asarray(cache.GetLocalToWorldTransform(body)).T
        if not np.allclose(m,source_chain(spec,spec['home'])[i],atol=2e-7,rtol=0):raise ValueError('Home assembly drift')
        if not link['surfaces']:
            if mesh:raise ValueError('Invented mesh on source-empty link')
            exact.append({'link':i,'triangles':0,'surface_count':0,'source_empty_link_preserved':True,'triangles_and_RGBA_exact':True});continue
        raw=np.concatenate([s['points'] for s in link['surfaces']]).reshape(-1,3).astype(float)*.001
        target=np.asarray(mesh.GetPointsAttr().Get());indices=np.asarray(mesh.GetFaceVertexIndicesAttr().Get())
        if not np.array_equal(target[indices],raw.astype('float32')):raise ValueError('Source triangle values changed')
        offset=0;rgba=[]
        for surface in link['surfaces']:
            sub=UsdGeom.Subset(stage.GetPrimAtPath(f'/Asset/link_{i}/Geometry/surface_{surface["index"]}'))
            if list(sub.GetIndicesAttr().Get())!=list(range(offset,offset+surface['triangles'])):raise ValueError('Material face partition mismatch')
            offset+=surface['triangles'];mat,_=UsdShade.MaterialBindingAPI(sub.GetPrim()).ComputeBoundMaterial()
            sh=mat.ComputeSurfaceSource()[0]
            actual=tuple(sh.GetInput('diffuseColor').Get())+(sh.GetInput('opacity').Get(),)
            if actual!=tuple(surface['rgba']):raise ValueError('Source RGBA not exact')
            rgba.append(actual)
        m=np.asarray(cache.GetLocalToWorldTransform(stage.GetPrimAtPath(f'/Asset/link_{i}'))).T
        if not np.allclose(m,source_chain(spec,spec['home'])[i],atol=2e-7,rtol=0):raise ValueError('Home assembly drift')
        exact.append({'link':i,'triangles':len(raw)//3,'surface_count':len(rgba),'triangles_and_RGBA_exact':True})
    checks=[]
    for name,q in samples(spec):
        if 'controller_coordinate_matrix' in spec:
            if not np.allclose(controller_coordinates(spec,joint_coordinates(spec,q)),q,atol=1e-10,rtol=0):raise ValueError('Controller coordinate roundtrip failed')
        expected=source_chain(spec,q);actual=target_chain(stage,q)
        residual=float(np.max(np.abs(np.asarray(expected)-np.asarray(actual))))
        if residual>2e-6:raise ValueError(f'{name}: source vs serialized USD joint frame mismatch {residual}')
        checks.append({'pose':name,('q_source_units' if 'joint_types' in spec else 'q_degrees'):q,'max_matrix_residual':residual,'status':'passed'})
    for i in range(n):
        source_index=spec.get('joint_source_indices',list(range(n)))[i]
        cls=UsdPhysics.PrismaticJoint if kinds[i]=='prismatic' else UsdPhysics.RevoluteJoint
        j=cls(stage.GetPrimAtPath(f'/Asset/Joints/axis_{i+1}'))
        scale=.001 if kinds[i]=='prismatic' else 1
        if not j:raise ValueError('Source joint type changed')
        if 'joint_source_indices' in spec and j.GetPrim().GetAttribute('unilab:sourceControllerIndex').Get()!=source_index:raise ValueError('Controller-to-chain index mapping mismatch')
        lower,upper=joint_bounds(spec,i)
        if j.GetLowerLimitAttr().Get()!=float(np.float32(lower*scale)) or j.GetUpperLimitAttr().Get()!=float(np.float32(upper*scale)):raise ValueError('Limit float32 transport mismatch')
        if 'controller_coordinate_matrix' in spec and list(j.GetPrim().GetAttribute('unilab:sourceControllerCoefficients').Get() or [])!=spec['controller_coordinate_matrix'][i]:raise ValueError('Lost controller coupling')
        if j.GetJointEnabledAttr().Get():raise ValueError('Unqualified dynamics unexpectedly enabled')
    flange=UsdGeom.Xformable(stage.GetPrimAtPath(f'/Asset/link_{n}/Interfaces/SourceFlange'))
    expected_flange=xyz_rx_ry_rz(spec['flange_xyz_rx_ry_rz']);expected_flange[:3,3]*=.001
    if not flange or not np.allclose(np.asarray(flange.GetLocalTransformation()).T,expected_flange,atol=1e-12,rtol=0):raise ValueError('Source flange full XYZ/RxRyRz transform mismatch')
    return {'status':'passed','source_sha256':parsed['source_sha256'],'usd_sha256':digest(entry),'joint_count':n,'link_count':len(parsed['links']),
        'auxiliary_linkage':spec.get('auxiliary_linkage'),
        'source_geometry_and_materials':exact,'poses':checks,'native_runtime_equivalence':'not_run',
        'scope':'Independent source re-read/closed-form MDH vs serialized USD local frames; not independent manufacturer/native solver evidence',
        'native_static_provenance':spec['native_static_provenance'],'controller_mapping_mode':spec['controller_to_mdh_mode'],
        'source_base_and_full_flange':'passed source-recomputed vs USD readback',
        'contact_and_dynamics':'not_run','cad':'not_present_in_source_container'}

def convert(source,output,metadata=None,usd_format='usda'):
    source=Path(source);output=Path(output);parsed=read(source);spec=source_spec(parsed)
    output.mkdir(parents=True,exist_ok=False);(output/'source').mkdir();shutil.copyfile(source,output/'source'/source.name)
    if usd_format not in ('usda','usdc'):raise ValueError('Unsupported USD output encoding')
    entry=output/('asset.'+usd_format);stage=Usd.Stage.CreateNew(str(entry));root=UsdGeom.Xform.Define(stage,'/Asset');stage.SetDefaultPrim(root.GetPrim())
    UsdGeom.SetStageMetersPerUnit(stage,1);UsdGeom.SetStageUpAxis(stage,'Z')
    root.GetPrim().SetCustomData({'source_sha256':parsed['source_sha256'],'kinematics':'source_MDH_offline_candidate',
        'dynamic_qualification':'not_run_disabled','material_provenance':'exact source surface RGBA; unspecified PBR uses USD defaults'})
    if 'controller_constraint' in spec:
        root.GetPrim().CreateAttribute('unilab:controllerConstraint',Sdf.ValueTypeNames.String).Set(json.dumps(spec['controller_constraint']))
    if 'auxiliary_linkage' in spec:
        root.GetPrim().CreateAttribute('unilab:auxiliaryLinkage',Sdf.ValueTypeNames.String).Set(json.dumps(spec['auxiliary_linkage']))
    mats={};poses=source_chain(spec,spec['home'])
    for link in parsed['links']:
        i=link['index'];path=f'/Asset/link_{i}';body=UsdGeom.Xform.Define(stage,path);body.AddTransformOp().Set(Gf.Matrix4d(poses[i].T.tolist()))
        UsdPhysics.RigidBodyAPI.Apply(body.GetPrim()).CreateRigidBodyEnabledAttr(False)
        if not link['surfaces']:
            body.GetPrim().SetCustomData({'source_surface_count':0,'geometry_status':'source_empty_link_no_mesh_invented'});continue
        points=np.concatenate([s['points'] for s in link['surfaces']]).astype(float)*.001
        geom=UsdGeom.Mesh.Define(stage,path+'/Geometry');geom.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(points.reshape(-1,3).astype('float32')))
        geom.CreateFaceVertexCountsAttr([3]*len(points));geom.CreateFaceVertexIndicesAttr(list(range(len(points)*3)));geom.CreateSubdivisionSchemeAttr('none')
        cross=np.cross(points[:,1]-points[:,0],points[:,2]-points[:,0]);norm=np.linalg.norm(cross,axis=1);cross/=np.where(norm>0,norm,1)[:,None]
        geom.CreateNormalsAttr(Vt.Vec3fArray.FromNumpy(cross.astype('float32')));geom.SetNormalsInterpolation('uniform')
        geom.CreateExtentAttr([Gf.Vec3f(*points.min(axis=(0,1))),Gf.Vec3f(*points.max(axis=(0,1)))])
        offset=0
        for surf in link['surfaces']:
            color=tuple(surf['rgba'])
            if color not in mats:
                mat=UsdShade.Material.Define(stage,f'/Asset/Materials/SourceRGBA_{len(mats)}');sh=UsdShade.Shader.Define(stage,str(mat.GetPath())+'/Shader');sh.CreateIdAttr('UsdPreviewSurface')
                sh.CreateInput('diffuseColor',Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*color[:3]));sh.CreateInput('opacity',Sdf.ValueTypeNames.Float).Set(color[3]);mat.CreateSurfaceOutput().ConnectToSource(sh.ConnectableAPI(),'surface');mats[color]=mat
            subset=UsdGeom.Subset.Define(stage,path+f'/Geometry/surface_{surf["index"]}');subset.CreateElementTypeAttr('face');subset.CreateFamilyNameAttr('materialBind');subset.CreateIndicesAttr(list(range(offset,offset+surf['triangles'])));offset+=surf['triangles']
            UsdShade.MaterialBindingAPI.Apply(subset.GetPrim()).Bind(mats[color])
        UsdGeom.Subset.SetFamilyType(geom,'materialBind',UsdGeom.Tokens.partition)
    for i,row in enumerate(spec['mdh']):
        kind=spec.get('joint_types',['revolute']*len(spec['mdh']))[i];scale=.001 if kind=='prismatic' else 1
        source_index=spec.get('joint_source_indices',list(range(len(spec['mdh']))))[i]
        local=mdh_matrix(row,0);local[:3,3]*=.001
        # Rotate both joint frames 180deg about X for negative source axis.
        # A*F*Rz(q)*inv(F) = A*Rz(-q), retaining source input limits/home.
        flip=np.diag([1.,-1.,-1.]) if spec['joint_directions'][i]<0 else np.eye(3)
        local[:3,:3]=local[:3,:3]@flip
        cls=UsdPhysics.PrismaticJoint if kind=='prismatic' else UsdPhysics.RevoluteJoint
        j=cls.Define(stage,f'/Asset/Joints/axis_{i+1}');j.CreateBody0Rel().SetTargets([f'/Asset/link_{i}']);j.CreateBody1Rel().SetTargets([f'/Asset/link_{i+1}'])
        lower,upper=joint_bounds(spec,i)
        j.CreateAxisAttr('Z');j.CreateLocalPos0Attr(Gf.Vec3f(*local[:3,3]));j.CreateLocalRot0Attr(quat(local[:3,:3]));j.CreateLocalPos1Attr(Gf.Vec3f(0));j.CreateLocalRot1Attr(quat(flip));j.CreateLowerLimitAttr(lower*scale);j.CreateUpperLimitAttr(upper*scale);j.CreateJointEnabledAttr(False)
        j.GetPrim().CreateAttribute('unilab:sourceHomeMillimeters' if kind=='prismatic' else 'unilab:sourceHomeDegrees',Sdf.ValueTypeNames.Double).Set(float(joint_coordinates(spec,spec['home'])[i]))
        if 'controller_coordinate_matrix' in spec:
            j.GetPrim().CreateAttribute('unilab:sourceControllerCoefficients',Sdf.ValueTypeNames.DoubleArray).Set(spec['controller_coordinate_matrix'][i])
        if 'joint_source_indices' in spec:
            j.GetPrim().CreateAttribute('unilab:sourceControllerIndex',Sdf.ValueTypeNames.Int).Set(source_index)
            j.GetPrim().SetDisplayName('Source axis '+str(source_index+1)+' (chain '+str(i+1)+')')
        j.GetPrim().CreateAttribute('unilab:sourceAxisDirection',Sdf.ValueTypeNames.Double).Set(spec['joint_directions'][i])
    flange=xyz_rx_ry_rz(spec['flange_xyz_rx_ry_rz']);flange[:3,3]*=.001
    UsdGeom.Xform.Define(stage,f'/Asset/link_{len(spec["mdh"])}/Interfaces/SourceFlange').AddTransformOp().Set(Gf.Matrix4d(flange.T.tolist()))
    stage.GetRootLayer().Save();stage=None
    write(output/'source-parameters.json',spec);write(output/'source-header.json',{'values':parsed['header'].tolist()})
    check=validate(entry,source);write(output/'joint-validation.json',check)
    record={'source_path':str(source.resolve()),'source_sha256':parsed['source_sha256'],'entry':str(entry.resolve()),'output':str(output.resolve()),
        'triangles':sum(x['triangles'] for x in check['source_geometry_and_materials']),'materials':len(mats),'joint_count':len(spec['mdh']),'link_count':len(parsed['links']),
        'status':'passed_offline_joint_assembly','dynamics':'not_run','native_equivalence':'not_run',**(metadata or {})}
    write(output/'conversion.json',record);return record

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--source',required=True);ap.add_argument('--output',required=True);ap.add_argument('--native-delivery-name');ap.add_argument('--manufacturer');ap.add_argument('--thumbnail');a=ap.parse_args()
    print(json.dumps(convert(a.source,a.output),ensure_ascii=False))
    if a.native_delivery_name:
        from deliver_conversion import deliver
        print(json.dumps(deliver(a.output,a.source,'robodk',a.native_delivery_name,manufacturer=a.manufacturer,thumbnail=a.thumbnail),ensure_ascii=False))
