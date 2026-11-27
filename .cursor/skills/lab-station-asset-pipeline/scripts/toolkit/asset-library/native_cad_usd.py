"""Adapt verified SolidWorks-native mesh output to the existing USD/CAD package contract."""
import json
import shutil
import struct
import subprocess
import sys
from pathlib import Path

from core import read,write,digest,uid
from solidworks_native import inspect_glb
from source_semantics import validate_glb


def label_occurrences(source,destination,identity,source_sha256):
    document=validate_glb(source);binary=Path(source).read_bytes()
    json_size=struct.unpack_from('<I',binary,12)[0]
    chunks=binary[20+json_size:]
    geometry=inspect_glb(source)
    relevant=set()
    for row in geometry['nodes']:
        if row['triangles']:relevant.update([row['node'],*row['ancestors']])
    occurrences=[];definitions={}
    for row in geometry['nodes']:
        if row['node'] not in relevant:continue
        node=document['nodes'][row['node']]
        lineage='/'.join(map(str,[*row['ancestors'],row['node']]))
        occurrence=uid(identity+'/native-occurrence/'+lineage)
        definition=uid(identity+'/native-definition/'+str(node.get('mesh','assembly-'+lineage)))
        original=node.get('name','')
        definitions.setdefault(definition,{'name':original,'source_label':lineage,'source_sha256':source_sha256,
            'kind':'assembly' if any(child in relevant for child in node.get('children',[])) else 'shape'})
        occurrences.append({'id':occurrence,'instance_id':occurrence,'definition_id':definition,
                            'source_label':original,'native_node_path':lineage,'world_matrix_m':row['world_matrix']})
        node['name']='o_'+occurrence.replace('-','_')
    encoded=json.dumps(document,ensure_ascii=False,separators=(',',':')).encode()
    encoded+=b' '*((-len(encoded))%4)
    payload=struct.pack('<II',len(encoded),0x4e4f534a)+encoded+chunks
    destination.write_bytes(b'glTF'+struct.pack('<II',2,12+len(payload))+payload)
    validate_glb(destination)
    return {'schema':'unilab.neutral-cad-source-map/1','source_sha256':source_sha256,'units':'m',
            'frame_policy':'native SolidWorks document XYZ; glTF import axis conversion explicitly cancelled',
            'definitions':definitions,'occurrences':occurrences}


def project(source,export,work,identity,blender):
    from pxr import Usd,UsdGeom,Sdf,Gf
    source=Path(source).resolve();export=Path(export);work=Path(work)
    receipt=read(export/'native-export.json')
    if digest(source)!=receipt['source_sha256']:raise ValueError('CAD source changed after native export')
    glb=Path(receipt['glb'])
    if digest(glb)!=receipt['packing']['packed_glb_sha256']:raise ValueError('Native mesh changed')
    labelled=export/'labelled.glb'
    mapping=label_occurrences(glb,labelled,identity,receipt['source_sha256'])
    script=Path(__file__).with_name('historical_blender.py')
    with (export/'usd-export.log').open('w',encoding='utf8') as log:
        subprocess.run([str(blender),'--background','--factory-startup','--threads','2','--python-exit-code','1',
                        '--python',str(script),'--','--source',str(labelled),'--output',str(work),
                        '--family','glb','--native-cad-frame'],stdout=log,stderr=subprocess.STDOUT,check=True,timeout=600)
    result=read(work/'conversion.json');stage=Usd.Stage.Open(str(work/result['entrypoints']['usd']))
    cache=UsdGeom.XformCache();prims={prim.GetName():prim for prim in stage.Traverse()}
    native_parts={row['gltf_node']:row for row in receipt['native_tree'] if row.get('kind')=='part' and 'gltf_node' in row}
    for occurrence in mapping['occurrences']:
        name='o_'+occurrence['id'].replace('-','_');prim=prims.get(name)
        if prim is None:raise ValueError('Native CAD occurrence missing from USD')
        transform=cache.GetLocalToWorldTransform(prim)
        error=max(abs(transform[j][i]-occurrence['world_matrix_m'][i][j]) for i in range(4) for j in range(4))
        if error>1e-6:raise ValueError('Native occurrence transform differs in USD')
        occurrence['usd_prim']=str(prim.GetPath())
        part=native_parts.get(int(occurrence['native_node_path'].split('/')[-1]))
        if part:
            definition=mapping['definitions'][occurrence['definition_id']]
            for key in ('solid_count','sheet_count','face_count'):
                if key in part:definition[key]=part[key]
            for key in ('volume_m3','area_m2'):
                if key=='volume_m3' and part.get('sheet_count',0):continue
                if part.get(key) is not None:
                    definition[key]=part[key]
                    prim.CreateAttribute('unilab:cad:'+key,Sdf.ValueTypeNames.Double).Set(part[key])
            if part.get('center_of_volume_m') is not None and not part.get('sheet_count',0):
                definition['center_of_volume_m']=part['center_of_volume_m']
                prim.CreateAttribute('unilab:cad:center_of_volume_m',Sdf.ValueTypeNames.Double3).Set(Gf.Vec3d(*part['center_of_volume_m']))
    points=[];triangles=0
    for prim in stage.Traverse():
        if not prim.IsA(UsdGeom.Mesh):continue
        mesh=UsdGeom.Mesh(prim);transform=cache.GetLocalToWorldTransform(prim)
        points.extend(transform.Transform(point) for point in mesh.GetPointsAttr().Get())
        triangles+=sum(size-2 for size in mesh.GetFaceVertexCountsAttr().Get())
    if not points:raise ValueError('Empty USD after native projection')
    bounds=[[min(p[i] for p in points) for i in range(3)],[max(p[i] for p in points) for i in range(3)]]
    expected=receipt['gltf_geometry']
    error=max(abs(a-b) for x,y in zip(expected['bounds_m'],bounds) for a,b in zip(x,y))
    if error>1e-6 or triangles!=expected['triangles']:raise ValueError('USD geometry differs from native export')
    stage.GetRootLayer().Save();stage=None
    independent=None
    if source.suffix.lower() in ('.step','.stp'):
        from verify_source_geometry import verify
        independent=verify(source,work/result['entrypoints']['usd'],'step')
        write(export/'independent-source-bounds.json',independent)
        if independent['status']!='passed':raise ValueError('Native import differs from original STEP bounds')
    dependency_root=Path(receipt.get('dependency_root',str(source.parent)))
    captures={source:receipt['source_sha256']}
    for row in receipt['native_tree']:
        if row.get('source_sha256'):captures[Path(row['source_file']).resolve()]=row['source_sha256']
    sources=[]
    for path,expected_hash in captures.items():
        if not path.is_relative_to(dependency_root) or digest(path)!=expected_hash:
            raise ValueError('Native CAD dependency changed or escapes source root')
        copied=work/'source'/path.relative_to(dependency_root)
        copied.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(path,copied)
        if digest(copied)!=expected_hash:raise ValueError('CAD copy differs')
        sources.append({'path':copied.relative_to(work).as_posix(),'sha256':expected_hash,'original_name':path.name})
    original=work/'source'/source.relative_to(dependency_root)
    # Packed native glTF is a conversion intermediate, with its digest in evidence.
    intermediate=work/'source'/'labelled.glb'
    intermediate.unlink()
    result.update(source_sha256=receipt['source_sha256'],
                  sources=sources,
                  cad_mapping_data=mapping)
    result['entrypoints']['cad']=original.relative_to(work).as_posix()
    result['conversion'].update(family='solidworks-native',solidworks=receipt['version'],
                                native_configuration=receipt['configuration'],native_triangles=triangles,
                                native_glb_sha256=receipt['packing']['packed_glb_sha256'],
                                native_geometry_check=receipt.get('native_geometry_check'),
                                materials=expected['materials'],images=expected['images'])
    verification={'status':'passed','family':'solidworks-native','source_sha256':receipt['source_sha256'],
                  'source_bounds_m':expected['bounds_m'],'usd_bounds_m':bounds,'max_error_m':error,'tolerance_m':1e-6,
                  'scope':'native tessellation to USD geometry and all exported occurrence transforms',
                  'native_cad_check':receipt.get('native_geometry_check'),'independent_original_cad':independent}
    write(work/'conversion.json',result);write(work/'cad-mapping.json',mapping)
    write(work.parent/'source-geometry.json',verification)
    return result,verification,work


def convert(source,work,identity,blender,binding,code,dependency_root):
    export=Path(work).parent/'solidworks-native'
    args=[sys.executable,'-B','-X','utf8',str(Path(__file__).with_name('solidworks_native.py')),
          '--source',str(source),'--output',str(export),'--configuration',binding.get('builder_configuration',code),
          '--dependency-root',str(dependency_root)]
    if binding.get('requires_named_configuration'):args.append('--require-named')
    with (Path(work).parent/'solidworks-native.log').open('w',encoding='utf8') as log:
        try:subprocess.run(args,stdout=log,stderr=subprocess.STDOUT,check=True,timeout=600)
        except subprocess.TimeoutExpired:
            import psutil
            session=export.parent/'private-session.json'
            if session.is_file():
                owner=read(session)
                try:
                    process=psutil.Process(owner['pid'])
                    if process.name().lower()=='sldworks.exe' and process.create_time()==owner['created']:process.kill()
                except psutil.NoSuchProcess:pass
            raise
        except subprocess.CalledProcessError as exc:
            log.flush();lines=(Path(work).parent/'solidworks-native.log').read_text(encoding='utf8',errors='replace').splitlines()
            raise ValueError('SolidWorks native export failed: '+(lines[-1] if lines else str(exc))) from exc
    return project(source,export,work,identity,blender)
