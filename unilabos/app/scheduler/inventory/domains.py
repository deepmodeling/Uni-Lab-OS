"""实验室领域包（domain pack）.

「标准实验室操作系统」的领域化入口：同一套仓储/调度/布局内核，
通过 domain pack 描述不同学科实验室（有机化学 / 无机 / 生物 / 材料…）的
物料类别、危险分级、默认分区与常见容器组合。后续新增学科只需注册新 pack，
不动内核代码。

领域选择持久化在 lab_meta['lab_domain']（见 layout.py 的 profile API）。
"""

from __future__ import annotations

from typing import Any, Dict, List

# zone.kind 的通用取值（前端着色 / 图例依据）
ZONE_KINDS = [
    {"kind": "bench", "name": "实验台", "color": "#3b82f6"},
    {"kind": "instrument", "name": "仪器区", "color": "#8b5cf6"},
    {"kind": "storage", "name": "存储区", "color": "#f59e0b"},
    {"kind": "safety", "name": "安全设施", "color": "#ef4444"},
    {"kind": "prep", "name": "预处理区", "color": "#10b981"},
    {"kind": "waste", "name": "废弃物区", "color": "#71717a"},
]

DOMAIN_PACKS: Dict[str, Dict[str, Any]] = {
    "general": {
        "domain": "general",
        "name": "通用实验室",
        "accent": "#2563eb",
        "description": "标准实验室操作系统基座：仓储 / 调度 / 布局通用内核。",
        "material_categories": [
            {"id": "reagent", "name": "试剂", "color": "#3b82f6"},
            {"id": "consumable", "name": "耗材", "color": "#10b981"},
            {"id": "labware", "name": "器皿", "color": "#8b5cf6"},
            {"id": "sample", "name": "样品", "color": "#f59e0b"},
        ],
        "hazard_classes": [
            {"id": "none", "name": "无危害", "color": "#22c55e"},
            {"id": "flammable", "name": "易燃", "color": "#ef4444"},
            {"id": "corrosive", "name": "腐蚀", "color": "#f59e0b"},
            {"id": "toxic", "name": "有毒", "color": "#8b5cf6"},
        ],
        "default_zone_kinds": ["bench", "instrument", "storage", "safety"],
    },
    "organic": {
        "domain": "organic",
        "name": "有机化学实验室",
        "accent": "#f97316",
        "description": "合成 / 分离纯化 / 表征流程：反应釜、旋蒸、柱层析、GC-MS。",
        "material_categories": [
            {"id": "solvent", "name": "溶剂", "color": "#3b82f6"},
            {"id": "reagent", "name": "试剂", "color": "#f97316"},
            {"id": "catalyst", "name": "催化剂", "color": "#8b5cf6"},
            {"id": "substrate", "name": "底物", "color": "#10b981"},
            {"id": "labware", "name": "玻璃器皿", "color": "#71717a"},
        ],
        "hazard_classes": [
            {"id": "flammable", "name": "易燃", "color": "#ef4444"},
            {"id": "peroxide", "name": "易过氧化", "color": "#f59e0b"},
            {"id": "toxic", "name": "有毒", "color": "#8b5cf6"},
            {"id": "corrosive", "name": "腐蚀", "color": "#eab308"},
        ],
        "default_zone_kinds": ["bench", "instrument", "storage", "safety", "waste"],
    },
    "inorganic": {
        "domain": "inorganic",
        "name": "无机化学实验室",
        "accent": "#0ea5e9",
        "description": "无机合成 / 高温处理 / 晶体生长：马弗炉、水热釜、手套箱。",
        "material_categories": [
            {"id": "salt", "name": "无机盐", "color": "#0ea5e9"},
            {"id": "acid_base", "name": "酸碱", "color": "#ef4444"},
            {"id": "metal", "name": "金属/氧化物", "color": "#71717a"},
            {"id": "crucible", "name": "坩埚/耐高温器皿", "color": "#f59e0b"},
        ],
        "hazard_classes": [
            {"id": "corrosive", "name": "强腐蚀", "color": "#ef4444"},
            {"id": "oxidizer", "name": "氧化剂", "color": "#f59e0b"},
            {"id": "high_temp", "name": "高温", "color": "#f97316"},
        ],
        "default_zone_kinds": ["bench", "instrument", "storage", "safety"],
    },
    "bio": {
        "domain": "bio",
        "name": "生物实验室",
        "accent": "#22c55e",
        "description": "细胞培养 / 分子生物学 / 高通量：生物安全柜、培养箱、移液工作站。",
        "material_categories": [
            {"id": "cell_line", "name": "细胞系", "color": "#22c55e"},
            {"id": "medium", "name": "培养基", "color": "#f59e0b"},
            {"id": "enzyme", "name": "酶/抗体", "color": "#8b5cf6"},
            {"id": "plate", "name": "板/管耗材", "color": "#3b82f6"},
            {"id": "primer", "name": "引物/核酸", "color": "#ec4899"},
        ],
        "hazard_classes": [
            {"id": "bsl1", "name": "BSL-1", "color": "#22c55e"},
            {"id": "bsl2", "name": "BSL-2", "color": "#f59e0b"},
            {"id": "cold_chain", "name": "冷链", "color": "#0ea5e9"},
        ],
        "default_zone_kinds": ["bench", "instrument", "storage", "safety", "prep"],
    },
    "materials": {
        "domain": "materials",
        "name": "材料实验室",
        "accent": "#8b5cf6",
        "description": "薄膜 / 粉体 / 器件制备与表征：镀膜机、球磨机、XRD、SEM。",
        "material_categories": [
            {"id": "powder", "name": "粉体", "color": "#8b5cf6"},
            {"id": "target", "name": "靶材", "color": "#71717a"},
            {"id": "substrate", "name": "基片", "color": "#3b82f6"},
            {"id": "precursor", "name": "前驱体", "color": "#f59e0b"},
        ],
        "hazard_classes": [
            {"id": "nano", "name": "纳米粉尘", "color": "#f59e0b"},
            {"id": "flammable", "name": "易燃", "color": "#ef4444"},
            {"id": "inert_gas", "name": "惰性气氛", "color": "#0ea5e9"},
        ],
        "default_zone_kinds": ["bench", "instrument", "storage", "prep"],
    },
}


def get_domain_pack(domain: str) -> Dict[str, Any]:
    return DOMAIN_PACKS.get(domain, DOMAIN_PACKS["general"])


def list_domain_packs() -> List[Dict[str, Any]]:
    return list(DOMAIN_PACKS.values())
