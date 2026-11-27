"""Read-only dictionaries and deterministic public-record projections."""
import json
from collections import Counter
from pathlib import Path
from urllib.parse import quote

from unified import atomic, encoded, digest, read, validate, TYPE_LABELS
from discovery_fields import canonical_stage, stage_facets, model_presence, STAGE_ALIASES
from scene_catalog import bind_scenes, has_os_module, stable_id


class Dictionaries:
    def __init__(self, root, library_root=None):
        self.root=Path(root)
        self.library_root=Path(library_root) if library_root else self.root.parent
        self.taxonomy=read(self.root/'分类/category-index.json')
        self.nodes={n['id']:n for n in self.taxonomy['canonical_nodes']}
        self.manufacturers=read(self.root/'manufacturers.json')['records']
        self.facets=read(self.root/'facets.json')['records']
        self.facet_aliases=stage_facets(self.facets)
        self.scene_plan=read(self.root/'分类/scene-index.json') if (self.root/'分类/scene-index.json').exists() else None
        self.ancestors={}
        for key in self.nodes:
            chain=[];current=key
            while current is not None:
                if current in chain:raise ValueError('分类存在循环: '+key)
                chain.append(current);current=self.nodes[current]['parent_id']
            self.ancestors[key]=list(reversed(chain))

    def categories(self, entry):
        ids=[t for t in entry['tags'] if t in self.nodes]
        for key in ids:
            if self.nodes[key]['resource_type']!=entry['resource_type']:
                raise ValueError('分类与 resource_type 冲突: '+entry['uuid']+' '+key)
        return ids

    def check(self, entry, known_ids, require_category=False):
        validate(entry)
        ids=self.categories(entry)
        if require_category and not any(self.nodes[k]['parent_id'] is not None for k in ids):
            raise ValueError('未选择具体分类: '+entry['uuid'])
        if any(t not in self.nodes and t not in self.facets for t in entry['tags']):
            raise ValueError('未登记的标签: '+entry['uuid'])
        mid=entry['manufacturer_id']
        if mid is not None and mid not in self.manufacturers:
            raise ValueError('未登记的厂家: '+str(mid))
        for relation in entry['relations']:
            if relation['target_uuid'] not in known_ids:
                raise ValueError('关联目标不存在: '+relation['target_uuid'])
        return ids

    def project(self, entry, previous):
        row={k:v for k,v in previous.items() if k not in ('_search','reuse_kind','incoming_relations')}
        ids=self.categories(entry)
        ancestry=list(dict.fromkeys(a for key in ids for a in self.ancestors[key]))
        mid=entry['manufacturer_id'];manufacturer=self.manufacturers.get(mid,{})
        facets=[self.facets[t] for t in entry['tags'] if t in self.facets]
        def names(kind):return list(dict.fromkeys(canonical_stage(t['name']) if kind=='experimental_step' else t['name'] for t in facets if t['type']==kind))
        domains=names('experimental_domain') or ['待归类']
        stages=names('experimental_step') or ['待归类']
        scenes=names('experimental_scene')
        targets=(entry.get('model') or {}).get('targets',[]) if isinstance(entry.get('model'),dict) else []
        has_3d_model,model_3d_status=model_presence(entry.get('model'),self.library_root)
        display_tags=list(dict.fromkeys(self.facet_aliases.get(t,t) for t in entry['tags']))
        row.update(id=entry['uuid'],uuid=entry['uuid'],title=entry['display_name'],name=entry['name'],
            resource_type=entry['resource_type'],square='consumables' if entry['resource_type']=='resource' else 'equipment',
            manufacturer_id=mid,manufacturer=manufacturer.get('name') or '未知厂家',
            manufacturer_status=manufacturer.get('status','unknown'),manufacturer_aliases=manufacturer.get('aliases',[]),
            tags=entry['tags'],tag_ids=list(dict.fromkeys(entry['tags']+display_tags+ancestry)),
            tag_labels=list(dict.fromkeys(self.nodes[t]['name'] if t in self.nodes else canonical_stage(self.facets[t]['name']) if self.facets[t]['type']=='experimental_step' else self.facets[t]['name'] for t in display_tags if t in self.nodes or self.facets[t]['type']!='legacy_keyword')),
            types=ids,category_ids=ids,category_ancestors=ancestry,
            category_paths=[' / '.join(self.nodes[t]['path']) for t in ids],
            classification_status='mapped' if ids else 'unresolved',
            majors=list(dict.fromkeys(self.ancestors[t][1] if len(self.ancestors[t])>1 else t for t in ids)),
            domains=domains,stages=stages,scenes=scenes,
            has_3d_model=has_3d_model,model_3d_status=model_3d_status,
            description=entry.get('description') or '',module=entry.get('module'),
            cover=entry.get('cover'),image='/api/catalog/cover?id='+quote(entry['uuid']) if entry.get('cover') else None,
            relations=entry['relations'],has_cad=any(any(v.get('format','').lower() in ('step','stp','sldprt','sldasm','iges','igs') for v in t.get('entrypoints',{}).values() if isinstance(v,dict)) for t in targets))
        row.setdefault('source','统一目录');row.setdefault('status','registered')
        if targets:row['asset_id']=targets[0]['asset_id']
        else:row.pop('asset_id',None)
        row['image_note']=previous.get('image_note','来源图片')
        row['has_os_module']=has_os_module(entry.get('module'))
        identity=(entry.get('device_params') or {}).get('labware_identity',{}) if isinstance(entry.get('device_params'),dict) else {}
        row['search_aliases']=identity.get('names',[])+identity.get('catalog_numbers',[])
        if self.scene_plan:
            bindings=bind_scenes(entry,ancestry,self.scene_plan)
            row['scene_bindings']=bindings
            row['domains']=list(dict.fromkeys(b['domain'] for b in bindings)) or ['待归类']
            row['stages']=list(dict.fromkeys(b['stage'] for b in bindings)) or ['待归类']
            row['scenes']=list(dict.fromkeys(b['scene'] for b in bindings))
            replaced={'experimental_domain','experimental_scene','experimental_step'}
            row['tag_ids']=[t for t in row['tag_ids'] if self.facets.get(t,{}).get('type') not in replaced]
            row['tag_ids']+=list(dict.fromkeys([b['scene_id'] for b in bindings]+[stable_id('domain',b['domain']) for b in bindings]+[stable_id('stage',b['stage']) for b in bindings]))
            row['tag_labels']=[self.nodes[t]['name'] for t in ids]+row['domains']+row['scenes']+row['stages']
        return row


def rebuild(root, index, library_root=None):
    root=Path(root);d=Dictionaries(root,library_root);old={r['uuid']:r for r in index['entries']}
    entries={};files={}
    for path in sorted((root/'条目').glob('*.json')):
        data=path.read_bytes();e=json.loads(data);key=e['uuid']
        if path.stem!=key or key in entries:raise ValueError('条目文件名/身份冲突')
        entries[key]=e;files[key]={'path':path.relative_to(root).as_posix(),'sha256':digest(data)}
    rows=[]
    for key,e in entries.items():
        d.check(e,entries)
        rows.append(d.project(e,old.get(key,{})))
    byid={r['uuid']:r for r in rows}
    for row in rows:
        row['relations']=[{**rel,'target_name':entries[rel['target_uuid']]['display_name']} for rel in row['relations']]
        row['related_ids']=[rel['target_uuid'] for rel in row['relations']]
    for row in rows:
        for rel in row['relations']:
            other=byid[rel['target_uuid']]
            other.setdefault('incoming_relations',[]).append({'source_uuid':row['uuid'],'source_name':row['title'],**rel})
            if row['uuid'] not in other['related_ids']:other['related_ids'].append(row['uuid'])
    types={}
    for key,n in d.nodes.items():
        types[key]={'id':key,'n':n['name'],'major':' / '.join(n['path'][:-1]),'path':n['path'],
                    'resource_type':n['resource_type'],'d':[],'s':[],'p':' / '.join(n['path']),'st':'unknown'}
    for row in rows:
        for key in row['category_ancestors']:
            for field in ('d','s'):
                values=row['domains' if field=='d' else 'stages']
                types[key][field]=list(dict.fromkeys(types[key][field]+values))
    tag_defs=[{'key':k,'id':k,'name':' / '.join(n['path']),'type':'canonical_category','resource_type':n['resource_type']} for k,n in d.nodes.items()]
    tag_defs += [{'key':k,'id':k,**v,'name':canonical_stage(v['name']) if v['type']=='experimental_step' else v['name'],
        'source_ids':[k]+[old for old,new in d.facet_aliases.items() if new==k]}
        for k,v in d.facets.items() if v['type']!='legacy_keyword' and k not in d.facet_aliases]
    if d.scene_plan:
        tag_defs=[t for t in tag_defs if t['type'] not in ('experimental_domain','experimental_scene','experimental_step')]
        for kind,values,prefix in [('experimental_domain',d.scene_plan['domains'],'domain'),
            ('experimental_scene',[r['name'] for r in d.scene_plan['records']],'scene'),
            ('experimental_step',d.scene_plan['stages'],'stage')]:
            tag_defs += [{'key':stable_id(prefix,n),'id':stable_id(prefix,n),'name':n,'type':kind,'source':'scene-index.json'} for n in values]
    for key in entries:index['aliases'].setdefault(key,key)
    index.update(entries=rows,files=files,types=types,tag_definitions=tag_defs,
        facet_aliases=d.facet_aliases,stage_aliases=STAGE_ALIASES,
        dictionaries={'category-index.json':digest((root/'分类/category-index.json').read_bytes()),
                      'manufacturers.json':digest((root/'manufacturers.json').read_bytes()),
                      'facets.json':digest((root/'facets.json').read_bytes())},
        catalog_contract='unilab.public-entry/2')
    if d.scene_plan:
        index['scene_plan']=d.scene_plan
        index['facet_aliases'].update({k:stable_id('stage',canonical_stage(v['name'])) for k,v in d.facets.items() if v['type']=='experimental_step'})
        index['dictionaries']['scene-index.json']=digest((root/'分类/scene-index.json').read_bytes())
    atomic(root/'library.json',encoded(index))
    return {'records':len(rows),'resource_types':dict(Counter(r['resource_type'] for r in rows)),
            'classification':dict(Counter(r['classification_status'] for r in rows))}
