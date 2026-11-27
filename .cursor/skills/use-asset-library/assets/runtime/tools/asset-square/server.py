"""One stdlib HTTP service; static UI + read-only library projection, port 8766."""
import argparse
import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from catalog import Catalog
from catalog import inside
from portal import Portal

WEB=Path(__file__).parent/'web'
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def send(self,body,kind='application/json; charset=utf-8',status=200,cache='no-store'):
        if not isinstance(body,bytes):body=json.dumps(body,ensure_ascii=False).encode('utf-8')
        self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control',cache)
        self.send_header('X-Content-Type-Options','nosniff');self.send_header('Referrer-Policy','same-origin')
        self.send_header('Content-Security-Policy',"default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'"+ (" 'unsafe-inline'" if urlsplit(self.path).path.startswith('/reference/') else '')+"; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers();self.wfile.write(body)
    def do_GET(self):
        uri=urlsplit(self.path);args={k:v[0] for k,v in parse_qs(uri.query).items()};cat=self.server.catalog
        try:
            portal=self.server.portal
            if uri.path=='/setup.html':return self.send((WEB/'setup.html').read_bytes(),'text/html; charset=utf-8')
            if uri.path=='/api/portal/blender':
                portal.load()
                return self.send({'entries':[{k:v for k,v in r.items() if k!='_search'} for r in portal.entries],
                    'types':portal.types,'library_digest':portal.digest})
            if uri.path=='/api/catalog/dictionaries':
                if not portal.store:raise KeyError('尚未转换统一目录')
                from unified import read
                portal.store.load()
                return self.send({'manufacturers':read(portal.store.root/'manufacturers.json'),
                    'categories':read(portal.store.root/'分类/category-index.json'),
                    'facets':read(portal.store.root/'facets.json'),
                    'facet_aliases':portal.store.index.get('facet_aliases',{}),
                    'stage_aliases':portal.store.index.get('stage_aliases',{}),'scene_plan':portal.store.index.get('scene_plan')})
            if uri.path=='/api/catalog/cover':
                if not portal.store:raise KeyError('尚未转换统一目录')
                entry=portal.store.entry(args.get('id',''));cover=entry.get('cover')
                if not cover or not isinstance(cover,str):raise KeyError('无图片')
                candidates=[]
                for base in (cat.root,portal.root):
                    try:candidates.append(inside(base,cover))
                    except ValueError:pass
                # Historical table images sometimes contain only their basename.
                if '/' not in cover and '\\' not in cover:
                    candidates+=list((portal.root/'images').rglob(cover))
                p=next((p for p in candidates if p.is_file() and p.suffix.lower() in ('.png','.jpg','.jpeg','.webp','.gif')),None)
                if p is None:raise KeyError('图片不可用')
                return self.send(p.read_bytes(),mimetypes.guess_type(p.name)[0] or 'application/octet-stream')
            if uri.path=='/api/catalog/entry':
                if not portal.store:raise KeyError('尚未转换统一目录')
                return self.send(portal.store.entry(args.get('id','')))
            if uri.path=='/api/catalog/reference':
                if not portal.store:raise KeyError('尚未转换统一目录')
                return self.send(portal.store.reference(args.get('id',''),args.get('version')))
            if uri.path=='/api/portal/summary':return self.send(portal.summary(args))
            if uri.path=='/api/portal/entries':return self.send(portal.query(args))
            if uri.path=='/api/portal/record':return self.send(portal.detail(args.get('id','')))
            if uri.path.startswith('/reference/'):
                from urllib.parse import unquote
                relative=unquote(uri.path[len('/reference/'):]);p=inside(portal.root,relative)
                if relative not in ('index.html','types.html','matrix.html') and not relative.startswith('images/'):raise KeyError(relative)
                return self.send(p.read_bytes(),mimetypes.guess_type(p.name)[0] or 'application/octet-stream')
            if uri.path=='/api/health':
                s=cat.load();return self.send({'status':'ready','service':'asset-square','root':str(cat.root),'records':len(s['rows']),'snapshot_sha256':s['digest']})
            if uri.path=='/api/assets':return self.send(cat.query(args))
            if uri.path=='/api/asset':return self.send(cat.detail(args.get('id',''),args.get('version')))
            if uri.path=='/api/reference':return self.send(cat.detail(args.get('id',''),args.get('version'))['reference'])
            if uri.path=='/api/thumbnail':return self.send(cat.thumbnail(args.get('id',''),args.get('v')),'image/png',cache='private, max-age=3600')
            routes={'/catalog-navigation.js':'catalog-navigation.js','/filter-layout.css':'filter-layout.css','/':'index.html','/index.html':'index.html','/consumables':'index.html','/consumables.html':'index.html','/types.html':'types.html','/tags.html':'tags.html','/portal.js':'portal.js','/app.js':'app.js','/style.css':'style.css','/reference.css':'reference.css'}
            if uri.path in routes:
                p=WEB/routes[uri.path];kind=mimetypes.guess_type(p.name)[0] or 'application/octet-stream';return self.send(p.read_bytes(),kind+'; charset=utf-8')
            self.send({'error':'未找到页面'},status=404)
        except (KeyError,FileNotFoundError):self.send({'error':'当前索引中没有这个条目或文件'},status=404)
        except (ValueError,TypeError) as exc:self.send({'error':str(exc)},status=400)
        except OSError:self.send({'error':'资产库暂不可读，请检查镜像路径'},status=503)
    def do_POST(self):self.send({'error':'设备广场为只读服务'},status=405)

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--root',default='D:/Asset_library');ap.add_argument('--host',default='127.0.0.1');ap.add_argument('--port',type=int,default=8766);ap.add_argument('--directory',help='独立统一目录快照，用于候选验证');args=ap.parse_args()
    cat=Catalog(args.root);snap=cat.load();server=ThreadingHTTPServer((args.host,args.port),Handler);server.catalog=cat;server.portal=Portal(cat)
    if args.directory:
        from unified import UnifiedStore
        server.portal.store=UnifiedStore(args.directory)
    print(json.dumps({'url':f'http://{args.host}:{args.port}/','records':len(snap['rows']),'read_only':True}),flush=True);server.serve_forever()
if __name__=='__main__':main()
