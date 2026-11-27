"""Blender worker for explicit GLB or expanded URDF source conversion.

URDF joint/inertial/behavior declarations are preserved separately; this worker
authors visual geometry at the declared zero pose, not a dynamics controller.
"""
import argparse,sys,json,math,shutil
sys.dont_write_bytecode=True
import xml.etree.ElementTree as ET
from pathlib import Path
import bpy
from mathutils import Matrix,Euler,Vector

def run(source,out,family,native_cad_frame=False):
    from core import digest,write
    from source_semantics import urdf_declarations,validate_glb
    source=Path(source).resolve();out=Path(out).resolve();out.mkdir(parents=True,exist_ok=False)
    bpy.ops.object.select_all(action='SELECT');bpy.ops.object.delete(use_global=False)
    bpy.context.scene.unit_settings.system='METRIC';bpy.context.scene.unit_settings.scale_length=1
    sources=[]
    def capture(p):
        p=p.resolve()
        if not p.is_relative_to(source.parent):raise ValueError('Source dependency escapes declared root')
        rel=Path('source')/p.relative_to(source.parent);dst=out/rel;dst.parent.mkdir(parents=True,exist_ok=True)
        if not dst.exists():shutil.copyfile(p,dst);sources.append({'path':rel.as_posix(),'sha256':digest(p),'original_name':p.name})
    capture(source);declarations=None
    def import_mesh(p):
        before=set(bpy.data.objects);ext=p.suffix.lower()
        if ext=='.glb':
            validate_glb(p);bpy.ops.import_scene.gltf(filepath=str(p))
        elif ext=='.stl':bpy.ops.wm.stl_import(filepath=str(p))
        else:raise ValueError('Unsupported URDF mesh extension '+ext)
        return list(set(bpy.data.objects)-before)
    if family=='glb':
        if source.suffix.lower()!='.glb':raise ValueError('Only self-contained GLB is accepted by this route')
        import_mesh(source)
        if native_cad_frame:
            # Native SolidWorks glTF retains the document's XYZ coordinates.
            # Blender bakes its basis change into mesh data and conjugates node
            # transforms. Undo both, retaining native part-local coordinates.
            basis=Matrix(((1,0,0,0),(0,0,-1,0),(0,1,0,0),(0,0,0,1)))
            correction=basis.inverted()
            for ob in list(bpy.context.scene.objects):
                if ob.type in ('CAMERA','LIGHT'):
                    bpy.data.objects.remove(ob,do_unlink=True)
            transforms={ob:correction@ob.matrix_world@basis for ob in bpy.context.scene.objects}
            for data in {ob.data for ob in transforms if ob.type=='MESH'}:
                data.transform(correction);data.update()
            def depth(ob):
                level=0
                while ob.parent is not None:level+=1;ob=ob.parent
                return level
            for ob in sorted(transforms,key=depth):ob.matrix_world=transforms[ob]
            bpy.context.view_layer.update()
    else:
        declarations=urdf_declarations(source.read_bytes());root=ET.fromstring(source.read_bytes());links={e.get('name'):e for e in root.findall('link')};parents={};edges={}
        if len(links)!=len(root.findall('link')):raise ValueError('Duplicate link name')
        for joint in root.findall('joint'):
            if joint.get('type') not in {'fixed','revolute','continuous','prismatic'}:raise ValueError('Unsupported URDF joint '+str(joint.get('type')))
            parent=joint.find('parent').get('link');child=joint.find('child').get('link')
            if parent not in links or child not in links or child in parents:raise ValueError('Invalid joint tree')
            parents[child]=parent;edges[child]=joint
        roots=set(links)-set(parents)
        if len(roots)!=1:raise ValueError('URDF requires one root')
        def transform(e):
            if e is None:return Matrix.Identity(4)
            xyz=[float(x) for x in e.get('xyz','0 0 0').split()];rpy=[float(x) for x in e.get('rpy','0 0 0').split()]
            if len(xyz)!=3 or len(rpy)!=3 or not all(math.isfinite(x) for x in xyz+rpy):raise ValueError('Invalid URDF origin')
            return Matrix.Translation(Vector(xyz))@Euler(rpy,'XYZ').to_matrix().to_4x4()
        objects={};busy=set()
        def link_object(name):
            if name in objects:return objects[name]
            if name in busy:raise ValueError('URDF cycle')
            busy.add(name);ob=bpy.data.objects.new(name,None);bpy.context.collection.objects.link(ob)
            if name in parents:ob.parent=link_object(parents[name]);ob.matrix_local=transform(edges[name].find('origin'))
            objects[name]=ob;busy.remove(name);return ob
        materials={m.get('name'):m for m in root.findall('material')}
        visual_count=0
        for name,e in links.items():
            parent=link_object(name)
            for visual in e.findall('visual'):
                geometry=visual.find('geometry')
                if geometry is None or len(geometry)!=1:raise ValueError('Invalid URDF geometry')
                g=geometry[0];local=transform(visual.find('origin'));created=[]
                if g.tag=='box':
                    size=[float(x) for x in g.get('size').split()]
                    if len(size)!=3 or not all(math.isfinite(x) and x>0 for x in size):raise ValueError('Invalid box size')
                    bpy.ops.mesh.primitive_cube_add(size=1);ob=bpy.context.object;ob.scale=size;created=[ob]
                elif g.tag=='sphere':
                    radius=float(g.get('radius'))
                    if not math.isfinite(radius) or radius<=0:raise ValueError('Invalid radius')
                    bpy.ops.mesh.primitive_uv_sphere_add(segments=32,ring_count=16,radius=radius);created=[bpy.context.object]
                elif g.tag=='cylinder':
                    radius=float(g.get('radius'));length=float(g.get('length'))
                    if not all(math.isfinite(x) and x>0 for x in (radius,length)):raise ValueError('Invalid cylinder')
                    bpy.ops.mesh.primitive_cylinder_add(vertices=64,radius=radius,depth=length);created=[bpy.context.object]
                elif g.tag=='mesh':
                    filename=g.get('filename')
                    if '://' in filename:raise ValueError('URDF package URI needs an explicit resolver; no guessed package root')
                    mesh=(source.parent/filename).resolve();capture(mesh);created=import_mesh(mesh)
                    scale=[float(x) for x in g.get('scale','1 1 1').split()]
                    if len(scale)!=3 or not all(math.isfinite(x) and x>0 for x in scale):raise ValueError('Invalid mesh scale')
                    local=local@Matrix.Diagonal(Vector([*scale,1]))
                else:raise ValueError('Unsupported geometry '+g.tag)
                m=visual.find('material')
                if m is not None and m.find('color') is None:m=materials.get(m.get('name'),m)
                if m is not None and m.find('texture') is not None:raise ValueError('URDF texture mapping requires explicit UV handling')
                bpy.context.view_layer.update()
                for ob in created:
                    if ob.parent not in created:matrix=ob.matrix_world.copy();ob.parent=parent;ob.matrix_local=local@matrix
                    if ob.type=='MESH' and m is not None and m.find('color') is not None:
                        rgba=[float(x) for x in m.find('color').get('rgba').split()]
                        if len(rgba)!=4 or not all(0<=x<=1 for x in rgba):raise ValueError('Invalid material RGBA')
                        mat=bpy.data.materials.new(m.get('name') or 'source');mat.diffuse_color=rgba;mat.use_nodes=True;bsdf=mat.node_tree.nodes.get('Principled BSDF');bsdf.inputs['Base Color'].default_value=rgba;bsdf.inputs['Alpha'].default_value=rgba[3];ob.data.materials.clear();ob.data.materials.append(mat)
                visual_count+=1
        if not visual_count:raise ValueError('No URDF visuals')
    options={'filepath':str(out/'asset.usda'),'export_materials':True,'relative_paths':True,'selected_objects_only':False,'root_prim_path':'/Asset'}
    properties=bpy.ops.wm.usd_export.get_rna_type().properties
    if 'export_textures_mode' in properties:options['export_textures_mode']='NEW'
    elif 'export_textures' in properties:options['export_textures']=True
    else:raise ValueError('Unsupported USD texture exporter API')
    bpy.ops.wm.usd_export(**options)
    from pxr import Usd
    stage=Usd.Stage.Open(str(out/'asset.usda'))
    # Blender marks a synthetic root as generated and omits it on reimport.
    # This is our real asset coordinate root and must survive exact-ID import.
    stage.GetDefaultPrim().ClearCustomDataByKey('Blender')
    stage.GetRootLayer().Save();stage=None
    result={'status':'passed','entrypoints':{'usd':'asset.usda'},'sources':sources,'conversion':{'family':family,'blender':bpy.app.version_string,'scope':'source visual hierarchy/materials; URDF zero pose only, no dynamics or controller execution'}}
    if declarations:result['parameters']={'source_urdf_declarations':declarations}
    write(out/'conversion.json',result);return result

if __name__=='__main__':
    sys.path.insert(0,str(Path(__file__).resolve().parent));p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--output',required=True);p.add_argument('--family',choices=['urdf','glb'],required=True);p.add_argument('--native-cad-frame',action='store_true');a=p.parse_args(sys.argv[sys.argv.index('--')+1:]);print(run(a.source,a.output,a.family,a.native_cad_frame))
