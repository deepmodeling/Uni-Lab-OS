"""OSS 私有、不可覆盖对象发布与签名读取。凭据仅来自环境。"""
import json
import io,zipfile,threading,hashlib
import sys
sys.dont_write_bytecode=True
from pathlib import Path
import oss2
from oss2.credentials import EnvironmentVariableCredentialsProvider
from core import read,write,digest,sha,canonical,verify

class Store:
    def __init__(self,config):
        self.config=read(config);c=self.config;self.prefix=c['prefix']
        if not self.prefix or not self.prefix.endswith('/'):raise ValueError('存储前缀必须明确')
        # Reuse connections across the bounded publication worker pool.
        session=oss2.Session(pool_size=128)
        self.bucket=oss2.Bucket(oss2.ProviderAuthV4(EnvironmentVariableCredentialsProvider()),c['endpoint'],c['bucket'],region=c['region'],connect_timeout=20,session=session)
        self.versioning=self.bucket.get_bucket_versioning().status
        self._route_lock=threading.RLock();self._routes=None;self._archive=None
        self.readable_root=self.prefix[:-3] if self.prefix.endswith('/v1/') else self.prefix

    def _readable_routes(self):
        with self._route_lock:
            if self._routes is None:
                try:self._routes=json.loads(self.bucket.get_object(self.readable_root+'索引/发布位置.json').read())
                except oss2.exceptions.NoSuchKey:return None
            return self._routes

    def _legacy_get(self,key):
        with self._route_lock:
            if self._archive is None:
                meta=json.loads(self.bucket.get_object(self.readable_root+'索引/历史兼容.json').read())
                local=Path(self.config.get('mirror_root','__no_local_mirror__'))/meta['archive_path']
                data=local.read_bytes() if local.is_file() else self.bucket.get_object(self.readable_root+meta['archive_path']).read()
                if sha(data)!=meta['sha256']:raise ValueError('历史归档摘要不符')
                self._archive=zipfile.ZipFile(io.BytesIO(data))
            data=self._archive.read('v1/'+key)
            return data

    def get(self,key):
        if key.startswith('v1/') and self._readable_routes():return self.get(key[3:])
        # Old logical IDs/locks remain valid, but no UUID-named OSS objects are
        # necessary. New releases route to readable directories; old exact bytes
        # route to a verified archive. Also works with historical /v1/ configs.
        if key.startswith(('releases/','index/','catalog/','browse/','checks/','sources/','failures/')):
            routes=self._readable_routes()
            if routes:
                for r in routes['releases']:
                    prefix=f'releases/{r["asset_id"]}/{r["package_digest"]}/'
                    if key.startswith(prefix):return self.bucket.get_object(self.readable_root+r['path']+'/'+key[len(prefix):]).read()
                    if key==f'index/releases/{r["asset_id"]}/{r["package_digest"]}.json':return canonical(r['entry'])
                return self._legacy_get(key)
        return self.bucket.get_object(self.prefix+key).read()
    def list(self,prefix):
        if prefix.startswith(('index/','releases/','catalog/','browse/','checks/','sources/','failures/')):
            routes=self._readable_routes()
            if routes:
                meta=json.loads(self.bucket.get_object(self.readable_root+'索引/历史兼容.json').read())
                keys={o['key'][len(meta['legacy_prefix']):] for o in meta['objects']}
                keys.update(f'index/releases/{r["asset_id"]}/{r["package_digest"]}.json' for r in routes['releases'])
                return sorted(k for k in keys if k.startswith(prefix))
        return [o.key[len(self.prefix):] for o in oss2.ObjectIteratorV2(self.bucket,prefix=self.prefix+prefix,max_keys=1000)]

    def put_immutable(self,key,data):
        if self.config.get('layout')=='readable/1' and key.startswith(('releases/','index/','catalog/','browse/','checks/','sources/','failures/')):
            raise ValueError('可读库禁止重建 UUID/摘要散文件目录；使用 readable_library.py 发布')
        if self.prefix.endswith('/v1/') and self._readable_routes():raise ValueError('旧 v1 已归档，只读兼容；不能重新发布散文件')
        if self.versioning not in (None, ''):
            raise ValueError('此发布器要求未启用过版本控制的 Bucket；OSS forbid-overwrite 在版本控制下无效')
        full=self.prefix+key
        if self.bucket.object_exists(full):
            if self.get(key)!=data:raise ValueError('拒绝覆盖现有不同内容对象')
            action='reused'
        else:
            try:self.bucket.put_object(full,data,headers={'x-oss-forbid-overwrite':'true','x-oss-object-acl':'private','x-oss-meta-sha256':sha(data)})
            except oss2.exceptions.ServerError as exc:
                if exc.status!=409:raise
                if self.get(key)!=data:raise ValueError('并发对象内容不一致')
            action='created'
        # Existing objects were already fully read and compared above.
        if action=='created' and self.get(key)!=data:raise ValueError('OSS 读回摘要失败')
        if self.bucket.get_object_acl(full).acl!='private':raise ValueError('对象未设为私有')
        return {'key':full,'sha256':sha(data),'bytes':len(data),'action':action,'acl':'private'}

    def copy_or_publish(self,key,data,source_key):
        """Rename an unpublished category using a conditional, private OSS copy.

        Verify local SHA256, MD5 and CRC64 against the source and destination.
        SHA256 metadata alone is never treated as a byte-integrity proof.
        Missing source objects use the ordinary full upload/readback path.
        """
        if self.prefix!='asset-library/' or self.versioning not in (None,''):raise ValueError('Unexpected copy scope/versioning')
        for rel in (key,source_key):
            if not rel or '\\' in rel or any(p in ('','.','..') for p in rel.split('/')):raise ValueError('Unsafe copy key')
        crc=oss2.utils.Crc64();crc.update(data);expected_sha=sha(data);expected_md5=hashlib.md5(data).hexdigest()
        def check(head):
            if head.content_length!=len(data) or head.server_crc!=crc.crc or head.headers.get('x-oss-meta-sha256')!=expected_sha or head.etag.lower()!=expected_md5:
                raise ValueError('OSS copy byte checks differ')
        if key in getattr(self,'_publication_existing',set()):
            check(self.bucket.head_object(self.prefix+key))
            if self.bucket.get_object_acl(self.prefix+key).acl!='private':raise ValueError('Copied object not private')
            return {'key':self.prefix+key,'sha256':expected_sha,'bytes':len(data),'action':'copy_reused','acl':'private'}
        try:source=self.bucket.head_object(self.prefix+source_key)
        except oss2.exceptions.NoSuchKey:return self.put_immutable(key,data)
        check(source)
        try:
            self.bucket.copy_object(self.config['bucket'],self.prefix+source_key,self.prefix+key,headers={
                'x-oss-copy-source-if-match':source.etag,'x-oss-forbid-overwrite':'true','x-oss-object-acl':'private','x-oss-metadata-directive':'COPY'})
            action='server_copied'
        except oss2.exceptions.ServerError as exc:
            if exc.status!=409:raise
            action='copy_reused'
        check(self.bucket.head_object(self.prefix+key))
        if self.bucket.get_object_acl(self.prefix+key).acl!='private':raise ValueError('Copied object not private')
        return {'key':self.prefix+key,'sha256':expected_sha,'bytes':len(data),'action':action,'acl':'private',
                'validation':'source_and_destination_length_md5_crc64_sha256_metadata'}

    def publish(self,root):
        root=Path(root).resolve();result=verify(root);p=read(root/'package.json');package_digest=result['package_digest']
        base=f'releases/{p["asset_id"]}/{package_digest}/'
        transfers=[]
        # 文件完整上传与读回后，最后一条登记才使该版本可发现。
        manifest=read(root/'bundle.sha256.json')
        for item in manifest['files']:
            transfers.append(self.put_immutable(base+item['path'],(root/item['path']).read_bytes()))
        transfers.append(self.put_immutable(base+'bundle.sha256.json',(root/'bundle.sha256.json').read_bytes()))
        entry={'schema':'unilab.asset-index-entry/1','asset_id':p['asset_id'],'title':p['title'],
               'definition_revision':p['definition_revision'],'configuration':p['configuration'],
               'package_digest':package_digest,'package_key':base+'package.json','seal_key':base+'bundle.sha256.json',
               'entrypoints':p['entrypoints'],'capabilities':p['capabilities'],'counts':result,
               'bucket':self.config['bucket'],'prefix':self.prefix}
        entry_key=f'index/releases/{p["asset_id"]}/{package_digest}.json'
        transfers.append(self.put_immutable(entry_key,canonical(entry)))
        return {'status':'passed','entry':entry,'entry_key':entry_key,'transfers':transfers}

    def catalog(self):
        entries=[json.loads(self.get(k)) for k in sorted(self.list('index/releases/')) if k.endswith('.json')]
        content=canonical({'schema':'unilab.asset-index/1','entries':entries})
        key='index/snapshots/'+sha(content)+'.json';receipt=self.put_immutable(key,content)
        return {'status':'passed','entries':entries,'snapshot':receipt}

def main():
    import argparse
    ap=argparse.ArgumentParser();ap.add_argument('action',choices=['probe','publish','catalog']);ap.add_argument('--config',required=True);ap.add_argument('--bundle');ap.add_argument('--output',required=True);a=ap.parse_args()
    try:
        s=Store(a.config)
        if a.action=='probe':
            version=s.bucket.get_bucket_versioning()
            result={'status':'passed','bucket_acl':s.bucket.get_bucket_acl().acl,'versioning':getattr(version,'status',None),'existing_keys':s.list('index/releases/'),'sdk':oss2.__version__}
        elif a.action=='publish':result=s.publish(a.bundle)
        else:result=s.catalog()
        write(a.output,result)
        print(json.dumps({'status':result['status'],'action':a.action,'output':a.output,'entries':len(result.get('entries',[]))}))
    except Exception as exc:
        # OSS 错误原文可能含签名请求，禁止写入日志或回执。
        result={'status':'failed','error_type':type(exc).__name__,'oss_code':getattr(exc,'code',None),'http_status':getattr(exc,'status',None)}
        if not isinstance(exc,oss2.exceptions.OssError):result['reason']=str(exc)
        write(a.output,result);print(json.dumps(result));sys.exit(1)

if __name__=='__main__':main()
