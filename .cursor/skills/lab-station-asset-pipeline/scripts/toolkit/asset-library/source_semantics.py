"""Recover usable engineering declarations from retained source bytes and USD.

Reading an authored value does not certify it or authorize dynamics.
"""
import json,math,zipfile
import xml.etree.ElementTree as ET
from pathlib import Path
from core import digest,inside

CAD_EXTENSIONS={'.sldprt','.sldasm','.step','.stp','.iges','.igs','.x_t','.x_b'}

def validate_glb(path):
    """Reject external image/buffer dependencies before invoking an importer."""
    import struct
    data=Path(path).read_bytes()
    if len(data)<20 or data[:4]!=b'glTF':raise ValueError('Invalid GLB header')
    version,length,chunk_length,kind=struct.unpack_from('<IIII',data,4)
    if version!=2 or length!=len(data) or kind!=0x4e4f534a or 20+chunk_length>length:raise ValueError('Unsupported GLB structure')
    document=json.loads(data[20:20+chunk_length])
    cursor=20+chunk_length;binary=None
    while cursor<len(data):
        if cursor+8>len(data):raise ValueError('Truncated GLB chunk header')
        size,kind=struct.unpack_from('<II',data,cursor);cursor+=8
        if size%4 or cursor+size>len(data):raise ValueError('Truncated or unaligned GLB chunk')
        if kind==0x004e4942:
            if binary is not None:raise ValueError('Multiple GLB binary chunks')
            binary=size
        cursor+=size
    internal=[(i,row) for i,row in enumerate(document.get('buffers',[])) if not row.get('uri')]
    if internal:
        if len(internal)!=1 or internal[0][0]!=0:raise ValueError('Invalid internal GLB buffer')
        expected=internal[0][1]['byteLength']
        if not isinstance(expected,int) or expected<=0 or binary is None or not expected<=binary<=expected+3:
            raise ValueError('GLB binary payload is missing or incomplete')
    for view in document.get('bufferViews',[]):
        index=view['buffer']
        if not isinstance(index,int) or not 0<=index<len(document.get('buffers',[])):
            raise ValueError('Invalid GLB buffer index')
        start=view.get('byteOffset',0);size=view['byteLength']
        if start<0 or size<0 or start+size>document['buffers'][index]['byteLength']:
            raise ValueError('GLB buffer view is outside its declared buffer')
    for row in document.get('buffers',[])+document.get('images',[]):
        uri=row.get('uri')
        if uri and not uri.startswith('data:'):raise ValueError('GLB external dependency needs explicit capture')
    return document

def value(v):
    if v is None or isinstance(v,(str,bool,int)):return v
    if isinstance(v,float):
        if not math.isfinite(v):raise ValueError('Non-finite source property')
        return v
    try:return [value(x) for x in v]
    except TypeError:return str(v)

def urdf_declarations(data):
    root=ET.fromstring(data)
    if root.tag!='robot' or any('xacro' in e.tag for e in root.iter()):raise ValueError('Only expanded URDF is supported')
    return {'name':root.get('name'),'links':[{'name':e.get('name'),'inertial':ET.tostring(e.find('inertial'),encoding='unicode') if e.find('inertial') is not None else None} for e in root.findall('link')],
            'joints':[{'name':e.get('name'),'type':e.get('type'),'declaration':ET.tostring(e,encoding='unicode')} for e in root.findall('joint')],
            'behavior_declarations':[ET.tostring(e,encoding='unicode') for e in root if e.tag in ('transmission','gazebo','ros2_control')],
            'execution':'source_declarations_only; no controller or mimic execution'}

def extract(root,body):
    from pxr import Usd
    root=Path(root);entry=inside(root,body['entrypoints']['usd']);stage=Usd.Stage.Open(str(entry))
    if not stage:raise ValueError('USD cannot reopen')
    result={};cad=[];physics=[];variants=[]
    for prim in stage.TraverseAll():
        for attr in prim.GetAuthoredAttributes():
            name=attr.GetName()
            if name.startswith('unilab:cad:'):
                v=value(attr.Get())
                if name.endswith('sourceProperties') and isinstance(v,str):v=json.loads(v)
                cad.append({'prim':str(prim.GetPath()),'field':name,'value':v})
            if name.startswith(('physics:','physx')):physics.append({'prim':str(prim.GetPath()),'field':name,'value':value(attr.Get())})
        for name in prim.GetVariantSets().GetNames():
            vs=prim.GetVariantSet(name);variants.append({'prim':str(prim.GetPath()),'name':name,'choices':vs.GetVariantNames(),'selection':vs.GetVariantSelection()})
    provenance={'path':body['entrypoints']['usd'],'sha256':digest(entry),'authority':'authored_USD; not independent measurement'}
    if cad:result['cad_calculated_properties']={'source':provenance,'scope':'geometric/CAD calculation; density and material unverified; not runtime mass','fields':cad}
    if physics:result['authored_physics']={'source':provenance,'scope':'source-authored values preserved; no new dynamics qualification','fields':physics}
    if variants:result['variants']=variants
    urdfs=[]
    for source in body.get('sources',[]):
        f=inside(root,source['path'])
        if digest(f)!=source['sha256']:raise ValueError('Source bytes changed')
        if f.suffix.lower()=='.urdf':urdfs.append({'source':source,**urdf_declarations(f.read_bytes())})
        elif f.suffix.lower()=='.zip':
            with zipfile.ZipFile(f) as z:
                for info in z.infolist():
                    if info.filename.lower().endswith('.urdf'):
                        if info.file_size>16*1024**2:raise ValueError('URDF source exceeds limit')
                        data=z.read(info);urdfs.append({'source':source,'member':info.filename,'member_sha256':__import__('hashlib').sha256(data).hexdigest(),**urdf_declarations(data)})
    if urdfs:result['source_urdf']=urdfs
    return result

def ensure_cad_entry(root,body):
    if body['entrypoints'].get('cad'):return False
    candidates=[s for s in body.get('sources',[]) if Path(s['path']).suffix.lower() in CAD_EXTENSIONS]
    if not candidates:return False
    if len(candidates)!=1:raise ValueError('Multiple CAD originals require an explicit root entry')
    f=inside(root,candidates[0]['path'])
    if digest(f)!=candidates[0]['sha256']:raise ValueError('CAD source changed')
    body['entrypoints']['cad']=candidates[0]['path'];return True

def selection_template(kind):
    fields=['manufacturer','order_code','configuration','dimensions','mounting_interface','material','mass','operating_environment']
    if kind=='mechanism':fields+=['motion_axis','stroke','lead','speed','load_curve','drive_interface','limit_signals']
    return {'status':'engineering_template_not_source_fact','fields':[{'name':n,'value':None,'status':'needs_source_or_review'} for n in fields]}
