"""Bounded source Custom Dof normalization; no source code or VC execution."""
import re

def single_axis_custom(fields):
    name=fields.get('Name',{}).get('value');expression=fields.get('Joint',{}).get('expression')
    if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z_]\w*',name) or not isinstance(expression,str):raise ValueError('Custom Dof lacks explicit Name/Joint expression')
    pivot=fields.get('Pivot',{})
    if pivot.get('expression') not in ('','Matrix()'):raise ValueError('Custom Dof nonidentity/unresolved Pivot is outside proven subset')
    match=re.fullmatch(r'\s*([RT])([xyz])\s*\(\s*([+-]?)\s*'+re.escape(name)+r'\s*\)\s*',expression)
    if not match:raise ValueError('Custom Dof Joint is not a single unit-gain named-axis transform')
    kind='Rotational' if match[1]=='R' else 'Translational';axis='xyz'.index(match[2])+(4 if match[3]=='-' else 0)
    return {'source_type':'Custom','effective_type':kind,'axis_type':axis,'source_joint_expression':expression,'source_pivot_expression':pivot.get('expression'),'units':'degrees' if kind=='Rotational' else 'mm','unit_basis':'explicit Rx/Ry/Rz degree or Tx/Ty/Tz source-millimetre transform semantics; not manufacturer data','rule':'Revolution.dll bcb4985e... InitCustomVars0x1809688C0, GetLinkJointMatrix0x180968810; identity Pivot gives exact Joint expression; Matrix default0x18037EA90'}
