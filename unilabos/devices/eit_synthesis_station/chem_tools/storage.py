# -*- coding: utf-8 -*-
"""
功能:
    管理 chem_tools 下 ChemicalBook 缓存, 原始 HTML 与结构化记录目录.
    同时负责将旧的 eit_synthesis_station/data/chemicalbook_* 数据迁移到新目录.
参数:
    无.
返回:
    无.
"""

import logging
import shutil
from pathlib import Path


logger = logging.getLogger("ChemToolsStorage")

MODULE_ROOT = Path(__file__).resolve().parent
DATA_ROOT = MODULE_ROOT / "data"
CHEMICALBOOK_CACHE_ROOT = DATA_ROOT / "chemicalbook_cache"
CHEMICALBOOK_RAW_ROOT = DATA_ROOT / "chemicalbook_raw"
CHEMICALBOOK_RECORD_ROOT = DATA_ROOT / "chemicalbook_records"

LEGACY_DATA_ROOT = MODULE_ROOT.parent / "data"
LEGACY_CHEMICALBOOK_CACHE_ROOT = LEGACY_DATA_ROOT / "chemicalbook_cache"
LEGACY_CHEMICALBOOK_RAW_ROOT = LEGACY_DATA_ROOT / "chemicalbook_raw"
LEGACY_CHEMICALBOOK_RECORD_ROOT = LEGACY_DATA_ROOT / "chemicalbook_records"

_MIGRATION_DONE = False


def ensure_chemicalbook_data_layout() -> None:
    """
    功能:
        确保 chem_tools 化学查询数据目录存在, 并将旧目录中的 ChemicalBook 数据迁移到新目录.
        若目标文件已存在, 保留新目录文件并跳过覆盖.
    参数:
        无.
    返回:
        无.
    """
    global _MIGRATION_DONE

    if _MIGRATION_DONE is True:
        return

    for target_root in (
        CHEMICALBOOK_CACHE_ROOT,
        CHEMICALBOOK_RAW_ROOT,
        CHEMICALBOOK_RECORD_ROOT,
    ):
        target_root.mkdir(parents=True, exist_ok=True)

    _move_legacy_tree(LEGACY_CHEMICALBOOK_CACHE_ROOT, CHEMICALBOOK_CACHE_ROOT)
    _move_legacy_tree(LEGACY_CHEMICALBOOK_RAW_ROOT, CHEMICALBOOK_RAW_ROOT)
    _move_legacy_tree(LEGACY_CHEMICALBOOK_RECORD_ROOT, CHEMICALBOOK_RECORD_ROOT)

    _MIGRATION_DONE = True


def _move_legacy_tree(source_root: Path, target_root: Path) -> None:
    """
    功能:
        将旧目录下的文件迁移到新目录, 仅处理普通文件, 并保留目标目录已有文件.
    参数:
        source_root: Path, 旧目录根路径.
        target_root: Path, 新目录根路径.
    返回:
        无.
    """
    if source_root.exists() is False:
        return

    target_root.mkdir(parents=True, exist_ok=True)

    for source_path in sorted(source_root.rglob("*")):
        if source_path.is_file() is False:
            continue

        relative_path = source_path.relative_to(source_root)
        target_path = target_root / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)

        if target_path.exists() is True:
            logger.warning(
                "化学查询迁移跳过重复文件, 保留新目录文件: src=%s, dst=%s",
                source_path,
                target_path,
            )
            try:
                source_path.unlink()
            except OSError as exc:
                logger.warning("化学查询迁移删除旧重复文件失败: path=%s, err=%s", source_path, exc)
            continue

        shutil.move(str(source_path), str(target_path))

    _remove_empty_dirs(source_root)


def _remove_empty_dirs(root_path: Path) -> None:
    """
    功能:
        删除迁移后遗留的空目录, 不影响非空目录内容.
    参数:
        root_path: Path, 待清理目录.
    返回:
        无.
    """
    if root_path.exists() is False:
        return

    for directory in sorted(root_path.rglob("*"), reverse=True):
        if directory.is_dir() is False:
            continue
        try:
            directory.rmdir()
        except OSError:
            continue

    try:
        root_path.rmdir()
    except OSError:
        pass
