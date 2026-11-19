# Resource preset 模块与模型边界

资源预设、实例对象和 Site 模型按以下命名空间组织：

| 模块 | 主要公开符号 |
| --- | --- |
| `unilabos.resources.presets.container` | `RegularContainer`, `get_regular_container` |
| `unilabos.resources.presets.itemized_carrier` | `Bottle`, `ItemizedCarrier`, `BottleCarrier` |
| `unilabos.resources.presets.warehouse` | `WareHouse`, `warehouse_factory` |
| `unilabos.resources.presets.tips` | `make_unmeasured_tip`, `tip_geometry_metadata`（自定义枪头的未实测几何标记） |
| `unilabos.resources.objects.site` | `SiteDefinition`, `ResourceSite` 及 Site 校验函数 |

厂商物料预设（如 Bioyond、电池物料）随各自的外部驱动包发布，不在主仓库。

## Site 模型

`SiteDefinition` 描述 Registry 中不带实例 UUID 和占用关系的模板。`ResourceSite`
描述微后端返回的权威实例，包含 UUID 和占用关系。两者集中定义在
`objects/site.py`，并共用 pose 规范化与校验逻辑。

`ItemizedCarrier.serialize()` 与带 Site 的 Deck（如 PRCXI 驱动包的 PRCXI9300Deck）的 PLR payload 不包含额外的
`sites` 字段。规范化 Site 通过 `ResourceTreeSet.sites` 和 adapter sidecar
上传、下载，确保权威实例只有一种表示。

## ItemizedCarrier 与坐标

- `ItemizedCarrier` 遵循 PLR `Carrier` 语义：`carrier[item]` 返回
  `ResourceHolder`，占用物料通过 `carrier[item].resource` 访问。树关系为
  `carrier → holder → resource`。
- `get_child_identifier()` 接受 holder 或其占用物料，并返回对应的
  `identifier/idx/x/y/z`。
- `warehouse_factory()` 使用同一组网格记录生成标签、holder 位置和逻辑
  `(x, y, z)`，支持 `row-major`、`col-major` 和 `vertical-col-major`。
- `removed_positions` 删除槽位后，其余槽位保留原始三维索引。
