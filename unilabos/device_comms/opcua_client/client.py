import json
import time
import traceback
from typing import Any, Union, List, Dict, Callable, Optional, Tuple
from pydantic import BaseModel

from opcua import Client, ua
from opcua.ua import NodeClass
import pandas as pd
import os

from unilabos.device_comms.opcua_client.node.uniopcua import Base as OpcUaNodeBase
from unilabos.device_comms.opcua_client.node.uniopcua import Variable, Method, NodeType, DataType
from unilabos.device_comms.universal_driver import UniversalDriver
from unilabos.utils.log import logger


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


class InitFunctionJson(NodeFunctionJson):
    pass


class StartFunctionJson(NodeFunctionJson):
    write_functions: List[str]
    condition_functions: List[str]
    stop_condition_expression: str


class StopFunctionJson(NodeFunctionJson):
    pass


class CleanupFunctionJson(NodeFunctionJson):
    pass


class ActionJson(BaseModel):
    node_function_to_create: List[NodeFunctionJson]
    create_init_function: Optional[InitFunctionJson] = None
    create_start_function: Optional[StartFunctionJson] = None
    create_stop_function: Optional[StopFunctionJson] = None
    create_cleanup_function: Optional[CleanupFunctionJson] = None


class SimplifiedActionJson(BaseModel):
    """简化的动作JSON格式，直接定义节点列表和函数"""
    nodes: Optional[Dict[str, Dict[str, Any]]] = None  # 节点定义，格式为 {func_name: {node_name, mode, value}}
    init_function: Optional[Dict[str, Any]] = None
    start_function: Optional[Dict[str, Any]] = None
    stop_function: Optional[Dict[str, Any]] = None
    cleanup_function: Optional[Dict[str, Any]] = None


class WorkflowCreateJson(BaseModel):
    name: str
    action: List[Union[ActionJson, SimplifiedActionJson, 'WorkflowCreateJson', str]]
    parameters: Optional[List[str]] = None
    description: Optional[str] = None


class ExecuteProcedureJson(BaseModel):
    register_node_list_from_csv_path: Optional[Dict[str, Any]] = None
    create_flow: List[WorkflowCreateJson]
    execute_flow: List[str]


class BaseClient(UniversalDriver):
    client: Optional[Client] = None
    _node_registry: Dict[str, OpcUaNodeBase] = {}
    DEFAULT_ADDRESS_PATH = ""
    _variables_to_find: Dict[str, Dict[str, Any]] = {}
    _name_mapping: Dict[str, str] = {}  # 英文名到中文名的映射
    _reverse_mapping: Dict[str, str] = {}  # 中文名到英文名的映射

    def __init__(self):
        super().__init__()
        # 自动查找节点功能默认开启
        self._auto_find_nodes = True
        # 初始化名称映射字典
        self._name_mapping = {}
        self._reverse_mapping = {}

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
        """查找服务器中的节点"""
        if not self.client:
            raise ValueError('client is not connected')
            
        logger.info('开始查找节点...')
        try:
            # 获取根节点
            root = self.client.get_root_node()
            objects = root.get_child(["0:Objects"])
            
            # 查找节点
            self._find_nodes_recursive(objects)
            
            # 检查是否所有节点都已找到
            not_found = []
            for var_name, var_info in self._variables_to_find.items():
                if var_name not in self._node_registry:
                    not_found.append(var_name)
            
            if not_found:
                logger.warning(f"以下节点未找到: {', '.join(not_found)}")
            else:
                logger.info("所有节点均已找到")
                
        except Exception as e:
            logger.error(f"查找节点失败: {e}")
            traceback.print_exc()

    def _find_nodes_recursive(self, node) -> None:
        """递归查找节点"""
        try:
            # 获取当前节点的浏览名称
            browse_name = node.get_browse_name()
            node_name = browse_name.Name
            
            # 检查是否是我们要找的变量
            if node_name in self._variables_to_find and node_name not in self._node_registry:
                var_info = self._variables_to_find[node_name]
                node_type = var_info.get("node_type")
                data_type = var_info.get("data_type")
                
                # 根据节点类型创建相应的对象
                if node_type == NodeType.VARIABLE:
                    self._node_registry[node_name] = Variable(self.client, node_name, str(node.nodeid), data_type)
                    logger.info(f"找到变量节点: {node_name}")
                elif node_type == NodeType.METHOD:
                    # 对于方法节点，需要获取父节点ID
                    parent_node = node.get_parent()
                    parent_node_id = str(parent_node.nodeid)
                    self._node_registry[node_name] = Method(self.client, node_name, str(node.nodeid), parent_node_id, data_type)
                    logger.info(f"找到方法节点: {node_name}")
            
            # 递归处理子节点
            for child in node.get_children():
                self._find_nodes_recursive(child)
                
        except Exception as e:
            # 忽略处理单个节点时的错误，继续处理其他节点
            pass

    @classmethod
    def load_csv(cls, file_path: str) -> List[OpcUaNode]:
        """
        从CSV文件加载节点定义
        CSV文件需包含Name,NodeType,DataType列
        可选包含EnglishName和NodeLanguage列
        """
        df = pd.read_csv(file_path)
        df = df.drop_duplicates(subset='Name', keep='first')  # 重复的数据应该报错
        nodes = []
        
        # 检查是否包含英文名称列和节点语言列
        has_english_name = 'EnglishName' in df.columns
        has_node_language = 'NodeLanguage' in df.columns
        
        # 如果存在英文名称列，创建名称映射字典
        name_mapping = {}
        reverse_mapping = {}
        
        for _, row in df.iterrows():
            name = row.get('Name')
            node_type_str = row.get('NodeType')
            data_type_str = row.get('DataType')
            
            # 获取英文名称和节点语言（如果有）
            english_name = row.get('EnglishName') if has_english_name else None
            node_language = row.get('NodeLanguage') if has_node_language else 'English'  # 默认为英文
            
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
            
            # 创建节点对象，节点ID留空，将通过自动查找功能获取
            nodes.append(OpcUaNode(
                name=name,
                node_type=node_type,
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
                return self._node_registry[chinese_name]
            elif chinese_name in self._variables_to_find:
                logger.warning(f"节点 {chinese_name} (英文名: {name}) 尚未找到，尝试重新查找")
                if self.client:
                    self._find_nodes()
                    if chinese_name in self._node_registry:
                        return self._node_registry[chinese_name]
                raise ValueError(f'节点 {chinese_name} (英文名: {name}) 未注册或未找到')
        
        # 直接使用原始名称查找
        if name not in self._node_registry:
            if name in self._variables_to_find:
                logger.warning(f"节点 {name} 尚未找到，尝试重新查找")
                if self.client:
                    self._find_nodes()
                    if name in self._node_registry:
                        return self._node_registry[name]
            raise ValueError(f'节点 {name} 未注册或未找到')
        return self._node_registry[name]

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
        for node in node_list:
            if node is None:
                continue
                
            if node.name in self._node_registry:
                logger.info(f'节点 {node.name} 已存在')
                exist = self._node_registry[node.name]
                if exist.type != node.node_type:
                    raise ValueError(f'节点 {node.name} 类型 {node.node_type} 与已存在的类型 {exist.type} 不一致')
                continue
                
            # 将节点添加到待查找列表
            self._variables_to_find[node.name] = {
                "node_type": node.node_type,
                "data_type": node.data_type
            }
            logger.info(f'添加节点 {node.name} 到待查找列表')

        logger.info('节点注册完成')
        
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
                    
                    print(f"读取 {node_name} 返回值 = {val} (类型: {type(val).__name__}), 错误 = {err}")
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
            if isinstance(write_nodes, list):
                # 处理节点列表
                for node_name in write_nodes:
                    # 尝试从参数中获取同名参数的值
                    current_value = True  # 默认值
                    if hasattr(self, '_workflow_params') and node_name in self._workflow_params:
                        current_value = self._workflow_params[node_name]
                        print(f"初始化函数: 从参数获取值 {node_name} = {current_value}")
                    
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
                # 处理节点字典，使用指定的值
                for node_name, node_value in write_nodes.items():
                    # 检查值是否是字符串类型的参数名
                    current_value = node_value
                    if isinstance(node_value, str) and hasattr(self, '_workflow_params') and node_value in self._workflow_params:
                        current_value = self._workflow_params[node_value]
                        print(f"初始化函数: 从参数获取值 {node_value} = {current_value}")
                    
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
            # 直接处理写入节点
            if write_nodes:
                if isinstance(write_nodes, list):
                    # 处理节点列表，默认值都是True
                    for i, node_name in enumerate(write_nodes):
                        # 尝试获取与节点对应的参数值
                        param_name = f"write_{i}"
                        
                        # 获取参数值（如果有）
                        current_value = True  # 默认值
                        if hasattr(self, '_workflow_params') and param_name in self._workflow_params:
                            current_value = self._workflow_params[param_name]
                        
                        # 直接写入节点
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
                    # 处理节点字典，值是指定的
                    for node_name, node_value in write_nodes.items():
                        # 尝试获取参数值（如果节点名与参数名匹配）
                        current_value = node_value  # 使用指定的默认值
                        if hasattr(self, '_workflow_params') and node_name in self._workflow_params:
                            current_value = self._workflow_params[node_name]
                        
                        # 直接写入节点
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
    def __init__(self, url: str, config_path: str = None, username: str = None, password: str = None, refresh_interval: float = 1.0):
        # 降低OPCUA库的日志级别
        import logging
        logging.getLogger("opcua").setLevel(logging.WARNING)
        
        super().__init__()
        
        client = Client(url)
        
        if username and password:
            client.set_user(username)
            client.set_password(password)
            
        self._set_client(client)
        self._connect()
        
        # 节点值缓存和刷新相关属性
        self._node_values = {}  # 缓存节点值
        self._refresh_interval = refresh_interval  # 刷新间隔(秒)
        self._refresh_running = False
        self._refresh_thread = None
        
        # 如果提供了配置文件路径，则加载配置并注册工作流
        if config_path:
            self.load_config(config_path)
            
        # 启动节点值刷新线程
        self.start_node_refresh()
        
    def _register_nodes_as_attributes(self):
        """将所有节点注册为实例属性，可以通过self.node_name访问"""
        for node_name, node in self._node_registry.items():
            # 检查是否有对应的英文名称
            eng_name = self._reverse_mapping.get(node_name)
            if eng_name:
                # 如果有对应的英文名称，使用英文名称作为属性名
                attr_name = eng_name
            else:
                # 如果没有对应的英文名称，使用原始名称，但替换空格和特殊字符
                attr_name = node_name.replace(' ', '_').replace('-', '_')
            
            # 创建获取节点值的属性方法，使用中文名称获取节点值
            def create_property_getter(node_key):
                def getter(self):
                    # 优先从缓存获取值
                    if node_key in self._node_values:
                        return self._node_values[node_key]
                    # 缓存中没有则直接读取
                    value, _ = self.use_node(node_key).read()
                    return value
                return getter
            
            # 使用property装饰器将方法注册为类属性
            setattr(OpcUaClient, attr_name, property(create_property_getter(node_name)))
            logger.info(f"已注册节点 '{node_name}' 为属性 '{attr_name}'")
    
    def refresh_node_values(self):
        """刷新所有节点的值到缓存"""
        if not self.client:
            logger.warning("客户端未初始化，无法刷新节点值")
            return
            
        try:
            # 简单检查连接状态，如果不连接会抛出异常
            self.client.get_namespace_array()
        except Exception as e:
            logger.warning(f"客户端连接异常，无法刷新节点值: {e}")
            return
            
        for node_name, node in self._node_registry.items():
            try:
                if hasattr(node, 'read'):
                    value, error = node.read()
                    if not error:
                        self._node_values[node_name] = value
                        #logger.debug(f"已刷新节点 '{node_name}' 的值: {value}")
            except Exception as e:
                logger.error(f"刷新节点 '{node_name}' 失败: {e}")
                
    def get_node_value(self, name):
        """获取节点值，支持中文名和英文名"""
        # 如果提供的是英文名，转换为中文名
        if name in self._name_mapping:
            chinese_name = self._name_mapping[name]
            # 优先从缓存获取值
            if chinese_name in self._node_values:
                return self._node_values[chinese_name]
            # 缓存中没有则直接读取
            value, _ = self.use_node(chinese_name).read()
            return value
        # 如果提供的是中文名，直接使用
        elif name in self._node_registry:
            # 优先从缓存获取值
            if name in self._node_values:
                return self._node_values[name]
            # 缓存中没有则直接读取
            value, _ = self.use_node(name).read()
            return value
        else:
            raise ValueError(f"未找到名称为 '{name}' 的节点")
    
    def set_node_value(self, name, value):
        """设置节点值，支持中文名和英文名"""
        # 如果提供的是英文名，转换为中文名
        if name in self._name_mapping:
            chinese_name = self._name_mapping[name]
            node = self.use_node(chinese_name)
        # 如果提供的是中文名，直接使用
        elif name in self._node_registry:
            node = self.use_node(name)
        else:
            raise ValueError(f"未找到名称为 '{name}' 的节点")
        
        # 写入值
        error = node.write(value)
        if not error:
            # 更新缓存
            if hasattr(node, 'name'):
                self._node_values[node.name] = value
            return True
        return False
    
    def _refresh_worker(self):
        """节点值刷新线程的工作函数"""
        self._refresh_running = True
        logger.info(f"节点值刷新线程已启动，刷新间隔: {self._refresh_interval}秒")
        
        while self._refresh_running:
            try:
                self.refresh_node_values()
            except Exception as e:
                logger.error(f"节点值刷新过程出错: {e}")
            
            # 等待下一次刷新
            time.sleep(self._refresh_interval)
    
    def start_node_refresh(self):
        """启动节点值刷新线程"""
        if self._refresh_thread is not None and self._refresh_thread.is_alive():
            logger.warning("节点值刷新线程已在运行")
            return
            
        import threading
        self._refresh_thread = threading.Thread(target=self._refresh_worker, daemon=True)
        self._refresh_thread.start()
        
    def stop_node_refresh(self):
        """停止节点值刷新线程"""
        self._refresh_running = False
        if self._refresh_thread and self._refresh_thread.is_alive():
            self._refresh_thread.join(timeout=2.0)
            logger.info("节点值刷新线程已停止")
        
    def load_config(self, config_path: str) -> None:
        """从JSON配置文件加载并注册工作流"""
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config_data = json.load(f)
            
            # 处理节点注册
            if "register_node_list_from_csv_path" in config_data:
                # 获取配置文件所在目录
                config_dir = os.path.dirname(os.path.abspath(config_path))
                
                # 处理CSV路径，如果是相对路径，则相对于配置文件所在目录
                if "path" in config_data["register_node_list_from_csv_path"]:
                    csv_path = config_data["register_node_list_from_csv_path"]["path"]
                    if not os.path.isabs(csv_path):
                        # 转换为绝对路径
                        csv_path = os.path.join(config_dir, csv_path)
                        config_data["register_node_list_from_csv_path"]["path"] = csv_path
                
                # 直接使用字典
                self.register_node_list_from_csv_path(**config_data["register_node_list_from_csv_path"])
            
            # 处理工作流创建
            if "create_flow" in config_data:
                # 直接传递字典列表
                self.create_workflow_from_json(config_data["create_flow"])
                # 将工作流注册为实例方法
                self.register_workflows_as_methods()
                
            # 将所有节点注册为属性
            self._register_nodes_as_attributes()
                
            logger.info(f"成功从 {config_path} 加载配置")
        except Exception as e:
            logger.error(f"加载配置文件 {config_path} 失败: {e}")
            traceback.print_exc()
    
    def disconnect(self):
        # 停止刷新线程
        self.stop_node_refresh()
        
        if self.client:
            self.client.disconnect()
            logger.info("OPC UA client disconnected")


if __name__ == '__main__':
    # 示例用法
    
    # 使用配置文件创建客户端并自动注册工作流
    import os
    current_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(current_dir, "opcua_huairou.json")
    
    # 创建OPC UA客户端并加载配置
    try:
        client = OpcUaClient(
            url="opc.tcp://localhost:4840/freeopcua/server/",  # 替换为实际的OPC UA服务器地址
            config_path=config_path  # 传入配置文件路径
        )
        
        # 列出所有已注册的工作流
        print("\n已注册的工作流：")
        for workflow_name in client.workflow_name:
            print(f" - {workflow_name}")
        
        # 测试trigger_grab_action工作流 - 使用英文参数名
        print("\n测试trigger_grab_action工作流 - 使用英文参数名:")
        client.trigger_grab_action(reaction_tank_number=2, raw_tank_number=3)
        
        # 读取节点值 - 使用英文节点名
        grab_complete = client.get_node_value("grab_complete")
        reaction_tank = client.get_node_value("reaction_tank_number")
        raw_tank = client.get_node_value("raw_tank_number")
        
        print(f"\n执行后状态检查 (使用英文节点名):")
        print(f" - 抓取完成状态: {grab_complete}")
        print(f" - 当前反应罐号码: {reaction_tank}")
        print(f" - 当前原料罐号码: {raw_tank}")
        
        # 测试节点值写入 - 使用英文节点名
        print("\n测试节点值写入 (使用英文节点名):")
        success = client.set_node_value("atomization_fast_speed", 150.5)
        print(f" - 写入搅拌浆雾化快速 = 150.5, 结果: {success}")
        
        # 读取写入的值
        atomization_speed = client.get_node_value("atomization_fast_speed")
        print(f" - 读取搅拌浆雾化快速: {atomization_speed}")
        
        # 断开连接
        client.disconnect()
        
    except Exception as e:
        print(f"错误: {e}")
        traceback.print_exc()
    
    
