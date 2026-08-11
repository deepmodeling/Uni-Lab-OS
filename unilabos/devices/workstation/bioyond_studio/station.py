"""
Bioyond工作站实现
Bioyond Workstation Implementation

集成Bioyond物料管理的工作站示例
"""
import time
import traceback
import threading
from datetime import datetime
from typing import Dict, Any, List, Optional, Union
import json
from pathlib import Path

from unilabos.devices.workstation.workstation_base import WorkstationBase, ResourceSynchronizer
from unilabos.devices.workstation.bioyond_studio.bioyond_rpc import BioyondV1RPC
from unilabos.registry.placeholder_type import ResourceSlot, DeviceSlot
from unilabos.resources.warehouse import WareHouse
from unilabos.utils.log import logger
from unilabos.resources.graphio import resource_bioyond_to_plr, resource_plr_to_bioyond

from unilabos.ros.nodes.base_device_node import BaseROS2DeviceNode
from unilabos.ros.nodes.presets.workstation import ROS2WorkstationNode
from unilabos.ros.msgs.message_converter import convert_to_ros_msg, Float64, String
from pylabrobot.resources.resource import Resource as ResourcePLR


from unilabos.devices.workstation.workstation_http_service import WorkstationHTTPService


class ConnectionMonitor:
    """Bioyond连接监控器"""
    def __init__(self, workstation, check_interval=30):
        self.workstation = workstation
        self.check_interval = check_interval
        self._running = False
        self._thread = None
        self._last_status = "unknown"

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True, name="BioyondConnectionMonitor")
        self._thread.start()
        logger.info("Bioyond连接监控器已启动")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
            logger.info("Bioyond连接监控器已停止")

    def _monitor_loop(self):
        while self._running:
            try:
                # 使用 lightweight API 检查连接
                # query_matial_type_list 是比较快的查询
                start_time = time.time()
                result = self.workstation.hardware_interface.material_type_list()

                status = "online" if result else "offline"
                msg = "Connection established" if status == "online" else "Failed to get material type list"

                if status != self._last_status:
                    logger.info(f"Bioyond连接状态变更: {self._last_status} -> {status}")
                    self._publish_event(status, msg)
                    self._last_status = status

                # 发布心跳 (可选，或者只在状态变更时发布)
                # self._publish_event(status, msg)

            except Exception as e:
                logger.error(f"Bioyond连接检查异常: {e}")
                if self._last_status != "error":
                    self._publish_event("error", str(e))
                    self._last_status = "error"

            time.sleep(self.check_interval)

    def _publish_event(self, status, message):
        try:
            if hasattr(self.workstation, "_ros_node") and self.workstation._ros_node:
                event_data = {
                    "status": status,
                    "message": message,
                    "timestamp": datetime.now().isoformat()
                }

                # 动态发布消息，需要在 ROS2DeviceNode 中有对应支持
                # 这里假设通用事件发布机制，使用 String 类型的 topic
                # 话题: /<namespace>/events/device_status
                ns = self.workstation._ros_node.namespace
                topic = f"{ns}/events/device_status"

                # 使用 ROS2DeviceNode 的发布功能
                # 如果没有预定义的 publisher，需要动态创建
                # 注意：workstation base node 可能没有自动创建 arbitrary publishers 的机制
                # 这里我们先尝试用 String json 发布

                # 在 ROS2DeviceNode 中通常需要先 create_publisher
                # 为了简单起见，我们检查是否已有 publisher，没有则创建
                if not hasattr(self.workstation, "_device_status_pub"):
                    self.workstation._device_status_pub = self.workstation._ros_node.create_publisher(
                        String, topic, 10
                    )

                self.workstation._device_status_pub.publish(
                    convert_to_ros_msg(String, json.dumps(event_data, ensure_ascii=False))
                )
        except Exception as e:
            logger.error(f"发布设备状态事件失败: {e}")


class BioyondResourceSynchronizer(ResourceSynchronizer):
    """Bioyond资源同步器

    负责与Bioyond系统进行物料数据的同步
    """

    def __init__(self, workstation: 'BioyondWorkstation'):
        super().__init__(workstation)
        self.bioyond_api_client = None
        self.sync_interval = 60  # 默认60秒同步一次
        self.last_sync_time = 0
        self.initialize()

    def initialize(self) -> bool:
        """初始化Bioyond资源同步器"""
        try:
            self.bioyond_api_client = self.workstation.hardware_interface
            if self.bioyond_api_client is None:
                logger.error("Bioyond API客户端未初始化")
                return False

            # 设置同步间隔
            self.sync_interval = self.workstation.bioyond_config.get("sync_interval", 600)

            logger.info("Bioyond资源同步器初始化完成")
            return True
        except Exception as e:
            logger.error(f"Bioyond资源同步器初始化失败: {e}")
            return False

    def sync_from_external(self) -> bool:
        """从Bioyond系统同步物料数据"""
        try:
            if self.bioyond_api_client is None:
                logger.error("Bioyond API客户端未初始化")
                return False

            # 同时查询耗材类型(typeMode=0)、样品类型(typeMode=1)和试剂类型(typeMode=2)
            all_bioyond_data = []

            # 查询耗材类型物料（例如：枪头盒）
            bioyond_data_type0 = self.bioyond_api_client.stock_material('{"typeMode": 0, "includeDetail": true}')
            if bioyond_data_type0:
                all_bioyond_data.extend(bioyond_data_type0)
                logger.debug(f"从Bioyond查询到 {len(bioyond_data_type0)} 个耗材类型物料")

            # 查询样品类型物料（烧杯、试剂瓶、分装板等）
            bioyond_data_type1 = self.bioyond_api_client.stock_material('{"typeMode": 1, "includeDetail": true}')
            if bioyond_data_type1:
                all_bioyond_data.extend(bioyond_data_type1)
                logger.debug(f"从Bioyond查询到 {len(bioyond_data_type1)} 个样品类型物料")

            # 查询试剂类型物料（样品板、样品瓶等）
            bioyond_data_type2 = self.bioyond_api_client.stock_material('{"typeMode": 2, "includeDetail": true}')
            if bioyond_data_type2:
                all_bioyond_data.extend(bioyond_data_type2)
                logger.debug(f"从Bioyond查询到 {len(bioyond_data_type2)} 个试剂类型物料")

            if not all_bioyond_data:
                logger.warning("从Bioyond获取的物料数据为空")
                return False

            # 转换为UniLab格式
            unilab_resources = resource_bioyond_to_plr(
                all_bioyond_data,
                type_mapping=self.workstation.bioyond_config["material_type_mappings"],
                deck=self.workstation.deck
            )

            logger.info(f"从Bioyond同步了 {len(unilab_resources)} 个资源")
            return True
        except Exception as e:
            logger.error(f"从Bioyond同步物料数据失败: {e}")
            return False

    def sync_to_external(self, resource: Any) -> bool:
        """将本地物料数据变更同步到Bioyond系统"""
        try:
            # ✅ 跳过仓库类型的资源 - 仓库是容器，不是物料
            resource_category = getattr(resource, "category", None)
            if resource_category == "warehouse":
                logger.debug(f"[同步→Bioyond] 跳过仓库类型资源: {resource.name} (仓库是容器，不需要同步为物料)")
                return True

            logger.info(f"[同步→Bioyond] 收到物料变更: {resource.name}")

            # 获取物料的 Bioyond ID
            extra_info = getattr(resource, "unilabos_extra", {})
            material_bioyond_id = extra_info.get("material_bioyond_id")

            # 🔥 查询所有物料，用于获取物料当前位置等信息
            existing_materials = []
            try:
                import json
                logger.info(f"[同步→Bioyond] 查询 Bioyond 系统中的所有物料...")
                all_materials = []

                for type_mode in [0, 1, 2]:  # 0=耗材, 1=样品, 2=试剂
                    query_params = json.dumps({
                        "typeMode": type_mode,
                        "filter": "",
                        "includeDetail": True
                    })
                    materials = self.bioyond_api_client.stock_material(query_params)
                    if materials:
                        all_materials.extend(materials)

                existing_materials = all_materials
                logger.info(f"[同步→Bioyond] 查询到 {len(all_materials)} 个物料")
            except Exception as e:
                logger.error(f"查询 Bioyond 物料失败: {e}")
                return False

            # ⭐ 如果没有 Bioyond ID，尝试从查询结果中按名称匹配
            if not material_bioyond_id:
                logger.warning(f"[同步→Bioyond] 物料 {resource.name} 没有 Bioyond ID，尝试按名称查询...")
                for mat in existing_materials:
                    if mat.get("name") == resource.name:
                        material_bioyond_id = mat.get("id")
                        mat_type = mat.get("typeName", "未知")
                        logger.info(f"✅ 找到物料 {resource.name} ({mat_type}) 的 Bioyond ID: {material_bioyond_id[:8]}...")
                        # 保存 ID 到资源对象
                        extra_info["material_bioyond_id"] = material_bioyond_id
                        setattr(resource, "unilabos_extra", extra_info)
                        break

                if not material_bioyond_id:
                    logger.warning(f"⚠️ 在 Bioyond 系统中未找到名为 {resource.name} 的物料")
                    logger.info(f"[同步→Bioyond] 这是一个新物料，将创建并入库到 Bioyond 系统")

            # 检查是否有位置更新请求
            update_site = extra_info.get("update_resource_site")

            if not update_site:
                logger.debug(f"[同步→Bioyond] 物料 {resource.name} 无位置更新请求，跳过同步")
                return True

            # ===== 物料移动/创建流程 =====
            logger.info(f"[同步→Bioyond] 📍 物料 {resource.name} 目标库位: {update_site}")

            if material_bioyond_id:
                logger.info(f"[同步→Bioyond] 🔄 物料已存在于 Bioyond (ID: {material_bioyond_id[:8]}...)，执行移动操作")
            else:
                logger.info(f"[同步→Bioyond] ➕ 物料不存在于 Bioyond，将创建新物料并入库")

            # 第1步：从配置中获取仓库配置
            warehouse_mapping = self.bioyond_config.get("warehouse_mapping", {})

            # 确定目标仓库名称
            parent_name = None
            target_location_uuid = None
            current_warehouse = None

            # 🔥 优先级1: 从 Bioyond 查询结果中获取物料当前所在的仓库
            if material_bioyond_id:
                for mat in existing_materials:
                    if mat.get("name") == resource.name or mat.get("id") == material_bioyond_id:
                        locations = mat.get("locations", [])
                        if locations and len(locations) > 0:
                            current_warehouse = locations[0].get("whName")
                            logger.info(f"[同步→Bioyond] 💡 物料当前位于 Bioyond 仓库: {current_warehouse}")
                        break

                # 优先在当前仓库中查找目标库位
                if current_warehouse and current_warehouse in warehouse_mapping:
                    site_uuids = warehouse_mapping[current_warehouse].get("site_uuids", {})
                    if update_site in site_uuids:
                        parent_name = current_warehouse
                        target_location_uuid = site_uuids[update_site]
                        logger.info(f"[同步→Bioyond] ✅ 在当前仓库找到目标库位: {parent_name}/{update_site}")
                        logger.info(f"[同步→Bioyond] 目标库位UUID: {target_location_uuid[:8]}...")
                    else:
                        logger.warning(f"⚠️ [同步→Bioyond] 当前仓库 {current_warehouse} 中没有库位 {update_site}，将搜索其他仓库")

            # 🔥 优先级2: 检查 PLR 父节点名称
            if not parent_name or not target_location_uuid:
                if resource.parent is not None:
                    parent_name_candidate = resource.parent.name
                    logger.info(f"[同步→Bioyond] 从 PLR 父节点获取仓库名称: {parent_name_candidate}")

                    if parent_name_candidate in warehouse_mapping:
                        site_uuids = warehouse_mapping[parent_name_candidate].get("site_uuids", {})
                        if update_site in site_uuids:
                            parent_name = parent_name_candidate
                            target_location_uuid = site_uuids[update_site]
                            logger.info(f"[同步→Bioyond] ✅ 在父节点仓库找到目标库位: {parent_name}/{update_site}")
                            logger.info(f"[同步→Bioyond] 目标库位UUID: {target_location_uuid[:8]}...")

            # 🔥 优先级3: 遍历所有仓库查找（兜底方案）
            if not parent_name or not target_location_uuid:
                logger.info(f"[同步→Bioyond] 从所有仓库中查找库位 {update_site}...")
                for warehouse_name, warehouse_info in warehouse_mapping.items():
                    site_uuids = warehouse_info.get("site_uuids", {})
                    if update_site in site_uuids:
                        parent_name = warehouse_name
                        target_location_uuid = site_uuids[update_site]
                        logger.warning(f"[同步→Bioyond] ⚠️ 在其他仓库找到目标库位: {parent_name}/{update_site}")
                        logger.info(f"[同步→Bioyond] 目标库位UUID: {target_location_uuid[:8]}...")
                        break

            if not parent_name or not target_location_uuid:
                logger.error(f"❌ [同步→Bioyond] 库位 {update_site} 没有在 WAREHOUSE_MAPPING 中配置")
                logger.debug(f"[同步→Bioyond] 可用仓库: {list(warehouse_mapping.keys())}")
                return False

            # 第2步：转换为 Bioyond 格式
            logger.info(f"[同步→Bioyond] 🔄 转换物料为 Bioyond 格式...")

            # 从配置中获取物料默认参数
            material_default_params = self.workstation.bioyond_config.get("material_default_parameters", {})
            material_type_params = self.workstation.bioyond_config.get("material_type_parameters", {})

            # 合并参数配置：物料名称参数 + typeId参数（转换为 type:<uuid> 格式）
            merged_params = material_default_params.copy()
            for type_id, params in material_type_params.items():
                merged_params[f"type:{type_id}"] = params

            bioyond_material = resource_plr_to_bioyond(
                [resource],
                type_mapping=self.workstation.bioyond_config["material_type_mappings"],
                warehouse_mapping=self.workstation.bioyond_config["warehouse_mapping"],
                material_params=merged_params
            )[0]

            logger.info(f"[同步→Bioyond] 🔧 准备覆盖locations字段，目标仓库: {parent_name}, 库位: {update_site}, UUID: {target_location_uuid[:8]}...")

            # 🔥 强制覆盖 locations 信息，使用正确的目标库位 UUID
            # resource_plr_to_bioyond 可能会生成错误的仓库信息，这里直接覆盖
            bioyond_material["locations"] = [{
                "id": target_location_uuid,
                "whid": "",
                "whName": parent_name,
                "x": ord(update_site[0]) - ord('A') + 1,  # A→1, B→2, ...
                "y": int(update_site[1:]),  # 01→1, 02→2, ...
                "z": 1,
                "quantity": 0
            }]
            logger.info(f"[同步→Bioyond] ✅ 已覆盖库位信息: {parent_name}/{update_site} (UUID: {target_location_uuid[:8]}...)")

            logger.debug(f"[同步→Bioyond] Bioyond 物料数据: {bioyond_material}")

            location_info = bioyond_material.get("locations")
            logger.debug(f"[同步→Bioyond] 库位信息: {location_info}, 类型: {type(location_info)}")

            # 第3步：根据是否已有 Bioyond ID 决定创建还是使用现有物料
            if material_bioyond_id:
                # 物料已存在,直接使用现有 ID
                material_id = material_bioyond_id
                logger.info(f"✅ [同步→Bioyond] 使用已有物料 ID: {material_id[:8]}...")
            else:
                # 物料不存在,调用 API 创建新物料
                logger.info(f"[同步→Bioyond] 📤 调用 Bioyond API 添加物料...")
                material_id = self.bioyond_api_client.add_material(bioyond_material)

                if not material_id:
                    logger.error(f"❌ [同步→Bioyond] 添加物料失败，API 返回空")
                    return False

                logger.info(f"✅ [同步→Bioyond] 物料添加成功，Bioyond ID: {material_id[:8]}...")

                # 保存新创建的物料 ID 到资源对象
                extra_info["material_bioyond_id"] = material_id
                setattr(resource, "unilabos_extra", extra_info)

            # 第4步：物料入库前先检查目标库位是否被占用
            if location_info:
                logger.info(f"[同步→Bioyond] 📥 准备入库到库位 {update_site}...")

                # 处理不同的 location_info 数据结构
                if isinstance(location_info, list) and len(location_info) > 0:
                    location_id = location_info[0]["id"]
                elif isinstance(location_info, dict):
                    location_id = location_info["id"]
                else:
                    logger.warning(f"⚠️ [同步→Bioyond] 无效的库位信息格式: {location_info}")
                    location_id = None

                if location_id:
                    # 查询目标库位是否已有物料
                    logger.info(f"[同步→Bioyond] 🔍 检查库位 {update_site} (UUID: {location_id[:8]}...) 是否被占用...")

                    # 查询所有物料，检查是否有物料在目标库位
                    try:
                        all_materials_type1 = self.bioyond_api_client.stock_material('{"typeMode": 1, "includeDetail": true}')
                        all_materials_type2 = self.bioyond_api_client.stock_material('{"typeMode": 2, "includeDetail": true}')
                        all_materials = (all_materials_type1 or []) + (all_materials_type2 or [])

                        # 检查是否有物料已经在目标库位
                        location_occupied = False
                        occupying_material = None

                        # 同时检查当前物料是否在其他位置（需要先出库）
                        current_material_location = None
                        current_location_uuid = None

                        for material in all_materials:
                            locations = material.get("locations", [])

                            # 检查目标库位占用情况
                            for loc in locations:
                                if loc.get("id") == location_id:
                                    location_occupied = True
                                    occupying_material = material
                                    logger.warning(f"⚠️ [同步→Bioyond] 库位 {update_site} 已被占用！")
                                    logger.warning(f"   占用物料: {material.get('name')} (ID: {material.get('id', '')[:8]}...)")
                                    logger.warning(f"   占用位置: code={loc.get('code')}, x={loc.get('x')}, y={loc.get('y')}")
                                    logger.warning(f"   🔍 详细信息: location_id={loc.get('id')[:8]}..., 目标UUID={location_id[:8]}...")
                                    logger.warning(f"   🔍 完整location数据: {loc}")
                                    break

                            # 检查当前物料是否在其他位置
                            if material.get("id") == material_id and locations:
                                current_material_location = locations[0]
                                current_location_uuid = current_material_location.get("id")
                                logger.info(f"📍 [同步→Bioyond] 物料当前位置: {current_material_location.get('whName')}/{current_material_location.get('code')} (UUID: {current_location_uuid[:8]}...)")

                            if location_occupied:
                                break

                        if location_occupied:
                            # 如果是同一个物料（ID相同），说明已经在目标位置了，跳过
                            if occupying_material and occupying_material.get("id") == material_id:
                                logger.info(f"✅ [同步→Bioyond] 物料 {resource.name} 已经在库位 {update_site}，跳过重复入库")
                                return True
                            else:
                                logger.error(f"❌ [同步→Bioyond] 库位 {update_site} 已被其他物料占用，拒绝入库")
                                return False

                        logger.info(f"✅ [同步→Bioyond] 库位 {update_site} 可用，准备入库...")

                    except Exception as e:
                        logger.warning(f"⚠️ [同步→Bioyond] 检查库位状态时发生异常: {e}，继续尝试入库...")

                    # 🔧 如果物料当前在其他位置，先出库再入库
                    if current_location_uuid and current_location_uuid != location_id:
                        logger.info(f"[同步→Bioyond] 🚚 物料需要移动，先从当前位置出库...")
                        logger.info(f"   当前位置 UUID: {current_location_uuid[:8]}...")
                        logger.info(f"   目标位置 UUID: {location_id[:8]}...")

                        try:
                            # 获取物料数量用于出库
                            material_quantity = current_material_location.get("totalNumber", 1)
                            logger.info(f"   出库数量: {material_quantity}")

                            # 调用出库 API
                            outbound_response = self.bioyond_api_client.material_outbound_by_id(
                                material_id,
                                current_location_uuid,
                                material_quantity
                            )
                            logger.info(f"✅ [同步→Bioyond] 物料从 {current_material_location.get('code')} 出库成功")
                        except Exception as e:
                            logger.error(f"❌ [同步→Bioyond] 物料出库失败: {e}")
                            return False

                    # 执行入库
                    logger.info(f"[同步→Bioyond] 📥 调用 Bioyond API 物料入库...")
                    response = self.bioyond_api_client.material_inbound(material_id, location_id)

                    # 注意：Bioyond API 成功时返回空字典 {}，所以不能用 if not response 判断
                    # 只要没有抛出异常，就认为成功（response 是 dict 类型，即使是 {} 也不是 None）
                    if response is not None:
                        logger.info(f"✅ [同步→Bioyond] 物料 {resource.name} 成功入库到 {update_site}")

                        # 入库成功后，重新查询验证物料实际入库位置
                        logger.info(f"[同步→Bioyond] 🔍 验证物料实际入库位置...")
                        try:
                            all_materials_type1 = self.bioyond_api_client.stock_material('{"typeMode": 1, "includeDetail": true}')
                            all_materials_type2 = self.bioyond_api_client.stock_material('{"typeMode": 2, "includeDetail": true}')
                            all_materials = (all_materials_type1 or []) + (all_materials_type2 or [])

                            for material in all_materials:
                                if material.get("id") == material_id:
                                    locations = material.get("locations", [])
                                    if locations:
                                        actual_loc = locations[0]
                                        logger.info(f"📍 [同步→Bioyond] 物料实际位置: code={actual_loc.get('code')}, "
                                                  f"warehouse={actual_loc.get('whName')}, "
                                                  f"x={actual_loc.get('x')}, y={actual_loc.get('y')}")

                                        # 验证 UUID 是否匹配
                                        if actual_loc.get("id") != location_id:
                                            logger.error(f"❌ [同步→Bioyond] UUID 不匹配！")
                                            logger.error(f"   预期 UUID: {location_id}")
                                            logger.error(f"   实际 UUID: {actual_loc.get('id')}")
                                            logger.error(f"   这说明配置文件中的 UUID 映射有误，请检查 config.py 中的 WAREHOUSE_MAPPING")
                                    break
                        except Exception as e:
                            logger.warning(f"⚠️ [同步→Bioyond] 验证入库位置时发生异常: {e}")
                    else:
                        logger.error(f"❌ [同步→Bioyond] 物料入库失败")
                        return False
                else:
                    logger.warning(f"⚠️ [同步→Bioyond] 无法获取库位 ID，跳过入库操作")
            else:
                logger.warning(f"⚠️ [同步→Bioyond] 物料没有库位信息，跳过入库操作")
            return True

        except Exception as e:
            logger.error(f"❌ [同步→Bioyond] 同步物料 {resource.name} 时发生异常: {e}")
            import traceback
            traceback.print_exc()
            return False

    def handle_external_change(self, change_info: Dict[str, Any]) -> bool:
        """处理Bioyond系统的变更通知"""
        try:
            # 这里可以实现对Bioyond变更的处理逻辑
            logger.info(f"处理Bioyond变更通知: {change_info}")

            return True
        except Exception as e:
            logger.error(f"处理Bioyond变更通知失败: {e}")
            return False

    def _create_material_only(self, resource: Any) -> Optional[str]:
        """只创建物料到 Bioyond 系统（不入库）

        Transfer 阶段使用：只调用 add_material API 创建物料记录

        Args:
            resource: 要创建的资源对象

        Returns:
            str: 创建成功返回 Bioyond 物料 ID，失败返回 None
        """
        try:
            # 跳过仓库类型的资源
            resource_category = getattr(resource, "category", None)
            if resource_category == "warehouse":
                logger.debug(f"[创建物料] 跳过仓库类型资源: {resource.name}")
                return None

            logger.info(f"[创建物料] 开始创建物料: {resource.name}")

            # 检查是否已经有 Bioyond ID
            extra_info = getattr(resource, "unilabos_extra", {})
            material_bioyond_id = extra_info.get("material_bioyond_id")

            if material_bioyond_id:
                logger.info(f"[创建物料] 物料 {resource.name} 已存在 (ID: {material_bioyond_id[:8]}...)，跳过创建")
                return material_bioyond_id

            # 转换为 Bioyond 格式
            # 从配置中获取物料默认参数
            material_default_params = self.workstation.bioyond_config.get("material_default_parameters", {})
            material_type_params = self.workstation.bioyond_config.get("material_type_parameters", {})

            # 合并参数配置：物料名称参数 + typeId参数（转换为 type:<uuid> 格式）
            merged_params = material_default_params.copy()
            for type_id, params in material_type_params.items():
                merged_params[f"type:{type_id}"] = params

            bioyond_material = resource_plr_to_bioyond(
                [resource],
                type_mapping=self.workstation.bioyond_config["material_type_mappings"],
                warehouse_mapping=self.workstation.bioyond_config["warehouse_mapping"],
                material_params=merged_params
            )[0]

            # ⚠️ 关键：创建物料时不设置 locations，让 Bioyond 系统暂不分配库位
            # locations 字段在后续的入库操作中才会指定
            bioyond_material.pop("locations", None)

            logger.info(f"[创建物料] 调用 Bioyond API 创建物料（不指定库位）...")
            material_id = self.bioyond_api_client.add_material(bioyond_material)

            if not material_id:
                logger.error(f"[创建物料] 创建物料失败，API 返回空")
                return None

            logger.info(f"✅ [创建物料] 物料创建成功，ID: {material_id[:8]}...")

            # 保存 Bioyond ID 到资源对象
            extra_info["material_bioyond_id"] = material_id
            setattr(resource, "unilabos_extra", extra_info)

            return material_id

        except Exception as e:
            logger.error(f"❌ [创建物料] 创建物料 {resource.name} 时发生异常: {e}")
            import traceback
            traceback.print_exc()
            return None

    def _inbound_material_only(self, resource: Any, material_id: str) -> bool:
        """只执行物料入库操作（物料已存在于 Bioyond 系统）

        Add 阶段使用：调用 material_inbound API 将物料入库到指定库位

        Args:
            resource: 要入库的资源对象
            material_id: Bioyond 物料 ID

        Returns:
            bool: 入库成功返回 True，失败返回 False
        """
        try:
            logger.info(f"[物料入库] 开始入库物料: {resource.name} (ID: {material_id[:8]}...)")

            # 获取目标库位信息
            extra_info = getattr(resource, "unilabos_extra", {})
            update_site = extra_info.get("update_resource_site")

            if not update_site:
                logger.warning(f"[物料入库] 物料 {resource.name} 没有指定目标库位，跳过入库")
                return True

            logger.info(f"[物料入库] 目标库位: {update_site}")

            # 获取仓库配置和目标库位 UUID
            warehouse_mapping = self.workstation.bioyond_config.get("warehouse_mapping", {})

            parent_name = None
            target_location_uuid = None

            # 查找目标库位的 UUID
            if resource.parent is not None:
                parent_name_candidate = resource.parent.name
                if parent_name_candidate in warehouse_mapping:
                    site_uuids = warehouse_mapping[parent_name_candidate].get("site_uuids", {})
                    if update_site in site_uuids:
                        parent_name = parent_name_candidate
                        target_location_uuid = site_uuids[update_site]
                        logger.info(f"[物料入库] 从父节点找到库位: {parent_name}/{update_site}")

            # 兜底：遍历所有仓库查找
            if not target_location_uuid:
                for warehouse_name, warehouse_info in warehouse_mapping.items():
                    site_uuids = warehouse_info.get("site_uuids", {})
                    if update_site in site_uuids:
                        parent_name = warehouse_name
                        target_location_uuid = site_uuids[update_site]
                        logger.info(f"[物料入库] 从所有仓库找到库位: {parent_name}/{update_site}")
                        break

            if not target_location_uuid:
                logger.error(f"❌ [物料入库] 库位 {update_site} 未在配置中找到")
                return False

            logger.info(f"[物料入库] 库位 UUID: {target_location_uuid[:8]}...")

            # 调用入库 API
            logger.info(f"[物料入库] 调用 Bioyond API 执行入库...")
            response = self.bioyond_api_client.material_inbound(material_id, target_location_uuid)

            if response:  # 空字典 {} 表示失败，非空字典表示成功
                logger.info(f"✅ [物料入库] 物料 {resource.name} 成功入库到 {update_site}")
                return True
            else:
                logger.error(f"❌ [物料入库] 物料入库失败，API返回空响应或失败")
                return False

        except Exception as e:
            logger.error(f"❌ [物料入库] 入库物料 {resource.name} 时发生异常: {e}")
            import traceback
            traceback.print_exc()
            return False


class BioyondWorkstation(WorkstationBase):
    """Bioyond工作站

    集成Bioyond物料管理的工作站实现
    """

    def _publish_task_status(
        self,
        task_id: str,
        task_type: str,
        status: str,
        result: dict = None,
        progress: float = 0.0,
        task_code: str = None
    ):
        """发布任务状态事件"""
        try:
            if not getattr(self, "_ros_node", None):
                return

            event_data = {
                "task_id": task_id,
                "task_code": task_code,
                "task_type": task_type,
                "status": status,
                "progress": progress,
                "timestamp": datetime.now().isoformat()
            }
            if result:
                event_data["result"] = result

            topic = f"{self._ros_node.namespace}/events/task_status"

            if not hasattr(self, "_task_status_pub"):
                self._task_status_pub = self._ros_node.create_publisher(
                    String, topic, 10
                )

            self._task_status_pub.publish(
                convert_to_ros_msg(String, json.dumps(event_data, ensure_ascii=False))
            )
        except Exception as e:
            logger.error(f"发布任务状态事件失败: {e}")

    def __init__(
        self,
        bioyond_config: Optional[Dict[str, Any]] = None,
        deck: Optional[Any] = None,
        *args,
        **kwargs,
    ):
        # 初始化父类
        super().__init__(
            # 桌子
            deck=deck,
            *args,
            **kwargs,
        )

        # 检查 deck 是否为 None，防止 AttributeError
        if self.deck is None:
            logger.error("❌ Deck 配置为空，请检查配置文件中的 deck 参数")
            raise ValueError("Deck 配置不能为空，请在配置文件中添加正确的 deck 配置")

        # 初始化 warehouses 属性
        if not hasattr(self.deck, "warehouses") or self.deck.warehouses is None:
            self.deck.warehouses = {}

        # 仅当 warehouses 为空时尝试重新扫描（避免覆盖子类的修复）
        if not self.deck.warehouses:
            for resource in self.deck.children:
                # 兼容性增强: 只要是仓库类别或者是 WareHouse 实例均可
                is_warehouse = isinstance(resource, WareHouse) or getattr(resource, "category", "") == "warehouse"

                # 如果配置中有定义，也可以认定为 warehouse
                if not is_warehouse and "warehouse_mapping" in bioyond_config:
                    if resource.name in bioyond_config["warehouse_mapping"]:
                        is_warehouse = True

                if is_warehouse:
                    self.deck.warehouses[resource.name] = resource
                    # 确保 category 被正确设置，方便后续使用
                    if getattr(resource, "category", "") != "warehouse":
                        try:
                            resource.category = "warehouse"
                        except:
                            pass

        # 创建通信模块
        self._create_communication_module(bioyond_config)
        self.resource_synchronizer = BioyondResourceSynchronizer(self)
        self.resource_synchronizer.sync_from_external()

        # TODO: self._ros_node里面拿属性

        # 工作流加载
        self.is_running = False
        self.workflow_mappings = {}
        self.workflow_sequence = []
        self.pending_task_params = []

        if "workflow_mappings" in bioyond_config:
            self._set_workflow_mappings(bioyond_config["workflow_mappings"])

        # 准备 HTTP 报送接收服务配置（延迟到 post_init 启动）
        # 从 bioyond_config 中的 http_service_config 获取
        http_service_cfg = bioyond_config.get("http_service_config", {})
        self._http_service_config = {
            "host": http_service_cfg.get("http_service_host", "127.0.0.1"),
            "port": http_service_cfg.get("http_service_port", 8080)
        }
        self.http_service = None  # 将在 post_init 启动
        self.connection_monitor = None # 将在 post_init 启动

        logger.info(f"Bioyond工作站初始化完成")

    def __del__(self):
        """析构函数：清理资源，停止 HTTP 服务"""
        try:
            if hasattr(self, 'connection_monitor') and self.connection_monitor:
                self.connection_monitor.stop()
            if hasattr(self, 'http_service') and self.http_service is not None:
                logger.info("正在停止 HTTP 报送服务...")
                self.http_service.stop()
        except Exception as e:
            logger.error(f"停止 HTTP 服务时发生错误: {e}")

    def post_init(self, ros_node: ROS2WorkstationNode):
        self._ros_node = ros_node

        # 启动连接监控
        try:
            self.connection_monitor = ConnectionMonitor(self)
            self.connection_monitor.start()
        except Exception as e:
            logger.error(f"启动连接监控失败: {e}")

        # 启动 HTTP 报送接收服务（现在 device_id 已可用）
        # ⚠️ 检查子类是否已经自己管理 HTTP 服务
        if self.bioyond_config.get("_disable_auto_http_service"):
            logger.info("🔧 检测到 _disable_auto_http_service 标志，跳过自动启动 HTTP 服务")
            logger.info("   子类（BioyondCellWorkstation）已自行管理 HTTP 服务")
        elif hasattr(self, '_http_service_config'):
            try:
                self.http_service = WorkstationHTTPService(
                    workstation_instance=self,
                    host=self._http_service_config["host"],
                    port=self._http_service_config["port"]
                )
                self.http_service.start()
                logger.info(f"Bioyond工作站HTTP报送服务已启动: {self.http_service.service_url}")
            except Exception as e:
                logger.error(f"启动HTTP报送服务失败: {e}")
                import traceback
                traceback.print_exc()
                self.http_service = None

        # ⭐ 上传 deck（包括所有 warehouses 及其中的物料）
        # 注意：如果有从 Bioyond 同步的物料，它们已经被放置到 warehouse 中了
        # 所以只需要上传 deck，物料会作为 warehouse 的 children 一起上传
        logger.info("正在上传 deck（包括 warehouses 和物料）到云端...")
        self._ros_node.run_async_func(self._ros_node.update_resource, True, **{
            "resources": [self.deck]
        })

        # 清理临时变量（物料已经在 deck 的 warehouse children 中，不需要单独上传）
        if hasattr(self, "_synced_resources"):
            logger.info(f"✅ {len(self._synced_resources)} 个从Bioyond同步的物料已包含在 deck 中")
            self._synced_resources = []

    def transfer_resource_to_another(self, resource: List[ResourceSlot], mount_resource: List[ResourceSlot], sites: List[str], mount_device_id: DeviceSlot):
        future = self._ros_node.run_async_func(self._ros_node.transfer_resource_to_another, True, **{
            "plr_resources": resource,
            "target_device_id": mount_device_id,
            "target_resources": mount_resource,
            "sites": sites,
        })
        return future

    def _create_communication_module(self, config: Optional[Dict[str, Any]] = None) -> None:
        """创建Bioyond通信模块"""
        # 直接使用传入的配置，不再使用默认值
        # 所有配置必须从 JSON 文件中提供
        if config:
            self.bioyond_config = config
        else:
            # 如果没有配置，使用空字典（会导致后续错误，但这是预期的）
            self.bioyond_config = {}
            print("警告: 未提供 bioyond_config，请确保在 JSON 配置文件中提供完整配置")

        self.hardware_interface = BioyondV1RPC(self.bioyond_config)

    def resource_tree_add(self, resources: List[ResourcePLR]) -> None:
        """添加资源到资源树并更新ROS节点

        Args:
            resources (List[ResourcePLR]): 要添加的资源列表
        """
        logger.info(f"[resource_tree_add] 开始同步 {len(resources)} 个资源到 Bioyond 系统")
        for resource in resources:
            try:
                # 🔍 检查资源是否已有 Bioyond ID
                extra_info = getattr(resource, "unilabos_extra", {})
                material_bioyond_id = extra_info.get("material_bioyond_id")

                if material_bioyond_id:
                    # ⭐ 已有 Bioyond ID，说明 transfer 已经创建了物料
                    # 现在只需要执行入库操作
                    logger.info(f"✅ [resource_tree_add] 物料 {resource.name} 已有 Bioyond ID ({material_bioyond_id[:8]}...)，执行入库操作")
                    self.resource_synchronizer._inbound_material_only(resource, material_bioyond_id)
                else:
                    # ⚠️ 没有 Bioyond ID，说明是直接添加的物料（兜底逻辑）
                    # 需要先创建再入库
                    logger.info(f"⚠️ [resource_tree_add] 物料 {resource.name} 无 Bioyond ID，执行创建+入库操作")
                    self.resource_synchronizer.sync_to_external(resource)

            except Exception as e:
                logger.error(f"[resource_tree_add] 同步资源失败 {resource}: {e}")
                import traceback
                traceback.print_exc()

    def resource_tree_remove(self, resources: List[ResourcePLR]) -> None:
        """处理资源删除时的同步（出库操作）

        当 UniLab 前端删除物料时，需要将删除操作同步到 Bioyond 系统（出库）

        Args:
            resources: 要删除的资源列表
        """
        logger.info(f"[resource_tree_remove] 收到 {len(resources)} 个资源的移除请求（出库操作）")

        # ⭐ 关键优化：先找出所有的顶层容器（BottleCarrier），只对它们进行出库
        # 因为在 Bioyond 中，容器（如分装板 1105-12）是一个完整的物料
        # 里面的小瓶子是它的 detail 字段，不需要单独出库

        top_level_resources = []
        child_resource_names = set()

        # 第一步：识别所有子资源的名称
        for resource in resources:
            resource_category = getattr(resource, "category", None)
            if resource_category == "bottle_carrier":
                children = list(resource.children) if hasattr(resource, 'children') else []
                for child in children:
                    child_resource_names.add(child.name)

        # 第二步：筛选出顶层资源（不是任何容器的子资源）
        for resource in resources:
            resource_category = getattr(resource, "category", None)

            # 跳过仓库类型的资源
            if resource_category == "warehouse":
                logger.debug(f"[resource_tree_remove] 跳过仓库类型资源: {resource.name}")
                continue

            # 如果是容器，它就是顶层资源
            if resource_category == "bottle_carrier":
                top_level_resources.append(resource)
                logger.info(f"[resource_tree_remove] 识别到顶层容器资源: {resource.name}")
            # 如果不是任何容器的子资源，它也是顶层资源
            elif resource.name not in child_resource_names:
                top_level_resources.append(resource)
                logger.info(f"[resource_tree_remove] 识别到顶层独立资源: {resource.name}")
            else:
                logger.debug(f"[resource_tree_remove] 跳过子资源（将随容器一起出库）: {resource.name}")

        logger.info(f"[resource_tree_remove] 实际需要处理的顶层资源: {len(top_level_resources)} 个")

        # 第三步：对每个顶层资源执行出库操作
        for resource in top_level_resources:
            try:
                self._outbound_single_resource(resource)
            except Exception as e:
                logger.error(f"❌ [resource_tree_remove] 处理资源 {resource.name} 出库失败: {e}")
                import traceback
                traceback.print_exc()

        logger.info(f"[resource_tree_remove] 资源移除（出库）操作完成")

    def _outbound_single_resource(self, resource: ResourcePLR) -> bool:
        """对单个资源执行 Bioyond 出库操作

        Args:
            resource: 要出库的资源

        Returns:
            bool: 出库是否成功
        """
        try:
            logger.info(f"[resource_tree_remove] 🎯 开始处理资源出库: {resource.name}")

            # 获取资源的 Bioyond 信息
            extra_info = getattr(resource, "unilabos_extra", {})
            material_bioyond_id = extra_info.get("material_bioyond_id")
            material_bioyond_name = extra_info.get("material_bioyond_name")  # ⭐ 原始 Bioyond 名称

            # ⭐ 优先使用保存的 Bioyond ID，避免重复查询
            if material_bioyond_id:
                logger.info(f"✅ [resource_tree_remove] 从资源中获取到 Bioyond ID: {material_bioyond_id[:8]}...")
                if material_bioyond_name and material_bioyond_name != resource.name:
                    logger.info(f"   原始 Bioyond 名称: {material_bioyond_name} (当前名称: {resource.name})")
            else:
                # 如果没有 Bioyond ID，尝试按名称查询
                logger.info(f"[resource_tree_remove] 资源 {resource.name} 没有保存 Bioyond ID，尝试查询...")

                # ⭐ 优先使用保存的原始 Bioyond 名称，如果没有则使用当前名称
                query_name = material_bioyond_name if material_bioyond_name else resource.name
                logger.info(f"[resource_tree_remove] 查询 Bioyond 系统中的物料: {query_name}")

                # 查询所有类型的物料：0=耗材, 1=样品, 2=试剂
                all_materials = []
                for type_mode in [0, 1, 2]:
                    query_params = json.dumps({
                        "typeMode": type_mode,
                        "filter": query_name,  # ⭐ 使用原始 Bioyond 名称查询
                        "includeDetail": True
                    })
                    materials = self.hardware_interface.stock_material(query_params)
                    if materials:
                        all_materials.extend(materials)

                # 精确匹配物料名称
                matched_material = None
                for mat in all_materials:
                    if mat.get("name") == query_name:
                        matched_material = mat
                        material_bioyond_id = mat.get("id")
                        logger.info(f"✅ [resource_tree_remove] 找到物料 {query_name} 的 Bioyond ID: {material_bioyond_id[:8]}...")
                        break

                if not matched_material:
                    logger.warning(f"⚠️ [resource_tree_remove] Bioyond 系统中未找到物料: {query_name}")
                    logger.info(f"[resource_tree_remove] 该物料可能尚未入库或已被删除，跳过出库操作")
                    return True

            # 获取物料当前所在的库位信息
            logger.info(f"[resource_tree_remove] 📍 查询物料的库位信息...")

            # 重新查询物料详情以获取最新的库位信息
            all_materials_type1 = self.hardware_interface.stock_material('{"typeMode": 1, "includeDetail": true}')
            all_materials_type2 = self.hardware_interface.stock_material('{"typeMode": 2, "includeDetail": true}')
            all_materials_type0 = self.hardware_interface.stock_material('{"typeMode": 0, "includeDetail": true}')
            all_materials = (all_materials_type0 or []) + (all_materials_type1 or []) + (all_materials_type2 or [])

            location_id = None
            current_quantity = 0

            for material in all_materials:
                if material.get("id") == material_bioyond_id:
                    locations = material.get("locations", [])
                    if locations:
                        # 取第一个库位
                        location = locations[0]
                        location_id = location.get("id")
                        current_quantity = location.get("quantity", 1)
                        logger.info(f"📍 [resource_tree_remove] 物料位于库位:")
                        logger.info(f"   - 库位代码: {location.get('code')}")
                        logger.info(f"   - 仓库名称: {location.get('whName')}")
                        logger.info(f"   - 数量: {current_quantity}")
                        logger.info(f"   - 库位ID: {location_id[:8]}...")
                        break
                    else:
                        logger.warning(f"⚠️ [resource_tree_remove] 物料没有库位信息，可能尚未入库")
                        return True

            if not location_id:
                logger.warning(f"⚠️ [resource_tree_remove] 无法获取物料的库位信息，跳过出库")
                return False

            # 调用 Bioyond 出库 API
            logger.info(f"[resource_tree_remove] 📤 调用 Bioyond API 出库物料...")
            logger.info(f"   UniLab 名称: {resource.name}")
            if material_bioyond_name and material_bioyond_name != resource.name:
                logger.info(f"   Bioyond 名称: {material_bioyond_name}")
            logger.info(f"   物料ID: {material_bioyond_id[:8]}...")
            logger.info(f"   库位ID: {location_id[:8]}...")
            logger.info(f"   出库数量: {current_quantity}")

            response = self.hardware_interface.material_outbound_by_id(
                material_id=material_bioyond_id,
                location_id=location_id,
                quantity=current_quantity
            )

            if response is not None:
                logger.info(f"✅ [resource_tree_remove] 物料成功从 Bioyond 系统出库")
                return True
            else:
                logger.error(f"❌ [resource_tree_remove] 物料出库失败，API 返回空")
                return False

        except Exception as e:
            logger.error(f"❌ [resource_tree_remove] 物料 {resource.name} 出库时发生异常: {e}")
            import traceback
            traceback.print_exc()
            return False

    def resource_tree_transfer(self, old_parent: Optional[ResourcePLR], resource: ResourcePLR, new_parent: ResourcePLR) -> None:
        """处理资源在设备间迁移时的同步

        当资源从一个设备迁移到 BioyondWorkstation 时,只创建物料（不入库）
        入库操作由后续的 resource_tree_add 完成

        Args:
            old_parent: 资源的原父节点（可能为 None）
            resource: 要迁移的资源
            new_parent: 资源的新父节点
        """
        logger.info(f"[resource_tree_transfer] 资源迁移: {resource.name}")
        logger.info(f"  旧父节点: {old_parent.name if old_parent else 'None'}")
        logger.info(f"  新父节点: {new_parent.name}")

        try:
            # ⭐ Transfer 阶段：只创建物料到 Bioyond 系统，不执行入库
            logger.info(f"[resource_tree_transfer] 开始创建物料 {resource.name} 到 Bioyond 系统（不入库）")
            result = self.resource_synchronizer._create_material_only(resource)

            if result:
                logger.info(f"✅ [resource_tree_transfer] 物料 {resource.name} 创建成功，Bioyond ID: {result[:8]}...")
            else:
                logger.warning(f"⚠️ [resource_tree_transfer] 物料 {resource.name} 创建失败")

        except Exception as e:
            logger.error(f"❌ [resource_tree_transfer] 资源 {resource.name} 创建异常: {e}")
            import traceback
            traceback.print_exc()

    def resource_tree_update(self, resources: List[ResourcePLR]) -> None:
        """处理资源更新时的同步（位置移动、属性修改等）

        当 UniLab 前端更新物料信息时（如修改位置），需要将更新操作同步到 Bioyond 系统

        Args:
            resources: 要更新的资源列表
        """
        logger.info(f"[resource_tree_update] 开始同步 {len(resources)} 个资源更新到 Bioyond 系统")

        for resource in resources:
            try:
                logger.info(f"[resource_tree_update] 同步资源更新: {resource.name}")

                # 调用同步器的 sync_to_external 方法
                # 该方法会检查 unilabos_extra 中的 update_resource_site 字段
                # 如果存在，会执行位置移动操作
                result = self.resource_synchronizer.sync_to_external(resource)

                if result:
                    logger.info(f"✅ [resource_tree_update] 资源 {resource.name} 成功同步到 Bioyond 系统")
                else:
                    logger.warning(f"⚠️ [resource_tree_update] 资源 {resource.name} 同步到 Bioyond 系统失败")

            except Exception as e:
                logger.error(f"❌ [resource_tree_update] 同步资源 {resource.name} 时发生异常: {e}")
                import traceback
                traceback.print_exc()

        logger.info(f"[resource_tree_update] 资源更新同步完成")

    @property
    def bioyond_status(self) -> Dict[str, Any]:
        """获取 Bioyond 系统状态信息

        这个属性被 ROS 节点用来发布设备状态

        Returns:
            Dict[str, Any]: Bioyond 系统的状态信息
                - 连接成功时返回 {"connected": True}
                - 连接失败时返回 {"connected": False, "error": "错误信息"}
        """
        try:
            # 检查硬件接口是否存在
            if not self.hardware_interface:
                return {"connected": False, "error": "hardware_interface not initialized"}

            # 尝试获取调度器状态来验证连接
            scheduler_status = self.hardware_interface.scheduler_status()

            # 如果能成功获取状态，说明连接正常
            if scheduler_status:
                return {"connected": True}
            else:
                return {"connected": False, "error": "scheduler_status returned None"}

        except Exception as e:
            logger.warning(f"获取Bioyond状态失败: {e}")
            return {"connected": False, "error": str(e)}

    # ==================== 工作流合并与参数设置 API ====================

    def append_to_workflow_sequence(self, web_workflow_name: str) -> bool:
        # 检查是否为JSON格式的字符串
        actual_workflow_name = web_workflow_name
        if web_workflow_name.startswith('{') and web_workflow_name.endswith('}'):
            try:
                data = json.loads(web_workflow_name)
                actual_workflow_name = data.get("web_workflow_name", web_workflow_name)
                print(f"解析JSON格式工作流名称: {web_workflow_name} -> {actual_workflow_name}")
            except json.JSONDecodeError:
                print(f"JSON解析失败，使用原始字符串: {web_workflow_name}")

        workflow_id = self._get_workflow(actual_workflow_name)
        if workflow_id:
            # 兼容 BioyondReactionStation 中 workflow_sequence 被重写为 property 的情况
            if isinstance(self.workflow_sequence, list):
                self.workflow_sequence.append(workflow_id)
            elif hasattr(self, "_cached_workflow_sequence") and isinstance(self._cached_workflow_sequence, list):
                self._cached_workflow_sequence.append(workflow_id)
            else:
                print(f"❌ 无法添加工作流: workflow_sequence 类型错误 {type(self.workflow_sequence)}")
                return False

            print(f"添加工作流到执行顺序: {actual_workflow_name} -> {workflow_id}")
            return True
        return False

    def set_workflow_sequence(self, json_str: str) -> List[str]:
        try:
            data = json.loads(json_str)
            web_workflow_names = data.get("web_workflow_names", [])
        except:
            return []

        sequence = []
        for web_name in web_workflow_names:
            workflow_id = self._get_workflow(web_name)
            if workflow_id:
                sequence.append(workflow_id)

    def get_all_workflows(self) -> Dict[str, str]:
        return self.workflow_mappings.copy()

    def _get_workflow(self, web_workflow_name: str) -> str:
        if web_workflow_name not in self.workflow_mappings:
            print(f"未找到工作流映射配置: {web_workflow_name}")
            return ""
        workflow_id = self.workflow_mappings[web_workflow_name]
        print(f"获取工作流: {web_workflow_name} -> {workflow_id}")
        return workflow_id

    def _set_workflow_mappings(self, mappings: Dict[str, str]):
        self.workflow_mappings = mappings
        print(f"设置工作流映射配置: {mappings}")

    def process_web_workflows(self, json_str: str) -> Dict[str, str]:
        try:
            data = json.loads(json_str)
            web_workflow_list = data.get("web_workflow_list", [])
        except json.JSONDecodeError:
            print(f"无效的JSON字符串: {json_str}")
            return {}
        result = {}

        self.workflow_sequence = []
        for web_name in web_workflow_list:
            workflow_id = self._get_workflow(web_name)
            if workflow_id:
                result[web_name] = workflow_id
                self.workflow_sequence.append(workflow_id)
            else:
                print(f"无法获取工作流ID: {web_name}")
        print(f"工作流执行顺序: {self.workflow_sequence}")
        return result

    def clear_workflows(self):
        self.workflow_sequence = []
        print("清空工作流执行顺序")

    # ==================== 基础物料管理接口 ====================

    # ============ 工作站状态管理 ============
    def get_station_info(self) -> Dict[str, Any]:
        """获取工作站基础信息

        Returns:
            Dict[str, Any]: 工作站基础信息，包括设备ID、状态等
        """
        return {
            "device_id": getattr(self._ros_node, 'device_id', 'unknown'),
            "station_type": "BioyondWorkstation",
            "workflow_status": self.current_workflow_status.value if hasattr(self, 'current_workflow_status') else "unknown",
            "is_busy": getattr(self, 'is_busy', False),
            "deck_info": {
                "name": self.deck.name if self.deck and hasattr(self.deck, 'name') else "unknown",
                "children_count": len(self.deck.children) if self.deck and hasattr(self.deck, 'children') else 0
            } if self.deck else None,
            "hardware_interface": type(self.hardware_interface).__name__ if self.hardware_interface else None
        }

    def get_workstation_status(self) -> Dict[str, Any]:
        """获取工作站状态

        Returns:
            Dict[str, Any]: 工作站状态信息
        """
        try:
            # 获取基础工作站状态
            base_status = {
                "station_info": self.get_station_info(),
                "bioyond_status": self.bioyond_status
            }

            # 如果有接口，获取设备列表
            if self.hardware_interface:
                try:
                    devices = self.hardware_interface.device_list()
                    base_status["devices"] = devices
                except Exception as e:
                    logger.warning(f"获取设备列表失败: {e}")
                    base_status["devices"] = []

            return {
                "success": True,
                "data": base_status,
                "action": "get_workstation_status"
            }

        except Exception as e:
            error_msg = f"获取工作站状态失败: {str(e)}"
            logger.error(error_msg)
            return {
                "success": False,
                "message": error_msg,
                "action": "get_workstation_status"
            }

    def get_bioyond_status(self) -> Dict[str, Any]:
        """获取完整的 Bioyond 状态信息

        这个方法提供了比 bioyond_status 属性更详细的状态信息，
        包括错误处理和格式化的响应结构

        Returns:
            Dict[str, Any]: 格式化的 Bioyond 状态响应
        """
        try:
            status = self.bioyond_status
            return {
                "success": True,
                "data": status,
                "action": "get_bioyond_status"
            }

        except Exception as e:
            error_msg = f"获取 Bioyond 状态失败: {str(e)}"
            logger.error(error_msg)
            return {
                "success": False,
                "message": error_msg,
                "action": "get_bioyond_status"
            }

    def reset_workstation(self) -> Dict[str, Any]:
        """重置工作站

        重置工作站到初始状态

        Returns:
            Dict[str, Any]: 操作结果
        """
        try:
            logger.info("开始重置工作站")

            # 重置调度器
            if self.hardware_interface:
                self.hardware_interface.scheduler_reset()

            # 刷新物料缓存
            if self.hardware_interface:
                self.hardware_interface.refresh_material_cache()

            # 重新同步资源
            if self.resource_synchronizer:
                self.resource_synchronizer.sync_from_external()

            logger.info("工作站重置完成")
            return {
                "success": True,
                "message": "工作站重置成功",
                "action": "reset_workstation"
            }

        except Exception as e:
            error_msg = f"重置工作站失败: {str(e)}"
            logger.error(error_msg)
            return {
                "success": False,
                "message": error_msg,
                "action": "reset_workstation"
            }

    # ==================== HTTP 报送处理方法 ====================

    def process_step_finish_report(self, report_request) -> Dict[str, Any]:
        """处理步骤完成报送

        Args:
            report_request: WorkstationReportRequest 对象，包含步骤完成信息

        Returns:
            Dict[str, Any]: 处理结果
        """
        try:
            data = report_request.data
            logger.info(f"[步骤完成报送] 订单: {data.get('orderCode')}, 步骤: {data.get('stepName')}")
            logger.info(f"  样品ID: {data.get('sampleId')}")
            logger.info(f"  开始时间: {data.get('startTime')}")
            logger.info(f"  结束时间: {data.get('endTime')}")

            # TODO: 根据实际业务需求处理步骤完成逻辑
            # 例如：更新数据库、触发后续流程等

            # 发布任务状态事件 (running/progress update)
            self._publish_task_status(
                task_id=data.get('orderCode'), # 使用 OrderCode 作为关联 ID
                task_code=data.get('orderCode'),
                task_type="bioyond_step",
                status="running",
                progress=0.5, # 步骤完成视为任务进行中
                result={"step_name": data.get('stepName'), "step_id": data.get('stepId')}
            )

            # 更新物料信息
            # 步骤完成后，物料状态可能发生变化（如位置、用量等），触发同步
            logger.info(f"[步骤完成报送] 触发物料同步...")
            self.resource_synchronizer.sync_from_external()


            return {
                "processed": True,
                "step_id": data.get('stepId'),
                "timestamp": datetime.now().isoformat()
            }

        except Exception as e:
            logger.error(f"处理步骤完成报送失败: {e}")
            return {"processed": False, "error": str(e)}

    def process_sample_finish_report(self, report_request) -> Dict[str, Any]:
        """处理通量完成报送

        Args:
            report_request: WorkstationReportRequest 对象，包含通量完成信息

        Returns:
            Dict[str, Any]: 处理结果
        """
        try:
            data = report_request.data
            status_names = {
                "0": "待生产", "2": "进样", "10": "开始",
                "20": "完成", "-2": "异常停止", "-3": "人工停止"
            }
            status_desc = status_names.get(str(data.get('status')), f"状态{data.get('status')}")

            logger.info(f"[通量完成报送] 订单: {data.get('orderCode')}, 样品: {data.get('sampleId')}")
            logger.info(f"  状态: {status_desc}")
            logger.info(f"  开始时间: {data.get('startTime')}")
            logger.info(f"  结束时间: {data.get('endTime')}")

            # TODO: 根据实际业务需求处理通量完成逻辑

            # 发布任务状态事件
            self._publish_task_status(
                task_id=data.get('orderCode'),
                task_code=data.get('orderCode'),
                task_type="bioyond_sample",
                status="running",
                progress=0.7,
                result={"sample_id": data.get('sampleId'), "status": status_desc}
            )


            return {
                "processed": True,
                "sample_id": data.get('sampleId'),
                "status": data.get('status'),
                "timestamp": datetime.now().isoformat()
            }

        except Exception as e:
            logger.error(f"处理通量完成报送失败: {e}")
            return {"processed": False, "error": str(e)}

    def process_order_finish_report(self, report_request, used_materials: List) -> Dict[str, Any]:
        """处理任务完成报送

        Args:
            report_request: WorkstationReportRequest 对象，包含任务完成信息
            used_materials: 物料使用记录列表

        Returns:
            Dict[str, Any]: 处理结果
        """
        try:
            data = report_request.data
            status_names = {"30": "完成", "-11": "异常停止", "-12": "人工停止"}
            status_desc = status_names.get(str(data.get('status')), f"状态{data.get('status')}")

            logger.info(f"[任务完成报送] 订单: {data.get('orderCode')} - {data.get('orderName')}")
            logger.info(f"  状态: {status_desc}")
            logger.info(f"  开始时间: {data.get('startTime')}")
            logger.info(f"  结束时间: {data.get('endTime')}")
            logger.info(f"  使用物料数量: {len(used_materials)}")

            # 记录物料使用情况
            for material in used_materials:
                logger.debug(f"  物料: {material.materialId}, 用量: {material.usedQuantity}")

            # TODO: 根据实际业务需求处理任务完成逻辑
            # 例如：更新物料库存、生成报表等

            # 映射状态到事件状态
            event_status = "completed"
            if str(data.get('status')) in ["-11", "-12"]:
                event_status = "error"
            elif str(data.get('status')) == "30":
                event_status = "completed"
            else:
                event_status = "running" # 其他状态视为运行中（或根据实际定义）

            # 发布任务状态事件
            self._publish_task_status(
                task_id=data.get('orderCode'),
                task_code=data.get('orderCode'),
                task_type="bioyond_order",
                status=event_status,
                progress=1.0 if event_status in ["completed", "error"] else 0.9,
                result={"order_name": data.get('orderName'), "status": status_desc, "materials_count": len(used_materials)}
            )

            # 更新物料信息
            # 任务完成后，且状态为完成时，触发同步以更新最终物料状态
            if event_status == "completed":
                logger.info(f"[任务完成报送] 触发物料同步...")
                self.resource_synchronizer.sync_from_external()


            return {
                "processed": True,
                "order_code": data.get('orderCode'),
                "status": data.get('status'),
                "materials_count": len(used_materials),
                "timestamp": datetime.now().isoformat()
            }

        except Exception as e:
            logger.error(f"处理任务完成报送失败: {e}")
            return {"processed": False, "error": str(e)}

    def process_material_change_report(self, report_data: Dict[str, Any]) -> Dict[str, Any]:
        """处理物料变更报送

        Args:
            report_data: 物料变更数据

        Returns:
            Dict[str, Any]: 处理结果
        """
        try:
            logger.info(f"[物料变更报送] 工作站: {report_data.get('workstation_id')}")
            logger.info(f"  资源ID: {report_data.get('resource_id')}")
            logger.info(f"  变更类型: {report_data.get('change_type')}")
            logger.info(f"  时间戳: {report_data.get('timestamp')}")

            # TODO: 根据实际业务需求处理物料变更逻辑
            # 例如：同步到资源树、更新Bioyond系统等

            return {
                "processed": True,
                "resource_id": report_data.get('resource_id'),
                "change_type": report_data.get('change_type'),
                "timestamp": datetime.now().isoformat()
            }

        except Exception as e:
            logger.error(f"处理物料变更报送失败: {e}")
            return {"processed": False, "error": str(e)}


    def handle_external_error(self, error_data: Dict[str, Any]) -> Dict[str, Any]:
        """处理错误处理报送

        Args:
            error_data: 错误数据（可能是奔曜格式或标准格式）

        Returns:
            Dict[str, Any]: 处理结果
        """
        try:
            # 检查是否为奔曜格式
            if 'task' in error_data and 'code' in error_data:
                # 奔曜格式
                logger.error(f"[错误处理报送-奔曜] 任务: {error_data.get('task')}")
                logger.error(f"  错误代码: {error_data.get('code')}")
                logger.error(f"  错误信息: {error_data.get('message', '无')}")
                error_type = "bioyond_error"
            else:
                # 标准格式
                logger.error(f"[错误处理报送] 工作站: {error_data.get('workstation_id')}")
                logger.error(f"  错误类型: {error_data.get('error_type')}")
                logger.error(f"  错误信息: {error_data.get('error_message')}")
                error_type = error_data.get('error_type', 'unknown')

            # TODO: 根据实际业务需求处理错误
            # 例如：记录日志、发送告警、触发恢复流程等

            return {
                "handled": True,
                "error_type": error_type,
                "timestamp": datetime.now().isoformat()
            }

        except Exception as e:
            logger.error(f"处理错误报送失败: {e}")
            return {"handled": False, "error": str(e)}

    # ==================== 文件加载与其他功能 ====================

    def load_bioyond_data_from_file(self, file_path: str) -> bool:
        """从文件加载Bioyond数据（用于测试）"""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                bioyond_data = json.load(f)

            logger.info(f"从文件加载Bioyond数据: {file_path}")

            # 转换为UniLab格式
            unilab_resources = resource_bioyond_to_plr(
                bioyond_data,
                type_mapping=self.bioyond_config["material_type_mappings"],
                deck=self.deck
            )

            logger.info(f"成功加载 {len(unilab_resources)} 个资源")
            return True

        except Exception as e:
            logger.error(f"从文件加载Bioyond数据失败: {e}")
            return False


# 使用示例
def create_bioyond_workstation_example():
    """创建Bioyond工作站示例"""

    # 配置参数
    device_id = "bioyond_workstation_001"

    # 子资源配置
    children = {
        "plate_1": {
            "name": "plate_1",
            "type": "plate",
            "position": {"x": 100, "y": 100, "z": 0},
            "config": {
                "size_x": 127.76,
                "size_y": 85.48,
                "size_z": 14.35,
                "model": "Generic 96 Well Plate"
            }
        }
    }

    # Bioyond配置
    bioyond_config = {
        "base_url": "http://bioyond.example.com/api",
        "api_key": "your_api_key_here",
        "sync_interval": 60,  # 60秒同步一次
        "timeout": 30
    }

    # Deck配置
    deck_config = {
        "size_x": 1000.0,
        "size_y": 1000.0,
        "size_z": 100.0,
        "model": "BioyondDeck"
    }

    # 创建工作站
    workstation = BioyondWorkstation(
        station_resource=deck_config,
        bioyond_config=bioyond_config,
        deck_config=deck_config,
    )

    return workstation


if __name__ == "__main__":
    pass
