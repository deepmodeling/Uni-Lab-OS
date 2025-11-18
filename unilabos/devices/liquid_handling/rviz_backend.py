
import json
import threading
from typing import List, Optional, Union

from pylabrobot.liquid_handling.backends.backend import (
  LiquidHandlerBackend,
)
from pylabrobot.liquid_handling.standard import (
  Drop,
  DropTipRack,
  MultiHeadAspirationContainer,
  MultiHeadAspirationPlate,
  MultiHeadDispenseContainer,
  MultiHeadDispensePlate,
  Pickup,
  PickupTipRack,
  ResourceDrop,
  ResourceMove,
  ResourcePickup,
  SingleChannelAspiration,
  SingleChannelDispense,
)
from pylabrobot.resources import Resource, Tip

import rclpy
from rclpy.node import Node
from sensor_msgs.msg import JointState
import time
from rclpy.action import ActionClient
from unilabos_msgs.action import SendCmd
import re

from unilabos.devices.ros_dev.liquid_handler_joint_publisher_node import LiquidHandlerJointPublisher


class UniLiquidHandlerRvizBackend(LiquidHandlerBackend):
  """Chatter box backend for device-free testing. Prints out all operations."""

  _pip_length = 5
  _vol_length = 8
  _resource_length = 20
  _offset_length = 16
  _flow_rate_length = 10
  _blowout_length = 10
  _lld_z_length = 10
  _kwargs_length = 15
  _tip_type_length = 12
  _max_volume_length = 16
  _fitting_depth_length = 20
  _tip_length_length = 16
  _filter_length = 10

  def __init__(self, num_channels: int = 8 , tip_length: float = 0 , total_height: float = 310, **kwargs):
    """Initialize a chatter box backend."""
    super().__init__()
    self._num_channels = num_channels
    self.tip_length = tip_length
    self.total_height = total_height
    self.joint_config = kwargs.get("joint_config", None)
    self.lh_device_id = kwargs.get("lh_device_id", "lh_joint_publisher")
    if not rclpy.ok():
        rclpy.init()
    self.joint_state_publisher = None
    self.executor = None
    self.executor_thread = None

  async def setup(self):
    self.joint_state_publisher = LiquidHandlerJointPublisher(
                                joint_config=self.joint_config,
                                lh_device_id=self.lh_device_id,
                                simulate_rviz=True)

    # 启动ROS executor
    self.executor = rclpy.executors.MultiThreadedExecutor()
    self.executor.add_node(self.joint_state_publisher)
    self.executor_thread = threading.Thread(target=self.executor.spin, daemon=True)
    self.executor_thread.start()

    await super().setup()

    print("Setting up the liquid handler.")

  async def stop(self):
    # 停止ROS executor
    if self.executor and self.joint_state_publisher:
        self.executor.remove_node(self.joint_state_publisher)
    if self.executor_thread and self.executor_thread.is_alive():
        self.executor.shutdown()
    print("Stopping the liquid handler.")

  def serialize(self) -> dict:
    return {**super().serialize(), "num_channels": self.num_channels}

  @property
  def num_channels(self) -> int:
    return self._num_channels

  async def assigned_resource_callback(self, resource: Resource):
    print(f"Resource {resource.name} was assigned to the liquid handler.")

  async def unassigned_resource_callback(self, name: str):
    print(f"Resource {name} was unassigned from the liquid handler.")

  async def pick_up_tips(self, ops: List[Pickup], use_channels: List[int], **backend_kwargs):
    print("Picking up tips:")
    # print(ops.tip)
    header = (
      f"{'pip#':<{UniLiquidHandlerRvizBackend._pip_length}} "
      f"{'resource':<{UniLiquidHandlerRvizBackend._resource_length}} "
      f"{'offset':<{UniLiquidHandlerRvizBackend._offset_length}} "
      f"{'tip type':<{UniLiquidHandlerRvizBackend._tip_type_length}} "
      f"{'max volume (µL)':<{UniLiquidHandlerRvizBackend._max_volume_length}} "
      f"{'fitting depth (mm)':<{UniLiquidHandlerRvizBackend._fitting_depth_length}} "
      f"{'tip length (mm)':<{UniLiquidHandlerRvizBackend._tip_length_length}} "
      # f"{'pickup method':<{ChatterboxBackend._pickup_method_length}} "
      f"{'filter':<{UniLiquidHandlerRvizBackend._filter_length}}"
    )
    # print(header)

    for op, channel in zip(ops, use_channels):
      offset = f"{round(op.offset.x, 1)},{round(op.offset.y, 1)},{round(op.offset.z, 1)}"
      row = (
        f"  p{channel}: "
        f"{op.resource.name[-30:]:<{UniLiquidHandlerRvizBackend._resource_length}} "
        f"{offset:<{UniLiquidHandlerRvizBackend._offset_length}} "
        f"{op.tip.__class__.__name__:<{UniLiquidHandlerRvizBackend._tip_type_length}} "
        f"{op.tip.maximal_volume:<{UniLiquidHandlerRvizBackend._max_volume_length}} "
        f"{op.tip.fitting_depth:<{UniLiquidHandlerRvizBackend._fitting_depth_length}} "
        f"{op.tip.total_tip_length:<{UniLiquidHandlerRvizBackend._tip_length_length}} "
        # f"{str(op.tip.pickup_method)[-20:]:<{ChatterboxBackend._pickup_method_length}} "
        f"{'Yes' if op.tip.has_filter else 'No':<{UniLiquidHandlerRvizBackend._filter_length}}"
      )
      # print(row)
      # print(op.resource.get_absolute_location())
    
    self.tip_length = ops[0].tip.total_tip_length
    coordinate = ops[0].resource.get_absolute_location(x="c",y="c")
    offset_xyz = ops[0].offset
    x = coordinate.x + offset_xyz.x
    y = coordinate.y + offset_xyz.y
    z = self.total_height - (coordinate.z + self.tip_length) + offset_xyz.z
    # print("moving")
    self.joint_state_publisher.move_joints(ops[0].resource.name, x, y, z, "pick",channels=use_channels)
    #   goback()




  async def drop_tips(self, ops: List[Drop], use_channels: List[int], **backend_kwargs):
    print("Dropping tips:")
    header = (
      f"{'pip#':<{UniLiquidHandlerRvizBackend._pip_length}} "
      f"{'resource':<{UniLiquidHandlerRvizBackend._resource_length}} "
      f"{'offset':<{UniLiquidHandlerRvizBackend._offset_length}} "
      f"{'tip type':<{UniLiquidHandlerRvizBackend._tip_type_length}} "
      f"{'max volume (µL)':<{UniLiquidHandlerRvizBackend._max_volume_length}} "
      f"{'fitting depth (mm)':<{UniLiquidHandlerRvizBackend._fitting_depth_length}} "
      f"{'tip length (mm)':<{UniLiquidHandlerRvizBackend._tip_length_length}} "
      # f"{'pickup method':<{ChatterboxBackend._pickup_method_length}} "
      f"{'filter':<{UniLiquidHandlerRvizBackend._filter_length}}"
    )
    # print(header)

    for op, channel in zip(ops, use_channels):
      offset = f"{round(op.offset.x, 1)},{round(op.offset.y, 1)},{round(op.offset.z, 1)}"
      row = (
        f"  p{channel}: "
        f"{op.resource.name[-30:]:<{UniLiquidHandlerRvizBackend._resource_length}} "
        f"{offset:<{UniLiquidHandlerRvizBackend._offset_length}} "
        f"{op.tip.__class__.__name__:<{UniLiquidHandlerRvizBackend._tip_type_length}} "
        f"{op.tip.maximal_volume:<{UniLiquidHandlerRvizBackend._max_volume_length}} "
        f"{op.tip.fitting_depth:<{UniLiquidHandlerRvizBackend._fitting_depth_length}} "
        f"{op.tip.total_tip_length:<{UniLiquidHandlerRvizBackend._tip_length_length}} "
        # f"{str(op.tip.pickup_method)[-20:]:<{ChatterboxBackend._pickup_method_length}} "
        f"{'Yes' if op.tip.has_filter else 'No':<{UniLiquidHandlerRvizBackend._filter_length}}"
      )
      # print(row)

    coordinate = ops[0].resource.get_absolute_location(x="c",y="c")
    offset_xyz = ops[0].offset
    x = coordinate.x + offset_xyz.x
    y = coordinate.y + offset_xyz.y
    z = self.total_height - (coordinate.z + self.tip_length) + offset_xyz.z
    # print(x, y, z)
    # print("moving")
    self.joint_state_publisher.move_joints(ops[0].resource.name, x, y, z, "drop_trash",channels=use_channels)
    #   goback()

  async def aspirate(
    self,
    ops: List[SingleChannelAspiration],
    use_channels: List[int],
    **backend_kwargs,
  ):
    print("Aspirating:")
    header = (
      f"{'pip#':<{UniLiquidHandlerRvizBackend._pip_length}} "
      f"{'vol(ul)':<{UniLiquidHandlerRvizBackend._vol_length}} "
      f"{'resource':<{UniLiquidHandlerRvizBackend._resource_length}} "
      f"{'offset':<{UniLiquidHandlerRvizBackend._offset_length}} "
      f"{'flow rate':<{UniLiquidHandlerRvizBackend._flow_rate_length}} "
      f"{'blowout':<{UniLiquidHandlerRvizBackend._blowout_length}} "
      f"{'lld_z':<{UniLiquidHandlerRvizBackend._lld_z_length}}  "
      # f"{'liquids':<20}" # TODO: add liquids
    )
    for key in backend_kwargs:
      header += f"{key:<{UniLiquidHandlerRvizBackend._kwargs_length}} "[-16:]
    # print(header)

    for o, p in zip(ops, use_channels):
      offset = f"{round(o.offset.x, 1)},{round(o.offset.y, 1)},{round(o.offset.z, 1)}"
      row = (
        f"  p{p}: "
        f"{o.volume:<{UniLiquidHandlerRvizBackend._vol_length}} "
        f"{o.resource.name[-20:]:<{UniLiquidHandlerRvizBackend._resource_length}} "
        f"{offset:<{UniLiquidHandlerRvizBackend._offset_length}} "
        f"{str(o.flow_rate):<{UniLiquidHandlerRvizBackend._flow_rate_length}} "
        f"{str(o.blow_out_air_volume):<{UniLiquidHandlerRvizBackend._blowout_length}} "
        f"{str(o.liquid_height):<{UniLiquidHandlerRvizBackend._lld_z_length}} "
        # f"{o.liquids if o.liquids is not None else 'none'}"
      )
      for key, value in backend_kwargs.items():
        if isinstance(value, list) and all(isinstance(v, bool) for v in value):
          value = "".join("T" if v else "F" for v in value)
        if isinstance(value, list):
          value = "".join(map(str, value))
        row += f" {value:<15}"
      # print(row)
    coordinate = ops[0].resource.get_absolute_location(x="c",y="c")
    offset_xyz = ops[0].offset
    x = coordinate.x + offset_xyz.x
    y = coordinate.y + offset_xyz.y
    z = self.total_height - (coordinate.z + self.tip_length) + offset_xyz.z
    # print(x, y, z)
    # print("moving")
    self.joint_state_publisher.move_joints(ops[0].resource.name, x, y, z, "",channels=use_channels)


  async def dispense(
    self,
    ops: List[SingleChannelDispense],
    use_channels: List[int],
    **backend_kwargs,
  ):
    # print("Dispensing:")
    header = (
      f"{'pip#':<{UniLiquidHandlerRvizBackend._pip_length}} "
      f"{'vol(ul)':<{UniLiquidHandlerRvizBackend._vol_length}} "
      f"{'resource':<{UniLiquidHandlerRvizBackend._resource_length}} "
      f"{'offset':<{UniLiquidHandlerRvizBackend._offset_length}} "
      f"{'flow rate':<{UniLiquidHandlerRvizBackend._flow_rate_length}} "
      f"{'blowout':<{UniLiquidHandlerRvizBackend._blowout_length}} "
      f"{'lld_z':<{UniLiquidHandlerRvizBackend._lld_z_length}}  "
      # f"{'liquids':<20}" # TODO: add liquids
    )
    for key in backend_kwargs:
      header += f"{key:<{UniLiquidHandlerRvizBackend._kwargs_length}} "[-16:]
    # print(header)

    for o, p in zip(ops, use_channels):
      offset = f"{round(o.offset.x, 1)},{round(o.offset.y, 1)},{round(o.offset.z, 1)}"
      row = (
        f"  p{p}: "
        f"{o.volume:<{UniLiquidHandlerRvizBackend._vol_length}} "
        f"{o.resource.name[-20:]:<{UniLiquidHandlerRvizBackend._resource_length}} "
        f"{offset:<{UniLiquidHandlerRvizBackend._offset_length}} "
        f"{str(o.flow_rate):<{UniLiquidHandlerRvizBackend._flow_rate_length}} "
        f"{str(o.blow_out_air_volume):<{UniLiquidHandlerRvizBackend._blowout_length}} "
        f"{str(o.liquid_height):<{UniLiquidHandlerRvizBackend._lld_z_length}} "
        # f"{o.liquids if o.liquids is not None else 'none'}"
      )
      for key, value in backend_kwargs.items():
        if isinstance(value, list) and all(isinstance(v, bool) for v in value):
          value = "".join("T" if v else "F" for v in value)
        if isinstance(value, list):
          value = "".join(map(str, value))
        row += f" {value:<{UniLiquidHandlerRvizBackend._kwargs_length}}"
      # print(row)
    coordinate = ops[0].resource.get_absolute_location(x="c",y="c")
    offset_xyz = ops[0].offset
    x = coordinate.x + offset_xyz.x
    y = coordinate.y + offset_xyz.y
    z = self.total_height - (coordinate.z + self.tip_length) + offset_xyz.z

    self.joint_state_publisher.move_joints(ops[0].resource.name, x, y, z, "",channels=use_channels)

  async def pick_up_tips96(self, pickup: PickupTipRack, **backend_kwargs):
    print(f"Picking up tips from {pickup.resource.name}.")

  async def drop_tips96(self, drop: DropTipRack, **backend_kwargs):
    print(f"Dropping tips to {drop.resource.name}.")

  async def aspirate96(
    self, aspiration: Union[MultiHeadAspirationPlate, MultiHeadAspirationContainer]
  ):
    if isinstance(aspiration, MultiHeadAspirationPlate):
      resource = aspiration.wells[0].parent
    else:
      resource = aspiration.container
    print(f"Aspirating {aspiration.volume} from {resource}.")

  async def dispense96(self, dispense: Union[MultiHeadDispensePlate, MultiHeadDispenseContainer]):
    if isinstance(dispense, MultiHeadDispensePlate):
      resource = dispense.wells[0].parent
    else:
      resource = dispense.container
    print(f"Dispensing {dispense.volume} to {resource}.")

  async def pick_up_resource(self, pickup: ResourcePickup):
    print(f"Picking up resource: {pickup}")

  async def move_picked_up_resource(self, move: ResourceMove):
    print(f"Moving picked up resource: {move}")

  async def drop_resource(self, drop: ResourceDrop):
    print(f"Dropping resource: {drop}")

  def can_pick_up_tip(self, channel_idx: int, tip: Tip) -> bool:
    return True
    
