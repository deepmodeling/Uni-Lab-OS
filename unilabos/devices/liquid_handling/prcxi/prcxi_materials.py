import collections
import json
from pathlib import Path

from unilabos.devices.liquid_handling.prcxi.prcxi import PRCXI9300Container


prcxi_materials_path = str(Path(__file__).parent / "prcxi_material.json")
with open(prcxi_materials_path, mode="r", encoding="utf-8") as f:
    prcxi_materials = json.loads(f.read())


def tip_adaptor_1250ul(name="Tip头适配器 1250uL") -> PRCXI9300Container:  # 必须传入一个name参数，是plr的规范要求
    # tip_rack = PRCXI9300Container(name, prcxi_materials["name"]["Height"])
    tip_rack = PRCXI9300Container(name, 1000,400,800, "tip_rack", collections.OrderedDict())
    tip_rack.load_state({
        "Materials": {"uuid": "7960f49ddfe9448abadda89bd1556936", "materialEnum": "0"}
    })
    return tip_rack


