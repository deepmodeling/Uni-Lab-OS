"""SolidWorks-native geometry and materials; lossless glTF resource packing."""
import argparse
import base64
import json
import math
import struct
from pathlib import Path
from urllib.parse import unquote

from core import digest,read,write,matrix
from source_semantics import validate_glb


def pack_gltf(source,destination):
    source=Path(source).resolve()
    raw=source.read_bytes()
    encoding='utf-8-sig'
    try:document=json.loads(raw.decode(encoding))
    except UnicodeDecodeError:
        # This exporter writes UTF-8 names but ANSI buffer filenames in one JSON.
        # Recover only resource URI tokens using the OS-reported code page.
        import ctypes
        code_page='cp'+str(ctypes.windll.kernel32.GetACP())
        document=json.loads(raw.decode('utf-8-sig',errors='surrogateescape'))
        for item in document.get('buffers',[])+document.get('images',[]):
            uri=item.get('uri','')
            if any(0xdc80<=ord(char)<=0xdcff for char in uri):
                item['uri']=uri.encode('utf8',errors='surrogateescape').decode(code_page)
        # Reject undecodable names or any other unexpected mixed-encoding fields.
        json.dumps(document,ensure_ascii=False).encode('utf8')
        encoding='utf-8 with '+code_page+' resource URIs'
    if document.get('asset',{}).get('version')!='2.0':
        raise ValueError('Expected native glTF 2')
    dependencies=[]
    def resource(uri):
        if uri.startswith('data:'):
            prefix,data=uri.split(',',1)
            if not prefix.endswith(';base64'):raise ValueError('Unsupported embedded resource')
            return base64.b64decode(data,validate=True)
        path=(source.parent/unquote(uri)).resolve()
        if ':' in uri or not path.is_relative_to(source.parent) or not path.is_file():
            raise ValueError('Native glTF dependency escapes export directory')
        dependencies.append({'path':str(path),'sha256':digest(path),'bytes':path.stat().st_size})
        return path.read_bytes()
    binary=bytearray();offsets=[]
    def append(data):
        binary.extend(bytes((-len(binary))%4))
        offset=len(binary);binary.extend(data)
        return offset
    buffers=document.get('buffers',[])
    for buffer in buffers:
        data=resource(buffer['uri'])
        if len(data)!=buffer['byteLength']:raise ValueError('Incomplete native glTF buffer')
        offsets.append(append(data))
    views=document.setdefault('bufferViews',[])
    for view in views:
        index=view['buffer'];offset=view.get('byteOffset',0);length=view['byteLength']
        if not 0<=index<len(buffers) or offset<0 or length<0 or offset+length>buffers[index]['byteLength']:
            raise ValueError('Invalid native glTF buffer view')
        view['byteOffset']=offsets[index]+offset;view['buffer']=0
    for image in document.get('images',[]):
        if 'uri' not in image:continue
        data=resource(image.pop('uri'))
        if data.startswith(b'\x89PNG\r\n\x1a\n'):mime='image/png'
        elif data.startswith(b'\xff\xd8\xff'):mime='image/jpeg'
        else:raise ValueError('Native texture requires a supported lossless transfer')
        image['bufferView']=len(views);image['mimeType']=mime
        views.append({'buffer':0,'byteOffset':append(data),'byteLength':len(data)})
    if not binary:raise ValueError('Native export has no geometry buffer')
    document['buffers']=[{'byteLength':len(binary)}]
    encoded=json.dumps(document,ensure_ascii=False,separators=(',',':')).encode('utf8')
    encoded+=b' '*((-len(encoded))%4);binary.extend(bytes((-len(binary))%4))
    payload=struct.pack('<II',len(encoded),0x4e4f534a)+encoded+struct.pack('<II',len(binary),0x004e4942)+binary
    destination=Path(destination)
    destination.write_bytes(b'glTF'+struct.pack('<II',2,12+len(payload))+payload)
    validate_glb(destination)
    return {'native_gltf_sha256':digest(source),'native_json_encoding':encoding,'packed_glb_sha256':digest(destination),'dependencies':dependencies,
            'method':'binary concatenation and buffer offsets only; original vertex, index, normal, UV and image bytes preserved'}


def value(obj,name,*args):
    result=getattr(obj,name)
    return result(*args) if callable(result) and not hasattr(result,'_oleobj_') else result


def inspect_glb(path):
    """Independently read native node transforms and actual vertex/index bytes."""
    import numpy as np
    document=validate_glb(path);data=Path(path).read_bytes()
    cursor=12;binary=None
    while cursor<len(data):
        size,kind=struct.unpack_from('<II',data,cursor);cursor+=8
        if kind==0x004e4942:binary=memoryview(data)[cursor:cursor+size]
        cursor+=size
    def accessor(index):
        item=document['accessors'][index]
        if 'sparse' in item:raise ValueError('Sparse native accessor needs explicit support')
        size={'SCALAR':1,'VEC2':2,'VEC3':3,'VEC4':4}[item['type']]
        dtype=np.dtype({5121:'u1',5123:'<u2',5125:'<u4',5126:'<f4'}[item['componentType']])
        view=document['bufferViews'][item['bufferView']]
        local=item.get('byteOffset',0);start=view.get('byteOffset',0)+local
        stride=view.get('byteStride',size*dtype.itemsize);count=item['count']
        if count<=0 or stride<size*dtype.itemsize or local+(count-1)*stride+size*dtype.itemsize>view['byteLength']:
            raise ValueError('Native accessor exceeds buffer view')
        values=np.ndarray((count,size),dtype=dtype,buffer=binary,offset=start,strides=(stride,dtype.itemsize))
        if not np.isfinite(values).all():raise ValueError('Nonfinite native glTF coordinates')
        return values
    if len(document['scenes'])!=1:raise ValueError('Export must contain only the selected configuration scene')
    rows=[];triangles=0;points=[]
    def visit(index,parent,ancestors):
        nonlocal triangles
        if index in ancestors:raise ValueError('Cyclic native glTF hierarchy')
        node=document['nodes'][index]
        if 'matrix' in node:local=np.asarray(node['matrix'],dtype=float).reshape(4,4).T
        else:
            from scipy.spatial.transform import Rotation
            local=np.eye(4)
            local[:3,:3]=Rotation.from_quat(node.get('rotation',[0,0,0,1])).as_matrix()@np.diag(node.get('scale',[1,1,1]))
            local[:3,3]=node.get('translation',[0,0,0])
        transform=parent@local
        if not np.isfinite(transform).all():raise ValueError('Nonfinite native glTF transform')
        row={'node':index,'name':node.get('name',''),'ancestors':ancestors,'world_matrix':transform.tolist(),'triangles':0}
        local_points=[]
        if 'mesh' in node:
            for primitive in document['meshes'][node['mesh']]['primitives']:
                if primitive.get('mode',4)!=4:raise ValueError('Native primitive needs explicit conversion')
                vertices=accessor(primitive['attributes']['POSITION'])
                indices=accessor(primitive['indices']).reshape(-1) if 'indices' in primitive else np.arange(len(vertices))
                if len(indices)%3 or np.any(indices>=len(vertices)):raise ValueError('Invalid native triangle indices')
                world=vertices[indices]@transform[:3,:3].T+transform[:3,3]
                local_points.append(vertices[indices])
                points.append(world);row['triangles']+=len(indices)//3
            triangles+=row['triangles']
            row['local_bounds_m']=[np.min([p.min(axis=0) for p in local_points],axis=0).tolist(),
                                   np.max([p.max(axis=0) for p in local_points],axis=0).tolist()]
        rows.append(row)
        for child in node.get('children',[]):visit(child,transform,ancestors+[index])
    for index in document['scenes'][document.get('scene',0)]['nodes']:visit(index,np.eye(4),[])
    if not points:raise ValueError('Native scene has no surface geometry')
    return {'bounds_m':[np.min([p.min(axis=0) for p in points],axis=0).tolist(),
                        np.max([p.max(axis=0) for p in points],axis=0).tolist()],
            'triangles':triangles,'nodes':rows,'materials':document.get('materials',[]),
            'images':len(document.get('images',[]))}


def body_bounds(bodies):
    bounds=[[math.inf]*3,[-math.inf]*3]
    for body in bodies:
        for axis in range(3):
            for side,sign in enumerate((-1.,1.)):
                direction=[0.,0.,0.];direction[axis]=sign
                extreme=body.GetExtremePoint(*direction,0.,0.,0.)
                if not isinstance(extreme,tuple) or len(extreme)!=4 or not extreme[0]:
                    raise ValueError('Native body extreme unavailable')
                result=extreme[axis+1]
                if not math.isfinite(result):raise ValueError('Nonfinite native body extreme')
                bounds[side][axis]=(min if side==0 else max)(bounds[side][axis],result)
    return bounds


def describe_part(doc,require_mesh=True):
    solids=list(doc.GetBodies2(0,False) or [])
    sheets=list(doc.GetBodies2(1,False) or [])
    bodies=solids+sheets
    faces=[face for body in bodies for face in value(body,'GetFaces') or []]
    triangles=value(doc,'GetTessTriangles',True) or []
    witness='IPartDoc.GetTessTriangles'
    if bodies and not triangles and require_mesh:
        doc.GraphicsRedraw2()
        triangles=value(doc,'GetTessTriangles',True) or []
    if bodies and not triangles and require_mesh:
        face_triangles=[value(face,'GetTessTriangles',True) or [] for face in faces]
        if face_triangles and all(values and len(values)%9==0 for values in face_triangles):
            triangles=[coordinate for values in face_triangles for coordinate in values]
            witness='IFace2.GetTessTriangles; every source face required'
    if not bodies or (require_mesh and not triangles) or len(triangles)%9:
        raise ValueError(f'Native part has no complete surface tessellation: {len(bodies)} bodies, {len(triangles)} coordinate values')
    properties=[body.GetMassProperties(1.) for body in solids]
    if any(p is None or not all(math.isfinite(x) for x in p) for p in properties):
        raise ValueError('Native geometric properties unavailable')
    volume=sum(p[3] for p in properties)
    area=sum(value(face,'GetArea') for face in faces)
    return {'bounds_m':body_bounds(bodies),'body_count':len(bodies),'solid_count':len(solids),'sheet_count':len(sheets),
            'volume_m3':volume if not sheets else None,'area_m2':area,
            'center_of_volume_m':[sum(p[i]*p[3] for p in properties)/volume for i in range(3)] if volume>0 and not sheets else None,
            'face_count':len(faces),'tessellation_witness':witness,
            'native_triangles':len(triangles)//9}


def export_in_session(sw,source,output,configuration=None,require_named=False,dependency_root=None):
    import pythoncom
    from win32com.client import VARIANT
    source=Path(source).resolve();output=Path(output).resolve()
    output.mkdir(parents=True,exist_ok=False)
    before=digest(source)
    write(output/'stage.json',{'stage':'opening','source_sha256':before})
    error=VARIANT(pythoncom.VT_BYREF|pythoncom.VT_I4,0)
    warning=VARIANT(pythoncom.VT_BYREF|pythoncom.VT_I4,0)
    empty=VARIANT(pythoncom.VT_DISPATCH,None)
    if not sw.SetCurrentWorkingDirectory(str(output)):raise ValueError('Cannot isolate native import directory')
    native=source.suffix.lower() in ('.sldprt','.sldasm')
    if native:
        doc=sw.OpenDoc6(str(source),2 if source.suffix.lower()=='.sldasm' else 1,3,'',error,warning)
    else:
        settings=sw.GetImportFileData(str(source))
        if source.suffix.lower() in ('.step','.stp'):
            if settings is None:raise ValueError('Native STEP import settings unavailable')
            settings.MapConfigurationData=True
        doc=sw.LoadFile4(str(source),'r',settings if settings is not None else empty,error)
    if not doc or error.value or warning.value:
        raise ValueError(f'Native open failed with error {error.value}, warning {warning.value}')
    if native and Path(value(doc,'GetPathName')).resolve()!=source:
        raise ValueError('Native CAD root differs from requested source')
    write(output/'stage.json',{'stage':'describing','source_sha256':before})
    names=list(value(doc,'GetConfigurationNames') or [])
    write(output/'configuration-list.json',{'source_sha256':before,'names':names})
    selected=value(doc.ConfigurationManager.ActiveConfiguration,'Name')
    if native:
        matches=[name for name in names if configuration and name.casefold()==configuration.casefold()]
        if len(matches)==1:selected=matches[0]
        elif len(names)==1 and not require_named:selected=names[0]
        else:raise ValueError('Unsupported selection: no exact native SolidWorks configuration')
        if value(doc.ConfigurationManager.ActiveConfiguration,'Name')!=selected and not doc.ShowConfiguration2(selected):
            raise ValueError('Cannot select native configuration')
    if value(doc.ConfigurationManager.ActiveConfiguration,'Name')!=selected:
        raise ValueError('Native configuration drift')
    assembly=value(doc,'GetType')==2
    tree=[]
    if assembly:
        for component in doc.GetComponents(False) or []:
            path=value(component,'GetPathName')
            suppressed=bool(value(component,'IsSuppressed'))
            row={'instance':component.Name2,'configuration':component.ReferencedConfiguration,
                 'suppressed':suppressed,'source_file':path,'virtual':bool(value(component,'IsVirtual'))}
            if native and not row['virtual']:
                reference=Path(path).resolve()
                if not dependency_root or not reference.is_relative_to(Path(dependency_root).resolve()) or not reference.is_file():
                    raise ValueError('Native assembly references outside retained source')
                row['source_sha256']=digest(reference)
            elif not native:
                row['imported_from']=source.name
                row['source_file_role']='unsaved imported component'
                if path and Path(path).is_file():
                    raise ValueError('Neutral CAD importer reused an existing external part')
            if suppressed:tree.append(row);continue
            model=value(component,'GetModelDoc2')
            if model is None:raise ValueError('Unresolved native assembly instance')
            row['world_matrix']=matrix(list(value(component.Transform2,'ArrayData')))
            row['kind']='assembly' if value(model,'GetType')==2 else 'part'
            if row['kind']=='part':
                row.update(describe_part(model,False))
                if value(model.ConfigurationManager.ActiveConfiguration,'Name')!=row['configuration']:
                    raise ValueError('Component configuration needs per-instance evaluation')
            tree.append(row)
        if not tree:raise ValueError('Empty native assembly')
    else:tree=[{'instance':source.stem,'kind':'part','configuration':selected,**describe_part(doc,False)}]
    write(output/'native-tree-before-export.json',tree)
    target=output/'model.gltf'
    write(output/'stage.json',{'stage':'exporting_gltf','source_sha256':before})
    doc.ClearSelection2(True)
    ok=doc.Extension.SaveAs(str(target),0,1,empty,error,warning)
    if not ok or error.value or warning.value:raise ValueError('Native glTF export failed')
    candidates=[target,output/'model'/'model.gltf']
    emitted=[path for path in candidates if path.is_file()]
    if len(emitted)!=1:raise ValueError('Native glTF entry missing or ambiguous')
    if assembly:
        models={component.Name2:value(component,'GetModelDoc2') for component in doc.GetComponents(False) or []
                if not bool(value(component,'IsSuppressed'))}
        for row in tree:
            if not row['suppressed'] and row['kind']=='part':
                try:row.update(describe_part(models[row['instance']]))
                except ValueError as exc:raise ValueError(f"Native component {row['instance']}: {exc}") from exc
    else:tree[0].update(describe_part(doc))
    packed=output/'native.glb'
    packing=pack_gltf(emitted[0],packed)
    if digest(source)!=before:raise ValueError('Original CAD was modified')
    result={'schema':'unilab.solidworks-native-export/1','source':str(source),'source_sha256':before,
            'dependency_root':str(Path(dependency_root).resolve()) if dependency_root else str(source.parent),
            'version':value(sw,'RevisionNumber'),'configuration':selected,'configuration_names':names,
            'kind':'assembly' if assembly else 'part','native_tree':tree,'packing':packing,'glb':str(packed),
            'native_axis_policy':'native document coordinates in metres; glTF geometry frame verified before USD authoring'}
    result['gltf_geometry']=inspect_glb(packed)
    tolerance=.0001
    if not assembly:
        delta=max(abs(a-b) for x,y in zip(tree[0]['bounds_m'],result['gltf_geometry']['bounds_m']) for a,b in zip(x,y))
        if delta>tolerance:raise ValueError('Native tessellation differs from analytic CAD extents')
        if tree[0]['native_triangles']!=result['gltf_geometry']['triangles']:
            raise ValueError('Native tessellation coverage differs from glTF')
        mesh_nodes=[row['node'] for row in result['gltf_geometry']['nodes'] if row['triangles']]
        if len(mesh_nodes)==1:tree[0]['gltf_node']=mesh_nodes[0]
        result['native_geometry_check']={'status':'passed','max_error_m':delta,'tolerance_m':tolerance}
    else:
        scene=result['gltf_geometry']['nodes']
        by_index={row['node']:row for row in scene}
        mesh_ancestors={ancestor for row in scene if row['triangles'] for ancestor in row['ancestors']}
        by_path={}
        for row in scene:
            if not row['ancestors'] or not (row['triangles'] or row['node'] in mesh_ancestors):continue
            names=[by_index[i]['name'] for i in row['ancestors'][1:]]+[row['name']]
            path='/'.join(names)
            if path in by_path:raise ValueError('Ambiguous native glTF component path')
            by_path[path]=row
        active=[row for row in tree if not row['suppressed']]
        if set(by_path)!={row['instance'] for row in active}:
            raise ValueError('Native glTF component coverage differs from CAD')
        differences=[]
        for row in active:
            emitted=by_path[row['instance']]
            delta=max(abs(a-b) for x,y in zip(row['world_matrix'],emitted['world_matrix']) for a,b in zip(x,y))
            if delta>1e-6:raise ValueError('Native glTF component placement differs from CAD')
            row['gltf_node']=emitted['node']
            if row['kind']!='part':continue
            if row['native_triangles']!=emitted['triangles']:
                raise ValueError('Native glTF component surface coverage differs from CAD')
            delta=max(abs(a-b) for x,y in zip(row['bounds_m'],emitted['local_bounds_m']) for a,b in zip(x,y))
            if delta>tolerance:raise ValueError('Native component tessellation differs from analytic extents')
            differences.append(delta)
        result['native_geometry_check']={'status':'passed','components':len(active),'max_error_m':max(differences,default=0),
                                         'tolerance_m':tolerance,'placement_tolerance_m':1e-6}
    write(output/'native-export.json',result)
    write(output/'stage.json',{'stage':'passed','source_sha256':before})
    sw.CloseAllDocuments(True)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--configuration')
    parser.add_argument('--require-named',action='store_true')
    parser.add_argument('--dependency-root',type=Path)
    args=parser.parse_args()
    from supplier_sw_step import private_session
    # The process receipt lives outside the new per-asset directory.
    with private_session(args.output.parent/'native-session.json') as application:
        application.Visible=False
        export_in_session(application,args.source,args.output,args.configuration,args.require_named,args.dependency_root)
