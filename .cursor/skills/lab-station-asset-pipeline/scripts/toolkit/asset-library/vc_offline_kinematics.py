"""Solver-neutral normalization of a deliberately narrow serialized VC Dof subset.

No VC runtime, historical poses, dynamic solver, controller execution or geometry
snapshot inputs. Unknown rest/offset/coupling semantics are rejected explicitly.
"""
from pathlib import Path
import argparse,hashlib,json,math
import numpy as np
from vc_offline_geometry import Scope,apply,write

def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

def normalize(geometry,physics):
    source=geometry['source']['archive_sha256']
    if source!=physics['source_sha256'] or geometry['source']['rsc_sha256']!=physics['rsc_sha256']:raise ValueError('Geometry and physics exact source identity differs')
    if geometry['coordinate_contract']['metres_per_source_unit']!=.001:raise ValueError('Only explicit millimetre geometry contract supported')
    nodes={n['id']:n for n in geometry['nodes']};scopes={};joints=[];gaps=[]
    def node_scope(node):
        if node['id'] not in scopes:scopes[node['id']]=Scope(node['source_properties'],node_scope(nodes[node['parent_id']]) if node['parent_id'] else None)
        scope=scopes[node['id']]
        if not scope.namespaces:
            scope.namespaces={name:Scope(fields,scope) for name,fields in node.get('source_namespaces',{}).items()}
        return scope
    for record in physics['joints_and_drivers']:
        owner=record['owner'];fields=record['fields']
        if record.get('classification')=='parsed_fixed_node_declaration':continue
        try:
            custom=None;effective_type=owner['type']
            if owner['kind']=='Dof' and effective_type=='Custom':
                from vc_offline_dof import single_axis_custom
                custom=single_axis_custom(fields);effective_type=custom['effective_type']
            if owner['kind']!='Dof' or effective_type not in ('Rotational','Translational'):raise ValueError('Unsupported source Dof type')
            node=nodes[owner['node_id']]
            if not node['parent_id']:raise ValueError('Dof child has no explicit parent')
            if any(j['child_node_id']==node['id'] for j in joints):raise ValueError('Multiple Dof on one node unsupported')
            if not node.get('source_pose_evaluated'):raise ValueError('Source Offset/initial joint pose was not evaluated')
            rest=np.asarray(node['local_matrix_source'],float);offset_value=np.asarray(node['offset_matrix_source'],float)
            if not np.allclose(offset_value[:3,:3]@offset_value[:3,:3].T,np.eye(3),atol=1e-8) or np.linalg.det(offset_value[:3,:3])<0:raise ValueError('Scaled/reflected joint offset frame unsupported')
            scope=Scope(fields,node_scope(node));offset=node['source_properties']['Offset']
            if (fields.get('ValueExpression',{}).get('expression') or 'VALUE').strip()!='VALUE':raise ValueError('Nonidentity/coupled ValueExpression unsupported')
            kind='revolute' if effective_type=='Rotational' else 'prismatic';unit='degrees' if kind=='revolute' else 'mm'
            initial=fields['InitialValue'];expected='Angle' if kind=='revolute' else 'Distance'
            if not (custom and initial.get('quantity') is None) and (initial.get('quantity')!=expected or initial.get('magnitude')!=1):raise ValueError('InitialValue source unit declaration unsupported')
            name=scope.get('Name');qinitial=float(scope.get('InitialValue'));qcurrent=float(scope.get(name)) if name in fields else None
            axisvalue=custom['axis_type'] if custom else int(scope.get('AxisType'))
            if axisvalue not in (0,1,2,4,5,6):raise ValueError('Unsupported source axis enum')
            label=('-' if axisvalue&4 else '+')+'XYZ'[axisvalue&3];axis=np.eye(3)[axisvalue&3]*(-1 if axisvalue&4 else 1)
            low,high=float(scope.get('MinLimit')),float(scope.get('MaxLimit'))
            if not all(math.isfinite(v) for v in (low,high,qinitial)) or not low<=qinitial<=high or high<=low:raise ValueError('Invalid source bounds')
            row={'id':owner['id'],'source_name':name,'type':kind,'parent_node_id':node['parent_id'],'child_node_id':node['id'],'axis_parent':axis.tolist(),'source_axis_label':label,'limits_source':[low,high],'initial_source':qinitial,'source_coordinate_units':unit,'source_current':qcurrent,'source_node_rest_matrix':rest.tolist(),'source_offset_matrix':offset_value.tolist(),'offset_source_expression':offset.get('expression'),'coordinate_rule':'row-vector local(q)=axis-motion(q) @ source Offset; native UpdateNode0x180989e60, Multiply0x180b2d450','controller_reference':scope.get('Controller') if 'Controller' in fields else None,'source_fields':fields,'resolved_dependencies':scope.dependencies,'node_evidence':node['evidence'],'status':'normalized_offline_subset','native_equivalence':'not_validated'}
            if kind=='prismatic':row.update(limits_source_mm=[low,high],limits_m=[low*.001,high*.001],initial_source_mm=qinitial,initial_m=qinitial*.001,source_current_mm=qcurrent)
            else:row.update(limits_degrees=[low,high],initial_degrees=qinitial)
            if custom:row['source_custom_dof']=custom
            joints.append(row)
        except (KeyError,TypeError,ValueError) as exc:gaps.append({'owner':owner,'status':'decoder_gap','reason':str(exc)})
    return {'schema':'vc-offline-kinematics/1','source_sha256':source,'rsc_sha256':physics['rsc_sha256'],'status':'normalized_offline_subset' if joints and not gaps else 'partial' if joints else 'unresolved' if gaps else 'not_applicable','joints':joints,'gaps':gaps,'scope':'serialized kinematic declarations and derived poses; not native PhysX joints, OEM control or dynamics; not hardware validation','native_pose_comparison':'not_run','input_policy':'geometry and physics contracts from same exact archive only'}

def world_poses(geometry,normalized,coordinates_mm):
    nodes={n['id']:n for n in geometry['nodes']};bychild={j['child_node_id']:j for j in normalized['joints']};world={};active=set()
    def solve(nid):
        if nid in world:return world[nid]
        if nid in active:raise ValueError('Node cycle')
        active.add(nid);node=nodes[nid];local=np.asarray(node['local_matrix_source'],float).copy()
        if nid in bychild:
            j=bychild[nid];q=float(coordinates_mm.get(j['id'],j['initial_source']))
            if not j['limits_source'][0]<=q<=j['limits_source'][1]:raise ValueError('Coordinate outside normalized source bounds')
            local=motion_matrix(j,q)@np.asarray(j['source_offset_matrix'])
        world[nid]=local@solve(node['parent_id']) if node['parent_id'] else local
        active.remove(nid);return world[nid]
    for nid in nodes:solve(nid)
    return world

def motion_matrix(j,q):
    from vc_offline_expression import evaluate
    sign=-1 if j['source_axis_label'][0]=='-' else 1
    return evaluate(('R' if j['type']=='revolute' else 'T')+j['source_axis_label'][1].lower()+'('+repr(sign*q)+')',lambda key: (_ for _ in ()).throw(KeyError(key)))

def validate_poses(geometry,normalized):
    samples=[];initials=world_poses(geometry,normalized,{})
    nodes={n['id']:n for n in geometry['nodes']}
    for j in normalized['joints']:
        child=j['child_node_id'];parent=j['parent_node_id'];low,high=j['limits_source']
        initial_local=motion_matrix(j,j['initial_source'])@np.asarray(j['source_offset_matrix'])
        if not np.allclose(initial_local,nodes[child]['local_matrix_source'],atol=1e-8):raise ValueError('Authored source initial pose differs from normalized joint rule')
        for label,q in [('lower',low),('midpoint',(low+high)/2),('upper',high)]:
            worlds=world_poses(geometry,normalized,{j['id']:q});actual=worlds[child]@np.linalg.inv(worlds[parent]);expected=motion_matrix(j,q)@np.asarray(j['source_offset_matrix']);error=float(np.max(np.abs(actual-expected)))
            if error>1e-8:raise ValueError('Joint parent/child frame pose readback differs')
            samples.append({'joint_id':j['id'],'sample':label,'q_source':q,'source_units':j['source_coordinate_units'],'child_world_matrix_source_mm':worlds[child].tolist(),'local_frame_residual':error,'status':'passed_offline_pose_algebra'})
    return {'status':'passed_offline_pose_algebra' if samples else 'not_applicable','samples':samples,'sample_semantics':'three source-coordinate probes with fixed source initial coordinates on other joints; algebra/static reverse evidence, not measured native poses or dynamic simulation','native_pose_comparison':'not_run'}

def apply_kinematics(stage,normalized,node_paths,root='/Asset/SourceKinematics'):
    """Optional disabled USD schema mapping; never applies RigidBody or DriveAPI."""
    from pxr import UsdGeom,UsdPhysics,Sdf,Gf
    if abs(UsdGeom.GetStageMetersPerUnit(stage)-1)>1e-12:raise ValueError('Metre USD stage required')
    UsdGeom.Scope.Define(stage,root);rows=[]
    for index,j in enumerate(normalized['joints']):
        parent=node_paths[j['parent_node_id']];child=node_paths[j['child_node_id']]
        if not stage.GetPrimAtPath(parent) or not stage.GetPrimAtPath(child):raise ValueError('Missing explicit source-node USD mapping')
        constructor=UsdPhysics.RevoluteJoint if j['type']=='revolute' else UsdPhysics.PrismaticJoint
        joint=constructor.Define(stage,root+'/J_'+str(index));joint.CreateBody0Rel().SetTargets([Sdf.Path(parent)]);joint.CreateBody1Rel().SetTargets([Sdf.Path(child)]);joint.CreateAxisAttr(j['source_axis_label'][1]);joint.CreateJointEnabledAttr(False)
        limits=j['limits_degrees'] if j['type']=='revolute' else j['limits_m']
        if j['source_axis_label'][0]=='-':limits=[-limits[1],-limits[0]]
        joint.CreateLowerLimitAttr(limits[0]);joint.CreateUpperLimitAttr(limits[1])
        frame=np.asarray(j['source_offset_matrix'],float);rotation=Gf.Matrix3d(frame[:3,:3].tolist()).ExtractRotation().GetQuat()
        joint.CreateLocalPos0Attr(Gf.Vec3f(*(frame[3,:3]*.001)));joint.CreateLocalRot0Attr(Gf.Quatf(rotation));joint.CreateLocalPos1Attr(Gf.Vec3f(0));joint.CreateLocalRot1Attr(Gf.Quatf(1))
        joint.GetPrim().SetCustomData({'source_sha256':normalized['source_sha256'],'source_dof_id':j['id'],'scope':'disabled derived kinematic mapping only; no native constraint or actuator qualification','normalized_joint_json':json.dumps(j,ensure_ascii=False)})
        rows.append({'joint_id':j['id'],'usd_path':str(joint.GetPath()),'enabled':False})
    return rows

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--geometry',required=True);p.add_argument('--physics',required=True);p.add_argument('--output',required=True);a=p.parse_args();out=Path(a.output);out.mkdir(parents=True,exist_ok=False);g=json.loads(Path(a.geometry).read_text(encoding='utf8'));ph=json.loads(Path(a.physics).read_text(encoding='utf8'));normalized=normalize(g,ph);normalized['input_files']={str(Path(v).resolve()):digest(v) for v in (a.geometry,a.physics)};write(out/'joints.json',normalized);write(out/'pose-validation.json',validate_poses(g,normalized));print(normalized['status'],len(normalized['joints']),'joints',len(normalized['gaps']),'gaps')


def validate_usd_frames(stage, geometry, normalized, node_paths, mappings):
    """Read reopened USD against serialized source matrices, not USD self-consistency."""
    from pxr import UsdGeom, UsdPhysics, Gf
    cache=UsdGeom.XformCache(); expected_world={}; node_rows=[]; joint_rows=[]
    for node in geometry['nodes']:
        expected=np.asarray(node['local_matrix_source'],float).copy(); expected[3,:3]*=.001
        parent=node['parent_id']; expected_world[node['id']]=expected@expected_world[parent] if parent else expected
        prim=stage.GetPrimAtPath(node_paths[node['id']]); xf=UsdGeom.Xformable(prim)
        local=np.asarray(xf.GetLocalTransformation(),float); world=np.asarray(cache.GetLocalToWorldTransform(prim),float)
        error=max(float(np.max(abs(local-expected))),float(np.max(abs(world-expected_world[node['id']]))))
        if error>2e-6:raise ValueError('Source-to-USD node frame mismatch: '+node['id'])
        node_rows.append({'node_id':node['id'],'usd_path':str(prim.GetPath()),'maximum_matrix_error':error,'pose_basis':'evaluated Offset/InitialValue' if node.get('source_pose_evaluated') else 'explicit serialized Matrix'})
    byid={j['id']:j for j in normalized['joints']}
    for mapping in mappings:
        j=byid[mapping['joint_id']]; prim=stage.GetPrimAtPath(mapping['usd_path']); cls=UsdPhysics.RevoluteJoint if j['type']=='revolute' else UsdPhysics.PrismaticJoint; joint=cls(prim)
        if list(joint.GetBody0Rel().GetTargets())!=[node_paths[j['parent_node_id']]] or list(joint.GetBody1Rel().GetTargets())!=[node_paths[j['child_node_id']]]:raise ValueError('Joint endpoint readback mismatch')
        if joint.GetJointEnabledAttr().Get() or joint.GetAxisAttr().Get()!=j['source_axis_label'][1]:raise ValueError('Joint axis/enabled readback mismatch')
        def frame(pos,rot):
            m=Gf.Matrix4d(1); m.SetRotate(Gf.Quatd(rot)); m.SetTranslateOnly(Gf.Vec3d(pos)); return np.asarray(m,float)
        f0=frame(joint.GetLocalPos0Attr().Get(),joint.GetLocalRot0Attr().Get());f1=frame(joint.GetLocalPos1Attr().Get(),joint.GetLocalRot1Attr().Get())
        expected=np.asarray(j['source_offset_matrix'],float).copy();expected[3,:3]*=.001
        error=max(float(np.max(abs(f0-expected))),float(np.max(abs(f1-np.eye(4)))))
        bounds=j['limits_degrees'] if j['type']=='revolute' else j['limits_m'];bounds=[-bounds[1],-bounds[0]] if j['source_axis_label'][0]=='-' else bounds
        if error>2e-6 or not np.allclose([joint.GetLowerLimitAttr().Get(),joint.GetUpperLimitAttr().Get()],bounds,atol=2e-6):raise ValueError('Source-to-USD joint frame/bounds mismatch')
        samples=[]
        for q in (j['limits_source'][0],j['initial_source'],j['limits_source'][1]):
            motion=motion_matrix(j,q);motion[3,:3]*=.001
            source_pose=motion@expected; usd_pose=motion@f0
            residual=float(np.max(abs(source_pose-usd_pose)))
            if residual>2e-6:raise ValueError('Joint probe source/ USD mismatch')
            samples.append({'source_coordinate':q,'maximum_matrix_error':residual})
        joint_rows.append({'joint_id':j['id'],'usd_path':mapping['usd_path'],'frame_error':error,'samples':samples,'disabled':True})
    return {'status':'passed','nodes':node_rows,'joints':joint_rows,'scope':'reopened USD versus source declared initial pose and three source-coordinate probes; no native runtime or dynamics equivalence','tolerance':2e-6}
