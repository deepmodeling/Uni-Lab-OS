"""Exact-default, evidence-backed readiness view. No simulation or blanket certification.

Only explicit report adapters assign passed. Physical values alone never do.
Library.resolve_evidence verifies direct files or hash-pinned ZIP members.
"""
from __future__ import annotations
import ast,hashlib,json,posixpath,gzip,io
from urllib.parse import urlencode

STATES={'passed','failed','not_verified','missing_data','not_applicable'}
GROUPS=[('internal','内部检查'),('physical_data','物性与参数资料'),('nvidia_static','NVIDIA 官方静态规则'),('dynamics','实际动力学')]
PHYSICAL=[('mass','质量','厂家资料 / CAD 材料密度 / 实测'),('center_of_mass','质心','厂家资料 / CAD 质量属性 / 实测'),
 ('inertia','惯量张量','厂家资料 / 几何积分 / 实测'),('collision','碰撞表示','转换派生几何及独立检查'),
 ('friction_restitution','摩擦与恢复系数','厂家材料资料 / 接触实测'),('joints','关节、限位与坐标','原生文件 / 厂家模型'),
 ('effort_velocity','力矩、速度与加速度','厂家规格 / 实测'),('drive','驱动与阻尼参数','工程测试配置 / 厂家控制资料'),
 ('behavior','行为模型','源行为脚本 / OS graph / 实际运行'),('hardware_calibration','实物标定','实物测量及可追溯记录')]

def canonical(x):return json.dumps(x,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
def safe_join(base,path):
    if not isinstance(path,str) or not path or '\\' in path or path.startswith('/') or ':' in path or '..' in path.split('/'):raise ValueError('Invalid relative evidence path')
    return posixpath.join(base,path) if base else path
def status(value):return value if value in STATES else 'not_verified'
def literals(value):
    if isinstance(value,list):return value
    if not isinstance(value,str) or len(value)>65536:return []
    try:result=ast.literal_eval(value)
    except (ValueError,SyntaxError,RecursionError):return []
    return result if isinstance(result,list) else []

class Builder:
    def __init__(self,library,catalog_id):
        self.lib=library;self.cid=catalog_id;self.record=library.records.get(catalog_id) or library.source_records.get(catalog_id)
        if self.record is None:raise KeyError('Asset not found')
        self.target=(self.record.get('targets') or [None])[0];self.package_path=self.target.get('package_path','') if self.target else ''
        ep=(self.target or {}).get('entrypoints',{}).get('spatial',{})
        self.binding={'catalog_id':catalog_id,'default_target':True,'package_digest':(self.target or {}).get('package_digest'),'source_usd_sha256':ep.get('sha256'),'spatial_path':ep.get('path')}
        self.rows={k:[] for k,_ in GROUPS};self.evidence={};self.loaded={};self.archives={};self.pkg={};self.sealed=None
    def ref(self,path,expected=None,base=None):
        path=safe_join(self.package_path if base is None else base,path)
        if path in self.archives:
            ref=dict(self.archives[path])
            if expected and ref['member_sha256']!=expected:raise ValueError('Archive evidence hash mismatch')
            return ref
        item=getattr(self.lib,'files',{}).get(path)
        if not item:raise FileNotFoundError('Evidence not registered: '+path)
        if expected and item['sha256']!=expected:raise ValueError('Evidence reference hash mismatch: '+path)
        if self.sealed is not None and path.startswith(self.package_path+'/'):
            relative=path[len(self.package_path)+1:]
            if relative!='bundle.sha256.json' and self.sealed.get(relative)!=item['sha256']:raise ValueError('Evidence not bound to current package seal: '+path)
        return {'path':path,'sha256':item['sha256']}
    def register(self,ref,fragment=None):
        eid=hashlib.sha256(canonical(ref)).hexdigest()[:24]
        if eid not in self.evidence:self.evidence[eid]={'id':eid,'locator':ref,'href':'/api/readiness/evidence?'+urlencode({'id':self.cid,'key':eid})}
        return dict(self.evidence[eid],fragment=fragment) if fragment else self.evidence[eid]
    def load(self,ref):
        eid=self.register(ref)['id']
        if eid not in self.loaded:
            resolved=self.lib.resolve_evidence(ref)
            if resolved.get('verified') is not True:raise ValueError('Unverified evidence resolver result')
            data=resolved['data']
            if not isinstance(data,bytes):raise ValueError('Evidence resolver must return verified bytes')
            if ref.get('path',ref.get('member','')).endswith('.gz'):
                with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:data=stream.read(16*1024*1024+1)
                if len(data)>16*1024*1024:raise ValueError('Compressed evidence exceeds structured detail limit')
            self.loaded[eid]=json.loads(data.decode('utf-8-sig'))
        return self.loaded[eid]
    def row(self,group,id,label,state='not_verified',issue='',source_type='unknown',refs=(),**fields):
        item={'id':id,'label':label,'status':status(state),'issue':issue,'source_type':source_type,'binding':dict(self.binding),'evidence':list(refs),**fields}
        self.rows[group].append(item);return item
    def binding_problem(self,data,cap=None,require_usd=True):
        if not isinstance(data,dict):return '报告结构不受支持'
        package=data.get('package_digest')
        if package and package!=self.binding['package_digest']:return '证据属于旧封包，不能用于当前默认版本'
        hashes=[data[k] for k in ('usd_sha256','source_usd_sha256','input_sha256') if data.get(k)]
        declared=(cap or {}).get('input_sha256')
        if declared:hashes.append(declared)
        if not hashes and isinstance(data.get('input_files'),dict):
            entry=self.binding['spatial_path']
            if entry in data['input_files']:hashes.append(data['input_files'][entry])
        if hashes and any(h!=self.binding['source_usd_sha256'] for h in hashes):return '证据输入 USD 已过期，不适用于当前默认封包'
        if require_usd and not hashes:return '报告未绑定精确 USD 哈希，不能授予当前版本通过状态'
        return None
    def evidence_for_cap(self,name):
        cap=self.pkg.get('capabilities',{}).get(name,{})
        if not cap.get('evidence'):return cap,None,None,'未找到当前封包的验收回执'
        try:
            ref=self.ref(cap['evidence']);data=self.load(ref);return cap,ref,data,None
        except (ValueError,KeyError,OSError,TypeError) as exc:return cap,None,None,str(exc)
    def source_report(self,capname,label,require_usd=True):
        cap,ref,data,error=self.evidence_for_cap(capname)
        if error:return self.row('internal',capname,label,issue=error)
        issue=self.binding_problem(data,cap,require_usd)
        if not require_usd and not data.get('input_files'):issue=issue or '原件检查未列出精确输入摘要'
        if not require_usd and isinstance(data.get('input_files'),dict):
            for path,h in data['input_files'].items():
                try:self.ref(path,h)
                except (ValueError,OSError) as exc:issue=str(exc)
        state='not_verified' if issue else status(data.get('status'))
        return self.row('internal',capname,label,state,issue or data.get('scope',''), 'internal_report',[self.register(ref)],stale_evidence=bool(issue),report_scope=data.get('scope'))
    def archive_index(self):
        try:ref=self.ref('provenance/archive-index.json')
        except FileNotFoundError:return
        data=self.load(ref)
        # Explicit index contract, never infer members by basename/searching ZIPs.
        if data.get('final_usd_sha256') and data['final_usd_sha256']!=self.binding['source_usd_sha256']:raise ValueError('Archive index USD mismatch')
        for item in data.get('members',data.get('files',data.get('entries',[]))):
            locator=item.get('ref',item.get('locator',item.get('reference',item)))
            if all(k in locator for k in ('archive_path','archive_sha256','member','member_sha256')):
                original=item.get('original_path',item.get('path'))
                if original:self.archives[safe_join(self.package_path,original)]={k:locator[k] for k in ('archive_path','archive_sha256','member','member_sha256','bytes') if k in locator}
    def nvidia(self):
        cap,ref,summary,error=self.evidence_for_cap('nvidia_simready')
        if error:
            legacy=self.record.get('qualification',{}).get('nvidia_simready',{})
            if legacy.get('evidence'):
                try:ref=self.ref(legacy['evidence'],base='');summary=self.load(ref);error=None
                except (ValueError,KeyError,OSError) as exc:error=str(exc)
        if error:self.row('nvidia_static','profile','官方 profile 验收',issue=error,source_type='official_report_required');return
        refs=[self.register(ref)];issue=self.binding_problem(summary,cap)
        identity={k:summary.get(k) for k in ('profile','profile_version','validator_version','profile_definition_sha256')}
        rawref=None
        try:
            if summary.get('raw_report'):rawref=self.ref(summary['raw_report'],summary.get('raw_report_sha256'),base='')
            else:
                rawpath=posixpath.join(posixpath.dirname(cap.get('evidence','attachments/nvidia/summary.json')),'nvidia-validation.json');rawref=self.ref(rawpath,summary.get('raw_report_sha256'))
            raw=self.load(rawref);refs.append(self.register(rawref));details=next(iter(raw.values())) if isinstance(raw,dict) and len(raw)==1 else None
            if not isinstance(details,dict):raise ValueError('官方原始报告必须精确对应一个输入')
            if any(details.get(a)!=identity[b] for a,b in [('profile_id','profile'),('profile_version','profile_version'),('simready_validate_version','validator_version')]):raise ValueError('官方 profile / version / validator 身份不一致')
            if not all(identity[k] for k in ('profile','profile_version','validator_version')):raise ValueError('官方版本身份缺失')
            features=details.get('features_summary');required=summary.get('required_features')
            if not isinstance(features,dict) or not features or not isinstance(required,list) or not required:raise ValueError('官方规则覆盖清单缺失')
            if any(k not in features for k in required):raise ValueError('官方原始报告缺少必选 feature')
            contradictions=[k for k,v in features.items() if not isinstance(v,dict) or not isinstance(v.get('passed'),bool) or (v['passed'] and (v.get('blocking_failure') or literals(v.get('failing requirements'))))]
            if contradictions:raise ValueError('官方报告存在矛盾状态: '+', '.join(contradictions))
        except (ValueError,KeyError,OSError,TypeError,StopIteration) as exc:
            self.row('nvidia_static','profile','官方 profile 验收','not_verified',issue or str(exc),'official_validator',refs,**identity,stale_evidence=bool(issue));return
        profile_ok=summary.get('status')=='passed' and summary.get('official_profile_passed') is True and summary.get('exit_code')==0 and not summary.get('validation_contract_errors') and not any(v.get('blocking_failure') for v in features.values())
        if profile_ok and any(features[k]['passed'] is not True for k in required):profile_ok=False
        self.row('nvidia_static','profile','官方 profile 验收','not_verified' if issue else 'passed' if profile_ok else 'failed',issue or summary.get('scope','仅静态规则；不代表动力学或硬件验收'),'official_validator',refs,**identity,stale_evidence=bool(issue))
        for feature,value in features.items():
            loc=[self.register(rawref,'features_summary/'+feature)]
            self.row('nvidia_static','feature:'+feature,feature,'not_verified' if issue else 'passed' if value['passed'] else 'failed',issue or ('必选 feature' if feature in required else '可选 feature；未满足不等于 profile 阻断'),'official_validator',loc,**identity,feature_version=value.get('version'),required=feature in required,blocking=value.get('blocking_failure'),stale_evidence=bool(issue))
            # Original output reports unmet rule IDs, not per-rule passed results.
            for field in ('failing requirements','optional requirements'):
                for rule in literals(value.get(field)):
                    self.row('nvidia_static','rule:'+feature+':'+str(rule),str(rule),'not_verified' if issue else 'failed',issue or ('官方报告列为未满足要求；'+('非阻断可选项' if field=='optional requirements' else '失败要求')),'official_validator',loc,**identity,feature=feature,required=field=='failing requirements',rule_id=str(rule),stale_evidence=bool(issue))
    def dynamics(self):
        for name,label in [('physics','动力学引擎验收'),('native_physx','原生 PhysX 验收')]:
            cap,ref,data,error=self.evidence_for_cap(name)
            if error:self.row('dynamics',name,label,issue=error,source_type='runtime_report_required');continue
            issue=self.binding_problem(data,cap)
            if not data.get('engine') or not data.get('engine_version'):issue=issue or '缺少实际引擎及版本身份'
            if name=='native_physx' and not ('native' in str(data.get('engine','')).lower() and 'physx' in str(data.get('engine','')).lower()):issue=issue or '不是可识别的原生 PhysX 运行回执'
            checks=data.get('checks',{})
            if not checks:
                checks={k:v for k,v in data.items() if k in ('gravity_control','joint_limits','contact','no_state_overwrites_during_rollout')}
                if isinstance(data.get('trajectory'),dict):checks['trajectory']=data['trajectory'].get('status')
            if not checks:issue=issue or '缺少实际运行检查项'
            scope=data.get('scope',data.get('qualification',cap.get('scope','')))
            if not scope:issue=issue or '缺少动态验收适用范围'
            refs=[self.register(ref)];state='not_verified' if issue else status(data.get('status'))
            if state=='passed' and any(v is False or v=='failed' for v in checks.values()):state='failed'
            extra={'engine':data.get('engine'),'engine_version':data.get('engine_version'),'report_scope':scope,'stale_evidence':bool(issue)}
            self.row('dynamics',name,label,state,issue or scope,'actual_engine_run',refs,**extra)
            for key,value in checks.items():
                state='passed' if value is True or value=='passed' else 'failed' if value is False or value=='failed' else 'not_verified'
                self.row('dynamics',name+':'+key,key,'not_verified' if issue else state,issue or scope,'actual_engine_run',[self.register(ref,'checks/'+key)],value=value,**extra)
    def physical(self):
        rows={key:self.row('physical_data',key,label,'missing_data','尚无可追溯的当前输入资料；提取到值不等于验收通过',source) for key,label,source in PHYSICAL}
        try:
            ref=self.ref('provenance/stored-content.json');content=self.load(ref)
        except (ValueError,OSError,KeyError):content=None
        if content and content.get('schema')=='unilab.stored-asset-content/1' and content.get('source_usd_sha256')==self.binding['source_usd_sha256']:
            counts=content['usd_authored'];names=set(content['authored_field_names'])
            declarations={
                'mass':(counts['mass_api'], 'unilab:sourceSI:cachedMassKg' in names),
                'center_of_mass':(counts['mass_api'], 'unilab:sourceSI:cachedCenterOfMassMatrixM' in names),
                'inertia':(counts['mass_api'], 'unilab:sourceSI:cachedInertiaXYZKgM2' in names),
                'collision':(counts['collision_api'],False),
                'friction_restitution':(counts['physics_materials'],False),
                'joints':(counts['joints'],False),
                'drive':(int(any(n.startswith('drive:') for n in names)),any('Damping' in n for n in names))}
            for key,(standard,custom) in declarations.items():
                if standard or custom:
                    rows[key].update(status='not_verified',source_type='authored_usd_declaration',
                        issue='已存储；声明、启用与实际运行资格分别判断，源缓存不等于实测值',
                        value={'standard_declarations':standard,'source_custom_fields':bool(custom),'enabled_joints':counts['enabled_joints'] if key=='joints' else None,'physics_material_bindings':counts['physical_material_bindings'] if key=='friction_restitution' else None},
                        evidence=[self.register(ref)],source_present=True,extracted=True,usd_authored=True,runtime_verified=False)
            for semantic in content.get('semantic_files',[]):
                path=semantic['path']
                if path.endswith('.py.txt') or '/scripts/' in path:
                    rows['behavior'].update(status='not_verified',source_type='source_script',issue='源脚本已保存；未执行或迁移为 OS 行为模型',source_present=True,extracted=True,runtime_verified=False,evidence=[self.register(ref)])
                if path.endswith('mass-properties.json'):
                    try:
                        massref=self.ref(path,semantic['sha256']);data=self.load(massref)
                        if data.get('schema')!='unilab.vcmx-offline-mass/1':continue
                        for key,field in [('mass','mass_kg'),('center_of_mass','com_matrix_m'),('inertia','inertia_xyz_kg_m2')]:
                            values=[{'node':x.get('node_id'),'value':x.get('normalized_cache',{}).get(field)} for x in data.get('entities',[]) if x.get('normalized_cache',{}).get(field) is not None]
                            if values:rows[key].update(status='not_verified',source_type='serialized_source_cache',value=values,issue='源文件质量缓存，已换算 SI；不是实测，也未证明与完整原生碰撞体重算等价',evidence=[self.register(massref)])
                    except (ValueError,OSError,KeyError,TypeError):pass
        # Only recognize the actual vendor-physics schema; unrelated JSON keys do
        # not imply physical data. Intermediate model hashes are not current passes.
        extras=self.pkg.get('import_metadata',{}).get('extras',[])
        for extra in extras:
            path=extra.get('path',extra.get('original_package_path',''))
            if not path.endswith('physics-map.json'):continue
            try:ref=self.ref(path,extra.get('sha256'));data=self.load(ref)
            except (ValueError,OSError,KeyError):continue
            if data.get('schema')!='unilab.vendor-physics-map/1':continue
            issue='厂家标称数据已提取；未据此授予物性完整性或实物验收通过'
            if data.get('output_usd_sha256')!=self.binding['source_usd_sha256']:issue+='；映射报告对应中间 USD，需当前版本传递证据'
            bodies=data.get('bodies',[]);joints=data.get('joints',[])
            values={'mass':[{k:b.get(k) for k in ('usd_body','mass_kg','provenance')} for b in bodies if b.get('mass_kg') is not None],
                'center_of_mass':[{k:b.get(k) for k in ('usd_body','com_body_m','provenance')} for b in bodies if b.get('com_body_m') is not None],
                'inertia':[{k:b.get(k) for k in ('usd_body','inertia_com_body_kg_m2','provenance')} for b in bodies if b.get('inertia_com_body_kg_m2') is not None],
                'effort_velocity':joints,'joints':{'joints':joints,'matching':data.get('matching',{}).get('scope')} if joints else None}
            for key,value in values.items():
                if value:rows[key].update(status='not_verified',issue=issue,source_type='manufacturer_nominal_model',value=value,units=data.get('units'),evidence=[self.register(ref,key)],declared_output_usd_sha256=data.get('output_usd_sha256'))
            break
    def blender(self):
        if self.pkg.get('capabilities',{}).get('blender_qa'):return self.source_report('blender_qa','Blender 导入、编辑与重开')
        for extra in self.pkg.get('import_metadata',{}).get('extras',[]):
            if extra.get('role') not in ('exact_usd_blender_qa','blender_qa'):continue
            ref=self.ref(extra.get('path',extra.get('original_package_path')),extra.get('sha256'));data=self.load(ref)
            issue=self.binding_problem(data)
            self.row('internal','blender_qa','Blender 导入、编辑与重开','not_verified' if issue else status(data.get('status')),issue or '实际 Blender 消费回执；不代表关节动力学','blender_consumer_report',[self.register(ref)],stale_evidence=bool(issue));return
        self.row('internal','blender_qa','Blender 导入、编辑与重开',issue='未找到当前封包的显式 Blender 验收回执')
    def build(self):
        if not self.target:
            for group,_ in GROUPS:self.row(group,'source_only','仅保留原件','missing_data','无当前可插入 USD / 封包，不能授予空间、物理或官方规则通过状态','original_source')
        else:
            try:
                seal=self.load(self.ref('bundle.sha256.json',self.binding['package_digest']))
                self.sealed={x['path']:x['sha256'] for x in seal['files']}
                ref=self.ref('package.json');self.pkg=self.load(ref);self.archive_index()
                if self.pkg.get('asset_id')!=self.target['asset_id']:raise ValueError('Default package identity mismatch')
                if self.binding['spatial_path']:self.ref(self.binding['spatial_path'],self.binding['source_usd_sha256'])
                self.source_report('source_integrity','原始资料字节完整性',False)
                self.source_report('usd_reopen','USD 结构、单位、拓扑与依赖')
                # Blender may be stored as a capability or an explicit exact QA attachment.
                self.blender()
                self.source_report('cad_roundtrip','CAD 修改回写与重开')
                if self.record.get('reuse_kind')=='engineering_knowledge' and not self.binding['spatial_path']:
                    for group in ('physical_data','nvidia_static','dynamics'):self.row(group,'document','非空间资料','not_applicable','此默认包仅提供工程文档，没有物理模型','document_package')
                else:self.physical();self.nvidia();self.dynamics()
            except (ValueError,OSError,KeyError,TypeError) as exc:
                self.row('internal','evidence_access','当前封包证据读取','not_verified',str(exc),'evidence_resolution')
                for group,_ in GROUPS:
                    if not self.rows[group]:self.row(group,'unavailable','证据暂不可用',issue='当前输入证据无法验证；未授予通过状态')
        return {'schema':'unilab.asset-readiness/1','catalog_id':self.cid,'title':self.record['title'],'binding':self.binding,
            'groups':[{'id':k,'label':label,'items':self.rows[k]} for k,label in GROUPS],'evidence':list(self.evidence.values()),
            'scope':'逐项证据状态，不以一个总绿灯代表完整 Sim-Ready。提取值、静态官方规则、实际动力学与实物标定分别显示。'}

def build(library,catalog_id):
    if getattr(library,'minimal',False):
        from minimal_format import readiness
        return readiness(library,catalog_id)
    return Builder(library,catalog_id).build()
def evidence_text(library,document,key):
    entry=next((x for x in document['evidence'] if x['id']==key),None)
    if entry is None:raise KeyError('Evidence key not in current default readiness document')
    result=library.resolve_evidence(entry['locator'])
    if result.get('verified') is not True:raise ValueError('Evidence not verified')
    data=result['data'];limit=2*1024*1024;raw=data
    compressed=entry['locator'].get('path',entry['locator'].get('member','')).endswith('.gz')
    if compressed:
        with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:raw=stream.read(limit+1)
    text=raw[:limit].decode('utf-8-sig',errors='replace')
    return {'title':'验收证据','text':text,'locator':entry['locator'],'verified':True,'bytes':len(data),'gzip_decoded_for_preview':compressed,'truncated':len(raw)>limit,'preview_limit_bytes':limit}

