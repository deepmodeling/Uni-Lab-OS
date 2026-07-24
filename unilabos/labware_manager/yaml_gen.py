"""JSON → Registry YAML 文件生成。

按 type 分组输出到对应 YAML 文件（与现有格式完全一致）。
"""

from __future__ import annotations

import shutil
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

import yaml

from unilabos.labware_manager.models import LabwareDB, LabwareItem

_REGISTRY_DIR = Path(__file__).resolve().parents[1] / "registry" / "resources" / "prcxi"

# type → yaml 文件名
_TYPE_TO_YAML = {
    "plate": "plates",
    "tip_rack": "tip_racks",
    "trash": "trash",
    "tube_rack": "tube_racks",
    "plate_adapter": "plate_adapters",
}


def _build_entry(item: LabwareItem) -> dict:
    """构建单个 YAML 条目（与现有格式完全一致）。"""
    mi = item.material_info
    desc = item.registry_description
    if not desc:
        desc = f'{mi.Name} (Code: {mi.Code})' if mi.Name and mi.Code else item.function_name

    return {
        "category": list(item.registry_category),
        "class": {
            "module": f"unilabos.devices.liquid_handling.prcxi.prcxi_labware:{item.function_name}",
            "type": "pylabrobot",
        },
        "description": desc,
        "handles": [],
        "icon": "",
        "init_param_schema": {},
        "version": "1.0.0",
    }


class _YAMLDumper(yaml.SafeDumper):
    """自定义 Dumper: 空列表输出为 []，空字典输出为 {}。"""
    pass


def _represent_list(dumper, data):
    if not data:
        return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=True)
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data)


def _represent_dict(dumper, data):
    if not data:
        return dumper.represent_mapping("tag:yaml.org,2002:map", data, flow_style=True)
    return dumper.represent_mapping("tag:yaml.org,2002:map", data)


def _represent_str(dumper, data):
    if '\n' in data or ':' in data or "'" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="'")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


_YAMLDumper.add_representer(list, _represent_list)
_YAMLDumper.add_representer(dict, _represent_dict)
_YAMLDumper.add_representer(str, _represent_str)


def generate_yaml(db: LabwareDB, test_mode: bool = True) -> List[Path]:
    """生成所有 registry YAML 文件，返回输出文件路径列表。"""
    suffix = "_test" if test_mode else ""

    # 按 type 分组
    groups: Dict[str, Dict[str, dict]] = defaultdict(dict)
    for item in db.items:
        yaml_key = _TYPE_TO_YAML.get(item.type)
        if yaml_key is None:
            continue
        groups[yaml_key][item.function_name] = _build_entry(item)

    out_paths: List[Path] = []
    for yaml_key, entries in groups.items():
        out_path = _REGISTRY_DIR / f"{yaml_key}{suffix}.yaml"

        # 备份
        if out_path.exists():
            bak = out_path.with_suffix(".yaml.bak")
            shutil.copy2(out_path, bak)

        # 按函数名排序
        sorted_entries = dict(sorted(entries.items()))

        content = yaml.dump(sorted_entries, Dumper=_YAMLDumper, allow_unicode=True,
                           default_flow_style=False, sort_keys=False)
        out_path.write_text(content, encoding="utf-8")
        out_paths.append(out_path)

    return out_paths


if __name__ == "__main__":
    from unilabos.labware_manager.importer import load_db
    db = load_db()
    if not db.items:
        print("labware_db.json 为空，请先运行 importer.py")
    else:
        paths = generate_yaml(db, test_mode=True)
        print(f"已生成 {len(paths)} 个 YAML 文件:")
        for p in paths:
            print(f"  {p}")
