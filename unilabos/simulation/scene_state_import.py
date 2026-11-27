"""Import reference poses by explicit identity mapping; never import their assets."""
import numpy as np


def remap_body_poses(reference_poses, expected_paths, prefix_mapping, *, ignored_reference_paths=()):
    if len(expected_paths)!=len(set(expected_paths)):raise ValueError("Duplicate destination identities")
    source_prefixes=[a for a,_ in prefix_mapping]
    if len(source_prefixes)!=len(set(source_prefixes)):raise ValueError("Ambiguous destination prefix")
    result={};used=set()
    for path in expected_paths:
        matches=[(a,b) for a,b in prefix_mapping if path==a or path.startswith(a+"/")]
        if not matches:raise ValueError("No identity mapping for "+path)
        a,b=max(matches,key=lambda pair:len(pair[0]));old=b+path[len(a):]
        if old in used or old not in reference_poses:raise ValueError("Missing or reused reference identity: "+old)
        matrix=np.asarray(reference_poses[old],dtype=float)
        if (matrix.shape!=(4,4) or not np.isfinite(matrix).all()
            or not np.allclose(matrix[3],[0,0,0,1],atol=1e-9,rtol=0)
            or not np.allclose(matrix[:3,:3].T@matrix[:3,:3],np.eye(3),atol=1e-5,rtol=0)
            or np.linalg.det(matrix[:3,:3])<0):
            raise ValueError("Reference pose must be finite and rigid")
        result[path]=matrix.tolist();used.add(old)
    extras=set(reference_poses)-used
    if extras!=set(ignored_reference_paths):raise ValueError("Unaccounted reference identities")
    return result
