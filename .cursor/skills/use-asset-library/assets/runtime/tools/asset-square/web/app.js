const $ = id => document.getElementById(id);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const square = location.pathname.startsWith('/consumables') ? 'consumables' : 'equipment';
const consumables = square === 'consumables';
const initial = new URLSearchParams(location.search);
const filters = Object.fromEntries(['manufacturer','major','category','scene','q'].map(k=>[k,initial.get(k)||'']));
let page = Number(initial.get('page')) || 1, response, request = 0, detailRequest = 0, timer;
const displayName = consumables ? '耗材广场' : '设备广场';
$('title').textContent=displayName; document.title=displayName+' · Uni-Lab';
$(consumables?'consumablesNav':'equipmentNav').classList.add('active');
$('mapTitle').textContent=consumables?'耗材分类地图':'设备分类地图';
$('listTitle').textContent=consumables?'全部工件与耗材':'全部设备资产';
if(consumables)$('intro').textContent='查找瓶管、孔板、移液耗材和设计仿真所用工件。';
async function get(url){const r=await fetch(url);const data=await r.json();if(!r.ok)throw Error(data.error||'读取失败');return data;}
function barMarkup(values,key){const selected=filters[key];const sorted=[...values].sort((a,b)=>b.count-a.count).slice(0,5);const max=sorted[0]?.count||1;return sorted.map(v=>`<button class="bar ${selected===v.name?'is-cur':''}" data-filter="${key}" data-value="${esc(v.name)}" title="${esc(v.name)}"><span class="lbl">${esc(v.name)}</span><span class="trk"><b style="width:${100*v.count/max}%"></b></span><span class="num">${v.count}</span></button>`).join('')||'<span class="sub">暂无关联专题</span>';}
function picture(r){return `<div class="asset-picture">${r.thumbnail?`<img loading="lazy" src="${esc(r.thumbnail)}" alt="${esc(r.title)} 资产预览"><span class="placeholder" hidden>暂无当前版本预览</span>`:'<span class="placeholder">暂无当前版本预览</span>'}</div>`;}
function imageFallbacks(root){root.querySelectorAll('img').forEach(img=>img.addEventListener('error',()=>{img.hidden=true;img.nextElementSibling.hidden=false;},{once:true}));}
function render(data){
  response=data;page=data.page;
  $('snapshot').textContent=`索引更新 ${new Date(data.snapshot.updated).toLocaleString('zh-CN')} · 设备 ${data.counts.equipment||0} · 工件与耗材 ${data.counts.consumables||0}`;
  $('scopeN').textContent=data.square_count.toLocaleString();$('makerN').textContent=data.facets.manufacturer.length;$('sceneN').textContent=data.facets.scene.length;
  $('scopeList').innerHTML=`<div><i></i><span>${consumables?'工件与耗材':'设备、机构与零部件'} <b>${data.square_count}</b></span></div><div><i></i><span>浏览小类 <b>${data.facets.category.length}</b></span></div><div><i></i><span>有 CAD 入口 <b>${data.cad_count}</b></span></div>`;
  $('makerBars').innerHTML=barMarkup(data.facets.manufacturer,'manufacturer');
  const shortScenes=data.facets.scene.filter(v=>v.name.length<=10 && !v.name.includes('_'));
  $('sceneBars').innerHTML=barMarkup(shortScenes.length?shortScenes:data.facets.scene,'scene');
  $('categoryMap').innerHTML=data.groups.map(g=>`<div class="map-row"><div class="map-major">${esc(g.major)}<small>${g.categories.reduce((n,c)=>n+c.count,0)} 个条目</small></div><div class="map-cells">${g.categories.map(c=>`<button class="chip compatible ${filters.category===c.name?'selected':''}" data-filter="category" data-value="${esc(c.name)}" data-major="${esc(g.major)}"><span>${esc(c.name)}</span><b>${c.count}</b></button>`).join('')}</div></div>`).join('');
  for(const key of ['manufacturer','major','category','scene']){
    const values=key==='category' && filters.major ? data.groups.find(g=>g.major===filters.major)?.categories||[] : data.facets[key];
    $(key).innerHTML='<option value="">全部</option>'+values.map(v=>`<option value="${esc(v.name)}">${esc(v.name)} (${v.count})</option>`).join('');
    if(filters[key] && !values.some(v=>v.name===filters[key]))$(key).add(new Option(filters[key]+'（无匹配）',filters[key]));
    $(key).value=filters[key];
  }
  $('q').value=filters.q;$('resultCount').textContent=`找到 ${data.total.toLocaleString()} 个条目`;
  $('assetGrid').innerHTML=data.entries.map(r=>`<button class="asset-card" data-id="${esc(r.id)}" aria-label="查看 ${esc(r.title)}">${picture(r)}<div class="card-body"><div class="vendor">${esc(r.manufacturer)}</div><div class="card-title">${esc(r.title)}</div><div class="badges"><span class="badge primary">${esc(r.category)}</span>${r.has_cad?'<span class="badge">CAD 入口</span>':''}${r.classification_review?'<span class="badge review">分类待核对</span>':''}</div></div></button>`).join('');
  imageFallbacks($('assetGrid'));$('pageLabel').textContent=`${page} / ${data.pages}`;$('prev').disabled=page<=1;$('next').disabled=page>=data.pages;
  $('message').hidden=data.total>0;$('message').textContent='没有匹配的资产，请调整或清除筛选。';
}
async function load(){
  const ticket=++request;$('assetGrid').setAttribute('aria-busy','true');
  const args=new URLSearchParams({square,page:String(page),...filters});
  const address=new URLSearchParams(Object.entries({...filters,page:String(page)}).filter(([k,v])=>v && !(k==='page'&&v==='1')));
  history.replaceState(null,'',location.pathname+(address.size?'?'+address:''));
  try{const data=await get('/api/assets?'+args);if(ticket===request)render(data);}
  catch(e){if(ticket===request){$('message').hidden=false;$('message').textContent='索引读取失败：'+e.message;$('assetGrid').innerHTML='';$('resultCount').textContent='';}}
  finally{if(ticket===request)$('assetGrid').setAttribute('aria-busy','false');}
}
function setFilter(key,value,major){filters[key]=value;if(key==='major')filters.category='';if(major)filters.major=major;page=1;load();}
document.addEventListener('click',e=>{const chip=e.target.closest('[data-filter]');if(chip)setFilter(chip.dataset.filter,chip.dataset.value,chip.dataset.major);const card=e.target.closest('[data-id]');if(card)openDetail(card.dataset.id);});
$('filters').addEventListener('submit',e=>e.preventDefault());
for(const key of ['manufacturer','major','category','scene'])$(key).addEventListener('change',()=>setFilter(key,$(key).value));
$('q').addEventListener('input',()=>{clearTimeout(timer);const value=$('q').value;timer=setTimeout(()=>setFilter('q',value),220);});
$('clear').addEventListener('click',()=>{clearTimeout(timer);for(const key in filters)filters[key]='';page=1;load();});
$('refresh').addEventListener('click',load);
for(const [id,delta] of [['prev',-1],['next',1]])$(id).addEventListener('click',()=>{page+=delta;load();$('listTitle').scrollIntoView({behavior:'smooth',block:'start'});});
$('closeDetail').addEventListener('click',()=>{$('detail').close();++detailRequest;});
$('detail').addEventListener('click',e=>{if(e.target===$('detail')){const b=$('detail').getBoundingClientRect();if(e.clientX<b.left||e.clientX>b.right||e.clientY<b.top||e.clientY>b.bottom)$('detail').close();}});
async function openDetail(id,version){
  const ticket=++detailRequest;if(!$('detail').open)$('detail').showModal();$('detailTitle').textContent='资产详情';$('detailBody').textContent='正在读取固定版本…';
  try{
    const args=new URLSearchParams({id});if(version)args.set('version',version);const d=await get('/api/asset?'+args);if(ticket!==detailRequest)return;
    const r=d.record,t=d.target;const card=response.entries.find(x=>x.id===id);$('detailTitle').textContent=r.title;
    const pairs=[['厂家',r.manufacturer.name],['型号',r.model.value],['配置',t.configuration||r.configuration],['复用类别',card?.reuse_label||r.reuse_kind],['目录小类',card?.category||''],['场景与专题',card?.scenes.join('、')||'未关联'],['包内成员',t.member?'是，坐标与来源装配关联':'否'],['版本摘要',t.package_digest||'缺少固定摘要'],['本地包路径',d.package_root]];
    $('detailBody').innerHTML=`<div class="detail-summary">${picture({...card,title:r.title,thumbnail:version&&version!==r.targets[0].package_digest?null:card?.thumbnail})}<div><dl class="detail-kv">${pairs.map(([k,v])=>`<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl></div></div><p class="detail-description">${esc(card?.summary||r.summary||'')}</p><div class="detail-actions"><label>版本 <select id="versionSelect">${r.targets.map((x,i)=>`<option value="${esc(x.package_digest)}" ${x.package_digest===t.package_digest?'selected':''}>${i===0?'当前默认':'可选版本'} · ${esc(x.package_digest?.slice(0,12))}</option>`).join('')}</select></label><button class="btn btn-primary" id="copyRef">复制固定资产引用</button><a class="btn" href="/api/reference?${esc(args.toString())}" target="_blank" rel="noopener">查看引用 JSON</a></div><p class="detail-note">以下为资产包已保存的元数据与验收记录。广场读取不会执行新的仿真或完整包校验。</p>${[['编目字段',r],['工程、装配与行为字段',d.metadata.adapter||{}],['仿真与验收记录',d.metadata.readiness||{}]].map(([title,obj])=>`<details><summary>${esc(title)}</summary><pre>${esc(JSON.stringify(obj,null,2))}</pre></details>`).join('')}`;
    imageFallbacks($('detailBody'));$('versionSelect').addEventListener('change',()=>openDetail(id,$('versionSelect').value));
    $('copyRef').addEventListener('click',async()=>{try{await navigator.clipboard.writeText(JSON.stringify(d.reference,null,2));$('copyRef').textContent='已复制';}catch{$('copyRef').textContent='请从引用 JSON 复制';}});
  }catch(e){if(ticket===detailRequest)$('detailBody').textContent='读取失败：'+e.message;}
}
load();
