"""Read the observed RoboDK version-1 numeric/mesh containers without RoboDK.

Supported, fail-closed subset: 4000-double robot header with link count;
1600-double tool header; triangle surfaces with RGBA + 14-byte vertices;
empty curve/point trailers. This is a sample-verified format interpretation,
not a full implementation of RoboDK's proprietary format or solver.
"""
from pathlib import Path
import argparse, hashlib, json, struct, zlib
import numpy as np

MAX_BYTES = 512 * 1024 * 1024
VERTEX = np.dtype([('xyz', '<f4', (3,)), ('normal_code', '<u2')])

def object_footer(data,offset):
    """Read observed optional QString-like provenance trailer, never follow paths.

    Structural contract from exact length-prefixed source bytes: four nullable
    UTF-16LE strings, one zero flag after string2, four/five terminal zero bytes.
    Only string2 may be nonempty in this subset. Its apparent source path is
    preserved as text, not interpreted as an external dependency or geometry.
    """
    start=offset;strings=[];fields=[]
    for i in range(4):
        if i==2:
            if offset>=len(data) or data[offset]!=0:raise ValueError('Unverified object footer flag')
            offset+=1
        if offset+4>len(data):raise ValueError('Truncated object footer string length')
        n=struct.unpack_from('<I',data,offset)[0];pos=offset;offset+=4
        if n==0xffffffff:value=None
        else:
            if n>1024*1024 or n%2 or offset+n>len(data):raise ValueError('Invalid object footer UTF16 length')
            value=data[offset:offset+n].decode('utf-16le',errors='strict');offset+=n
        if i!=1 and value not in (None,''):raise ValueError('Unverified non-provenance object footer string')
        strings.append(value);fields.append({'offset':pos,'byte_length':offset-pos,'value':value})
    remaining=data[offset:]
    if remaining not in (b'\0'*4,b'\0'*5):raise ValueError('Unverified object footer terminal fields')
    return {'offset':start,'byte_length':len(data)-start,'sha256':sha(data[start:]),'hex':data[start:].hex(),
            'strings':strings,'fields':fields,'terminal_zero_bytes':len(remaining),'semantics':'structurally decoded source text and zero flags; field meaning unverified; no paths opened'}

def sha(data):
    return hashlib.sha256(data).hexdigest()

def read(path):
    raw = Path(path).read_bytes()
    if len(raw) > MAX_BYTES or len(raw) < 32:
        raise ValueError('Unsupported file size')
    if raw[:8] == struct.pack('<d', 1.):
        data, encoding = raw, 'uncompressed'
    else:
        expected = struct.unpack_from('>I', raw)[0]
        if not 32 <= expected <= MAX_BYTES:
            raise ValueError('Unsupported compression/header')
        decoder = zlib.decompressobj()
        data = decoder.decompress(raw[4:], expected + 1)
        if len(data) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError('Incomplete or inconsistent zlib container')
        encoding = 'big-endian-length + zlib'
    version, header_count, width, height = struct.unpack_from('<4d', data)
    if version != 1 or (header_count, width, height) not in [(4000., 20., 200.), (1600., 16., 100.)]:
        raise ValueError('Unverified header version/dimensions')
    header_bytes = int(header_count) * 8
    if header_bytes > len(data):
        raise ValueError('Truncated numeric header')
    header = np.frombuffer(data[:header_bytes], dtype='<f8').copy()
    if not np.isfinite(header).all():
        raise ValueError('Non-finite numeric header')
    offset = header_bytes
    def u64():
        nonlocal offset
        if offset + 8 > len(data):
            raise ValueError('Truncated count')
        value = struct.unpack_from('<Q', data, offset)[0]
        offset += 8
        return value
    count = u64() if header_count == 4000 else 1
    if not 1 <= count <= 128:
        raise ValueError('Unreasonable link count')
    if header_count == 4000 and header[22] != count:
        raise ValueError('Header and geometry link counts disagree')
    links = []
    for link_index in range(count):
        surfaces = []
        ns = u64()
        if not 0 <= ns <= 100000:
            raise ValueError('Unsupported surface count')
        for surface_index in range(ns):
            start = offset
            nt = u64()
            end = offset + 16 + nt * 42
            if not 0 <= nt <= 10000000 or end > len(data) or (nt==0 and header_count!=1600):
                raise ValueError('Invalid triangle count or truncated surface')
            rgba = struct.unpack_from('<4f', data, offset)
            if not np.isfinite(rgba).all() or min(rgba) < 0 or max(rgba) > 1:
                raise ValueError('Unverified RGBA encoding')
            offset += 16
            vertices = np.frombuffer(data, dtype=VERTEX, count=nt*3, offset=offset)
            points = vertices['xyz'].copy().reshape(nt, 3, 3)
            if not np.isfinite(points).all():
                raise ValueError('Non-finite mesh')
            surfaces.append({'index': surface_index, 'triangles': nt, 'rgba': rgba,
                'decompressed_range': [start, end], 'vertices_offset': offset,
                'source_record_sha256': sha(data[start:end]), 'points': points})
            offset = end
        trailer_offset = offset
        trailer = [u64(), u64()]
        if trailer != [0, 0]:
            raise ValueError('Nonempty auxiliary geometry unsupported; refusing partial decode')
        links.append({'index': link_index, 'surfaces': surfaces, 'trailer_offset': trailer_offset})
    opaque_footer = None
    if header_count == 1600 and offset<len(data):
        opaque_footer = object_footer(data,offset)
        offset=len(data)
    if offset != len(data):
        raise ValueError('Unparsed trailing bytes: %d' % (len(data)-offset))
    return {'source': str(Path(path).resolve()), 'source_sha256': sha(raw), 'source_bytes': len(raw),
        'encoding': encoding, 'decoded_sha256': sha(data), 'decoded_bytes': len(data),
        'fully_consumed_container': True, 'opaque_footer': opaque_footer, 'header': header, 'links': links,
        'unknowns': ['numeric header fields beyond compared source values', 'packed two-byte normal codec',
            'full native solver/controller semantics', 'original CAD absent']}

def export(source, output, native_evidence, reference_stl):
    from pxr import Usd, UsdGeom, UsdShade, Sdf, Gf, Vt
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    parsed = read(source)
    evidence = json.loads(Path(native_evidence).read_text(encoding='utf-8'))
    models = evidence['models'] if 'models' in evidence else [evidence]
    source_model = next(m for m in models if m['sha256'] == parsed['source_sha256'])
    poses = source_model.get('link_poses_mm') or [np.eye(4).tolist()]
    if len(poses) != len(parsed['links']):
        raise ValueError('Native reference frame count does not match parsed links')
    stage = Usd.Stage.CreateNew(str(output / 'asset.usda'))
    UsdGeom.SetStageMetersPerUnit(stage, 1)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    root = UsdGeom.Xform.Define(stage, '/Asset')
    stage.SetDefaultPrim(root.GetPrim())
    root.GetPrim().SetCustomData({'source_sha256': parsed['source_sha256'], 'geometry_status': 'complete_observed_container',
        'cad_status': 'absent', 'kinematics_status': 'historical_native_frames_only', 'physics_status': 'not_qualified',
        'material_status': 'source_RGBA; color-space and optical calibration unverified'})
    materials, rows = {}, []
    for link in parsed['links']:
        idx = link['index']; xp = '/Asset/link_%d' % idx
        xf = UsdGeom.Xform.Define(stage, xp)
        pose = np.array(poses[idx], dtype=float); pose[:3, 3] *= .001
        xf.AddTransformOp().Set(Gf.Matrix4d(pose.T.tolist()))
        triangles = np.concatenate([s['points'] for s in link['surfaces']])
        reference = Path(reference_stl) / (('tool.stl') if len(poses) == 1 else 'link-%d.stl' % idx)
        comparison = {'status': 'no_prior_native_STL', 'vertices_match': None}
        if reference.exists():
            blob = reference.read_bytes(); count = struct.unpack_from('<I', blob, 80)[0]
            if len(blob) != 84 + count*50:
                raise ValueError('Invalid reference STL')
            arr = np.frombuffer(blob, dtype=np.dtype([('n','<f4',(3,)),('v','<f4',(3,3)),('a','<u2')]), offset=84)
            same = count == len(triangles) and np.array_equal(arr['v'], triangles)
            if not same:
                raise ValueError('Offline triangles disagree with native STL: %s' % reference)
            comparison = {'status': 'passed', 'vertices_match': True, 'ordered_float32_vertices_exact': True,
                'source': str(reference.resolve()), 'sha256': sha(blob)}
        pts = triangles.astype(float) * .001
        cross = np.cross(pts[:,1]-pts[:,0], pts[:,2]-pts[:,0])
        length = np.linalg.norm(cross, axis=1)
        normals = cross / np.where(length > 0, length, 1)[:,None]
        mesh = UsdGeom.Mesh.Define(stage, xp + '/Geometry')
        mesh.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(pts.reshape(-1,3).astype('float32')))
        mesh.CreateFaceVertexCountsAttr([3] * len(pts))
        mesh.CreateFaceVertexIndicesAttr(list(range(len(pts)*3)))
        mesh.CreateSubdivisionSchemeAttr('none')
        mesh.CreateNormalsAttr(Vt.Vec3fArray.FromNumpy(normals.astype('float32')))
        mesh.SetNormalsInterpolation(UsdGeom.Tokens.uniform)
        mesh.CreateExtentAttr([Gf.Vec3f(*pts.min(axis=(0,1))), Gf.Vec3f(*pts.max(axis=(0,1)))])
        UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim())
        face_offset = 0
        for surface in link['surfaces']:
            color = tuple(surface['rgba'])
            if color not in materials:
                mat = UsdShade.Material.Define(stage, '/Asset/Materials/SourceRGBA_%d' % len(materials))
                shader = UsdShade.Shader.Define(stage, str(mat.GetPath()) + '/Shader')
                shader.CreateIdAttr('UsdPreviewSurface')
                shader.CreateInput('diffuseColor', Sdf.ValueTypeNames.Color3f).Set(Gf.Vec3f(*color[:3]))
                shader.CreateInput('opacity', Sdf.ValueTypeNames.Float).Set(color[3])
                mat.CreateSurfaceOutput().ConnectToSource(shader.ConnectableAPI(), 'surface')
                mat.GetPrim().SetCustomData({'source_rgba': list(color), 'roughness_metallic': 'not recovered'})
                materials[color] = mat
            subset = UsdGeom.Subset.Define(stage, xp + '/Geometry/surface_%d' % surface['index'])
            subset.CreateElementTypeAttr('face'); subset.CreateFamilyNameAttr('materialBind')
            subset.CreateIndicesAttr(list(range(face_offset, face_offset + surface['triangles'])))
            UsdShade.MaterialBindingAPI.Apply(subset.GetPrim()).Bind(materials[color])
            face_offset += surface['triangles']
        UsdGeom.Subset.SetFamilyType(mesh, 'materialBind', UsdGeom.Tokens.partition)
        rows.append({'link': idx, 'triangles': len(pts), 'surfaces': len(link['surfaces']),
            'native_STL_comparison': comparison, 'degenerate_area_le_1e_15_m2': int((length*.5 <= 1e-15).sum()),
            'usd_path': str(mesh.GetPath()), 'pose_source': 'prior native JointPoses, exact original file SHA',
            'bounds_m': [pts.min(axis=(0,1)).tolist(), pts.max(axis=(0,1)).tolist()]})
    if 'tool_pose_mm' in source_model:
        pose = np.array(source_model['tool_pose_mm']); pose[:3,3] *= .001
        UsdGeom.Xform.Define(stage, '/Asset/Interfaces/NativeTCP').AddTransformOp().Set(Gf.Matrix4d(pose.T.tolist()))
    stage.GetRootLayer().Save()
    reopened = Usd.Stage.Open(str(output / 'asset.usda'))
    assert sum(p.IsA(UsdGeom.Mesh) for p in reopened.Traverse()) == len(rows)
    report = {k:v for k,v in parsed.items() if k not in ('links','header')}
    report.update(status='geometry_conversion_passed', links=rows, rgba_materials=len(materials),
        triangles=sum(r['triangles'] for r in rows), usd_sha256=sha((output/'asset.usda').read_bytes()),
        frame_source_sha256=sha(Path(native_evidence).read_bytes()), new_native_API_calls=0,
        full_simready='not_qualified', collision='not_authored', dynamics='not_run',
        notes=['All decoded triangles retained, including source degeneracies.',
            'CR5 links 5/6 have structural and USD checks, but no previous native STL comparison.',
            'Uses historical native frame evidence: geometry extraction is offline; all-format pose decoding not claimed.'])
    (output/'conversion.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    headers = {'values': parsed['header'].tolist(), 'interpretation': 'raw numeric source table, mostly unknown'}
    (output/'source-header.json').write_text(json.dumps(headers), encoding='utf-8')
    mapping = [{**{k:v for k,v in link.items() if k!='surfaces'}, 'surfaces': [
        {k:v for k,v in surf.items() if k!='points'} for surf in link['surfaces']]} for link in parsed['links']]
    (output/'source-surfaces.json').write_text(json.dumps(mapping), encoding='utf-8')
    assert sha(Path(source).read_bytes()) == parsed['source_sha256']
    return report

if __name__ == '__main__':
    ap=argparse.ArgumentParser(); ap.add_argument('--source',required=True); ap.add_argument('--output',required=True)
    ap.add_argument('--native-evidence',required=True); ap.add_argument('--reference-stl',required=True)
    a=ap.parse_args(); r=export(a.source,a.output,a.native_evidence,a.reference_stl)
    print(json.dumps({k:r[k] for k in ('status','triangles','rgba_materials','new_native_API_calls')},indent=2))
