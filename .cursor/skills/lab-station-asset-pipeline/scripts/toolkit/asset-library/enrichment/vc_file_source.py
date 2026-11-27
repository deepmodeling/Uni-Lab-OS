"""Static VCMX/RSC evidence adapter. Never imports VC or executes source scripts.

Only source-authored physics boxes with literal dimensions and proven owner maps
are authored. Primitive feature definitions, driver expressions, IO/path contracts
remain explicit metadata when their native evaluator/runtime is not reproduced.
"""
import hashlib, io, json, math, re, struct, zipfile
from pathlib import Path
import numpy as np
from pxr import Gf, Sdf, UsdGeom, UsdPhysics, UsdShade

KINDS = 'Node|Feature|Functionality|Variable|Joint|Driver|Connection|Section|TemplateSection|rSimFlowField|rSimProcessorField|BaseFrame'
DECL = re.compile(r'(?m)^[ \t]*('+KINDS+r')\s*("(?:\\.|[^"\\])*")?\s*\{')
TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|[{}]', re.S)

def sha(data): return hashlib.sha256(data).hexdigest()
def qdecode(value): return json.loads(re.sub(r'\\\r?\n','',value))

def blocks(text):
    result=[]
    for m in DECL.finditer(text):
        depth=1; opening=m.end()-1
        for tok in TOKEN.finditer(text,opening+1):
            if tok[0]=='{': depth+=1
            elif tok[0]=='}':
                depth-=1
                if depth==0:
                    result.append({'kind':m[1],'type':json.loads(m[2]) if m[2] else None,'start':m.start(),
                        'end':tok.end(),'body_start':opening+1,'text':text[opening+1:tok.start()],'line':text.count('\n',0,m.start())+1})
                    break
        else: raise ValueError('Unbalanced source block')
    stack=[]
    for block in result:
        while stack and stack[-1]['end']<block['end']:stack.pop()
        block['_parent']=stack[-1] if stack else None
        block['_children']=[]
        if stack:stack[-1]['_children'].append(block)
        stack.append(block)
    return result

def parent(block, all_blocks, kind=None):
    owner=block.get('_parent')
    while owner is not None and kind is not None and owner['kind']!=kind:owner=owner.get('_parent')
    return owner

def props(block, all_blocks):
    if '_props' in block:return block['_props']
    result={}
    for var in block['_children']:
        if var['kind']!='Variable':continue
        name=re.search(r'\bName\s+"([^"\n]+)"',var['text'])
        if not name:continue
        expression=re.search(r'\bExpression\s+("(?:\\.|[^"\\])*")',var['text'],re.S)
        value=re.search(r'(?m)^\s*Value\s+([^\r\n]+)',var['text'])
        raw=value[1].strip() if value else None
        quoted=re.search(r'\bValue\s+("(?:\\.|[^"\\])*")',var['text'],re.S)
        if quoted:raw=quoted[1]
        decoded=None
        list_value=re.search(r'\bValue\s*\{([^{}]*)\}',var['text'])
        if list_value:
            raw=list_value[1].strip()
            decoded=[qdecode(x) for x in re.findall(r'"(?:\\.|[^"\\])*"',raw)]
            if raw and not decoded:decoded={'unresolved_source_list':raw}
        if raw and raw!='{':
            if not list_value:
                try:decoded=qdecode(raw)
                except ValueError:
                    try:decoded=[float(x) for x in raw.split()]
                    except ValueError:decoded=raw
        row={'value':decoded,'expression':qdecode(expression[1]) if expression else None,'line':var['line'],'type':var['type']}
        for key in ('Quantity','Magnitude'):
            match=re.search(r'(?m)^\s*'+key+r'\s+([^\r\n]+)',var['text'])
            if match:
                try:row[key.lower()]=json.loads(match[1])
                except ValueError:row[key.lower()]=match[1]
        if name[1] in result:raise ValueError('Duplicate property: '+name[1])
        result[name[1]]=row
    block['_props']=result
    return result

def _name(block, bs):
    if block['kind']=='Node':
        m=re.search(r'(?m)^Name\s+"([^"\n]+)"',block['text']);return m[1] if m else None
    return props(block,bs).get('Name',{}).get('value')

def _literal(field):
    value=field['expression'] if field.get('expression') is not None else field.get('value')
    try:value=float(value)
    except (ValueError,TypeError):raise ValueError('Nonliteral source expression')
    if not math.isfinite(value):raise ValueError('Nonfinite source value')
    return value

def direct_text(block):
    value=block['text'];offset=block['body_start']
    for child in reversed(block['_children']):
        lo=child['start']-offset;hi=child['end']-offset
        value=value[:lo]+' '*(hi-lo)+value[hi:]
    return value

def _matrix(block):
    match=re.search(r'(?m)^Matrix\s+([^\r\n]+)',direct_text(block))
    matrix=np.array([float(x) for x in match[1].split()]).reshape(4,4) if match else np.eye(4)
    if not np.isfinite(matrix).all() or not np.allclose(matrix[:,3],[0,0,0,1]) or not np.allclose(matrix[:3,:3]@matrix[:3,:3].T,np.eye(3),atol=1e-8):
        raise ValueError('Unverified source transform')
    return matrix

def decode_3ds(data):
    """Bounded standard 3DS vertex/triangle records; source feature transforms separate."""
    if len(data)<6 or struct.unpack_from('<HI',data)!=(0x4d4d,len(data)):raise ValueError('Not a bounded 3DS container')
    meshes=[];chunks=[]
    def walk(start,end,current=None):
        while start<end:
            if start+6>end:raise ValueError('Truncated 3DS chunk')
            cid,size=struct.unpack_from('<HI',data,start);stop=start+size
            if size<6 or stop>end:raise ValueError('Invalid 3DS chunk length')
            chunks.append({'id':hex(cid),'offset':start,'bytes':size})
            payload=start+6
            if cid in (0x4d4d,0x3d3d,0x4100):walk(payload,stop,current)
            elif cid==0x4000:
                zero=data.find(b'\0',payload,stop)
                if zero<0:raise ValueError('Unterminated object name')
                obj={'name':data[payload:zero].decode('latin1'),'vertices':None,'faces':None};meshes.append(obj);walk(zero+1,stop,obj)
            elif cid==0x4110 and current is not None:
                if current['vertices'] is not None:raise ValueError('Duplicate vertex chunk in 3DS object')
                count=struct.unpack_from('<H',data,payload)[0]
                if payload+2+count*12!=stop:raise ValueError('Vertex length mismatch')
                arr=np.frombuffer(data,dtype='<f4',count=count*3,offset=payload+2).reshape(-1,3)
                if not np.isfinite(arr).all():raise ValueError('Invalid 3DS coordinates')
                current['vertices']=arr.copy();current['vertex_offset']=payload+2
            elif cid==0x4120 and current is not None:
                if current['faces'] is not None:raise ValueError('Duplicate face chunk in 3DS object')
                count=struct.unpack_from('<H',data,payload)[0];sub=payload+2+count*8
                if sub>stop:raise ValueError('Face length mismatch')
                current['faces']=np.frombuffer(data,dtype='<u2',count=count*4,offset=payload+2).reshape(-1,4)[:,:3].copy()
                current['face_offset']=payload+2
                if sub<stop:walk(sub,stop,current)
            elif cid==0x4160 and current is not None:
                if size!=54:raise ValueError('Unrecognized 3DS local transform')
                current['local_transform_12f']=list(struct.unpack_from('<12f',data,payload))
            start=stop
        if start!=end:raise ValueError('3DS range not consumed')
    walk(0,len(data))
    for mesh in meshes:
        if mesh['vertices'] is None or mesh['faces'] is None:raise ValueError('Incomplete 3DS mesh')
        if len(mesh['faces']) and int(mesh['faces'].max())>=len(mesh['vertices']):raise ValueError('3DS face index outside vertices')
    return meshes,chunks

def enrich(stage, candidate, package_root):
    root=Path(package_root);rsc=root/'attachments/vc/component.rsc'
    if not rsc.is_file():return [{'adapter':'vc_file_source','status':'skipped','reason':'RSC absent'}]
    original=list((root/'sources/original').glob('*.vcmx'))
    if len(original)!=1:raise ValueError('Require one original VCMX')
    original=original[0];original_sha=sha(original.read_bytes())
    if original_sha not in {s.get('sha256') for s in candidate['sources']}:raise ValueError('Original VCMX identity mismatch')
    with zipfile.ZipFile(original) as z:
        source_bytes=z.read('component.rsc')
        if source_bytes!=rsc.read_bytes():raise ValueError('RSC differs from original VCMX member')
        inventory=[]
        for entry in z.infolist():
            if entry.file_size>128*1024*1024:raise ValueError('Oversized VCMX member')
            b=z.read(entry.filename)
            if b[:2]==b'MM':
                meshes,chunks=decode_3ds(b)
                inventory.append({'member':entry.filename,'sha256':sha(b),'meshes':len(meshes),'vertices':sum(len(m['vertices']) for m in meshes),
                    'triangles':sum(len(m['faces']) for m in meshes),'validated_indices':True,'scope':'raw 3DS local meshes; feature evaluator not reproduced'})
    # Keep source text intact so line numbers are exact; decode continuations only in string values.
    text=source_bytes.decode('utf-8')
    bs=blocks(text);mapping=json.loads((root/'attachments/vc/source-to-prim.json').read_text(encoding='utf-8'))
    names={}
    for item in mapping:
        if 'source_node_name' not in item:continue
        name=item['source_node_name']
        if name in names:raise ValueError('Duplicate source node mapping')
        prim=stage.GetPrimAtPath(item['usd_prim'])
        if not prim or prim.GetCustomDataByKey('vcNodeName')!=name:raise ValueError('USD node identity mapping mismatch: '+name)
        names[name]=prim
    if abs(UsdGeom.GetStageMetersPerUnit(stage)-1)>1e-12:raise ValueError('Expected metre-normalized USD')
    changes=[];plans=[]
    def record(status,field,value=None,source_line=None,**extra):
        row={'adapter':'vc_file_source','status':status,'field':field,'source_sha256':original_sha,'rsc_sha256':sha(source_bytes),'source':'attachments/vc/component.rsc'}
        if value is not None:row['value']=value
        if source_line:row['source_line']=source_line
        row.update(extra);changes.append(row)
    record('decoded','3ds_members',inventory)
    behaviors=[b for b in bs if b['kind']=='Functionality']
    probe_path=root/'attachments/vc/physics-runtime.json';probe=json.loads(probe_path.read_text(encoding='utf-8')) if probe_path.exists() else {}
    calibration={e['node']:e for e in probe.get('capture',{}).get('entities',[])} if probe.get('source_sha256')==original_sha else {}
    for behavior in behaviors:
        owner=parent(behavior,bs,'Node');owner_name=_name(owner,bs);fields=props(behavior,bs);prim=names.get(owner_name)
        if behavior['type']=='rPhysicsEntity' and prim:
            # Direct RSC values, with unit semantics anchored by existing source-matched calibration.
            entry=calibration.get(owner_name);mass=fields.get('Mass',{});cog=fields.get('CenterOfGravity',{}).get('value');inertia=fields.get('Inertia',{}).get('value')
            if not entry or mass.get('quantity')!='Mass' or mass.get('magnitude')!=1000:
                record('withheld','mass_from_RSC',source_line=behavior['line'],reason='Missing source-matched historical unit calibration');continue
            runtime_mass=next(p for p in entry['properties'] if p['Name']=='Mass')
            if runtime_mass['Value']!=mass['value'] or runtime_mass['unit_metadata']['factor']!=1000:raise ValueError('RSC mass/calibration conflict')
            kg=float(mass['value'])/1000
            if kg<=0 or not math.isfinite(kg) or not isinstance(cog,list) or len(cog)!=16 or not isinstance(inertia,list) or len(inertia)!=4:raise ValueError('Invalid source physical values')
            mm=np.array(cog).reshape(4,4);diag=np.array(inertia[:3])
            if not np.allclose(mm[:3,:3]@mm[:3,:3].T,np.eye(3),atol=1e-8) or not np.allclose(mm[:,3],[0,0,0,1]) or not np.isfinite(mm).all() or not np.isfinite(diag).all() or (diag<=0).any() or max(diag)>sum(diag)-max(diag)+1e-9:raise ValueError('Invalid source inertia/COG')
            if not prim.HasAPI(UsdPhysics.RigidBodyAPI):raise ValueError('Expected previously source-authored rigid body')
            plans.append(('mass',prim,kg,mm,diag,behavior,fields))
            record('verified_direct_source','mass_inertia_com',{'mass_kg':kg,'center_of_mass_m':(mm[3,:3]*.001).tolist(),'principal_inertia_kg_m2':diag.tolist()},mass['line'],
                target=str(prim.GetPath()),conversion='RSC Mass / Magnitude=1000; source-matched historical vcUnit kg calibration; COG mm to m; Inertia xyz only; fourth vector lane retained, not mass')
            record('withheld','physics:density',fields.get('Material Density',{}).get('value'),reason='Density unit unresolved; no inferred material class/density')
        if behavior['type']=='rPhysicsPath' and prim:
            speed=fields.get('Speed',{}).get('value')
            if isinstance(speed,(int,float)) and math.isfinite(speed):plans.append(('metadata',prim,'unilab:vc:physicsPathSpeedMps',Sdf.ValueTypeNames.Double,float(speed)*.001))
            record('authored_metadata','PhysicsPath',{'speed_m_s':float(speed)*.001,'source_properties':fields},behavior['line'],reason='No moving-surface solver or transport behavior implied')
    # Source script text supports the BOX selection; inspect it, never execute it.
    scripts=[p['value'] for b in behaviors for n,p in props(b,bs).items() if n=='Script' and isinstance(p.get('value'),str)]
    script='\n'.join(scripts)
    for feature in [b for b in bs if b['kind']=='Feature']:
        fp=props(feature,bs);ft=fp.get('PhysicsCollider',{}).get('value')
        if ft is None:continue
        owner=parent(feature,bs,'Node');owner_name=_name(owner,bs);prim=names.get(owner_name)
        name=fp.get('Name',{}).get('value')
        if feature['type']!='rPrimitiveBoxFeature' or ft!=2 or not prim:
            record('withheld','feature_collider',{'feature':name,'type':feature['type'],'enum':ft},feature['line'],reason='Only source explicit primitive box subset supported');continue
        if not re.search(r"createBlock\([^\n]+['\"]"+re.escape(name)+r"['\"][^\n]*\)\s*\n\s*setupPhysics\(dummy, VC_PHYSICSCOLLIDER_BOX\)",script):
            raise ValueError('BOX enum cannot be linked to source constructor: '+str(name))
        dimensions=np.array([_literal(fp[k]) for k in ('Length','Width','Height')])*.001
        if (dimensions<=0).any():raise ValueError('Invalid source box dimensions')
        matrix=_matrix(feature);ancestor=parent(feature,bs,'Feature')
        while ancestor:
            ap=props(ancestor,bs)
            expression=ap.get('Expression',{})
            expr=expression.get('expression') if expression.get('expression') is not None else expression.get('value')
            if ancestor['type']!='rTransformFeature' or expr not in ('',None):raise ValueError('Unresolved collider ancestor evaluator')
            matrix=matrix@_matrix(ancestor);ancestor=parent(ancestor,bs,'Feature')
        matrix[3,:3]*=.001
        if not prim.HasAPI(UsdPhysics.RigidBodyAPI):raise ValueError('Collider owner lacks source-authored rigid body')
        plans.append(('box',prim,name,dimensions,matrix,feature))
        record('authored','source_box_collider',{'name':name,'dimensions_m':dimensions.tolist(),'source_corner_transform':matrix.tolist(),'source_visible':fp.get('Visible',{}).get('value')},feature['line'],target=str(prim.GetPath()),
            basis='RSC primitive dimensions + Matrix; source createBlock and setupPhysics(BOX); feature origin treated as minimum corner, retains source top/bottom construction')
    for driver in [b for b in bs if b['kind']=='Driver']:
        fields=props(driver,bs);record('decoded_metadata','driver',fields,driver['line'],reason='Kinematic speed/limits not force, torque, stiffness or damping')
        plans.append(('metadata',stage.GetDefaultPrim(),'unilab:vc:sourceDriver',Sdf.ValueTypeNames.String,json.dumps(fields,ensure_ascii=False)))
    # Retain source behavior/controller/IO contract in inspectable JSON metadata.
    contract_blocks=[b for b in bs if b['kind'] in ('Functionality','Connection','Joint','BaseFrame')]
    contracts=[{'kind':b['kind'],'type':b['type'],'source_line':b['line'],'properties':{k:v for k,v in props(b,bs).items() if k!='Script'},
                'direct_records':dict(re.findall(r'(?m)^[ \t]*(JointType|ValueSource|Frame|ChildNodeLink)\s+([^\r\n]+)',direct_text(b)))} for b in contract_blocks]
    plans.append(('metadata',stage.GetDefaultPrim(),'unilab:vc:sourceBehaviorContracts',Sdf.ValueTypeNames.String,json.dumps(contracts,ensure_ascii=False)))
    record('authored_metadata','source_behavior_contracts',contracts,reason='No source Python or IO controller executed')
    node_parameters=[{'source_node':_name(b,bs),'source_line':b['line'],'properties':props(b,bs)} for b in bs if b['kind']=='Node']
    plans.append(('metadata',stage.GetDefaultPrim(),'unilab:vc:sourceNodeParameters',Sdf.ValueTypeNames.String,json.dumps(node_parameters,ensure_ascii=False)))
    record('authored_metadata','source_node_parameters',node_parameters,reason='Literal values and expressions retained; parameter change is not an evaluated geometry update')
    for b in bs:
        if b['kind']=='Feature':
            fields=props(b,bs)
            matrix_source=re.search(r'(?m)^Matrix\s+([^\r\n]+)',direct_text(b))
            record('decoded_metadata','feature_definition',{'type':b['type'],'properties':fields,'matrix_literal':matrix_source[1] if matrix_source else None},b['line'],reason='Source definition retained; generic feature/expression evaluator not implemented')
    for plan in plans:
        if plan[0]=='mass':
            _,prim,kg,mm,diag,_,_=plan;api=UsdPhysics.MassAPI.Apply(prim);api.CreateMassAttr(kg);api.CreateCenterOfMassAttr(Gf.Vec3f(*(mm[3,:3]*.001)));api.CreateDiagonalInertiaAttr(Gf.Vec3f(*diag));api.CreatePrincipalAxesAttr(Gf.Quatf(Gf.Matrix3d(*mm[:3,:3].reshape(-1)).ExtractRotation().GetQuat()))
        elif plan[0]=='metadata':_,prim,key,typ,value=plan;prim.CreateAttribute(key,typ,custom=True).Set(value)
        elif plan[0]=='box':
            _,prim,name,dims,matrix,feature=plan
            scope=prim.GetPath().AppendChild('SourceColliders');UsdGeom.Scope.Define(stage,scope)
            cube=UsdGeom.Cube.Define(stage,scope.AppendChild(re.sub(r'[^A-Za-z0-9_]','_',name)));cube.CreateSizeAttr(1)
            transform=np.eye(4);transform[:3,:3]=np.diag(dims);transform[3,:3]=dims*.5;transform=transform@matrix
            xf=UsdGeom.Xformable(cube);xf.MakeMatrixXform().Set(Gf.Matrix4d(transform.tolist()));cube.CreatePurposeAttr(UsdGeom.Tokens.guide)
            UsdPhysics.CollisionAPI.Apply(cube.GetPrim()).CreateCollisionEnabledAttr(True)
            cube.GetPrim().SetCustomData({'vcSourceFeature':name,'sourceLine':feature['line'],'sourceHash':original_sha,'dynamicTaskVerified':False})
    return changes
