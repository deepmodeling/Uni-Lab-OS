"""Bind explicit world-space OS animation caches in Blender.

Blender's USD importer inconsistently preserves parents for reset Xform stacks:
rigid bodies are detached, while material/robot Xforms may retain parents.
Only targets explicitly declared by the OS world-space overlay are detached.
Meshes and their local transforms are unchanged.
"""


def bind_world_motion_caches(objects, world_paths):
    expected=set(world_paths)
    if not expected:raise ValueError("Expected animation paths required")
    found={}
    for obj in objects:
        for constraint in obj.constraints:
            if constraint.type=="TRANSFORM_CACHE" and constraint.object_path in expected:
                if constraint.object_path in found:raise ValueError("Duplicate world animation cache")
                found[constraint.object_path]=obj
    if set(found)!=expected:
        raise ValueError("Missing world animation caches: "+str(sorted(expected-set(found))))
    changed=[]
    for path,obj in found.items():
        if obj.parent is not None:
            changed.append(dict(path=path,object=obj.name,previous_parent=obj.parent.name))
        obj.parent=None
        obj.matrix_parent_inverse.identity()
    return dict(space="world",targets=len(found),detached_parents=changed,
                geometry_modified=False,physics=False)
