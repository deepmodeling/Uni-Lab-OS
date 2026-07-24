# PLR 与 Uni-Lab 双写 `liquid_history` —— 根因 & B1 修复

> **日期**: 2026-05-28
> **关联排查**: `INVESTIGATION-2026-05-28-cross-slot-volume-doubling.md`
> **状态**: ✅ 根因锁定 / 🔧 B1 修复落地中
> **协议**: `Protocols/protocol_converter/transfer_actions_copy4/51b9a5.json`（Agar Plating，跨 9 板 12 列分发）

---

## 1. 现象（已与上一份排查文档同步）

跑 51b9a5 后，前端展示 `PRCXI_BioER_96_wellplate_slot_3_well_A6` 这个孔：

| 字段 | 实测 | 预期 |
|---|---|---|
| `volume` | **6** µL | 3 µL |
| `liquids` | `Unknown1,6,ul` | `Unknown1,3,ul` |
| `liquid_history` | `agar,0,Unknown1,3,Unknown1,3` | `agar,0,Unknown1,3` |
| `unknown_counter` | 1 | 1 |

OS 端从 `transfer_liquid` 入口到 PLR `dispense` 出口都是干净的 **1 次** 3 µL 操作（[T/B/D/P]-DBG 计数 12/108/108/12，slot_3 A6 只命中 1 次 dispense），但本地 `well.tracker.liquid_history` 已经被写成了 3 条。

---

## 2. 根因（[U2-DBG] 锁定）

**`pylabrobot.resources.volume_tracker.ContainerVolumeTracker` 自身就有 `liquid_history` 属性，并在以下 PLR 内置入口里自动 append：**

| PLR 入口 | append 形式 | Uni-Lab 哪条路径会触发 |
|---|---|---|
| `add_liquid(name, vol, unit)` | `(name, vol, unit)` 三元组 | `dispense` → `super().dispense()` |
| `remove_liquid(vol)` | `(None, -vol, "ul")` 三元组（无液体名） | `aspirate` → `super().aspirate()` |
| `liquids.setter`（`set_liquids` 触发，`abs(vol) > 1e-9` 时才写） | `(name, vol, unit)` 三元组 | `set_liquid` → `well.set_liquids(...)` |

证据链：

```text
PLR volume_tracker.py L286-292:
  def add_liquid(self, liquid, volume, unit="ul"):
      ...
      name = self._get_liquid_name(actual_liquid)
      self.liquid_history.append((name, actual_volume, self._normalize_unit(unit)))
```

但 Uni-Lab 的 P9 设计（`product_designs/protocol_convert/09-liquid-history-unknown-debug.md`）当时**误以为** `liquid_history` 是 Uni-Lab 自己挂在 PLR tracker 上的扩展属性（`resource_tracker._augment_states_with_liquid_history` 注释 §6.3 写明此假设），所以又在 `liquid_handler_abstract.py` 的 dispense / aspirate / set_liquid 三处调 `_append_liquid_history` 写了一遍 → **双写**。

PLR 写三元组 `(name, vol, "ul")`，Uni-Lab `append_liquid_history` 内部强制归一为二元组 `(name, vol)` 再 append 自己的二元组——这正是 [U2-DBG] 抓到的指纹（最末条带 `'ul'` 单位 = PLR 写的）：

```text
[U-DBG] origin=set_liquid:
  history = [('agar', 0.0)]                                                    长度 1
  ← Uni-Lab 写（vol=0，PLR setter 跳过；Uni-Lab 补一条占位）

[U2-DBG] dispense pre_append（在 _append_liquid_history 调用前）:
  history = [('agar', 0.0), ('Unknown1', 3.0, 'ul')]                           长度 2
                                            ^^^^ ← PLR add_liquid 已经写了

[U-DBG] origin=dispense（在 _append_liquid_history 调用后 / update_resource 上行前）:
  history = [('agar', 0.0), ('Unknown1', 3.0), ('Unknown1', 3.0)]              长度 3
                                               ^^^^^^^^^^^^^^^^^^^ ← Uni-Lab 重复写
```

数学严格对得上前端展示：volume = `sum(正项) = 0 + 3 + 3 = 6 µL`；liquids 累加同名 → `Unknown1, 6, ul`；unknown_counter 仍为 1（同名液体只计一种）。**双写 dispense 是唯一根因。**

为何之前的 `INVESTIGATION-2026-05-28-cross-slot-volume-doubling.md` 误判 OS 干净：那份文档只检查了 [D-DBG] 的 dispense 调用次数（确实只调 1 次），但没注意到**单次 dispense 内部 PLR + Uni-Lab 各写一条**——计数对，但每次都是 2 条。

---

## 3. 修复方案 —— B1：让 PLR 当 `liquid_history` 单一真相源

### 改动 1：`liquid_handler_abstract.py` L570（dispense）— 删除 `_append_liquid_history`

PLR `add_liquid(name, vol, "ul")` 已经写了带液体身份的 entry，Uni-Lab 不再重复。

### 改动 2：`liquid_handler_abstract.py` L407（aspirate）— 从 append 改为覆盖 PLR 末条

PLR `remove_liquid()` 写的是 `(None, -vol, "ul")`——丢失液体身份。Uni-Lab 用 P9 已有的 `liquid_names_before_aspirate[i]` 预读名字，把 PLR 那条 `None` 修补成 `(name_before, -vol, "ul")`，**只动末条不再 append**。

### 改动 3：`liquid_handler_abstract.py` L1107（set_liquid）— `set_liquids` 前后比较 len，避免与 PLR 重复

```text
len_before = len(history)
well.set_liquids([(name, vol)])
len_after = len(history)

if len_after == len_before:           # vol=0 → PLR setter 跳过 → Uni-Lab 补占位
    _append_liquid_history(well, name, vol, "set")
else:                                  # vol>0 → PLR 已经写了 → 不重复
    pass
```

支持 Stage 3 跨板 merged `set_liquid_from_plate` 节点的"vol=0 播种"语义不退化。

### 兼容性下游

| 下游消费点 | 原行为 | B1 后 |
|---|---|---|
| `liquid_history.py:normalize_liquid_history` | 已支持二元组 / 三元组 / dict / str → 二元组 | ✅ 不变 |
| `resource_tracker._augment_states_with_liquid_history` | `list(history)` 浅拷贝 | ✅ 完整保留三元组 |
| `_resolve_to_plr_resources` 远端→本地 history merge（L1041-1076） | `local 空 + remote 非空 → 覆盖` | ✅ 不变 |
| `liquid_handler_abstract.py:append_liquid_history` 内部归一化 | 强制归一为二元组 | ⚠️ 现在仅 set_liquid 占位路径调，影响面极小；PLR 三元组只走 Uni-Lab append 函数时被退化为二元组——B1 已不在 dispense / aspirate 走此函数 |
| 前端 `liquids` 字段（截图里 `Unknown1,6,ul`） | 已用三元组格式 | ✅ 不变 |

### 文档同步

`product_designs/protocol_convert/09-liquid-history-unknown-debug.md` 需要在后续 PR 同步修正 §6.3 「liquid_history 是 Uni-Lab 扩展属性」的错误前提：实际为 **PLR 原生属性**，Uni-Lab 仅做 aspirate 液体名后处理 + vol=0 占位补写。

---

## 4. 验证（修复落地后跑）

### 4.1 单元测试

```bash
cd /home/rx78/LeapLab/Uni-Lab-OS

pytest tests/devices/liquid_handling/test_liquid_history.py            # P9 helper（应全绿，未改 helper）
pytest tests/devices/liquid_handling/test_set_liquid_from_plate_cross_plate.py   # 跨板 set_liquid mock
pytest tests/devices/liquid_handling/test_tip_reuse_by_liquid_name.py  # tip 复用（依赖 liquids[-1]）
pytest tests/devices/liquid_handling/test_transfer_liquid.py           # transfer 主链路
pytest tests/resources/test_resource_tracker_history.py                # 序列化 / 升级链路
```

预期：全绿。如果 mock tracker 没模拟 PLR `add_liquid` / `set_liquids` 自动写 history 的行为，可能要 mock 端补一行；但 P9 helper 单测里 mock 已经实现了 `liquid_history.append`（见 `test_set_liquid_from_plate_cross_plate.py:61`），不需要改。

### 4.2 端到端：51b9a5 协议复现

```bash
cd /home/rx78/LeapLab/Uni-Lab-OS
unilab -g test/experiments/prcxi_9320_slim.json \
       --ak 3fbe84b2-c9f8-498b-8a88-7b0250e72d2c \
       --sk d585fdc8-7791-4368-a3cc-4d015fd42d4d \
       --upload_registry --addr test --disable_browser \
       --backend ros 2>&1 | tee /tmp/51b9a5_fix.log
```

跑完后检查：

```bash
# slot_3 A6 dispense 后的 history（B1 修复后期望长度 = 2）
grep "\[U-DBG\] origin=dispense.*slot_3_well_A6" /tmp/51b9a5_fix.log
# 期望: history_lens=[2] histories=[[('agar', 0.0), ('Unknown1', 3.0, 'ul')]]

# [U2-DBG] 此时 history_pre_append 应仍是 2（PLR 写完）；不再被 _append_liquid_history 增长
grep "\[U2-DBG\].*slot_3_well_A6" /tmp/51b9a5_fix.log
# 期望: history_pre_append_len=2  history=[..., ('Unknown1', 3.0, 'ul')]
```

### 4.3 前端展示

打开前端 → 选 `PRCXI_BioER_96_wellplate_slot_3_well_A6` 弹窗 → 期望：

| 字段 | 期望值 |
|---|---|
| `volume` | **3** |
| `liquids` | `Unknown1,3,ul` |
| `liquid_history` | `agar,0,Unknown1,3` |
| `unknown_counter` | 1 |
| `max_volume` | 2200 |

### 4.4 旁路抽样（不止 slot_3 A6）

至少抽 3-5 个跨板 reagent_key 的目标孔 (e.g. slot_5 A1, slot_9 A12, slot_11 A6)，确认 volume = 3 而非 6。

---

## 5. 修复完后的 DBG log 清理

修复 + 全部验证通过后，按 `INVESTIGATION-2026-05-28-cross-slot-volume-doubling.md` §5.1 清理：

```bash
cd /home/rx78/LeapLab/Uni-Lab-OS
git diff unilabos/devices/liquid_handling/liquid_handler_abstract.py \
         unilabos/devices/liquid_handling/prcxi/prcxi.py
```

`git checkout --` 清理 6 处 DBG log（[T/D/B/P]-DBG / [U-DBG] / [U2-DBG]），保留 B1 的 3 处真实修复。

---

## 6. TL;DR

1. **现象**：跨板 transfer 后每个目标孔 volume 翻倍 (3 → 6)，liquid_history 多 1 条 dispense。
2. **根因**：PLR 的 `ContainerVolumeTracker.add_liquid / remove_liquid / liquids.setter` 自动写 `liquid_history`（PLR 原生属性，非 Uni-Lab 扩展）。Uni-Lab 误以为是自家扩展属性又写一次 → 双写。
3. **修复（B1）**：让 PLR 当 history 单一真相源；Uni-Lab 仅 (a) aspirate 后修补 PLR 写的 `None` 末条、(b) `set_liquid` 在 PLR 跳过时（vol=0）补占位。
4. **验证**：4 套单测 + 51b9a5 端到端 + 前端弹窗 + 跨板抽样。
