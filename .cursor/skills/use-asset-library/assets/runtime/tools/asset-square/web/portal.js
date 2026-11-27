/* Uses the supplied HTML's classes and styles without an override stylesheet. */
const $=id=>document.getElementById(id);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const Q=new URLSearchParams(location.search), tagPage=location.pathname==='/tags.html', typePage=location.pathname==='/types.html';
const square='all';
const topicsPage=Q.get('view')==='topics';
const state=Object.fromEntries(['manufacturer','major','domain','stage','scene','source','tag','status','q','type','resource_type','classification_status','related_to','has_3d_model','has_os_module','topic'].map(k=>[k,Q.get(k)||'']));
if(!Q.has('resource_type'))state.resource_type=location.pathname.startsWith('/consumables')?'resource':(Q.get('square')==='all'||topicsPage?'':'device');
const panorama=()=>state.resource_type==='device'&&!topicsPage;
CatalogNavigation.normalize(state);
let page=Number(Q.get('page'))||1, snapshot, ticket=0, timer;
const expanded=new Set();
let filterNotice="";
const STATUS={verified:'已验证（原网页声明）',compatible:'规范兼容（原网页声明）',registered:'历史登记 · 待核对',asset:'工程资产',candidate:'工程候选 · 按用途查看验证',unconnected:'未接入'};
const RESOURCE_LABELS={component:'零件',mechanism:'模组',device:'设备',resource:'耗材与资源',workcell:'工站',tool:'工具'};
const NS={canonical_category:'规范分类',device_template_tag:'设备类型',experimental_domain:'实验范式',experimental_step:'实验阶段',experimental_scene:'实验场景'};
const COLOR={'生物':'#1fa88d','化学':'#6986df','环境':'#55a18e','材料':'#d48767','物理':'#8a6fd1'};
function url(path,extra={},reset=false){const p=new URLSearchParams({square,...(topicsPage?{view:'topics'}:{}),...(reset?{}:state),...extra});for(const[k,v]of [...p])if(!v)p.delete(k);return path+'?'+p;}
async function get(path){const r=await fetch(path);const d=await r.json();if(!r.ok)throw Error(d.error||'读取失败');return d;}
function options(key,values,label){if(state[key]&&!values.some(v=>(typeof v==='string'?v:v.value)===state[key]))values=[{value:state[key],text:state[key]+'（当前条件无结果）'},...values];return `<label class="select"><span>${label}</span><select data-key="${key}" aria-label="${label}"><option value="">全部</option>${values.map(v=>{const value=typeof v==='string'?v:v.value,text=typeof v==='string'?v:v.text;return `<option value="${esc(value)}" ${state[key]===value?'selected':''}>${esc(text)}</option>`;}).join('')}</select></label>`;}
function controls(){
  const filters=snapshot.filter_options||snapshot;
  const types=filters.types.filter(t=>t.count>0);
  const row1=panorama()?options('domain',filters.facets.domain,'学科')+options('scene',filters.facets.scene,'场景')+options('stage',filters.facets.stage,'阶段'):'';
  const row2=options('major',filters.facets.major,'大类')+options('type',types.map(t=>({value:t.id||t.n,text:(t.path||[t.n]).join(' / ')+` (${t.count})`})),'分类')+options('manufacturer',filters.facets.manufacturer,'厂家');
  const row3=options('has_3d_model',[{value:'true',text:'含 3D 模型'},{value:'false',text:'不含 3D 模型'}],'3D 模型')+options('has_os_module',[{value:'true',text:'有 OS module'},{value:'false',text:'无 OS module'}],'OS module');
  const more=options('classification_status',[{value:'mapped',text:'已映射'},{value:'unresolved',text:'待补分类'}],'分类状态')+options('source',filters.facets.source,'来源')+options('tag',filters.tags.filter(t=>t.count>0).map(t=>({value:t.key,text:`${t.label} · ${t.name} (${t.count})`})),'分类 tag');
  const hasMore=['classification_status','source','tag'].some(k=>state[k]);
  return `<div class="portal-filter-row" data-filter-row="1">${row1}</div><div class="portal-filter-row" data-filter-row="2">${row2}</div><div class="portal-filter-row" data-filter-row="3">${row3}</div><div class="portal-filter-actions"><label class="search"><input id="portalSearch" type="search" aria-label="搜索设备与资产" placeholder="搜索厂家、型号、设备类型或 tag" value="${esc(state.q)}"></label><button class="btn" id="clearPortal">清除筛选</button>${state.has_3d_model?`<a class="btn" href="${esc(url(location.pathname,{has_3d_model:'',page:'1'}))}">清除 3D 条件</a>`:''}<a class="btn" href="${esc(url('/types.html',{page:'1'}))}">查看匹配条目</a></div><details class="portal-more" ${hasMore?'open':''}><summary>更多筛选</summary><div class="portal-filter-row">${more}</div></details>`;
}
function installControls(){document.querySelectorAll('[data-key]').forEach(el=>el.addEventListener('change',()=>{filterNotice='';state[el.dataset.key]=el.value;if(el.dataset.key==='domain')state.scene='';if(el.dataset.key==='major')state.type='';if(el.dataset.key==='type')state.major='';page=1;refresh();}));$('clearPortal')?.addEventListener('click',()=>{filterNotice='';for(const k in state)if(k!=='resource_type')state[k]='';page=1;refresh();});$('portalSearch')?.addEventListener('input',e=>{clearTimeout(timer);const value=e.target.value;timer=setTimeout(()=>{state.q=value;page=1;refresh();},240);});}
function bar(values,field){const max=Math.max(...values.map(x=>x.count),1);return values.map(v=>`<a class="bar" href="${esc(url('/',{[field]:v.name},false))}"><span class="lbl">${esc(v.name)}</span><span class="trk"><b style="width:${100*v.count/max}%"></b></span><span class="num">${v.count}</span></a>`).join('');}
function overview(){
  $('portalTitle').textContent='设备场景全景';document.title=$('portalTitle').textContent+' · Uni-Lab';
  $('portalMeta').textContent=`原网页 + 历史表格 + 工程资产库 · ${snapshot.total.toLocaleString()} 个条目 · ${snapshot.types.length} 个类型 · ${snapshot.tags.filter(t=>t.count>0).length} 个当前分类与场景标签`;
  $('internalStrip').hidden=false;$('internalStrip').textContent='内部视图 · 已合并确认重复的网页与表格条目；来源统计含交集，不可相加';
  $('scopeN').textContent=snapshot.total.toLocaleString();$('scopeList').innerHTML=Object.entries(snapshot.matched_source_counts||snapshot.counts).map(([n,c])=>`<div><i></i><span>${esc(n)} <b>${c}</b> 条</span></div>`).join('')+`<div><i></i><span>分类 tag <b>${snapshot.tags.filter(t=>t.count>0).length}</b> 个，当前筛选下有关联资产</span></div>`;
  const matrix=snapshot.matrix||[], typeById=new Map(snapshot.types.map(t=>[t.id||t.n,t]));
  const countTypes=cells=>new Set(cells.flatMap(c=>c.types.map(t=>t.id))).size;
  const domains=snapshot.domains.filter(n=>matrix.some(c=>c.domain===n)).map(name=>({name,count:countTypes(matrix.filter(c=>c.domain===name))}));
  const stages=snapshot.stages.map(name=>({name,count:countTypes(matrix.filter(c=>c.stage===name))}));
  $('domN').textContent=domains.length;$('stgN').textContent=stages.length;$('domBars').innerHTML=bar(domains,'domain');$('stgBars').innerHTML=bar(stages,'stage');
  $('portalFilters').innerHTML=controls();
  const selections=[state.domain&&`学科：${state.domain}`,state.major&&`大类：${snapshot.facets.major.find(v=>v.value===state.major)?.text||state.major}`,state.has_os_module&&`OS module：${state.has_os_module==='true'?'有':'无'}`,state.stage&&`阶段：${state.stage}`,state.scene&&`场景：${state.scene}`,state.has_3d_model&&`3D：${state.has_3d_model==='true'?'含模型':'不含模型'}`].filter(Boolean);
  $('portalCount').textContent=`${filterNotice}${selections.length?selections.join('；')+'。':'按学科／大类、阶段和场景筛选设备类型。'}匹配 ${snapshot.matched_type_count||0} 个类型、${snapshot.total.toLocaleString()} 个条目；点击矩阵类型查看对应条目。${snapshot.matched_unclassified?`其中 ${snapshot.matched_unclassified} 条待补分类，可点“查看匹配条目”。`:''}`;
  const filtered=Object.values(state).some(Boolean),stagesShown=state.stage?[state.stage]:snapshot.stages;
  const head=`<thead><tr><th class="corner"><span class="c-stage">阶段 →</span><span class="c-dom">学科 / 大类 ↓</span></th>${stagesShown.map((s,i)=>`<th class="col"><em>${String(i+1).padStart(2,'0')}</em>${esc(s)}</th>`).join('')}</tr></thead>`;
  const body=domains.filter(d=>!state.domain||d.name===state.domain).map(({name:d,count})=>`<tr><th class="dom"><i style="background:${COLOR[d]||'var(--bh-color-primary)'}"></i>${esc(d)}<small>${count} 个类型</small></th>${stagesShown.map(s=>{
    const all=(matrix.find(c=>c.domain===d&&c.stage===s)?.types||[]).map(c=>({...typeById.get(c.id),count:c.count}));
    const key=d+'|'+s,open=filtered||expanded.has(key),shown=open?all:all.slice(0,15);
    return `<td>${shown.map(t=>`<a class="chip ${t.st==='verified'?'verified':t.st==='compatible'?'compatible':'unknown'}" href="${esc(url('/types.html',{type:t.id||t.n,domain:d,stage:s,page:'1'}))}" title="${esc(t.major)} · ${t.count} 个条目">${esc(t.n)} · ${t.count}</a>`).join('')}${all.length>15&&!filtered?`<button class="chip more" data-cell="${esc(key)}">${open?'收起 ▴':`还有 ${all.length-15} 种 ▾`}</button>`:''}</td>`;
  }).join('')}</tr>`).join('');
  $('table').innerHTML=`<table class="grid">${head}<tbody>${body||'<tr><td>没有匹配的条目，请清除筛选。</td></tr>'}</tbody></table>`;installControls();document.querySelectorAll('[data-cell]').forEach(el=>el.onclick=()=>{const k=el.dataset.cell;expanded.has(k)?expanded.delete(k):expanded.add(k);overview();});
}
function card(r){const color=r.status==='verified'?'verified':r.status==='compatible'?'compatible':'unconnected';const detail='/api/portal/record?id='+encodeURIComponent(r.id);
  return `<div class="pcard ${color}"><div class="photo">${r.image?`<img src="${esc(r.image)}" alt="${esc(r.title)}" loading="lazy"><span class="slot">${esc(r.image_note||'来源图片')}</span>`:'<span class="slot">来源未提供图片</span>'}</div><div class="pbody"><div class="vendor">${esc(r.manufacturer)}</div><div class="model">${esc(r.title)}</div><div class="use">${esc(r.description.slice(0,450))}${r.description.length>450?'…':''}</div><div class="tags"><i class="pd">${esc((r.sources||[r.source]).join(' + '))}</i><i class="pd">${esc(RESOURCE_LABELS[r.resource_type]||'')}</i><i class="pd">${esc(STATUS[r.status]||r.status)}</i>${r.has_os_module?'<i class="pd">有 OS module</i>':''}${r.has_3d_model?`<i class="pd">${r.model_3d_status==='external_reference'?'3D 外部引用（未验证）':'含 3D 模型'}</i>`:''}${r.has_cad?'<i class="pd">CAD 入口</i>':''}${r.simulation?`<i class="pd">${esc(r.simulation)}</i>`:''}</div>${r.evidence?`<div class="use">原验证依据：${esc(r.evidence)}</div>`:''}${r.module?`<div class="use">驱动登记：${esc(r.module)}</div>`:''}${r.classification_status==='unresolved'?'<div class="use">待补分类：保留原始标签与映射记录</div>':''}${r.relations?.length?`<div class="use">关联资产：${r.relations.map(x=>`<a href="${esc(url('/types.html',{q:x.target_uuid,square:'all'},true))}">${esc(x.target_name||x.target_uuid)}</a> · ${esc({related:'关联（未证明兼容）',accessory_for:'配件适用于',compatible_with:'兼容'}[x.type])}`).join('；')}</div>`:''}${r.related_ids?.length?`<a class="btn" href="${esc(url('/types.html',{related_to:r.id,square:'all'},true))}">关联与被关联条目</a>`:''}${r.parse_errors?.length?`<div class="use">导出字段不完整：${esc(r.parse_errors.join('、'))}</div>`:''}<div class="use">${(r.tag_labels||r.tags).slice(0,12).map(t=>esc(t)).join(' · ')}</div>${snapshot.catalog_mode==='unified'?`<a class="btn" href="/api/catalog/entry?id=${encodeURIComponent(r.id)}" target="_blank" rel="noopener">统一条目字段</a> `:''}<a class="btn" href="${esc(detail)}" target="_blank" rel="noopener">原始字段与来源</a>${r.asset_id?` <a class="btn" href="${snapshot.catalog_mode==='unified'?'/api/catalog/reference?id='+encodeURIComponent(r.id):'/api/reference?id='+encodeURIComponent(r.asset_id)}" target="_blank" rel="noopener">固定资产引用</a>`:''}</div></div>`;
}
async function typeView(myticket){
  const data=await get(url('/api/portal/entries',{page:String(page)}));if(myticket!==ticket)return;page=data.page;
  const t=snapshot.types.find(t=>(t.id||t.n)===state.type||t.n===state.type);$('back').href=url(square==='consumables'?'/consumables':'/',{type:'',page:''});$('crumbName').textContent=t?.n||state.type||'全部条目';$('internalStrip').hidden=false;$('internalStrip').textContent='内部视图 · 原网页验证声明、历史登记与工程资产分别保留';document.title=(t?.n||state.type||'全部条目')+' · 设备与资产总览';
  $('page').innerHTML=`<div class="head"><div><h1>${esc(t?.n||state.type||'全部条目')}</h1><div class="meta">${esc(snapshot.facets.major.find?.(v=>v.value===state.major)?.text||state.major||t?.major||'全部分类')} · ${data.total.toLocaleString()} 个条目</div><div class="kv"><span>学科</span><div>${esc(t?.d.join(' · ')||'按条目来源')}</div><span>实验阶段</span><div>${esc(t?.s.join(' · ')||'按条目来源')}</div><span>用途</span><div>${esc(state.major==='自动化机械与物流'?'机构与工站设计用工程资产':t?.p||'统一检索')}</div></div></div></div><div class="toolbar" id="portalFilters">${controls()}</div><section><div class="sec-head"><h2 class="sec">${square==='consumables'?'工件与耗材':'设备与资产条目'} <small>匹配 ${data.total} 条</small></h2></div><div class="wall">${data.entries.map(card).join('')}</div>${!data.total?'<div class="none">没有匹配的条目，请调整筛选。</div>':''}</section><div class="embed-pager"><button class="btn" id="prevPortal" ${page<=1?'disabled':''}>上一页</button><span>${page} / ${data.pages}</span><button class="btn" id="nextPortal" ${page>=data.pages?'disabled':''}>下一页</button></div>`;
  $('prevPortal').onclick=()=>{page--;refresh();};$('nextPortal').onclick=()=>{page++;refresh();};installControls();
  document.querySelectorAll('.photo img').forEach(img=>img.addEventListener('error',()=>{img.hidden=true;img.nextElementSibling.textContent='来源图片不可用';},{once:true}));
}
function directory(){
  $('portalMeta').textContent=`当前显示 ${snapshot.tags.filter(t=>t.count>0).length} 个分类与场景标签；已接入确认分类树。原始标签及映射保留在来源中。仅显示当前筛选下有资产的分类。`;
  $('portalFilters').innerHTML=`<label class="search"><input id="tagSearch" placeholder="搜索分类或 tag" aria-label="搜索分类 tag"></label>`;
  function draw(q=''){
    $('tagDirectory').innerHTML=Object.entries(NS).map(([kind,label])=>{const rows=snapshot.tags.filter(t=>t.count>0&&t.type===kind&&t.name.toLowerCase().includes(q.toLowerCase()));return `<h2>${label} · ${rows.length}</h2><div class="table"><table class="grid"><thead><tr><th>分类 / tag</th><th>源 ID</th><th>当前广场关联条目</th></tr></thead><tbody>${rows.map(t=>`<tr><td><a class="chip unknown" href="${esc(url('/types.html',{tag:t.key},true))}">${esc(t.name)}</a></td><td>${esc(t.id)}</td><td>${t.count}</td></tr>`).join('')}</tbody></table></div>`;}).join('')+`<h2>全部设备类型 · ${snapshot.types.length}</h2><div class="scope-list">${snapshot.types.filter(t=>t.count>0&&t.n.toLowerCase().includes(q.toLowerCase())).map(t=>`<a href="${esc(url('/types.html',{type:t.id||t.n},true))}">${esc(t.major)} / ${esc(t.n)} (${t.total})</a>`).join('')}</div>`;
  }draw();$('tagSearch').oninput=e=>draw(e.target.value);
}
async function refresh(){
  const stageAliases={'备料&前处理设备':'备料&前处理','实验执行&合成设备':'实验执行&合成','后处理设备':'后处理','器件/细胞设备':'器件/细胞','表征设备':'表征'};
  state.stage=stageAliases[state.stage]||state.stage;
  CatalogNavigation.normalize(state);
  const current=++ticket;history.replaceState(null,'',url(location.pathname,{page:String(page)}));
  try{snapshot=await get(url('/api/portal/summary'));if(current!==ticket)return;navigation();if(topicsPage)topicView();else if(!panorama())await catalogView(current);else if(tagPage)directory();else if(typePage)await typeView(current);else overview();}
  catch(e){const node=$('page')||$('tagDirectory')||$('table');node.textContent='读取失败：'+e.message;}
}
function navigation(){
  const top=document.querySelector('.wrap > .toolbar');
  top.classList.add('asset-sections');top.setAttribute('aria-label','资产类型');
  const tabs=[['device','设备'],['component','零部件'],['mechanism','模组'],['resource','耗材与资源'],['workcell','工站'],['tool','工具'],['','全部资产']];
  top.innerHTML=tabs.map(([kind,label])=>`<a class="btn ${!topicsPage&&state.resource_type===kind?'selected':''}" ${!topicsPage&&state.resource_type===kind?'aria-current="page"':''} href="/?square=all&resource_type=${kind}">${label}${snapshot.resource_type_counts?.[kind]!=null?` <small>${snapshot.resource_type_counts[kind]}</small>`:''}</a>`).join('')+`<a class="btn ${topicsPage?'selected':''}" href="/?square=all&view=topics">专题</a><a class="btn" href="/setup.html">首次使用 / OSS 权限</a>`;
}
function catalogHost(title){
  document.title=title+' · Uni-Lab';
  $('internalStrip').hidden=true;
  if($('portalTitle')){
    $('portalTitle').textContent=title;
    $('portalMeta').textContent='按当前分类逐级浏览，保留来源、固定版本与模型入口。';
    document.querySelector('.scope').hidden=true;
    $('map').textContent='资产分类目录';
    $('portalFilters').innerHTML=controls();
    $('portalCount').textContent=`${state.topic?'专题：'+state.topic+' · ':''}匹配 ${snapshot.total.toLocaleString()} 个条目。只显示当前条件下有资产的分类。`;
    $('table').classList.add('catalog-surface');
    return $('table');
  }
  if($('page')){
    $('back').href='/?square=all&resource_type='+encodeURIComponent(state.resource_type);
    $('crumbName').textContent=title;
    $('page').innerHTML=`<h1>${esc(title)}</h1><div id="portalFilters" class="toolbar">${controls()}</div><div id="catalogContent"></div>`;
    return $('catalogContent');
  }
  $('portalFilters').innerHTML=controls();return $('tagDirectory');
}
function categoryLink(node,label){
  return node.id?`<a href="${esc(url('/',{type:node.id,major:'',page:'1'}))}" ${state.type===node.id?'aria-current="page"':''}>${esc(label||node.name)}${node.count!=null?` <small>${node.count}</small>`:''}</a>`:`<span>${esc(label||node.name)}</span>`;
}
async function catalogView(myticket){
  const data=await get(url('/api/portal/entries',{page:String(page)}));if(myticket!==ticket)return;page=data.page;
  const title=(RESOURCE_LABELS[state.resource_type]||'全部资产')+'目录';
  const host=catalogHost(title),tree=CatalogNavigation.tree((snapshot.filter_options||snapshot).types),selected=tree.nodes.find(n=>n.id===state.type);
  const under=(path,prefix)=>prefix.every((v,i)=>path[i]===v);
  function branch(nodes){return nodes.map(n=>n.children.length?`<details ${n.path.length===1||selected&&under(selected.path,n.path)?'open':''}><summary>${categoryLink(n)}</summary><div class="category-children">${branch(n.children)}</div></details>`:`<div class="category-leaf">${categoryLink(n)}</div>`).join('');}
  const scope=snapshot.filter_options?.category_scope||{total:snapshot.total,classified:snapshot.total-snapshot.matched_unclassified,unclassified:snapshot.matched_unclassified};
  const crumbs=selected?selected.path.map((_,i)=>tree.nodes.find(n=>JSON.stringify(n.path)===JSON.stringify(selected.path.slice(0,i+1)))).filter(Boolean):[];
  const children=selected?selected.children:(tree.roots.length===1&&tree.roots[0].children.length?tree.roots[0].children:tree.roots);
  host.innerHTML=`<div class="catalog-layout"><aside class="category-tree" aria-label="多层分类"><h3>分类目录</h3><a class="category-root" href="${esc(url('/',{type:'',major:'',page:'1'}))}">全部 ${scope.total}</a><p class="classification-counts">已分类 ${scope.classified} · <a href="${esc(url('/',{type:'',major:'',classification_status:'unresolved',page:'1'}))}">待补分类 ${scope.unclassified}</a></p>${branch(tree.roots)||'<p>当前筛选没有分类</p>'}</aside><main class="catalog-results"><nav class="category-breadcrumb" aria-label="分类路径"><a href="${esc(url('/',{type:'',major:'',page:'1'}))}">全部分类</a>${crumbs.map(n=>' / '+categoryLink(n)).join('')}</nav>${state.topic?`<div class="topic-selection">专题：${esc(state.topic)} <a href="${esc(url('/',{topic:'',page:'1'}))}">退出专题</a></div>`:''}<div class="category-folders">${children.map(n=>`<div class="category-folder"><span aria-hidden="true">▱</span>${categoryLink(n)}${n.children.length?`<small>${n.children.length} 个下级分类</small>`:''}</div>`).join('')}</div><div class="catalog-result-count">${selected?esc(selected.path.join(' / ')):'当前资产'} · ${data.total.toLocaleString()} 个条目${snapshot.matched_unclassified?` · ${snapshot.matched_unclassified} 条待补分类`:''}</div><div class="wall">${data.entries.map(card).join('')}</div>${data.total?'':'<p class="none">没有匹配的条目，请调整筛选。</p>'}<div class="embed-pager"><button class="btn" id="prevPortal" ${page<=1?'disabled':''}>上一页</button><span>${page} / ${data.pages}</span><button class="btn" id="nextPortal" ${page>=data.pages?'disabled':''}>下一页</button></div></main></div>`;
  $('prevPortal').onclick=()=>{page--;refresh();};$('nextPortal').onclick=()=>{page++;refresh();};installControls();
}
function topicView(){
  const host=catalogHost('当前资产专题');
  if($('map'))$('map').textContent='专题索引';
  host.innerHTML=`<p>按当前条目的项目归属和场景关联组织；专题只保存检索入口，不复制资产，也不恢复历史集合包。</p><div class="topic-grid">${(snapshot.topics||[]).map(t=>`<a class="topic-card" href="/?square=all&resource_type=&topic=${encodeURIComponent(t.name)}"><strong>${esc(t.name)}</strong><span>${t.count} 个当前条目</span><small>进入分类目录 →</small></a>`).join('')||'<p>当前条件下没有专题。</p>'}</div>`;
  installControls();
}
refresh();
