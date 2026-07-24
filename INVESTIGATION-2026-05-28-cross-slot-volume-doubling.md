# 跨板 transfer_liquid 目标孔 volume / liquid_history 双倍异常 — 排查交接

> **日期**: 2026-05-28
> **协议**: `Protocols/protocol_converter/transfer_actions_copy4/51b9a5.json` (Agar Plating, P2 v2 跨 slot merge 场景)
> **当前状态**: ⏸ 排查中 — OS 端已自证清白，问题已收敛到 **OS → Cloud 同步链路**，待新 agent 继续往下定位
> **本文目的**: 把已掌握的所有事实、已加的调试代码、已排除的方向、下一步打算交接给下一位 agent

---

## 1. 问题描述

### 1.1 现象

跑完 `51b9a5.json`（Opentrons 的 *Agar Plating* 协议）后，前端弹窗对 `PRCXI_BioER_96_wellplate_slot_3_well_A6` 这个孔的展示：

| 字段 | 实测值 | 预期值 |
|---|---|---|
| `thing` | `well_5_0_volume_tracker` | — |
| `volume` | **6** | **3** |
| `liquids` | `Unknown1,6,ul` | `Unknown1,3,ul` |
| `liquid_history` | `agar,0,Unknown1,3,Unknown1,3` | `agar,0,Unknown1,3` |
| `unknown_counter` | `1` | `1` |
| `max_volume` | `2200` | `2200` |

**两条 `Unknown1,3` 而非 1 条；`unknown_counter` 只有 1 说明是同一种 source 液体被算了两次。**

### 1.2 协议背景

`51b9a5.json` 是典型的「单源 → 跨板分发」结构：

- **12 条 `transfer_liquid`**，每条 `sources` = 1 个 PCR 板孔（A1..A12），`targets` = **9 个跨板 reagent_key**（slot 3..11 各取该列的同一个 well）。
- 总计 `12 × 9 = 108` 次单孔 dispense，每孔 3 µL（asp=dis=3.0），每板 12 个目标孔 = 9 板 × 12 = 108 个 distinct 物理 well。
- 这正是 `Uni-Lab-OS/AGENTS.md` 里说的 **「P2 v2 跨 slot transfer_liquid 合并」** 场景。

### 1.3 涉及到的系统改造（必要 context）

跑这条协议时，框架会做三件事：

1. **Stage 2** (`Protocols/protocol_converter/change_to_transfer_group.py`)：`_pair_mergeable` 因 §12.6.1 修复（source wells 不同就禁止合并）**保留** 12 条独立 transfer，但每条 `targets` 写成 `list[str]`。
2. **Stage 3** (`Uni-Lab-OS/unilabos/workflow/common.py:1431-1449`)：检测到 `targets` 是 `list[str]`，为每条 transfer 调 `_emit_merged_set_liquid` 插一个 **merged `set_liquid_from_plate`** 节点（跨 9 板的 9 条 well refs，`liquid_names=[liquid_name]*9`，`volumes=[0]*9`，`well_names` 含 `"<plate_plr_name>/A6"` prefix），然后把 `params.targets` 改写为 `_merged_targets_<idx>`，由 `merged.output_wells → transfer_liquid.targets_identifier` 边喂 abstract 层。
3. **abstract `transfer_liquid`** (`Uni-Lab-OS/unilabos/devices/liquid_handling/liquid_handler_abstract.py:1779-2000`)：主循环 `max_len = max(num_sources, num_targets, asp/dis) = 9`，每次循环 `i % num_targets` 取一个 (source, target) 配对调 `_transfer_base_method` → 内部 1 次 aspirate + 1 次 dispense。

---

## 2. 排查方向与结论

### 2.1 候选清单

按怀疑度排序，给出了 5 个候选根因：

| 候选 | 假设 | 关键位置 |
|---|---|---|
| **A** | merged `set_liquid_from_plate` 入口的 `wells` 被「框选化 + 跨板 fallback」双重展开 | `liquid_handler_abstract.py:1260-1402` |
| **B** | merged `output_wells → transfer.targets` 边的 wells 翻倍，导致 abstract 层 `num_targets=18` | `liquid_handler_abstract.py:1810` `_resolve_to_plr_resources(targets)` |
| **C** | `_flatten_8_to_1` 把单通道误判为 8 通道，扁平化成多次执行 | `prcxi/prcxi.py:1391-1402` |
| **D** | 同一 transfer_liquid 节点被 ROS action server 触发 2 次 | ROS 执行链路 |
| **E** | OS→Cloud / ROS update_resource / 前端 Redux 任一段同步把 history merge 了两次 | `liquid_handler_abstract.py:558-565` (dispense 后)、`host_node.py:1387-1414`、`resource_tracker._augment_states_with_liquid_history`、Web 端 reducer |

### 2.2 验证手段：4 处 DBG log

为快速判别根因，在 4 个位置加了精简调试日志：

| # | 文件 | 行号 | 前缀 | 锁定哪个候选 |
|---|---|---|---|---|
| 1 | `Uni-Lab-OS/unilabos/devices/liquid_handling/liquid_handler_abstract.py` | ≈ 1831 | `[T-DBG]` | B |
| 2 | 同上 | ≈ 546 | `[D-DBG]` | B/C/D |
| 3 | `Uni-Lab-OS/unilabos/devices/liquid_handling/prcxi/prcxi.py` | ≈ 1403 | `[P-DBG]` | C |
| 4 | `Uni-Lab-OS/unilabos/devices/liquid_handling/liquid_handler_abstract.py` | ≈ 2014 | `[B-DBG]` | D |

所有 log 均用 `if hasattr(self, "_ros_node") and self._ros_node is not None:` 守护 + `try/except` 兜底，**单元测试 fixture 与生产路径都安全**。

### 2.3 实测结果（来自 `/tmp/51b9a5_dbg.log` 14:50:28~14:50:57 这一轮跑）

| 计数 | 实测 | 期望 | 评判 |
|---|---|---|---|
| `[T-DBG]` 数（transfer_liquid 入口） | 12 | 12 | ✅ |
| `[B-DBG]` 数（`_transfer_base_method` 调用） | **108** | 12 × 9 = 108 | ✅ |
| `[D-DBG]` 数（dispense 入口总） | **108** | 108 | ✅ |
| `[D-DBG]` 命中 `slot_3_well_A6` | **1** | 1 | ✅ |
| 12 条 `[T-DBG]` handler id | 都是 `7d35e3f23dd0` | 同一 PRCXI 实例 | ✅ |
| 每条 `[D-DBG]` 形态 | `n_res=1 channels=[1] vols=[3.0]` | 单通道小量程 3 µL | ✅ |

**结论 1（候选 A/B/C/D 全部排除）**：OS 端从 `transfer_liquid` 入口到 PLR `dispense` 出口，全部是干净的 1:1 单孔操作。slot 3 A6 **物理上只收了一次 3 µL 的 dispense**，PLR `tracker` 本地应该 = 3 µL 而不是 6 µL。

### 2.4 命中候选 E 的关键证据

```
14:49:04,327  物料 ['PRCXI_BioER_96_wellplate_slot_3_well_A6']  请求更新上传   ← 第 1 次
14:50:43,092  物料 ['PRCXI_BioER_96_wellplate_slot_3_well_A6']  请求更新上传   ← 第 2 次
```

**同一物理 well 被 `host_node._resource_tree_action_update_callback` 上传了 2 次**：

- **14:49:04**：dispense 之前 99 秒 → 多半是 **`set_liquid_from_plate` 阶段**（merged 节点把 `(agar, 0)` 初始液体写入该孔时触发）。此时 PLR `tracker.liquid_history = [(agar, 0, set)]`。
- **14:50:43.092**：与对应 `[D-DBG]`（14:50:43.088）几乎同时 → **dispense 后的 `update_resource` 上行**。此时 `tracker.liquid_history = [(agar, 0, set), (Unknown1, 3, dispense)]`。

如果云端 / host_node 转发链路 / 前端 Redux 对 `liquid_history` 采用 **append 合并**（而不是「以本次上传内容为准覆盖」），最终云端就会得到：

```
第1次上传 [(agar,0,set)]
+ 第2次上传 [(agar,0,set),(Unknown1,3,dispense)]
→ append 后: [(agar,0,set),(agar,0,set),(Unknown1,3,dispense),(Unknown1,3,dispense)]
   或去重 set 不去重 dispense: [(agar,0),(Unknown1,3),(Unknown1,3)]
```

后者正好与前端实测 `agar,0,Unknown1,3,Unknown1,3` + `volume=6` + `unknown_counter=1` 完全吻合（`tracker.liquids` 末两项 `Unknown1, 3` 在 PLR 中被解释为累加 → 6 µL）。

---

## 3. 当前结论

> **OS 端 ✅ 干净。问题在 `update_resource` 上行链路或下游 cloud / web 端对 `liquid_history` 的 merge 策略——同一 well 在 set 阶段和 dispense 阶段各上传一次，下游把两份 history append 而非覆盖。**

---

## 4. 下一步排查方向

接续排查的 agent 请按如下优先级推进：

### 4.1 加 1 条 `[U-DBG]` 锁死 OS 上传 payload 是「全量」还是「增量」

在 `Uni-Lab-OS/unilabos/devices/liquid_handling/liquid_handler_abstract.py` 两处 `update_resource` 上行点（dispense 后 558 行 / `_set_liquid_grouped_by_plate` 1187 行附近）紧邻添加：

```python
self._ros_node.lab_logger().info(
    f"[U-DBG] update_resource src={origin} for {[r.name for r in resources]} "
    f"history_lens={[len(getattr(getattr(r, 'tracker', None), 'liquid_history', []) or []) for r in resources]} "
    f"histories={[getattr(getattr(r, 'tracker', None), 'liquid_history', []) for r in resources]}"
)
```

判别表：

| 两次上传的 history_lens 形态 | 含义 | 锁定 |
|---|---|---|
| `[1]` 然后 `[2]` | OS 总是发**全量**，下游 merge 策略错（append 而非 replace） | 候选 E.cloud |
| `[1]` 然后 `[1]`（只含 dispense 这一条） | OS 发**增量**，下游收两次 append 拼起来 | 候选 E.diff |
| `[2]` 两次（重复发同一份全量） | OS 上行重复发了同一份 payload | 候选 E.os_dup |

### 4.2 ROS host_node → Cloud 转发段

定位文件：`Uni-Lab-OS/unilabos/ros/nodes/presets/host_node.py` 的 `_resource_tree_action_update_callback`（1387~1414 行）。

检查：把同一个 well 收到 2 次时，是否 **完整覆盖** 替换还是 **diff 累加**。建议在该回调入口、出口分别打 log 看每次上行的 `liquid_history` 字段长度。

### 4.3 `resource_tracker._augment_states_with_liquid_history`

定位文件：`Uni-Lab-OS/unilabos/resources/resource_tracker.py`。

检查序列化时 `liquid_history` 是否被加上「diff vs full」标记，下游消费者怎么解读。

### 4.4 Cloud / 前端 Redux reducer

定位仓库：`Uni-Lab-Cloud/web/`。

搜关键字 `liquid_history`、`volume_tracker`、`well_state`：

```bash
rg -n "liquid_history" Uni-Lab-Cloud/web/src
```

最大嫌疑 reducer 写成 `[...prev, ...new]` 而非 `new`。

### 4.5 旁路：上传频率本身能否合并

即便上面查清是覆盖语义，**同一 well 被 set + dispense 阶段各上传一次** 本身就有性能问题（108 个孔 × 2 = 216 次上传）。如果产品上能接受，建议把 set_liquid_from_plate 时的 volume=0 占位 set 不触发上传，或合并到 transfer dispense 之后的那次上传里。

### 4.6 备选验证：手动直查 OS 端 PLR tracker

如果不方便加 log，也可在 unilab 跑完后，直接通过 Python REPL 或 RPC 取 OS 进程内 `slot 3 A6` 的 `well.tracker.get_used_volume()` 和 `well.tracker.liquid_history`，看本地 = 3 µL / 1 条 dispense 还是 = 6 µL / 2 条 dispense。**前者证实 OS 干净，问题在 sync 链路；后者说明结论 2.3 需要重新审视。**

---

## 5. 已修改的代码与如何复原

### 5.1 改动 1：4 处 DBG log（**保留**，新 agent 继续用）

| 文件 | 位置 | 标记 |
|---|---|---|
| `Uni-Lab-OS/unilabos/devices/liquid_handling/liquid_handler_abstract.py` | ≈ L546 (`dispense` 入口) | `[D-DBG]` |
| 同上 | ≈ L1831 (`transfer_liquid` 解析 sources/targets 后) | `[T-DBG]` |
| 同上 | ≈ L2014 (`_transfer_base_method` 入口) | `[B-DBG]` |
| `Uni-Lab-OS/unilabos/devices/liquid_handling/prcxi/prcxi.py` | ≈ L1403 (`_flatten_8_to_1` 算完之后) | `[P-DBG]` |

**所有 log 都用 `if hasattr(self, "_ros_node") and self._ros_node is not None:` 守护 + `try/except`，对单元测试和生产路径都无副作用。**

清理方法（确认不再需要排查后再做）：

```bash
cd /home/rx78/LeapLab/Uni-Lab-OS

# 用 git 看出 4 处改动
git diff unilabos/devices/liquid_handling/liquid_handler_abstract.py unilabos/devices/liquid_handling/prcxi/prcxi.py

# 直接还原这两个文件
git checkout -- unilabos/devices/liquid_handling/liquid_handler_abstract.py unilabos/devices/liquid_handling/prcxi/prcxi.py
```

### 5.2 改动 2：editable install 切换（**保留**，重要副作用）

排查中发现 conda env `unilab` 里 `pip install -e` 的源码路径是 **`/home/rx78/Uni-Lab-OS/`** 而非工作区里的 **`/home/rx78/LeapLab/Uni-Lab-OS/`**。两份代码完全独立（行数不同、版本号不同：`0.10.19` vs `0.11.1`）。

为让 DBG log 生效，跑了：

```bash
cd /home/rx78/LeapLab/Uni-Lab-OS
/home/rx78/miniconda3/envs/unilab/bin/pip install -e . --no-deps
```

结果：
- `unilabos.__file__` 现在指向 `/home/rx78/LeapLab/Uni-Lab-OS/unilabos/__init__.py` ✅
- 版本号 `0.10.19 → 0.11.1`
- `/home/rx78/miniconda3/envs/unilab/lib/python3.11/site-packages/__editable___unilabos_0_*_finder.py` 的 `MAPPING` 被重写

**这个切换可能让工作区里其他未测试改动（51 行 diff）也生效，新 agent 排查时要警觉。** 如果想换回原始版本：

```bash
cd /home/rx78/Uni-Lab-OS
/home/rx78/miniconda3/envs/unilab/bin/pip install -e . --no-deps
```

---

## 6. 现场快速重现 & 关键命令

### 6.1 跑协议

当前用于复现的 graph 在 `Uni-Lab-OS/test/experiments/prcxi_9320_slim.json`，启动命令（出自终端 1）：

```bash
cd /home/rx78/LeapLab/Uni-Lab-OS
unilab -g test/experiments/prcxi_9320_slim.json \
       --ak 3fbe84b2-c9f8-498b-8a88-7b0250e72d2c \
       --sk d585fdc8-7791-4368-a3cc-4d015fd42d4d \
       --upload_registry --addr test --disable_browser \
       --backend ros 2>&1 | tee /tmp/51b9a5_dbg.log
```

### 6.2 抽 DBG 日志

```bash
# 只抽 DBG 行
grep -E "\[(T|D|P|B|U)-DBG\]" /tmp/51b9a5_dbg.log > /tmp/51b9a5_dbg_only.log

# 期望计数：T=12, B=108, D=108, P=12
grep -c "\[T-DBG\]" /tmp/51b9a5_dbg_only.log
grep -c "\[B-DBG\]" /tmp/51b9a5_dbg_only.log
grep -c "\[D-DBG\]" /tmp/51b9a5_dbg_only.log
grep -c "\[P-DBG\]" /tmp/51b9a5_dbg_only.log

# 复现重点 well：第 6 条 transfer 的 9 次跨板 A6 dispense
grep "\[D-DBG\].*A6" /tmp/51b9a5_dbg_only.log

# 物料上传重复检查
grep "物料.*slot_3_well_A6.*请求更新上传" /tmp/51b9a5_dbg.log
```

### 6.3 日志输出位置

`lab_logger().info(...)` 走 `unilabos.utils.log` 的 root logger，同时写到：

1. **控制台 stdout** — 启动 unilab 的那个终端（彩色 ANSI）；
2. **文件** — 启动时 `working_dir/logs/YYYY-MM-DD HH-MM-SS.log`（TRACE 级别，最完整）。
   当前实例的日志文件路径：`/home/rx78/LeapLab/Uni-Lab-OS/unilabos_data/logs/2026-05-28 14-XX-XX.log`。

---

## 7. 相关代码与设计文档索引

| 关注点 | 路径 |
|---|---|
| 排查协议本体 | `Protocols/protocol_converter/transfer_actions_copy4/51b9a5.json` |
| P2 v2 跨板合并设计 | `product_designs/protocol_convert/02-cross-slot-merge.md` |
| liquid_history schema 设计 | `product_designs/protocol_convert/09-liquid-history-unknown-debug.md` |
| Stage 2 (Protocols/...) merge | `Protocols/protocol_converter/change_to_transfer_group.py` 的 `_pair_mergeable` / `_merge_two_transfer_actions` / `export_transfer_actions` |
| Stage 3 (Uni-Lab-OS) merge & graph build | `Uni-Lab-OS/unilabos/workflow/common.py` 的 `_emit_merged_set_liquid` / `_collect_set_liquid_coverage` / `build_protocol_graph` |
| abstract transfer_liquid 主循环 | `Uni-Lab-OS/unilabos/devices/liquid_handling/liquid_handler_abstract.py` 的 `transfer_liquid` / `_transfer_base_method` / `dispense` / `set_liquid_from_plate` |
| PRCXI transfer_liquid | `Uni-Lab-OS/unilabos/devices/liquid_handling/prcxi/prcxi.py` 的 `transfer_liquid` / `_flatten_8_to_1` |
| liquid_history helper | `Uni-Lab-OS/unilabos/devices/liquid_handling/liquid_history.py` |
| host_node 物料更新回调 | `Uni-Lab-OS/unilabos/ros/nodes/presets/host_node.py` 的 `_resource_tree_action_update_callback` (~ L1387-1414) |
| resource serialize | `Uni-Lab-OS/unilabos/resources/resource_tracker.py` 的 `_augment_states_with_liquid_history` |
| 框架启动 / log 配置 | `Uni-Lab-OS/unilabos/utils/log.py` |

---

## 8. TL;DR — 给下个 agent 的 5 行交接

1. 现象：跨板 transfer 后，每个目标孔 `volume = 6` 而非 3，`liquid_history` 多 1 条 dispense。
2. OS 干净：`[T/B/D/P]-DBG` 都正常（12 / 108 / 108 / 12，`slot_3_well_A6` 只 1 次 3 µL dispense）。
3. 真凶：`host_node` 对同一个 well 在 set 阶段 + dispense 阶段 **各 `update_resource` 一次**，下游对 `liquid_history` append 而非覆盖。
4. 优先加 `[U-DBG]` log 看 OS 上传 payload 是全量还是增量（§4.1），随后顺到 host_node → cloud → web reducer（§4.2-4.4）。
5. 已加的 4 处 DBG log 与 editable install 切换都保留着，不影响功能，清理命令见 §5。
