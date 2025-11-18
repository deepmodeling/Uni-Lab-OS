# coding=utf-8
from enum import Enum
from abc import ABC, abstractmethod
from typing import Tuple, Union, Optional, Any, List

from opcua import Client, Node
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
                # 检查是否是NumericNodeId(ns=X;i=Y)格式
                if "NumericNodeId" in self._node_id:
                    # 从字符串中提取命名空间和标识符
                    import re
                    match = re.search(r'ns=(\d+);i=(\d+)', self._node_id)
                    if match:
                        ns = int(match.group(1))
                        identifier = int(match.group(2))
                        node_id = NodeId(identifier, ns)
                        self._node = self._client.get_node(node_id)
                    else:
                        raise ValueError(f"无法解析节点ID: {self._node_id}")
                else:
                    # 直接使用节点ID字符串
                    self._node = self._client.get_node(self._node_id)
            except Exception as e:
                print(f"获取节点失败: {self._node_id}, 错误: {e}")
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
                # 检查是否是NumericNodeId(ns=X;i=Y)格式
                if "NumericNodeId" in self._parent_node_id:
                    # 从字符串中提取命名空间和标识符
                    import re
                    match = re.search(r'ns=(\d+);i=(\d+)', self._parent_node_id)
                    if match:
                        ns = int(match.group(1))
                        identifier = int(match.group(2))
                        node_id = NodeId(identifier, ns)
                        self._parent_node = self._client.get_node(node_id)
                    else:
                        raise ValueError(f"无法解析父节点ID: {self._parent_node_id}")
                else:
                    # 直接使用节点ID字符串
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