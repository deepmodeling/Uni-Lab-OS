"""Resolve exact named visual presets from explicit static XML dependencies.

No installed-path defaults, application calls or historical captures. Preset
values describe rendering, never contact materials, density or manufacturer BOM.
"""
import hashlib
import math
from pathlib import Path
import xml.etree.ElementTree as ET


def load_catalog(path):
    path = Path(path).resolve()
    raw = path.read_bytes()
    if len(raw) > 8 * 1024 * 1024 or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('Unsupported material XML size or entity declaration')
    root = ET.fromstring(raw)
    entries = {}
    for node in root.findall('Material'):
        name = node.get('Name')
        if not name or name in entries:
            raise ValueError('Missing or duplicate exact material name')
        attrs = {}
        for target, source in [('roughness', 'Roughness'), ('metallic', 'Metallic'),
                               ('opacity', 'Opacity'), ('clearcoat', 'Clearcoat'),
                               ('clearcoatRoughness', 'ClearcoatRoughness'), ('ior', 'IndexOfRefraction')]:
            if source in node.attrib:
                value = float(node.get(source))
                if not math.isfinite(value):
                    raise ValueError('Nonfinite material value')
                attrs[target] = value
        keys = ['BaseColor.X', 'BaseColor.Y', 'BaseColor.Z']
        if all(k in node.attrib for k in keys):
            attrs['diffuseColor'] = [float(node.get(k)) for k in keys]
        if not all(math.isfinite(v) for v in attrs.get('diffuseColor', [])):
            raise ValueError('Nonfinite material color')
        textures = {c.tag: c.text for c in node if c.text and c.text.strip()}
        entries[name] = {'id': name, 'name': name, 'usd_preview_inputs': attrs,
                         'source_kind': 'explicit_external_static_preset',
                         'source': str(path), 'source_sha256': hashlib.sha256(raw).hexdigest(),
                         'selector': 'Material[@Name='+repr(name)+']',
                         'source_shader': node.get('ShaderName'), 'source_attributes': dict(node.attrib),
                         'unresolved_texture_references': textures,
                         'scope': 'selected static preset values; not proof of legacy renderer selection or contact physics'}
    return {'source': str(path), 'sha256': hashlib.sha256(raw).hexdigest(), 'entries': entries}


def resolve(name, archive_materials, catalogs):
    """Resolve a typed archive reference, never merge 3DS-local namespaces.

    3DS face groups use ``member#material:name`` exact IDs. Bare feature/node
    references use the component material table, then explicitly supplied
    presets. The archive definition is authoritative for this portable export;
    this does not reproduce a running application's prior material registry.
    """
    exact_id = [m for m in archive_materials if m.get('id') == name]
    scoped = '#material:' in str(name)
    component = [m for m in archive_materials if m.get('member') == 'materials.dat'
                 or (not m.get('member') and '#material:' not in str(m.get('id', '')))]
    # RSC rMaterialVariable::ReadValue passes flag 0 to FindMaterial: exact
    # wchar comparison. The Python API's different default must not leak here.
    # Static rule: Revolution.dll bcb4985e..., ReadValue 0x1800ca010,
    # FindMaterial 0x1802fc590 -> 0x1802fc4e0 -> 0x180b43a40 -> 0x18006c4b0.
    same_name = [m for m in component if m.get('name') == name]
    matches = exact_id if scoped else (exact_id or same_name)
    rule = 'exact_member_scoped_id' if scoped else 'component_table_unique_exact_name'
    if len(matches) > 1:
        return {'id': name, 'status': 'unresolved', 'reason': 'ambiguous_archive_material_name',
                'candidates': [m['id'] for m in matches], 'resolution_rule': rule}
    if matches:
        row = dict(matches[0]); attrs = dict(row.get('usd_preview_inputs', {}))
        if row.get('diffuse') is not None:
            attrs['diffuseColor'] = list(row['diffuse'])[:3]
        if row.get('opacity') is not None:
            value = row['opacity']
            attrs['opacity'] = float(value[0] if isinstance(value, list) else value)
        elif row.get('transparency_percent') is not None:
            attrs['opacity'] = 1-float(row['transparency_percent'])/100
        row.update(status='partial_archive_values' if row.get('unresolved_texture_references') else 'resolved_archive_values', usd_preview_inputs=attrs,
                   requested_reference=name, resolution_rule=rule,
                   scope='serialized diffuse/opacity only; unspecified PBR channels not reconstructed')
        return row
    if scoped:
        return {'id': name, 'status': 'unresolved', 'reason': 'scoped_material_definition_missing',
                'resolution_rule': rule}
    matches = [m for c in catalogs for key,m in c['entries'].items() if key == name]
    if len(matches) == 1:
        row = dict(matches[0]); row['status'] = 'resolved_external_preset'
        row.update(requested_reference=name, resolution_rule='explicit_presets_unique_exact_name')
        if row['unresolved_texture_references']:
            row['status'] = 'partial_external_preset'
        return row
    return {'id': name, 'status': 'unresolved',
            'reason': 'ambiguous_external_material' if matches else 'referenced_material_definition_not_in_inputs'}
