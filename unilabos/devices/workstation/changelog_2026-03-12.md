# 代码变更说明 — 2026-03-12

> 本次变更基于 `implementation_plan_v2.md` 执行，目标：**物理几何结构初始化与物料内容物填充彻底解耦**，消除 PLR 反序列化时的 `Resource already assigned to deck` 错误，并修复若干运行时新增问题。

---

## 一、物料系统标准化重构（主线任务）

### 1. `unilabos/resources/battery/magazine.py`

**改动**：`MagazineHolder_6_Cathode`、`MagazineHolder_6_Anode`、`MagazineHolder_4_Cathode` 三个工厂函数的 `klasses` 参数改为 `None`。

**原因**：原来三个工厂函数在初始化时就向洞位填满极片对象（`ElectrodeSheet`），导致 PLR 反序列化时"几何结构已创建子节点 + DB 再次 assign"双重冲突。

**原则**：物料余量改由寄存器直读（阶段 F），资源树不再追踪每个极片实体。`MagazineHolder_6_Battery` 原本就是 `klasses=None`，三者现在保持一致。

---

### 2. `unilabos/resources/battery/magazine.py`（追加，响应重复 UUID 问题）

**改动**：为 `Magazine`（洞位类）新增 `serialize` 和 `deserialize` 重写：
- `serialize`：序列化时强制将 `children` 置空，不再把极片写回数据库。
- `deserialize`：反序列化时强制忽略 `children` 字段，阻止数据库中旧极片记录被恢复。

**原因**：数据库中遗留有旧的 `ElectrodeSheet` 记录（`A1_sheet100` 等），启动时被 PLR 反序列化进来，导致同一 UUID 出现在多个 Magazine 洞位中，触发 `发现重复的uuid` 错误。此修复从源头截断旧数据，经过一次完整的"启动 → 资源树写回"后，数据库旧极片记录也会被干净覆盖。

---

### 3. `unilabos/resources/battery/bottle_carriers.py`

**改动**：删除 `YIHUA_Electrolyte_12VialCarrier` 末尾的 12 瓶填充循环及对应 `import`。

**原因**：`bottle_rack_6x2` 和 `bottle_rack_6x2_2` 应初始化为空载架，瓶子由 Bioyond 侧实际转运后再填入。原来初始化时直接塞满 `YB_pei_ye_xiao_Bottle`，反序列化时产生重复 assign。

---

### 4. `unilabos/resources/bioyond/decks.py`

**改动**：
- 将 `BIOYOND_YB_Deck` 重命名为 `BioyondElectrolyteDeck`，保留 `BIOYOND_YB_Deck` 作为向后兼容别名。
- 工厂函数 `YB_Deck()` 重命名为 `bioyond_electrolyte_deck()`，保留 `YB_Deck` 作为别名。
- `BIOYOND_PolymerReactionStation_Deck`、`BIOYOND_PolymerPreparationStation_Deck`、`BioyondElectrolyteDeck` 三个 Deck 类：
  - 移除 `__init__` 中的 `setup: bool = False` 参数及 `if setup: self.setup()` 调用。
  - 删除临时 `deserialize` 补丁（该补丁是为了强制 `setup=False`，根本原因消除后不再需要）。

**原因**：`setup` 参数导致 PLR 反序列化时先通过 `__init__` 创建所有子资源，再从 JSON `children` 字段再次 assign，产生 `already assigned to deck` 错误。正确模式：`__init__` 只初始化自身几何，`setup()` 由工厂函数调用，反序列化由 PLR 从 DB 数据重建子资源。

---

### 5. `unilabos/devices/workstation/coin_cell_assembly/YB_YH_materials.py`

**改动**：
- `CoincellDeck` 重命名为 `YihuaCoinCellDeck`，保留 `CoincellDeck` 作为向后兼容别名。
- 工厂函数 `YH_Deck()` 重命名为 `yihua_coin_cell_deck()`，保留 `YH_Deck` 作为别名。
- 移除 `YihuaCoinCellDeck.__init__` 中的 `setup: bool = False` 参数及调用，删除 `deserialize` 补丁（原因同 decks.py）。
- `MaterialPlate.__init__` 移除 `fill` 参数和 `fill=True` 分支，新增类方法 `MaterialPlate.create_with_holes()` 作为"带洞位"的工厂方法，`setup()` 改为调用该工厂方法。
- `YihuaCoinCellDeck.setup()` 末尾新增 `electrolyte_buffer`（`ResourceStack`）接驳槽，用于接收来自 Bioyond 侧的分液瓶板，命名与 `bioyond_cell_workstation.py` 中 `sites=["electrolyte_buffer"]` 一致。

---

### 6. `unilabos/resources/resource_tracker.py`

**改动 1**：`to_plr_resources` 中，`load_all_state` 调用前预填 `Container` 类资源缺失的键：

```python
state.setdefault("liquid_history", [])
state.setdefault("pending_liquids", {})
```

**原因**：新版 PLR 要求 `Container` 状态中必须包含这两个键，旧数据库记录缺失时 `load_all_state` 会抛出 `KeyError`。

**改动 2**：`_validate_tree` 中，遇到重复 UUID 时改为自动重新分配新 UUID 并打 `WARNING`，不再直接抛异常崩溃。

**原因**：旧数据库中存在多个同名同 UUID 的极片对象（历史脏数据），严格校验会导致节点无法启动。改为 WARNING + 自动修复，确保启动成功，下次资源树写回后脏数据自然清除。

---

### 7. `unilabos/resources/itemized_carrier.py`

**改动**：将原来的 `idx is None` 兜底补丁（静默调用 `super().assign_child_resource`，不更新槽位追踪）替换为两段式逻辑：

1. **XY 近似匹配**（容差 2mm）：精确三维坐标匹配失败时，仅对比 XY 二维坐标，找到最近槽位后用槽位的正确坐标（含 Z）完成 assign，并打 `WARNING`。
2. **XY 也失败才抛异常**：给出详细的槽位列表和传入坐标，便于问题排查。

**原因**：数据库中存储的资源坐标 Z=0，而 `warehouse_factory` 定义的槽位 Z=dz（如 10mm）。精确匹配永远失败，原补丁静默兜底掩盖了这一问题。近似匹配修复了 Z 偏移，同时保留了真正异常时的报错能力。

---

### 8. `unilabos/devices/workstation/bioyond_studio/bioyond_cell/bioyond_cell_workstation.py`

**改动 1**：更新导入：`BIOYOND_YB_Deck` → `BioyondElectrolyteDeck, bioyond_electrolyte_deck`。

**改动 2**：`__main__` 入口处改为调用 `bioyond_electrolyte_deck(name="YB_Deck")`。

**改动 3**：新增 `_get_resource_from_device(device_id, resource_name)` 方法，用于从目标设备的资源树中动态查找 PLR 资源对象（带降级回退逻辑）。

**改动 4**：跨站转运逻辑中，将原来"创建 `size=1,1,1` 的虚拟 `ResourcePLR` + 硬编码 UUID"的方式，改为通过 `_get_resource_from_device` 从目标设备获取真实的 `electrolyte_buffer` 资源对象。

**原因**：原代码使用硬编码 UUID 的虚拟资源作为转运目标，该对象在 YihuaCoinCellDeck 的资源树中不存在，转移后资源树状态混乱。

---

### 9. `unilabos/devices/workstation/coin_cell_assembly/coin_cell_assembly.py`

**改动 1**：更新导入：`CoincellDeck` → `YihuaCoinCellDeck, yihua_coin_cell_deck`，`__main__` 入口改为调用 `yihua_coin_cell_deck()`。

**改动 2**：新增 10 个 `@property`，实现对依华扣电工站 Modbus 寄存器的直读：

| 属性名 | 寄存器地址 | 说明 |
|---|---|---|
| `data_10mm_positive_plate_remaining` | 520 | 10mm正极片余量 |
| `data_12mm_positive_plate_remaining` | 522 | 12mm正极片余量 |
| `data_16mm_positive_plate_remaining` | 524 | 16mm正极片余量 |
| `data_aluminum_foil_remaining` | 526 | 铝箔余量 |
| `data_positive_shell_remaining` | 528 | 正极壳余量 |
| `data_flat_washer_remaining` | 530 | 平垫余量 |
| `data_negative_shell_remaining` | 532 | 负极壳余量 |
| `data_spring_washer_remaining` | 534 | 弹垫余量 |
| `data_finished_battery_remaining_capacity` | 536 | 成品电池余量 |
| `data_finished_battery_ng_remaining_capacity` | 538 | 成品电池NG槽余量 |

**原因**：`coin_cell_workstation.yaml` 的 `status_types` 中定义了这 10 个属性，但代码中从未实现，导致每次前端轮询时均报 `AttributeError`。

---

## 二、配置与注册表更新

### 10. `yibin_electrolyte_config.json`
- `BIOYOND_YB_Deck` → `BioyondElectrolyteDeck`（class、type、_resource_type 三处）
- `CoincellDeck` → `YihuaCoinCellDeck`（class、type、_resource_type 三处）
- 移除 `"setup": true` 字段

### 11. `yibin_coin_cell_only_config.json`
- `CoincellDeck` → `YihuaCoinCellDeck`
- 移除 `"setup": true`

### 12. `yibin_electrolyte_only_config.json`
- `BIOYOND_YB_Deck` → `BioyondElectrolyteDeck`
- 移除 `"setup": true`

### 13. `unilabos/registry/resources/bioyond/deck.yaml`
- `BIOYOND_YB_Deck` → `BioyondElectrolyteDeck`，工厂函数路径更新为 `bioyond_electrolyte_deck`
- `CoincellDeck` → `YihuaCoinCellDeck`，工厂函数路径更新为 `yihua_coin_cell_deck`

---

## 三、独立 Bug 修复

### 14. `unilabos/devices/workstation/coin_cell_assembly/coin_cell_assembly_b.csv`

**改动**：10 条余量寄存器记录的 `DataType` 列从 `REAL` 改为 `FLOAT32`。

**原因**：`REAL` 是 IEC 61131-3 PLC 工程师惯用名称，但 pymodbus 的 `DATATYPE` 枚举只有 `FLOAT32`，`DataType['REAL']` 查表时抛 `KeyError: 'REAL'`，导致 `CoinCellAssemblyWorkstation` 节点启动失败。

---

## 四、运行期新增 Bug 修复（第二轮，2026-03-12 18:12 日志）

### 15. `unilabos/devices/workstation/bioyond_studio/station.py`

**改动**：第 261 行 `self.bioyond_config` → `self.workstation.bioyond_config`。

**原因**：`BioyondResourceSynchronizer.sync_to_external` 内部误用了 `self.bioyond_config`，而该类从未设置此属性（应通过 `self.workstation.bioyond_config` 访问）。触发场景：用户在前端将任意物料拖入仓库时，同步到 Bioyond 必定抛出 `AttributeError: 'BioyondResourceSynchronizer' object has no attribute 'bioyond_config'`。

---

### 16. `unilabos/devices/workstation/bioyond_studio/bioyond_cell/bioyond_cell_workstation.py`

**改动**：`_get_type_id_by_name` 方法新增"直接英文 key 命中"分支：

- **原逻辑**：仅按 `value[0]`（中文名，如 `"5ml分液瓶板"`）遍历比较。
- **新逻辑**：先以 `type_name` 直接查找 `material_type_mappings` 字典 key（英文 model 名，如 `"YB_Vial_5mL_Carrier"`），命中则立即返回 UUID；否则再按中文名兜底遍历。

**原因**：`resource_tree_transfer` 将 `plr_resource.model`（英文 key）作为 `board_type` / `bottle_type` 传给 `create_sample`，后者再调用 `_get_type_id_by_name`。旧版函数只按中文名查，导致英文 key 永远匹配不到 → `ValueError: 未找到板类型 'YB_Vial_5mL_Carrier' 的配置`。新函数兼容两种查找方式，同时保持向后兼容。

---

## 五、运行期新增 Bug 修复（第三轮，2026-03-12 20:30 日志）

### 17. `unilabos/resources/resource_tracker.py`（追加）

**改动**：在 `to_plr_resources` 中，`sub_cls.deserialize` 调用前新增 `_deduplicate_plr_dict(plr_dict)` 预处理函数。

**函数逻辑**：递归遍历整个 `plr_dict` 树，在**全树范围**对 `children` 列表按 `name` 去重——保留首次出现的同名节点，跳过重复项并打 `WARNING`。

**根本原因**：
1. 用户通过前端将 `YB_Vial_5mL_Carrier` 拖入仓库 E01，carrier 及其子 vial（`YB_Vial_5mL_Carrier_vial_A1` 等）被写入数据库。
2. 随后 `sync_from_external`（Bioyond 定期同步）以**新 UUID** 重新创建同名 carrier 并赋给同一槽位，PLR 内存树中的旧 carrier 被替换，但**数据库旧记录未被清除**。
3. 下次重启时，数据库同一 `WareHouse` 下存在两条同名 `BottleCarrier`（不同 UUID），`node_to_plr_dict` 将二者都放入 `children` 列表，PLR 反序列化第二个 carrier 时子 vial 命名冲突，抛出 `ValueError: Resource with name 'YB_Vial_5mL_Carrier_vial_A1' already exists in the tree.`，整个 deck 无法加载，系统启动失败。

**连锁错误（随根因修复自动消除）**：
- `TypeError: Deck.__init__() got an unexpected keyword argument 'data'` — deck 加载失败后 `driver_creator.py` 触发降级路径，参数类型错误
- `AttributeError: 'ResourceDictInstance' object has no attribute 'copy'` — 另一条降级路径失败
- `ValueError: Deck 配置不能为空` — 所有 deck 创建路径失败，`deck=None` 传入工作站

---

> **验证状态**：2026-03-12 20:56 日志确认系统正常运行，无新增 ERROR 级错误。

---

## 六、变更文件汇总（最终）

| 文件 | 变更类型 | 轮次 |
|---|---|---|
| `resources/battery/magazine.py` | 重构 + Bug 修复（极片子节点解耦 + 旧数据清理） | 第一轮 |
| `resources/battery/bottle_carriers.py` | 重构（移除初始化时自动填瓶） | 第一轮 |
| `resources/bioyond/decks.py` | 重构 + 重命名（BioyondElectrolyteDeck） | 第一轮 |
| `devices/workstation/coin_cell_assembly/YB_YH_materials.py` | 重构 + 重命名（YihuaCoinCellDeck）+ 新增 electrolyte_buffer 槽位 | 第一轮 |
| `resources/resource_tracker.py` | Bug 修复 × 3（Container 状态键预填 + 重复 UUID 自动修复 + 树级名称去重） | 第一/三轮 |
| `resources/itemized_carrier.py` | Bug 修复（XY 近似坐标匹配，修复 Z 偏移） | 第一轮 |
| `devices/workstation/bioyond_studio/bioyond_cell/bioyond_cell_workstation.py` | 重构 + Bug 修复（跨站转运 + 类型映射双模式查找） | 第一/二轮 |
| `devices/workstation/bioyond_studio/station.py` | Bug 修复（sync_to_external 属性访问路径） | 第二轮 |
| `devices/workstation/coin_cell_assembly/coin_cell_assembly.py` | 新增 10 个 Modbus 余量属性 + 更新导入 | 第一轮 |
| `yibin_electrolyte_config.json` | 配置更新（类名 + 移除 setup） | 第一轮 |
| `yibin_coin_cell_only_config.json` | 配置更新（类名 + 移除 setup） | 第一轮 |
| `yibin_electrolyte_only_config.json` | 配置更新（类名 + 移除 setup） | 第一轮 |
| `registry/resources/bioyond/deck.yaml` | 注册表更新（类名 + 工厂函数路径） | 第一轮 |
| `devices/workstation/coin_cell_assembly/coin_cell_assembly_b.csv` | Bug 修复（REAL → FLOAT32） | 第一轮 |
