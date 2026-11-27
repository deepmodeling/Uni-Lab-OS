/* Navigation rules shared by the page and regression checks. */
(function(root){
  function normalize(state){
    if(state.resource_type!=='device')for(const k of ['domain','scene','stage'])state[k]='';
    return state;
  }
  function tree(types){
    const nodes=new Map();
    for(const t of types.filter(t=>t.count>0)){
      const path=t.path?.length?t.path:[t.n];
      for(let i=1;i<=path.length;i++){
        const part=path.slice(0,i),key=JSON.stringify(part);
        if(!nodes.has(key))nodes.set(key,{key,path:part,name:part.at(-1),children:[]});
        if(i===path.length)Object.assign(nodes.get(key),{id:t.id||t.n,count:t.count});
      }
    }
    const roots=[];
    for(const n of nodes.values()){
      const parent=nodes.get(JSON.stringify(n.path.slice(0,-1)));
      (parent?parent.children:roots).push(n);
    }
    const sort=items=>{items.sort((a,b)=>a.name.localeCompare(b.name,'zh-CN'));items.forEach(n=>sort(n.children));};
    sort(roots);return {roots,nodes:[...nodes.values()]};
  }
  const api={normalize,tree};
  if(typeof module!=='undefined')module.exports=api;else root.CatalogNavigation=api;
})(typeof window==='undefined'?globalThis:window);
