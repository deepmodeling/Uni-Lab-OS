"""USD-backed collection instances; all scene mutation runs on Blender's main thread."""
import bpy,json,hashlib,uuid,re
from pathlib import Path
from mathutils import Matrix
from contextlib import contextmanager

LAYOUT_CACHE_REVISION='usd-static-layout-v5-identity-root'

@contextmanager
def scene_context():
    """Timers have no editor context; resolve an actual 3D view before operators."""
    if bpy.app.background:
        yield
        return
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type=='VIEW_3D':
                region=next(r for r in area.regions if r.type=='WINDOW')
                with bpy.context.temp_override(window=window,area=area,region=region):yield
                return
    raise RuntimeError('请先打开一个 3D 视图，再插入资产')

def read(path):return json.loads(Path(path).read_text(encoding='utf8'))
def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()

@contextmanager
def usd_import_map():
    """Capture Blender's authoritative prim-to-ID map, never guess duplicate names."""
    captured={}
    class UniLabLayoutImportHook(bpy.types.USDHook):
        bl_idname='unilab_layout_import_hook'
        bl_label='UniLab layout import map'
        @staticmethod
        def on_import(context):
            captured['prim_map']=context.get_prim_map()
            return True
    bpy.utils.register_class(UniLabLayoutImportHook)
    try:
        yield captured
        if 'prim_map' not in captured:raise ValueError('Blender USD 导入器未提供精确 prim 映射，已拒绝猜测层级')
    finally:bpy.utils.unregister_class(UniLabLayoutImportHook)

def restore_reset_layout(captured,imported,usd_path):
    """Restore static USD layout and ancestry through importer-skipped Scopes.

    Keep the source USD untouched. Re-express reset nodes relative to the Blender
    parent for an editable layout snapshot; USD physics stays in the source package.
    """
    # Blender's hook may expose a pxrBlender ABI while installed pxr uses another
    # ABI under the same Python namespace. Use the hook only for its string/ID
    # map and open a read-only stage through one consistent pxr module set.
    from pxr import Usd,UsdGeom,Sdf
    stage=Usd.Stage.Open(str(usd_path))
    def get_prim(path):return stage.GetPrimAtPath(Sdf.Path(path))
    cache=UsdGeom.XformCache(Usd.TimeCode.Default())
    mapping={};paths={}
    for raw,ids in captured['prim_map'].items():
        path=str(raw);prim=get_prim(path)
        if not prim or not prim.IsA(UsdGeom.Xformable):continue
        for ob in ids:
            if not isinstance(ob,bpy.types.Object) or ob not in imported:continue
            if ob in paths and paths[ob]!=path:raise ValueError('USD 对象映射不唯一，不能安全恢复布局')
            if path in mapping and mapping[path]!=ob:raise ValueError('USD prim 映射到多个对象，不能安全恢复布局')
            paths[ob]=path;mapping[path]=ob;ob['lab_usd_prim_path']=path
    resets=[path for path in mapping if UsdGeom.Xformable(get_prim(path)).GetResetXformStack()]
    for path in mapping:
        xf=UsdGeom.Xformable(get_prim(path))
        if xf.TransformMightBeTimeVarying():raise ValueError('动画资产需要显式选择时间快照；当前仅支持静态布局')
        if mapping[path].constraints or mapping[path].animation_data:raise ValueError('带动画/约束的导入对象不能静态恢复布局')
    def expected(path):return Matrix(cache.GetLocalToWorldTransform(get_prim(path))).transposed()
    # One stage has one axis/unit basis. Some importer paths below USD Scope
    # become independent Blender roots; deriving a basis from each such root
    # would conceal their wrong frame. Anchor only at the actual default prim.
    anchor=str(stage.GetDefaultPrim().GetPath())
    anchor_basis='default_prim'
    if anchor not in mapping:
        # Blender can omit a top-level identity Xform (observed on native LH05
        # exports). A direct mapped child has no intervening frame to lose.
        # Do not fall back to arbitrary meshes beneath skipped/transformed Scopes.
        default=stage.GetDefaultPrim();identity=Matrix.Identity(4)
        if (not default or not default.GetParent().IsPseudoRoot() or
            not default.IsA(UsdGeom.Xformable) or UsdGeom.Xformable(default).TransformMightBeTimeVarying() or
            max(abs(expected(anchor)[i][j]-identity[i][j]) for i in range(4) for j in range(4))>1e-8):
            raise ValueError('缺少非平凡 USD defaultPrim 的精确导入对象')
        candidates=[path for path in mapping if get_prim(path).GetParent()==default
                    and mapping[path].parent is None and not UsdGeom.Xformable(get_prim(path)).GetResetXformStack()]
        if not candidates:raise ValueError('缺少 USD 恒等根的直接子对象；不能推测全局轴变换')
        bases=[mapping[path].matrix_world.copy()@expected(path).inverted() for path in candidates]
        reference=bases[0];rotation=reference.to_3x3()
        if (reference.translation.length>1e-5 or abs(rotation.determinant()-1)>1e-5 or
            max(abs((rotation.transposed()@rotation)[i][j]-(1 if i==j else 0)) for i in range(3) for j in range(3))>1e-5 or
            any(max(abs(value[i][j]-reference[i][j]) for i in range(4) for j in range(4))>1e-5 for value in bases[1:])):
            raise ValueError('USD 恒等根的子对象轴变换不一致')
        anchor=candidates[0];anchor_basis='direct_child_of_omitted_top_level_identity_xform'
    basis=mapping[anchor].matrix_world.copy()@expected(anchor).inverted()
    desired={ob:basis@expected(path) for path,ob in mapping.items()}
    corrected=0;reparented=0;parents={}
    for path,ob in mapping.items():
        ancestor=get_prim(path).GetParent()
        while ancestor and not ancestor.IsPseudoRoot() and str(ancestor.GetPath()) not in mapping:
            ancestor=ancestor.GetParent()
        parents[ob]=mapping.get(str(ancestor.GetPath())) if ancestor and not ancestor.IsPseudoRoot() else None
    # Resolve every mapped static object, including interface/frame helpers whose
    # Scope parent may have been skipped by Blender. Local matrices are computed
    # from desired parent worlds, so assignment order cannot compound errors.
    for path,ob in mapping.items():
        world=desired[ob]
        parent=parents[ob]
        parent_world=desired[parent] if parent else None
        local=parent_world.inverted()@world if parent_world else world
        error=max(abs(ob.matrix_local[i][j]-local[i][j]) for i in range(4) for j in range(4))
        parent_changed=ob.parent!=parent
        # Blender's XYZ Euler decomposition can flip the small cosine term near
        # a gimbal singularity, even for an orthonormal source matrix. Retain
        # quaternion object channels before assigning the exact desired local
        # matrix. This only changes the editable snapshot representation, never
        # the USD, instance-root controls or the world readback tolerance.
        rotation_changed=ob.rotation_mode!='QUATERNION'
        if error>1e-6 or parent_changed or rotation_changed:
            if parent_changed:ob.parent=parent;reparented+=1
            ob.rotation_mode='QUATERNION'
            ob.matrix_parent_inverse=Matrix.Identity(4)
            ob.matrix_local=local;corrected+=1
        ob['lab_layout_snapshot']='static USD world and nearest mapped USD ancestor restored'
    bpy.context.view_layer.update()
    residual=max(max(abs(ob.matrix_world[i][j]-desired[ob][i][j]) for i in range(4) for j in range(4)) for ob in desired)
    if residual>1e-5:raise ValueError('USD 布局快照回读误差超过容差: '+str(residual))
    if any(ob.parent!=parent for ob,parent in parents.items()):raise ValueError('USD 层级恢复回读不一致')
    return {'reset_nodes':len(resets),'corrected_nodes':corrected,'reparented_nodes':reparented,'max_matrix_error':residual,
            'cache_revision':LAYOUT_CACHE_REVISION,'rotation_representation':'QUATERNION','anchor_path':anchor,'anchor_basis':anchor_basis,
            'scope':'static_default_time_layout_snapshot; source USD unchanged; no Blender dynamics claim'}

def restore_authored_opacity(captured,imported,usd_path):
    """Read authored constant opacity through exact USDHook material identities.

    Blender 5.1 can import a literal zero opacity as one. Preserve that zero,
    without guessing transparency from colors, names or material slot numbers.
    Connected/other shaders remain importer-owned and are explicitly unverified.
    """
    from pxr import Usd,UsdShade,Sdf
    stage=Usd.Stage.Open(str(usd_path));rows=[];unverified=[];seen={}
    for raw,ids in captured['prim_map'].items():
        path=str(raw);prim=stage.GetPrimAtPath(Sdf.Path(path))
        if not prim or not prim.IsA(UsdShade.Material):continue
        materials=[x for x in ids if isinstance(x,bpy.types.Material)]
        if len(materials)!=1:raise ValueError('USD 材质映射不唯一，不能安全恢复透明度')
        mat=materials[0]
        if mat in seen and seen[mat]!=path:raise ValueError('多个 USD 材质共用同一导入材质，不能安全恢复透明度')
        seen[mat]=path;source=UsdShade.Material(prim).ComputeSurfaceSource()[0]
        if not source or source.GetIdAttr().Get()!='UsdPreviewSurface':
            unverified.append({'source_material':path,'reason':'unsupported_surface_shader'});continue
        opacity=source.GetInput('opacity')
        if not opacity or not opacity.GetAttr().HasAuthoredValueOpinion():continue
        if opacity.HasConnectedSource() or opacity.GetAttr().ValueMightBeTimeVarying():
            unverified.append({'source_material':path,'reason':'connected_or_animated_opacity'});continue
        threshold=source.GetInput('opacityThreshold')
        if threshold and (threshold.HasConnectedSource() or (threshold.Get() not in (None,0.0))):
            unverified.append({'source_material':path,'reason':'opacity_threshold_requires_separate_shader_verification'});continue
        value=float(opacity.Get());nodes=[n for n in mat.node_tree.nodes if n.type=='BSDF_PRINCIPLED'] if mat.use_nodes else []
        if len(nodes)!=1 or nodes[0].inputs['Alpha'].is_linked:raise ValueError('导入材质透明度连接不明确，拒绝覆盖')
        if not 0<=value<=1:raise ValueError('源材质透明度超出有效范围')
        socket=nodes[0].inputs['Alpha'];previous=float(socket.default_value);previous_display=float(mat.diffuse_color[3])
        changed=abs(previous-value)>1e-7 or abs(previous_display-value)>1e-7
        if changed:
            if any(ob not in imported and ob.type=='MESH' and any(m==mat for m in ob.data.materials) for ob in bpy.data.objects):
                raise ValueError('材质被本次导入之外的对象使用，拒绝修改共享材质')
            socket.default_value=value;color=list(mat.diffuse_color);color[3]=value;mat.diffuse_color=color
        slots=[{'object_prim':ob.get('lab_usd_prim_path'),'slot':i} for ob in imported if ob.type=='MESH'
               for i,m in enumerate(ob.data.materials) if m==mat]
        rows.append({'source_material':path,'source_input':str(opacity.GetAttr().GetPath()),'source_value':value,
                     'blender_material':mat.name,'previous_alpha':previous,'previous_display_alpha':previous_display,
                     'readback_alpha':float(socket.default_value),'changed':changed,'slots':slots})
    return {'scope':'authored constant UsdPreviewSurface opacity; source USD unchanged',
            'materials':rows,'corrected_materials':sum(r['changed'] for r in rows),'unverified':unverified}
def checked_inputs(request):
    root=Path(request['package_root']).resolve();target=request['target']
    from . import native_package
    if (root/native_package.MANIFEST).exists():
        native_package.verify(root,target['package_digest'])
        return root,native_package.package(root)
    if (root/'asset.json').exists():
        if digest(root/'asset.json')!=target['package_digest']:raise ValueError('资产清单摘要不符')
        p=read(root/'asset.json')
        for row in p['files']:
            path=(root/row['path']).resolve()
            if not path.is_relative_to(root) or digest(path)!=row['sha256']:raise ValueError('资产文件缺失或变化')
        if not target['entrypoints'].get('spatial'):raise ValueError('此资产没有空间入口')
        return root,p
    if digest(root/'bundle.sha256.json')!=target['package_digest']:raise ValueError('发布包摘要不符')
    files={f['path']:f['sha256'] for f in read(root/'bundle.sha256.json')['files']}
    def check(rel):
        path=(root/rel).resolve()
        if not path.is_relative_to(root) or files.get(rel)!=digest(path):raise ValueError('包内输入缺失或变化')
        return path
    p=read(check('package.json'));usd=target['entrypoints'].get('spatial')
    if not usd:raise ValueError('此条目没有空间表示，不能插入场景')
    check(usd['path'])
    for key in ('definitions','assembly'):
        if key in p:check(p[key])
    for attachment in p.get('import_metadata',{}).get('extras',[]):
        if attachment.get('role')=='cad_usd_occurrence_mapping':check(attachment['path'])
    if 'os_graph' in p['entrypoints']:check(p['entrypoints']['os_graph'])
    return root,p

def source_collection(request):
    root,p=checked_inputs(request);target=request['target'];key=target['package_digest']+':'+request['catalog_id']+':'+LAYOUT_CACHE_REVISION
    for col in bpy.data.collections:
        if col.get('lab_source_key')==key:return col
    owner_key=target['package_digest']+':owner:'+LAYOUT_CACHE_REVISION
    owner=next((c for c in bpy.data.collections if c.get('lab_source_key')==owner_key),None)
    if owner is None:
        before=set(bpy.data.objects);owner=bpy.data.collections.new('库源 · '+p['title']);owner.use_fake_user=True
        try:
            from pxr import Usd,UsdGeom
            source_stage=Usd.Stage.Open(str(root/target['entrypoints']['spatial']['path']))
            purposes={UsdGeom.Imageable(prim).ComputePurpose() for prim in source_stage.Traverse() if prim.IsA(UsdGeom.Gprim)}
            # Dedicated collision assets contain only guide geometry. Show those
            # when inserted alone; keep guides hidden in ordinary render assets.
            guide_only=purposes=={UsdGeom.Tokens.guide}
            owner['lab_guide_only_preview']=guide_only
            with scene_context(),usd_import_map() as captured:
                bpy.ops.wm.usd_import(filepath=str(root/target['entrypoints']['spatial']['path']),merge_parent_xform=False,apply_unit_conversion_scale=False,scale=1.0,set_frame_range=False,import_cameras=False,import_lights=False,create_collection=False,property_import_mode='ALL',import_guide=guide_only)
            imported=set(bpy.data.objects)-before;bpy.context.view_layer.update()
            owner['lab_usd_layout_receipt']=json.dumps(restore_reset_layout(captured,imported,root/target['entrypoints']['spatial']['path']))
            owner['lab_usd_material_receipt']=json.dumps(restore_authored_opacity(captured,imported,root/target['entrypoints']['spatial']['path']))
            # Native unilab.json uses an inline assembly dependency object;
            # legacy engineering packages use an assembly JSON path. Avoid
            # evaluating a legacy path fallback for already embedded data.
            rows=p.get('assembly_data')
            if isinstance(rows,dict):rows=rows.get('occurrences',[])
            if rows is None:
                assembly=p.get('assembly',{})
                if isinstance(assembly,str):assembly=read(root/assembly)
                rows=assembly.get('occurrences',[])
            definitions=p.get('definitions_data')
            if definitions is None:
                definition_source=p.get('definitions',{})
                if isinstance(definition_source,str):definition_source=read(root/definition_source)
                definitions=definition_source.get('definitions',{})
            byid={r['id']:r for r in rows};observed=set()
            neutral=next((a for a in p.get('import_metadata',{}).get('extras',[]) if a.get('role')=='cad_usd_occurrence_mapping'),None)
            if (neutral or p.get('cad_mapping_data')) and not rows:
                mapping=p.get('cad_mapping_data') or read(root/neutral['path'])
                if mapping.get('schema')=='unilab.neutral-cad-source-map/1':
                    rows=[{'id':r['instance_id'],'definition_id':r['definition_id'],'definition_revision':p['definition_revision'],'configuration':p['configuration'],'world_matrix':r['world_matrix_m'],'source_label':r['source_label'],'cad_sha256':mapping['source_sha256']} for r in mapping['occurrences']]
                    definitions={k:{'display_name':d['name']} for k,d in mapping['definitions'].items()};byid={r['id']:r for r in rows}
            graph=read(root/p['entrypoints']['os_graph']) if 'os_graph' in p['entrypoints'] else {};node_ids={n['id'] for n in graph.get('nodes',[])}
            for ob in imported:
                original=ob.name.split('.')[0];ob['lab_usd_name']=original
                match=re.fullmatch('o_([0-9a-f_]{36})',original)
                if match:
                    oid=match[1].replace('_','-');ob['lab_source_occurrence']=oid
                    if oid in byid:
                        r=byid[oid];observed.add(oid)
                        for k in ('definition_id','definition_revision','configuration'):ob['lab_'+k]=r[k]
                        ob['lab_source_matrix']=json.dumps(r['world_matrix'])
                        if r.get('cad_sha256'):ob['lab_source_cad_sha256']=r['cad_sha256'];ob['lab_source_cad_label']=r['source_label']
                        ob.name=definitions[r['definition_id']]['display_name']
                if original in node_ids:ob['lab_source_graph_node']=original
                ob['lab_package_digest']=target['package_digest']
            if set(byid)!=observed:raise ValueError('USD 实例与工程清单覆盖不符')
            for ob in imported:
                if ob.type=='MESH' and ob.parent:
                    for k in ('lab_definition_id','lab_definition_revision','lab_configuration','lab_source_occurrence','lab_source_cad_sha256','lab_source_cad_label'):
                        if k in ob.parent:ob[k]=ob.parent[k]
                for collection in list(ob.users_collection):collection.objects.unlink(ob)
                owner.objects.link(ob)
            owner['lab_source_key']=owner_key;owner['lab_package_digest']=target['package_digest'];owner['lab_readonly_source']=True
        except Exception:
            for ob in set(bpy.data.objects)-before:bpy.data.objects.remove(ob,do_unlink=True)
            bpy.data.collections.remove(owner);raise
    if not target.get('member'):
        # Alias the same owner geometry; the visible instance gets its own identity.
        return owner
    did=target['definition_id'];matches=[o for o in owner.objects if o.type=='EMPTY' and o.get('lab_definition_id')==did]
    if not matches:raise ValueError('没有找到成员对应的 USD 实例')
    wanted=target['entrypoints']['spatial'].get('prim_paths',[])
    oid=wanted[0].rsplit('/o_',1)[-1].replace('_','-') if wanted else None
    selected=next((o for o in matches if o.get('lab_source_occurrence')==oid),matches[0])
    subset=[selected,*selected.children_recursive];col=bpy.data.collections.new('库源 · '+request['title']);col.use_fake_user=True
    copies={o:o.copy() for o in subset};matrices={o:o.matrix_local.copy() for o in subset}
    for original,copy in copies.items():
        col.objects.link(copy);copy.parent=copies.get(original.parent);copy.matrix_parent_inverse=Matrix.Identity(4)
        copy.matrix_local=Matrix.Identity(4) if original==selected else matrices[original]
    col['lab_source_key']=key;col['lab_package_digest']=target['package_digest'];col['lab_member_rebased']=True;col['lab_source_occurrence']=selected.get('lab_source_occurrence','');col['lab_readonly_source']=True
    return col

def insert_asset(request,location=None):
    source=source_collection(request);scene=bpy.context.scene;unit=scene.unit_settings.scale_length
    if unit<=0:raise ValueError('场景单位比例无效')
    root=bpy.data.objects.new(request['title'],None);bpy.context.collection.objects.link(root)
    root.instance_type='COLLECTION';root.instance_collection=source;root.empty_display_type='PLAIN_AXES';root.empty_display_size=.06/unit
    root.location=location if location is not None else scene.cursor.location;root.scale=(1/unit,)*3
    root['lab_instance_id']=str(uuid.uuid4());root['lab_catalog_id']=request['catalog_id'];root['lab_asset_id']=request['target']['asset_id'];root['lab_package_digest']=request['target']['package_digest']
    root['lab_insert_kind']='layout_preview' if request['reuse_kind']=='station_template' else 'asset_instance';root['lab_source_json']=json.dumps(request,ensure_ascii=False)
    root['lab_member_rebased']=bool(request['target'].get('member'));root['lab_physics_qualified']=False
    for ob in bpy.context.selected_objects:ob.select_set(False)
    root.select_set(True);bpy.context.view_layer.objects.active=root;bpy.context.view_layer.update();return root

def make_editable(root):
    if root.instance_type!='COLLECTION' or not root.get('lab_instance_id'):raise ValueError('请选择一个资产实例')
    source=root.instance_collection;copies={o:o.copy() for o in source.objects};local={o:o.matrix_local.copy() for o in source.objects}
    for original,copy in copies.items():
        if original.data:copy.data=original.data.copy()
        bpy.context.collection.objects.link(copy);copy.parent=copies.get(original.parent,root);copy.matrix_parent_inverse=Matrix.Identity(4);copy.matrix_local=local[original]
    root.instance_type='NONE';root.instance_collection=None;root['lab_revision_state']='draft';return root

def export_instances(path):
    unit=bpy.context.scene.unit_settings.scale_length;rows=[];seen=set()
    for ob in bpy.context.scene.objects:
        if not ob.get('lab_instance_id'):continue
        if ob['lab_instance_id'] in seen:raise ValueError('发现重复实例身份；复制时请使用插件插入新实例')
        seen.add(ob['lab_instance_id']);matrix=[list(r) for r in ob.matrix_world]
        for i in range(3):matrix[i]=[x*unit for x in matrix[i]]
        rows.append({'instance_id':ob['lab_instance_id'],'catalog_id':ob['lab_catalog_id'],'asset_id':ob['lab_asset_id'],'package_digest':ob['lab_package_digest'],'world_matrix_m':matrix,'state':ob.get('lab_revision_state','instance'),'source':json.loads(ob['lab_source_json'])})
    Path(path).write_text(json.dumps({'schema':'unilab.blender-instances/1','units':'m','instances':rows,'os_graph_applied':False},ensure_ascii=False,indent=2),encoding='utf8')
    return rows
