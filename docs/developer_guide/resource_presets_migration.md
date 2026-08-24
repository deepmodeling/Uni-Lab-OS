# Resource preset 迁移记录

本文记录 `origin/dev`（`e757967c`）中资源类的原始模块，以及迁移后的规范模块。
本次没有保留旧模块转发层；仓库内代码、Registry YAML 和测试都必须使用新路径。

## 模块迁移

| `origin/dev` 模块 | 新模块 | 保持的公开符号 |
| --- | --- | --- |
| `unilabos.resources.container` | `unilabos.resources.presets.container` | `RegularContainer`, `get_regular_container` |
| `unilabos.resources.itemized_carrier` | `unilabos.resources.presets.itemized_carrier` | `Bottle`, `ItemizedCarrier`, `BottleCarrier` |
| `unilabos.resources.warehouse` | `unilabos.resources.presets.warehouse` | `WareHouse`, `warehouse_factory` |
| `unilabos.resources.bioyond` | `unilabos.resources.presets.bioyond` | 整包迁移，子模块和符号名不变 |
| `unilabos.resources.battery` | `unilabos.resources.presets.battery` | 整包迁移，子模块和符号名不变 |
| `unilabos.resources.site_definition` | `unilabos.resources.objects.site` | `SiteDefinition`, `normalize_available_sites`, `validate_instantiated_sites` |

## 类迁移

| `origin/dev` 类 | 新类路径 |
| --- | --- |
| `unilabos.resources.container.RegularContainer` | `unilabos.resources.presets.container.RegularContainer` |
| `unilabos.resources.itemized_carrier.Bottle` | `unilabos.resources.presets.itemized_carrier.Bottle` |
| `unilabos.resources.itemized_carrier.ItemizedCarrier` | `unilabos.resources.presets.itemized_carrier.ItemizedCarrier` |
| `unilabos.resources.itemized_carrier.BottleCarrier` | `unilabos.resources.presets.itemized_carrier.BottleCarrier` |
| `unilabos.resources.warehouse.WareHouse` | `unilabos.resources.presets.warehouse.WareHouse` |
| `unilabos.resources.battery.electrode_sheet.ElectrodeSheetState` | `unilabos.resources.presets.battery.electrode_sheet.ElectrodeSheetState` |
| `unilabos.resources.battery.electrode_sheet.ElectrodeSheet` | `unilabos.resources.presets.battery.electrode_sheet.ElectrodeSheet` |
| `unilabos.resources.battery.electrode_sheet.BatteryState` | `unilabos.resources.presets.battery.electrode_sheet.BatteryState` |
| `unilabos.resources.battery.electrode_sheet.Battery` | `unilabos.resources.presets.battery.electrode_sheet.Battery` |
| `unilabos.resources.battery.magazine.Magazine` | `unilabos.resources.presets.battery.magazine.Magazine` |
| `unilabos.resources.battery.magazine.MagazineHolder` | `unilabos.resources.presets.battery.magazine.MagazineHolder` |
| `unilabos.resources.bioyond.decks.BIOYOND_PolymerReactionStation_Deck` | `unilabos.resources.presets.bioyond.decks.BIOYOND_PolymerReactionStation_Deck` |
| `unilabos.resources.bioyond.decks.BIOYOND_PolymerPreparationStation_Deck` | `unilabos.resources.presets.bioyond.decks.BIOYOND_PolymerPreparationStation_Deck` |
| `unilabos.resources.bioyond.decks.BIOYOND_YB_Deck` | `unilabos.resources.presets.bioyond.decks.BIOYOND_YB_Deck` |
| `unilabos.resources.site_definition.SiteDefinition` | `unilabos.resources.objects.site.SiteDefinition` |
| `unilabos.resources.objects.site.ResourceSite` | `unilabos.resources.objects.site.ResourceSite`（实例模型，路径不变） |

BIOYOND 与 battery 包内的工厂函数数量较多，统一只替换模块前缀：
`unilabos.resources.bioyond.*` → `unilabos.resources.presets.bioyond.*`，
`unilabos.resources.battery.*` → `unilabos.resources.presets.battery.*`；函数名、Registry 模板名和序列化模型名均不改。

## Site 模型边界

`SiteDefinition` 与 `ResourceSite` 不是两份等价实现：前者是 Registry 中不带实例
UUID/占用关系的模板定义，后者是微后端返回的、带 UUID 和占用关系的权威实例。
两者仍是独立模型，但集中在 `objects/site.py`，共享同一套 pose 规范化与校验逻辑。

`ItemizedCarrier.serialize()` 与 `PRCXI9300Deck` 使用相同边界：PLR payload 不额外
输出 `sites`；canonical Site 仅通过 `ResourceTreeSet.sites` 和 adapter sidecar
上传、下载，避免 PLR 自身序列化与微后端权威 Site 出现两份表示。

## ItemizedCarrier 与坐标语义

- `ItemizedCarrier` 现在遵循 PLR `Carrier`：`carrier[item]` 返回
  `ResourceHolder`，占用物料通过 `carrier[item].resource` 访问；载架、holder、物料
  的树关系保持为 `carrier → holder → resource`。
- `get_child_identifier()` 可接收 holder 或其占用物料，返回同一组
  `identifier/idx/x/y/z`。
- `warehouse_factory()` 用同一条网格记录生成标签、holder 位置和逻辑
  `(x, y, z)`；row-major、col-major、vertical-col-major 不再分别生成后再按顺序
  `zip`。
- `removed_positions` 删除槽位后保留其余槽位的原始三维索引，不再按压缩后的线性
  序号重算坐标。
- 后处理工作站不再维护另一份 `WareHouse` 类，只保留其数字标签工厂并复用
  `unilabos.resources.presets.warehouse.WareHouse`。

## 删除项

- `unilabos.resources.registry`：旧 `add_schema/resource_schema` 全仓无调用。
- `state_keys.py`、`extra_keys.py`、`bioyond/state.py`：仅包含 BIOYOND 或旧 extra
  兼容；框架不再提供 `get_bioyond_state`。
- `resource_state.py`：孤立的 `_unilabos_state` sidecar，无生产入口；资源状态走
  PLR 原生 `serialize_state/load_state`，厂商状态由具体驱动实现。
