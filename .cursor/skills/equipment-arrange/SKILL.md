---
name: equipment-arrange
description: Arranges Uni-Lab cloud lab equipment and materials in 3D space — download resource tree, fix device size0, grid layout, bbox conflict check when model present. Edits pose only (not config/position). Use when the user mentions 设备排布/台面布局/equipment arrange/空间排布/3D布局/size0/bbox冲突/摆位/deck layout.
---

# Equipment Arrange（设备与物料空间排布）

感知云端实验室资源树，规划并写入 3D 布局。**只改 `pose`**，不改 `config`、不写顶层 `position`。

数据模型真值：`unilabos/resources/resource_tracker.py`（`ResourceDict` / `ResourceDictPosition`）。

---

## 前置条件（缺一不可）

缺任一项 → **立即询问用户并终止**。

### 1. ak / sk → AUTH

```bash
python .cursor/skills/create-device-skill/scripts/gen_auth.py <ak> <sk>
```

### 2. --addr → BASE

| `--addr` | BASE |
|----------|------|
| `test` | `https://leap-lab.test.bohrium.com` |
| `uat` | `https://leap-lab.uat.bohrium.com` |
| `local` | `http://127.0.0.1:48197` |
| 默认 | `https://leap-lab.bohrium.com` |

```bash
BASE="<url>"
AUTH="Authorization: Lab <token>"
```

### 3. lab_uuid（自动，不问用户）

```bash
curl -s -X GET "$BASE/api/v1/edge/lab/info" -H "$AUTH"
```

> Windows 用 `curl.exe`。POST/PUT 加 `Content-Type: application/json`。

---

## 核心规则（必守）

### 写入范围

| 写什么 | 规则 |
|--------|------|
| `pose.*` | ✅ 布局唯一入口 |
| 顶层 `position` | ❌ 不要写 |
| `config.*` | ❌ 不要写 |

### 按节点类型

| `pose` 字段 | 设备 `type=device` | 物料 |
|-------------|-------------------|------|
| `position` / `position_3d` | ✅ | ✅（depth≤3） |
| `size` / `scale` / `rotation` / `layout` / `cross_section_type` | ✅ | ❌ **禁止** |

- **物料**：只改 pos；尺寸来自注册表/实测，**never 改 size/rotate/scale**。
- **设备**：pos + size/scale/rotation 均可改；`pose.size` 任一边 ≤0 → 3D 看不见，须补立方体（如 200³ mm）。

### 按层级（pos 可改范围）

根 depth=0，沿 `parent_uuid` 向上累加：

| depth | 示例 | 改 pos |
|-------|------|--------|
| 0 | 根设备 / 根上物料 | ✅ |
| 1 | 子设备、deck | ✅ |
| 2 | 二级子设备、第一层物料 | ✅ |
| 3 | 设备下物料（最深） | ✅ |
| ≥4 | well、tip_spot 等 | ❌ 不要动 |

---

## API 速查

| # | 用途 | 方法 |
|---|------|------|
| 1 | 列出整树 | `GET /api/v1/lab/material/download/{lab_uuid}` |
| 2 | 单节点 | `GET /api/v1/lab/material?id=<id>&with_children=false` |
| 3 | **改布局** | `PUT /api/v1/lab/material` body `{"nodes":[...]}` |
| 4 | 创建节点 | `POST /api/v1/edge/material/node` |
| 5 | 删除 | `DELETE /api/v1/lab/resource/batch_delete/?id=<id>` |
| 6 | 台面废弃 | `POST /api/v1/edge/material/bench/discard` body `{"uuids":[...]}`（1~100，无 lab_uuid） |

**#3 read-modify-write**：GET 完整节点 → 只改允许的 `pose` 子字段 → 整节点回传。按 `uuid` upsert，不影响未列出的节点。

云端 download 中 3D 位置 key 为 **`position_3d`（下划线）**。

---

## 排布工作流

```
Task Progress:
- [ ] Step 1: GET /edge/lab/info → lab_uuid
- [ ] Step 2: GET #1 下载整树 → nodes[]
- [ ] Step 3: 建 parent_uuid 层级树，算 depth
- [ ] Step 4: 跑校验（见下）→ 输出 check 表
- [ ] Step 5: 修 size0（仅设备 pose.size）→ 规划 pos 网格/避让
- [ ] Step 6: GET 单节点 → 改 pose → PUT #3（批量可一次 nodes[]）
- [ ] Step 7: 重新下载，复跑校验直至通过
```

**根设备网格建议**：立方体边长 `CUBE=200`，间距 `STEP=CUBE+GAP`（GAP=50），按 name 排序铺 3×N 网格，world 坐标无重叠。

**子设备**：pos 可改；size0 只补 `pose.size`，pos 保持相对父节点。

**物料**：仅在 depth≤3 且用户明确要求时改 pos；默认排布任务**只动设备**。

---

## 排布校验（必做）

排布前、PUT 后**必须**输出 check 表。可执行：

```bash
python .cursor/skills/equipment-arrange/scripts/validate_layout.py tmp_material_tree.json
```

### 1. 设备 size0

`type=device` 且 `pose.size` 任一边 ≤0 → ⚠️ size0 → 写 `pose.size` 立方体。

### 2. bbox 冲突（有 model 时必做）

`model` 非空时，**不得仅用 pose.size 判碰撞**，须用 **`model.bbox`**：

1. `model.bbox.min/max`（首选）
2. 无 bbox → 回退 `pose.size`，标注 `bbox_fallback`

检测：**同级兄弟**（同 `parent_uuid`）局部 AABB；**根设备** world AABB。

### 3. check 表模板

| 节点 | type | depth | size0 | has_model | footprint | 冲突 |
|------|------|-------|-------|-----------|-----------|------|
| host_node | device | 0 | OK | 否 | pose.size | - |
| PRCXI | device | 1 | 已修复 | 是 | model.bbox | - |

---

## pose 字段速查

| 字段 | 设备 | 物料 |
|------|------|------|
| `position` / `position_3d` | ✅ | ✅ depth≤3 |
| `size` | ✅ | ❌ |
| `scale` / `rotation` / `layout` / `cross_section_type` | ✅ | ❌ |

---

## 渐进加载

1. **SKILL.md**（本文件）— 规则 + 工作流 + API
2. **[reference.md](reference.md)** — 字段语义、depth/bbox 算法、curl 示例
3. **[scripts/validate_layout.py](scripts/validate_layout.py)** — size0 + bbox 校验脚本
