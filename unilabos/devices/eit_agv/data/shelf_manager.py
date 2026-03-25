# coding:utf-8
"""
功能:
    货架物料状态管理器, 负责追踪货架12个槽位的物料占用情况.
    使用 JSON 文件持久化, 支持放置/取走/查询空位等操作.
"""

import copy
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 货架全部槽位定义(3层 x 4列 = 12个槽位)
SHELF_SLOT_NAMES: List[str] = [
    "shelf_tray_1-1", "shelf_tray_1-2", "shelf_tray_1-3", "shelf_tray_1-4",
    "shelf_tray_2-1", "shelf_tray_2-2", "shelf_tray_2-3", "shelf_tray_2-4",
    "shelf_tray_3-1", "shelf_tray_3-2", "shelf_tray_3-3", "shelf_tray_3-4",
]


class ShelfManager:
    """
    功能:
        货架槽位物料状态管理器.
        每个槽位可以为空或存放一种物料, 物料信息包含类型、来源、时间戳等.
        状态持久化到 shelf_status.json 文件.
    """

    def __init__(self, data_dir: Optional[Path] = None):
        """
        功能:
            初始化货架管理器, 加载或创建 shelf_status.json
        参数:
            data_dir: 数据存储目录, 默认为本文件所在目录
        """
        if data_dir is None:
            data_dir = Path(__file__).parent
        self.data_dir = Path(data_dir)
        self.status_file = self.data_dir / "shelf_status.json"

        self._ensure_directory()
        self._status = self._load_status()

    def _ensure_directory(self) -> None:
        """确保数据目录存在"""
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _load_status(self) -> Dict[str, Any]:
        """
        功能:
            从 JSON 文件加载货架状态, 不存在则初始化全空
        返回:
            Dict, 包含 last_updated 和 slots 的状态字典
        """
        if self.status_file.exists():
            try:
                with self.status_file.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                # 兼容处理: 确保所有槽位都存在
                slots = data.get("slots", {})
                for slot_name in SHELF_SLOT_NAMES:
                    if slot_name not in slots:
                        slots[slot_name] = None
                data["slots"] = slots
                logger.debug("已从 %s 加载货架状态", self.status_file)
                return data
            except Exception as e:
                logger.error("加载货架状态失败: %s, 将初始化为全空", e)

        # 初始化全空状态
        return self._create_empty_status()

    def _create_empty_status(self) -> Dict[str, Any]:
        """
        功能:
            创建全空的货架状态
        返回:
            Dict, 全部槽位为 null 的状态字典
        """
        return {
            "last_updated": datetime.now().isoformat(),
            "slots": {name: None for name in SHELF_SLOT_NAMES},
        }

    def _save_status(self) -> None:
        """
        功能:
            将当前状态持久化到 shelf_status.json
        """
        self._status["last_updated"] = datetime.now().isoformat()
        try:
            with self.status_file.open("w", encoding="utf-8") as f:
                json.dump(self._status, f, ensure_ascii=False, indent=2)
            logger.debug("货架状态已保存到 %s", self.status_file)
        except Exception as e:
            logger.error("保存货架状态失败: %s", e)

    def place_material(
        self,
        slot_name: str,
        material_type: str,
        source: str,
        description: str = "",
    ) -> bool:
        """
        功能:
            在指定槽位记录放置物料
        参数:
            slot_name: 槽位名称, 例如 "shelf_tray_1-1"
            material_type: 物料类型标识, 例如 "FLASH_FILTER_OUTER_BOTTLE_TRAY"
            source: 物料来源位置, 例如 "analysis_station_tray_1-2"
            description: 物料描述说明
        返回:
            bool, True 表示成功, False 表示失败(槽位不存在或已占用)
        """
        if slot_name not in SHELF_SLOT_NAMES:
            logger.error("无效的槽位名称: %s", slot_name)
            return False

        if self._status["slots"][slot_name] is not None:
            logger.warning("槽位 %s 已有物料, 拒绝覆写", slot_name)
            return False

        self._status["slots"][slot_name] = {
            "material_type": material_type,
            "source": source,
            "description": description,
            "placed_at": datetime.now().isoformat(),
        }
        self._save_status()
        logger.info("已在槽位 %s 记录物料: 类型=%s, 来源=%s", slot_name, material_type, source)
        return True

    def remove_material(self, slot_name: str) -> bool:
        """
        功能:
            清除指定槽位的物料记录(表示物料已被取走)
        参数:
            slot_name: 槽位名称
        返回:
            bool, True 表示成功, False 表示槽位不存在或本身为空
        """
        if slot_name not in SHELF_SLOT_NAMES:
            logger.error("无效的槽位名称: %s", slot_name)
            return False

        if self._status["slots"][slot_name] is None:
            logger.warning("槽位 %s 已经为空, 无需清除", slot_name)
            return False

        old_info = self._status["slots"][slot_name]
        self._status["slots"][slot_name] = None
        self._save_status()
        logger.info("已清除槽位 %s 的物料记录 (原物料类型: %s)", slot_name, old_info.get("material_type", "未知"))
        return True

    def find_empty_slots(self, count: int = 1) -> List[str]:
        """
        功能:
            按固定顺序查找空闲槽位
        参数:
            count: 需要的空位数量, 默认为1
        返回:
            List[str], 空闲槽位名称列表, 可能少于 count(空位不足时)
        """
        empty_slots = []
        for slot_name in SHELF_SLOT_NAMES:
            if self._status["slots"][slot_name] is None:
                empty_slots.append(slot_name)
                if len(empty_slots) >= count:
                    break

        if len(empty_slots) < count:
            logger.warning(
                "空闲槽位不足: 需要 %d 个, 仅找到 %d 个", count, len(empty_slots)
            )

        return empty_slots

    def get_slot_info(self, slot_name: str) -> Optional[Dict[str, Any]]:
        """
        功能:
            获取单个槽位的物料信息
        参数:
            slot_name: 槽位名称
        返回:
            Dict 或 None, 槽位物料信息, 空槽位返回 None
        """
        if slot_name not in SHELF_SLOT_NAMES:
            logger.error("无效的槽位名称: %s", slot_name)
            return None

        info = self._status["slots"][slot_name]
        if info is not None:
            return copy.deepcopy(info)
        return None

    def get_all_status(self) -> Dict[str, Any]:
        """
        功能:
            获取全部货架状态的深拷贝
        返回:
            Dict, 包含 last_updated 和 slots 的完整状态
        """
        return copy.deepcopy(self._status)

    def print_status(self) -> None:
        """
        功能:
            在控制台打印可视化的货架状态表格
        """
        print("\n========== 货架物料状态 ==========")
        print(f"  最后更新: {self._status.get('last_updated', '未知')}")
        print("-" * 60)
        print(f"  {'槽位':<20} {'状态':<8} {'物料类型':<16} {'来源'}")
        print("-" * 60)

        empty_count = 0
        occupied_count = 0

        for slot_name in SHELF_SLOT_NAMES:
            info = self._status["slots"].get(slot_name)
            if info is None:
                print(f"  {slot_name:<20} {'空闲':<8}")
                empty_count += 1
            else:
                material_type = info.get("material_type", "未知")
                source = info.get("source", "未知")
                print(f"  {slot_name:<20} {'占用':<8} {material_type:<16} {source}")
                occupied_count += 1

        print("-" * 60)
        print(f"  合计: 占用 {occupied_count} / 空闲 {empty_count} / 总计 {len(SHELF_SLOT_NAMES)}")
        print("=" * 38)

    def reset_all(self) -> None:
        """
        功能:
            重置全部槽位为空(调试用)
        """
        self._status = self._create_empty_status()
        self._save_status()
        logger.info("已重置全部货架槽位为空")
