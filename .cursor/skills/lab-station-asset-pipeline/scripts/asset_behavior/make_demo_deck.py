"""用 PLR 的耗材定义生成演示资源图（OS 图节点格式）：台面原点在设备原点，耗材放在给定的原点（毫米）。
这是演示台面，不是现场布局；用来验证「资源树算点 → 资产把工具点送过去」。
用法：python make_demo_deck.py 输出.json tips:名字:x,y,z plate:名字:x,y,z ...
"""
import json, sys
from pylabrobot.resources import TipSpot, Well, Cor_96_wellplate_360ul_Fb
from pylabrobot.resources.hamilton import hamilton_96_tiprack_300uL_filter
nodes = [{'id': 'deck', 'name': 'deck', 'parent': None, 'type': 'deck', 'position': {'x': 0, 'y': 0, 'z': 0}, 'config': {}}]
def walk(r, parent, origin=None):
    loc = r.location; pos = origin or {'x': loc.x, 'y': loc.y, 'z': loc.z}
    cfg = {'type': type(r).__name__, 'size_x': r.get_size_x(), 'size_y': r.get_size_y(), 'size_z': r.get_size_z(), 'rotation': {'x': 0, 'y': 0, 'z': 0}}
    if isinstance(r, TipSpot):
        t = r.make_tip(); cfg['prototype_tip'] = {'total_tip_length': t.total_tip_length, 'fitting_depth': t.fitting_depth}
    if isinstance(r, Well): cfg['material_z_thickness'] = getattr(r, 'material_z_thickness', None)
    nodes.append({'id': r.name, 'name': r.name, 'parent': parent, 'type': 'resource', 'position': pos, 'config': cfg})
    for c in r.children: walk(c, r.name)
for item in sys.argv[2:]:
    kind, name, xyz = item.split(':'); x, y, z = (float(v) for v in xyz.split(','))
    r = hamilton_96_tiprack_300uL_filter(name=name) if kind == 'tips' else Cor_96_wellplate_360ul_Fb(name=name)
    walk(r, 'deck', {'x': x, 'y': y, 'z': z})
json.dump({'nodes': nodes}, open(sys.argv[1], 'w'), indent=0); print(len(nodes), 'nodes ->', sys.argv[1])
