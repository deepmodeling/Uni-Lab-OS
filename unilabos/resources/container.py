import json
from typing import Dict, Any

from pylabrobot.resources import Container
from unilabos_msgs.msg import Resource

from unilabos.ros.msgs.message_converter import convert_from_ros_msg


class RegularContainer(Container):
    def __init__(self, *args, **kwargs):
        pose = kwargs.pop("pose", None)
        if "size_x" not in kwargs:
            kwargs["size_x"] = 0
        if "size_y" not in kwargs:
            kwargs["size_y"] = 0
        if "size_z" not in kwargs:
            kwargs["size_z"] = 0
        self.kwargs = kwargs
        self.state = {}
        super().__init__(*args, category="container", **kwargs)

    def load_state(self, state: Dict[str, Any]):
        self.state = state
#
# class RegularContainer(object):
#     # 第一个参数必须是id传入
#     # noinspection PyShadowingBuiltins
#     def __init__(self, id: str):
#         self.id = id
#         self.ulr_resource = Resource()
#         self._data = None
#
#     @property
#     def ulr_resource_data(self):
#         if self._data is None:
#             self._data = json.loads(self.ulr_resource.data) if self.ulr_resource.data else {}
#         return self._data
#
#     @ulr_resource_data.setter
#     def ulr_resource_data(self, value: dict):
#         self._data = value
#         self.ulr_resource.data = json.dumps(self._data)
#
#     @property
#     def liquid_type(self):
#         return self.ulr_resource_data.get("liquid_type", None)
#
#     @liquid_type.setter
#     def liquid_type(self, value: str):
#         if value is not None:
#             self.ulr_resource_data["liquid_type"] = value
#         else:
#             self.ulr_resource_data.pop("liquid_type", None)
#
#     @property
#     def liquid_volume(self):
#         return self.ulr_resource_data.get("liquid_volume", None)
#
#     @liquid_volume.setter
#     def liquid_volume(self, value: float):
#         if value is not None:
#             self.ulr_resource_data["liquid_volume"] = value
#         else:
#             self.ulr_resource_data.pop("liquid_volume", None)
#
#     def get_ulr_resource(self) -> Resource:
#         """
#         获取UlrResource对象
#         :return: UlrResource对象
#         """
#         self.ulr_resource_data = self.ulr_resource_data  # 确保数据被更新
#         return self.ulr_resource
#
#     def get_ulr_resource_as_dict(self) -> Resource:
#         """
#         获取UlrResource对象
#         :return: UlrResource对象
#         """
#         to_dict = convert_from_ros_msg(self.get_ulr_resource())
#         to_dict["type"] = "container"
#         return to_dict
#
#     def __str__(self):
#         return f"{self.id}"