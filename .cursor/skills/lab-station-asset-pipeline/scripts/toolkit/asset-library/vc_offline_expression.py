"""Small, non-executing VC expression interpreter. No eval or VC dependencies.

Numbers use source units (mm/degrees); matrices are row-vector affine matrices.
Only whitelisted functions/operators are accepted. Resolver controls scope and
can record dependencies. Unknown names, units, calls and attributes fail closed.
"""
import ast,math,re
import numpy as np

class UnresolvedExpression(ValueError):pass

def _ternary(s):
    # Rewrite conditional syntax only, respecting opaque literal strings and
    # argument/group nesting. Execution remains a bounded AST whitelist.
    tokens=list(re.finditer(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[()]|.',s,re.S))
    out=[];i=0
    while i<len(tokens):
        token=tokens[i]
        if token[0]=='(':
            depth=1;j=i+1
            while j<len(tokens) and depth:
                if tokens[j][0]=='(':depth+=1
                elif tokens[j][0]==')':depth-=1
                j+=1
            if depth:raise UnresolvedExpression('Unbalanced expression group')
            out.append('('+_ternary(s[token.end():tokens[j-1].start()])+')');i=j
        elif token[0]==')':raise UnresolvedExpression('Unbalanced expression group')
        else:out.append(token[0]);i+=1
    s=''.join(out);depth=0;question=None;nested=0;commas=[]
    for token in re.finditer(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|.',s,re.S):
        c=token[0];i=token.start()
        if c=='(':depth+=1
        elif c==')':depth-=1
        elif c==',' and depth==0:commas.append(i)
    if commas:
        bounds=[-1,*commas,len(s)];return ','.join(_ternary(s[bounds[i]+1:bounds[i+1]]) for i in range(len(bounds)-1))
    for token in re.finditer(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|.',s,re.S):
        c=token[0];i=token.start()
        if c=='(':depth+=1
        elif c==')':depth-=1
        elif c=='?' and depth==0:
            if question is None:question=i
            else:nested+=1
        elif c==':' and depth==0 and question is not None:
            if nested:nested-=1
            else:return '('+_ternary(s[question+1:i])+' if '+_ternary(s[:question])+' else '+_ternary(s[i+1:])+')'
    if question is not None:raise UnresolvedExpression('Incomplete conditional expression')
    return s

def _transform(name,args):
    m=np.eye(4)
    if name=='Vector' and len(args)==3:return np.array(args,dtype=float)
    if name=='Matrix' and not args:return m
    if name in ('Tx','Ty','Tz','Rx','Ry','Rz','Sx','Sy','Sz') and len(args)==1:
        axis='xyz'.index(name[1].lower());v=float(args[0])
        if name[0]=='T':m[3,axis]=v
        elif name[0]=='S':m[axis,axis]=v
        else:
            a,b=(axis+1)%3,(axis+2)%3;c=math.cos(math.radians(v));s=math.sin(math.radians(v));m[a,a]=m[b,b]=c;m[a,b]=s;m[b,a]=-s
        return m
    raise UnresolvedExpression('Unknown function or arity: '+name)

def evaluate(expression,resolver):
    if not isinstance(expression,str):return expression
    expression=expression.strip()
    if not expression:return np.eye(4)
    # VC property namespaces are identifiers, not Python attributes or code.
    # Translate only outside literal strings; resolve their exact original key.
    aliases={}
    pattern=re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|[A-Za-z_]\w*(?:::[A-Za-z_]\w*)+')
    def namespace(match):
        value=match[0]
        if value.startswith(('"',"'")):return value
        key='__vc_property_'+str(len(aliases));aliases[key]=value;return key
    expression=pattern.sub(namespace,expression)
    units=re.findall(r'\{([^{}]+)\}',expression)
    if any(u.strip() not in ('mm','deg') for u in units):raise UnresolvedExpression('Unsupported unit annotation: '+repr(units))
    expression=re.sub(r'\{(?:mm|deg)\}','',expression).strip()
    expression=_ternary(expression).replace('&&',' and ').replace('||',' or ')
    expression=re.sub(r'!(?!=)',' not ',expression).strip()
    try:tree=ast.parse(expression,mode='eval')
    except SyntaxError as e:raise UnresolvedExpression(str(e)) from e
    if sum(1 for _ in ast.walk(tree))>256:raise UnresolvedExpression('Expression complexity limit')
    def visit(n):
        if isinstance(n,ast.Constant) and isinstance(n.value,(int,float,str,bool)):return n.value
        if isinstance(n,ast.Name):
            if n.id in ('True','true'):return True
            if n.id in ('False','false'):return False
            if n.id in ('PI','pi'):return math.pi
            return resolver(aliases.get(n.id,n.id))
        if isinstance(n,ast.IfExp):return visit(n.body if visit(n.test) else n.orelse)
        if isinstance(n,ast.UnaryOp):
            v=visit(n.operand)
            if isinstance(n.op,ast.USub):return -v
            if isinstance(n.op,ast.UAdd):return +v
            if isinstance(n.op,ast.Not):return not v
        if isinstance(n,ast.BoolOp):
            if isinstance(n.op,ast.And):return all(visit(v) for v in n.values)
            if isinstance(n.op,ast.Or):return any(visit(v) for v in n.values)
        if isinstance(n,ast.Compare):
            vals=[visit(n.left)]+[visit(v) for v in n.comparators]
            ops={ast.Eq:lambda a,b:a==b,ast.NotEq:lambda a,b:a!=b,ast.Lt:lambda a,b:a<b,ast.LtE:lambda a,b:a<=b,ast.Gt:lambda a,b:a>b,ast.GtE:lambda a,b:a>=b}
            return all(ops[type(o)](vals[i],vals[i+1]) for i,o in enumerate(n.ops))
        if isinstance(n,ast.BinOp):
            a,b=visit(n.left),visit(n.right)
            if isinstance(n.op,ast.Add):return a+b
            if isinstance(n.op,ast.Sub):return a-b
            if isinstance(n.op,ast.Mult):return a@b if isinstance(a,np.ndarray) and isinstance(b,np.ndarray) and a.shape==b.shape==(4,4) else a*b
            if isinstance(n.op,ast.Div):return a/b
            if isinstance(n.op,ast.Mod):return a%b
            if isinstance(n.op,ast.Pow) and abs(float(b))<=8:return a**b
        if isinstance(n,ast.Call) and not n.keywords:
            args=[visit(v) for v in n.args]
            if isinstance(n.func,ast.Name):
                if n.func.id=='int' and len(args)==1:
                    value=args[0]
                    if not isinstance(value,(int,float,bool)) or not math.isfinite(value) or not -2147483648<=value<2147483648:raise UnresolvedExpression('VC int cast outside finite signed32 range')
                    return int(value)  # registered double->int 0x18035AE20: CVTTSD2SI
                trig={'sin':math.sin,'cos':math.cos,'tan':math.tan,'asin':math.asin,'acos':math.acos,'atan':math.atan,'atan2':math.atan2}
                name=n.func.id;degree=name.endswith('d') and name[:-1] in trig;base=name[:-1] if degree else name
                if base in trig:
                    expected=2 if base=='atan2' else 1
                    if len(args)!=expected or any(not isinstance(x,(int,float)) for x in args):raise UnresolvedExpression('Invalid trigonometric arguments')
                    if degree and base in ('sin','cos','tan'):args=[math.radians(x) for x in args]
                    value=trig[base](*args)
                    return math.degrees(value) if degree and base not in ('sin','cos','tan') else value
                if n.func.id in ('min','max','abs','floor','ceil','sqrt'):
                    return {'min':min,'max':max,'abs':abs,'floor':math.floor,'ceil':math.ceil,'sqrt':math.sqrt}[n.func.id](*args)
                return _transform(n.func.id,args)
            if isinstance(n.func,ast.Attribute) and n.func.attr in ('Tx','Ty','Tz','Rx','Ry','Rz','Sx','Sy','Sz'):
                base=visit(n.func.value)
                if isinstance(base,np.ndarray) and base.shape==(4,4):return _transform(n.func.attr,args)@base
        raise UnresolvedExpression('Unsupported syntax: '+ast.dump(n))
    try:result=visit(tree.body)
    except (KeyError,TypeError,ZeroDivisionError,OverflowError) as e:raise UnresolvedExpression(str(e)) from e
    if isinstance(result,(float,int,np.ndarray)) and not np.isfinite(result).all():raise UnresolvedExpression('Non-finite result')
    return result
