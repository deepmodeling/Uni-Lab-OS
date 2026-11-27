"""Version 3 acceptance by use. Version 2 remains unchanged in core.py."""
from pathlib import Path
import uuid,re
from core import read,digest,inside,canonical

SCHEMA='unilab.engineering-asset/3'
KINDS={'component','workpiece_consumable','mechanism','equipment','station_template','engineering_knowledge'}
PROFILES={'cad':({'component','workpiece_consumable','mechanism'},('cad',),('native_reopen',)),
          'spatial':({'component','workpiece_consumable','mechanism','equipment'},('cad','usd'),('native_reopen','usd_reopen')),
          'station_layout':({'station_template'},('os_graph','os_lock','usd'),('os_scene','usd_reopen')),
          'knowledge':({'engineering_knowledge'},('document',),('document_integrity',))}
PROFILES['imported_candidate']=({'component','workpiece_consumable','mechanism','equipment'},('usd',),('source_integrity','usd_reopen'))

def check_runtime_evidence(root,entry,capability,claim):
    """Bind qualified capabilities to the exact asset and the claimed evaluator."""
    report=read(inside(root,claim['evidence']))
    field=claim.get('input_hash_field','input_sha256')
    if field not in {'source_usd_sha256','input_sha256','usd_sha256'}:
        raise ValueError('Unsupported acceptance hash field')
    if report.get('status')!='passed' or report.get(field)!=digest(inside(root,entry)):
        raise ValueError('Acceptance does not qualify these exact USD bytes: '+capability)
    if not isinstance(claim.get('scope'),str) or not claim['scope'].strip():
        raise ValueError('Acceptance requires an explicit task scope')
    if capability=='nvidia_simready':
        common={'FET_001_STANDARD','FET_003_STANDARD','FET_006_STANDARD','FET_031_STANDARD','FET_033_STANDARD'}
        profiles={('Robot-Body','3.0.0'):common,('Robot-Gripper','3.0.0'):common,
                  ('Robotics-Prop','4.0.0'):common|{'FET_000_STANDARD','FET_011_STANDARD','FET_011_RTX','FET_046_STANDARD'}}
        required=profiles.get((report.get('profile'),report.get('profile_version')))
        if (required is None
            or report.get('official_profile_passed') is not True or report.get('direct_required_full_passed') is not True
            or not required<=set(report.get('required_features',[])) or not report.get('validator_version')
            or report.get('exit_code')!=0 or report.get('validation_contract_errors') or report.get('blocking_features')):
            raise ValueError('Receipt does not prove the selected NVIDIA profile')
    elif capability=='native_physx':
        checks=report.get('checks',{})
        needed={'six_axis_driven_movement','gravity_counterfactual','native_contact_counterfactual','source_layers_unchanged'}
        if report.get('engine')!='Isaac Sim native PhysX CPU' or not needed<=set(checks) or not all(v is True for v in checks.values()) or report.get('steps_total',0)<=0:
            raise ValueError('Receipt does not prove native PhysX runtime')
    elif capability=='physics':
        if (report.get('engine')!='PyBullet DIRECT CPU' or report.get('steps_total',0)<=0
            or report.get('trajectory',{}).get('status')!='passed'
            or any(report.get(k)!='passed' for k in ('gravity_control','joint_limits','contact'))
            or report.get('no_state_overwrites_during_rollout') is not True):
            raise ValueError('Receipt does not prove the selected dynamics suite')
    else:raise ValueError('Unsupported acceptance capability '+capability)
    return report

def check_evidence(root,p,key):
    c=p['capabilities'][key]
    if c['status']!='passed':raise ValueError('未通过用途验收 '+key)
    report=read(inside(root,c['evidence']))
    if report['status']!='passed':raise ValueError('验收回执失败 '+key)
    # Explicit binding prevents unrelated successful receipts from being reused.
    inputs=report.get('input_files',{})
    if not inputs:raise ValueError('验收缺少固定输入 '+key)
    for path,h in inputs.items():
        if digest(inside(root,path))!=h:raise ValueError('验收输入已变化 '+path)
    required={'native_reopen':('cad',),'usd_reopen':('usd',),'os_scene':('os_graph','os_lock','usd'),'document_integrity':('document',),'source_integrity':()}[key]
    if any(p['entrypoints'][name] not in inputs for name in required):raise ValueError('验收未覆盖用途入口 '+key)
    if key=='source_integrity' and p['source_manifest'] not in inputs:raise ValueError('来源验收未绑定来源清单')
    return report

def validate_profile(root,p):
    if p['schema']!=SCHEMA:raise ValueError('未知工程包格式')
    if str(uuid.UUID(p['asset_id']))!=p['asset_id']:raise ValueError('非法资产身份')
    if p.get('profile') not in PROFILES:raise ValueError('未知用途 profile')
    kinds,entries,checks=PROFILES[p['profile']]
    if p.get('reuse_kind') not in kinds:raise ValueError('复用类别与用途不符')
    if not set(entries)<=set(p['entrypoints']):raise ValueError('缺少用途入口')
    extensions={'cad':{'.sldprt','.sldasm'},'usd':{'.usd','.usda','.usdc'},'os_graph':{'.json'},'os_lock':{'.json'},'document':{'.md','.pdf','.json','.csv'}}
    if p['profile']=='imported_candidate':extensions['cad']|={'.step','.stp','.iges','.igs','.brep'}
    representations={r['path']:r for r in p['representations']}
    for name,path in p['entrypoints'].items():
        target=inside(root,path)
        if not target.is_file() or (name in extensions and target.suffix.lower() not in extensions[name]):raise ValueError('入口格式/文件错误 '+name)
        if path not in representations or representations[path]['sha256']!=digest(target):raise ValueError('入口缺少固定表示摘要 '+name)
    for r in list(p['representations'])+list(p.get('exports',[])):
        if digest(inside(root,r['path']))!=r['sha256']:raise ValueError('表示/可选导出摘要错误')
    for key in checks:check_evidence(root,p,key)
    for key in ('physics','native_physx','nvidia_simready'):
        claim=p.get('capabilities',{}).get(key,{})
        if claim.get('status')=='passed':check_runtime_evidence(root,p['entrypoints']['usd'],key,claim)
    if p['profile']=='imported_candidate':
        if p.get('qualification_scope')!='imported_candidate':raise ValueError('导入候选不得冒充完整工程/动力学资产')
        sources=read(inside(root,p['source_manifest']))['files']
        if not sources:raise ValueError('导入候选缺少原始来源')
        for source in sources:
            if not source.get('original_uri') or digest(inside(root,source['path']))!=source['sha256']:raise ValueError('原始来源缺失或变更')
        correspondence=read(inside(root,p['cad_correspondence']))
        if correspondence.get('status') not in ('mapped','root_model_only','absent','partial'):raise ValueError('CAD 对应状态不明确')
        if correspondence['status']=='absent' and 'cad' in p['entrypoints']:raise ValueError('无CAD对应的候选不能声明CAD入口')
        if correspondence['status']!='absent' and 'cad' not in p['entrypoints']:raise ValueError('CAD 对应缺少实际入口')
    if p['profile']=='knowledge':
        if not p.get('source_refs'):raise ValueError('知识资料缺少来源')
        for ref in p['source_refs']:
            if not ref.get('uri') or not re.fullmatch('[a-f0-9]{64}',ref['sha256']):raise ValueError('知识来源未固定')
    if p['profile']=='station_layout':
        graph=read(inside(root,p['entrypoints']['os_graph']));lock=read(inside(root,p['entrypoints']['os_lock']))
        if lock['deployment_graph']['sha256']!=digest(inside(root,p['entrypoints']['os_graph'])):raise ValueError('OS 图与锁不一致')
        declared={(d['asset_id'],d['package_digest'],d['path']) for d in p['dependencies']}
        locked={(d['asset_id'],d['package_digest'],d['path']) for d in lock['dependencies']}
        if len(declared)!=len(p['dependencies']) or declared!=locked:raise ValueError('OS 依赖闭包与锁不符')
        if sorted(p['dependencies'],key=canonical)!=sorted(lock['dependencies'],key=canonical):raise ValueError('OS 依赖配置/节点字段漂移')
        lockdir=inside(root,p['entrypoints']['os_lock']).parent
        graphpath=(lockdir/lock['deployment_graph']['path']).resolve()
        if graphpath!=inside(root,p['entrypoints']['os_graph']):raise ValueError('OS 锁相对路径未指向图入口')
        if lock.get('domain_asset_index'):
            indexpath=(lockdir/lock['domain_asset_index']['path']).resolve()
            if not indexpath.is_relative_to(Path(root).resolve()) or digest(indexpath)!=lock['domain_asset_index']['sha256']:raise ValueError('OS 领域索引不完整/变化')
        expected={d['node_id'] for d in lock['dependencies']}
        if expected!=set(graph['scene']['node_bindings']) or expected!={n['id'] for n in graph['nodes']}:raise ValueError('OS 节点覆盖错误')
        mapping=read(inside(root,p['cad_mapping']))
        if set(mapping['nodes'])!=expected:raise ValueError('OS 物理节点缺 CAD 对应')
        for d in lock['dependencies']:
            b=graph['scene']['node_bindings'][d['node_id']];m=mapping['nodes'][d['node_id']]
            if b['path']!=d['path']+'/'+d['entrypoints']['usd'] or m['package_digest']!=d['package_digest'] or m['cad_path']!=d['entrypoints']['cad']:raise ValueError('OS CAD/USD 绑定漂移')
        if p.get('qualification_scope')!='layout_only':raise ValueError('本 profile 只验证布局，不授予生产工站/完整 CAD 回写资格')
    return {'status':'passed','profile':p['profile'],'reuse_kind':p['reuse_kind'],'qualification_scope':p.get('qualification_scope',p['profile'])}
