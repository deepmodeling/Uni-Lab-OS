"""Derived discovery fields; preserve public records and original facet evidence."""
from pathlib import Path
from urllib.parse import urlsplit

STAGES=['备料&前处理','实验执行&合成','后处理','器件/细胞','表征','物流/机械','待归类']
STAGE_ALIASES={name+'设备':name for name in STAGES[:5]}
GEOMETRY_EXTENSIONS={'.usd','.usda','.usdc','.usdz','.glb','.gltf','.obj','.stl',
    '.ply','.dae','.fbx','.blend','.step','.stp','.iges','.igs','.sldprt','.sldasm',
    '.urdf','.xacro','.3mf','.brep'}


def canonical_stage(name):
    return STAGE_ALIASES.get(name,name)


def stage_facets(facets):
    """Resolve explicit aliases, without deleting or editing their source records."""
    steps={key:v for key,v in facets.items() if v['type']=='experimental_step'}
    byname={v['name']:key for key,v in steps.items()}
    aliases={key:byname.get(canonical_stage(v['name']),key) for key,v in steps.items()}
    return {key:target for key,target in aliases.items() if key!=target}


def model_presence(model, library_root):
    """Recognized geometry file or explicit remote geometry reference, not a SKU.

    Local files must exist within the library/package. External references are
    registered, not downloaded or certified by this inexpensive index operation.
    """
    if not isinstance(model,dict):return False,'none'
    remote=False
    root=Path(library_root).resolve()

    def inspect(value,base):
        if not isinstance(value,str) or not value:return None
        uri=urlsplit(value)
        if Path(uri.path).suffix.lower() not in GEOMETRY_EXTENSIONS:return None
        if uri.scheme in ('http','https','oss') and uri.netloc:return 'external_reference'
        if uri.scheme:return None
        path=(base/value).resolve()
        if path.is_relative_to(base) and path.is_file():return 'local_file'
        return None

    for target in model.get('targets',[]):
        if not isinstance(target,dict) or not target.get('package_path'):continue
        base=(root/target['package_path']).resolve()
        if not base.is_relative_to(root):continue
        for ep in target.get('entrypoints',{}).values():
            result=inspect(ep.get('path') if isinstance(ep,dict) else ep,base)
            if result=='local_file':return True,result
            remote=remote or result=='external_reference'
    for key in ('path','mesh','children_mesh_path','children_mesh'):
        result=inspect(model.get(key),root)
        if result=='local_file':return True,result
        remote=remote or result=='external_reference'
    return (True,'external_reference') if remote else (False,'none')
