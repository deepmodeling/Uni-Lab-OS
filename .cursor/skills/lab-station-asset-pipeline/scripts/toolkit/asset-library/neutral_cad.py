"""STEP/XDE to USD with definition/occurrence and source colour correspondence.

OCP is an optional CAD-kernel dependency; no historical capture is consumed.
"""
import math,shutil
from pathlib import Path
from core import write,digest,uid,IDENTITY,mul

def reference_geometry(shape,stage,path,chord):
    """Preserve face-free CAD definitions (assembly guide wires and points)."""
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_EDGE,TopAbs_VERTEX
    from OCP.TopoDS import TopoDS
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Curve
    from OCP.GCPnts import GCPnts_QuasiUniformDeflection
    from pxr import UsdGeom
    coords=[];sizes=[]
    edges=TopExp_Explorer(shape,TopAbs_EDGE)
    while edges.More():
        curve=BRepAdaptor_Curve(TopoDS.Edge_s(edges.Current()))
        sampled=GCPnts_QuasiUniformDeflection(curve,chord)
        if not sampled.IsDone() or sampled.NbPoints()<2:raise ValueError('Unresolved CAD reference curve')
        sizes.append(sampled.NbPoints())
        coords.extend(tuple(v*.001 for v in sampled.Value(i).Coord()) for i in range(1,sampled.NbPoints()+1))
        edges.Next()
    if sizes:
        curves=UsdGeom.BasisCurves.Define(stage,path+'/ReferenceCurves')
        curves.CreateTypeAttr('linear');curves.CreateWrapAttr('nonperiodic')
        curves.CreateCurveVertexCountsAttr(sizes);curves.CreatePointsAttr(coords)
        curves.CreatePurposeAttr('guide')
    isolated=[];vertices=TopExp_Explorer(shape,TopAbs_VERTEX,TopAbs_EDGE)
    while vertices.More():
        point=BRep_Tool.Pnt_s(TopoDS.Vertex_s(vertices.Current()))
        isolated.append(tuple(v*.001 for v in point.Coord()));vertices.Next()
    if isolated:
        points=UsdGeom.Points.Define(stage,path+'/ReferencePoints')
        points.CreatePointsAttr(isolated);points.CreatePurposeAttr('guide')
    if not (sizes or isolated):raise ValueError('Empty CAD reference definition')
    return {'reference_edges':len(sizes),'isolated_points':len(isolated)}

def collapsed_trim_face_evidence(face):
    """Recognize a narrow zero-area case, never infer collapse from failed meshing.

    One wire traverses the same linear UV segment in opposite directions. CAD
    retains this face; a renderer cannot represent its zero-area interior.
    """
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_WIRE,TopAbs_REVERSED
    from OCP.TopoDS import TopoDS
    from OCP.BRepTools import BRepTools_WireExplorer
    from OCP.BRepAdaptor import BRepAdaptor_Curve2d
    from OCP.GeomAbs import GeomAbs_BSplineCurve
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    wires=[];ex=TopExp_Explorer(face,TopAbs_WIRE)
    while ex.More():wires.append(TopoDS.Wire_s(ex.Current()));ex.Next()
    if len(wires)!=1:return None
    edges=[];ex=BRepTools_WireExplorer(wires[0],face)
    while ex.More():edges.append(ex.Current());ex.Next()
    if len(edges)!=2 or (edges[0].Orientation()==TopAbs_REVERSED)==(edges[1].Orientation()==TopAbs_REVERSED):return None
    curves=[BRepAdaptor_Curve2d(e,face) for e in edges]
    if any(c.GetType()!=GeomAbs_BSplineCurve for c in curves):return None
    splines=[c.BSpline() for c in curves]
    if any(s.Degree()!=1 or s.NbPoles()!=2 or s.IsRational() or s.NbKnots()!=2 for s in splines):return None
    a,b=splines
    if any(abs(a.Knot(i)-b.Knot(i))>1e-13 or a.Multiplicity(i)!=b.Multiplicity(i) for i in (1,2)):return None
    if any(abs(getattr(curves[0],k)()-getattr(curves[1],k)())>1e-13 for k in ('FirstParameter','LastParameter')):return None
    delta=max(a.Pole(i).Distance(b.Pole(i)) for i in (1,2))
    if delta>1e-13:return None
    area=GProp_GProps();BRepGProp.SurfaceProperties_s(face,area)
    if abs(area.Mass())>1e-10:return None
    return {'reason':'opposite coincident linear UV trims; no surface interior','area_mm2':area.Mass(),'max_uv_pole_difference':delta,'source_cad_preserved':True}


def refine_planar_face(face):
    """Refine only the missing planar trim, leaving its shared CAD edges intact."""
    from OCP.BRep import BRep_Tool
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
    from OCP.BRepGProp import BRepGProp
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.GeomAbs import GeomAbs_Plane
    from OCP.GProp import GProp_GProps
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS
    if BRepAdaptor_Surface(face).GetType()!=GeomAbs_Plane:
        return None
    props=GProp_GProps()
    BRepGProp.SurfaceProperties_s(face,props)
    expected=props.Mass()
    if not math.isfinite(expected) or expected<=0:
        return None
    copy=TopoDS.Face_s(BRepBuilderAPI_Copy(face,True,False).Shape())
    if not BRepMesh_IncrementalMesh(copy,.00005,False,.1,False).IsDone():
        return None
    location=TopLoc_Location()
    mesh=BRep_Tool.Triangulation_s(copy,location)
    if mesh is None or not mesh.NbTriangles():
        return None
    areas=[]
    from OCP.gp import gp_Vec
    for i in range(1,mesh.NbTriangles()+1):
        a,b,c=[mesh.Node(j) for j in mesh.Triangle(i).Get()]
        areas.append(gp_Vec(a,b).Crossed(gp_Vec(a,c)).Magnitude()*.5)
    measured=math.fsum(areas)
    if not math.isclose(measured,expected,rel_tol=.0001,abs_tol=.0001):
        return None
    return mesh,location,{'chord_mm':.00005,'cad_area_mm2':expected,'mesh_area_mm2':measured}


def convert(source,work,identity,deflection_mm=.05,allow_collapsed_trim_faces=False,source_colour=None):
    if not math.isfinite(deflection_mm) or deflection_mm<=0:raise ValueError('Deflection must be finite and positive')
    from OCP.STEPCAFControl import STEPCAFControl_Reader
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.TDocStd import TDocStd_Document
    from OCP.TCollection import TCollection_ExtendedString,TCollection_AsciiString
    from OCP.XCAFDoc import XCAFDoc_DocumentTool,XCAFDoc_ShapeTool as ST,XCAFDoc_ColorSurf,XCAFDoc_ColorGen
    from OCP.TDF import TDF_Label,TDF_LabelSequence,TDF_Tool
    from OCP.TDataStd import TDataStd_Name
    from OCP.TopoDS import TopoDS
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopAbs import TopAbs_FACE,TopAbs_REVERSED
    from OCP.TopLoc import TopLoc_Location
    from OCP.BRep import BRep_Tool
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.BRepCheck import BRepCheck_Analyzer
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.Quantity import Quantity_Color
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    from pxr import Usd,UsdGeom,UsdShade,Sdf,Gf
    source=Path(source).resolve();work=Path(work).resolve();work.mkdir(parents=True,exist_ok=False)
    import re
    if re.search(r'\b(?:DOCUMENT_FILE|EXTERNALLY_DEFINED_ITEM)\s*\(',source.read_text('latin1'),re.I):
        raise ValueError('External STEP document/item needs an explicit dependency adapter')
    copied=work/'source'/source.name;copied.parent.mkdir();shutil.copyfile(source,copied)
    h=digest(source);doc=TDocStd_Document(TCollection_ExtendedString('BinXCAF'));reader=STEPCAFControl_Reader()
    reader.SetColorMode(True);reader.SetNameMode(True)
    if reader.ReadFile(str(source))!=IFSelect_RetDone:raise ValueError('STEP read failed')
    reader.ChangeReader().SetSystemLengthUnit(1.)  # OCCT working coordinates in millimetres.
    if not reader.Transfer(doc):raise ValueError('STEP XDE transfer failed')
    st=XCAFDoc_DocumentTool.ShapeTool_s(doc.Main());ct=XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
    stage=Usd.Stage.CreateNew(str(work/'asset.usda'));UsdGeom.SetStageMetersPerUnit(stage,1);UsdGeom.SetStageUpAxis(stage,'Z')
    top=UsdGeom.Xform.Define(stage,'/Asset');stage.SetDefaultPrim(top.GetPrim());defs={};occ=[];stats={'faces':0,'triangles':0,'missing_colour_faces':0,'filtered_zero_area_triangles':0,'non_renderable_cad_faces':[]}
    def label_id(label):
        s=TCollection_AsciiString();TDF_Tool.Entry_s(label,s);return s.ToCString()
    def name(label):
        a=TDataStd_Name();return a.Get().ToExtString() if label.FindAttribute(TDataStd_Name.GetID_s(),a) else label_id(label)
    def colour(*shapes):
        for shape in shapes:
            for kind in (XCAFDoc_ColorSurf,XCAFDoc_ColorGen):
                c=Quantity_Color()
                if ct.GetColor(shape,kind,c):return [c.Red(),c.Green(),c.Blue()]
        return None
    mats={}
    def material(rgb):
        key=tuple(rgb)
        if key not in mats:
            m=UsdShade.Material.Define(stage,'/Asset/Materials/c'+str(len(mats)));s=UsdShade.Shader.Define(stage,str(m.GetPath())+'/Preview')
            s.CreateIdAttr('UsdPreviewSurface');s.CreateInput('diffuseColor',Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*rgb));m.CreateSurfaceOutput().ConnectToSource(s.ConnectableAPI(),'surface');mats[key]=m
        return mats[key]
    def visit(label,parent,parent_world,lineage):
        ref=TDF_Label();definition=ref if ST.GetReferredShape_s(label,ref) else label
        did=uid(identity+'/definition/'+label_id(definition));oid=uid(identity+'/occurrence/'+lineage)
        t=ST.GetLocation_s(label).Transformation();local=[[t.Value(i+1,j+1)*(0.001 if j==3 else 1) for j in range(4)] for i in range(3)]+[[0,0,0,1]]
        world=mul(parent_world,local);path=parent+'/o_'+oid.replace('-','_');xf=UsdGeom.Xform.Define(stage,path)
        xf.AddTransformOp().Set(Gf.Matrix4d(*[local[j][i] for i in range(4) for j in range(4)]));xf.GetPrim().SetDisplayName(name(label))
        xf.GetPrim().SetCustomDataByKey('engineering:definition_id',did);xf.GetPrim().SetCustomDataByKey('engineering:id',oid)
        occ.append({'id':oid,'instance_id':oid,'definition_id':did,'parent_prim':parent,'prim':path,'usd_prim':path,'source_label':name(label),'local_matrix':local,'world_matrix':world,'world_matrix_m':world})
        defs.setdefault(did,{'source_label':label_id(definition),'name':name(definition),'source_sha256':h,'kind':'assembly' if ST.IsAssembly_s(definition) else 'shape'})
        if ST.IsAssembly_s(definition):
            seq=TDF_LabelSequence();ST.GetComponents_s(definition,seq)
            for k in range(1,seq.Length()+1):visit(seq.Value(k),path,world,lineage+'/'+label_id(seq.Value(k)))
            return
        shape=ST.GetShape_s(definition)
        if shape.IsNull() or not BRepCheck_Analyzer(shape).IsValid():raise ValueError('Invalid BRep at '+label_id(definition))
        if not TopExp_Explorer(shape,TopAbs_FACE).More():
            defs[did].update(kind='reference_geometry',**reference_geometry(shape,stage,path,deflection_mm))
            return
        # OCCT may report IsDone even when a narrow trim received no triangles.
        # Retry the whole definition so shared edges use the same discretisation.
        for factor in (1.,.3,.08):
            chord=deflection_mm*factor
            if factor!=1.:
                from OCP.BRepTools import BRepTools
                BRepTools.Clean_s(shape)
            if not BRepMesh_IncrementalMesh(shape,chord,False,.3,False).IsDone():
                if factor==.08:raise ValueError('Tessellation failed after bounded refinement')
                continue
            probe=TopExp_Explorer(shape,TopAbs_FACE)
            while probe.More():
                f=TopoDS.Face_s(probe.Current())
                mesh_data=BRep_Tool.Triangulation_s(f,TopLoc_Location())
                if (mesh_data is None or mesh_data.NbTriangles()==0) and not (allow_collapsed_trim_faces and collapsed_trim_face_evidence(f)):
                    break
                probe.Next()
            if not probe.More():break
        if factor!=1.:stats.setdefault('mesh_refinements_mm',{})[did]=chord
        points=[];indices=[];groups={};ex=TopExp_Explorer(shape,TopAbs_FACE);face_count=0
        while ex.More():
            face=TopoDS.Face_s(ex.Current());loc=TopLoc_Location();tri=BRep_Tool.Triangulation_s(face,loc)
            collapsed=collapsed_trim_face_evidence(face) if allow_collapsed_trim_faces else None
            if collapsed is not None:
                row={'definition_id':did,'face_index':face_count+1,**collapsed};stats['non_renderable_cad_faces'].append(row)
                marker=UsdGeom.Scope.Define(stage,path+'/NonRenderableCADFaces/face_'+str(face_count+1)).GetPrim()
                marker.SetCustomDataByKey('engineering:cad_face_index',face_count+1)
                marker.SetCustomDataByKey('engineering:representation_note',collapsed['reason'])
                marker.SetCustomDataByKey('engineering:source_cad_preserved',True)
                face_count+=1;ex.Next();continue
            if tri is None or tri.NbTriangles()==0:
                refined=refine_planar_face(face)
                if refined is None:
                    raise ValueError('Face has no verified triangulation at face '+str(face_count+1)+'; source retained, no implicit omission')
                tri,loc,measurement=refined
                stats.setdefault('local_face_refinements',[]).append(dict(definition_id=did,face_index=face_count+1,**measurement))
            rgb=colour(face,ST.GetShape_s(label),shape)
            if rgb is None and source_colour is not None:rgb=source_colour
            if rgb is None:stats['missing_colour_faces']+=1;rgb=[.5,.5,.5]
            off=len(points);tf=loc.Transformation()
            for k in range(1,tri.NbNodes()+1):
                p=tri.Node(k).Transformed(tf);points.append([p.X()*.001,p.Y()*.001,p.Z()*.001])
            first_index=len(indices)
            for k in range(1,tri.NbTriangles()+1):
                a,b,c=tri.Triangle(k).Get();a,b,c=[off+x-1 for x in (a,b,c)]
                if face.Orientation()==TopAbs_REVERSED:b,c=c,b
                va,vb,vc=[Gf.Vec3d(*points[x]) for x in (a,b,c)]
                if Gf.Cross(vb-va,vc-va).GetLength()<1e-18:stats['filtered_zero_area_triangles']+=1;continue
                groups.setdefault(tuple(rgb),[]).append(len(indices)//3);indices.extend((a,b,c))
            if len(indices)==first_index:
                raise ValueError('CAD face '+str(face_count+1)+' produced only degenerate triangles')
            face_count+=1;ex.Next()
        if not indices:raise ValueError('No usable triangles')
        mesh=UsdGeom.Mesh.Define(stage,path+'/Geometry');mesh.CreatePointsAttr(points);mesh.CreateFaceVertexCountsAttr([3]*(len(indices)//3));mesh.CreateFaceVertexIndicesAttr(indices);mesh.CreateSubdivisionSchemeAttr('none')
        for rgb,faces in groups.items():
            subset=UsdGeom.Subset.Define(stage,str(mesh.GetPath())+'/color'+str(len(mesh.GetPrim().GetChildren())));subset.CreateElementTypeAttr('face');subset.CreateFamilyNameAttr('materialBind');subset.CreateIndicesAttr(faces);UsdShade.MaterialBindingAPI.Apply(subset.GetPrim()).Bind(material(rgb))
        UsdGeom.Subset.SetFamilyType(mesh,'materialBind','partition')
        volume=GProp_GProps();area=GProp_GProps();BRepGProp.VolumeProperties_s(shape,volume);BRepGProp.SurfaceProperties_s(shape,area)
        center=volume.CentreOfMass();box=Bnd_Box();BRepBndLib.AddOptimal_s(shape,box,False,False);bb=box.Get()
        defs[did].update(volume_m3=volume.Mass()*1e-9,area_m2=area.Mass()*1e-6,center_of_volume_m=[center.X()*.001,center.Y()*.001,center.Z()*.001],bounds_m=[x*.001 for x in bb],faces=face_count)
        for key in ('volume_m3','area_m2'):xf.GetPrim().CreateAttribute('unilab:cad:'+key,Sdf.ValueTypeNames.Double).Set(defs[did][key])
        xf.GetPrim().CreateAttribute('unilab:cad:center_of_volume_m',Sdf.ValueTypeNames.Double3).Set(Gf.Vec3d(*defs[did]['center_of_volume_m']))
        stats['faces']+=face_count;stats['triangles']+=len(indices)//3
    roots=TDF_LabelSequence();st.GetFreeShapes(roots)
    if roots.Length()==0:raise ValueError('No XDE roots')
    for k in range(1,roots.Length()+1):visit(roots.Value(k),'/Asset',IDENTITY,label_id(roots.Value(k)))
    if not stats['triangles']:raise ValueError('CAD contains reference geometry only; no usable surface triangles')
    stage.GetRootLayer().Save();stage=None
    mapping={'schema':'unilab.neutral-cad-source-map/1','source_sha256':h,'units':'m','frame_policy':'XDE local transforms; kernel mm converted once to m','definitions':defs,'occurrences':occ}
    write(work/'cad-mapping.json',mapping)
    result={'status':'passed','source_sha256':h,'entrypoints':{'usd':'asset.usda','cad':copied.relative_to(work).as_posix()},'sources':[{'path':copied.relative_to(work).as_posix(),'sha256':h,'original_name':source.name}],'cad_mapping_data':mapping,'conversion':{'family':'step','working_units':'mm','deflection_mm':deflection_mm,**stats,'scope':'source colours or explicit grey fallback; geometry calculation is not material/physical qualification'}}
    write(work/'conversion.json',result);return result
