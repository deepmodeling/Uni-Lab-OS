"""从资产包生成 web 轻量变体：prim 层级、名字、关节、变换、材质绑定全部不变，
只替换网格（meshoptimizer 减面）和贴图（缩到上限）。原包不动，输出到新目录。

用法：python webprofile.py <包的 simready_usd 目录> <输出目录> [--rel 1e-3] [--texmax 1024] --meshopt-from <含 meshoptimizer 的 node_modules>
做成资产库新修订（含校验、web.json、入口、血缘、封包）用同目录的 make_web_profile_revision.py。
"""
import argparse, json, os, shutil, struct, subprocess, sys, tempfile
import numpy as np
from pxr import Sdf, Vt, Gf

HERE = os.path.dirname(os.path.abspath(__file__))
CORNER_INTERP = ('faceVarying',)
VERTEX_INTERP = ('vertex', 'varying')


def attr_value(layer, prim_path, name):
    spec = layer.GetAttributeAtPath(prim_path.AppendProperty(name))
    if spec is None or not spec.HasDefaultValue():
        return None, None
    return spec, spec.default


def interp_of(spec):
    if spec is None:
        return None
    if spec.HasInfo('interpolation'):
        return spec.GetInfo('interpolation')
    return None


def collect_primvars(layer, path, prim_spec):
    """可随顶点搬运的属性：normals 和 primvars:*（float 向量），返回 [(名字, spec, 值数组, 插值, indices)]。"""
    out = []
    for prop in prim_spec.properties:
        if not isinstance(prop, Sdf.AttributeSpec):
            continue
        name = prop.name
        if name.endswith(':indices'):
            continue
        if name != 'normals' and not name.startswith('primvars:'):
            continue
        if not prop.HasDefaultValue() or prop.default is None:
            continue
        tname = str(prop.typeName)
        if not any(tname.startswith(t) for t in ('normal3f', 'float3', 'texCoord2f', 'float2', 'vector3f', 'color3f', 'float[]', 'point3f')):
            continue
        interp = interp_of(prop) or ('vertex' if name == 'normals' else 'constant')
        if interp in ('constant',):
            continue
        idx_spec = layer.GetAttributeAtPath(path.AppendProperty(name + ':indices'))
        idx = None
        if idx_spec is not None and idx_spec.HasDefaultValue():
            try:
                idx = np.array(idx_spec.default, dtype=np.int64)
            except TypeError:  # ValueBlock
                idx = None
        try:
            arr = np.array(prop.default, dtype=np.float32)
        except TypeError:
            continue
        if arr.ndim == 1:
            arr = arr.reshape(-1, 1)
        out.append((name, prop, arr, interp, idx))
    return out


def triangulate(counts, indices):
    """扇形三角化：返回每个三角形的角在原 faceVertexIndices 里的序号，以及所属面。"""
    corners, faces = [], []
    start = 0
    for f, c in enumerate(counts):
        for k in range(1, c - 1):
            corners.extend((start, start + k, start + k + 1))
            faces.append(f)
        start += c
    return np.array(corners, dtype=np.int64), np.array(faces, dtype=np.int64)


def process_layer(path, args, report):
    layer = Sdf.Layer.FindOrOpen(path)
    if layer is None:
        return
    meshes = []
    def visit(spec_path):
        spec = layer.GetPrimAtPath(spec_path)
        if spec is None:
            return
        if spec.typeName == 'Mesh':
            meshes.append(spec_path)
        for child in spec.nameChildren:
            visit(child.path)
    for root in layer.rootPrims:
        visit(root.path)
    if not meshes:
        return

    jobs, prepared = [], []
    blob = bytearray()
    def put(arr):
        off = len(blob)
        blob.extend(arr.tobytes())
        return off
    for mid, mpath in enumerate(meshes):
        spec = layer.GetPrimAtPath(mpath)
        _, pts = attr_value(layer, mpath, 'points')
        _, counts = attr_value(layer, mpath, 'faceVertexCounts')
        _, fvi = attr_value(layer, mpath, 'faceVertexIndices')
        if pts is None or counts is None or fvi is None:
            continue
        pts = np.array(pts, dtype=np.float32).reshape(-1, 3)
        counts = np.array(counts, dtype=np.int64)
        fvi = np.array(fvi, dtype=np.int64)
        if len(counts) == 0 or np.any(counts < 3):
            continue
        primvars = collect_primvars(layer, mpath, spec)
        # 带孔面不处理，保持原网格。
        if attr_value(layer, mpath, 'holeIndices')[1]:
            report['skipped'] += 1
            continue
        tri_corner, tri_face = triangulate(counts, fvi)
        tris_before = len(tri_face)
        if tris_before < args.min_triangles:
            continue
        # 每个三角角点的属性：位置 + 各 primvar（按插值取值）
        point_idx = fvi[tri_corner]
        columns = [pts[point_idx]]
        carried = []
        for name, pspec, arr, interp, idx in primvars:
            if interp in CORNER_INTERP:
                sel = tri_corner
            elif interp in VERTEX_INTERP:
                sel = point_idx
            elif interp == 'uniform':
                sel = np.repeat(tri_face, 3)
            else:
                continue
            if idx is not None:
                sel = idx[sel]
            if sel.max(initial=-1) >= len(arr):
                break
            columns.append(arr[sel])
            carried.append((name, pspec, arr.shape[1]))
        else:
            pass
        if len(carried) != len([p for p in primvars if p[3] in CORNER_INTERP + VERTEX_INTERP + ('uniform',)]):
            report['skipped'] += 1
            continue
        corner_data = np.concatenate(columns, axis=1).astype(np.float32)
        # 角点去重 → 顶点
        uniq, inverse = np.unique(corner_data, axis=0, return_inverse=True)
        inverse = inverse.reshape(-1).astype(np.uint32)
        positions = np.ascontiguousarray(uniq[:, :3])
        attrs = np.ascontiguousarray(uniq[:, 3:]) if uniq.shape[1] > 3 else np.zeros((len(uniq), 0), np.float32)
        weights = []
        for name, _, width in carried:
            w = float(os.environ.get('W_NORMAL', '0.5')) if 'normal' in name.lower() else float(os.environ.get('W_UV', '0.1'))
            weights.extend([w] * width)
        # 面组：GeomSubset（elementType=face）各自成组，其余面一组
        subsets = []
        for child in spec.nameChildren:
            if child.typeName == 'GeomSubset':
                _, ind = attr_value(layer, child.path, 'indices')
                et = layer.GetAttributeAtPath(child.path.AppendProperty('elementType'))
                if et is not None and et.HasDefaultValue() and et.default not in (None, 'face'):
                    subsets = None
                    break
                subsets.append((child.path, set(int(i) for i in (ind or []))))
        if subsets is None:
            report['skipped'] += 1
            continue
        tri_groups = []
        assigned = np.zeros(len(tri_face), dtype=bool)
        for spath, faceset in subsets:
            mask = np.isin(tri_face, list(faceset)) if faceset else np.zeros(len(tri_face), bool)
            assigned |= mask
            tri_groups.append((spath, np.nonzero(mask)[0]))
        rest = np.nonzero(~assigned)[0]
        tri_groups.append((None, rest))
        groups = []
        for spath, tris in tri_groups:
            idx = inverse.reshape(-1, 3)[tris].reshape(-1).astype(np.uint32)
            groups.append({'idx': put(idx), 'n': int(len(idx))})
        jobs.append({'id': len(prepared), 'nv': int(len(positions)), 'pos': put(positions), 'attrStride': int(attrs.shape[1]),
                     'attr': put(attrs) if attrs.shape[1] else 0, 'weights': weights, 'groups': groups})
        prepared.append((mpath, positions, attrs, carried, tri_groups, tris_before))
    if not jobs:
        return
    tmp = tempfile.mkdtemp(prefix='webvar-')
    with open(os.path.join(tmp, 'job.json'), 'w') as f:
        json.dump({'meshes': jobs, 'relativeError': args.rel, 'minTriangles': args.min_triangles // 1}, f)
    with open(os.path.join(tmp, 'job.bin'), 'wb') as f:
        f.write(blob)
    env = dict(os.environ, MESHOPT_FROM=args.meshopt_from)
    subprocess.run([os.environ.get('NODE', 'node'), os.path.join(HERE, 'simplify.mjs'), os.path.join(tmp, 'job'), os.path.join(tmp, 'out')], check=True, env=env)
    result = json.load(open(os.path.join(tmp, 'out.json')))
    obin = open(os.path.join(tmp, 'out.bin'), 'rb').read()
    shutil.rmtree(tmp, ignore_errors=True)

    for mesh_out, (mpath, positions, attrs, carried, tri_groups, tris_before) in zip(result['meshes'], prepared):
        all_idx, ranges, start_tri = [], [], 0
        for g in mesh_out['groups']:
            arr = np.frombuffer(obin, dtype=np.uint32, count=g['n'], offset=g['idx'])
            all_idx.append(arr)
            ranges.append((start_tri, start_tri + g['n'] // 3))
            start_tri += g['n'] // 3
        idx = np.concatenate(all_idx) if all_idx else np.zeros(0, np.uint32)
        used, remap = np.unique(idx, return_inverse=True)
        new_pts = positions[used]
        new_attrs = attrs[used]
        new_idx = remap.astype(np.int32)
        tris_after = len(new_idx) // 3
        report['tris_before'] += tris_before
        report['tris_after'] += tris_after
        layer.GetAttributeAtPath(mpath.AppendProperty('points')).default = Vt.Vec3fArray.FromNumpy(new_pts.astype(np.float32))
        layer.GetAttributeAtPath(mpath.AppendProperty('faceVertexCounts')).default = Vt.IntArray.FromNumpy(np.full(tris_after, 3, np.int32))
        layer.GetAttributeAtPath(mpath.AppendProperty('faceVertexIndices')).default = Vt.IntArray.FromNumpy(new_idx)
        col = 0
        for name, pspec, width in carried:
            data = new_attrs[:, col:col + width]
            col += width
            pspec.default = type(pspec.default).FromNumpy(data.reshape(-1) if width == 1 else np.ascontiguousarray(data))
            pspec.SetInfo('interpolation', 'vertex')
            ispec = layer.GetAttributeAtPath(mpath.AppendProperty(name + ':indices'))
            if ispec is not None:
                layer.GetPrimAtPath(mpath).RemoveProperty(ispec)
        ext = layer.GetAttributeAtPath(mpath.AppendProperty('extent'))
        if ext is not None and len(new_pts):
            ext.default = Vt.Vec3fArray([Gf.Vec3f(*new_pts.min(0).tolist()), Gf.Vec3f(*new_pts.max(0).tolist())])
        for (spath, _), (a, b) in zip(tri_groups, ranges):
            if spath is not None:
                layer.GetAttributeAtPath(spath.AppendProperty('indices')).default = Vt.IntArray(list(range(a, b)))
        report['meshes'] += 1
    # usdc 原地保存是追加写，文件不会变小；导出成新文件再替换。
    root, ext = os.path.splitext(path)
    tmp = root + '.__web__' + ext
    layer.Export(tmp)
    os.replace(tmp, path)


def optimize_png(raw, texmax=1024):
    """web 档 PNG：长边缩到 texmax 并无损重编码（去掉全不透明的 alpha、灰度图存单通道、
    保留 gAMA/cHRM/sRGB、EXIF 与 dpi）。动画、16 位/浮点、带 ICC 或 HDR 元数据的图不在这里改，
    原字节返回，由调用方决定是否只做缩放。结果不比原来小时也返回原字节。"""
    import io
    import struct
    from PIL import Image, ImageChops, PngImagePlugin

    with Image.open(io.BytesIO(raw)) as original:
        if original.format != 'PNG':
            raise ValueError('PNG 文件内容与后缀不一致')
        original.load()
        if getattr(original, 'is_animated', False):
            return raw, 'kept_animation'
        if original.mode.startswith('I') or original.mode == 'F':
            return raw, 'kept_high_precision'
        if original.info.get('icc_profile'):
            return raw, 'kept_icc_profile'
        pnginfo = PngImagePlugin.PngInfo()
        offset = 8
        while offset < len(raw):
            length = struct.unpack_from('>I', raw, offset)[0]
            tag = raw[offset + 4:offset + 8]
            if tag in {b'cICP', b'cLLI', b'mDCV'}:
                return raw, 'kept_hdr_metadata'
            if tag in {b'gAMA', b'cHRM', b'sRGB'}:
                pnginfo.add(tag, raw[offset + 8:offset + 8 + length])
            offset += 12 + length
        image = original.convert('RGBA')
        if image.getchannel('A').getextrema() == (255, 255):
            image = image.convert('RGB')
        scale = min(1.0, texmax / max(image.size))
        if scale < 1:
            size = tuple(max(1, int(value * scale + 0.5)) for value in image.size)
            image = image.resize(size, Image.Resampling.LANCZOS)
        r, g, b = image.convert('RGB').split()
        if ImageChops.difference(r, g).getbbox() is None and ImageChops.difference(g, b).getbbox() is None:
            image = r if image.mode == 'RGB' else Image.merge('LA', (r, image.getchannel('A')))
        options = {key: original.info[key] for key in ('exif', 'dpi') if key in original.info}
        encoded = io.BytesIO()
        image.save(encoded, format='PNG', optimize=True, pnginfo=pnginfo, **options)
        result = encoded.getvalue()
    return (result, 'optimized') if len(result) < len(raw) else (raw, 'kept_not_smaller')


def _resize_only(p, texmax):
    from PIL import Image
    with Image.open(p) as im:
        w, h = im.size
        if max(w, h) <= texmax:
            return False
        scale = texmax / max(w, h)
        small = im.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)
        fmt = im.format
        small.save(p + '.tmp', format=fmt, **({'optimize': True} if fmt == 'PNG' else {'quality': 92}))
    os.replace(p + '.tmp', p)
    return True


def _png_edge(raw):
    import io
    from PIL import Image
    with Image.open(io.BytesIO(raw)) as im:
        return max(im.size)


def resize_textures(root, texmax, report):
    """PNG 一律走 optimize_png（缩放 + 无损重编码）；它不处理的特殊 PNG 与 JPEG 只按长边缩放。"""
    report.setdefault('png_actions', {})
    for dirpath, _, files in os.walk(root):
        for name in sorted(files):
            if not name.lower().endswith(('.png', '.jpg', '.jpeg')):
                continue
            p = os.path.join(dirpath, name)
            before = os.path.getsize(p)
            resized = False
            if name.lower().endswith('.png'):
                with open(p, 'rb') as f:
                    raw = f.read()
                output, action = optimize_png(raw, texmax)
                report['png_actions'][action] = report['png_actions'].get(action, 0) + 1
                if output is raw:
                    # 特殊 PNG 或重编码不划算：只按长边缩放（不超过 texmax 时不动）。
                    resized = _resize_only(p, texmax)
                else:
                    with open(p + '.tmp', 'wb') as f:
                        f.write(output)
                    os.replace(p + '.tmp', p)
                    resized = _png_edge(raw) > texmax
            else:
                resized = _resize_only(p, texmax)
            report['tex_resized'] += int(resized)
            report['tex_bytes_saved'] += before - os.path.getsize(p)


PROFILE_SKIP_DIRS = {'profiles', 'behavior', 'evidence'}
PROFILE_KEEP = ('.usd', '.usda', '.usdc', '.png', '.jpg', '.jpeg', '.exr', '.hdr', '.tga', '.bmp', '.tif', '.tiff', '.ktx2')


def copy_profile_tree(simready, out):
    """把 simready_usd 下的 USD 层和贴图按原相对路径复制到 out（不带 profiles/behavior/证据和元数据）。"""
    for dirpath, dirnames, files in os.walk(simready):
        rel = os.path.relpath(dirpath, simready)
        if rel == '.':
            dirnames[:] = [d for d in dirnames if d not in PROFILE_SKIP_DIRS]
        for name in files:
            if name.lower().endswith(PROFILE_KEEP):
                target = os.path.join(out, rel, name)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copy2(os.path.join(dirpath, name), target)


def build_profile(simready, out, rel=1e-3, texmax=1024, min_triangles=200, meshopt_from=None):
    """生成 web 轻量 profile：网格减面、贴图缩小，prim 层级/名字/关节/变换/材质绑定不变。"""
    class A: pass
    args = A(); args.rel = rel; args.texmax = texmax; args.min_triangles = min_triangles
    args.meshopt_from = meshopt_from or os.environ.get('MESHOPT_FROM') or DEFAULT_MESHOPT
    if os.path.exists(out):
        shutil.rmtree(out)
    copy_profile_tree(simready, out)
    report = {'meshes': 0, 'skipped': 0, 'tris_before': 0, 'tris_after': 0, 'tex_resized': 0, 'tex_bytes_saved': 0}
    for dirpath, _, files in os.walk(out):
        for name in sorted(files):
            if name.lower().endswith(('.usdc', '.usda', '.usd')):
                process_layer(os.path.join(dirpath, name), args, report)
    resize_textures(out, texmax, report)
    return report


DEFAULT_MESHOPT = None  # 由 --meshopt-from 或环境变量 MESHOPT_FROM 给出（例如前端仓库 node_modules/.pnpm/meshoptimizer@1.1.1/node_modules）


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('src')
    ap.add_argument('dst')
    ap.add_argument('--rel', type=float, default=1e-3, help='相对误差（相对网格尺寸）')
    ap.add_argument('--texmax', type=int, default=1024)
    ap.add_argument('--min-triangles', type=int, default=200)
    ap.add_argument('--meshopt-from', default=os.environ.get('MESHOPT_FROM') or DEFAULT_MESHOPT)
    args = ap.parse_args()
    if os.path.exists(args.dst):
        shutil.rmtree(args.dst)
    shutil.copytree(args.src, args.dst, symlinks=False)
    report = {'meshes': 0, 'skipped': 0, 'tris_before': 0, 'tris_after': 0, 'tex_resized': 0, 'tex_bytes_saved': 0}
    for dirpath, _, files in os.walk(args.dst):
        for name in sorted(files):
            if name.lower().endswith(('.usdc', '.usda', '.usd')):
                process_layer(os.path.join(dirpath, name), args, report)
    resize_textures(args.dst, args.texmax, report)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
