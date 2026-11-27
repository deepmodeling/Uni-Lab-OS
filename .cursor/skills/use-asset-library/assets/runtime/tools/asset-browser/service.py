"""Local catalog bridge shared by the web UI and Blender. No OSS secrets over HTTP."""
import sys
sys.dont_write_bytecode=True
import argparse,json,mimetypes,os,threading,time,uuid,traceback
from pathlib import Path
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlsplit,parse_qs,unquote
from concurrent.futures import ThreadPoolExecutor
from library import Library
import readiness
from access_guidance import safe_error

class App:
    def __init__(self,config):
        self.config=config;self.library=Library(config['library_root'],config['oss_config'],config['state_root']);self.jobs={};self.pool=ThreadPoolExecutor(max_workers=3)
    def assets(self):
        with self.library.lock:
            if (self.library.root/'library.json').exists():
                stamp=(self.library.root/'library.json').stat().st_mtime_ns
                if stamp!=getattr(self,'candidate_stamp',None):
                    self.library.reload()
                    self.candidate_stamp=stamp
            data=self.library.list()
            # Batch collections remain provenance in the source index; they
            # are not the current category/scene navigation contract.
            data['collection_index_count']=len(data.get('collections',[]))
            data['collections']=[]
            return data

    def job(self,kind,data):
        if kind not in ('prepare','sync','sync_full'):raise ValueError('不支持的操作')
        if self.config.get('local_candidates') and kind.startswith('sync'):
            self.assets()
            return self.local_refresh_receipt()
        if kind=='prepare' and data.get('catalog_id') not in self.library.records:raise ValueError('资产不在目录中')
        key=uuid.uuid4().hex;job={'id':key,'state':'queued','kind':kind};self.jobs[key]=job
        def execute():
            job['state']='running'
            try:
                if kind=='prepare':
                    result=self.library.prepare(data['catalog_id'])
                elif self.config.get('catalog_sync')=='square':
                    from catalog_sync import sync
                    with self.library.lock:
                        result=sync(self.library,previews=self.config.get('download_previews',False))
                    if kind=='sync_full':
                        for catalog_id,record in self.library.records.items():
                            if record.get('targets'):self.library.prepare(catalog_id)
                        result['mode']='full'
                else:
                    result=self.library.sync(full=kind=='sync_full')
                job.update(state='done',result=result)
            except Exception as exc:
                # OSS exceptions may contain signed requests; never forward them.
                problem=safe_error(exc)
                job.update(state='failed',error=problem['message'],error_code=problem['code'])
        self.pool.submit(execute);return {'id':key,'state':'queued'}
    def local_refresh_receipt(self):
        key=uuid.uuid4().hex
        self.jobs[key]={'id':key,'state':'done','result':{'records':len(self.library.records),'downloaded':0}}
        return {'id':key,'state':'done'}

    def readiness(self,catalog_id):
        # Detail is deliberately on demand; no full-library evidence scan or
        # persistent pass cache that can survive a changed default/package.
        with self.library.lock:return readiness.build(self.library,catalog_id)

class Handler(BaseHTTPRequestHandler):
    server_version='UniLabCatalog/0.2.6'
    def log_message(self,format,*args):pass
    @property
    def app(self):return self.server.app
    def valid_host(self):return self.headers.get('Host') in ('127.0.0.1:'+str(self.server.server_port),'localhost:'+str(self.server.server_port))
    def reply(self,value,status=200):
        data=json.dumps(value,ensure_ascii=False).encode();self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8');self.send_header('Cache-Control','no-store');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    def file(self,path,download=False):
        data=Path(path).read_bytes();self.send_response(200);self.send_header('Content-Type',mimetypes.guess_type(str(path))[0] or 'application/octet-stream');self.send_header('Content-Length',str(len(data)));self.send_header('X-Content-Type-Options','nosniff')
        if download:self.send_header('Content-Disposition','attachment')
        self.end_headers();self.wfile.write(data)
    def do_GET(self):
        if not self.valid_host():return self.reply({'error':'Invalid host'},403)
        uri=urlsplit(self.path);args=parse_qs(uri.query)
        try:
            if uri.path in ('/','/index.html'):
                port=int(self.app.config.get('square_port',8766))
                self.send_response(302);self.send_header('Location',f'http://127.0.0.1:{port}/');self.send_header('Content-Length','0');self.end_headers();return
            if uri.path=='/api/health':return self.reply({'status':'ready','version':'0.2.6','root':str(self.app.library.root)})
            if uri.path=='/api/assets':return self.reply(self.app.assets())
            if uri.path=='/api/readiness':return self.reply(self.app.readiness(args.get('id',[''])[0]))
            if uri.path=='/api/readiness/evidence':
                document=self.app.readiness(args.get('id',[''])[0])
                return self.reply(readiness.evidence_text(self.app.library,document,args.get('key',[''])[0]))
            if uri.path=='/api/notes':
                source=self.app.library.source_records.get(args.get('id',[''])[0])
                if source:return self.reply({'title':source['title']+' · 来源与转换记录','text':json.dumps(source,ensure_ascii=False,indent=2)})
                record=self.app.library.records[args.get('id',[''])[0]];supplemental=record.get('supplemental')
                sources=self.app.library.all_source_records.get(record['catalog_id'],[])
                if sources:
                    notes=self.app.library.review(supplemental['metadata_path']) if supplemental else {}
                    return self.reply({'title':record['title']+' · 转换与额外内容','text':json.dumps({'qualification':record['qualification'],'source_attempts':sources,**notes},ensure_ascii=False,indent=2)})
                if not supplemental:return self.reply({'error':'此条目没有附加审查资料'},404)
                notes=self.app.library.review(supplemental['metadata_path'])
                return self.reply({'title':record['title']+' · 转换与额外内容','text':json.dumps({'qualification':record['qualification'],**notes,'indexed_source_catalog':record.get('source_catalog',{})},ensure_ascii=False,indent=2)})
            if uri.path.startswith('/api/jobs/'):
                job=self.app.jobs.get(uri.path.rsplit('/',1)[-1]);return self.reply(job or {'error':'任务不存在'},200 if job else 404)
            if uri.path=='/api/thumbnail':
                record=self.app.library.presentation.get(args.get('id',[''])[0],{});thumb=record.get('thumbnail')
                if not thumb:return self.reply({'error':'暂无缩略图'},404)
                from core import inside,digest
                path=inside(self.app.library.root,thumb['path'])
                if digest(path)!=thumb['sha256']:raise ValueError('缩略图摘要不符')
                return self.file(path)
            if uri.path=='/api/document':
                rid=args.get('id',[''])[0];record=self.app.library.records[rid];t=record['targets'][0]
                if record['reuse_kind']!='engineering_knowledge':raise ValueError('不是资料条目')
                if self.app.library.status(t)!='local':raise ValueError('请先准备到本地')
                from core import inside,digest
                ep=t['entrypoints']['document'];path=inside(self.app.library.root,t['package_path']+'/'+ep['path'])
                if digest(path)!=ep['sha256']:raise ValueError('资料摘要不符')
                return self.reply({'title':record['title'],'text':path.read_text(encoding='utf8')})
            if uri.path=='/downloads/unilab_asset_browser.zip':return self.file(Path(self.app.config.get('addon_zip',str(Path(self.app.config['state_root']).parent/'distribution/unilab_asset_browser.zip'))),True)
            static={'/':'index.html','/app.js':'app.js','/style.css':'style.css'}
            if uri.path in static:return self.file(Path(__file__).parent/'web'/static[uri.path])
            return self.reply({'error':'Not found'},404)
        except Exception as exc:return self.reply({'error':str(exc) if not type(exc).__module__.startswith('oss2') else 'OSS 访问失败'},400)
    def do_POST(self):
        if not self.valid_host():return self.reply({'error':'Invalid host'},403)
        origin=self.headers.get('Origin');allowed=('http://127.0.0.1:'+str(self.server.server_port),'http://localhost:'+str(self.server.server_port))
        if (origin and origin not in allowed) or self.headers.get('X-UniLab-Client') not in ('web','blender','test'):return self.reply({'error':'请求来源不被允许'},403)
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=16384:raise ValueError('请求长度无效')
            data=json.loads(self.rfile.read(length))
            if self.path=='/api/jobs':return self.reply(self.app.job(data['kind'],data),202)
            return self.reply({'error':'Not found'},404)
        except Exception as exc:return self.reply({'error':str(exc)},400)

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--config');ap.add_argument('--save-credentials',action='store_true');a=ap.parse_args()
    if a.save_credentials:
        from credentials import save_credentials
        print(json.dumps(save_credentials()));return
    config=json.loads(Path(a.config).read_text(encoding='utf8'));app=App(config)
    server=ThreadingHTTPServer(('127.0.0.1',config.get('port',8765)),Handler);server.app=app
    print(json.dumps({'status':'ready','url':'http://127.0.0.1:'+str(server.server_port)}),flush=True);server.serve_forever()
if __name__=='__main__':main()
