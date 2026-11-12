"""测试共用的 PLR 物料夹具：带权威 Site 的最小 Deck 与标准耗材工厂。

厂商驱动（如 PRCXI）已迁到外部驱动包；框架测试改用这里的最小实现，
只覆盖框架依赖的 Deck 行为：构造器接收 canonical ``ResourceSite`` 列表、
按 spot / label / 坐标 / 首个空位挂载，挂载与卸载同步 ``occupied_material_uuid``。
"""

from __future__ import annotations

import collections
from typing import Any, Dict, List, Optional, Sequence, Union
from uuid import uuid4

from pylabrobot.resources import Coordinate, Deck, Resource
from pylabrobot.resources.corning.plates import cor_96_wellplate_2mL_Vb
from pylabrobot.resources.opentrons.tip_racks import opentrons_96_tiprack_300ul

from unilabos.resources.objects.site import ResourceSite
from unilabos.resources.resource_tracker import set_plr_template_name

# 注册表条目形态的标准耗材（PLR 自带定义，不依赖任何驱动包）
TIP_RACK_MODULE = "pylabrobot.resources.opentrons.tip_racks:opentrons_96_tiprack_300ul"
PLATE_MODULE = "pylabrobot.resources.corning.plates:cor_96_wellplate_2mL_Vb"


def tip_rack(name: str):
    """96 孔 300 uL 枪头盒，孔位预置枪头。"""

    return opentrons_96_tiprack_300ul(name)


def plate(name: str):
    """96 孔 2 mL 深孔板；孔名带板名前缀，同一棵树可放多块。"""

    return cor_96_wellplate_2mL_Vb(name)


def registry_entry(module: str) -> Dict[str, Any]:
    """与注册表 YAML 同构的最小 pylabrobot 资源条目。"""

    return {"class": {"module": module, "type": "pylabrobot"}}


class SiteDeck(Deck):
    """只接受权威 ResourceSite 快照的最小 Deck。"""

    def __init__(
        self,
        name: str,
        size_x: float,
        size_y: float,
        size_z: float,
        sites: Optional[Sequence[Union[ResourceSite, Dict[str, Any]]]] = None,
        **kwargs: Any,
    ):
        super().__init__(size_x, size_y, size_z, name)
        if sites is None:
            raise ValueError(f"Deck {name} 缺少 ResourceSite 快照")
        self.sites: List[ResourceSite] = [
            site.model_copy(deep=True) if isinstance(site, ResourceSite) else ResourceSite.model_validate(site)
            for site in sites
        ]
        template_name = self.__class__.__name__
        if self.sites:
            self.unilabos_uuid = self.sites[0].material_uuid
            template_name = self.sites[0].template_name
        set_plr_template_name(self, template_name)
        self._ordering = collections.OrderedDict((site.label, None) for site in self.sites)

    def _site_location(self, idx: int) -> Coordinate:
        position = self.sites[idx].pose.position3d
        return Coordinate(position.x, position.y, position.z)

    def _site_resource(self, idx: int) -> Optional[Resource]:
        location = self._site_location(idx)
        return next((child for child in self.children if child.location == location), None)

    def assign_child_resource(
        self,
        resource: Resource,
        location: Optional[Coordinate] = None,
        reassign: bool = True,
        spot: Optional[int] = None,
    ):
        idx = spot
        if idx is None:
            idx = next(
                (
                    i
                    for i, site in enumerate(self.sites)
                    if site.label == resource.name
                    or (location is not None and self._site_location(i) == location)
                ),
                None,
            )
        if idx is None:
            idx = next((i for i in range(len(self.sites)) if self._site_resource(i) is None), None)
        if idx is None or not 0 <= idx < len(self.sites):
            raise ValueError(f"Deck {self.name} 没有可用 Site 放置 {resource.name}")
        if not reassign and self._site_resource(idx) is not None:
            raise ValueError(f"Site {self.sites[idx].label} 已被占用")

        expected = self.sites[idx].occupied_material_uuid
        occupant = getattr(resource, "unilabos_uuid", "")
        if expected:
            if occupant and str(occupant) != expected:
                raise ValueError(
                    f"Site {self.sites[idx].label} 期望物料 {expected!r}，实际为 {occupant!r}"
                )
            if not occupant:
                resource.unilabos_uuid = occupant = expected
        if not occupant:
            raise ValueError(f"物料 {resource.name} 缺少微后端分配的 UUID，不能放入 Site")
        # 先登记占用再触发 PLR assign：assign 回调会立即冻结快照
        previous = self.sites[idx]
        self.sites[idx] = previous.model_copy(update={"occupied_material_uuid": str(occupant)})
        try:
            super().assign_child_resource(resource, location=self._site_location(idx), reassign=reassign)
        except BaseException:
            self.sites[idx] = previous
            raise

    def unassign_child_resource(self, resource: Resource):
        idx = next((i for i in range(len(self.sites)) if self._site_resource(i) is resource), None)
        previous = self.sites[idx] if idx is not None else None
        if idx is not None:
            self.sites[idx] = previous.model_copy(update={"occupied_material_uuid": None})
        try:
            super().unassign_child_resource(resource)
        except BaseException:
            if idx is not None:
                self.sites[idx] = previous
            raise


def site_deck(
    deck_uuid: str,
    name: str,
    labels: Sequence[str] = ("T1", "T2"),
    *,
    pitch: float = 138.0,
    template_name: str = "SiteDeck",
) -> SiteDeck:
    """按标签横排生成 Site 的 Deck（图/定义形态：带 uuid 与 Site 身份）。"""

    def _site(index: int, label: str) -> ResourceSite:
        x = pitch * index
        return ResourceSite(
            uuid=str(uuid4()),
            template_name=template_name,
            material_uuid=deck_uuid,
            index=index,
            label=label,
            pose={
                "position": {"x": x, "y": 0.0, "z": 0.0},
                "position3d": {"x": x, "y": 0.0, "z": 0.0},
                "size": {"width": 128.0, "height": 86.0, "depth": 0.0},
            },
            allowed_resource_categories=["plate", "tip_rack", "container"],
        )

    deck = SiteDeck(
        name=name,
        size_x=max(542.0, pitch * len(labels)),
        size_y=374.0,
        size_z=0.0,
        sites=[_site(i, label) for i, label in enumerate(labels)],
    )
    deck.unilabos_uuid = deck_uuid
    return deck
