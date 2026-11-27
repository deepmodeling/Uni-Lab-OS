"""
自动化液体处理工作站物料类定义 - 简化版
Automated Liquid Handling Station Resource Classes - Simplified Version
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple, TypeVar, Union
from uuid import UUID

import pylabrobot
from pylabrobot.resources import Resource as ResourcePLR
from pylabrobot.resources import ResourceHolder, Well
from pylabrobot.resources.coordinate import Coordinate

LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


class Bottle(Well):
    """瓶子类 - 简化版，不追踪瓶盖。

    serialize / deserialize 完全交给父类：
    - barcode（须为 PLR ``Barcode`` 对象）由父类管理：``Resource.__init__`` 默认置 None，
      反序列化时由 ``Resource.deserialize`` 经 ``Barcode.deserialize`` 还原；本类不自行初始化/赋值。
    - diameter/height 与 size_x/size_z 等价、缺省互相回填，父类序列化的 size_* 已足够无损重建。
    """

    def __init__(
        self,
        name: str,
        diameter: Optional[float] = None,
        height: Optional[float] = None,
        max_volume: Optional[float] = None,
        size_x: float = 0.0,
        size_y: float = 0.0,
        size_z: float = 0.0,
        category: str = "container",
        model: Optional[str] = None,
        **kwargs,
    ):
        # 反序列化时父类只回传 size_*（不含 diameter/height）；二者等价，缺一即互相回填
        diameter = diameter if diameter is not None else size_x
        height = height if height is not None else size_z
        super().__init__(
            name=name,
            size_x=diameter,
            size_y=diameter,
            size_z=height,
            max_volume=max_volume,
            category=category,
            model=model,
            bottom_type="flat",
            cross_section_type="circle"
        )
        self.diameter = diameter
        self.height = height

T = TypeVar("T", bound=ResourceHolder)

S = TypeVar("S", bound=ResourceHolder)


def _canonical_site_uuid(value: object) -> str:
  """规范化库存权威分配的稳定库位（Site）UUID。

  参数：``value`` 是 ``sites[]`` 描述中的候选 UUID。返回：小写连字符形式的
  UUID。异常：空值或非法 UUID 抛出 ``ValueError``，防止设备动作使用模糊身份。
  """

  try:
    return str(UUID(str(value).strip()))
  except (AttributeError, TypeError, ValueError) as error:
    raise ValueError(f"库位 UUID 非法: {value!r}") from error


def _site_identity_maps(
  sites: Sequence[dict],
) -> tuple[dict[str, str], dict[str, str]]:
  """从序列化库位描述建立双向稳定身份索引。

  参数：``sites`` 是离散载架的 ``sites[]`` 一等字段。返回：局部名称到 UUID、
  UUID 到局部名称的两个独立字典。异常：名称为空、名称重复、UUID 畸形或重复
  时抛出 ``ValueError``；未分配 UUID 的库位仍可用于本地 PLR 操作。
  """

  uuid_by_name: dict[str, str] = {}
  name_by_uuid: dict[str, str] = {}
  seen_names: set[str] = set()
  for site in sites:
    if not isinstance(site, dict):
      raise ValueError("库位描述必须是对象")
    # ``site_name`` 是设备驱动使用的局部库位名称，必须在同一载架内唯一。
    site_name = str(site.get("label") or "").strip()
    if not site_name:
      raise ValueError("库位名称不能为空")
    if site_name in seen_names:
      raise ValueError(f"库位名称重复: {site_name}")
    seen_names.add(site_name)
    site_uuid_value = site.get("uuid")
    if site_uuid_value in (None, ""):
      continue
    # ``site_uuid`` 是库存权威身份；规范化后仍必须在同一载架内唯一。
    site_uuid = _canonical_site_uuid(site_uuid_value)
    if site_uuid in name_by_uuid:
      raise ValueError(f"库位 UUID 重复: {site_uuid}")
    uuid_by_name[site_name] = site_uuid
    name_by_uuid[site_uuid] = site_name
  return uuid_by_name, name_by_uuid


class ItemizedCarrier(ResourcePLR):
  """Base class for all carriers."""

  def __init__(
    self,
    name: str,
    size_x: float,
    size_y: float,
    size_z: float,
    num_items_x: int = 0,
    num_items_y: int = 0,
    num_items_z: int = 0,
    layout: str = "x-y",
    sites: Optional[Dict[Union[int, str], Optional[ResourcePLR]]] = None,
    category: Optional[str] = "carrier",
    model: Optional[str] = None,
    invisible_slots: Optional[str] = None,
  ):
    super().__init__(
      name=name,
      size_x=size_x,
      size_y=size_y,
      size_z=size_z,
      category=category,
      model=model,
    )
    self.num_items = len(sites)
    self.num_items_x, self.num_items_y, self.num_items_z = num_items_x, num_items_y, num_items_z
    self.invisible_slots = [] if invisible_slots is None else invisible_slots
    self.layout = "z-y" if self.num_items_z > 1 and self.num_items_x == 1 else "x-z" if self.num_items_z > 1 and self.num_items_y == 1 else "x-y"

    if isinstance(sites, dict):
      sites = sites or {}
      self.sites: List[Optional[ResourcePLR]] = list(sites.values())
      self._ordering = sites
      self.child_locations: Dict[str, Coordinate] = {}
      self.child_size: Dict[str, dict] = {}
      for spot, resource in sites.items():
        if resource is not None and getattr(resource, "location", None) is None:
          raise ValueError(f"resource {resource} has no location")
        if resource is not None:
          self.child_locations[spot] = resource.location
          self.child_size[spot] = {"width": resource._size_x, "height": resource._size_y, "depth": resource._size_z}
        else:
          self.child_locations[spot] = Coordinate.zero()
          self.child_size[spot] = {"width": 0, "height": 0, "depth": 0}
    elif isinstance(sites, list):
      # deserialize时走这里；还需要根据 self.sites 索引children
      self.site_uuid_by_name, self.site_name_by_uuid = _site_identity_maps(sites)
      self.child_locations = {site["label"]: Coordinate(**site["position"]) for site in sites}
      self.child_size = {site["label"]: site["size"] for site in sites}
      self.sites = [site["occupied_by"] for site in sites]
      self._ordering = {site["label"]: site["position"] for site in sites}
    else:
      print("sites:", sites)

    if isinstance(sites, dict):
      # 源码工厂只声明设备局部名称；稳定 UUID 会在库存水合后随 ``sites[]`` 注入。
      self.site_uuid_by_name: dict[str, str] = {}
      self.site_name_by_uuid: dict[str, str] = {}

  @property
  def capacity(self):
    """The number of sites on this carrier."""
    return len(self.sites)

  def __len__(self) -> int:
    """Return the number of sites on this carrier."""
    return len(self.sites)

  def assign_child_resource(
    self,
    resource: ResourcePLR,
    location: Optional[Coordinate],
    reassign: bool = True,
    spot: Optional[int] = None,
  ):
    idx = spot
    # 如果只给 location，根据坐标和 deserialize 后的 self.sites（持有names）来寻找 resource 该摆放的位置
    if spot is not None:
      idx = spot
    else:
      for i, site in enumerate(self.sites):
        site_location = list(self.child_locations.values())[i]
        if type(site) == str and site == resource.name:
          idx = i
          break
        if site_location == location:
          idx = i
          break

    if not reassign and self.sites[idx] is not None:
      raise ValueError(f"a site with index {idx} already exists")
    location = list(self.child_locations.values())[idx]
    super().assign_child_resource(resource, location=location, reassign=reassign)
    self.sites[idx] = resource

  def assign_resource_to_site(self, resource: ResourcePLR, spot: int):
    if self.sites[spot] is not None and not isinstance(self.sites[spot], ResourceHolder):
      raise ValueError(f"spot {spot} already has a resource, {resource}")
    self.assign_child_resource(resource, location=self.child_locations.get(list(self._ordering.keys())[spot]), spot=spot)

  def unassign_child_resource(self, resource: ResourcePLR):
    found = False
    for spot, res in enumerate(self.sites):
      if res == resource:
        self.sites[spot] = None
        found = True
        break
    if not found:
      raise ValueError(f"Resource {resource} is not assigned to this carrier")
    super().unassign_child_resource(resource)
    # if hasattr(resource, "unassign"):
    #   resource.unassign()

  def get_child_identifier(self, child: ResourcePLR):
    """Get the identifier information for a given child resource.

    Args:
        child: The Resource object to find the identifier for

    Returns:
        dict: A dictionary containing:
            - identifier: The string identifier (e.g. "A1", "B2")
            - idx: The integer index in the sites list
            - x: The x index (column index, 0-based)
            - y: The y index (row index, 0-based)
            - z: The z index (layer index, 0-based)

    Raises:
        ValueError: If the child resource is not found in this carrier
    """
    # Find the child resource in sites
    for idx, resource in enumerate(self.sites):
      if resource is child:
        # Get the identifier from ordering keys
        identifier = list(self._ordering.keys())[idx]

        # Parse identifier to get x, y, z indices
        x_idx, y_idx, z_idx = self._parse_identifier_to_indices(identifier, idx)

        return {
          "identifier": identifier,
          "idx": idx,
          "x": x_idx,
          "y": y_idx,
          "z": z_idx
        }

    # If not found, raise an error
    raise ValueError(f"Resource {child} is not assigned to this carrier")

  def site_name_for_child(self, child: ResourcePLR) -> str:
    """返回真实子物料当前占用的设备局部库位名称。

    参数：``child`` 是机械臂动作收到的真实 PLR 物料。返回：载架内部唯一的
    局部库位名称。异常：物料不是该载架当前子物料时抛出 ``ValueError``；不按
    UUID、名称或条码猜测占用关系。
    """

    return str(self.get_child_identifier(child)["identifier"])

  def site_name_for_uuid(self, site_uuid: str) -> str:
    """把稳定库位 UUID 转换为设备局部库位名称。

    参数：``site_uuid`` 是库存权威发布的稳定身份。返回：同一 ``sites[]`` 描述
    中的局部名称。异常：UUID 非法或不属于该载架时抛出 ``ValueError``，禁止把
    UUID 原样发送给设备。
    """

    normalized_uuid = _canonical_site_uuid(site_uuid)
    try:
      return self.site_name_by_uuid[normalized_uuid]
    except KeyError as error:
      raise ValueError(f"载架不包含库位 UUID: {normalized_uuid}") from error

  def _parse_identifier_to_indices(self, identifier: str, idx: int) -> Tuple[int, int, int]:
    """Parse identifier string to get x, y, z indices.

    Args:
        identifier: String identifier like "A1", "B2", etc.
        idx: Linear index as fallback for calculation

    Returns:
        Tuple of (x_idx, y_idx, z_idx)
    """
    # If we have explicit dimensions, calculate from idx
    if self.num_items_x > 0 and self.num_items_y > 0:
      # Calculate 3D indices from linear index
      z_idx = idx // (self.num_items_x * self.num_items_y) if self.num_items_z > 0 else 0
      remaining = idx % (self.num_items_x * self.num_items_y)
      y_idx = remaining // self.num_items_x
      x_idx = remaining % self.num_items_x
      return x_idx, y_idx, z_idx

    # Fallback: parse from Excel-style identifier
    if isinstance(identifier, str) and len(identifier) >= 2:
      # Extract row (letter) and column (number)
      row_letters = ""
      col_numbers = ""

      for char in identifier:
        if char.isalpha():
          row_letters += char
        elif char.isdigit():
          col_numbers += char

      if row_letters and col_numbers:
        # Convert letter(s) to row index (A=0, B=1, etc.)
        y_idx = 0
        for char in row_letters:
          y_idx = y_idx * 26 + (ord(char.upper()) - ord('A'))

        # Convert number to column index (1-based to 0-based)
        x_idx = int(col_numbers) - 1
        z_idx = 0  # Default layer

        return x_idx, y_idx, z_idx

    # If all else fails, assume linear arrangement
    return idx, 0, 0

  def __getitem__(
    self,
    identifier: Union[str, int, Sequence[int], Sequence[str], slice, range],
  ) -> Union[List[T], T]:
    """Get the items with the given identifier.

    This is a convenience method for getting the items with the given identifier. It is equivalent
    to :meth:`get_items`, but adds support for slicing and supports single items in the same
    functional call. Note that the return type will always be a list, even if a single item is
    requested.

    Examples:
      Getting the items with identifiers "A1" through "E1":

        >>> items["A1:E1"]

        [<Item A1>, <Item B1>, <Item C1>, <Item D1>, <Item E1>]

      Getting the items with identifiers 0 through 4 (note that this is the same as above):

        >>> items[range(5)]

        [<Item A1>, <Item B1>, <Item C1>, <Item D1>, <Item E1>]

      Getting items with a slice (note that this is the same as above):

        >>> items[0:5]

        [<Item A1>, <Item B1>, <Item C1>, <Item D1>, <Item E1>]

      Getting a single item:

        >>> items[0]

        [<Item A1>]
    """

    if isinstance(identifier, str):
      if ":" in identifier:  # multiple # TODO: deprecate this, use `"A1":"E1"` instead (slice)
        return self.get_items(identifier)

      return self.get_item(identifier)  # single

    if isinstance(identifier, int):
      return self.get_item(identifier)

    if isinstance(identifier, (slice, range)):
      start, stop = identifier.start, identifier.stop
      if isinstance(identifier.start, str):
        start = list(self._ordering.keys()).index(identifier.start)
      elif identifier.start is None:
        start = 0
      if isinstance(identifier.stop, str):
        stop = list(self._ordering.keys()).index(identifier.stop)
      elif identifier.stop is None:
        stop = self.num_items
      identifier = list(range(start, stop, identifier.step or 1))
      return self.get_items(identifier)

    if isinstance(identifier, (list, tuple)):
      return self.get_items(identifier)

    raise TypeError(f"Invalid identifier type: {type(identifier)}")

  def get_item(self, identifier: Union[str, int, Tuple[int, int]]) -> T:
    """Get the item with the given identifier.

    Args:
      identifier: The identifier of the item. Either a string, an integer, or a tuple. If an
      integer, it is the index of the item in the list of items (counted from 0, top to bottom, left
      to right).  If a string, it uses transposed MS Excel style notation, e.g. "A1" for the first
      item, "B1" for the item below that, etc. If a tuple, it is (row, column).

    Raises:
      IndexError: If the identifier is out of range. The range is 0 to self.num_items-1 (inclusive).
    """

    if isinstance(identifier, tuple):
      row, column = identifier
      identifier = LETTERS[row] + str(column + 1)  # standard transposed-Excel style notation
    if isinstance(identifier, str):
      try:
        identifier = list(self._ordering.keys()).index(identifier)
      except ValueError as e:
        raise IndexError(
          f"Item with identifier '{identifier}' does not exist on " f"resource '{self.name}'."
        ) from e

    if not 0 <= identifier < self.capacity:
      raise IndexError(
        f"Item with identifier '{identifier}' does not exist on " f"resource '{self.name}'."
      )

    # Cast child to item type. Children will always be `T`, but the type checker doesn't know that.
    return self.sites[identifier]

  def get_items(self, identifiers: Union[str, Sequence[int], Sequence[str]]) -> List[T]:
    """Get the items with the given identifier.

    Args:
      identifier: Deprecated. Use `identifiers` instead. # TODO(deprecate-ordered-items)
      identifiers: The identifiers of the items. Either a string range or a list of integers. If a
        string, it uses transposed MS Excel style notation. Regions of items can be specified using
        a colon, e.g. "A1:H1" for the first column. If a list of integers, it is the indices of the
        items in the list of items (counted from 0, top to bottom, left to right).

    Examples:
      Getting the items with identifiers "A1" through "E1":

        >>> items.get_items("A1:E1")

        [<Item A1>, <Item B1>, <Item C1>, <Item D1>, <Item E1>]

      Getting the items with identifiers 0 through 4:

        >>> items.get_items(range(5))

        [<Item A1>, <Item B1>, <Item C1>, <Item D1>, <Item E1>]
    """

    if isinstance(identifiers, str):
      identifiers = pylabrobot.utils.expand_string_range(identifiers)
    return [self.get_item(i) for i in identifiers]

  def __setitem__(self, idx: Union[int, str], resource: Optional[ResourcePLR]):
    """Assign a resource to this carrier."""
    if resource is None:  # setting to None
      assigned_resource = self[idx]
      if assigned_resource is not None:
        self.unassign_child_resource(assigned_resource)
    else:
      idx = list(self._ordering.keys()).index(idx) if isinstance(idx, str) else idx
      self.assign_resource_to_site(resource, spot=idx)

  def __delitem__(self, idx: int):
    """Unassign a resource from this carrier."""
    assigned_resource = self[idx]
    if assigned_resource is not None:
      self.unassign_child_resource(assigned_resource)

  def get_resources(self) -> List[ResourcePLR]:
    """Get all resources assigned to this carrier."""
    return [resource for resource in self.sites.values() if resource is not None]

  def __eq__(self, other):
    return super().__eq__(other) and self.sites == other.sites

  def get_free_sites(self) -> List[int]:
    return [spot for spot, resource in self.sites.items() if resource is None]

  def serialize(self):
    """序列化载架及一等库位身份。

    参数：无。返回：兼容 PLR 的载架字典；已由库存权威分配的 UUID 与局部名称
    保存在同一 ``sites[]`` 成员，未分配 UUID 的源码模板保持原有形状。
    """

    return {
      **super().serialize(),
      "num_items_x": self.num_items_x,
      "num_items_y": self.num_items_y,
      "num_items_z": self.num_items_z,
      "layout": self.layout,
      "sites": [{
        "label": str(identifier),
        **(
          {"uuid": self.site_uuid_by_name[str(identifier)]}
          if str(identifier) in self.site_uuid_by_name
          else {}
        ),
        "visible": False if identifier in self.invisible_slots else True,
        "occupied_by": self[identifier].name
                        if isinstance(self[identifier], ResourcePLR) and not isinstance(self[identifier], ResourceHolder) else
                        self[identifier] if isinstance(self[identifier], str) else None,
        "position": {"x": location.x, "y": location.y, "z": location.z},
        "size": self.child_size[identifier],
        "content_type": ["bottle", "container", "tube", "bottle_carrier", "tip_rack"]
      } for identifier, location in self.child_locations.items()]
    }


class BottleCarrier(ItemizedCarrier):
    """瓶载架 - 直接继承自 TubeCarrier"""

    def __init__(
        self,
        name: str,
        size_x: float,
        size_y: float,
        size_z: float,
        sites: Optional[Dict[Union[int, str], ResourceHolder]] = None,
        category: str = "bottle_carrier",
        model: Optional[str] = None,
        invisible_slots: List[str] = None,
        **kwargs,
    ):
        super().__init__(
            name=name,
            size_x=size_x,
            size_y=size_y,
            size_z=size_z,
            sites=sites,
            category=category,
            model=model,
            invisible_slots=invisible_slots,
        )
