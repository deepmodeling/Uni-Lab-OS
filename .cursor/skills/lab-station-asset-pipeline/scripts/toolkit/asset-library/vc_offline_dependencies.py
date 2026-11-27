"""Recover serialized joint dependencies without executing vendor behaviors.

This is a separate algebra result, not permission to admit incomplete geometry
or a claim that a Follower backed by runtime IK can be reconstructed offline.
"""
from collections import Counter
import math
from vc_offline_expression import evaluate


def analyze(physics):
    records=[r for r in physics['joints_and_drivers'] if r['owner']['kind']=='Dof']
    names=[r['fields'].get('Name',{}).get('value') for r in records]
    counts=Counter(n for n in names if n)
    named={n:r for n,r in zip(names,records) if n and counts[n]==1}
    rows=[];values={};active=set()
    def initial(name):
        if name in values:return values[name]
        if counts[name]>1:raise ValueError('Ambiguous Dof name '+name)
        if name not in named:raise ValueError('Runtime/external driver unavailable: '+name)
        if name in active:raise ValueError('Cyclic joint dependency '+name)
        active.add(name)
        try:
            r=named[name];f=r['fields']
            if 'InitialValue' not in f:raise ValueError('No serialized InitialValue for '+name)
            v=f['InitialValue'];v=evaluate(v['expression'],initial) if v.get('expression') is not None else v.get('value')
            if not isinstance(v,(int,float)):raise ValueError('Non-numeric initial coordinate')
            values[name]=v;return v
        finally:active.remove(name)
    for r,name in zip(records,names):
        f=r['fields'];kind=r['owner']['type'];row={'owner':r['owner'],'name':name,'source_type':kind,'dependencies':[],
            'native_semantics':'not_verified','admission_effect':'none; full geometry and kinematics gates still required'}
        resolving_value=False
        def resolve(key):
            nonlocal resolving_value
            if key=='VALUE':
                if kind.endswith('Follower'):
                    if resolving_value:raise ValueError('Cyclic Follower VALUE driver')
                    driver=f.get('Driver',{});expr=driver.get('expression')
                    resolving_value=True
                    try:
                        if expr is None:
                            pointer=driver.get('value')
                            if not isinstance(pointer,str) or not pointer:raise ValueError('Follower has no explicit driver pointer/expression')
                            return resolve(pointer)
                        return evaluate(expr,resolve)
                    finally:resolving_value=False
                v=f.get('InitialValue')
                if v is None:raise ValueError('No serialized InitialValue for '+str(name))
                return evaluate(v['expression'],initial) if v.get('expression') is not None else v.get('value')
            row['dependencies'].append(key);return initial(key)
        try:
            expression=f.get('ValueExpression',{}).get('expression') or 'VALUE'
            row['value_expression']=expression
            row['evaluated_initial_algebra']=evaluate(expression,resolve)
            if not isinstance(row['evaluated_initial_algebra'],(int,float)) or not math.isfinite(row['evaluated_initial_algebra']):raise ValueError('Initial joint algebra is not a finite scalar')
            row['source_limits']={}
            for field in ('MinLimit','MaxLimit'):
                if field in f:
                    v=f[field];row['source_limits'][field]={'expression':v.get('expression'),'at_initial':evaluate(v['expression'],resolve) if v.get('expression') is not None else v.get('value')}
                    limit=row['source_limits'][field]['at_initial']
                    if not isinstance(limit,(int,float)) or not math.isfinite(limit):raise ValueError('Joint bound algebra is not a finite scalar')
            row['status']='serialized_algebra_recovered'
            if kind.endswith('Follower'):
                row['driver']=f.get('Driver');row['qualification_gap']='Follower transform/unit binding requires native static proof; no independent actuator limits invented'
        except (ValueError,KeyError,TypeError) as exc:row.update(status='unresolved',reason=str(exc))
        row['dependencies']=sorted(set(row['dependencies']));rows.append(row)
    return {'schema':'vc-serialized-joint-dependencies/1','source_sha256':physics['source_sha256'],
        'scope':'Source expression algebra and dependency inventory only; no VC, IK solver, or scripts executed',
        'rows':rows,'counts':dict(Counter(r['status'] for r in rows))}
