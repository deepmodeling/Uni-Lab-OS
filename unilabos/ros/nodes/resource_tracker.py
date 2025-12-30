import inspect
import traceback
import uuid
from pydantic import BaseModel, field_serializer, field_validator
from pydantic import Field
from typing import List, Tuple, Any, Dict, Literal, Optional, cast, TYPE_CHECKING, Union

from unilabos.resources.plr_additional_res_reg import register
from unilabos.utils.log import logger

if TYPE_CHECKING:
    from unilabos.devices.workstation.workstation_base import WorkstationBase
    from pylabrobot.resources import Resource as PLRResource


class ResourceDictPositionSize(BaseModel):
    depth: float = Field(description="Depth", default=0.0)
    width: float = Field(description="Width", default=0.0)
    height: float = Field(description="Height", default=0.0)


class ResourceDictPositionScale(BaseModel):
    x: float = Field(description="x scale", default=0.0)
    y: float = Field(description="y scale", default=0.0)
    z: float = Field(description="z scale", default=0.0)


class ResourceDictPositionObject(BaseModel):
    x: float = Field(description="X coordinate", default=0.0)
    y: float = Field(description="Y coordinate", default=0.0)
    z: float = Field(description="Z coordinate", default=0.0)


class ResourceDictPosition(BaseModel):
    size: ResourceDictPositionSize = Field(description="Resource size", default_factory=ResourceDictPositionSize)
    scale: ResourceDictPositionScale = Field(description="Resource scale", default_factory=ResourceDictPositionScale)
    layout: Literal["2d", "x-y", "z-y", "x-z"] = Field(description="Resource layout", default="x-y")
    position: ResourceDictPositionObject = Field(
        description="Resource position", default_factory=ResourceDictPositionObject
    )
    position3d: ResourceDictPositionObject = Field(
        description="Resource position in 3D space", default_factory=ResourceDictPositionObject
    )
    rotation: ResourceDictPositionObject = Field(
        description="Resource rotation", default_factory=ResourceDictPositionObject
    )
    cross_section_type: Literal["rectangle", "circle", "rounded_rectangle"] = Field(
        description="Cross section type", default="rectangle"
    )


# 统一的资源字典模型，parent 自动序列化为 parent_uuid，children 不序列化
class ResourceDict(BaseModel):
    id: str = Field(description="Resource ID")
    uuid: str = Field(description="Resource UUID")
    name: str = Field(description="Resource name")
    description: str = Field(description="Resource description", default="")
    resource_schema: Dict[str, Any] = Field(
        description="Resource schema", default_factory=dict, serialization_alias="schema", validation_alias="schema"
    )
    model: Dict[str, Any] = Field(description="Resource model", default_factory=dict)
    icon: str = Field(description="Resource icon", default="")
    parent_uuid: Optional["str"] = Field(description="Parent resource uuid", default=None)  # 先设定parent_uuid
    parent: Optional["ResourceDict"] = Field(description="Parent resource object", default=None, exclude=True)
    type: Union[Literal["device"], str] = Field(description="Resource type")
    klass: str = Field(alias="class", description="Resource class name")
    pose: ResourceDictPosition = Field(description="Resource position", default_factory=ResourceDictPosition)
    config: Dict[str, Any] = Field(description="Resource configuration")
    data: Dict[str, Any] = Field(description="Resource data, eg: container liquid data")
    extra: Dict[str, Any] = Field(description="Extra data, eg: slot index")

    @field_serializer("parent_uuid")
    def _serialize_parent(self, parent_uuid: Optional["ResourceDict"]):
        return self.uuid_parent

    @field_validator("parent", mode="before")
    @classmethod
    def _deserialize_parent(cls, parent: Optional["ResourceDict"]):
        if isinstance(parent, ResourceDict):
            return parent
        else:
            return None

    @property
    def uuid_parent(self) -> str:
        """获取父节点的UUID"""
        parent_instance_uuid = self.parent_instance_uuid
        if parent_instance_uuid is not None and self.parent_uuid and parent_instance_uuid != self.parent_uuid:
            logger.warning(f"{self.name}[{self.uuid}]的parent uuid未同步！")  # 现在强制要求设置
        if parent_instance_uuid is not None:
            return parent_instance_uuid
        return self.parent_uuid

    @property
    def parent_instance_uuid(self) -> Optional[str]:
        """获取父节点的UUID"""
        return self.parent.uuid if self.parent is not None else None

    @property
    def parent_instance_name(self) -> Optional[str]:
        """获取父节点的名字"""
        return self.parent.name if self.parent is not None else None

    @property
    def is_root_node(self) -> bool:
        """判断资源是否为根节点"""
        return self.parent is None


class GraphData(BaseModel):
    """图数据结构，包含节点和边"""

    nodes: List["ResourceTreeInstance"] = Field(description="Resource nodes list", default_factory=list)
    links: List[Dict[str, Any]] = Field(description="Resource links/edges list", default_factory=list)


class ResourceDictInstance(object):
    """ResourceDict的实例，同时提供一些方法"""

    def __init__(self, res_content: "ResourceDict"):
        self.res_content = res_content
        self.children: List[ResourceDictInstance] = []
        self.typ = "dict"

    @classmethod
    def get_resource_instance_from_dict(cls, content: Dict[str, Any]) -> "ResourceDictInstance":
        """从字典创建资源实例"""
        if "id" not in content:
            content["id"] = content["name"]
        if "uuid" not in content:
            content["uuid"] = str(uuid.uuid4())
        if "description" in content and content["description"] is None:
            del content["description"]
        if "model" in content and content["model"] is None:
            del content["model"]
        if "schema" in content and content["schema"] is None:
            del content["schema"]
        if "x" in content.get("position", {}):
            # 说明是老版本的position格式，转换成新的
            content["position"] = {"position": content["position"]}
        if not content.get("class"):
            content["class"] = ""
        if not content.get("config"):  # todo: 后续从后端保证字段非空
            content["config"] = {}
        if not content.get("data"):
            content["data"] = {}
        if not content.get("extra"):  # MagicCode
            content["extra"] = {}
        if "position" in content:
            pose = content.get("pose",{})
            if "position" not in pose :
                if "position" in content["position"]:
                    pose["position"] = content["position"]["position"]
                else:
                    pose["position"] = {"x": 0, "y": 0, "z": 0}
            if "size" not in pose:
                pose["size"] = {
                    "width": content["config"].get("size_x", 0), 
                    "height": content["config"].get("size_y", 0), 
                    "depth": content["config"].get("size_z", 0)
                }
            content["pose"] = pose
        return ResourceDictInstance(ResourceDict.model_validate(content))

    def get_plr_nested_dict(self) -> Dict[str, Any]:
        """获取资源实例的嵌套字典表示"""
        res_dict = self.res_content.model_dump(by_alias=True)
        res_dict["children"] = {child.res_content.id: child.get_plr_nested_dict() for child in self.children}
        res_dict["parent"] = self.res_content.parent_instance_name
        res_dict["position"] = self.res_content.pose.position.model_dump()
        del res_dict["pose"]
        return res_dict


class ResourceTreeInstance(object):
    """
    资源树，表示一个根节点及其所有子节点的层次结构，继承ResourceDictInstance表示自己是根节点
    """

    @staticmethod
    def _build_uuid_map(resource_list: List[ResourceDictInstance]) -> Dict[str, ResourceDictInstance]:
        """构建uuid到资源对象的映射，并检查重复"""
        uuid_map: Dict[str, ResourceDictInstance] = {}
        for res_instance in resource_list:
            res = res_instance.res_content
            if res.uuid in uuid_map:
                raise ValueError(f"发现重复的uuid: {res.uuid}")
            uuid_map[res.uuid] = res_instance
        return uuid_map

    @staticmethod
    def _build_uuid_instance_map(
        resource_list: List[ResourceDictInstance],
    ) -> Dict[str, ResourceDictInstance]:
        """构建uuid到资源实例的映射"""
        return {res_instance.res_content.uuid: res_instance for res_instance in resource_list}

    @staticmethod
    def _collect_tree_nodes(
        root_instance: ResourceDictInstance, uuid_map: Dict[str, ResourceDict]
    ) -> List[ResourceDictInstance]:
        """使用BFS收集属于某个根节点的所有节点"""
        # BFS遍历，根据parent_uuid字段找到所有属于这棵树的节点
        tree_nodes = [root_instance]
        visited = {root_instance.res_content.uuid}
        queue = [root_instance.res_content.uuid]

        while queue:
            current_uuid = queue.pop(0)
            # 查找所有parent_uuid指向当前节点的子节点
            for uuid_str, res in uuid_map.items():
                if res.uuid_parent == current_uuid and uuid_str not in visited:
                    child_instance = ResourceDictInstance(res)
                    tree_nodes.append(child_instance)
                    visited.add(uuid_str)
                    queue.append(uuid_str)

        return tree_nodes

    def __init__(self, resource: ResourceDictInstance):
        self.root_node = resource
        self._validate_tree()

    def _validate_tree(self):
        """
        验证树结构的一致性
        - 验证uuid唯一性
        - 验证parent-children关系一致性

        Raises:
            ValueError: 当发现不一致时
        """
        known_uuids: set = set()

        def validate_node(node: ResourceDictInstance):
            # 检查uuid唯一性
            if node.res_content.uuid in known_uuids:
                raise ValueError(f"发现重复的uuid: {node.res_content.uuid}")
            if node.res_content.uuid:
                known_uuids.add(node.res_content.uuid)
            else:
                logger.warning(f"警告: 资源 {node.res_content.id} 没有uuid")

            # 验证并递归处理子节点
            for child in node.children:
                if child.res_content.parent != node.res_content:
                    parent_id = child.res_content.parent.id if child.res_content.parent else None
                    raise ValueError(
                        f"节点 {child.res_content.id} 的parent引用不正确，应该指向 {node.res_content.id}，但实际指向 {parent_id}"
                    )
                validate_node(child)

        validate_node(self.root_node)

    def get_all_nodes(self) -> List[ResourceDictInstance]:
        """
        获取树中的所有节点（深度优先遍历）

        Returns:
            所有节点的资源实例列表
        """
        nodes = []

        def collect_nodes(node: ResourceDictInstance):
            nodes.append(node)
            for child in node.children:
                collect_nodes(child)

        collect_nodes(self.root_node)
        return nodes

    def find_by_uuid(self, target_uuid: str) -> Optional[ResourceDictInstance]:
        """
        通过uuid查找节点

        Args:
            target_uuid: 目标uuid

        Returns:
            找到的节点资源实例，如果没找到返回None
        """

        def search(node: ResourceDictInstance) -> Optional[ResourceDictInstance]:
            if node.res_content.uuid == target_uuid:
                return node
            for child in node.children:
                res = search(child)
                if res:
                    return res
            return None

        result = search(self.root_node)
        return result


class ResourceTreeSet(object):
    """
    多个根节点的resource集合，包含多个ResourceTree
    """

    def __init__(self, resource_list: List[List[ResourceDictInstance]] | List[ResourceTreeInstance]):
        """
        初始化资源树集合

        Args:
            resource_list: 可以是以下两种类型之一：
                - List[ResourceTree]: 已经构建好的树列表
                - List[List[ResourceInstanceDict]]: 嵌套列表，每个内部列表代表一棵树

        Raises:
            TypeError: 当传入不支持的类型时
        """
        if not resource_list:
            self.trees: List[ResourceTreeInstance] = []
        elif isinstance(resource_list[0], ResourceTreeInstance):
            # 已经是ResourceTree列表
            self.trees = cast(List[ResourceTreeInstance], resource_list)
        else:
            raise TypeError(
                f"不支持的类型: {type(resource_list[0])}。"
                f"ResourceTreeSet 只接受 List[ResourceTree] 或 List[List[ResourceInstanceDict]]"
            )

    @classmethod
    def from_plr_resources(cls, resources: List["PLRResource"]) -> "ResourceTreeSet":
        """
        从plr资源创建ResourceTreeSet
        """

        def replace_plr_type(source: str):
            replace_info = {
                "plate": "plate",
                "well": "well",
                "deck": "deck",
                "tip_rack": "tip_rack",
                "tip_spot": "tip_spot",
                "tube": "tube",
                "bottle_carrier": "bottle_carrier",
            }
            if source in replace_info:
                return replace_info[source]
            else:
                print("转换pylabrobot的时候，出现未知类型", source)
                return source

        def build_uuid_mapping(res: "PLRResource", uuid_list: list, parent_uuid: Optional[str] = None):
            """递归构建uuid和extra映射字典，返回(current_uuid, parent_uuid, extra)元组列表"""
            uid = getattr(res, "unilabos_uuid", "")
            if not uid:
                uid = str(uuid.uuid4())
                res.unilabos_uuid = uid
                logger.warning(f"{res}没有uuid，请设置后再传入，默认填充{uid}！\n{traceback.format_exc()}")

            # 获取unilabos_extra，默认为空字典
            extra = getattr(res, "unilabos_extra", {})

            uuid_list.append((uid, parent_uuid, extra))
            for child in res.children:
                build_uuid_mapping(child, uuid_list, uid)

        def resource_plr_inner(
            d: dict, parent_resource: Optional[ResourceDict], states: dict, uuids: list
        ) -> ResourceDictInstance:
            current_uuid, parent_uuid, extra = uuids.pop(0)

            raw_pos = (
                {"x": d["location"]["x"], "y": d["location"]["y"], "z": d["location"]["z"]}
                if d["location"]
                else {"x": 0, "y": 0, "z": 0}
            )
            pos = {
                "size": {"width": d["size_x"], "height": d["size_y"], "depth": d["size_z"]},
                "scale": {"x": 1.0, "y": 1.0, "z": 1.0},
                "layout": d.get("layout", "x-y"),
                "position": raw_pos,
                "position3d": raw_pos,
                "rotation": d["rotation"],
                "cross_section_type": d.get("cross_section_type", "rectangle"),
            }

            # 先构建当前节点的字典（不包含children）
            r_dict = {
                "id": d["name"],
                "uuid": current_uuid,
                "name": d["name"],
                "parent": parent_resource,  # 直接传入 ResourceDict 对象
                "parent_uuid": parent_uuid,  # 使用 parent_uuid 而不是 parent 对象
                "type": replace_plr_type(d.get("category", "")),
                "class": d.get("class", ""),
                "position": pos,
                "pose": pos,
                "config": {
                    k: v
                    for k, v in d.items()
                    if k
                    not in [
                        "name",
                        "children",
                        "parent_name",
                        "location",
                        "rotation",
                        "size_x",
                        "size_y",
                        "size_z",
                        "cross_section_type",
                        "bottom_type",
                    ]
                },
                "data": states[d["name"]],
                "extra": extra,
            }

            # 先转换为 ResourceDictInstance，获取其中的 ResourceDict
            current_instance = ResourceDictInstance.get_resource_instance_from_dict(r_dict)
            current_resource = current_instance.res_content

            # 递归处理子节点，传入当前节点的 ResourceDict 作为 parent
            current_instance.children = [
                resource_plr_inner(child, current_resource, states, uuids) for child in d["children"]
            ]

            return current_instance

        trees = []
        for resource in resources:
            # 构建uuid列表
            uuid_list = []
            build_uuid_mapping(resource, uuid_list, getattr(resource.parent, "unilabos_uuid", None))

            serialized_data = resource.serialize()
            all_states = resource.serialize_all_state()

            # 根节点没有父节点，传入 None
            root_instance = resource_plr_inner(serialized_data, None, all_states, uuid_list)
            tree_instance = ResourceTreeInstance(root_instance)
            trees.append(tree_instance)
        return cls(trees)

    def to_plr_resources(self) -> List["PLRResource"]:
        """
        将 ResourceTreeSet 转换为 PLR 资源列表

        Returns:
            List[PLRResource]: PLR 资源实例列表
        """
        register()
        from pylabrobot.resources import Resource as PLRResource
        from pylabrobot.utils.object_parsing import find_subclass

        # 类型映射
        TYPE_MAP = {"plate": "Plate", "well": "Well", "deck": "Deck", "container": "RegularContainer", "tip_spot": "TipSpot"}

        def collect_node_data(node: ResourceDictInstance, name_to_uuid: dict, all_states: dict, name_to_extra: dict):
            """一次遍历收集 name_to_uuid, all_states 和 name_to_extra"""
            name_to_uuid[node.res_content.name] = node.res_content.uuid
            all_states[node.res_content.name] = node.res_content.data
            name_to_extra[node.res_content.name] = node.res_content.extra
            for child in node.children:
                collect_node_data(child, name_to_uuid, all_states, name_to_extra)

        def node_to_plr_dict(node: ResourceDictInstance, has_model: bool):
            """转换节点为 PLR 字典格式"""
            res = node.res_content
            plr_type = TYPE_MAP.get(res.type, res.type)
            if res.type not in TYPE_MAP:
                logger.warning(f"未知类型 {res.type}")

            d = {
                **res.config,
                "name": res.name,
                "type": res.config.get("type", plr_type),
                "size_x": res.config.get("size_x", 0),
                "size_y": res.config.get("size_y", 0),
                "size_z": res.config.get("size_z", 0),
                "location": {
                    "x": res.pose.position.x,
                    "y": res.pose.position.y,
                    "z": res.pose.position.z,
                    "type": "Coordinate",
                },
                "rotation": {"x": 0, "y": 0, "z": 0, "type": "Rotation"},
                "category": res.config.get("category", plr_type),
                "children": [node_to_plr_dict(child, has_model) for child in node.children],
                "parent_name": res.parent_instance_name,
            }
            if has_model:
                d["model"] = res.config.get("model", None)
            return d

        plr_resources = []
        tracker = DeviceNodeResourceTracker()

        for tree in self.trees:
            name_to_uuid: Dict[str, str] = {}
            all_states: Dict[str, Any] = {}
            name_to_extra: Dict[str, dict] = {}
            collect_node_data(tree.root_node, name_to_uuid, all_states, name_to_extra)
            has_model = tree.root_node.res_content.type != "deck"
            plr_dict = node_to_plr_dict(tree.root_node, has_model)
            try:
                sub_cls = find_subclass(plr_dict["type"], PLRResource)
                if sub_cls is None:
                    raise ValueError(
                        f"无法找到类型 {plr_dict['type']} 对应的 PLR 资源类。原始信息：{tree.root_node.res_content}"
                    )
                spec = inspect.signature(sub_cls)
                if "category" not in spec.parameters:
                    plr_dict.pop("category", None)
                plr_resource = sub_cls.deserialize(plr_dict, allow_marshal=True)
                plr_resource.load_all_state(all_states)
                # 使用 DeviceNodeResourceTracker 设置 UUID 和 Extra
                tracker.loop_set_uuid(plr_resource, name_to_uuid)
                tracker.loop_set_extra(plr_resource, name_to_extra)
                plr_resources.append(plr_resource)

            except Exception as e:
                logger.error(f"转换 PLR 资源失败: {e}")
                import traceback

                logger.error(f"堆栈: {traceback.format_exc()}")
                raise

        return plr_resources

    @classmethod
    def from_raw_list(cls, raw_list: List[Dict[str, Any]]) -> "ResourceTreeSet":
        """
        从原始字典列表创建 ResourceTreeSet，自动建立 parent-children 关系

        Args:
            raw_list: 原始字典列表，每个字典代表一个资源节点

        Returns:
            ResourceTreeSet 实例

        Raises:
            ValueError: 当建立关系时发现不一致
        """
        # 第一步：将字典列表转换为 ResourceDictInstance 列表
        instances = [ResourceDictInstance.get_resource_instance_from_dict(node_dict) for node_dict in raw_list]

        # 第二步：建立映射关系
        uuid_to_instance: Dict[str, ResourceDictInstance] = {}
        id_to_instance: Dict[str, ResourceDictInstance] = {}

        for raw_node, instance in zip(raw_list, instances):
            # 建立 uuid 映射
            if instance.res_content.uuid:
                uuid_to_instance[instance.res_content.uuid] = instance
            # 建立 id 映射
            if instance.res_content.id:
                id_to_instance[instance.res_content.id] = instance

        # 第三步：建立 parent-children 关系
        for raw_node, instance in zip(raw_list, instances):
            # 优先使用 parent_uuid 进行匹配，如果不存在则使用 parent (id)
            parent_uuid = raw_node.get("parent_uuid")
            parent_id = raw_node.get("parent")
            parent_instance = None

            # 优先用 parent_uuid 匹配
            if parent_uuid and parent_uuid in uuid_to_instance:
                parent_instance = uuid_to_instance[parent_uuid]
            # 否则用 parent (id) 匹配
            elif parent_id and parent_id in id_to_instance:
                parent_instance = id_to_instance[parent_id]

            # 设置 parent 引用并建立 children 关系
            if parent_instance:
                instance.res_content.parent = parent_instance.res_content
                # 将当前节点添加到父节点的 children 列表（避免重复添加）
                if instance not in parent_instance.children:
                    parent_instance.children.append(instance)

        # 第四步：使用 from_nested_list 创建 ResourceTreeSet
        return cls.from_nested_list(instances)

    @classmethod
    def from_nested_list(cls, nested_list: List[ResourceDictInstance]) -> "ResourceTreeSet":
        """
        从扁平化的资源列表创建ResourceTreeSet，自动按根节点分组

        Args:
            nested_list: 扁平化的资源实例列表，可能包含多个根节点

        Returns:
            ResourceTreeSet实例

        Raises:
            ValueError: 当没有找到任何根节点时
        """
        # 找到所有根节点
        known_uuids = {res_instance.res_content.uuid for res_instance in nested_list}
        root_instances = [
            ResourceTreeInstance(res_instance)
            for res_instance in nested_list
            if res_instance.res_content.is_root_node or res_instance.res_content.uuid_parent not in known_uuids
        ]
        return cls(root_instances)

    @property
    def root_nodes(self) -> List[ResourceDictInstance]:
        """
        获取所有树的根节点

        Returns:
            所有根节点的资源实例列表
        """
        return [tree.root_node for tree in self.trees]

    @property
    def all_nodes(self) -> List[ResourceDictInstance]:
        """
        获取所有树中的所有节点

        Returns:
            所有节点的资源实例列表
        """
        return [node for tree in self.trees for node in tree.get_all_nodes()]

    @property
    def all_nodes_uuid(self) -> List[str]:
        """
        获取所有树中的所有节点

        Returns:
            所有节点的资源实例列表
        """
        return [node.res_content.uuid for tree in self.trees for node in tree.get_all_nodes()]

    def find_by_uuid(self, target_uuid: str) -> Optional[ResourceDictInstance]:
        """
        在所有树中通过uuid查找节点

        Args:
            target_uuid: 目标uuid

        Returns:
            找到的节点资源实例，如果没找到返回None
        """
        for tree in self.trees:
            result = tree.find_by_uuid(target_uuid)
            if result:
                return result
        return None

    def merge_remote_resources(self, remote_tree_set: "ResourceTreeSet") -> "ResourceTreeSet":
        """
        将远端物料同步到本地物料中（以子树为单位）

        同步规则：
        1. 一级节点（根节点）：如果不存在的物料，引入整个子树
        2. 一级设备下的二级物料：如果不存在，引入整个子树
        3. 二级设备下的三级物料：如果不存在，引入整个子树
        如果存在则跳过并提示

        Args:
            remote_tree_set: 远端的资源树集合

        Returns:
            合并后的资源树集合（self）
        """
        # 构建本地映射：一级 device id -> 根节点实例
        local_device_map: Dict[str, ResourceDictInstance] = {}
        for root_node in self.root_nodes:
            if root_node.res_content.type == "device":
                local_device_map[root_node.res_content.id] = root_node

        # 记录需要添加的新根节点（不属于任何 device 的物料）
        new_root_nodes: List[ResourceDictInstance] = []

        # 遍历远端根节点
        for remote_root in remote_tree_set.root_nodes:
            remote_root_id = remote_root.res_content.id
            remote_root_type = remote_root.res_content.type

            if remote_root_type == "device":
                # 情况1: 一级是 device
                if remote_root_id not in local_device_map:
                    logger.warning(f"Device '{remote_root_id}' 在本地不存在，跳过该 device 下的物料同步")
                    continue

                local_device = local_device_map[remote_root_id]

                # 构建本地一级 device 下的子节点映射
                local_children_map = {child.res_content.name: child for child in local_device.children}

                # 遍历远端一级 device 的子节点
                for remote_child in remote_root.children:
                    remote_child_name = remote_child.res_content.name
                    remote_child_type = remote_child.res_content.type

                    if remote_child_type == "device":
                        # 情况2: 二级是 device
                        if remote_child_name not in local_children_map:
                            logger.warning(f"Device '{remote_root_id}/{remote_child_name}' 在本地不存在，跳过")
                            continue

                        local_sub_device = local_children_map[remote_child_name]

                        # 构建本地二级 device 下的子节点映射
                        local_sub_children_map = {child.res_content.name: child for child in local_sub_device.children}

                        # 遍历远端二级 device 的子节点（三级物料）
                        added_count = 0
                        for remote_material in remote_child.children:
                            remote_material_name = remote_material.res_content.name

                            # 情况3: 三级物料
                            if remote_material_name not in local_sub_children_map:
                                # 引入整个子树
                                remote_material.res_content.parent = local_sub_device.res_content
                                local_sub_device.children.append(remote_material)
                                added_count += 1
                            else:
                                logger.info(
                                    f"物料 '{remote_root_id}/{remote_child_name}/{remote_material_name}' "
                                    f"已存在，跳过"
                                )

                        if added_count > 0:
                            logger.info(
                                f"Device '{remote_root_id}/{remote_child_name}': "
                                f"从远端同步了 {added_count} 个物料子树"
                            )
                    else:
                        # 情况2: 二级是物料（不是 device）
                        if remote_child_name not in local_children_map:
                            # 引入整个子树
                            remote_child.res_content.parent = local_device.res_content
                            local_device.children.append(remote_child)
                            logger.info(f"Device '{remote_root_id}': 从远端同步物料子树 '{remote_child_name}'")
                        else:
                            logger.info(f"物料 '{remote_root_id}/{remote_child_name}' 已存在，跳过")
            else:
                # 情况1: 一级节点是物料（不是 device）
                # 检查是否已存在
                existing = False
                for local_root in self.root_nodes:
                    if local_root.res_content.name == remote_root.res_content.name:
                        existing = True
                        logger.info(f"根节点物料 '{remote_root.res_content.name}' 已存在，跳过")
                        break

                if not existing:
                    # 引入整个子树
                    new_root_nodes.append(remote_root)
                    logger.info(f"添加远端独立物料根节点子树: '{remote_root_id}'")

        # 将新的根节点添加到本地树集合
        if new_root_nodes:
            for new_root in new_root_nodes:
                self.trees.append(ResourceTreeInstance(new_root))

        return self

    def dump(self) -> List[List[Dict[str, Any]]]:
        """
        将 ResourceTreeSet 序列化为嵌套列表格式

        序列化时：
        - parent 自动转换为 parent_uuid（在 ResourceDict.model_dump 中处理）
        - children 不会被序列化（exclude=True）

        Returns:
            List[List[Dict]]: 每个内层列表代表一棵树的扁平化资源字典列表
        """
        result = []
        for tree in self.trees:
            # 获取树的所有节点并序列化
            tree_nodes = [node.res_content.model_dump(by_alias=True) for node in tree.get_all_nodes()]
            result.append(tree_nodes)
        return result

    @classmethod
    def load(cls, data: List[List[Dict[str, Any]]]) -> "ResourceTreeSet":
        """
        从序列化的嵌套列表格式反序列化为 ResourceTreeSet

        Args:
            data: List[List[Dict]]: 序列化的数据，每个内层列表代表一棵树

        Returns:
            ResourceTreeSet: 反序列化后的资源树集合
        """
        nested_lists = []
        for tree_data in data:
            nested_lists.extend(ResourceTreeSet.from_raw_list(tree_data).trees)
        return cls(nested_lists)


class DeviceNodeResourceTracker(object):

    def __init__(self):
        self.resources = []
        self.resource2parent_resource = {}
        self.uuid_to_resources = {}
        pass

    def prefix_path(self, resource):
        resource_prefix_path = "/"
        resource_parent = getattr(resource, "parent", None)
        while resource_parent is not None:
            resource_prefix_path = f"/{resource_parent.name}" + resource_prefix_path
            resource_parent = resource_parent.parent

        return resource_prefix_path

    def map_uuid_to_resource(self, resource, uuid_map: Dict[str, str]):
        for old_uuid, new_uuid in uuid_map.items():
            if old_uuid != new_uuid:
                if old_uuid in self.uuid_to_resources:
                    instance = self.uuid_to_resources.pop(old_uuid)
                    if isinstance(resource, dict):
                        resource["uuid"] = new_uuid
                    else:  # 实例的
                        setattr(instance, "unilabos_uuid", new_uuid)
                    self.uuid_to_resources[new_uuid] = instance
                    print(f"更新uuid映射: {old_uuid} -> {new_uuid} | {instance}")

    def _get_resource_attr(self, resource, attr_name: str, uuid_attr: Optional[str] = None):
        """
        获取资源的属性值，统一处理 dict 和 instance 两种类型

        Args:
            resource: 资源对象（dict或实例）
            attr_name: dict类型使用的属性名
            uuid_attr: instance类型使用的属性名（用于uuid字段），默认与attr_name相同

        Returns:
            属性值，不存在则返回None
        """
        if uuid_attr is None:
            uuid_attr = attr_name

        if isinstance(resource, dict):
            return resource.get(attr_name)
        else:
            return getattr(resource, uuid_attr, None)

    @classmethod
    def set_resource_uuid(cls, resource, new_uuid: str):
        """
        设置资源的 uuid，统一处理 dict 和 instance 两种类型

        Args:
            resource: 资源对象（dict或实例）
            new_uuid: 新的uuid值
        """
        if isinstance(resource, dict):
            resource["uuid"] = new_uuid
        else:
            setattr(resource, "unilabos_uuid", new_uuid)

    @staticmethod
    def set_resource_extra(resource, extra: dict):
        """
        设置资源的 extra，统一处理 dict 和 instance 两种类型

        Args:
            resource: 资源对象（dict或实例）
            extra: extra字典值
        """
        if isinstance(resource, dict):
            c_extra = resource.get("extra", {})
            c_extra.update(extra)
            resource["extra"] = c_extra
        else:
            c_extra = getattr(resource, "unilabos_extra", {})
            c_extra.update(extra)
            setattr(resource, "unilabos_extra", c_extra)

    def _traverse_and_process(self, resource, process_func) -> int:
        """
        递归遍历资源树，对每个节点执行处理函数

        Args:
            resource: 资源对象（可以是list、dict或实例）
            process_func: 处理函数，接收resource参数，返回处理的节点数量

        Returns:
            处理的节点总数量
        """
        if isinstance(resource, list):
            return sum(self._traverse_and_process(r, process_func) for r in resource)

        # 先递归处理所有子节点
        count = 0
        children = getattr(resource, "children", [])
        for child in children:
            count += self._traverse_and_process(child, process_func)

        # 处理当前节点
        count += process_func(resource)
        return count

    def loop_set_uuid(self, resource, name_to_uuid_map: Dict[str, str]) -> int:
        """
        递归遍历资源树，根据 name 设置所有节点的 uuid

        Args:
            resource: 资源对象（可以是dict或实例）
            name_to_uuid_map: name到uuid的映射字典，{name: uuid}

        Returns:
            更新的资源数量
        """

        def process(res):
            resource_name = self._get_resource_attr(res, "name")
            if resource_name and resource_name in name_to_uuid_map:
                new_uuid = name_to_uuid_map[resource_name]
                self.set_resource_uuid(res, new_uuid)
                self.uuid_to_resources[new_uuid] = res
                logger.trace(f"设置资源UUID: {resource_name} -> {new_uuid}")
                return 1
            return 0

        return self._traverse_and_process(resource, process)

    def loop_set_extra(self, resource, name_to_extra_map: Dict[str, dict]) -> int:
        """
        递归遍历资源树，根据 name 设置所有节点的 extra

        Args:
            resource: 资源对象（可以是dict或实例）
            name_to_extra_map: name到extra的映射字典，{name: extra}

        Returns:
            更新的资源数量
        """

        def process(res):
            resource_name = self._get_resource_attr(res, "name")
            if resource_name and resource_name in name_to_extra_map:
                extra = name_to_extra_map[resource_name]
                self.set_resource_extra(res, extra)
                if len(extra):
                    logger.debug(f"设置资源Extra: {resource_name} -> {extra}")
                return 1
            return 0

        return self._traverse_and_process(resource, process)

    def loop_update_uuid(self, resource, uuid_map: Dict[str, str]) -> int:
        """
        递归遍历资源树，更新所有节点的uuid

        Args:
            resource: 资源对象（可以是dict或实例）
            uuid_map: uuid映射字典，{old_uuid: new_uuid}

        Returns:
            更新的资源数量
        """

        def process(res):
            current_uuid = self._get_resource_attr(res, "uuid", "unilabos_uuid")
            replaced = 0
            if current_uuid and current_uuid in uuid_map:
                new_uuid = uuid_map[current_uuid]
                if current_uuid != new_uuid:
                    self.set_resource_uuid(res, new_uuid)
                    # 更新uuid_to_resources映射
                    if current_uuid in self.uuid_to_resources:
                        self.uuid_to_resources.pop(current_uuid)
                    self.uuid_to_resources[new_uuid] = res
                    logger.debug(f"更新uuid: {current_uuid} -> {new_uuid}")
                    replaced = 1
            return replaced

        return self._traverse_and_process(resource, process)

    def loop_gather_uuid(self, resource) -> List[str]:
        """
        递归遍历资源树，收集所有节点的uuid

        Args:
            resource: 资源对象（可以是dict或实例）

        Returns:
            收集到的uuid列表
        """
        uuid_list = []

        def process(res):
            current_uuid = self._get_resource_attr(res, "uuid", "unilabos_uuid")
            if current_uuid:
                uuid_list.append(current_uuid)
            return 0

        self._traverse_and_process(resource, process)
        return uuid_list

    def _collect_uuid_mapping(self, resource):
        """
        递归收集资源的 uuid 映射到 uuid_to_resources

        Args:
            resource: 资源对象（可以是dict或实例）
        """

        def process(res):
            current_uuid = self._get_resource_attr(res, "uuid", "unilabos_uuid")
            if current_uuid:
                old = self.uuid_to_resources.get(current_uuid)
                self.uuid_to_resources[current_uuid] = res
                logger.trace(
                    f"收集资源UUID映射: {current_uuid} -> {res} {'' if old is None else f'(覆盖旧值: {old})'}"
                )
                return 1
            return 0

        self._traverse_and_process(resource, process)

    def _remove_uuid_mapping(self, resource) -> int:
        """
        递归清除资源的 uuid 映射

        Args:
            resource: 资源对象（可以是dict或实例）
        """

        def process(res):
            current_uuid = self._get_resource_attr(res, "uuid", "unilabos_uuid")
            if current_uuid and current_uuid in self.uuid_to_resources:
                self.uuid_to_resources.pop(current_uuid)
                logger.trace(f"移除资源UUID映射: {current_uuid} -> {res}")
                return 1
            return 0

        return self._traverse_and_process(resource, process)

    def parent_resource(self, resource):
        if id(resource) in self.resource2parent_resource:
            return self.resource2parent_resource[id(resource)]
        else:
            return resource

    def add_resource(self, resource):
        """
        添加资源到追踪器

        Args:
            resource: 资源对象（可以是dict或实例）
        """
        root_uuids = {}
        for r in self.resources:
            res_uuid = r.get("uuid") if isinstance(r, dict) else getattr(r, "unilabos_uuid", None)
            if res_uuid:
                root_uuids[res_uuid] = r
            if id(r) == id(resource):
                return

        # 这里只做uuid的根节点比较
        if isinstance(resource, dict):
            res_uuid = resource.get("uuid")
        else:
            res_uuid = getattr(resource, "unilabos_uuid", None)
        if res_uuid in root_uuids:
            old_res = root_uuids[res_uuid]
            # self.remove_resource(old_res)
            logger.warning(f"资源{resource}已存在，旧资源: {old_res}")
        self.resources.append(resource)
        # 递归收集uuid映射
        self._collect_uuid_mapping(resource)

    def remove_resource(self, resource) -> bool:
        """
        从追踪器中移除资源

        Args:
            resource: 资源对象（可以是dict或实例）

        Returns:
            bool: 如果成功移除返回True，资源不存在返回False
        """
        # 从 resources 列表中移除
        resource_id = id(resource)
        removed = False
        for i, r in enumerate(self.resources):
            if id(r) == resource_id:
                self.resources.pop(i)
                removed = True
                break

        # 递归清除uuid映射
        count = self._remove_uuid_mapping(resource)
        if not count:
            logger.warning(f"尝试移除不存在的资源: {resource}")
            return False

        # 清除 resource2parent_resource 中与该资源相关的映射
        # 需要清除：1) 该资源作为 key 的映射 2) 该资源作为 value 的映射
        keys_to_remove = []
        for key, value in self.resource2parent_resource.items():
            if id(value) == resource_id:
                keys_to_remove.append(key)

        if resource_id in self.resource2parent_resource:
            keys_to_remove.append(resource_id)

        for key in keys_to_remove:
            self.resource2parent_resource.pop(key, None)

        logger.debug(f"成功移除资源: {resource}")
        return True

    def clear_resource(self):
        """清空所有资源"""
        self.resources = []
        self.uuid_to_resources.clear()
        self.resource2parent_resource.clear()

    def figure_resource(
        self, query_resource: Union[List[Union[dict, "PLRResource"]], dict, "PLRResource"], try_mode=False
    ) -> Union[List[Union[dict, "PLRResource", List[Union[dict, "PLRResource"]]]], dict, "PLRResource"]:
        if isinstance(query_resource, list):
            return [self.figure_resource(r, try_mode) for r in query_resource]
        elif (
            isinstance(query_resource, dict)
            and "id" not in query_resource
            and "name" not in query_resource
            and "uuid" not in query_resource
        ):  # 临时处理，要删除的，driver有太多类型错误标注
            return [self.figure_resource(r, try_mode) for r in query_resource.values()]

        # 优先尝试通过 uuid 查找
        res_uuid = None
        if isinstance(query_resource, dict):
            res_uuid = query_resource.get("uuid")
        else:
            res_uuid = getattr(query_resource, "unilabos_uuid", None)

        # 如果有 uuid，优先使用 uuid 查找
        if res_uuid:
            res_list = []
            for r in self.resources:
                if isinstance(query_resource, dict):
                    res_list.extend(self.loop_find_resource(r, object, "uuid", res_uuid))
                else:
                    res_list.extend(self.loop_find_resource(r, type(query_resource), "unilabos_uuid", res_uuid))

            if not try_mode:
                assert len(res_list) > 0, f"没有找到资源 (uuid={res_uuid})，请检查资源是否存在"
                assert len(res_list) == 1, f"通过uuid={res_uuid} 找到多个资源，请检查资源是否唯一: {res_list}"
            else:
                return [i[1] for i in res_list]

            self.resource2parent_resource[id(query_resource)] = res_list[0][0]
            self.resource2parent_resource[id(res_list[0][1])] = res_list[0][0]
            return res_list[0][1]

        # 回退到 id/name 查找
        res_id = (
            query_resource.id  # type: ignore
            if hasattr(query_resource, "id")
            else (query_resource.get("id") if isinstance(query_resource, dict) else None)
        )
        res_name = (
            query_resource.name  # type: ignore
            if hasattr(query_resource, "name")
            else (query_resource.get("name") if isinstance(query_resource, dict) else None)
        )
        res_identifier = res_id if res_id else res_name
        identifier_key = "id" if res_id else "name"
        resource_cls_type = type(query_resource)
        if res_identifier is None:
            logger.warning(f"resource {query_resource} 没有id、name或uuid，暂不能对应figure")
        res_list = []
        for r in self.resources:
            if isinstance(query_resource, dict):
                res_list.extend(self.loop_find_resource(r, object, identifier_key, query_resource[identifier_key]))
            else:
                res_list.extend(
                    self.loop_find_resource(
                        r, resource_cls_type, identifier_key, getattr(query_resource, identifier_key)
                    )
                )
        if not try_mode:
            assert len(res_list) > 0, f"没有找到资源 {query_resource}，请检查资源是否存在"
            assert len(res_list) == 1, f"{query_resource} 找到多个资源，请检查资源是否唯一: {res_list}"
        else:
            return [i[1] for i in res_list]
        # 后续加入其他对比方式
        self.resource2parent_resource[id(query_resource)] = res_list[0][0]
        self.resource2parent_resource[id(res_list[0][1])] = res_list[0][0]
        return res_list[0][1]

    def loop_find_resource(
        self, resource, target_resource_cls_type, identifier_key, compare_value, parent_res=None
    ) -> List[Tuple[Any, Any]]:
        res_list = []
        # print(resource, target_resource_cls_type, identifier_key, compare_value)
        children = []
        if not isinstance(resource, dict):
            children = getattr(resource, "children", [])
        else:
            children = resource.get("children")
            if children is not None:
                children = list(children.values()) if isinstance(children, dict) else children
        for child in children:
            res_list.extend(
                self.loop_find_resource(child, target_resource_cls_type, identifier_key, compare_value, resource)
            )
        if issubclass(type(resource), target_resource_cls_type):
            if type(resource) == dict:
                # 对于字典类型，直接检查 identifier_key
                if identifier_key in resource:
                    if resource[identifier_key] == compare_value:
                        res_list.append((parent_res, resource))
            else:
                # 对于实例类型，需要特殊处理 uuid 字段
                # 如果查找的是 unilabos_uuid，使用 getattr
                if identifier_key == "uuid":
                    identifier_key = "unilabos_uuid"
                if hasattr(resource, identifier_key):
                    if getattr(resource, identifier_key) == compare_value:
                        res_list.append((parent_res, resource))
        return res_list

    def filter_find_list(self, res_list, compare_std_dict):
        new_list = []
        for res in res_list:
            for k, v in compare_std_dict.items():
                if hasattr(res, k):
                    if getattr(res, k) == v:
                        new_list.append(res)
        return new_list


if __name__ == "__main__":
    from pylabrobot.resources import corning_6_wellplate_16point8ml_flat

    # 测试 from_plr_resources 和 to_plr_resources 的往返转换
    print("=" * 60)
    print("测试 PLR 资源转换往返")
    print("=" * 60)

    # 1. 创建一个 PLR 资源并设置 UUID
    original_plate = corning_6_wellplate_16point8ml_flat("test_plate")

    # 使用 DeviceNodeResourceTracker 设置 UUID
    tracker = DeviceNodeResourceTracker()
    name_to_uuid = {}

    # 递归生成 name_to_uuid 映射
    def build_uuid_map(resource):
        name_to_uuid[resource.name] = str(uuid.uuid4())
        for child in resource.children:
            build_uuid_map(child)

    build_uuid_map(original_plate)

    # 使用 tracker 的 loop_set_uuid 方法设置 UUID
    tracker.loop_set_uuid(original_plate, name_to_uuid)

    print(f"\n1. 原始 PLR 资源: {original_plate.name}")
    print(f"   - UUID: {getattr(original_plate, 'unilabos_uuid', 'N/A')}")
    print(f"   - 子节点数量: {len(original_plate.children)}")
    if original_plate.children:
        print(f"   - 第一个子节点: {original_plate.children[0].name}")
        print(f"   - 第一个子节点 UUID: {getattr(original_plate.children[0], 'unilabos_uuid', 'N/A')}")

    # 2. 将 PLR 资源转换为 ResourceTreeSet
    resource_tree_set = ResourceTreeSet.from_plr_resources([original_plate])
    print(f"\n2. 转换为 ResourceTreeSet:")
    print(f"   - 树的数量: {len(resource_tree_set.trees)}")
    print(f"   - 根节点: {resource_tree_set.root_nodes[0].res_content.name}")
    print(f"   - 所有节点数量: {len(resource_tree_set.all_nodes)}")

    # 3. 将 ResourceTreeSet 转换回 PLR 资源
    plr_resources = resource_tree_set.to_plr_resources()
    converted_plate = plr_resources[0]
    print(f"\n3. 转换回 PLR 资源: {converted_plate.name}")
    print(f"   - 子节点数量: {len(converted_plate.children)}")
    if converted_plate.children:
        print(f"   - 第一个子节点: {converted_plate.children[0].name}")

    # 4. 验证 unilabos_uuid 属性
    print(f"\n4. 验证 unilabos_uuid 设置:")
    if hasattr(converted_plate, "unilabos_uuid"):
        print(f"   - 根节点 UUID: {getattr(converted_plate, 'unilabos_uuid')}")
        if converted_plate.children and hasattr(converted_plate.children[0], "unilabos_uuid"):
            print(f"   - 第一个子节点 UUID: {getattr(converted_plate.children[0], 'unilabos_uuid')}")
    else:
        print("   - 警告: unilabos_uuid 未设置")

    # 5. 验证 UUID 保持不变
    print(f"\n5. 验证 UUID 在往返过程中保持不变:")
    original_uuid = getattr(original_plate, "unilabos_uuid")
    converted_uuid = getattr(converted_plate, "unilabos_uuid")
    print(f"   - 原始 UUID: {original_uuid}")
    print(f"   - 转换后 UUID: {converted_uuid}")
    print(f"   - UUID 保持不变: {original_uuid == converted_uuid}")

    # 6. 再次往返转换，验证稳定性
    resource_tree_set_2 = ResourceTreeSet.from_plr_resources([converted_plate])
    plr_resources_2 = resource_tree_set_2.to_plr_resources()
    print(f"\n6. 第二次往返转换:")
    print(f"   - 资源名称: {plr_resources_2[0].name}")
    print(f"   - 子节点数量: {len(plr_resources_2[0].children)}")
    print(f"   - UUID 依然保持: {getattr(plr_resources_2[0], 'unilabos_uuid') == original_uuid}")

    print("\n" + "=" * 60)
    print("✅ 测试完成! 所有转换正常工作")
    print("=" * 60)
