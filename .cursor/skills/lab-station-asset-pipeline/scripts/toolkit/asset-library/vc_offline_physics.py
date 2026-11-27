"""Source-only VCMX physics/servo/behavior contract; never execute embedded code.

Serialized auto-updated mass properties are caches, not evaluated native actors.
RSC line identities match vc_offline_geometry. No runtime capture is an input.
"""
import argparse, hashlib, json, math, re, zipfile
from pathlib import Path
from vc_offline_source import blocks, props, parent, direct_text, qdecode, top_fields

PHYSICAL={'Mass','CenterOfGravity','Inertia','Material Density','Physics Type',
          'Static Friction','Dynamic Friction','Restitution','Linear Damping','Angular Damping',
          'Enable Mass Property Update','Solver Position Iterations','Solver Velocity Iterations',
          'Collision Group','Scene Id','PhysicsCollider'}
SCRIPT_EFFECT=re.compile(r'createBehavio[u]?r|createComponent|clone\(|deleteComponent|deleteBehavio[u]?r|Physics|physics|\.Mass\b|\.Inertia\b|CenterOfGravity|\.MaxSpeed|\.MaxAcceleration|\.MaxDeceleration|moveJoint|moveTo|\.Value\s*=|\.OnChanged\s*=|\.OnEvaluate\s*=|\.PositionMatrix\s*=|\.Geometry\b|\.rebuild\(')

def sha(data):return hashlib.sha256(data).hexdigest()
def write(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf8')
def header_name(block):
    if '_offline_header_name' not in block:
        match=re.search(r'(?m)^[ \t]*Name[ \t]+("(?:\\.|[^"\\])*")',direct_text(block))
        block['_offline_header_name']=qdecode(match[1]) if match else None
    return block['_offline_header_name']
def ref(block,bs):
    if block is None:return None
    if '_offline_ref' in block:return block['_offline_ref']
    node=block if block['kind']=='Node' else parent(block,bs,'Node')
    ancestors=[];cursor=node
    while cursor is not None:
        ancestors.append({'node_id':'node:'+str(cursor['line']),'name':header_name(cursor)});cursor=parent(cursor,bs,'Node')
    fields=props(block,bs);name=header_name(block) or fields.get('Name',{}).get('value')
    block['_offline_ref']={'id':block['kind'].lower()+':'+str(block['line']),'kind':block['kind'],'type':block['type'],'name':name,'line':block['line'],
                          'node_id':'node:'+str(node['line']) if node else None,'node_path':list(reversed(ancestors))}
    return block['_offline_ref']

def field_rows(block,bs,member):
    result={}
    for child in block['_children']:
        if child['kind']!='Variable':continue
        for name,value in props({'_children':[child]},bs).items():
            if name in result:raise ValueError('Ambiguous duplicate owner property '+name)
            row=dict(value);row.update(archive_member=member,owner=ref(block,bs),classification='parsed_serialized_value',evidence_class='direct_serialized')
            row['raw_block_sha256']=sha(child['text'].encode('utf8'))
            syntax=top_fields(child['text'])
            row['source_settings']=re.findall(r'(?m)^[ \t]*([A-Z_]{3,})\s*$',syntax.get('Settings',{}).get('raw',''))
            if row.get('expression') is not None:row['classification']='unevaluated_source_expression'
            enum=[]
            for m in re.finditer(r'\bStep\s*\{([^{}]*)\}',syntax.get('StepList',{}).get('raw',''),re.S):
                v=re.search(r'(?m)^[ \t]*Value\s+([^\r\n]+)',m[1]);label=re.search(r'\bDescription\s+("(?:\\.|[^"\\])*")',m[1])
                if v and label:
                    try:val=qdecode(v[1].strip())
                    except ValueError:val=v[1].strip()
                    enum.append({'value':val,'label':qdecode(label[1])})
            if enum:
                row['enum']=enum;selected=[e['label'] for e in enum if e['value']==row['value']]
                if len(selected)==1:row['enum_label']=selected[0]
            row['unit_status']='source_quantity_and_magnitude_only' if 'quantity' in row else 'not_declared'
            result[name]=row
    return result

def derive_expressions(block,bs,fields):
    from vc_offline_expression import evaluate
    for name,row in fields.items():
        expr=row.get('expression')
        if not expr:continue
        dependencies=[];active=set()
        def resolve(key):
            scope=block
            while scope is not None:
                values=props(scope,bs)
                if key in values:
                    value=values[key];token=(scope['line'],key)
                    if token in active:raise ValueError('Cyclic source expression '+key)
                    dependencies.append({'owner_id':ref(scope,bs)['id'],'field':key,'line':value['line']})
                    if value.get('expression'):
                        active.add(token)
                        try:return evaluate(value['expression'],resolve)
                        finally:active.remove(token)
                    if isinstance(value.get('value'),(bool,int,float,str,list)):return value['value']
                    raise ValueError('Source parameter is not scalar/vector '+key)
                scope=parent(scope,bs)
            raise ValueError('Unresolved source parameter '+key)
        try:
            active.add((block['line'],name));value=evaluate(expr,resolve)
            if hasattr(value,'tolist'):value=value.tolist()
            row.update(derived_value_source_units=value,classification='derived_whitelist_expression',evidence_class='deterministic_from_file',dependencies=dependencies,
                       evaluation_scope='serialized configuration only; embedded scripts not evaluated')
        except (ValueError,TypeError,KeyError,ArithmeticError) as exc:
            row.update(classification='decoder_gap',resolution_status='semantics_unresolved',
                       potential_resolution='deterministic_from_file_when_expression_semantics_implemented',
                       evaluation_error=str(exc),dependencies=dependencies)
    return fields

def script_contract(block,fields,bs,output_dir):
    source=fields.get('Script',{});code=source.get('value')
    if not isinstance(code,str):return None
    script_path=Path(output_dir)/'scripts'/('rsc-line-'+str(source['line'])+'.py.txt')
    script_path.parent.mkdir(parents=True,exist_ok=True);script_path.write_text(code,encoding='utf8')
    effects=[];function=None
    for number,line in enumerate(code.splitlines(),1):
        match=re.match(r'^def\s+(\w+)\s*\(',line)
        if match:function=match[1]
        if not SCRIPT_EFFECT.search(line) or line.lstrip().startswith('#'):continue
        kind='script_property_or_behavior_reference'
        if re.search(r'createBehavio[u]?r|createComponent|clone\(',line):kind='script_creation_path'
        elif re.search(r'\.\w+(?:\[[^]]+\])?\s*=',line):kind='script_assignment_path'
        elif 'moveJoint' in line or 'moveTo' in line:kind='script_motion_command'
        effects.append({'archive_member':source['archive_member'],'rsc_variable_line':source['line'],
                       'decoded_script_line':number,'function':function,'text':line,'classification':kind,
                       'evidence_class':'direct_serialized','execution':'not_executed',
                       'evaluation_dependency':'requires_dependency_analysis; not automatically an external scenario',
                       'owner':ref(block,bs)})
    return {'owner':ref(block,bs),'source_variable_line':source['line'],'source_member':source['archive_member'],
            'decoded_script':str(script_path.resolve()),'decoded_script_sha256':sha(code.encode('utf8')),
            'analysis':'conservative lexical source analysis; not complete Python control/data flow',
            'effects':effects,'executed':False}

def inspect(source,output_dir,expected_sha256=None):
    source=Path(source).resolve();output_dir=Path(output_dir).resolve();output_dir.mkdir(parents=True,exist_ok=True)
    raw=source.read_bytes();digest=sha(raw)
    if expected_sha256 and expected_sha256!=digest:raise ValueError('Source digest mismatch')
    with zipfile.ZipFile(source) as archive:
        members=[n for n in archive.namelist() if n.lower().endswith('component.rsc')]
        if len(members)!=1:raise ValueError('One component.rsc required')
        member=members[0];info=archive.getinfo(member)
        if info.file_size>64*1024*1024:raise ValueError('Oversized RSC')
        data=archive.read(member)
    text=data.decode('utf-8-sig');bs=blocks(text)
    (output_dir/'component.rsc').write_bytes(data)
    nodes=[ref(b,bs) for b in bs if b['kind']=='Node'];entities=[];colliders=[];controllers=[];joints=[];scoped=[];scripts=[]
    for block in bs:
        if block['kind']=='Variable':continue
        fields=derive_expressions(block,bs,field_rows(block,bs,member));owner=ref(block,bs)
        if block['kind']=='Functionality' and block['type']=='rPythonScript':
            s=script_contract(block,fields,bs,output_dir)
            if s:scripts.append(s)
        selected={k:v for k,v in fields.items() if k in PHYSICAL or k.startswith(('PartPhysics::','Product Properties::'))}
        if selected:scoped.append({'owner':owner,'fields':selected,'scope':'payload_frame_not_body' if block['kind']=='BaseFrame' else 'owner_source_configuration'})
        if block['kind']=='Functionality' and block['type']=='rPhysicsEntity':
            auto=fields.get('Enable Mass Property Update',{}).get('value')==1
            inert=fields.get('Inertia',{}).get('value');positive=isinstance(inert,list) and len(inert)==4 and all(isinstance(x,(int,float)) and x>0 for x in inert[:3])
            for k in ('Mass','Inertia','CenterOfGravity'):
                if k in fields:
                    if auto:
                        fields[k].update(classification='parsed_serialized_auto_update_cache',evidence_class='direct_serialized',
                                         recomputation='deterministic_from_geometry_if_units_semantics_known; native_parity_pending')
            mode=fields.get('Physics Type',{});label=mode.get('enum_label')
            plan={'node_id':owner['node_id'],'owner_id':owner['id'],'mode_source_label':label,
                  'rigid_body_kinematic_candidate':label=='#Kinematic','native_actor_observed':False,
                  'mass_properties_authorable':False,'mass_properties_omitted_reasons':[], 'material_dimensionless_candidates':{}}
            if auto:plan['mass_properties_omitted_reasons'].append('Enable Mass Property Update=1: serialized cache is not final; deterministic offline recomputation is possible once geometry/unit semantics are established, with engine parity validation pending')
            plan['recomputation_class']='deterministic_from_file'
            plan['native_equivalence_class']='engine_parity_pending'
            if not positive:plan['mass_properties_omitted_reasons'].append('Serialized inertia xyz is absent or nonpositive; fourth lane is preserved, not interpreted as mass')
            plan['mass_properties_omitted_reasons'].append('No source-proven SI inertia/density unit conversion; Quantity/Magnitude alone is not a unit definition')
            for k,target in [('Static Friction','staticFriction'),('Dynamic Friction','dynamicFriction'),('Restitution','restitution')]:
                value=fields.get(k,{}).get('value')
                if isinstance(value,(int,float)) and math.isfinite(value) and value>=0 and (k!='Restitution' or value<=1):
                    plan['material_dimensionless_candidates'][target]={'value':value,'field':k,'line':fields[k]['line'],'classification':'parsed_source_material_coefficient','scope':'design-time entity default; native contact shape binding not observed'}
            entities.append({'owner':owner,'fields':fields,'auto_mass_update':auto,'serialized_inertia_positive':positive,'usd_integration_plan':plan})
        if block['kind']=='Feature' and 'PhysicsCollider' in fields:
            colliders.append({'owner':owner,'fields':fields,'scope':'feature collider declaration; no native cooked shape or actor implied'})
        if block['kind'] in ('Joint','Driver','Dof'):
            joints.append({'owner':owner,'fields':fields,'direct_text':direct_text(block),'scope':'source kinematic/driver declaration; not automatically a PhysX joint'})
        if block['kind']=='Node':
            for fixed in re.finditer(r'(?m)^[ \t]*Dof\s+"Fixed"\s*$',direct_text(block)):
                joints.append({'owner':owner,'fields':{},'declaration':'Dof "Fixed"','classification':'parsed_fixed_node_declaration',
                               'scope':'source fixed node relation; no separately instantiated physics joint claimed'})
        if block['kind']=='Functionality' and any(t in (block['type'] or '').lower() for t in ('servo','physics','joint','motion')):
            controllers.append({'owner':owner,'fields':fields,'direct_text':direct_text(block),
                                'scope':'source declared behavior; scripts may modify it when evaluated'})
    # All scalar source parameters are preserved for the shared safe expression
    # evaluator. No embedded Python is imported, compiled, evaluated or executed.
    parameters=[{'owner':ref(b,bs),'fields':field_rows(b,bs,member)} for b in bs if b['kind']=='Node']
    result={'schema':'unilab.vcmx-offline-physics/1','source':str(source),'source_sha256':digest,
            'archive_member':member,'rsc_sha256':sha(data),'node_identity':'component.rsc opening line, with full ancestor line path',
            'nodes':nodes,'entities':entities,'colliders':colliders,'joints_and_drivers':joints,'controllers':controllers,
            'scoped_physical_fields':scoped,'source_parameters':parameters,'embedded_scripts':scripts,
            'counts':{'nodes':len(nodes),'entities':len(entities),'colliders':len(colliders),'joints_and_drivers':len(joints),'scripts':len(scripts)},
            'policy':{'vc_process_started':False,'vc_dll_loaded':False,'embedded_python_executed':False,
                      'historical_runtime_inputs':False,'source_modified':False,
                      'units':'retain Quantity/Magnitude/raw matrix/vector; no historical runtime calibration; do not infer kg-m2 from unitless vector',
                      'dynamics':'No evaluated actor, generated product instances, contact shape cooking or native dynamics claim'}}
    write(output_dir/'physics-contract.json',result);return result

def main():
    p=argparse.ArgumentParser();p.add_argument('--selection',required=True);p.add_argument('--output',required=True);a=p.parse_args()
    output=Path(a.output).resolve();output.mkdir(parents=True,exist_ok=False);records=[]
    for selection in json.loads(Path(a.selection).read_text(encoding='utf8')):
        slug=re.sub('[^a-z0-9]+','-',selection['title'].lower()).strip('-');folder=output/slug
        result=inspect(selection['source'],folder,selection['sha256'])
        records.append({'title':selection['title'],'contract':str(folder/'physics-contract.json'),'source_sha256':result['source_sha256'],'counts':result['counts']})
    write(output/'index.json',{'schema':'unilab.vcmx-offline-physics-batch/1','assets':records})
    print(json.dumps(records,ensure_ascii=False))
if __name__=='__main__':main()
