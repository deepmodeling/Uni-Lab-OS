"""读取固定工程包：按原生 NVIDIA 清单与 BOM 校验后返回 unilab.json。"""
from pathlib import Path

from ..asset_fetch.native_package import ADAPTER, read_json
from ..asset_fetch.native_package import verify_package as verify_native_package


def verify_package(root, expected):
    """校验包清单摘要、元数据与 BOM 全部条目；任一漂移即失败，不允许项目内副本静默改动。

    参数：root 为工程包目录，expected 为锁定的 com.nvidia.simready.packaging.json 摘要。
    返回：simready_usd/unilab.json 内容。
    """
    verify_native_package(Path(root), expected)
    return read_json(Path(root) / ADAPTER)
