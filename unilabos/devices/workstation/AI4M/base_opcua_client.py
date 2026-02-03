"""
OPC UA 通讯基类（简化版 - 无工作流功能）
提供基础的 OPC UA 通讯功能：
- 客户端连接管理
- 节点注册和查找
- 节点读写操作
- 订阅和缓存机制

其他设备可以继承这个基类来使用 OPC UA 通讯功能
"""

import json
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple
from pydantic import BaseModel

from opcua import Client, ua
import pandas as pd
import os

from unilabos.device_comms.opcua_client.node.uniopcua import Base as OpcUaNodeBase
from unilabos.device_comms.opcua_client.node.uniopcua import Variable, Method, NodeType, DataType
from unilabos.device_comms.universal_driver import UniversalDriver
from unilabos.utils.log import logger


class OpcUaNode(BaseModel):
    """OPC UA 节点定义"""
    name: str
    node_type: NodeType
    node_id: str = ""
    data_type: Optional[DataType] = None
    parent_node_id: Optional[str] = None


class BaseOpcUaClient(UniversalDriver):
    """
    OPC UA 通讯基类
    提供基础的 OPC UA 通讯功能
    """
    client: Optional[Client] = None
    _node_registry: Dict[str, OpcUaNodeBase] = {}
    DEFAULT_ADDRESS_PATH = ""
    _variables_to_find: Dict[str, Dict[str, Any]] = {}
    _name_mapping: Dict[str, str] = {}  # 英文名到中文名的映射
    _reverse_mapping: Dict[str, str] = {}  # 中文名到英文名的映射
    _found_node_objects: Dict[str, Any] = {}  # 缓存已找到的 ua.Node 对象

    def __init__(self):
        super().__init__()
        # 自动查找节点功能默认开启
        self._auto_find_nodes = True
        # 初始化名称映射字典
        self._name_mapping = {}
        self._reverse_mapping = {}
        # 初始化线程锁
        import threading
        self._client_lock = threading.RLock()

    def _set_client(self, client: Optional[Client]) -> None:
        if client is None:
            raise ValueError('client is not valid')
        self.client = client

    def _connect(self) -> None:
        logger.info('try to connect client...')
        if self.client:
            try:
                self.client.connect()
                logger.info('client connected!')
                
                # 连接后开始查找节点
                if self._variables_to_find:
                    self._find_nodes()
            except Exception as e:
                logger.error(f'client connect failed: {e}')
                raise
        else:
            raise ValueError('client is not initialized')
    
    def _find_nodes(self) -> None:
        """查找服务器中的节点（通过NodeID直接获取）"""
        if not self.client:
            raise ValueError('client is not connected')
            
        logger.info(f'开始查找 {len(self._variables_to_find)} 个节点...')
        try:
            # 记录查找前的状态
            before_count = len(self._node_registry)
            
            # 通过NodeID直接查找节点
            for var_name, var_info in self._variables_to_find.items():
                if var_name in self._node_registry:
                    continue  # 已经找到的节点跳过
                    
                node_id = var_info.get("node_id")
                if not node_id:
                    logger.warning(f"节点 '{var_name}' 缺少NodeID，跳过")
                    continue
                    
                try:
                    # 通过NodeID直接获取节点
                    node = self.client.get_node(node_id)
                    
                    # 验证节点是否存在（通过读取浏览名称）
                    browse_name = node.get_browse_name()
                    
                    node_type = var_info.get("node_type")
                    data_type = var_info.get("data_type")
                    node_id_str = str(node.nodeid)
                    
                    # 根据节点类型创建相应的对象
                    if node_type == NodeType.VARIABLE:
                        self._node_registry[var_name] = Variable(self.client, var_name, node_id_str, data_type)
                        logger.debug(f"✓ 找到变量节点: '{var_name}', NodeId: {node_id_str}, DataType: {data_type}")
                        # 缓存真实的 ua.Node 对象用于订阅
                        self._found_node_objects[var_name] = node
                    elif node_type == NodeType.METHOD:
                        # 对于方法节点，需要获取父节点ID
                        parent_node = node.get_parent()
                        parent_node_id = str(parent_node.nodeid)
                        self._node_registry[var_name] = Method(self.client, var_name, node_id_str, parent_node_id, data_type)
                        logger.debug(f"✓ 找到方法节点: '{var_name}', NodeId: {node_id_str}, ParentId: {parent_node_id}")
                        
                except Exception as e:
                    logger.warning(f"无法获取节点 '{var_name}' (NodeId: {node_id}): {e}")
                    continue
            
            # 记录查找后的状态
            after_count = len(self._node_registry)
            newly_found = after_count - before_count
            
            logger.info(f"本次查找新增 {newly_found} 个节点，当前共 {after_count} 个")
            
            # 检查是否所有节点都已找到
            not_found = []
            for var_name, var_info in self._variables_to_find.items():
                if var_name not in self._node_registry:
                    not_found.append(var_name)
            
            if not_found:
                logger.warning(f"⚠ 以下 {len(not_found)} 个节点未找到: {', '.join(not_found[:10])}{'...' if len(not_found) > 10 else ''}")
                logger.warning(f"提示：请检查这些节点的NodeID是否正确")
            else:
                logger.info(f"✓ 所有 {len(self._variables_to_find)} 个节点均已找到并注册")
                
        except Exception as e:
            logger.error(f"查找节点失败: {e}")
            traceback.print_exc()

    @classmethod
    def load_csv(cls, file_path: str) -> Tuple[List[OpcUaNode], Dict[str, str], Dict[str, str]]:
        """
        从CSV文件加载节点定义
        CSV文件需包含Name,NodeType,DataType列
        可选包含EnglishName,NodeLanguage和NodeId列
        
        返回: (节点列表, 英文到中文映射, 中文到英文映射)
        """
        df = pd.read_csv(file_path)
        df = df.drop_duplicates(subset='Name', keep='first')
        nodes = []
        
        # 检查是否包含英文名称列、节点语言列和NodeId列
        has_english_name = 'EnglishName' in df.columns
        has_node_language = 'NodeLanguage' in df.columns
        has_node_id = 'NodeId' in df.columns
        
        # 如果存在英文名称列，创建名称映射字典
        name_mapping = {}
        reverse_mapping = {}
        
        for _, row in df.iterrows():
            name = row.get('Name')
            node_type_str = row.get('NodeType')
            data_type_str = row.get('DataType')
            
            # 获取英文名称、节点语言和NodeId（如果有）
            english_name = row.get('EnglishName') if has_english_name else None
            node_language = row.get('NodeLanguage') if has_node_language else 'English'
            node_id = row.get('NodeId') if has_node_id else None
            
            # 如果有英文名称，添加到映射字典
            if english_name and not pd.isna(english_name) and node_language == 'Chinese':
                name_mapping[english_name] = name
                reverse_mapping[name] = english_name
            
            if not name or not node_type_str:
                logger.warning(f"跳过无效行: 名称或节点类型缺失")
                continue
                
            # 只支持VARIABLE和METHOD两种类型
            if node_type_str not in ['VARIABLE', 'METHOD']:
                logger.warning(f"不支持的节点类型: {node_type_str}，仅支持VARIABLE和METHOD")
                continue
                
            try:
                node_type = NodeType[node_type_str]
            except KeyError:
                logger.warning(f"无效的节点类型: {node_type_str}")
                continue
                
            # 对于VARIABLE节点，必须指定数据类型
            if node_type == NodeType.VARIABLE:
                if not data_type_str or pd.isna(data_type_str):
                    logger.warning(f"变量节点 {name} 必须指定数据类型")
                    continue
                    
                try:
                    data_type = DataType[data_type_str]
                except KeyError:
                    logger.warning(f"无效的数据类型: {data_type_str}")
                    continue
            else:
                # 对于METHOD节点，数据类型可选
                data_type = None
                if data_type_str and not pd.isna(data_type_str):
                    try:
                        data_type = DataType[data_type_str]
                    except KeyError:
                        logger.warning(f"无效的数据类型: {data_type_str}，将使用默认值")
            
            # 处理NodeId（如果有的话）
            node_id_value = ""
            if node_id and not pd.isna(node_id):
                node_id_value = str(node_id).strip()
            
            # 创建节点对象
            nodes.append(OpcUaNode(
                name=name,
                node_type=node_type,
                node_id=node_id_value,
                data_type=data_type
            ))
            
        return nodes, name_mapping, reverse_mapping

    def use_node(self, name: str) -> OpcUaNodeBase:
        """
        获取已注册的节点
        如果节点尚未找到，会尝试再次查找
        支持使用英文名称访问中文节点
        """
        # 检查是否使用英文名称访问中文节点
        if name in self._name_mapping:
            chinese_name = self._name_mapping[name]
            if chinese_name in self._node_registry:
                node = self._node_registry[chinese_name]
                logger.debug(f"使用节点: '{name}' -> '{chinese_name}', NodeId: {node.node_id}")
                return node
            elif chinese_name in self._variables_to_find:
                logger.warning(f"节点 {chinese_name} (英文名: {name}) 尚未找到，尝试重新查找")
                if self.client:
                    self._find_nodes()
                    if chinese_name in self._node_registry:
                        node = self._node_registry[chinese_name]
                        logger.info(f"重新查找成功: '{chinese_name}', NodeId: {node.node_id}")
                        return node
                raise ValueError(f'节点 {chinese_name} (英文名: {name}) 未注册或未找到')
        
        # 直接使用原始名称查找
        if name not in self._node_registry:
            if name in self._variables_to_find:
                logger.warning(f"节点 {name} 尚未找到，尝试重新查找")
                if self.client:
                    self._find_nodes()
                    if name in self._node_registry:
                        node = self._node_registry[name]
                        logger.info(f"重新查找成功: '{name}', NodeId: {node.node_id}")
                        return node
            logger.error(f"❌ 节点 '{name}' 未注册或未找到。已注册节点: {list(self._node_registry.keys())[:5]}...")
            raise ValueError(f'节点 {name} 未注册或未找到')
        node = self._node_registry[name]
        logger.debug(f"使用节点: '{name}', NodeId: {node.node_id}")
        return node

    def get_node_registry(self) -> Dict[str, OpcUaNodeBase]:
        """获取所有已注册的节点"""
        return self._node_registry

    def register_node_list_from_csv_path(self, path: str = None) -> "BaseOpcUaClient":
        """从CSV文件注册节点"""
        if path is None:
            path = self.DEFAULT_ADDRESS_PATH
        nodes, name_mapping, reverse_mapping = self.load_csv(path)
        self._name_mapping.update(name_mapping)
        self._reverse_mapping.update(reverse_mapping)
        return self.register_node_list(nodes)

    def register_node_list(self, node_list: List[OpcUaNode]) -> "BaseOpcUaClient":
        """注册节点列表"""
        if not node_list or len(node_list) == 0:
            logger.warning('节点列表为空')
            return self

        logger.info(f'开始注册 {len(node_list)} 个节点...')
        new_nodes_count = 0
        for node in node_list:
            if node is None:
                continue
                
            if node.name in self._node_registry:
                logger.debug(f'节点 "{node.name}" 已存在于注册表')
                exist = self._node_registry[node.name]
                if exist.type != node.node_type:
                    raise ValueError(f'节点 {node.name} 类型 {node.node_type} 与已存在的类型 {exist.type} 不一致')
                continue
                
            # 将节点添加到待查找列表，包括node_id
            self._variables_to_find[node.name] = {
                "node_type": node.node_type,
                "data_type": node.data_type,
                "node_id": node.node_id
            }
            new_nodes_count += 1
            logger.debug(f'添加节点 "{node.name}" ({node.node_type}, NodeId: {node.node_id}) 到待查找列表')

        logger.info(f'节点注册完成：新增 {new_nodes_count} 个待查找节点，总计 {len(self._variables_to_find)} 个')
        
        # 如果客户端已连接，立即开始查找
        if self.client:
            self._find_nodes()
            
        return self

    def read_node(self, node_name: str) -> str:
        """
        读取节点值的便捷方法
        返回JSON格式字符串
        """
        with self._client_lock:
            try:
                node = self.use_node(node_name)
                value, error = node.read()
                
                result = {
                    "value": value,
                    "error": error,
                    "node_name": node_name,
                    "timestamp": time.time()
                }
                
                return json.dumps(result)
            except Exception as e:
                logger.error(f"读取节点 {node_name} 失败: {e}")
                result = {
                    "value": None,
                    "error": True,
                    "node_name": node_name,
                    "error_message": str(e),
                    "timestamp": time.time()
                }
                return json.dumps(result)
            
    def write_node(self, json_input: str) -> str:
        """
        写入节点值的便捷方法
        接受JSON格式的字符串作为输入: '{"node_name": "节点名", "value": 值}'
        返回JSON格式的字符串
        """
        with self._client_lock:
            try:
                if not isinstance(json_input, str):
                    json_input = str(json_input)
                    
                try:
                    input_data = json.loads(json_input)
                    if not isinstance(input_data, dict):
                        return json.dumps({"error": True, "error_message": "输入必须是包含node_name和value的JSON对象", "success": False})
                        
                    node_name = input_data.get("node_name")
                    value = input_data.get("value")
                    
                    if node_name is None:
                        return json.dumps({"error": True, "error_message": "JSON中缺少node_name字段", "success": False})
                except json.JSONDecodeError as e:
                    return json.dumps({"error": True, "error_message": f"JSON解析错误: {str(e)}", "success": False})
                
                node = self.use_node(node_name)
                error = node.write(value)
                
                result = {
                    "value": value,
                    "error": error,
                    "node_name": node_name,
                    "timestamp": time.time(),
                    "success": not error
                }
                
                return json.dumps(result)
            except Exception as e:
                logger.error(f"写入节点失败: {e}")
                result = {
                    "error": True,
                    "error_message": str(e),
                    "timestamp": time.time(),
                    "success": False
                }
                return json.dumps(result)
            
    def call_method(self, node_name: str, *args) -> Tuple[Any, bool]:
        """
        调用方法节点的便捷方法
        返回 (返回值, 是否出错)
        """
        try:
            node = self.use_node(node_name)
            if hasattr(node, 'call'):
                return node.call(*args)
            else:
                logger.error(f"节点 {node_name} 不是方法节点")
                return None, True
        except Exception as e:
            logger.error(f"调用方法 {node_name} 失败: {e}")
            return None, True


class OpcUaClientWithSubscription(BaseOpcUaClient):
    """
    带订阅和缓存功能的 OPC UA 客户端
    在 BaseOpcUaClient 的基础上增加了：
    - 订阅机制
    - 缓存机制
    - 连接监控
    """
    
    def __init__(
        self, 
        url: str,
        username: str = None, 
        password: str = None,
        use_subscription: bool = True,
        cache_timeout: float = 5.0,
        subscription_interval: int = 500,
        *args,
        **kwargs,
    ):
        # 降低OPCUA库的日志级别
        import logging
        logging.getLogger("opcua").setLevel(logging.WARNING)
        
        super().__init__()
        
        # OPC UA 客户端初始化
        client = Client(url)
        
        if username and password:
            client.set_user(username)
            client.set_password(password)
            
        self._set_client(client)

        # 订阅相关属性
        self._use_subscription = use_subscription
        self._subscription = None
        self._subscription_handles = {}
        self._subscription_interval = subscription_interval
        
        # 缓存相关属性
        self._node_values = {}
        self._cache_timeout = cache_timeout
        
        # 连接状态监控
        self._connection_check_interval = 30.0
        self._connection_monitor_running = False
        self._connection_monitor_thread = None
        
        # 连接到服务器
        self._connect()
        
        # 启动连接监控
        self._start_connection_monitor()
        

    def _connect(self) -> None:
        """连接到OPC UA服务器"""
        logger.info('尝试连接到 OPC UA 服务器...')
        if self.client:
            try:
                self.client.connect()
                logger.info('✓ 客户端已连接!')
                
                # 连接后开始查找节点
                if self._variables_to_find:
                    self._find_nodes()
                    
                # 如果启用订阅模式，设置订阅
                if self._use_subscription:
                    self._setup_subscriptions()
                else:
                    logger.info("订阅模式已禁用，将使用按需读取模式")
                    
            except Exception as e:
                logger.error(f'客户端连接失败: {e}')
                raise
        else:
            raise ValueError('客户端未初始化')
    
    class SubscriptionHandler:
        """订阅处理器"""
        def __init__(self, outer):
            self.outer = outer

        def datachange_notification(self, node, val, data):
            try:
                self.outer._on_subscription_datachange(node, val, data)
            except Exception as e:
                logger.error(f"订阅数据回调处理失败: {e}")

        def event_notification(self, event):
            pass

    def _setup_subscriptions(self):
        """设置 OPC UA 订阅"""
        if not self.client or not self._use_subscription:
            return
            
        with self._client_lock:
            try:
                logger.info(f"开始设置订阅 (发布间隔: {self._subscription_interval}ms)...")
                
                # 创建订阅
                handler = OpcUaClientWithSubscription.SubscriptionHandler(self)
                self._subscription = self.client.create_subscription(
                    self._subscription_interval,
                    handler
                )
                
                # 为所有变量节点创建监控项
                subscribed_count = 0
                skipped_count = 0
                
                for node_name, node in self._node_registry.items():
                    if node.type == NodeType.VARIABLE and node.node_id:
                        try:
                            ua_node = self._found_node_objects.get(node_name)
                            if ua_node is None:
                                ua_node = self.client.get_node(node.node_id)
                            handle = self._subscription.subscribe_data_change(ua_node)
                            self._subscription_handles[node_name] = handle
                            subscribed_count += 1
                            logger.debug(f"✓ 已订阅节点: {node_name}")
                        except Exception as e:
                            skipped_count += 1
                            logger.warning(f"✗ 订阅节点 {node_name} 失败: {e}")
                    else:
                        skipped_count += 1
                        
                logger.info(f"订阅设置完成: 成功 {subscribed_count} 个, 跳过 {skipped_count} 个")
                
            except Exception as e:
                logger.error(f"设置订阅失败: {e}")
                traceback.print_exc()
                self._use_subscription = False
                logger.warning("订阅模式设置失败，已自动切换到按需读取模式")
    
    def _on_subscription_datachange(self, node, val, data):
        """订阅数据变化处理器"""
        try:
            node_id = str(node.nodeid)
            current_time = time.time()
            for node_name, node_obj in self._node_registry.items():
                if node_obj.node_id == node_id:
                    self._node_values[node_name] = {
                        'value': val,
                        'timestamp': current_time,
                        'source': 'subscription'
                    }
                    logger.debug(f"订阅更新: {node_name} = {val}")
                    break
        except Exception as e:
            logger.error(f"处理订阅数据失败: {e}")
    
    def get_node_value(self, name, use_cache=True, force_read=False):
        """获取节点值（智能缓存版本）"""
        # 处理名称映射
        if name in self._name_mapping:
            chinese_name = self._name_mapping[name]
        elif name in self._node_registry:
            chinese_name = name
        else:
            raise ValueError(f"未找到名称为 '{name}' 的节点")
        
        # 如果强制读取，直接从服务器读取
        if force_read:
            with self._client_lock:
                value, _ = self.use_node(chinese_name).read()
                self._node_values[chinese_name] = {
                    'value': value,
                    'timestamp': time.time(),
                    'source': 'forced_read'
                }
                return value
        
        # 检查缓存
        if use_cache and chinese_name in self._node_values:
            cache_entry = self._node_values[chinese_name]
            cache_age = time.time() - cache_entry['timestamp']
            
            if cache_entry.get('source') == 'subscription' or cache_age < self._cache_timeout:
                logger.debug(f"从缓存读取: {chinese_name} = {cache_entry['value']} (age: {cache_age:.2f}s, source: {cache_entry.get('source', 'unknown')})")
                return cache_entry['value']
        
        # 缓存过期或不存在，从服务器读取
        with self._client_lock:
            try:
                value, error = self.use_node(chinese_name).read()
                if not error:
                    self._node_values[chinese_name] = {
                        'value': value,
                        'timestamp': time.time(),
                        'source': 'on_demand_read'
                    }
                    return value
                else:
                    logger.warning(f"读取节点 {chinese_name} 失败")
                    return None
            except Exception as e:
                logger.error(f"读取节点 {chinese_name} 出错: {e}")
                return None
    
    def set_node_value(self, name, value):
        """设置节点值"""
        # 处理名称映射
        if name in self._name_mapping:
            chinese_name = self._name_mapping[name]
        elif name in self._node_registry:
            chinese_name = name
        else:
            raise ValueError(f"未找到名称为 '{name}' 的节点")
        
        with self._client_lock:
            try:
                node = self.use_node(chinese_name)
                error = node.write(value)
                
                if not error:
                    self._node_values[chinese_name] = {
                        'value': value,
                        'timestamp': time.time(),
                        'source': 'write'
                    }
                    logger.debug(f"写入成功: {chinese_name} = {value}")
                    return True
                else:
                    logger.warning(f"写入节点 {chinese_name} 失败")
                    return False
            except Exception as e:
                logger.error(f"写入节点 {chinese_name} 出错: {e}")
                return False
    
    def _check_connection(self) -> bool:
        """检查连接状态"""
        try:
            with self._client_lock:
                if self.client:
                    self.client.get_namespace_array()
                    return True
        except Exception as e:
            logger.warning(f"连接检查失败: {e}")
            return False
        return False
    
    def _connection_monitor_worker(self):
        """连接监控线程工作函数"""
        self._connection_monitor_running = True
        logger.info(f"连接监控线程已启动 (检查间隔: {self._connection_check_interval}秒)")
        
        reconnect_attempts = 0
        max_reconnect_attempts = 5
        
        while self._connection_monitor_running:
            try:
                if not self._check_connection():
                    logger.warning("检测到连接断开，尝试重新连接...")
                    reconnect_attempts += 1
                    
                    if reconnect_attempts <= max_reconnect_attempts:
                        try:
                            with self._client_lock:
                                if self.client:
                                    try:
                                        self.client.disconnect()
                                    except:
                                        pass
                                    
                                    self.client.connect()
                                    logger.info("✓ 重新连接成功")
                                    
                                    if self._use_subscription:
                                        self._setup_subscriptions()
                                    
                                    reconnect_attempts = 0
                        except Exception as e:
                            logger.error(f"重新连接失败 (尝试 {reconnect_attempts}/{max_reconnect_attempts}): {e}")
                            time.sleep(5)
                    else:
                        logger.error(f"达到最大重连次数 ({max_reconnect_attempts})，停止重连")
                        self._connection_monitor_running = False
                else:
                    reconnect_attempts = 0
                
            except Exception as e:
                logger.error(f"连接监控出错: {e}")
            
            time.sleep(self._connection_check_interval)
    
    def _start_connection_monitor(self):
        """启动连接监控线程"""
        if self._connection_monitor_thread is not None and self._connection_monitor_thread.is_alive():
            logger.warning("连接监控线程已在运行")
            return
            
        import threading
        self._connection_monitor_thread = threading.Thread(
            target=self._connection_monitor_worker, 
            daemon=True,
            name="OpcUaConnectionMonitor"
        )
        self._connection_monitor_thread.start()
    
    def _stop_connection_monitor(self):
        """停止连接监控线程"""
        self._connection_monitor_running = False
        if self._connection_monitor_thread and self._connection_monitor_thread.is_alive():
            self._connection_monitor_thread.join(timeout=2.0)
            logger.info("连接监控线程已停止")
    
    def read_node(self, node_name: str) -> str:
        """读取节点值的便捷方法（使用缓存）"""
        try:
            value = self.get_node_value(node_name, use_cache=True)
            
            chinese_name = self._name_mapping.get(node_name, node_name)
            cache_info = self._node_values.get(chinese_name, {})
            
            result = {
                "value": value,
                "error": False,
                "node_name": node_name,
                "timestamp": time.time(),
                "cache_age": time.time() - cache_info.get('timestamp', time.time()),
                "source": cache_info.get('source', 'unknown')
            }
            
            return json.dumps(result)
        except Exception as e:
            logger.error(f"读取节点 {node_name} 失败: {e}")
            result = {
                "value": None,
                "error": True,
                "node_name": node_name,
                "error_message": str(e),
                "timestamp": time.time()
            }
            return json.dumps(result)

    def get_cache_stats(self) -> Dict[str, Any]:
        """获取缓存统计信息"""
        current_time = time.time()
        stats = {
            'total_cached_nodes': len(self._node_values),
            'subscription_nodes': 0,
            'on_demand_nodes': 0,
            'expired_nodes': 0,
            'cache_timeout': self._cache_timeout,
            'using_subscription': self._use_subscription
        }
        
        for node_name, cache_entry in self._node_values.items():
            source = cache_entry.get('source', 'unknown')
            cache_age = current_time - cache_entry['timestamp']
            
            if source == 'subscription':
                stats['subscription_nodes'] += 1
            elif source in ['on_demand_read', 'forced_read', 'write']:
                stats['on_demand_nodes'] += 1
                
            if cache_age > self._cache_timeout:
                stats['expired_nodes'] += 1
        
        return stats
    
    def print_cache_stats(self):
        """打印缓存统计信息"""
        stats = self.get_cache_stats()
        print("\n" + "="*80)
        print("缓存统计信息")
        print("="*80)
        print(f"总缓存节点数: {stats['total_cached_nodes']}")
        print(f"订阅模式: {'启用' if stats['using_subscription'] else '禁用'}")
        print(f"  - 订阅更新节点: {stats['subscription_nodes']}")
        print(f"  - 按需读取节点: {stats['on_demand_nodes']}")
        print(f"  - 已过期节点: {stats['expired_nodes']}")
        print(f"缓存超时时间: {stats['cache_timeout']}秒")
        print("="*80 + "\n")
    
    def load_nodes_from_csv(self, csv_path: str) -> None:
        """直接从CSV文件加载并注册节点"""
        try:
            logger.info(f"开始从CSV文件加载节点: {csv_path}")
            
            # 如果是相对路径，转换为绝对路径
            if not os.path.isabs(csv_path):
                current_dir = os.path.dirname(os.path.abspath(__file__))
                csv_path = os.path.join(current_dir, csv_path)
                logger.info(f"相对路径已转换为绝对路径: {csv_path}")
            
            # 检查文件是否存在
            if not os.path.exists(csv_path):
                logger.error(f"CSV文件不存在: {csv_path}")
                return
            
            # 注册节点
            logger.info(f"注册CSV文件中的节点: {csv_path}")
            self.register_node_list_from_csv_path(path=csv_path)
            
            # 查找节点
            if self.client and self._variables_to_find:
                logger.info(f"CSV加载完成，待查找 {len(self._variables_to_find)} 个节点...")
                self._find_nodes()
            else:
                logger.warning(f"⚠ 跳过节点查找 - client: {self.client is not None}, 待查找节点: {len(self._variables_to_find)}")
            
            # 将所有节点注册为属性
            self._register_nodes_as_attributes()
            
            # 打印统计信息
            found_count = len(self._node_registry)
            total_count = len(self._variables_to_find)
            if found_count < total_count:
                logger.warning(f"节点查找完成：找到 {found_count}/{total_count} 个节点")
            else:
                logger.info(f"✓ 节点查找完成：所有 {found_count} 个节点均已找到")
            
            # 如果使用订阅模式，设置订阅
            if self._use_subscription and found_count > 0:
                self._setup_subscriptions()
                
            logger.info(f"✓ 成功从 CSV 加载 {found_count} 个节点")
        except Exception as e:
            logger.error(f"从CSV文件加载节点失败 {csv_path}: {e}")
            traceback.print_exc()
    
    def disconnect(self):
        """断开连接并清理资源"""
        logger.info("正在断开连接...")
        
        # 停止连接监控
        self._stop_connection_monitor()
        
        # 删除订阅
        if self._subscription:
            try:
                with self._client_lock:
                    self._subscription.delete()
                    logger.info("订阅已删除")
            except Exception as e:
                logger.warning(f"删除订阅失败: {e}")
        
        # 断开客户端连接
        if self.client:
            try:
                with self._client_lock:
                    self.client.disconnect()
                logger.info("✓ OPC UA 客户端已断开连接")
            except Exception as e:
                logger.error(f"断开连接失败: {e}")
    
    def _register_nodes_as_attributes(self):
        """将所有节点注册为实例属性"""
        for node_name, node in self._node_registry.items():
            if not node.node_id or node.node_id == "":
                logger.warning(f"⚠ 节点 '{node_name}' 的 node_id 为空，跳过注册为属性")
                continue
                
            eng_name = self._reverse_mapping.get(node_name)
            attr_name = eng_name if eng_name else node_name.replace(' ', '_').replace('-', '_')
            
            def create_property_getter(node_key):
                def getter(self):
                    return self.get_node_value(node_key, use_cache=True)
                return getter
            
            setattr(OpcUaClientWithSubscription, attr_name, property(create_property_getter(node_name)))
            logger.debug(f"已注册节点 '{node_name}' 为属性 '{attr_name}'")
