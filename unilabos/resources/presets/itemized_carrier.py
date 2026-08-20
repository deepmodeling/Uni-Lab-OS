"""UniLabOS 预设的按位载架与瓶子资源。"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple, TypeVar, Union

import pylabrobot
from pylabrobot.resources import Carrier, Resource as ResourcePLR
from pylabrobot.resources import ResourceHolder, Well
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
            cross_section_type="circle",
        )
        self.diameter = diameter
        self.height = height


T = TypeVar("T", bound=ResourceHolder)


class ItemizedCarrier(Carrier[ResourceHolder]):
    """UniLabOS 按位载架。

    与 PLR ``Carrier`` 保持相同语义：``carrier[item]`` 返回
    :class:`ResourceHolder`，槽位中的物料通过 ``carrier[item].resource`` 访问。
    ``resource_sites`` 只保存微后端的 canonical Site 快照，不替代 PLR 槽位树。
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
        layout: Optional[str] = None,
        sites: Optional[
            Union[
                Dict[Union[int, str], Optional[ResourcePLR]],
                Sequence[Union[ResourceSite, Dict[str, Any]]],
            ]
        ] = None,
        category: Optional[str] = "carrier",
        model: Optional[str] = None,
        invisible_slots: Optional[Sequence[Union[int, str]]] = None,
        site_labels: Optional[Sequence[Union[int, str]]] = None,
        site_indices: Optional[Mapping[Union[int, str], Sequence[int]]] = None,
    ):
        self.resource_sites: Optional[List[ResourceSite]] = None
        self._ordering: Dict[Union[int, str], ResourceHolder] = {}
        self.child_locations: Dict[Union[int, str], Coordinate] = {}
        self.child_size: Dict[Union[int, str], Dict[str, float]] = {}
        self._pending_site_labels = list(site_labels or [])
        self._site_indices: Dict[str, Tuple[int, int, int]] = {}
        for label, indices in (site_indices or {}).items():
            if len(indices) != 3:
                raise ValueError(f"Site {label!r} 的坐标索引必须是 [x, y, z]")
            self._site_indices[str(label)] = tuple(int(value) for value in indices)

        super().__init__(
            name=name,
            size_x=size_x,
            size_y=size_y,
            size_z=size_z,
            sites=None,
            category=category,
            model=model,
        )
        self.num_items_x, self.num_items_y, self.num_items_z = (
            num_items_x,
            num_items_y,
            num_items_z,
        )
        self.invisible_slots = list(invisible_slots or [])
        inferred_layout = (
            "z-y"
            if self.num_items_z > 1 and self.num_items_x == 1
            else "x-z"
            if self.num_items_z > 1 and self.num_items_y == 1
            else "x-y"
        )
        self.layout = layout or inferred_layout

        if isinstance(sites, dict):
            for spot, site_holder in sites.items():
                if site_holder is None:
                    site_holder = ResourceHolder(
                        name=str(spot),
                        size_x=0,
                        size_y=0,
                        size_z=0,
                        child_location=Coordinate.zero(),
                    )
                    site_holder.location = Coordinate.zero()
                if not isinstance(site_holder, ResourceHolder):
                    raise TypeError(
                        f"ItemizedCarrier Site {spot!r} 必须是 ResourceHolder，"
                        f"不能是 {type(site_holder).__name__}"
                    )
                if site_holder.location is None:
                    raise ValueError(f"site {site_holder} has no location")
                self._add_site_holder(spot, site_holder)
        elif sites is not None:
            normalized = [
                site.model_copy(deep=True)
                if isinstance(site, ResourceSite)
                else ResourceSite.model_validate(site)
                for site in sites
            ]
            self.resource_sites = normalized
            for site in normalized:
                site_holder = ResourceHolder(
                    name=site.label,
                    size_x=site.pose.size.width,
                    size_y=site.pose.size.height,
                    size_z=site.pose.size.depth,
                    child_location=Coordinate.zero(),
                )
                site_holder.location = Coordinate(
                    site.pose.position3d.x,
                    site.pose.position3d.y,
                    site.pose.position3d.z,
                )
                site_holder.unilabos_site_uuid = site.uuid
                site_holder.unilabos_site_metadata = site.model_dump()
                # canonical Site 本身已有权威 UUID；当返回树没有单列结构 holder
                # 物料节点时，用 Site UUID 给运行时 holder 一个稳定身份。
                site_holder.unilabos_uuid = site.uuid
                self._add_site_holder(site.label, site_holder)
            if normalized:
                owner_uuids = {site.material_uuid for site in normalized}
                template_names = {site.template_name for site in normalized}
                if len(owner_uuids) != 1 or len(template_names) != 1:
                    raise ValueError(
                        f"ItemizedCarrier {self.name} 的 canonical sites "
                        "必须共享同一 owner 和 template"
                    )
                self.unilabos_uuid = normalized[0].material_uuid
                # 局部导入避免 resource_tracker 注册本类时循环依赖。
                from unilabos.resources.resource_tracker import set_plr_template_name

                set_plr_template_name(self, normalized[0].template_name)

        self.num_items = len(self.sites)

    def _add_site_holder(
        self,
        label: Union[int, str],
        site_holder: ResourceHolder,
        *,
        spot: Optional[int] = None,
        reassign: bool = True,
    ) -> None:
        """把一个 PLR holder 注册成载架槽位，并同步静态 Site 视图。"""

        if label in self._ordering and self._ordering[label] is not site_holder:
            raise ValueError(f"ItemizedCarrier {self.name} 已存在 Site {label!r}")
        if site_holder.location is None:
            raise ValueError(f"site {site_holder} has no location")
        idx = len(self.sites) if spot is None else spot
        super().assign_child_resource(
            site_holder,
            location=site_holder.location,
            reassign=reassign,
            spot=idx,
        )
        self._ordering[label] = site_holder
        self.child_locations[label] = site_holder.location
        self.child_size[label] = {
            "width": site_holder.get_size_x(),
            "height": site_holder.get_size_y(),
            "depth": site_holder.get_size_z(),
        }

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
            occupant = self.sites[ordinal].resource
            actual_uuid = (
                str(getattr(occupant, "unilabos_uuid", "") or "") or None
                if occupant is not None
                else None
            )
            if actual_uuid != site.occupied_material_uuid:
                raise ValueError(
                    f"ItemizedCarrier {self.name} 的 Site {site.label} 占用关系冲突: "
                    f"native={actual_uuid}, canonical={site.occupied_material_uuid}"
                )
            holder = self.sites[ordinal]
            holder.unilabos_site_uuid = site.uuid
            holder.unilabos_site_metadata = site.model_dump()
            if not getattr(holder, "unilabos_uuid", ""):
                holder.unilabos_uuid = site.uuid
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
        """注册 holder 子节点；占用物料必须通过 ``assign_resource_to_site``。"""

        if not isinstance(resource, ResourceHolder):
            raise TypeError(
                f"ItemizedCarrier 的直接子节点必须是 ResourceHolder，"
                f"不能是 {type(resource).__name__}"
            )
        if location is None:
            raise ValueError(f"site {resource} has no location")
        # PLR Resource.deserialize 在构造 child 后通过本方法恢复相对位置。
        resource.location = location
        idx = len(self.sites) if spot is None else spot
        label = (
            self._pending_site_labels[idx]
            if idx < len(self._pending_site_labels)
            else resource.name
        )
        self._add_site_holder(
            label,
            resource,
            spot=idx,
            reassign=reassign,
        )
        self.num_items = len(self.sites)

    def _validate_occupant_uuid(self, resource: ResourcePLR, spot: int) -> str:
        resource_uuid = str(getattr(resource, "unilabos_uuid", "") or "")
        if self.resource_sites is not None:
            expected_uuid = self.resource_sites[spot].occupied_material_uuid
            if expected_uuid:
                if resource_uuid and resource_uuid != expected_uuid:
                    raise ValueError(
                        f"ItemizedCarrier {self.name} 的 Site {self.resource_sites[spot].label} "
                        f"期望物料 UUID {expected_uuid!r}，实际为 {resource_uuid!r}"
                    )
                if not resource_uuid:
                    resource.unilabos_uuid = expected_uuid
                    resource_uuid = expected_uuid
            if not resource_uuid:
                raise ValueError(
                    f"物料 {resource.name} 缺少微后端分配的 UUID，不能放入 Site"
                )
        return resource_uuid

    def assign_resource_to_site(self, resource: ResourcePLR, spot: int):
        holder = self.sites[spot]
        if holder.resource is not None:
            raise ValueError(f"spot {spot} already has a resource, {holder.resource}")
        resource_uuid = self._validate_occupant_uuid(resource, spot)
        holder.assign_child_resource(resource)
        if self.resource_sites is not None:
            self.resource_sites[spot] = self.resource_sites[spot].model_copy(
                update={"occupied_material_uuid": resource_uuid}
            )

    def unassign_child_resource(self, resource: ResourcePLR):
        for spot, holder in self.sites.items():
            if holder.resource is resource:
                if self.resource_sites is not None:
                    self.resource_sites[spot] = self.resource_sites[spot].model_copy(
                        update={"occupied_material_uuid": None}
                    )
                return super().unassign_child_resource(resource)
        raise ValueError(f"Resource {resource} is not assigned to this carrier")

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
        for idx, (identifier, holder) in enumerate(self._ordering.items()):
            if holder is child or holder.resource is child:

                # Parse identifier to get x, y, z indices
                x_idx, y_idx, z_idx = self._parse_identifier_to_indices(
                    str(identifier), idx
                )

                return {
                    "identifier": identifier,
                    "idx": idx,
                    "x": x_idx,
                    "y": y_idx,
                    "z": z_idx,
                }

        raise ValueError(f"Resource {child} is not assigned to this carrier")

    def _parse_identifier_to_indices(
        self, identifier: str, idx: int
    ) -> Tuple[int, int, int]:
        """Parse identifier string to get x, y, z indices.

        Args:
            identifier: String identifier like "A1", "B2", etc.
            idx: Linear index as fallback for calculation

        Returns:
            Tuple of (x_idx, y_idx, z_idx)
        """
        explicit = self._site_indices.get(str(identifier))
        if explicit is not None:
            return explicit

        # 标准二维载架以标识符为准，避免把 PLR 的列优先顺序误当成行优先。
        if self.num_items_z <= 1 and isinstance(identifier, str) and len(identifier) >= 2:
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
                    y_idx = y_idx * 26 + (ord(char.upper()) - ord("A") + 1)
                y_idx -= 1

                # Convert number to column index (1-based to 0-based)
                x_idx = int(col_numbers) - 1
                z_idx = 0  # Default layer

                return x_idx, y_idx, z_idx

        if self.num_items_x > 0 and self.num_items_y > 0:
            plane_size = self.num_items_x * self.num_items_y
            z_idx = idx // plane_size
            remaining = idx % plane_size
            ordering_layout = getattr(self, "ordering_layout", "col-major")
            if ordering_layout in {"col-major", "vertical-col-major"}:
                x_idx = remaining // self.num_items_y
                y_idx = remaining % self.num_items_y
            else:
                y_idx = remaining // self.num_items_x
                x_idx = remaining % self.num_items_x
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
            if (
                ":" in identifier
            ):  # multiple # TODO: deprecate this, use `"A1":"E1"` instead (slice)
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
            identifier = LETTERS[row] + str(
                column + 1
            )  # standard transposed-Excel style notation
        if isinstance(identifier, str):
            try:
                identifier = list(self._ordering.keys()).index(identifier)
            except ValueError as e:
                raise IndexError(
                    f"Item with identifier '{identifier}' does not exist on "
                    f"resource '{self.name}'."
                ) from e

        if not 0 <= identifier < self.capacity:
            raise IndexError(
                f"Item with identifier '{identifier}' does not exist on "
                f"resource '{self.name}'."
            )

        # Cast child to item type. Children will always be `T`, but the type checker doesn't know that.
        return self.sites[identifier]

    def get_items(
        self, identifiers: Union[str, Sequence[int], Sequence[str]]
    ) -> List[T]:
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
            assigned_resource = self[idx].resource
            if assigned_resource is not None:
                self.unassign_child_resource(assigned_resource)
        else:
            idx = (
                list(self._ordering.keys()).index(idx) if isinstance(idx, str) else idx
            )
            self.assign_resource_to_site(resource, spot=idx)

    def __delitem__(self, idx: int):
        """Unassign a resource from this carrier."""
        assigned_resource = self[idx].resource
        if assigned_resource is not None:
            self.unassign_child_resource(assigned_resource)

    def get_resources(self) -> List[ResourcePLR]:
        """Get all resources assigned to this carrier."""
        return [
            holder.resource
            for holder in self.sites.values()
            if holder.resource is not None
        ]

    def __eq__(self, other):
        return (
            super().__eq__(other)
            and self.sites == other.sites
            and self.resource_sites == other.resource_sites
        )

    def get_free_sites(self) -> List[ResourceHolder]:
        return [holder for holder in self.sites.values() if holder.resource is None]

    def serialize(self):
        """只序列化 PLR 原生资源结构。

        canonical Site 快照属于 ``ResourceDict.sites``，统一通过
        ``extract_plr_sites`` 提取。这与 ``PRCXI9300Deck`` 一致，避免
        PLR payload 和 ResourceTreeSet 各自序列化一份 Site 真相。
        """

        return {
            **super().serialize(),
            "num_items_x": self.num_items_x,
            "num_items_y": self.num_items_y,
            "num_items_z": self.num_items_z,
            "layout": self.layout,
            "site_labels": list(self._ordering),
            "site_indices": {
                label: list(indices) for label, indices in self._site_indices.items()
            },
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
        invisible_slots: Optional[Sequence[Union[int, str]]] = None,
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
            **kwargs,
        )
