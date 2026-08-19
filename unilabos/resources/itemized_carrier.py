"""
自动化液体处理工作站物料类定义 - 简化版
Automated Liquid Handling Station Resource Classes - Simplified Version
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TypeVar, Union, Sequence, Tuple

import pylabrobot
from pylabrobot.resources import Resource as ResourcePLR
from pylabrobot.resources import Well, ResourceHolder
from pylabrobot.resources.coordinate import Coordinate

from unilabos.resources.objects.site import ResourceSite

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


class ItemizedCarrier(ResourcePLR):
  """UniLabOS 按位载架。

  ``resource_sites`` 是唯一 Site 元数据；``sites`` 只保存运行时占用对象。
  序列化和反序列化均使用 canonical ``ResourceSite``，不再接受按资源名
  表示占用关系的旧 PLR Site 字典。
  """

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
    sites: Optional[
      Union[
        Dict[Union[int, str], Optional[ResourcePLR]],
        Sequence[Union[ResourceSite, Dict[str, Any]]],
      ]
    ] = None,
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
    self.num_items_x, self.num_items_y, self.num_items_z = num_items_x, num_items_y, num_items_z
    self.invisible_slots = [] if invisible_slots is None else invisible_slots
    self.layout = "z-y" if self.num_items_z > 1 and self.num_items_x == 1 else "x-z" if self.num_items_z > 1 and self.num_items_y == 1 else "x-y"
    self.resource_sites: Optional[List[ResourceSite]] = None
    self.sites: List[Optional[ResourcePLR]] = []
    self._ordering: Dict[Union[int, str], Any] = {}
    self.child_locations: Dict[Union[int, str], Coordinate] = {}
    self.child_size: Dict[Union[int, str], Dict[str, float]] = {}

    if isinstance(sites, dict):
      sites = sites or {}
      self.sites = list(sites.values())
      self._ordering = sites
      for spot, resource in sites.items():
        if resource is not None and getattr(resource, "location", None) is None:
          raise ValueError(f"resource {resource} has no location")
        if resource is not None:
          self.child_locations[spot] = resource.location
          self.child_size[spot] = {"width": resource._size_x, "height": resource._size_y, "depth": resource._size_z}
        else:
          self.child_locations[spot] = Coordinate.zero()
          self.child_size[spot] = {"width": 0, "height": 0, "depth": 0}
    elif sites is not None:
      normalized = [
        site.model_copy(deep=True)
        if isinstance(site, ResourceSite)
        else ResourceSite.model_validate(site)
        for site in sites
      ]
      self.resource_sites = normalized
      self.sites = [None] * len(normalized)
      self._ordering = {site.label: None for site in normalized}
      self.child_locations = {
        site.label: Coordinate(
          site.pose.position3d.x,
          site.pose.position3d.y,
          site.pose.position3d.z,
        )
        for site in normalized
      }
      self.child_size = {
        site.label: site.pose.size.model_dump() for site in normalized
      }
      if normalized:
        self.unilabos_uuid = normalized[0].material_uuid

    self.num_items = len(self.sites)

  def set_resource_sites(
    self, sites: Sequence[Union[ResourceSite, Dict[str, Any]]]
  ) -> None:
    """注入并核对微后端返回的 canonical Site 快照。"""

    normalized = [
      site.model_copy(deep=True)
      if isinstance(site, ResourceSite)
      else ResourceSite.model_validate(site)
      for site in sites
    ]
    native_labels = [str(label) for label in self.child_locations]
    canonical_labels = [site.label for site in normalized]
    if native_labels != canonical_labels:
      raise ValueError(
        f"ItemizedCarrier {self.name} 的槽位与 canonical sites 不一致: "
        f"native={native_labels}, canonical={canonical_labels}"
      )
    if len(self.sites) != len(normalized):
      raise ValueError(
        f"ItemizedCarrier {self.name} 的槽位数量与 canonical sites 不一致"
      )

    for ordinal, site in enumerate(normalized):
      site_key = site.label if site.label in self.child_locations else site.index
      location = self.child_locations[site_key]
      if location != Coordinate(
        site.pose.position3d.x,
        site.pose.position3d.y,
        site.pose.position3d.z,
      ):
        raise ValueError(
          f"ItemizedCarrier {self.name} 的 Site {site.label} 位置与 canonical 快照冲突"
        )
      if self.child_size[site_key] != site.pose.size.model_dump():
        raise ValueError(
          f"ItemizedCarrier {self.name} 的 Site {site.label} 尺寸与 canonical 快照冲突"
        )
      occupant = self.sites[ordinal]
      actual_uuid = None
      if occupant is not None and not isinstance(occupant, ResourceHolder):
        actual_uuid = str(getattr(occupant, "unilabos_uuid", "") or "") or None
      if actual_uuid != site.occupied_material_uuid:
        raise ValueError(
          f"ItemizedCarrier {self.name} 的 Site {site.label} 占用关系冲突: "
          f"native={actual_uuid}, canonical={site.occupied_material_uuid}"
        )
    self.resource_sites = normalized

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
    # 如果只给 location，根据 canonical Site 坐标定位槽位。
    if spot is not None:
      idx = spot
    else:
      for i in range(len(self.sites)):
        site_location = list(self.child_locations.values())[i]
        if site_location == location:
          idx = i
          break

    if idx is None:
      raise ValueError(f"无法为资源 {resource.name} 确定 ItemizedCarrier Site")
    if not reassign and self.sites[idx] is not None:
      raise ValueError(f"a site with index {idx} already exists")
    if self.resource_sites is not None:
      expected_uuid = self.resource_sites[idx].occupied_material_uuid
      resource_uuid = str(getattr(resource, "unilabos_uuid", "") or "")
      if expected_uuid:
        if resource_uuid and resource_uuid != expected_uuid:
          raise ValueError(
            f"ItemizedCarrier {self.name} 的 Site {self.resource_sites[idx].label} "
            f"期望物料 UUID {expected_uuid!r}，实际为 {resource_uuid!r}"
          )
        if not resource_uuid:
          resource.unilabos_uuid = expected_uuid
          resource_uuid = expected_uuid
      if not resource_uuid:
        raise ValueError(f"物料 {resource.name} 缺少微后端分配的 UUID，不能放入 Site")
    location = list(self.child_locations.values())[idx]
    super().assign_child_resource(resource, location=location, reassign=reassign)
    self.sites[idx] = resource
    if self.resource_sites is not None:
      self.resource_sites[idx] = self.resource_sites[idx].model_copy(
        update={"occupied_material_uuid": resource_uuid}
      )

  def assign_resource_to_site(self, resource: ResourcePLR, spot: int):
    if self.sites[spot] is not None and not isinstance(self.sites[spot], ResourceHolder):
      raise ValueError(f"spot {spot} already has a resource, {resource}")
    self.assign_child_resource(resource, location=self.child_locations.get(list(self._ordering.keys())[spot]), spot=spot)

  def unassign_child_resource(self, resource: ResourcePLR):
    found = False
    for spot, res in enumerate(self.sites):
      if res == resource:
        self.sites[spot] = None
        if self.resource_sites is not None:
          self.resource_sites[spot] = self.resource_sites[spot].model_copy(
            update={"occupied_material_uuid": None}
          )
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
    return [resource for resource in self.sites if resource is not None]

  def __eq__(self, other):
    return (
      super().__eq__(other)
      and self.sites == other.sites
      and self.resource_sites == other.resource_sites
    )

  def get_free_sites(self) -> List[int]:
    return [spot for spot, resource in enumerate(self.sites) if resource is None]

  def serialize(self):
    if self.resource_sites is None:
      raise ValueError(
        f"ItemizedCarrier {self.name} 缺少微后端返回的 canonical ResourceSite 快照"
      )
    return {
      **super().serialize(),
      "num_items_x": self.num_items_x,
      "num_items_y": self.num_items_y,
      "num_items_z": self.num_items_z,
      "layout": self.layout,
      "sites": [site.model_dump() for site in self.resource_sites],
    }


class BottleCarrier(ItemizedCarrier):
    """瓶载架 - 直接继承自 TubeCarrier"""

    def __init__(
        self,
        name: str,
        size_x: float,
        size_y: float,
        size_z: float,
        sites: Optional[
            Union[
                Dict[Union[int, str], Optional[ResourcePLR]],
                Sequence[Union[ResourceSite, Dict[str, Any]]],
            ]
        ] = None,
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
