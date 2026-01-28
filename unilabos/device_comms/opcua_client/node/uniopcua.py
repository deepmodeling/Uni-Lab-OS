# coding=utf-8
from enum import Enum
from abc import ABC, abstractmethod
from typing import Tuple, Union, Optional, Any, List

from opcua import Client, Node, ua
from opcua.ua import NodeId, NodeClass, VariantType


class DataType(Enum):
    BOOLEAN = VariantType.Boolean
    SBYTE = VariantType.SByte
    BYTE = VariantType.Byte
    INT16 = VariantType.Int16
    UINT16 = VariantType.UInt16
    INT32 = VariantType.Int32
    UINT32 = VariantType.UInt32
    INT64 = VariantType.Int64
    UINT64 = VariantType.UInt64
    FLOAT = VariantType.Float
    DOUBLE = VariantType.Double
    STRING = VariantType.String
    DATETIME = VariantType.DateTime
    BYTESTRING = VariantType.ByteString


class NodeType(Enum):
    VARIABLE = NodeClass.Variable
    OBJECT = NodeClass.Object
    METHOD = NodeClass.Method
    OBJECTTYPE = NodeClass.ObjectType
    VARIABLETYPE = NodeClass.VariableType
    REFERENCETYPE = NodeClass.ReferenceType
    DATATYPE = NodeClass.DataType
    VIEW = NodeClass.View


class Base(ABC):
    def __init__(self, client: Client, name: str, node_id: str, typ: NodeType, data_type: DataType):
        self._node_id: str = node_id
        self._client = client
        self._name = name
        self._type = typ
        self._data_type = data_type
        self._node: Optional[Node] = None
        
    def _get_node(self) -> Node:
        if self._node is None:
            try:
                # 尝试多种 NodeId 字符串格式解析，兼容不同服务器/库的输出
                # 可能的格式示例: 'ns=2;i=1234', 'ns=2;s=SomeString',
                # 'StringNodeId(ns=4;s=OPC|变量名)', 'NumericNodeId(ns=2;i=1234)' 等
                import re

                nid = self._node_id
                # 如果已经是 NodeId/Node 对象（库用户可能传入），直接使用
                try:
                    from opcua.ua import NodeId as UaNodeId
                    if isinstance(nid, UaNodeId):
                        self._node = self._client.get_node(nid)
                        return self._node
                except Exception:
                    # 若导入或类型判断失败，则继续下一步
                    pass

                # 直接以字符串形式处理
                if isinstance(nid, str):
                    nid = nid.strip()
                    
                    # 处理包含类名的格式，如 'StringNodeId(ns=4;s=...)' 或 'NumericNodeId(ns=2;i=...)'
                    # 提取括号内的内容
                    match_wrapped = re.match(r'(String|Numeric|Byte|Guid|TwoByteNode|FourByteNode)NodeId\((.*)\)', nid)
                    if match_wrapped:
                        # 提取括号内的实际 node_id 字符串
                        nid = match_wrapped.group(2).strip()

                    # 常见短格式 'ns=2;i=1234' 或 'ns=2;s=SomeString'
                    if re.match(r'^ns=\d+;[is]=', nid):
                        self._node = self._client.get_node(nid)
                    else:
                        # 尝试提取 ns 和 i 或 s
                        # 对于字符串标识符，可能包含特殊字符，使用非贪婪匹配
                        m_num = re.search(r'ns=(\d+);i=(\d+)', nid)
                        m_str = re.search(r'ns=(\d+);s=(.+?)(?:\)|$)', nid)
                        if m_num:
                            ns = int(m_num.group(1))
                            identifier = int(m_num.group(2))
                            node_id = NodeId(identifier, ns)
                            self._node = self._client.get_node(node_id)
                        elif m_str:
                            ns = int(m_str.group(1))
                            identifier = m_str.group(2).strip()
                            # 对于字符串标识符，直接使用字符串格式
                            node_id_str = f"ns={ns};s={identifier}"
                            self._node = self._client.get_node(node_id_str)
                        else:
                            # 回退：尝试直接传入字符串（有些实现接受其它格式）
                            try:
                                self._node = self._client.get_node(self._node_id)
                            except Exception as e:
                                # 输出更详细的错误信息供调试
                                print(f"获取节点失败(尝试直接字符串): {self._node_id}, 错误: {e}")
                                raise
                else:
                    # 非字符串，尝试直接使用
                    self._node = self._client.get_node(self._node_id)
            except Exception as e:
                print(f"获取节点失败: {self._node_id}, 错误: {e}")
                # 添加额外提示，帮助定位 BadNodeIdUnknown 问题
                print("提示: 请确认该 node_id 是否来自当前连接的服务器地址空间，" \
                      "以及 CSV/配置中名称与服务器 BrowseName 是否匹配。")
                raise
        return self._node

    @abstractmethod
    def read(self) -> Tuple[Any, bool]:
        """读取节点值，返回(值, 是否出错)"""
        pass
    
    @abstractmethod
    def write(self, value: Any) -> bool:
        """写入节点值，返回是否出错"""
        pass
    
    @property
    def type(self) -> NodeType:
        return self._type
    
    @property
    def node_id(self) -> str:
        return self._node_id

    @property
    def name(self) -> str:
        return self._name


class Variable(Base):
    def __init__(self, client: Client, name: str, node_id: str, data_type: DataType):
        super().__init__(client, name, node_id, NodeType.VARIABLE, data_type)

    def read(self) -> Tuple[Any, bool]:
        try:
            value = self._get_node().get_value()
            return value, False
        except Exception as e:
            print(f"读取变量 {self._name} 失败: {e}")
            return None, True

    def write(self, value: Any) -> bool:
        try:
            # 如果声明了数据类型，则尝试转换并使用对应的 Variant 写入
            coerced = value
            try:
                if self._data_type is not None:
                    # 基于声明的数据类型做简单类型转换
                    dt = self._data_type
                    if dt in (DataType.SBYTE, DataType.BYTE, DataType.INT16, DataType.UINT16,
                              DataType.INT32, DataType.UINT32, DataType.INT64, DataType.UINT64):
                        # 数值类型 -> int
                        if isinstance(value, str):
                            coerced = int(value)
                        else:
                            coerced = int(value)
                    elif dt in (DataType.FLOAT, DataType.DOUBLE):
                        if isinstance(value, str):
                            coerced = float(value)
                        else:
                            coerced = float(value)
                    elif dt == DataType.BOOLEAN:
                        if isinstance(value, str):
                            v = value.strip().lower()
                            if v in ("true", "1", "yes", "on"):
                                coerced = True
                            elif v in ("false", "0", "no", "off"):
                                coerced = False
                            else:
                                coerced = bool(value)
                        else:
                            coerced = bool(value)
                    elif dt == DataType.STRING or dt == DataType.BYTESTRING or dt == DataType.DATETIME:
                        coerced = str(value)

                    # 使用 ua.Variant 明确指定 VariantType
                    try:
                        variant = ua.Variant(coerced, dt.value)
                        self._get_node().set_value(variant)
                    except Exception:
                        # 回退：有些 set_value 实现接受 (value, variant_type)
                        try:
                            self._get_node().set_value(coerced, dt.value)
                        except Exception:
                            # 最后回退到直接写入（保持兼容性）
                            self._get_node().set_value(coerced)
                else:
                    # 未声明数据类型，直接写入
                    self._get_node().set_value(value)
            except Exception:
                # 若在转换或按数据类型写入失败，尝试直接写入原始值并让上层捕获错误
                self._get_node().set_value(value)

            return False
        except Exception as e:
            print(f"写入变量 {self._name} 失败: {e}")
            return True


class Method(Base):
    def __init__(self, client: Client, name: str, node_id: str, parent_node_id: str, data_type: DataType):
        super().__init__(client, name, node_id, NodeType.METHOD, data_type)
        self._parent_node_id = parent_node_id
        self._parent_node = None
        
    def _get_parent_node(self) -> Node:
        if self._parent_node is None:
            try:
                # 处理父节点ID，使用与_get_node相同的解析逻辑
                import re
                
                nid = self._parent_node_id
                
                # 如果已经是 NodeId 对象，直接使用
                try:
                    from opcua.ua import NodeId as UaNodeId
                    if isinstance(nid, UaNodeId):
                        self._parent_node = self._client.get_node(nid)
                        return self._parent_node
                except Exception:
                    pass
                
                # 字符串处理
                if isinstance(nid, str):
                    nid = nid.strip()
                    
                    # 处理包含类名的格式
                    match_wrapped = re.match(r'(String|Numeric|Byte|Guid|TwoByteNode|FourByteNode)NodeId\((.*)\)', nid)
                    if match_wrapped:
                        nid = match_wrapped.group(2).strip()
                    
                    # 常见短格式
                    if re.match(r'^ns=\d+;[is]=', nid):
                        self._parent_node = self._client.get_node(nid)
                    else:
                        # 提取 ns 和 i 或 s
                        m_num = re.search(r'ns=(\d+);i=(\d+)', nid)
                        m_str = re.search(r'ns=(\d+);s=(.+?)(?:\)|$)', nid)
                        if m_num:
                            ns = int(m_num.group(1))
                            identifier = int(m_num.group(2))
                            node_id = NodeId(identifier, ns)
                            self._parent_node = self._client.get_node(node_id)
                        elif m_str:
                            ns = int(m_str.group(1))
                            identifier = m_str.group(2).strip()
                            node_id_str = f"ns={ns};s={identifier}"
                            self._parent_node = self._client.get_node(node_id_str)
                        else:
                            # 回退
                            self._parent_node = self._client.get_node(self._parent_node_id)
                else:
                    self._parent_node = self._client.get_node(self._parent_node_id)
            except Exception as e:
                print(f"获取父节点失败: {self._parent_node_id}, 错误: {e}")
                raise
        return self._parent_node

    def read(self) -> Tuple[Any, bool]:
        """方法节点不支持读取操作"""
        return None, True

    def write(self, value: Any) -> bool:
        """方法节点不支持写入操作"""
        return True
        
    def call(self, *args) -> Tuple[Any, bool]:
        """调用方法，返回(返回值, 是否出错)"""
        try:
            result = self._get_parent_node().call_method(self._get_node(), *args)
            return result, False
        except Exception as e:
            print(f"调用方法 {self._name} 失败: {e}")
            return None, True


class Object(Base):
    def __init__(self, client: Client, name: str, node_id: str):
        super().__init__(client, name, node_id, NodeType.OBJECT, None)
        
    def read(self) -> Tuple[Any, bool]:
        """对象节点不支持直接读取操作"""
        return None, True

    def write(self, value: Any) -> bool:
        """对象节点不支持直接写入操作"""
        return True
    
    def get_children(self) -> Tuple[List[Node], bool]:
        """获取子节点列表，返回(子节点列表, 是否出错)"""
        try:
            children = self._get_node().get_children()
            return children, False
        except Exception as e:
            print(f"获取对象 {self._name} 的子节点失败: {e}")
            return [], True 