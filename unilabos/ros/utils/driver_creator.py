"""
设备类实例创建工厂

这个模块包含用于创建设备类实例的工厂类。
基础工厂类提供通用的实例创建方法，而特定工厂类提供针对特定设备类的创建方法。
"""

import asyncio
import inspect
import traceback
from abc import abstractmethod
from typing import Type, Any, Dict, Optional, TypeVar, Generic, List

from unilabos.resources.resource_tracker import DeviceNodeResourceTracker, ResourceTreeSet, ResourceDictInstance, \
    ResourceTreeInstance
from unilabos.utils import logger
from unilabos.utils.cls_creator import create_instance_from_config

# 定义泛型类型变量
T = TypeVar("T")


class ClassCreator(Generic[T]):
    @abstractmethod
    def create_instance(self, *args, **kwargs) -> T:
        pass


class DeviceClassCreator(Generic[T]):
    """
    设备类实例创建器基类

    这个类提供了从任意类创建实例的通用方法。
    """

    def __init__(self, cls: Type[T], children: List[ResourceDictInstance], resource_tracker: DeviceNodeResourceTracker):
        """
        初始化设备类创建器

        Args:
            cls: 要创建实例的类
        """
        self.device_cls = cls
        self.device_instance: Optional[T] = None
        self.children = children
        self.resource_tracker = resource_tracker

    def attach_resource(self):
        """
        附加资源到设备类实例
        """
        if self.device_instance is not None:
            for c in self.children:
                if c.res_content.type != "device":
                    self.resource_tracker.add_resource(c.get_plr_nested_dict())

    def create_instance(self, data: Dict[str, Any]) -> T:
        """
        创建设备类实例

        Args:


        Returns:
            设备类的实例
        """
        self.device_instance = create_instance_from_config(
            {
                "_cls": self.device_cls.__module__ + ":" + self.device_cls.__name__,
                "_params": data,
            }
        )
        self.post_create()
        self.attach_resource()
        return self.device_instance

    def get_instance(self) -> Optional[T]:
        """
        获取当前实例

        Returns:
            当前设备类实例，如果尚未创建则返回None
        """
        return self.device_instance

    def post_create(self):
        pass


class PyLabRobotCreator(DeviceClassCreator[T]):
    """
    PyLabRobot设备类创建器

    这个类提供了针对PyLabRobot设备类的实例创建方法，特别处理deserialize方法。
    """

    def __init__(self, cls: Type[T], children: List[ResourceDictInstance], resource_tracker: DeviceNodeResourceTracker):
        """
        初始化PyLabRobot设备类创建器

        Args:
            cls: PyLabRobot设备类
            children: 子资源字典，用于资源替换
        """
        super().__init__(cls, children, resource_tracker)
        # 检查类是否具有deserialize方法
        self.has_deserialize = hasattr(cls, "deserialize") and callable(getattr(cls, "deserialize"))
        if not self.has_deserialize:
            logger.warning(f"类 {cls.__name__} 没有deserialize方法，将使用标准构造函数")

    def attach_resource(self):
        pass  # 只能增加实例化物料，原来默认物料仅为字典查询

    # def _process_resource_mapping(self, resource, source_type):
    #     if source_type == dict:
    #         from pylabrobot.resources.resource import Resource
    #
    #         return nested_dict_to_list(resource), Resource
    #     return resource, source_type

    def _process_resource_references(
        self, data: Any, to_dict=False, states=None, prefix_path="", name_to_uuid=None
    ) -> Any:
        """
        递归处理资源引用，替换_resource_child_name对应的资源

        Args:
            data: 需要处理的数据，可能是字典、列表或其他类型
            to_dict: 是否返回字典形式的资源
            states: 用于保存所有资源状态
            prefix_path: 当前递归路径
            name_to_uuid: name到uuid的映射字典

        Returns:
            处理后的数据
        """
        from pylabrobot.resources import Resource

        if states is None:
            states = {}

        if isinstance(data, dict):
            if "_resource_child_name" in data:
                child_name = data["_resource_child_name"]
                resource: Optional[ResourceDictInstance] = None
                for child in self.children:
                    if child.res_content.name == child_name:
                        resource = child
                if resource is not None:
                    if "_resource_type" in data:
                        type_path = data["_resource_type"]
                        try:
                            # target_type = import_manager.get_class(type_path)
                            # contain_model = not issubclass(target_type, Deck)
                            # resource, target_type = self._process_resource_mapping(resource, target_type)
                            res_tree = ResourceTreeInstance(resource)
                            res_tree_set = ResourceTreeSet([res_tree])
                            resource_instance: Resource = res_tree_set.to_plr_resources()[0]
                            # resource_instance: Resource = resource_ulab_to_plr(resource, contain_model)  # 带state
                            states[prefix_path] = resource_instance.serialize_all_state()
                            # 使用 prefix_path 作为 key 存储资源状态
                            if to_dict:
                                serialized = resource_instance.serialize()
                                states[prefix_path] = resource_instance.serialize_all_state()
                                return serialized
                            else:
                                self.resource_tracker.add_resource(resource_instance)
                                # 立即设置UUID，state已经在resource_ulab_to_plr中处理过了
                                if name_to_uuid:
                                    self.resource_tracker.loop_set_uuid(resource_instance, name_to_uuid)
                            return resource_instance
                        except Exception as e:
                            logger.warning(f"无法导入资源类型 {type_path}: {e}")
                            logger.warning(traceback.format_exc())
                    else:
                        logger.debug(f"找不到资源类型，请补全_resource_type {self.device_cls.__name__} {data.keys()}")
                    return resource
                else:
                    logger.warning(f"找不到资源引用 '{child_name}'，保持原值不变")

            # 递归处理每个键值
            result = {}
            for key, value in data.items():
                new_prefix = f"{prefix_path}.{key}" if prefix_path else key
                result[key] = self._process_resource_references(value, to_dict, states, new_prefix, name_to_uuid)
            return result

        elif isinstance(data, list):
            return [
                self._process_resource_references(item, to_dict, states, f"{prefix_path}[{i}]", name_to_uuid)
                for i, item in enumerate(data)
            ]

        else:
            return data

    def create_instance(self, data: Dict[str, Any]) -> Optional[T]:
        """
        从数据创建PyLabRobot设备实例

        Args:
            data: 用于反序列化的数据

        Returns:
            PyLabRobot设备类实例
        """
        deserialize_error = None
        stack = None

        # 递归遍历 children 构建 name_to_uuid 映射
        def collect_name_to_uuid(children_list: List[ResourceDictInstance], result: Dict[str, str]):
            """递归遍历嵌套的 children 字典，收集 name 到 uuid 的映射"""
            for child in children_list:
                if isinstance(child, ResourceDictInstance):
                    result[child.res_content.name] = child.res_content.uuid
                    collect_name_to_uuid(child.children, result)

        name_to_uuid = {}
        collect_name_to_uuid(self.children, name_to_uuid)
        if self.has_deserialize:
            deserialize_method = getattr(self.device_cls, "deserialize")
            spect = inspect.signature(deserialize_method)
            spec_args = spect.parameters
            for param_name, param_value in data.copy().items():
                if (
                    isinstance(param_value, dict)
                    and "_resource_child_name" in param_value
                    and "_resource_type" not in param_value
                ):
                    arg_value = spec_args[param_name].annotation
                    data[param_name]["_resource_type"] = self.device_cls.__module__ + ":" + arg_value
                    logger.debug(f"自动补充 _resource_type: {data[param_name]['_resource_type']}")

            # 首先处理资源引用
            states = {}
            processed_data = self._process_resource_references(
                data, to_dict=True, states=states, name_to_uuid=name_to_uuid
            )

            try:
                from pylabrobot.resources import Resource

                self.device_instance: Resource = deserialize_method(**processed_data)
                self.resource_tracker.loop_set_uuid(self.device_instance, name_to_uuid)
                all_states = self.device_instance.serialize_all_state()
                for k, v in states.items():
                    logger.debug(f"PyLabRobot反序列化设置状态：{k}")
                    for kk, vv in all_states.items():
                        if kk not in v:
                            v[kk] = vv
                    self.device_instance.load_all_state(v)
                self.resource_tracker.add_resource(self.device_instance)
                self.post_create()  # 对应DeviceClassCreator进行调用
                return self.device_instance  # type: ignore
            except Exception as e:
                # 先静默继续，尝试另外一种创建方法
                deserialize_error = e
                stack = traceback.format_exc()

        if self.device_instance is None:
            try:
                spect = inspect.signature(self.device_cls.__init__)
                spec_args = spect.parameters
                for param_name, param_value in data.copy().items():
                    if (
                        isinstance(param_value, dict)
                        and "_resource_child_name" in param_value
                        and "_resource_type" not in param_value
                    ):
                        arg_value = spec_args[param_name].annotation
                        data[param_name]["_resource_type"] = self.device_cls.__module__ + ":" + arg_value
                        logger.debug(f"自动补充 _resource_type: {data[param_name]['_resource_type']}")
                processed_data = self._process_resource_references(data, to_dict=False, name_to_uuid=name_to_uuid)
                self.device_instance = super(PyLabRobotCreator, self).create_instance(processed_data)  # 补全变量后直接调用，调用的自身的attach_resource
            except Exception as e:
                logger.error(f"PyLabRobot创建实例失败: {e}")
                logger.error(f"PyLabRobot创建实例堆栈: {traceback.format_exc()}")
            finally:
                if self.device_instance is None:
                    if deserialize_error:
                        logger.error(f"PyLabRobot反序列化失败: {deserialize_error}")
                        logger.error(f"PyLabRobot反序列化堆栈: {stack}")

        return self.device_instance

    def post_create(self):
        if hasattr(self.device_instance, "setup") and asyncio.iscoroutinefunction(
            getattr(self.device_instance, "setup")
        ):
            from unilabos.ros.nodes.base_device_node import ROS2DeviceNode

            def done_cb(*args):
                from pylabrobot.resources import set_volume_tracking

                # from pylabrobot.resources import set_tip_tracking
                set_volume_tracking(enabled=True)
                # set_tip_tracking(enabled=True)  # 序列化tip_spot has为False
                logger.debug(f"PyLabRobot设备实例 {self.device_instance} 设置完成")
                from unilabos.config.config import BasicConfig

                if BasicConfig.vis_2d_enable:
                    from pylabrobot.visualizer.visualizer import Visualizer

                    vis = Visualizer(resource=self.device_instance, open_browser=True)

                    def vis_done_cb(*args):
                        logger.info(f"PyLabRobot设备实例开启了Visualizer {self.device_instance}")

                    ROS2DeviceNode.run_async_func(vis.setup).add_done_callback(vis_done_cb)
                    logger.debug(f"PyLabRobot设备实例提交开启Visualizer {self.device_instance}")

            ROS2DeviceNode.run_async_func(getattr(self.device_instance, "setup")).add_done_callback(done_cb)


class WorkstationNodeCreator(DeviceClassCreator[T]):
    """
    WorkstationNode设备类创建器

    这个类提供了针对WorkstationNode设备类的实例创建方法，处理children参数。
    """

    def __init__(self, cls: Type[T], children: List[ResourceDictInstance], resource_tracker: DeviceNodeResourceTracker):
        """
        初始化WorkstationNode设备类创建器

        Args:
            cls: WorkstationNode设备类
            children: 子资源字典，用于资源替换
        """
        super().__init__(cls, children, resource_tracker)

    def create_instance(self, data: Dict[str, Any]) -> T:
        """
        从数据创建WorkstationNode设备实例

        Args:
            data: 用于创建实例的数据

        Returns:
            WorkstationNode设备类实例
        """
        try:
            # 创建实例，额外补充一个给protocol node的字段，后面考虑取消
            data["children"] = self.children
            for child in self.children:
                if child.res_content.type != "device":
                    self.resource_tracker.add_resource(child.get_plr_nested_dict())
            deck_dict = data.get("deck")
            if deck_dict:
                from pylabrobot.resources import Deck, Resource

                plrc = PyLabRobotCreator(Deck, self.children, self.resource_tracker)
                deck = plrc.create_instance(deck_dict)
                data["deck"] = deck
            else:
                data["deck"] = None
            self.device_instance = super(WorkstationNodeCreator, self).create_instance(data)
            self.post_create()
            return self.device_instance
        except Exception as e:
            logger.error(f"WorkstationNode创建实例失败: {e}")
            logger.error(f"WorkstationNode创建实例堆栈: {traceback.format_exc()}")
            raise
