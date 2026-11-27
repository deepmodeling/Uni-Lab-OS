"""Portable content identity for every USD layer used by an OS assembly."""
import hashlib
import json
from pathlib import Path

SCHEMA="unilab.usd-dependencies/v1"

def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",",":")).encode()).hexdigest()

def capture_dependencies(stage,asset_root,*,binding):
    root=Path(asset_root).resolve()
    files={}
    excluded={stage.GetRootLayer().identifier,stage.GetSessionLayer().identifier}
    for layer in stage.GetUsedLayers():
        if layer.identifier in excluded:continue
        if layer.anonymous or not layer.realPath:raise ValueError("Unpersisted assembly dependency")
        path=Path(layer.realPath).resolve()
        if not path.is_relative_to(root):raise ValueError("USD dependency outside declared asset root: "+str(path))
        relative=path.relative_to(root).as_posix()
        files[relative]=hashlib.sha256(path.read_bytes()).hexdigest()
    if not files:raise ValueError("Assembly has no source dependencies")
    value=dict(schema=SCHEMA,binding=binding,files=dict(sorted(files.items())))
    return dict(value,digest=_digest(value))

def verify_dependencies(value,asset_root,*,binding):
    value=dict(value);claimed=value.pop("digest",None)
    if value.get("schema")!=SCHEMA or _digest(value)!=claimed:
        raise ValueError("Dependency manifest schema/digest differs")
    if value.get("binding")!=binding:raise ValueError("Dependency manifest belongs to another assembly")
    root=Path(asset_root).resolve()
    if not value.get("files"):raise ValueError("Empty dependency manifest")
    for relative,expected in value["files"].items():
        path=(root/relative).resolve()
        if Path(relative).is_absolute() or not path.is_relative_to(root):
            raise ValueError("Dependency path must be relative to the declared root")
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
            raise ValueError("Assembly dependency changed: "+relative)
    return dict(verified=True,files=len(value["files"]),manifest_digest=claimed,binding=binding)
