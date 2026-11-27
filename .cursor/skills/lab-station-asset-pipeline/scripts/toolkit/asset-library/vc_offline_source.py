"""Non-executing RSC lexer: quoted scripts are opaque, braces are structural."""
import bisect,json,re

KINDS='Node|Feature|Functionality|Variable|Joint|Driver|Dof|Connection|Section|TemplateSection|rSim[A-Za-z0-9_]*Field|BaseFrame|Action|SingleMode|BatchMode|TableMode|DistributionMode|ProductType|Output|Input|SimAttribute|Proxy|InternalProxy'
QUOTED=re.compile(r'"(?:\\.|[^"\\])*"',re.S)
TOKEN=re.compile(r'"(?:\\.|[^"\\])*"|[{}]|[^\s{}"]+',re.S)

def qdecode(value):return json.loads(re.sub(r'\\\r?\n','',value),strict=False)

def blocks(text):
    # Replace complete quoted spans before finding declarations. A quoted script
    # can contain fake Node/Feature declarations and braces on physical newlines.
    masked=list(text)
    for q in QUOTED.finditer(text):
        masked[q.start():q.end()]=['\n' if c=='\n' else ' ' for c in q[0]]
    masked=''.join(masked);declaration=re.compile(r'(?m)^[ \t]*('+KINDS+r')\b(?:[ \t]+[0-9]+)?\s*\{')
    # One structural pass avoids re-scanning every descendant for each owner.
    # Preserve the same offsets/line identities and quoted-string masking.
    closes={};brace_stack=[]
    for token in re.finditer('[{}]',masked):
        if token[0]=='{':brace_stack.append(token.start())
        elif brace_stack:closes[brace_stack.pop()]=token.start()
        else:raise ValueError('Unbalanced source closing brace')
    if brace_stack:raise ValueError('Unbalanced source block')
    newlines=[m.start() for m in re.finditer('\n',text)]
    result=[]
    for m in declaration.finditer(masked):
        opening=m.end()-1;pos=closes[opening]
        kind_end=m.start(1)+len(m[1]);typed=QUOTED.search(text,kind_end,opening)
        result.append({'kind':m[1],'type':qdecode(typed[0]) if typed else None,'start':m.start(),'end':pos+1,
                       'body_start':opening+1,'text':text[opening+1:pos],'line':bisect.bisect_left(newlines,m.start())+1})
    stack=[]
    for block in result:
        while stack and stack[-1]['end']<block['end']:stack.pop()
        block['_parent']=stack[-1] if stack else None;block['_children']=[]
        if stack:stack[-1]['_children'].append(block)
        stack.append(block)
    return result

def parent(block,all_blocks=None,kind=None):
    item=block.get('_parent')
    while item is not None and kind is not None and item['kind']!=kind:item=item.get('_parent')
    return item

def direct_text(block):
    text=block['text'];offset=block['body_start']
    for child in reversed(block['_children']):
        a=child['start']-offset;b=child['end']-offset
        text=text[:a]+''.join('\n' if x=='\n' else ' ' for x in text[a:b])+text[b:]
    return text

def top_fields(text):
    """Top-level key/value spans only; nested enum Step values cannot win."""
    tokens=list(TOKEN.finditer(text));result={};i=0
    while i<len(tokens):
        key=tokens[i];word=key[0]
        if word in ('{','}') or word.startswith('"'):raise ValueError('Invalid top-level RSC key '+word[:40])
        i+=1
        if i>=len(tokens):break
        value=tokens[i]
        # Empty fields (e.g. NodeClass) are followed by a brace, not next key.
        if value[0]=='{':
            depth=1;j=i+1
            while j<len(tokens) and depth:
                if tokens[j][0]=='{':depth+=1
                elif tokens[j][0]=='}':depth-=1
                j+=1
            if depth:raise ValueError('Unbalanced field '+word)
            raw=text[value.start():tokens[j-1].end()];i=j
        elif value[0].startswith('"'):
            raw=value[0];i+=1
            # Quantity may have an extra quoted class on the same line.
            while i<len(tokens) and '\n' not in text[tokens[i-1].end():tokens[i].start()] and tokens[i][0].startswith('"'):i+=1
            # Quantity "Distance" "VectorQuantity" may be followed by a
            # serialized custom-unit definition block; it is metadata of this
            # same field, not a second top-level key or the property's Value.
            if i<len(tokens) and tokens[i][0]=='{':
                depth=1;i+=1
                while i<len(tokens) and depth:
                    if tokens[i][0]=='{':depth+=1
                    elif tokens[i][0]=='}':depth-=1
                    i+=1
                if depth:raise ValueError('Unbalanced quoted field metadata')
        else:
            end=text.find('\n',value.start());end=len(text) if end<0 else end
            raw=text[value.start():end].strip()
            while i<len(tokens) and tokens[i].start()<end:i+=1
        if word in result:raise ValueError('Duplicate top-level field '+word)
        result[word]={'raw':raw,'offset':key.start()}
    return result

def decode_scalar(raw):
    if raw.startswith('"'):return qdecode(raw)
    try:return json.loads(raw)
    except ValueError:
        try:return [float(x) for x in raw.split()]
        except ValueError:return raw

def props(block,all_blocks=None):
    if '_offline_props' in block:return block['_offline_props']
    result={}
    for var in block['_children']:
        if var['kind']!='Variable':continue
        fields=top_fields(var['text'])
        if 'Name' not in fields:continue
        name=decode_scalar(fields['Name']['raw']);raw=fields.get('Value',{}).get('raw');value=None;expression=None
        if raw is not None:
            if raw.startswith('{'):
                body=raw[1:-1];ts=list(TOKEN.finditer(body))
                if ts and ts[0][0]=='Expression':expression=decode_scalar(top_fields(body)['Expression']['raw'])
                elif ts and all(t[0].startswith('"') for t in ts):value=[qdecode(t[0]) for t in ts]
                elif body.strip():value={'unresolved_source_list':body.strip()}
                else:value=[]
            else:value=decode_scalar(raw)
        row={'value':value,'expression':expression,'line':var['line'],'type':var['type']}
        for key in ('Quantity','Magnitude'):
            if key in fields:row[key.lower()]=decode_scalar(fields[key]['raw'])
        if name in result:raise ValueError('Duplicate owner property '+str(name))
        result[name]=row
    block['_offline_props']=result
    return result

def _name(block,bs=None):
    if block['kind']=='Node':
        match=re.search(r'(?m)^[ \t]*Name[ \t]+("(?:\\.|[^"\\])*")',direct_text(block))
        return qdecode(match[1]) if match else None
    return props(block,bs).get('Name',{}).get('value')
