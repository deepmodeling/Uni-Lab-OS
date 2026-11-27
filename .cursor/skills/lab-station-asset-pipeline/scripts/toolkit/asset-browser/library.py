"""Shared local mirror, catalog synchronization and pinned on-demand downloads."""
import sys,json,threading,shutil,time,uuid
from pathlib import Path
sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'asset-library'))
from core import read,write,digest,sha,inside,verify,canonical
from catalog import CATEGORIES
CATEGORIES={**CATEGORIES,'engineering_knowledge':'06_工程知识与规则'}

class Library:
    def __init__(self,root,oss_config,state,store=None):
        self.root=Path(root).resolve();self.oss_config=oss_config;self.state=Path(state).resolve();self.state.mkdir(parents=True,exist_ok=True)
        self._store=store;self.lock=threading.RLock();self.asset_locks={};self.checked={};self.files={};self.snapshot=None;self.source_records={};self.all_source_records={}
        self.reload()
    @property
    def store(self):
        if self._store is None:
            from credentials import load_credentials
            load_credentials()
            from oss_store import Store
            self._store=Store(self.oss_config)
        return self._store
    def reload(self):
        self.minimal=(self.root/'library.json').exists()
        if self.minimal:
            from minimal_format import load_library
            return load_library(self)
        manifest=self.root/'library.manifest.json'
        if not manifest.exists():self.manifest=None;self.records={};self.presentation={};self.collections=[];self.source_records={};self.all_source_records={};return
        self.manifest=read(manifest);self.files={f['path']:f for f in self.manifest['files']}
        index=self.checked_read('索引/资产索引.json');self.records={r['catalog_id']:r for r in index['entries']}
        self.presentation=self.checked_read('索引/展示信息.json').get('entries',{}) if '索引/展示信息.json' in self.files else {}
        source_key='索引/全量来源.json'
        source_rows=self.checked_read(source_key).get('entries',[]) if source_key in self.files else []
        self.all_source_records={}
        for row in source_rows:self.all_source_records.setdefault(row['catalog_id'],[]).append(row)
        self.source_records={r['catalog_id']:r for r in source_rows if r['catalog_id'] not in self.records}
        self.collections=[]
        for key in self.files:
            if key.startswith('专题集合/') and key.endswith('/集合.json') and (self.root/key).exists():
                c=self.checked_read(key);self.collections.append({'name':c['name'],'ids':[r['catalog_id'] for r in c['members']]})
        for family in sorted({r['source_family'] for r in source_rows}):
            self.collections.append({'name':'全量来源_'+family,'ids':[r['catalog_id'] for r in source_rows if r['source_family']==family]})
    def checked_read(self,key):
        path=inside(self.root,key)
        if key not in self.files or digest(path)!=self.files[key]['sha256']:raise ValueError('本地索引有修改或损坏：'+key)
        return read(path)
    def resolve_evidence(self,ref):
        from evidence import resolve
        return resolve(self,ref)
    def review(self,key):
        with self.lock:
            path=inside(self.root,key)
            if key not in self.files:raise ValueError('审查资料未登记')
            if not path.exists():
                data=self.store.get(key);info=self.files[key]
                if len(data)!=info['bytes'] or sha(data)!=info['sha256']:raise ValueError('OSS 审查资料校验失败')
                path.parent.mkdir(parents=True,exist_ok=True);temporary=path.with_name(path.name+'.download')
                temporary.write_bytes(data);temporary.replace(path)
            return self.checked_read(key)
    def status(self,target):
        if self.minimal:
            from minimal_format import availability
            return availability(self,target,True)
        root=inside(self.root,target['package_path'])
        if not (root/'bundle.sha256.json').exists():return 'remote'
        try:
            # Full validation on first use and whenever file size/mtime changes.
            signature=tuple((str(p.relative_to(root)),p.stat().st_size,p.stat().st_mtime_ns) for p in sorted(root.rglob('*')) if p.is_file() and not p.name.startswith('~$'))
            if self.checked.get(target['package_digest'])!=signature:verify(root,target['package_digest']);self.checked[target['package_digest']]=signature
            return 'local'
        except Exception:return 'damaged'
    def catalog_status(self,target):
        """Availability only; prepare() still verifies every byte before use.

        Browsing thousands of entries must not reread all native files/USDs.
        A local marker is a cache indication, never a qualification result.
        """
        if self.minimal:
            from minimal_format import availability
            return availability(self,target)
        root=inside(self.root,target['package_path']);seal=root/'bundle.sha256.json'
        if not seal.exists():return 'remote'
        if not (root/'package.json').is_file():return 'damaged'
        try:
            return 'local' if digest(seal)==target['package_digest'] else 'damaged'
        except OSError:return 'damaged'
    def list(self):
        rows=[];statuses={}
        for r in self.records.values():
            t=r['targets'][0];pd=t['package_digest']
            if pd not in statuses:statuses[pd]=self.catalog_status(t)
            display=self.presentation.get(r['catalog_id'],{});thumb=display.get('thumbnail',{});path=self.root/thumb.get('path','__missing__')
            rows.append({**r,'summary':display.get('summary',self.summary(r)),'thumbnail':thumb.get('path'),'thumbnail_local':str(path) if path.is_file() else None,
                         'appearance':{'status':thumb.get('material_status','not_recorded'),'fallback_faces':thumb.get('fallback_faces'),'source_package_digest':thumb.get('source_package_digest'),
                             'color_policy':thumb.get('color_policy','preserve'),'display_adaptation':thumb.get('display_adaptation','none')},
                         'availability':statuses[pd],'insertable':'spatial' in t['entrypoints'],'category_label':CATEGORIES[r['reuse_kind']][3:],
                         'has_source_attempts':r['catalog_id'] in self.all_source_records,'integrity_check':'full_verification_on_prepare'})
        for r in self.source_records.values():
            # Large attempt/evidence trees are read through /api/notes on demand.
            # Keep the shared web/Blender browse response bounded per card.
            card={k:r[k] for k in ('catalog_id','title','manufacturer','model','reuse_kind','source_family','source_sha256','state','source_card','retained_source') if k in r}
            rows.append({**card,'record_kind':'source_record','original_name':r['title'],'configuration':'原始来源（转换待完成）',
                         'primary_purpose':'source','targets':[],'tags':[r['source_family'],'源文件保留'],
                         'summary':'已保存原件与逐次转换记录；当前状态：'+r['state']+'。尚无通过验收的可插入模型。',
                         'qualification':{'scope':'source_preserved','physics':'not_run','cad_correspondence':'not_claimed'},
                         'thumbnail':None,'thumbnail_local':None,'appearance':{},'availability':'source_only',
                         'insertable':False,'category_label':CATEGORIES[r['reuse_kind']][3:]})
        return {'entries':rows,'categories':CATEGORIES,'collections':self.collections,'root':str(self.root),'offline_ready':self.manifest is not None,
                'usable_catalog_records':len(self.records),'source_only_records':len(self.source_records)}
    @staticmethod
    def summary(r):
        if r.get('summary'):return r['summary']
        label=CATEGORIES[r['reuse_kind']][3:]
        if r['reuse_kind']=='engineering_knowledge':return '可复用的选型规则、参数模板和工程资料；适用范围见说明。'
        if r['reuse_kind']=='station_template':return '旋蒸资产库接入验收布局。保留 OS graph 与固定依赖；仅用于布局预览。'
        suffix='来自所属装配，插入时恢复该零件的 CAD 局部坐标。' if r['record_kind']=='package_member' else '支持按固定版本重复实例化。'
        return f"{label} · {r['original_name']} · 配置 {r['configuration']}。{suffix}几何已核验；物理与接口资格未验证。"
    def sync(self,full=False):
        if self.minimal or read(self.oss_config).get('layout') in ('minimal/1','nvidia-usd/1'):
            from minimal_format import sync
            with self.lock:return sync(self,full)
        with self.lock:
            manifest_bytes=self.store.get('library.manifest.json');manifest=json.loads(manifest_bytes)
            files={f['path']:f for f in manifest['files']};needed=[k for k in files if full or k.startswith(('索引/','预览/','专题集合/','按厂家/')) or k=='README.md' or k.endswith('/asset.ref.json') or (k.count('/')==1 and k.endswith('/README.md'))]
            old=self.files if self.manifest else {};staging=self.state/'sync'/uuid.uuid4().hex;staging.mkdir(parents=True)
            changed=[]
            for key in needed:
                dest=inside(self.root,key);info=files[key]
                if dest.exists():
                    h=digest(dest)
                    if h==info['sha256']:continue
                    if key not in old or h!=old[key]['sha256']:raise ValueError('本地文件已修改，保留现场：'+key)
                data=self.store.get(key)
                if len(data)!=info['bytes'] or sha(data)!=info['sha256']:raise ValueError('OSS 文件校验失败：'+key)
                path=inside(staging,key);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data);changed.append(key)
            # All downloads have passed before updating the local catalog snapshot.
            for key in changed:
                dest=inside(self.root,key);dest.parent.mkdir(parents=True,exist_ok=True)
                if dest.exists():
                    backup=self.state/'metadata-history'/digest(dest)/Path(key).name;backup.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(dest,backup)
                (staging/key).replace(dest)
            self.root.mkdir(parents=True,exist_ok=True);tmp=self.root/('.manifest-'+uuid.uuid4().hex);tmp.write_bytes(manifest_bytes);tmp.replace(self.root/'library.manifest.json');self.reload()
            if hasattr(self.store,'_routes'):self.store._routes=None
            return {'status':'passed','downloaded':len(changed),'mode':'full' if full else 'catalog_and_previews','records':len(self.records)}
    def prepare(self,catalog_id):
        if catalog_id not in self.records:raise ValueError('目录中没有此资产')
        if self.minimal:
            from minimal_format import prepare
            return prepare(self,catalog_id)
        r=self.records[catalog_id];target=r['targets'][0]
        with self.lock:lock=self.asset_locks.setdefault(target['package_digest'],threading.Lock())
        with lock:
            state=self.status(target);dest=inside(self.root,target['package_path'])
            if state=='damaged':raise ValueError('本地资产被修改或损坏，已保留；请选择新库目录恢复')
            if state=='remote':
                from client import fetch
                cached,receipt=fetch(self.store,target['asset_id'],target['package_digest'],self.state/'download-cache')
                if dest.exists():raise ValueError('存在未完成的本地目录，保留现场：'+str(dest))
                temp=self.state/'materialize'/uuid.uuid4().hex;temp.parent.mkdir(parents=True,exist_ok=True);shutil.copytree(cached,temp)
                verify(temp,target['package_digest']);dest.parent.mkdir(parents=True,exist_ok=True)
                # Works across volumes: copy then verify; incomplete copies stay diagnosable.
                shutil.copytree(temp,dest);verify(dest,target['package_digest'])
            verify(dest,target['package_digest'])
            return {'catalog_id':r['catalog_id'],'title':r['title'],'reuse_kind':r['reuse_kind'],'configuration':r['configuration'],'target':target,'package_root':str(dest),'downloaded':state=='remote','qualification':r['qualification']}
