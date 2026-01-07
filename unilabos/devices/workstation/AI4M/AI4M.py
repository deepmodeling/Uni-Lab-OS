import json
import time
import traceback
from typing import Any, Union, List, Dict, Callable, Optional, Tuple
from pydantic import BaseModel

from opcua import Client, ua
import pandas as pd
import os

from unilabos.device_comms.opcua_client.node.uniopcua import Base as OpcUaNodeBase
from unilabos.device_comms.opcua_client.node.uniopcua import Variable, Method, NodeType, DataType
from unilabos.device_comms.universal_driver import UniversalDriver
from unilabos.resources.resource_tracker import ResourceTreeSet
from unilabos.utils.log import logger
from unilabos.devices.workstation.AI4M.decks import AI4M_deck

class OpcUaNode(BaseModel):
    name: str
    node_type: NodeType
    node_id: str = ""
    data_type: Optional[DataType] = None
    parent_node_id: Optional[str] = None


class OpcUaWorkflow(BaseModel):
    name: str
    actions: List[
        Union[
            "OpcUaWorkflow",
            Callable[
                [Callable[[str], OpcUaNodeBase]],
                None
            ]]
    ]


class Action(BaseModel):
    name: str
    rw: bool  # read是0 write是1


class WorkflowAction(BaseModel):
    init: Optional[Callable[[Callable[[str], OpcUaNodeBase]], bool]] = None
    start: Optional[Callable[[Callable[[str], OpcUaNodeBase]], bool]] = None
    stop: Optional[Callable[[Callable[[str], OpcUaNodeBase]], bool]] = None
    cleanup: Optional[Callable[[Callable[[str], OpcUaNodeBase]], None]] = None


class OpcUaWorkflowModel(BaseModel):
    name: str
    actions: List[Union["OpcUaWorkflowModel", WorkflowAction]]
    parameters: Optional[List[str]] = None
    description: Optional[str] = None


""" 前后端Json解析用 """
class NodeFunctionJson(BaseModel):
    func_name: str
    node_name: str
    mode: str  # read, write, call
    value: Any = None


class ExecuteProcedureJson(BaseModel):
    register_node_list_from_csv_path: Optional[Dict[str, Any]] = None
#    create_flow: List[WorkflowCreateJson]
    execute_flow: List[str]


class BaseClient(UniversalDriver):
    client: Optional[Client] = None
    _node_registry: Dict[str, OpcUaNodeBase] = {}
    DEFAULT_ADDRESS_PATH = ""
    _variables_to_find: Dict[str, Dict[str, Any]] = {}
    _name_mapping: Dict[str, str] = {}  # 英文名到中文名的映射
    _reverse_mapping: Dict[str, str] = {}  # 中文名到英文名的映射
    # 直接缓存已找到的 ua.Node 对象，避免因字符串 NodeId 格式导致订阅失败
    _found_node_objects: Dict[str, Any] = {}

    def __init__(self):
        super().__init__()
        # 自动查找节点功能默认开启
        self._auto_find_nodes = True
        # 初始化名称映射字典
        self._name_mapping = {}
        self._reverse_mapping = {}
        # 初始化线程锁（在子类中会被重新创建，这里提供默认实现）
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
    def load_csv(cls, file_path: str) -> List[OpcUaNode]:
        """
        从CSV文件加载节点定义
        CSV文件需包含Name,NodeType,DataType列
        可选包含EnglishName,NodeLanguage和NodeId列
        """
        df = pd.read_csv(file_path)
        df = df.drop_duplicates(subset='Name', keep='first')  # 重复的数据应该报错
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
            node_language = row.get('NodeLanguage') if has_node_language else 'English'  # 默认为英文
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
            
            # 创建节点对象，如果有NodeId则使用，否则留空
            nodes.append(OpcUaNode(
                name=name,
                node_type=node_type,
                node_id=node_id_value,
                data_type=data_type
            ))
            
        # 返回节点列表和名称映射字典
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
        return self._node_registry

    def register_node_list_from_csv_path(self, path: str = None) -> "BaseClient":
        """从CSV文件注册节点"""
        if path is None:
            path = self.DEFAULT_ADDRESS_PATH
        nodes, name_mapping, reverse_mapping = self.load_csv(path)
        self._name_mapping.update(name_mapping)
        self._reverse_mapping.update(reverse_mapping)
        return self.register_node_list(nodes)

    def register_node_list(self, node_list: List[OpcUaNode]) -> "BaseClient":
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

    def run_opcua_workflow(self, workflow: OpcUaWorkflow) -> None:
        if not self.client:
            raise ValueError('client is not connected')

        logger.info(f'start to run workflow {workflow.name}...')

        for action in workflow.actions:
            if isinstance(action, OpcUaWorkflow):
                self.run_opcua_workflow(action)
            elif callable(action):
                action(self.use_node)
            else:
                raise ValueError(f'invalid action {action}')

    def call_lifecycle_fn(
            self,
            workflow: OpcUaWorkflowModel,
            fn: Optional[Callable[[Callable], bool]],
    ) -> bool:
        if not fn:
            raise ValueError('fn is not valid in call_lifecycle_fn')
        try:
            result = fn(self.use_node)
            # 处理函数返回值可能是元组的情况
            if isinstance(result, tuple) and len(result) == 2:
                # 第二个元素是错误标志，True表示出错，False表示成功
                value, error_flag = result
                return not error_flag  # 转换成True表示成功，False表示失败
            return result
        except Exception as e:
            traceback.print_exc()
            logger.error(f'execute {workflow.name} lifecycle failed, err: {e}')
            return False

    def run_opcua_workflow_model(self, workflow: OpcUaWorkflowModel) -> bool:
        if not self.client:
            raise ValueError('client is not connected')

        logger.info(f'start to run workflow {workflow.name}...')

        for action in workflow.actions:
            if isinstance(action, OpcUaWorkflowModel):
                if self.run_opcua_workflow_model(action):
                    logger.info(f"{action.name} workflow done.")
                    continue
                else:
                    logger.error(f"{action.name} workflow failed")
                    return False
            elif isinstance(action, WorkflowAction):
                init = action.init
                start = action.start
                stop = action.stop
                cleanup = action.cleanup
                if not init and not start and not stop:
                    raise ValueError(f'invalid action {action}')

                is_err = False
                try:
                    if init and not self.call_lifecycle_fn(workflow, init):
                        raise ValueError(f"{workflow.name} init action failed")
                    if not self.call_lifecycle_fn(workflow, start):
                        raise ValueError(f"{workflow.name} start action failed")
                    if not self.call_lifecycle_fn(workflow, stop):
                        raise ValueError(f"{workflow.name} stop action failed")
                    logger.info(f"{workflow.name} action done.")
                except Exception as e:
                    is_err = True
                    traceback.print_exc()
                    logger.error(f"{workflow.name} action failed, err: {e}")
                finally:
                    logger.info(f"{workflow.name} try to run cleanup")
                    if cleanup:
                        self.call_lifecycle_fn(workflow, cleanup)
                    else:
                        logger.info(f"{workflow.name} cleanup is not defined")
                    if is_err:
                        return False
                    return True
            else:
                raise ValueError(f'invalid action type {type(action)}')

        return True

    function_name: Dict[str, Callable[[Callable[[str], OpcUaNodeBase]], bool]] = {}

    def create_node_function(self, func_name: str = None, node_name: str = None, mode: str = None, value: Any = None, **kwargs) -> Callable[[Callable[[str], OpcUaNodeBase]], bool]:
        def execute_node_function(use_node: Callable[[str], OpcUaNodeBase]) -> Union[bool, Tuple[Any, bool]]:
            target_node = use_node(node_name)
            
            # 检查是否有对应的参数值可用
            current_value = value
            if hasattr(self, '_workflow_params') and func_name in self._workflow_params:
                current_value = self._workflow_params[func_name]
                print(f"使用参数值 {func_name} = {current_value}")
            else:
                print(f"执行 {node_name}, {type(target_node).__name__}, {target_node.node_id}, {mode}, {current_value}")
            
            if mode == 'read':
                result_str = self.read_node(node_name)
                
                try:
                    # 将字符串转换为字典
                    result_str = result_str.replace("'", '"')  # 替换单引号为双引号以便JSON解析
                    result_dict = json.loads(result_str)
                    
                    # 从字典获取值和错误标志
                    val = result_dict.get("value")
                    err = result_dict.get("error")
                    
                    print(f"读取 {node_name} 返回值 = {val} (类型: {type(val).__name__}, 错误 = {err}")
                    return val, err
                except Exception as e:
                    print(f"解析读取结果失败: {e}, 原始结果: {result_str}")
                    return None, True
            elif mode == 'write':
                # 构造完整的JSON输入，包含node_name和value
                input_json = json.dumps({"node_name": node_name, "value": current_value})
                result_str = self.write_node(input_json)
                
                try:
                    # 解析返回的字符串为字典
                    result_str = result_str.replace("'", '"')  # 替换单引号为双引号以便JSON解析
                    result = json.loads(result_str)
                    success = result.get("success", False)
                    print(f"写入 {node_name} = {current_value}, 结果 = {success}")
                    return success
                except Exception as e:
                    print(f"解析写入结果失败: {e}, 原始结果: {result_str}")
                    return False
            elif mode == 'call' and hasattr(target_node, 'call'):
                args = current_value if isinstance(current_value, list) else [current_value]
                result = target_node.call(*args)
                print(f"调用方法 {node_name} 参数 = {args}, 返回值 = {result}")
                return result
            return False
            
        if func_name is None:
            func_name = f"{node_name}_{mode}_{str(value)}"
            
        print(f"创建 node function: {mode}, {func_name}")
        self.function_name[func_name] = execute_node_function
        
        return execute_node_function
    
    def create_init_function(self, func_name: str = None, write_nodes: Union[Dict[str, Any], List[str]] = None):
        """
        创建初始化函数
        
        参数:
            func_name: 函数名称
            write_nodes: 写节点配置，可以是节点名列表[节点1,节点2]或节点值映射{节点1:值1,节点2:值2}
                         值可以是具体值，也可以是参数名称字符串（将从_workflow_params中查找）
        """
        if write_nodes is None:
            raise ValueError("必须提供write_nodes参数")

        def execute_init_function(use_node: Callable[[str], OpcUaNodeBase]) -> bool:
            """根据 _workflow_params 为各节点写入真实数值。

            约定:
            - write_nodes 为 list 时: 节点名 == 参数名，从 _workflow_params[node_name] 取值；
            - write_nodes 为 dict 时:
                * value 为字符串且在 _workflow_params 中: 当作参数名去取值；
                * 否则 value 视为常量直接写入。
            """

            params = getattr(self, "_workflow_params", {}) or {}

            if isinstance(write_nodes, list):
                # 节点列表形式: 节点名与参数名一致
                for node_name in write_nodes:
                    if node_name not in params:
                        print(f"初始化函数: 参数中未找到 {node_name}, 跳过写入")
                        continue

                    current_value = params[node_name]
                    print(f"初始化函数: 写入节点 {node_name} = {current_value}")
                    input_json = json.dumps({"node_name": node_name, "value": current_value})
                    result_str = self.write_node(input_json)
                    try:
                        result_str = result_str.replace("'", '"')
                        result = json.loads(result_str)
                        success = result.get("success", False)
                        print(f"初始化函数: 写入结果 = {success}")
                    except Exception as e:
                        print(f"初始化函数: 解析写入结果失败: {e}, 原始结果: {result_str}")
            elif isinstance(write_nodes, dict):
                # 映射形式: 节点名 -> 参数名或常量
                for node_name, node_value in write_nodes.items():
                    if isinstance(node_value, str) and node_value in params:
                        current_value = params[node_value]
                        print(f"初始化函数: 从参数获取值 {node_value} = {current_value}")
                    else:
                        current_value = node_value
                        print(f"初始化函数: 使用常量值 写入 {node_name} = {current_value}")

                    print(f"初始化函数: 写入节点 {node_name} = {current_value}")
                    input_json = json.dumps({"node_name": node_name, "value": current_value})
                    result_str = self.write_node(input_json)
                    try:
                        result_str = result_str.replace("'", '"')
                        result = json.loads(result_str)
                        success = result.get("success", False)
                        print(f"初始化函数: 写入结果 = {success}")
                    except Exception as e:
                        print(f"初始化函数: 解析写入结果失败: {e}, 原始结果: {result_str}")
            return True
                
        if func_name is None:
            func_name = f"init_function_{str(time.time())}"
            
        print(f"创建初始化函数: {func_name}")
        self.function_name[func_name] = execute_init_function
        return execute_init_function
    
    def create_stop_function(self, func_name: str = None, write_nodes: Union[Dict[str, Any], List[str]] = None):
        """
        创建停止函数
        
        参数:
            func_name: 函数名称
            write_nodes: 写节点配置，可以是节点名列表[节点1,节点2]或节点值映射{节点1:值1,节点2:值2}
        """
        if write_nodes is None:
            raise ValueError("必须提供write_nodes参数")
            
        def execute_stop_function(use_node: Callable[[str], OpcUaNodeBase]) -> bool:
            if isinstance(write_nodes, list):
                # 处理节点列表，默认值都是False
                for node_name in write_nodes:
                    # 直接写入False
                    print(f"停止函数: 写入节点 {node_name} = False")
                    input_json = json.dumps({"node_name": node_name, "value": False})
                    result_str = self.write_node(input_json)
                    try:
                        result_str = result_str.replace("'", '"')
                        result = json.loads(result_str)
                        success = result.get("success", False)
                        print(f"停止函数: 写入结果 = {success}")
                    except Exception as e:
                        print(f"停止函数: 解析写入结果失败: {e}, 原始结果: {result_str}")
            elif isinstance(write_nodes, dict):
                # 处理节点字典，使用指定的值
                for node_name, node_value in write_nodes.items():
                    print(f"停止函数: 写入节点 {node_name} = {node_value}")
                    input_json = json.dumps({"node_name": node_name, "value": node_value})
                    result_str = self.write_node(input_json)
                    try:
                        result_str = result_str.replace("'", '"')
                        result = json.loads(result_str)
                        success = result.get("success", False)
                        print(f"停止函数: 写入结果 = {success}")
                    except Exception as e:
                        print(f"停止函数: 解析写入结果失败: {e}, 原始结果: {result_str}")
            return True
                
        if func_name is None:
            func_name = f"stop_function_{str(time.time())}"
            
        print(f"创建停止函数: {func_name}")
        self.function_name[func_name] = execute_stop_function
        return execute_stop_function
        
    def create_cleanup_function(self, func_name: str = None, write_nodes: Union[Dict[str, Any], List[str]] = None):
        """
        创建清理函数
        
        参数:
            func_name: 函数名称
            write_nodes: 写节点配置，可以是节点名列表[节点1,节点2]或节点值映射{节点1:值1,节点2:值2}
        """
        if write_nodes is None:
            raise ValueError("必须提供write_nodes参数")
            
        def execute_cleanup_function(use_node: Callable[[str], OpcUaNodeBase]) -> bool:
            if isinstance(write_nodes, list):
                # 处理节点列表，默认值都是False
                for node_name in write_nodes:
                    # 直接写入False
                    print(f"清理函数: 写入节点 {node_name} = False")
                    input_json = json.dumps({"node_name": node_name, "value": False})
                    result_str = self.write_node(input_json)
                    try:
                        result_str = result_str.replace("'", '"')
                        result = json.loads(result_str)
                        success = result.get("success", False)
                        print(f"清理函数: 写入结果 = {success}")
                    except Exception as e:
                        print(f"清理函数: 解析写入结果失败: {e}, 原始结果: {result_str}")
            elif isinstance(write_nodes, dict):
                # 处理节点字典，使用指定的值
                for node_name, node_value in write_nodes.items():
                    print(f"清理函数: 写入节点 {node_name} = {node_value}")
                    input_json = json.dumps({"node_name": node_name, "value": node_value})
                    result_str = self.write_node(input_json)
                    try:
                        result_str = result_str.replace("'", '"')
                        result = json.loads(result_str)
                        success = result.get("success", False)
                        print(f"清理函数: 写入结果 = {success}")
                    except Exception as e:
                        print(f"清理函数: 解析写入结果失败: {e}, 原始结果: {result_str}")
            return True
                
        if func_name is None:
            func_name = f"cleanup_function_{str(time.time())}"
            
        print(f"创建清理函数: {func_name}")
        self.function_name[func_name] = execute_cleanup_function
        return execute_cleanup_function

    def create_start_function(self, func_name: str, stop_condition_expression: str = "True", write_nodes: Union[Dict[str, Any], List[str]] = None, condition_nodes: Union[Dict[str, str], List[str]] = None):
        """
        创建开始函数
        
        参数:
            func_name: 函数名称
            stop_condition_expression: 停止条件表达式，可直接引用节点名称
            write_nodes: 写节点配置，可以是节点名列表[节点1,节点2]或节点值映射{节点1:值1,节点2:值2}
            condition_nodes: 条件节点列表 [节点名1, 节点名2]
        """
        def execute_start_function(use_node: Callable[[str], OpcUaNodeBase]) -> bool:
            """开始函数: 写入触发节点, 然后轮询条件节点直到满足停止条件。"""

            params = getattr(self, "_workflow_params", {}) or {}

            # 先处理写入节点（触发位等）
            if write_nodes:
                if isinstance(write_nodes, list):
                    # 列表形式: 节点名与参数名一致, 若无参数则直接写 True
                    for node_name in write_nodes:
                        if node_name in params:
                            current_value = params[node_name]
                        else:
                            current_value = True

                        print(f"直接写入节点 {node_name} = {current_value}")
                        input_json = json.dumps({"node_name": node_name, "value": current_value})
                        result_str = self.write_node(input_json)
                        try:
                            result_str = result_str.replace("'", '"')
                            result = json.loads(result_str)
                            success = result.get("success", False)
                            print(f"直接写入 {node_name} = {current_value}, 结果: {success}")
                        except Exception as e:
                            print(f"解析直接写入结果失败: {e}, 原始结果: {result_str}")
                elif isinstance(write_nodes, dict):
                    # 字典形式: 节点名 -> 常量值(如 True/False)
                    for node_name, node_value in write_nodes.items():
                        if node_name in params:
                            current_value = params[node_name]
                        else:
                            current_value = node_value

                        print(f"直接写入节点 {node_name} = {current_value}")
                        input_json = json.dumps({"node_name": node_name, "value": current_value})
                        result_str = self.write_node(input_json)
                        try:
                            result_str = result_str.replace("'", '"')
                            result = json.loads(result_str)
                            success = result.get("success", False)
                            print(f"直接写入 {node_name} = {current_value}, 结果: {success}")
                        except Exception as e:
                            print(f"解析直接写入结果失败: {e}, 原始结果: {result_str}")
            
            # 如果没有条件节点，立即返回
            if not condition_nodes:
                return True
                
            # 处理条件检查和等待
            while True:
                next_loop = False
                condition_source = {}
                
                # 直接读取条件节点
                if isinstance(condition_nodes, list):
                    # 处理节点列表
                    for i, node_name in enumerate(condition_nodes):
                        # 直接读取节点
                        result_str = self.read_node(node_name)
                        try:
                            time.sleep(1)
                            result_str = result_str.replace("'", '"')
                            result_dict = json.loads(result_str)
                            read_res = result_dict.get("value")
                            read_err = result_dict.get("error", False)
                            print(f"直接读取 {node_name} 返回值 = {read_res}, 错误 = {read_err}")
                            
                            if read_err:
                                next_loop = True
                                break
                                
                            # 将节点值存入条件源字典，使用节点名称作为键
                            condition_source[node_name] = read_res
                            # 为了向后兼容，也保留read_i格式
                            condition_source[f"read_{i}"] = read_res
                        except Exception as e:
                            print(f"解析直接读取结果失败: {e}, 原始结果: {result_str}")
                            read_res, read_err = None, True
                            next_loop = True
                            break
                elif isinstance(condition_nodes, dict):
                    # 处理节点字典
                    for condition_func, node_name in condition_nodes.items():
                        # 直接读取节点
                        result_str = self.read_node(node_name)
                        try:
                            result_str = result_str.replace("'", '"')
                            result_dict = json.loads(result_str)
                            read_res = result_dict.get("value")
                            read_err = result_dict.get("error", False)
                            print(f"直接读取 {node_name} 返回值 = {read_res}, 错误 = {read_err}")
                            
                            if read_err:
                                next_loop = True
                                break
                                
                            # 将节点值存入条件源字典
                            condition_source[node_name] = read_res
                            # 也保存使用函数名作为键
                            condition_source[condition_func] = read_res
                        except Exception as e:
                            print(f"解析直接读取结果失败: {e}, 原始结果: {result_str}")
                            next_loop = True
                            break
                
                if not next_loop:
                    if stop_condition_expression:
                        # 添加调试信息
                        print(f"条件源数据: {condition_source}")
                        condition_source["__RESULT"] = None
                        
                        # 确保安全地执行条件表达式
                        try:
                            # 先尝试使用eval更安全的方式计算表达式
                            result = eval(stop_condition_expression, {}, condition_source)
                            condition_source["__RESULT"] = result
                        except Exception as e:
                            print(f"使用eval执行表达式失败: {e}")
                            try:
                                # 回退到exec方式
                                exec(f"__RESULT = {stop_condition_expression}", {}, condition_source)
                            except Exception as e2:
                                print(f"使用exec执行表达式也失败: {e2}")
                                condition_source["__RESULT"] = False
                                
                        res = condition_source["__RESULT"]
                        print(f"取得计算结果: {res}, 条件表达式: {stop_condition_expression}")
                        
                        if res:
                            print("满足停止条件，结束工作流")
                            break
                    else:
                        # 如果没有停止条件，直接退出
                        break
                else:
                    time.sleep(0.3)
                    
            return True
            
        self.function_name[func_name] = execute_start_function
        return execute_start_function

    create_action_from_json = None
    
    def create_action_from_json(self, data: Union[Dict, Any]) -> WorkflowAction:
        """
        从JSON配置创建工作流动作
        
        参数:
            data: 动作JSON数据
            
        返回:
            WorkflowAction对象
        """
        # 初始化所需变量
        start_function = None
        write_nodes = {}
        condition_nodes = []
        stop_function = None
        init_function = None
        cleanup_function = None
        
        # 提取start_function相关信息
        if hasattr(data, "start_function") and data.start_function:
            start_function = data.start_function
            if "write_nodes" in start_function:
                write_nodes = start_function["write_nodes"]
            if "condition_nodes" in start_function:
                condition_nodes = start_function["condition_nodes"]
        elif isinstance(data, dict) and data.get("start_function"):
            start_function = data.get("start_function")
            if "write_nodes" in start_function:
                write_nodes = start_function["write_nodes"]
            if "condition_nodes" in start_function:
                condition_nodes = start_function["condition_nodes"]
                
        # 提取stop_function信息
        if hasattr(data, "stop_function") and data.stop_function:
            stop_function = data.stop_function
        elif isinstance(data, dict) and data.get("stop_function"):
            stop_function = data.get("stop_function")
            
        # 提取init_function信息
        if hasattr(data, "init_function") and data.init_function:
            init_function = data.init_function
        elif isinstance(data, dict) and data.get("init_function"):
            init_function = data.get("init_function")
            
        # 提取cleanup_function信息
        if hasattr(data, "cleanup_function") and data.cleanup_function:
            cleanup_function = data.cleanup_function
        elif isinstance(data, dict) and data.get("cleanup_function"):
            cleanup_function = data.get("cleanup_function")
            
        # 创建工作流动作组件
        init = None
        start = None
        stop = None
        cleanup = None
        
        # 处理init function
        if init_function:
            init_params = {"func_name": init_function.get("func_name")}
            if "write_nodes" in init_function:
                init_params["write_nodes"] = init_function["write_nodes"]
            else:
                # 如果没有write_nodes，创建一个空字典
                init_params["write_nodes"] = {}
                
            init = self.create_init_function(**init_params)
            
        # 处理start function
        if start_function:
            start_params = {
                "func_name": start_function.get("func_name"),
                "stop_condition_expression": start_function.get("stop_condition_expression", "True"),
                "write_nodes": write_nodes,
                "condition_nodes": condition_nodes
            }
            start = self.create_start_function(**start_params)
            
        # 处理stop function
        if stop_function:
            stop_params = {
                "func_name": stop_function.get("func_name"),
                "write_nodes": stop_function.get("write_nodes", {})
            }
            stop = self.create_stop_function(**stop_params)
                
        # 处理cleanup function
        if cleanup_function:
            cleanup_params = {
                "func_name": cleanup_function.get("func_name"),
                "write_nodes": cleanup_function.get("write_nodes", {})
            }
            cleanup = self.create_cleanup_function(**cleanup_params)
                
        return WorkflowAction(init=init, start=start, stop=stop, cleanup=cleanup)
    
    workflow_name: Dict[str, OpcUaWorkflowModel] = {}

    def create_workflow_from_json(self, data: List[Dict]) -> None:
        """
        从JSON配置创建工作流程序
        
        参数:
            data: 工作流配置列表
        """
        for ind, flow_dict in enumerate(data):
            print(f"正在创建 workflow {ind}, {flow_dict['name']}")
            actions = []
            
            for i in flow_dict["action"]:
                if isinstance(i, str):
                    print(f"沿用已有 workflow 作为 action: {i}")
                    action = self.workflow_name[i]
                else:
                    print("创建 action")
                    # 直接将字典转换为SimplifiedActionJson对象或直接使用字典
                    action = self.create_action_from_json(i)
                    
                actions.append(action)
                
            # 获取参数
            parameters = flow_dict.get("parameters", [])
                
            flow_instance = OpcUaWorkflowModel(
                name=flow_dict["name"], 
                actions=actions,
                parameters=parameters,
                description=flow_dict.get("description", "")
            )
            print(f"创建完成 workflow: {flow_dict['name']}")
            self.workflow_name[flow_dict["name"]] = flow_instance

    def execute_workflow_from_json(self, data: List[str]) -> None:
        for i in data:
            print(f"正在执行 workflow: {i}")
            self.run_opcua_workflow_model(self.workflow_name[i])

    def execute_procedure_from_json(self, data: Union[ExecuteProcedureJson, Dict]) -> None:
        """从JSON配置执行工作流程序"""
        if isinstance(data, dict):
            # 处理字典类型
            register_params = data.get("register_node_list_from_csv_path")
            create_flow = data.get("create_flow", [])
            execute_flow = data.get("execute_flow", [])
        else:
            # 处理Pydantic模型类型
            register_params = data.register_node_list_from_csv_path
            create_flow = data.create_flow
            execute_flow = data.execute_flow if hasattr(data, "execute_flow") else []
            
        # 注册节点
        if register_params:
            print(f"注册节点 csv: {register_params}")
            self.register_node_list_from_csv_path(**register_params)
            
        # 创建工作流
        print("创建工作流")
        self.create_workflow_from_json(create_flow)
        
        # 注册工作流为实例方法
        self.register_workflows_as_methods()
        
        # 如果存在execute_flow字段，则执行指定的工作流（向后兼容）
        if execute_flow:
            print("执行工作流")
            self.execute_workflow_from_json(execute_flow)

    def register_workflows_as_methods(self) -> None:
        """将工作流注册为实例方法"""
        for workflow_name, workflow in self.workflow_name.items():
            # 获取工作流的参数信息（如果存在）
            workflow_params = getattr(workflow, 'parameters', []) or []
            workflow_desc = getattr(workflow, 'description', None) or f"执行工作流: {workflow_name}"
            
            # 创建执行工作流的方法
            def create_workflow_method(wf_name=workflow_name, wf=workflow, params=workflow_params):
                def workflow_method(*args, **kwargs):
                    logger.info(f"执行工作流: {wf_name}, 参数: {args}, {kwargs}")
                    
                    # 处理传入的参数
                    if params and (args or kwargs):
                        # 将位置参数转换为关键字参数
                        params_dict = {}
                        for i, param_name in enumerate(params):
                            if i < len(args):
                                params_dict[param_name] = args[i]
                                
                        # 合并关键字参数
                        params_dict.update(kwargs)
                        
                        # 保存参数，供节点函数使用
                        self._workflow_params = params_dict
                    else:
                        self._workflow_params = {}
                        
                    # 执行工作流
                    result = self.run_opcua_workflow_model(wf)
                    
                    # 清理参数
                    self._workflow_params = {}
                    
                    return result
                
                # 设置方法的文档字符串
                workflow_method.__doc__ = workflow_desc
                if params:
                    param_doc = ", ".join(params)
                    workflow_method.__doc__ += f"\n参数: {param_doc}"
                
                return workflow_method
            
            # 注册为实例方法
            method = create_workflow_method()
            setattr(self, workflow_name, method)
            logger.info(f"已将工作流 '{workflow_name}' 注册为实例方法")

    def read_node(self, node_name: str) -> Dict[str, Any]:
        """
        读取节点值的便捷方法
        返回包含result字段的字典
        """
        # 使用锁保护客户端访问
        with self._client_lock:
            try:
                node = self.use_node(node_name)
                value, error = node.read()
                
                # 创建结果字典
                result = {
                        "value": value,
                        "error": error,
                        "node_name": node_name,
                        "timestamp": time.time()
                }
                
                # 返回JSON字符串
                return json.dumps(result)
            except Exception as e:
                logger.error(f"读取节点 {node_name} 失败: {e}")
                # 创建错误结果字典
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
        接受单个JSON格式的字符串作为输入，包含节点名称和值
        eg:'{\"node_name\":\"反应罐号码\",\"value\":\"2\"}'
        返回JSON格式的字符串，包含操作结果
        """
        # 使用锁保护客户端访问
        with self._client_lock:
            try:
                # 解析JSON格式的输入
                if not isinstance(json_input, str):
                    json_input = str(json_input)
                    
                try:
                    input_data = json.loads(json_input)
                    if not isinstance(input_data, dict):
                        return json.dumps({"error": True, "error_message": "输入必须是包含node_name和value的JSON对象", "success": False})
                        
                    # 从JSON中提取节点名称和值
                    node_name = input_data.get("node_name")
                    value = input_data.get("value")
                    
                    if node_name is None:
                        return json.dumps({"error": True, "error_message": "JSON中缺少node_name字段", "success": False})
                except json.JSONDecodeError as e:
                    return json.dumps({"error": True, "error_message": f"JSON解析错误: {str(e)}", "success": False})
                
                node = self.use_node(node_name)
                error = node.write(value)
                
                # 创建结果字典
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


class OpcUaClient(BaseClient):
    def __init__(
        self, 
        url: str, 
        deck: Optional[AI4M_deck] = None,
        csv_path: str = None, 
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
        
        # ===== 关键修改：参照 BioyondWorkstation 处理 deck =====

        super().__init__()

        # 处理 deck 参数
        if deck is None or isinstance(deck["data"], dict) or len(deck["data"].children) == 0:
            self.deck = AI4M_deck(setup=True)
        else:
            # self.resource = ResourceTreeSet.from_nested_instance_list([deck["data"]])
            # self.deck = self.resource.to_plr_resources()
            self.deck = deck["data"]
        # elif isinstance(deck, dict):
        #     # 从 dict 中提取参数创建 deck
        #     deck_config = deck.get('config', {})
        #     deck_size_x = deck_config.get('size_x', 1217.0)
        #     deck_size_y = deck_config.get('size_y', 1580.0)
        #     deck_size_z = deck_config.get('size_z', 2670.0)
        #     self.deck = AI4M_deck(
        #         size_x=deck_size_x,
        #         size_y=deck_size_y,
        #         size_z=deck_size_z,
        #         setup=True
        #     )
        #     logger.info(f"Deck 尺寸设置: {deck_size_x}x{deck_size_y}x{deck_size_z} mm")
        # elif hasattr(deck, 'children'):
        #     self.deck = deck
        # else:
        #     raise ValueError(f"deck 参数类型不支持: {type(deck)}")

        if self.deck is None:
            raise ValueError("Deck 配置不能为空")

        # 统计仓库信息
        warehouse_count = 0
        if hasattr(self.deck, 'children'):
            warehouse_count = len(self.deck.children)
            logger.info(f"Deck 初始化完成，加载 {warehouse_count} 个资源")
        
        
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
        self._node_values = {}  # 修改为支持时间戳的缓存结构
        self._cache_timeout = cache_timeout
        
        # 连接状态监控
        self._connection_check_interval = 30.0  # 连接检查间隔(秒)
        self._connection_monitor_running = False
        self._connection_monitor_thread = None
        
        # # 添加线程锁，保护OPC UA客户端的并发访问
        import threading
        self._client_lock = threading.RLock()
        
        # 连接到服务器
        self._connect()
        
        # 如果提供了 CSV 路径，则直接加载节点
        if csv_path:
            self.load_nodes_from_csv(csv_path)
        
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
        """freeopcua订阅处理器：必须实现 datachange_notification 方法"""
        def __init__(self, outer):
            self.outer = outer

        def datachange_notification(self, node, val, data):
            # 委托给外层类的处理函数
            try:
                self.outer._on_subscription_datachange(node, val, data)
            except Exception as e:
                logger.error(f"订阅数据回调处理失败: {e}")

        # 可选：事件通知占位，避免库调用时报缺失
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
                handler = OpcUaClient.SubscriptionHandler(self)
                self._subscription = self.client.create_subscription(
                    self._subscription_interval,
                    handler
                )
                
                # 为所有变量节点创建监控项
                subscribed_count = 0
                skipped_count = 0
                
                for node_name, node in self._node_registry.items():
                    # 只为变量节点创建订阅
                    if node.type == NodeType.VARIABLE and node.node_id:
                        try:
                            # 优先使用在查找阶段缓存的真实 ua.Node 对象
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
                # 订阅失败时回退到按需读取模式
                self._use_subscription = False
                logger.warning("订阅模式设置失败，已自动切换到按需读取模式")
    
    def _on_subscription_datachange(self, node, val, data):
        """订阅数据变化处理器（供内部 SubscriptionHandler 调用）"""
        try:
            node_id = str(node.nodeid)
            current_time = time.time()
            # 查找对应的节点名称
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
        """
        获取节点值（智能缓存版本）
        
        参数:
            name: 节点名称（支持中文名或英文名）
            use_cache: 是否使用缓存
            force_read: 是否强制从服务器读取（忽略缓存）
        """
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
                # 更新缓存
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
            
            # 如果是订阅模式，缓存永久有效（由订阅更新）
            # 如果是按需读取模式，检查缓存超时
            if cache_entry.get('source') == 'subscription' or cache_age < self._cache_timeout:
                logger.debug(f"从缓存读取: {chinese_name} = {cache_entry['value']} (age: {cache_age:.2f}s, source: {cache_entry.get('source', 'unknown')})")
                return cache_entry['value']
        
        # 缓存过期或不存在，从服务器读取
        with self._client_lock:
            try:
                value, error = self.use_node(chinese_name).read()
                if not error:
                    # 更新缓存
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
        """
        设置节点值
        写入成功后会立即更新本地缓存
        """
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
                    # 写入成功，立即更新缓存
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
                    # 尝试获取命名空间数组来验证连接
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
                # 检查连接状态
                if not self._check_connection():
                    logger.warning("检测到连接断开，尝试重新连接...")
                    reconnect_attempts += 1
                    
                    if reconnect_attempts <= max_reconnect_attempts:
                        try:
                            # 尝试重新连接
                            with self._client_lock:
                                if self.client:
                                    try:
                                        self.client.disconnect()
                                    except:
                                        pass
                                    
                                    self.client.connect()
                                    logger.info("✓ 重新连接成功")
                                    
                                    # 重新设置订阅
                                    if self._use_subscription:
                                        self._setup_subscriptions()
                                    
                                    reconnect_attempts = 0
                        except Exception as e:
                            logger.error(f"重新连接失败 (尝试 {reconnect_attempts}/{max_reconnect_attempts}): {e}")
                            time.sleep(5)  # 重连失败后等待5秒
                    else:
                        logger.error(f"达到最大重连次数 ({max_reconnect_attempts})，停止重连")
                        self._connection_monitor_running = False
                else:
                    # 连接正常，重置重连计数
                    reconnect_attempts = 0
                
            except Exception as e:
                logger.error(f"连接监控出错: {e}")
            
            # 等待下次检查
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
        """
        读取节点值的便捷方法（使用缓存）
        返回JSON格式字符串
        """
        try:
            # 使用get_node_value方法，自动处理缓存
            value = self.get_node_value(node_name, use_cache=True)
            
            # 获取缓存信息
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
            
            # 如果是相对路径，转换为相对于 AI4M.py 文件所在目录的绝对路径
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
            
            # 如果使用订阅模式，设置订阅（确保新节点被订阅）
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
            
            setattr(OpcUaClient, attr_name, property(create_property_getter(node_name)))
            logger.debug(f"已注册节点 '{node_name}' 为属性 '{attr_name}'")

    def post_init(self, ros_node):
        """ROS2 节点就绪后的初始化"""
        if not (hasattr(self, 'deck') and self.deck):
            return
            
        if not (hasattr(ros_node, 'resource_tracker') and ros_node.resource_tracker):
            logger.warning("resource_tracker 不存在，无法注册 deck")
            return
        
        # 1. 本地注册（必需）
        ros_node.resource_tracker.add_resource(self.deck)
        
        # 2. 上传云端
        try:
            from unilabos.ros.nodes.base_device_node import ROS2DeviceNode
            ROS2DeviceNode.run_async_func(
                ros_node.update_resource,
                True,
                resources=[self.deck]
            )
            logger.info("Deck 已上传到云端")
        except Exception as e:
            logger.error(f"上传失败: {e}")
   
    def start_manual_mode(
        self
    ) -> bool:
        """
        指令作业模式函数：
        - 将模式切换、手自动切换写false
        - 等待自动模式为false
        - 将模式切换写true

        返回: 是否成功完成自动作业
        """
        self.success = False

        print("启动指令作业模式...")

        # 将模式切换、手自动切换写true
        print("设置模式切换和手自动切换为true...")
        self.set_node_value("mode_switch", True)
        self.set_node_value("manual_auto_switch", False)

        # 等待自动模式为false
        print("等待自动模式为False...")
        auto_mode = self.get_node_value("auto_mode")
        while auto_mode:
            print("等待自动模式变为False...")
            time.sleep(1.0)
            auto_mode = self.get_node_value("auto_mode")
        else:
            print("模式切换完成")
            self.success = True

        return self.success

    def trigger_robot_pick_beaker(
        self,
        pick_beaker_id: int,
        place_station_id: int,
    ) -> bool:
        """
        机器人取烧杯并放到检测位：
        - 先写入取烧杯编号，等待取烧杯完成
        - 取完成后再写入放检测编号，等待对应的放检测完成信号
        
        参数:
            pick_beaker_id: 取烧杯编号（1-5）
            place_station_id: 放检测编号（1-3）
            timeout: 超时时间（秒），保留用于向后兼容，实际不使用
            poll_interval: 轮询间隔（秒）
            
        返回: 是否成功完成操作
        """
        self.success = False

        # 校验输入范围
        if pick_beaker_id not in (1, 2, 3, 4, 5):
            logger.error("取烧杯编号必须在 1-5 范围内")
            return False
        if place_station_id not in (1, 2, 3):
            logger.error("放检测编号必须在 1-3 范围内")
            return False

        # 获取仓库资源
        rack_warehouse = self.deck.warehouses["水凝胶烧杯堆栈"]
        station_warehouse = self.deck.warehouses[f"反应工站{place_station_id}"]
        rack_site_key = f"A{pick_beaker_id}"

        pick_complete_node = f"robot_rack_pick_beaker_{pick_beaker_id}_complete"
        place_complete_node = f"robot_place_station_{place_station_id}_complete"

        # 阶段1：下发取烧杯编号并等待完成
        print("下发取烧杯编号，等待完成...")
        self.set_node_value("robot_pick_beaker_id", pick_beaker_id)
        
        # 等待取烧杯完成
        pick_complete = self.get_node_value(pick_complete_node)
        while not pick_complete:
            print("取烧杯中...")
            time.sleep(2.0)
            pick_complete = self.get_node_value(pick_complete_node)
        
        # 获取载具（carrier）
        carrier = rack_warehouse[rack_site_key]
        if carrier is None:
            logger.error(f"堆栈位置 {rack_site_key} 没有载具")
            return False
        
        # 阶段1.5：机器人取烧杯完成后，从堆栈解绑载具
        try:
            rack_warehouse.unassign_child_resource(carrier)
            print(f"✓ 已从堆栈解绑载具 {carrier.name}")
        except Exception as e:
            logger.error(f"从堆栈解绑载具失败: {e}")
            return False
        
        # 阶段2：取完成后再下发放检测编号并等待完成
        print("取完成，开始下发放检测编号...")
        self.set_node_value("robot_place_station_id", place_station_id)
        
        # 等待放检测完成
        place_complete = self.get_node_value(place_complete_node)
        while not place_complete:
            print("放检测中...")
            time.sleep(2.0)
            place_complete = self.get_node_value(place_complete_node)
        
        # 阶段2.5：机器人放到检测站完成后，绑定载具到检测站
        # 注意：每个检测站都有独立的 warehouse，且是 1x1x1，所以索引始终是 0
        try:
            # 每个检测站 warehouse 只有 1 个 site，索引固定为 0
            station_site_idx = 0
            station_site_key = list(station_warehouse._ordering.keys())[station_site_idx]
            station_location = station_warehouse.child_locations[station_site_key]
            
            # 绑定到检测站 warehouse
            station_warehouse.assign_child_resource(carrier, location=station_location, spot=station_site_idx)
            print(f"✓ 已绑定载具 {carrier.name} 到检测站{place_station_id}")
        except Exception as e:
            logger.error(f"绑定载具到检测站失败: {e}")
            # 即使绑定失败，物理上机器人已经完成了操作
        
        print("放检测完成")
        self.success = True
            
        # 更新资源树到前端
        if hasattr(self, '_ros_node') and self._ros_node:
            try:
                from unilabos.ros.nodes.base_device_node import ROS2DeviceNode
                ROS2DeviceNode.run_async_func(self._ros_node.update_resource, True, resources=[self.deck])
                print(f"✓ 已同步资源更新到前端")
            except Exception as e:
                logger.warning(f"前端资源更新失败: {e}")

        return self.success

    def trigger_robot_place_beaker(
        self,
        place_beaker_id: int,
        pick_station_id: int,
    ) -> bool:
        """
        机器人从检测位取烧杯并放回：
        - 先写入取检测编号，等待取检测完成
        - 取完成后再写入放烧杯编号，等待对应的放烧杯完成信号
        
        参数:
            place_beaker_id: 放烧杯编号（1-5）
            pick_station_id: 取检测编号（1-3）
            
        返回: 是否成功完成操作
        """
        self.success = False

        # 校验输入范围
        if place_beaker_id not in (1, 2, 3, 4, 5):
            logger.error("放烧杯编号必须在 1-5 范围内")
            return False
        if pick_station_id not in (1, 2, 3):
            logger.error("取检测编号必须在 1-3 范围内")
            return False

        # 获取仓库资源
        rack_warehouse = self.deck.warehouses["水凝胶烧杯堆栈"]
        station_warehouse = self.deck.warehouses[f"反应工站{pick_station_id}"]
        
        # 获取检测站的载具
        # 注意：每个检测站都有独立的 warehouse，且是 1x1x1，所以索引始终是 0
        # 当检测站有 carrier 时，sites[0] 直接返回 BottleCarrier（和堆栈一样）
        # 当检测站为空时，sites[0] 返回 ResourceHolder（占位符）
        station_site_idx = 0  # 每个检测站 warehouse 只有 1 个 site
        
        if not station_warehouse.sites or len(station_warehouse.sites) == 0:
            logger.error(f"检测站{pick_station_id} 的 warehouse sites 列表为空")
            return False
        
        carrier = station_warehouse.sites[station_site_idx]
        
        # 检查是否是 ResourceHolder（说明检测站为空）
        if carrier is None or type(carrier).__name__ == 'ResourceHolder':
            logger.error(f"检测站{pick_station_id} 没有载具（可能是空的 ResourceHolder）")
            return False
        
        # 确定堆栈目标位置（place_beaker_id 1-5 对应 C1-C5）
        rack_site_key = f"C{place_beaker_id}"

        pick_complete_node = f"robot_pick_station_{pick_station_id}_complete"
        place_complete_node = f"robot_rack_place_beaker_{place_beaker_id}_complete"

        # 阶段1：下发取检测编号并等待完成
        print("下发取检测编号，等待完成...")
        self.set_node_value("robot_pick_station_id", pick_station_id)
        
        # 等待取检测完成
        pick_complete = self.get_node_value(pick_complete_node)
        while not pick_complete:
            print("取检测中...")
            time.sleep(2.0)
            pick_complete = self.get_node_value(pick_complete_node)
        
        # 阶段1.5：机器人取检测完成后，从检测站解绑载具
        try:
            station_warehouse.unassign_child_resource(carrier)
            print(f"✓ 已从检测站{pick_station_id}解绑载具 {carrier.name}")
        except Exception as e:
            logger.error(f"从检测站解绑载具失败: {e}")
            return False
        
        # 阶段2：取完成后再下发放烧杯编号并等待完成
        print("取完成，开始下发放烧杯编号...")
        self.set_node_value("robot_place_beaker_id", place_beaker_id)
        
        # 等待放烧杯完成
        place_complete = self.get_node_value(place_complete_node)
        while not place_complete:
            print("放烧杯中...")
            time.sleep(2.0)
            place_complete = self.get_node_value(place_complete_node)
        
        # 阶段2.5：机器人放烧杯完成后，绑定载具回堆栈
        try:
            # 获取堆栈的位置信息（rack_site_key 已在前面定义为 C{place_beaker_id}）
            rack_site_idx = list(rack_warehouse._ordering.keys()).index(rack_site_key)
            rack_location = rack_warehouse.child_locations[rack_site_key]
            
            # 绑定回堆栈 warehouse
            rack_warehouse.assign_child_resource(carrier, location=rack_location, spot=rack_site_idx)
            print(f"✓ 已绑定载具 {carrier.name} 回堆栈 {rack_site_key}")
        except Exception as e:
            logger.error(f"绑定载具回堆栈失败: {e}")
            # 即使绑定失败，物理上机器人已经完成了操作
        
        print("放烧杯完成")
        self.success = True
        
        # 更新资源树到前端
        if hasattr(self, '_ros_node') and self._ros_node:
            try:
                from unilabos.ros.nodes.base_device_node import ROS2DeviceNode
                ROS2DeviceNode.run_async_func(self._ros_node.update_resource, True, resources=[self.deck])
                print(f"✓ 已同步资源更新到前端")
            except Exception as e:
                logger.warning(f"前端资源更新失败: {e}")

        return self.success

    def trigger_station_process(
        self,
        station_id: int,
        mag_stir_stir_speed: int,
        mag_stir_heat_temp: int,
        mag_stir_time_set: int,
        syringe_pump_abs_position_set: int,
    ) -> bool:
        """
        执行检测工艺流程：
        1. 等待检测站请求参数
        2. 下发对应编号的搅拌仪和注射泵参数
        3. 等待参数已执行
        4. 给出检测开始信号
        5. 等待检测工艺完成
        
        参数:
            station_id: 检测编号（1-3）
            mag_stir_stir_speed: 磁力搅拌仪搅拌速度
            mag_stir_heat_temp: 磁力搅拌仪加热温度
            mag_stir_time_set: 磁力搅拌仪时间设置
            syringe_pump_abs_position_set: 注射泵绝对位置设置
            
        返回: 是否成功完成工艺
        """
        self.success = False

        # 校验输入范围
        if station_id not in (1, 2, 3):
            logger.error("检测编号必须在 1-3 范围内")
            return False

        # 检测站索引（0-2）
        station_idx = station_id - 1
        
        # 节点名称
        request_node = f"station_{station_id}_request_params"
        params_received_node = f"station_{station_id}_params_received"
        start_node = f"station_{station_id}_start"
        complete_node = f"station_{station_id}_process_complete"
        
        self.set_node_value(complete_node, False)
        self.set_node_value(start_node, False)
        self.set_node_value(params_received_node, False)

        # 阶段1：等待检测站请求参数
        print(f"等待检测{station_id}请求参数...")
        request_params = self.get_node_value(request_node)
        while not request_params:
            print(f"等待检测{station_id}请求参数中...")
            time.sleep(2.0)
            request_params = self.get_node_value(request_node)
        
        print(f"检测{station_id}已请求参数，开始下发...")
        
        # 阶段2：下发对应编号的搅拌仪参数
        self.set_node_value(f"mag_stirrer_c{station_idx}_stir_speed", mag_stir_stir_speed)
        self.set_node_value(f"mag_stirrer_c{station_idx}_heat_temp", mag_stir_heat_temp)
        self.set_node_value(f"mag_stirrer_c{station_idx}_time_set", mag_stir_time_set)
        print(f"已下发检测{station_id}磁力搅拌仪参数：速度={mag_stir_stir_speed}, 温度={mag_stir_heat_temp}, 时间={mag_stir_time_set}")
        
        # 下发对应编号的注射泵参数
        self.set_node_value(f"syringe_pump_{station_idx}_abs_position_set", syringe_pump_abs_position_set)
        print(f"已下发检测{station_id}注射泵绝对位置设置：{syringe_pump_abs_position_set}")

        
        # 阶段3：等待参数已执行
        self.set_node_value(start_node, True)
        print(f"等待检测{station_id}参数已执行...")
        params_received = self.get_node_value(params_received_node)
        while not params_received:
            print(f"检测{station_id}参数执行中...")
            time.sleep(2.0)
            params_received = self.get_node_value(params_received_node)
        
        print(f"检测{station_id}参数已执行")
           
        # 阶段4：等待检测工艺完成
        print(f"等待检测{station_id}工艺完成...")
        process_complete = self.get_node_value(complete_node)
        while not process_complete:
            print(f"检测{station_id}工艺执行中...")
            time.sleep(2.0)
            process_complete = self.get_node_value(complete_node)
        else:
            print(f"检测{station_id}工艺完成")
            self.set_node_value(start_node, False)
            self.success = True

        return self.success

    def trigger_init(
        self
    ) -> bool:
        """
        初始化函数：
        - 将手自动切换写false
        - 等待自动模式为false
        - 将初始化PC写true
        - 等待初始化完成PC为true
        - 将初始化PC写false
        - 返回成功

        参数:
            poll_interval: 轮询间隔（秒）

        返回: 是否成功完成初始化
        """
        self.success = False

        print("开始初始化...")
        
        # 将手自动切换写false
        print("设置手自动切换为false...")
        self.set_node_value("manual_auto_switch", False)
        self.set_node_value("initialize", False)
        time.sleep(1.0)
        # 等待自动模式为false
        print("等待自动模式为false...")
        auto_mode = self.get_node_value("auto_mode")
        while auto_mode:
            print("等待自动模式变为false...")
            time.sleep(2.0)
            auto_mode = self.get_node_value("auto_mode")
        
        # 将初始化PC写true
        print("自动模式已为false，设置初始化PC为true...")
        self.set_node_value("initialize", True)
        time.sleep(2.0)
        # 等待初始化完成PC为true
        print("等待初始化完成...")
        init_finished = self.get_node_value("init finished")
        while not init_finished:
            print("初始化中...")
            time.sleep(2.0)
            init_finished = self.get_node_value("init finished")
        else:
            # 将初始化PC写false
            print("初始化完成，设置初始化PC为false...")
            self.set_node_value("initialize", False)
            self.success = True
        
        return self.success

    def download_auto_params(
        self,
        mag_stir_stir_speed: int,
        mag_stir_heat_temp: int,
        mag_stir_time_set: int,
        syringe_pump_abs_position_set: int,
        auto_job_stop_delay: int
    ) -> bool:
        """
        自动模式参数下发函数：
        - 将搅拌仪的搅拌速度、加热温度、时间设置、泵的绝对位置设置和自动作业停止等待时间作为传入参数
        - 一起下发给3个搅拌仪和3个泵
        - 下发后将自动作业参数已下发写true
        - 等待自动作业参数已执行为true
        - 将已下发写false
        - 返回成功

        参数:
            mag_stir_stir_speed: 磁力搅拌仪搅拌速度
            mag_stir_heat_temp: 磁力搅拌仪加热温度
            mag_stir_time_set: 磁力搅拌仪时间设置
            syringe_pump_abs_position_set: 注射泵绝对位置设置
            auto_job_stop_delay: 自动作业等待停止时间
            poll_interval: 轮询间隔（秒）

        返回: 是否成功完成参数下发
        """
        self.success = False
        
        print("开始下发自动模式参数...")
        self.set_node_value("auto_param_applied", False)
        self.set_node_value("auto_param_downloaded", False)
        self.set_node_value("mode_switch", False)
        # 下发3个磁力搅拌仪的参数
        for c in (0, 1, 2):
            self.set_node_value(f"mag_stirrer_c{c}_stir_speed", mag_stir_stir_speed)
            self.set_node_value(f"mag_stirrer_c{c}_heat_temp", mag_stir_heat_temp)
            self.set_node_value(f"mag_stirrer_c{c}_time_set", mag_stir_time_set)
        print(f"已下发3个磁力搅拌仪参数：速度={mag_stir_stir_speed}, 温度={mag_stir_heat_temp}, 时间={mag_stir_time_set}")

        # 下发3个注射泵的绝对位置设置
        for p in (0, 1, 2):
            self.set_node_value(f"syringe_pump_{p}_abs_position_set", syringe_pump_abs_position_set)
        print(f"已下发3个注射泵绝对位置设置：{syringe_pump_abs_position_set}")

        # 下发自动作业等待停止时间
        self.set_node_value("auto_job_stop_delay", auto_job_stop_delay)
        print(f"已下发自动作业等待停止时间：{auto_job_stop_delay}")

        # 将自动作业参数已下发写true
        print("设置自动作业参数已下发为true...")
        self.set_node_value("auto_param_downloaded", True)

        # 等待自动作业参数已执行为true
        print("等待自动作业参数已执行...")
        param_applied = self.get_node_value("auto_param_applied")
        while not param_applied:
            print("参数执行中...")
            time.sleep(2.0)
            param_applied = self.get_node_value("auto_param_applied")
        else:
            print("自动作业参数已执行")
            # 将已下发写false
            self.set_node_value("auto_param_downloaded", False)
            self.success = True

        return self.success

    def start_auto_mode(
        self
    ) -> bool:
        """
        自动作业模式函数：
        - 将模式切换、手自动切换写true
        - 等待自动模式为true
        - 将自动作业开始触发写true
        - 等待自动作业完成为true
        - 返回成功

        参数:
            poll_interval: 轮询间隔（秒）

        返回: 是否成功完成自动作业
        """
        self.success = False

        print("启动自动作业模式...")

        # 将模式切换、手自动切换写true
        print("设置模式切换和手自动切换为true...")
        self.set_node_value("mode_switch", False)
        self.set_node_value("manual_auto_switch", False)
        self.set_node_value("auto_run_start_trigger", False)
        self.set_node_value("auto_run_complete", False)
        time.sleep(1.0)
        self.set_node_value("manual_auto_switch", True)

        # 等待自动模式为true
        print("等待自动模式为true...")
        auto_mode = self.get_node_value("auto_mode")
        while not auto_mode:
            print("等待自动模式变为true...")
            time.sleep(5.0)
            auto_mode = self.get_node_value("auto_mode")
        
        # 将自动作业开始触发写true
        print("自动模式已为true，设置自动作业开始触发为true...")
        self.set_node_value("auto_run_start_trigger", True)

        # 等待自动作业完成为true
        print("等待自动作业完成...")
        auto_run_complete = self.get_node_value("auto_run_complete")
        while not auto_run_complete:
            print("自动作业执行中...")
            time.sleep(5.0)
            auto_run_complete = self.get_node_value("auto_run_complete")
        else:
            print("自动作业完成")
            self.set_node_value("manual_auto_switch", False)
            self.success = True

        return self.success

    

if __name__ == '__main__':
    # 示例用法
    

    # 创建OPC UA客户端并加载配置
    try:
        client = OpcUaClient(
            url="opc.tcp://127.0.0.1:49320",  # 替换为实际的OPC UA服务器地址
            csv_path="C:\\Users\\Roy\\Desktop\\DPLC\\Uni-Lab-OS\\unilabos\\devices\\workstation\\AI4M\\opcua_nodes_AI4M.csv"  # 使用AI4M的CSV路径
        )
        
        # 测试1: 初始化函数
        print("\n" + "="*80)
        print("测试: 物料载具转移函数")
        print("="*80)
        
        # 测试将物料载具从水凝胶堆栈转移到反应工站1
        result = client.transfer_carrier_to_station(
            carrier_name="Hydrogel_Clean_1BottleCarrier",
            source_warehouse_name="水凝胶烧杯堆栈",
            target_station_name="反应工站1"
        )
        print(f"物料载具转移结果: {'成功' if result else '失败'}")
        
        print("\n" + "="*80)
        print("测试完成")
        print("="*80)
        
        # 断开连接
        client.disconnect()
        
    except Exception as e:
        print(f"错误: {e}")
        traceback.print_exc()


