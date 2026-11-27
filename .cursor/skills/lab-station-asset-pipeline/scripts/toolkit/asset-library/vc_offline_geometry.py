"""Source-only VCMX feature geometry interpreter; never loads VC or its DLLs.

Raw archive meshes and source fields are preserved separately from deterministic
derived geometry. Unsupported feature semantics are explicit decoder gaps.
"""
from pathlib import Path
import argparse,hashlib,json,math,re,struct,zipfile
import numpy as np
from scipy.spatial import ConvexHull
from enrichment.vc_file_source import decode_3ds
from vc_offline_source import blocks,props,parent,direct_text,_name
from vc_offline_expression import evaluate,UnresolvedExpression

def sha(data):return hashlib.sha256(data).hexdigest()
def serial(v):
    if isinstance(v,np.ndarray):return v.tolist()
    if isinstance(v,np.generic):return v.item()
    if isinstance(v,dict):return {k:serial(x) for k,x in v.items()}
    if isinstance(v,(list,tuple)):return [serial(x) for x in v]
    return v
def write(path,data):Path(path).write_text(json.dumps(serial(data),ensure_ascii=False,separators=(',',':'),allow_nan=False),encoding='utf8')
def source_matrix(b):
    m=re.search(r'(?m)^[ \t]*Matrix\s+([^\r\n]+)',direct_text(b))
    a=np.array([float(x) for x in m[1].split()]).reshape(4,4) if m else np.eye(4)
    if not np.isfinite(a).all() or not np.allclose(a[:,3],[0,0,0,1]):raise ValueError('Non-affine source matrix')
    return a
def apply(points,m):return np.asarray(points)@m[:3,:3]+m[3,:3]

def clean_derived_mesh(points,faces):
    """Weld analytic periodic seams/poles; never called on archive mesh data."""
    points=np.asarray(points,float);faces=np.asarray(faces,int)
    rounded=np.round(points,10);unique,first,inverse=np.unique(rounded,axis=0,return_index=True,return_inverse=True);vertices=points[first];mapped=inverse[faces]
    area=np.linalg.norm(np.cross(vertices[mapped[:,1]]-vertices[mapped[:,0]],vertices[mapped[:,2]]-vertices[mapped[:,0]]),axis=1)
    keep=area>1e-12
    return vertices,mapped[keep],{'scope':'derived analytic/sweep vertices only; raw 3DS untouched','weld_decimal_places_mm':10,'removed_degenerate_triangles':int(sum(~keep)),'original_derived_vertices':len(points),'welded_vertices':len(vertices),'doubled_area_cutoff_mm2':1e-12}

class Scope:
    def __init__(self,fields,outer=None):self.fields=fields;self.outer=outer;self.cache={};self.active=set();self.dependencies=[];self.namespaces={}
    def get(self,name):
        if name in self.cache:return self.cache[name]
        if name not in self.fields:
            if '::' in name:
                namespace,field=name.split('::',1)
                if namespace in self.namespaces:return self.namespaces[namespace].get(field)
            if self.outer:return self.outer.get(name)
            raise UnresolvedExpression('Unknown source property '+name)
        if name in self.active:raise UnresolvedExpression('Property expression cycle '+name)
        self.active.add(name);f=self.fields[name]
        try:
            v=evaluate(f['expression'],self.get) if f.get('expression') is not None else f.get('value')
            self.cache[name]=v;self.dependencies.append({'name':name,'line':f['line'],'expression':f.get('expression'),'value':serial(v)})
            return v
        finally:self.active.remove(name)
    def value(self,name,default=None):return self.get(name) if name in self.fields else default

def primitive(kind,s):
    """Source primitive parameter geometry, with tessellation counts from RSC."""
    if kind=='rPrimitiveWedgeFeature':
        # Static format evidence: Revolution CreatePolyWedge 0x180244160;
        # base vertices 0x18024420d..277, double mirror 0x1802451ad..2aa.
        # Inputs retain their named values even if SmallerLength>BiggerLength.
        a,b,c,d,w=[float(s.get(x)) for x in ('BiggerLength','SmallerLength','BiggerHeight','SmallerHeight','Width')]
        if min(a,b,c,d,w)<=0:raise ValueError('Degenerate wedge branches not implemented')
        back,down=bool(s.get('MirrorBack')),bool(s.get('MirrorDown'))
        if back and down:profile=[(b,-c),(a,-d),(a,d),(b,c),(-b,c),(-a,d),(-a,-d),(-b,-c)]
        elif back:profile=[(-a,0),(a,0),(a,d),(b,c),(-b,c),(-a,d)]
        elif down:profile=[(0,-c),(b,-c),(a,-d),(a,d),(b,c),(0,c)]
        else:profile=[(0,0),(a,0),(a,d),(b,c),(0,c)]
        n=len(profile);p=np.array([[x,y,z] for y in (0,w) for x,z in profile]);f=[]
        for i in range(1,n-1):f.extend([[0,i,i+1],[n,n+i+1,n+i]])
        for i in range(n):j=(i+1)%n;f.extend([[i,n+i,n+j],[i,n+j,j]])
        return p,np.array(f)
    if kind=='rPrimitiveBoxFeature':
        l,w,h=[float(s.get(x)) for x in ('Length','Width','Height')]
        if min(l,w,h)<0:raise ValueError('Negative primitive dimension')
        p=np.array([[0,0,0],[l,0,0],[l,w,0],[0,w,0],[0,0,h],[l,0,h],[l,w,h],[0,w,h]],float)
        f=np.array([[0,2,1],[0,3,2],[4,5,6],[4,6,7],[0,1,5],[0,5,4],[1,2,6],[1,6,5],[2,3,7],[2,7,6],[3,0,4],[3,4,7]])
        return p,f
    if kind in ('rPrimitiveCylinderFeature','rPrimitiveConeFeature','rPrimitiveSphereFeature'):
        start=float(s.value('StartSweep',0));end=float(s.value('EndSweep',360));count=int(s.get('Sections'))
        if count<3 or count>4096 or not 0<end-start<=360.000001:raise ValueError('Unsupported primitive sweep/count')
        full=abs(end-start-360)<1e-6;angles=np.linspace(math.radians(start),math.radians(end),count+1);n=len(angles);p=[];f=[]
        if kind=='rPrimitiveSphereFeature':
            radius=float(s.get('Radius'));spans=int(s.get('Spans'))
            if radius<=0 or not 2<=spans<=4096 or not full:raise ValueError('Unsupported sphere domain')
            for phi in np.linspace(-math.pi/2,math.pi/2,spans+1):
                p.extend([[radius*math.cos(phi)*math.cos(a),radius*math.cos(phi)*math.sin(a),radius*math.sin(phi)] for a in angles])
            for j in range(spans):
                for i in range(count):a=j*n+i;b=a+1;c=a+n;d=c+1;f.extend([[a,b,d],[a,d,c]])
        else:
            rb=float(s.get('Radius') if kind=='rPrimitiveCylinderFeature' else s.get('BottomRadius'));rt=rb if kind=='rPrimitiveCylinderFeature' else float(s.get('TopRadius'));h=float(s.get('Height'))
            if min(rb,rt,h)<0:raise ValueError('Negative primitive dimension')
            p=[[r*math.cos(a),r*math.sin(a),z] for r,z in [(rb,0),(rt,h)] for a in angles]
            for i in range(count):f.extend([[i,i+1,n+i+1],[i,n+i+1,n+i]])
            if s.value('Caps',True):
                p.extend([[0,0,0],[0,0,h]]);bottom,top=2*n,2*n+1
                for i in range(count):f.extend([[bottom,i+1,i],[top,n+i,n+i+1]])
            if not full:
                if not s.value('Fill',False):raise ValueError('Open partial primitive sweep not supported')
                p.extend([[0,0,0],[0,0,h]]);a,b=len(p)-2,len(p)-1;f.extend([[a,0,n],[a,n,b],[a,b,2*n-1],[a,2*n-1,n-1]])
        return np.array(p),np.array(f)
    raise ValueError('No proven primitive decoder for '+kind)

def revolve_solid(points,triangles,angle,count):
    """Offline reconstruction of the proven capped +Z TriMesh sweep subset.

    Static evidence: Revolution.dll bcb4985e..e65135, HelicalSplitMesh
    0x1802979a0, AddFront/BackVertex 0x180294780/0x180294440,
    GenerateRevolutionMesh 0x180296fd0. This is a front/back boundary sweep,
    not a convex hull or a Boolean union of sampled solids. Native byte-for-byte
    tessellation and self-intersection repair are not claimed.
    """
    p,f,unused=clean_derived_mesh(points,triangles)
    if len(f)==0:raise ValueError('Revolve solid has no triangles')
    if not np.isfinite(p).all():raise ValueError('Nonfinite revolve vertices')
    # A closed oriented input makes the native front/back curve construction
    # unambiguous. Open/nonmanifold inputs remain decoder gaps in this subset.
    edges={}
    for ti,tri in enumerate(f):
        for a,b in zip(tri,np.roll(tri,-1)):
            edges.setdefault(tuple(sorted((int(a),int(b)))),[]).append((ti,int(a),int(b)))
    if any(len(v)!=2 or v[0][1:]!=v[1][1:][::-1] for v in edges.values()):
        raise ValueError('Solid revolve needs a closed consistently oriented triangle mesh')
    front=[]
    # Native rFVector operators are float32; the comparisons promote to double.
    # Dot product is checked at a, then b/c only while -1e-4 < dot < 1e-4.
    for tri in f:
        abc=p[tri].astype(np.float32);normal=np.cross(abc[1]-abc[0],abc[2]-abc[1])
        for vertex in abc:
            score=float(np.dot(normal,np.cross(np.array([0,0,1],np.float32),vertex)))
            if not -0.0001<score<0.0001:break
        front.append(score<0.0001)
    front=np.array(front,bool)
    boundary=[]
    for pair in edges.values():
        if front[pair[0][0]]!=front[pair[1][0]]:
            boundary.append(next(v[1:] for v in pair if front[v[0]]))
    if not boundary:raise ValueError('No proven front/back boundary for solid revolve')
    rings=[]
    for theta in np.linspace(0,angle,count+1):
        rings.append(apply(p,evaluate('Rz('+repr(float(theta))+')',lambda name:None)))
    result=np.vstack(rings);n=len(p)
    faces=[tri.tolist() if front[i] else (tri+count*n).tolist() for i,tri in enumerate(f)]
    for a,b in boundary:
        for j in range(count):
            a0,b0,a1,b1=j*n+a,j*n+b,(j+1)*n+a,(j+1)*n+b
            # Reverse the stationary cap's boundary, preserving outward winding.
            faces.extend([[a0,b1,b0],[a0,a1,b1]])
    faces=np.asarray(faces,int);used,inverse=np.unique(faces,return_inverse=True)
    return result[used],inverse.reshape(-1,3)

def revolve(meshes,s):
    """Planar profile sweep or statically proven solid TriMesh sweep subset."""
    if len(meshes)!=1:raise ValueError('Revolve needs exactly one profile or triangle mesh')
    item=meshes[0];original=apply(item['points_mm'],item['_matrix']);p=np.unique(np.round(original,10),axis=0)
    axis=np.asarray(s.get('RevolveAxis'),float);angle=float(s.get('RevolveAngle'));sections=float(s.get('Sections'));count=int(sections);offset=s.value('RevolveOffset',0)
    if isinstance(offset,np.ndarray):offset=0 if np.allclose(offset,np.eye(4)) else None
    if axis.shape!=(3,) or not np.array_equal(axis,[0,0,1]) or offset!=0 or not 0<angle<=360 or not 3<=count<=4096 or count!=sections:raise ValueError('Only finite zero-offset +Z revolve with integer Sections 3..4096 implemented')
    if not np.isfinite(p).all() or len(p)<3:raise ValueError('Invalid revolve points')
    centered=p-p.mean(0);_,sing,vt=np.linalg.svd(centered)
    if sing[-1]>1e-7:
        if angle>=360 or not s.value('Caps',True) or s.value('Reverse',False):raise ValueError('Solid revolve subset requires partial angle, Caps=true and Reverse=false')
        faces=np.asarray(item['triangles'],int)
        if np.linalg.det(item['_matrix'][:3,:3])<0:faces=faces[:,::-1]
        return revolve_solid(original,faces,angle,count)
    if sing[1]<1e-7:raise ValueError('Revolve child is not a planar area')
    projected=centered@vt[:2].T;order=ConvexHull(projected).vertices
    if len(order)!=len(p):raise ValueError('Only convex profile boundaries supported')
    p=p[order];n=len(p);points=[];faces=[]
    for theta in np.linspace(0,angle,count+1):
        m=evaluate('Rz('+repr(float(theta))+')',lambda name:None);points.extend(apply(p,m))
    for j in range(count):
        for i in range(n):k=(i+1)%n;a=j*n+i;b=j*n+k;c=(j+1)*n+i;d=(j+1)*n+k;faces.extend([[a,b,d],[a,d,c]])
    if abs(angle-360)>1e-8 and s.value('Caps',True):
        for i in range(1,n-1):faces.extend([[0,i+1,i],[count*n,count*n+i,count*n+i+1]])
    if s.value('Reverse',False):faces=[f[::-1] for f in faces]
    return np.array(points),np.array(faces)

def extra_3ds(data):
    """Standard 3DS material colors and face groups, with exact byte offsets."""
    materials=[];groups=[];uvs=[]
    def walk(start,end,mat=None,obj=None,field=None):
        while start<end:
            if end-start<6:raise ValueError(f'Truncated 3DS supplement header at {start}, boundary {end}')
            cid,size=struct.unpack_from('<HI',data,start);stop=start+size;pos=start+6
            if size<6 or stop>end:raise ValueError('Invalid 3DS supplement chunk')
            if cid in (0x4d4d,0x3d3d,0x4100):walk(pos,stop,mat,obj)
            elif cid==0xafff:
                row={'evidence':{'byte_offset':start,'byte_length':size}};materials.append(row);walk(pos,stop,row,obj)
            elif cid in (0xa200,0xa204,0xa210,0xa230,0xa33a,0xa33c,0xa33d,0xa33e) and mat is not None:
                mat.setdefault('unresolved_texture_references',[]).append({'chunk_id':hex(cid),'byte_offset':start,'byte_length':size,'reason':'source 3DS texture/map shader not reconstructed'})
            elif cid==0xa000 and mat is not None:mat['name']=data[pos:stop].split(b'\0')[0].decode('latin1')
            elif cid in (0xa010,0xa020,0xa030,0xa040,0xa050) and mat is not None:walk(pos,stop,mat,obj,{0xa010:'ambient',0xa020:'diffuse',0xa030:'specular',0xa040:'shininess_percent',0xa050:'transparency_percent'}[cid])
            elif mat is not None and field:
                if cid in (0x10,0x13):mat[field]=list(struct.unpack_from('<3f',data,pos))
                elif cid in (0x11,0x12):mat[field]=[v/255 for v in data[pos:pos+3]]
                elif cid==0x30:
                    raw=struct.unpack_from('<H',data,pos)[0];mat[field]=raw/256.0
                    mat.setdefault('percentage_encoding',{})[field]={'kind':'VC uint16 Q8 percent','raw_uint16':raw,'scale_to_percent':1/256,'byte_offset':pos,'rule':'VisualComponents.Revolution.3ds.dll 475e649b... writer0x18000EAC7 reader0x18000AA77'}
                elif cid==0x31:
                    mat[field]=struct.unpack_from('<f',data,pos)[0]
                    mat.setdefault('percentage_encoding',{})[field]={'kind':'float32 percent','byte_offset':pos,'scale_to_percent':1,'rule':'VC 3DS reader0x180009A64 float direct read'}
            elif cid==0x4000:
                zero=data.index(0,pos,stop);walk(zero+1,stop,mat,{'name':data[pos:zero].decode('latin1'),'byte_range':[start,stop]})
            elif cid==0x4120:
                count=struct.unpack_from('<H',data,pos)[0];walk(pos+2+count*8,stop,mat,obj)
            elif cid==0x4130:
                zero=data.index(0,pos,stop)
                if zero+3>stop:raise ValueError(f'Truncated 3DS material-group count at {start}')
                count=struct.unpack_from('<H',data,zero+1)[0]
                if zero+3+2*count!=stop:raise ValueError(f'3DS material-group bounds mismatch at {start}: declared {count} indices, available {stop-zero-3} bytes')
                groups.append({'object':obj['name'],'object_byte_range':obj['byte_range'],'material':data[pos:zero].decode('latin1'),'face_indices':list(struct.unpack_from('<'+'H'*count,data,zero+3)),'byte_offset':start})
            elif cid==0x4140:
                count=struct.unpack_from('<H',data,pos)[0];uvs.append({'object':obj['name'],'object_byte_range':obj['byte_range'],'uv':np.frombuffer(data,dtype='<f4',count=count*2,offset=pos+2).reshape(-1,2).tolist(),'byte_offset':start})
            start=stop
    walk(0,len(data));return materials,groups,uvs

def bounded_3ds_member(content):
    """Parse a complete 3DS root; preserve the proven VC duplicate-size trailer."""
    if len(content)<6:raise ValueError('Truncated 3DS header')
    magic,declared=struct.unpack_from('<HI',content)
    if magic!=0x4d4d or declared<6 or declared>len(content):raise ValueError('Invalid/truncated 3DS root bounds')
    proof={'member_bytes':len(content),'declared_root_bytes':declared,'root_sha256':sha(content[:declared]),'trailing_bytes':len(content)-declared}
    if declared==len(content):proof['rule']='exact bounded 3DS container';return content,proof
    if len(content)-declared==4 and declared>=22 and struct.unpack_from('<HI',content,6)==(2,10) and struct.unpack_from('<I',content,12)[0]==3 and struct.unpack_from('<HI',content,16)==(0x3d3d,declared-16) and struct.unpack_from('<I',content,declared)[0]==declared-16:
        proof.update(rule='VC legacy repeated editor-chunk size outside declared root; native Read0x180002F68 compares root size to editor size+16 and exits at0x180003058',trailing_hex=content[declared:].hex(),trailer_preserved_in_original=True,binary_sha256='475e649b76d7b7ad264629f36e5c1505577f657c59827abe7c2d02384c2ec474')
        return content[:declared],proof
    raise ValueError('Unrecognized data beyond declared 3DS root; preserved without guessing')


def evaluate_archive(source,output_dir,expected_sha256=None):
    source=Path(source);output=Path(output_dir);output.mkdir(parents=True,exist_ok=False);data=source.read_bytes();digest=sha(data)
    if expected_sha256 and digest!=expected_sha256:raise ValueError('Source identity mismatch')
    with zipfile.ZipFile(source) as archive:
        members={i.filename:archive.read(i.filename) for i in archive.infolist() if i.file_size<=128*1024*1024}
    text=members['component.rsc'].decode('utf8');bs=blocks(text);rsc_hash=sha(members['component.rsc']);(output/'component.rsc').write_bytes(members['component.rsc'])
    def evidence(b):return {'member':'component.rsc','sha256':rsc_hash,'line_start':b['line'],'line_end':text.count('\n',0,b['end'])+1,'char_start':b['start'],'char_end':b['end']}
    raw=[];materials=[];raw_by_member={};gaps=[];member_containers=[]
    for name,content in members.items():
        if content[:2]!=b'MM':continue
        try:
            bounded,container=bounded_3ds_member(content);member_containers.append(dict(container,member=name,member_sha256=sha(content)))
            meshes,chunks=decode_3ds(bounded);mats,groups,uvs=extra_3ds(bounded);raw_by_member[name]=[]
            for i,m in enumerate(meshes):
                row={'id':name+'#'+str(i),'member':name,'member_sha256':sha(content),'object':m['name'],'source_vertices':m['vertices'],'source_triangles':m['faces'],'local_transform_12f':m.get('local_transform_12f'),'vertex_byte_offset':m['vertex_offset'],'face_byte_offset':m['face_offset'],'face_material_groups':[g for g in groups if g['object_byte_range'][0]<=m['vertex_offset']<g['object_byte_range'][1]],'uv_sets':[u for u in uvs if u['object_byte_range'][0]<=m['vertex_offset']<u['object_byte_range'][1]]};raw.append(row);raw_by_member[name].append(row)
            materials.extend([dict(m,id=name+'#material:'+m.get('name',''),member=name,member_sha256=sha(content)) for m in mats])
        except Exception as e:gaps.append({'member':name,'status':'decoder_gap','reason':str(e)})
    if members.get('materials.dat'):
        mt=members['materials.dat'].decode('utf8')
        for match in re.finditer(r'\bMaterial\s*\{([^{}]*)\}',mt):
            m={'id':'materials.dat:'+str(match.start()),'member':'materials.dat','member_sha256':sha(members['materials.dat']),'source_text':match[0],'line_start':mt.count('\n',0,match.start())+1}
            for line in match[1].splitlines():
                parts=line.strip().split(maxsplit=1)
                if len(parts)!=2:continue
                k,v=parts
                try:m[k.lower()]=json.loads(v) if v.startswith('"') else [float(x) for x in v.split()]
                except ValueError:pass
            materials.append(m)
    scopes={};nodes=[]
    for b in bs:
        if b['kind']!='Node':continue
        par=parent(b,bs,'Node');scope=Scope(props(b,bs),scopes.get(par['start']) if par else None);scopes[b['start']]=scope
        material=re.search(r'(?m)^[ \t]*Material\s+"([^"\n]*)"',direct_text(b))
        nodes.append({'id':'node:'+str(b['line']),'name':_name(b,bs),'source_type':b['type'],'parent_id':'node:'+str(par['line']) if par else None,'local_matrix_source':source_matrix(b),'matrix_convention':'row-vector; translation in row 3; source mm','evidence':evidence(b),'source_properties':props(b,bs),'source_material_name':material[1] if material else None,'visible':bool(scope.value('Visible',True)),'joint_pose_scope':'serialized static node Matrix only; joint driver evaluation is separate'})
        for behavior in (c for c in b['_children'] if c['kind']=='Functionality'):
            fields=props(behavior,bs);name=fields.get('Name',{}).get('value')
            if isinstance(name,str) and name:
                if name in scope.namespaces:raise ValueError('Ambiguous behavior namespace '+name)
                scope.namespaces[name]=Scope(fields,scope);nodes[-1].setdefault('source_namespaces',{})[name]=fields
    node_pose_gaps=[]
    for b,node in zip([b for b in bs if b['kind']=='Node'],nodes):
        if 'Offset' not in props(b,bs):continue
        scope=scopes[b['start']]
        try:
            offset=np.asarray(scope.get('Offset'),float)
            if offset.shape!=(4,4) or not np.isfinite(offset).all() or not np.allclose(offset[:,3],[0,0,0,1]):raise ValueError('Offset is not finite row affine matrix')
            dofs=[c for c in b['_children'] if c['kind']=='Dof'];motion=np.eye(4)
            if len(dofs)>1:raise ValueError('Multiple source Dof on node')
            if dofs:
                dof=dofs[0];fields=props(dof,bs);ds=Scope(fields,scope)
                effective_type=dof['type'];custom=None
                if effective_type=='Custom':
                    from vc_offline_dof import single_axis_custom
                    custom=single_axis_custom(fields);effective_type=custom['effective_type'];node['source_custom_dof']=custom
                if effective_type not in ('Rotational','Translational'):raise ValueError('Unsupported source Dof '+str(dof['type']))
                expression=fields.get('ValueExpression',{}).get('expression')
                if expression is not None and expression.strip()!='VALUE':raise ValueError('Coupled/nonidentity joint value expression')
                initial=float(ds.get('InitialValue'));axis=custom['axis_type'] if custom else int(ds.get('AxisType'))
                if axis not in (0,1,2,4,5,6):raise ValueError('Unknown source joint axis')
                motion=evaluate(('R' if effective_type=='Rotational' else 'T')+'xyz'[axis&3]+'('+repr(initial*(-1 if axis&4 else 1))+')',scope.get)
                node['source_dof_pose']={'id':'dof:'+str(dof['line']),'kind':dof['type'],'effective_kind':effective_type,'axis_type':axis,'initial_value':initial,'units':'degrees' if effective_type=='Rotational' else 'mm','value_policy':'serialized InitialValue; source behavior not executed'}
            node['serialized_matrix_cache']=node['local_matrix_source'];node['offset_matrix_source']=offset.tolist();node['local_matrix_source']=(motion@offset).tolist();node['source_pose_evaluated']=True
            node['joint_pose_scope']='source InitialValue pose from native static rule Joint@Offset (row matrix); no external constraints or source scripts evaluated'
            node['pose_rule']={'binary_sha256':'bcb4985eaa77d51f81d93d6e8720c3db57d388ebf198fc89af084a79a5e65135','update_node':'0x180989e60','postmultiply':'0x180b2dc20','multiply':'0x180b2d450','rotation_axis':'0x180967c50'}
        except (ValueError,TypeError,KeyError) as exc:
            node_pose_gaps.append({'node_id':node['id'],'status':'decoder_gap','reason':str(exc)});node['source_pose_evaluated']=False
    coverage=[];features=[];active_members=set()
    def skipped_feature(b,choice_owner):
        coverage.append({'id':'feature:'+str(b['line']),'type':b['type'],'name':_name(b,bs),'evidence':evidence(b),'status':'inactive_source_switch_branch','selected_by_switch':choice_owner,'source_properties':props(b,bs),'output_mesh_count':0})
        for child in b['_children']:
            if child['kind']=='Feature':skipped_feature(child,choice_owner)
    def evaluate_feature(b,outer):
        s=Scope(props(b,bs),outer);row={'id':'feature:'+str(b['line']),'type':b['type'],'name':_name(b,bs),'evidence':evidence(b),'source_properties':props(b,bs),'status':'evaluated','interpretation':'offline decoder; native tessellation identity not asserted'};coverage.append(row);features.append(row);kind=b['type']
        try:
            matrix=source_matrix(b);visible=bool(s.value('Visible',True));children=[c for c in b['_children'] if c['kind']=='Feature'];items=[]
            if kind=='rSwitchFeature':
                choice=s.get('Choice')
                if isinstance(choice,str):
                    matches=[c for c in children if _name(c,bs)==choice]
                    if len(matches)>1:raise ValueError('Ambiguous source switch child name')
                    selected=matches[0] if matches else None
                elif isinstance(choice,(bool,int,float)) and math.isfinite(choice):
                    index=int(choice)-1;selected=children[index] if 0<=index<len(children) else None
                else:raise ValueError('Unsupported source switch Choice type')
                row['choice_contract']={'value':choice,'selected_feature_id':'feature:'+str(selected['line']) if selected else None,'rule':'native ChooseChild0x1800b2a50: string exact name; bool/int/truncated-double 1-based child; <=0 selects none','binary_sha256':'bcb4985eaa77d51f81d93d6e8720c3db57d388ebf198fc89af084a79a5e65135'}
                for c in children:
                    if c is selected:items.extend(evaluate_feature(c,s))
                    else:skipped_feature(c,row['id'])
            else:
                for c in children:items.extend(evaluate_feature(c,s))
            if kind=='rSwitchFeature':pass
            elif kind=='rTransformFeature':
                exp=s.value('Expression',np.eye(4))
                if not isinstance(exp,np.ndarray) or exp.shape!=(4,4):raise ValueError('Transform expression is not matrix')
                matrix=exp@matrix
            elif kind=='rLinearCloneFeature':
                count=int(s.get('Count'));step=float(s.get('Step'));direction=np.asarray(s.get('Direction'))
                if not 0<=count<=1000 or direction.shape!=(3,):raise ValueError('Invalid clone parameters')
                items=[dict(item,_matrix=item['_matrix']@np.array([[1,0,0,0],[0,1,0,0],[0,0,1,0],[*(direction*step*i),1]]),instance_suffix=item.get('instance_suffix','')+'/linear:'+str(i)) for i in range(count) for item in items]
            elif kind=='rAngularCloneFeature':
                count=int(s.get('Count'));step=float(s.get('Step'));offset=np.asarray(s.get('Offset'))
                if not 0<=count<=1000 or offset.shape!=(3,):raise ValueError('Invalid angular clone parameters')
                instances=[]
                for i in range(count):
                    before=np.eye(4);before[3,:3]=-offset;after=np.eye(4);after[3,:3]=offset;rotation=evaluate('Rz('+repr(step*i)+')',s.get);tf=before@rotation@after
                    instances.extend(dict(item,_matrix=item['_matrix']@tf,instance_suffix=item.get('instance_suffix','')+'/angular:'+str(i)) for item in items)
                items=instances
            elif kind=='rMirrorFeature':
                direction=np.asarray(s.get('Direction'))
                if direction.shape!=(3,) or not np.all(np.isin(direction,[-1,1])):raise ValueError('Unverified mirror direction domain')
                tf=np.diag([*direction,1]);copies=[dict(item,_matrix=item['_matrix']@tf,instance_suffix=item.get('instance_suffix','')+'/mirror') for item in items]
                items=copies if s.value('HideOriginal',False) else items+copies
            elif kind=='rGeoFeature':
                uri=s.get('Uri');items=[];active_members.add(uri)
                if uri not in raw_by_member:raise ValueError('Embedded mesh missing or unsupported: '+str(uri))
                inheritance=re.search(r'\bMaterialInheritance\s+"([^"\n]+)"',direct_text(b))
                for m in raw_by_member[uri]:
                    local=m['local_transform_12f']
                    if local is not None and not np.allclose(local,[1,0,0,0,1,0,0,0,1,0,0,0]):raise ValueError('Nonidentity 3DS local transform semantics not implemented')
                    items.append({'feature_id':row['id'],'raw_mesh_ref':m['id'],'points_mm':m['source_vertices'],'triangles':m['source_triangles'],'_matrix':np.eye(4),'material_id':None,'raw_face_material_groups':m['face_material_groups'],'source_material_inheritance':inheritance[1] if inheritance else None,'visible':True})
            elif kind=='rRevolveFeature':
                points,faces=revolve(items,s);points,faces,cleanup=clean_derived_mesh(points,faces)
                child_points=apply(items[0]['points_mm'],items[0]['_matrix']);solid=np.linalg.matrix_rank(child_points-child_points.mean(0),tol=1e-7)==3
                contract={'method':'front_back_trimesh_boundary_sweep' if solid else 'convex_planar_profile_sweep','angle_degrees':float(s.get('RevolveAngle')),'sections':int(s.get('Sections')),'native_runtime_compared':False,'output_arithmetic':'float64 rotation, derived weld 1e-10 mm; solid face classification float32','max_arc_chord_deviation_mm':float(np.linalg.norm(child_points[:,:2],axis=1).max()*(1-math.cos(math.radians(float(s.get('RevolveAngle')))/int(s.get('Sections'))/2)))}
                if solid:contract['static_format_evidence']={'binary_sha256':'bcb4985eaa77d51f81d93d6e8720c3db57d388ebf198fc89af084a79a5e65135','HelicalSplitMesh':'0x1802979a0','GenerateRevolutionMesh':'0x180296fd0','scope':'subset of native mesh-boundary algorithm; exact native index order/float output not asserted'}
                items=[{'feature_id':row['id'],'points_mm':points,'triangles':faces,'_matrix':np.eye(4),'material_id':s.value('Material'),'visible':True,'derived_from_features':[i['feature_id'] for i in items],'tessellation':'source Sections; convex planar profile or front/back triangle-boundary solid sweep; offline topology, no native parity claim','derived_cleanup':cleanup,'revolve_contract':contract}]
            elif kind=='rFrameFeature':row['status']='frame_only';row['frame_source_text']=direct_text(b);items=[]
            elif kind.startswith('rPrimitive'):
                points,faces=primitive(kind,s);points,faces,cleanup=clean_derived_mesh(points,faces);items=[{'feature_id':row['id'],'points_mm':points,'triangles':faces,'_matrix':np.eye(4),'material_id':s.value('Material'),'visible':True,'tessellation':'source Sections/Spans where present; triangulation offline-derived','derived_cleanup':cleanup}]
            else:raise ValueError('Unknown feature type '+kind)
            for item in items:item['_matrix']=item['_matrix']@matrix;item['visible']=item['visible'] and visible;item.setdefault('feature_chain',[]);item['feature_chain']=item['feature_chain']+[row['id']]
            row['resolved_dependencies']=s.dependencies;row['output_mesh_count']=len(items);row['effective_local_matrix']=matrix;return items
        except Exception as e:row.update(status='decoder_gap',reason=str(e),resolved_dependencies=s.dependencies);return []
    final=[]
    node_by_id={n['id']:n for n in nodes}
    for b,node in zip([b for b in bs if b['kind']=='Node'],nodes):
        scope=scopes[b['start']]
        for f in [c for c in b['_children'] if c['kind']=='Feature']:
            for item in evaluate_feature(f,scope):
                if not item.get('material_id') and not item.get('raw_mesh_ref'):
                    owner=node
                    while owner and not owner['source_material_name']:owner=node_by_id.get(owner['parent_id'])
                    if owner:item['material_id']=owner['source_material_name'];item['material_binding_rule']='empty primitive material inherits nearest ancestor node SimApplyMaterialAttribute';item['material_node_id']=owner['id']
                item['id']='mesh:'+str(len(final));item['node_id']=node['id'];item['local_to_node_matrix']=item.pop('_matrix');item['points_space']='feature-local millimetres; apply local_to_node_matrix as row-vector, then node hierarchy';item['winding_flip_required']=bool(np.linalg.det(item['local_to_node_matrix'][:3,:3])<0);item['evidence']=next(x['evidence'] for x in coverage if x['id']==item['feature_id']);final.append(item)
    names={m.get('name') for m in materials};missing_materials=sorted({str(m['material_id']) for m in final if m.get('material_id') and m['material_id'] not in names})
    for gap in gaps:
        inactive=[{'feature_id':c['id'],'switch_id':c['selected_by_switch'],'uri_field':c['source_properties']['Uri']} for c in coverage if c['status']=='inactive_source_switch_branch' and c['type']=='rGeoFeature' and c['source_properties'].get('Uri',{}).get('value')==gap['member'] and not c['source_properties']['Uri'].get('expression')]
        gap['affects_active_configuration']=not (inactive and gap['member'] not in active_members)
        if not gap['affects_active_configuration']:
            gap['status']='proven_inactive_switch_member_decoder_gap';gap['inactive_reference_evidence']=inactive
        else:gap['status']='active_or_unproven_member_decoder_gap'
    gaps.extend(dict(gap,member='component.rsc',affects_active_configuration=True) for gap in node_pose_gaps)
    result={'schema':'vc-offline-geometry/1','status':'partial' if gaps or any(c['status']=='decoder_gap' for c in coverage) or missing_materials else 'evaluated_unvalidated','source':{'path':str(source.resolve()),'archive_sha256':digest,'rsc_sha256':rsc_hash,'input_policy':'archive-only; no runtime snapshots, DLLs or source scripts executed'},'nodes':nodes,'meshes':final,'materials':materials,'raw_mesh_file':'raw-meshes.json','feature_coverage':coverage,'archive_gaps':gaps,'member_containers':member_containers,'unresolved_named_materials':missing_materials,'coordinate_contract':{'points':'feature-local source mm','matrices':'row-vector affine, p_node = p_feature * local_to_node_matrix','node_matrices':'child to parent static serialization','metres_per_source_unit':.001,'basis':'source +Z up; no axis remap'},'limitations':['offline tessellation may differ from native','material references absent from archive remain unresolved','joint-driver home evaluation and simulation-generated geometry are separate','no claim of complete visual equivalence without independent comparison']}
    write(output/'raw-meshes.json',{'schema':'vc-raw-3ds/1','source_archive_sha256':digest,'meshes':raw});write(output/'geometry.json',result);write(output/'input-manifest.json',{'archive_sha256':digest,'members':{n:sha(v) for n,v in members.items()},'executed_source_scripts':False,'loaded_vc_libraries':False,'historical_runtime_input':False});return result

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--output',required=True);p.add_argument('--expected-sha256');a=p.parse_args();r=evaluate_archive(a.source,a.output,a.expected_sha256);print(json.dumps({'status':r['status'],'nodes':len(r['nodes']),'meshes':len(r['meshes']),'features':len(r['feature_coverage']),'gaps':len([x for x in r['feature_coverage'] if x['status']=='decoder_gap'])}))
