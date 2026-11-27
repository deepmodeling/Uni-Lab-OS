"""Run installed NVIDIA NP.005 and independent official hash algorithms.

This intentionally does not claim a complete SimReady profile or dynamics.
"""
import argparse,hashlib,json
from pathlib import Path
import simready.validate as validation
from pxr import Usd

def check(root):
    validation.initialize([],[],[])
    from simready.foundation.tier_core.capabilities.core.naming_paths.validation import AssetFolderStructureChecker
    from simready.foundation.tier_core.capabilities.packaging.packaging_core.validation import _content_hash_buffer_from_bom,_package_hash_buffer
    root=Path(root);index=json.loads((root/'library.json').read_text(encoding='utf-8'));rows=[]
    for row in index['packages']:
        folder=root/row['path'];m=json.loads((folder/'com.nvidia.simready.packaging.json').read_text(encoding='utf-8'))
        b=json.loads((folder/'.metadata/com.nvidia.simready.packaging.bom.json').read_text(encoding='utf-8'))
        p=json.loads((folder/'simready_usd/unilab.json').read_text(encoding='utf-8'))
        assert hashlib.sha256(_content_hash_buffer_from_bom(b)).hexdigest()==m['content_hash']['sha256']
        assert hashlib.sha256(_package_hash_buffer(m)).hexdigest()==m['package_hash']['sha256']
        checker=AssetFolderStructureChecker();checker.CheckStage(Usd.Stage.Open(str(folder/p['entrypoints']['usd'])))
        issues=list(checker.GetIssues())
        if issues:raise ValueError(str(issues))
        rows.append({'asset_id':row['asset_id'],'np005':'passed','official_hash_algorithms':'passed'})
    return {'status':'passed','validator':'simready-foundation-tier-core 2026.8.0','scope':'NP.005 and official content/package hash algorithms only; not complete profile validation','assets':rows}

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--root',required=True);ap.add_argument('--output',required=True);a=ap.parse_args();r=check(a.root)
    Path(a.output).write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({'status':r['status'],'assets':len(r['assets']),'scope':r['scope']}))
