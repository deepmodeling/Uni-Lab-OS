---
name: neware-battery-submit
description: Submit Neware battery test experiments — query channel status, select available channels, generate CSV, submit via notebook API. Use when the user mentions 新威/Neware/电池测试/battery test/提交测试/submit test/通道状态/channel status/扣电.
---

# 新威电池测试提交实验 Skill

## 设备信息

- **device_id**: `neware_battery_test_system`
- **device_name**: `NEWARE_BATTERY_TEST_SYSTEM`
- **Python 源码**: 新威电池测试驱动包中的 `neware_battery_test_system.py`（原 `unilabos/devices/neware_battery_test_system/`，已迁出主仓库）
- **设备类**: `NewareBatteryTestSystem`
- **通道结构**: devid（设备号）→ subdevid（排号）→ chlid（通道号），通道名格式 `设备号-排号-通道号`
- **可放电池的通道状态**: `stop` / `protect` / `finish`（`working` 状态的通道正在使用中）

## 前置条件（缺一不可）

### 1. ak / sk → AUTH

```bash
python -c "import base64; ak='<ak>'; sk='<sk>'; print('Lab ' + base64.b64encode(f'{ak}:{sk}'.encode()).decode())"
```

结果作为 `Authorization: Lab <token>` 使用。

### 2. --addr → BASE URL

| `--addr` 值  | BASE                                |
| ------------ | ----------------------------------- |
| `test`       | `https://leap-lab.test.bohrium.com` |
| `uat`        | `https://leap-lab.uat.bohrium.com`  |
| `local`      | `http://127.0.0.1:48197`            |
| 不传（默认） | `https://leap-lab.bohrium.com`      |

### 3. workflow_uuid

用户提供工作流 UUID，或从工作流 URL 中提取：

```
$BASE/laboratory/<lab_uuid>/workflow/<workflow_uuid>
```

## 请求约定

- 所有请求使用 `curl.exe -s`（Windows 平台必须用 `curl.exe`，非 PowerShell 的 `curl` 别名）
- JSON body 写入 `tmp_body.json` 文件，用 `--data-binary "@tmp_body.json"` 传递（避免 PowerShell 引号转义问题）

## Session State

- `lab_uuid` — 通过 `GET /api/v1/edge/lab/info` 自动获取
- `workflow_uuid` — 用户提供
- `AUTH` — `Authorization: Lab <token>`
- `BASE` — 根据 `--addr` 确定

---

## API Endpoints

### #1 获取实验室信息

```bash
curl.exe -s -X GET "$BASE/api/v1/edge/lab/info" -H "$AUTH"
```

返回 `data.uuid` 为 `lab_uuid`。

### #2 获取资源树（通道状态）

```bash
curl.exe -s -X GET "$BASE/api/v1/lab/material/download/$lab_uuid" -H "$AUTH"
```

### #3 获取工作流模板详情

```bash
curl.exe -s -X GET "$BASE/api/v1/lab/workflow/template/detail/$workflow_uuid" -H "$AUTH"
```

返回 `data.nodes[]` 含节点 uuid、name、param、device_name。

### #4 创建工作流

```bash
curl.exe -s -X POST "$BASE/api/v1/lab/workflow/owner" \
  -H "$AUTH" -H "Content-Type: application/json" \
  --data-binary "@tmp_body.json"
# body: {"name":"<名称>","lab_uuid":"<lab_uuid>","description":"<描述>"}
```

返回 `data.uuid` 为 `workflow_uuid`。

### #5 创建工作流节点

```bash
curl.exe -s -X POST "$BASE/api/v1/edge/workflow/node" \
  -H "$AUTH" -H "Content-Type: application/json" \
  --data-binary "@tmp_body.json"
# body: {"workflow_uuid":"<uuid>","resource_template_name":"neware_battery_test_system","node_template_name":"submit_from_csv"}
```

返回 `data.uuid` 为 `node_uuid`，`data.param` 为节点参数模板。

### #6 更新节点参数

```bash
curl.exe -s -X PATCH "$BASE/api/v1/lab/workflow/node" \
  -H "$AUTH" -H "Content-Type: application/json" \
  --data-binary "@tmp_body.json"
# body: {"workflow_uuid":"<uuid>","uuid":"<node_uuid>","param":{"csv_path":"<path>","output_dir":"<dir>"}}
```

### #7 提交实验（notebook）

```bash
curl.exe -s -X POST "$BASE/api/v1/lab/notebook" \
  -H "$AUTH" -H "Content-Type: application/json" \
  --data-binary "@tmp_body.json"
```

返回 `data.uuid` 为 notebook UUID。

### #8 查询实验状态

```bash
curl.exe -s -X GET "$BASE/api/v1/lab/notebook/status?uuid=<notebook_uuid>" -H "$AUTH"
```

提交后轮询此接口，确认实验执行状态。

---

## 扣电测试提交流程（逐步引导）

以下是完整的扣电测试提交流程。agent 按步骤依次执行，每一步需要用户确认后才能继续。

### 第一步：获取环境信息

1. 生成 AUTH token（从用户提供的 ak/sk）
2. 调用 API #1 获取 `lab_uuid`
3. 调用 API #2 获取资源树，保存为 JSON 文件

### 第二步：解析通道状态

用 Python 脚本递归搜索资源树中所有含 `Channel_Name` + `status` 字段的节点：

```python
def find_channels(obj):
    results = []
    if isinstance(obj, dict):
        if "status" in obj and "Channel_Name" in obj:
            results.append({
                "channel_name": obj.get("Channel_Name", ""),
                "status": obj.get("status"),
                "voltage": obj.get("voltage", 0),
            })
        for v in obj.values():
            results.extend(find_channels(v))
    elif isinstance(obj, list):
        for item in obj:
            results.extend(find_channels(item))
    return results
```

### 第三步：展示可用通道

1. 筛选 `status` 为 `stop`/`protect`/`finish` 的通道
2. 按设备号分组，统计每个设备号下 stop/protect/finish 各有多少
3. 展示汇总表格：

```
| 设备号 | stop | protect | finish | 合计可用 |
|--------|------|---------|--------|---------|
| 1      | 0    | 3       | 29     | 32      |
| ...    |      |         |        |         |
```

4. 展示明细表格（设备号-排号-通道号 / 状态 / 电压）

### 第四步：询问测试电池数量

向用户提问：**「请问您需要测试几个电池？」**

### 第五步：询问电池体系

向用户提问：**「请选择电池体系：」**

目前支持的体系（来源于 `generate_xml_content` 模块的 `xml_<体系名>` 函数）：

- `811_Li_002`, `811_Li_005`
- `LB6`, `Gr_Li`, `LFP_Li`, `LFP_Gr`
- `SiGr_Li_Step`, `811_SiGr`, `811_Cu_aging`
- `ZQXNLRMO`

> 注：`generate_xml_content` 模块位于设备目录下，如暂不存在则按用户指定的体系名填入 CSV。

### 第六步：建议并选择通道

根据用户需要的电池数量，从可用通道中推荐：

1. **优先选择 `stop` 状态**的通道（最干净）
2. 其次选择 `finish` 状态
3. 最后选择 `protect` 状态
4. 尽量选同一设备号、同一排号的相邻通道

展示推荐列表，向用户提问：**「建议使用以下通道，是否确认？或请指定其他通道。」**

```
| # | 设备号-排号-通道号 | 状态   | 电压(V) |
|---|-------------------|--------|---------|
| 1 | 6-10-4            | stop   | 2.15    |
| 2 | 6-10-6            | finish | 0.00    |
```

### 第七步：确认模板来源

向用户提问：**「是否基于已有 CSV 模板修改通道号直接测试？还是需要填写全新的电池信息？」**

- **复用旧模板** → 引导用户指定旧 CSV 文件路径，读取并修改通道号、时间戳
- **全新信息** → 进入第八步逐项引导

### 第八步：引导填写电池参数

对于每个电池，依次引导用户填写以下参数：

#### 8a. 自动生成的字段（不需用户填写）

- `Timestamp` — 当前时间 `YYYY/MM/DD HH:MM`
- `Battery_Count` — 从 1 递增
- `设备号` / `排号` / `通道号` — 来自第六步选择
- `电池体系` — 来自第五步选择

#### 8b. 电池基本信息（需用户填写）

向用户提问：**「请提供以下电池信息：」**

| 字段                 | 说明             | 示例       |
| -------------------- | ---------------- | ---------- |
| Assembly_Time        | 组装时间（分钟） | 175        |
| Open_Circuit_Voltage | 开路电压(V)      | 0.194      |
| Pole_Weight          | 极片重量(mg)     | 29.33      |
| Assembly_Pressure    | 组装压力         | 3609       |
| Battery_Code         | 电池编号         | YS104329   |
| Electrolyte_Code     | 电解液编号       | LY26020507 |

#### 8c. 关键计算参数（必须引导填写，不可跳过）

向用户**逐项确认**：

1. **集流体质量(mg)**：**「请输入集流体质量（单位 mg）：」** — 示例：`3.64`
2. **活性物质含量**：**「请输入活性物质含量（0-1 之间的小数）：」** — 示例：`0.967`
3. **克容量mah/g**：**「请输入理论克容量（单位 mAh/g）：」** — 示例：`270`

填完后自动计算并展示：

```
活性物质质量 = (Pole_Weight - 集流体质量) × 活性物质含量
           = (29.33 - 3.64) × 0.967 = 24.84 mg
容量 = 活性物质质量 × 克容量 / 1000
     = 24.84 × 270 / 1000 = 6.708 mAh
```

> **容量为负数时必须警告用户**并确认是否继续。

### 第九步：生成 CSV 并核验

1. 生成 CSV 文件（**GBK 编码**，因为 `submit_from_csv` 使用 `pd.read_csv(encoding='gbk')` 读取）
2. 文件名格式：`YYYYMMDD.csv`（当日日期）
3. 展示完整表格让用户核验

```python
import csv, datetime

filename = datetime.datetime.now().strftime("%Y%m%d") + ".csv"
with open(filename, "w", newline="", encoding="gbk") as f:
    writer = csv.writer(f)
    writer.writerow([
        "Timestamp", "Battery_Count", "Assembly_Time", "Open_Circuit_Voltage",
        "Pole_Weight", "Assembly_Pressure", "Battery_Code", "Electrolyte_Code",
        "集流体质量", "活性物质含量", "克容量mah/g", "电池体系",
        "设备号", "排号", "通道号"
    ])
    for row in rows:
        writer.writerow(row)
```

向用户展示表格并提问：**「请核验以上 CSV 内容，确认无误后提交。」**

### 第十步：提交实验（notebook）

1. 调用 API #3 获取 workflow 模板详情 → 提取 `submit_from_csv` 节点 `uuid`
2. 构建 notebook 请求体：

```json
{
  "lab_uuid": "<lab_uuid>",
  "workflow_uuid": "<workflow_uuid>",
  "name": "新威电池测试-<YYYYMMDD>",
  "node_params": [
    {
      "sample_uuids": [],
      "datas": [
        {
          "node_uuid": "<节点uuid>",
          "param": {
            "csv_path": "<CSV绝对路径>",
            "output_dir": "<输出目录>"
          },
          "sample_params": []
        }
      ]
    }
  ]
}
```

3. 调用 API #4 提交 → 返回 notebook UUID 即为成功

### 第十一步：确认结果

1. 提交成功后拿到 notebook UUID
2. 调用 API #8 查询实验状态：`GET /api/v1/lab/notebook/status?uuid=<notebook_uuid>`
3. 向用户展示实验执行状态

---

## CSV 列定义参考

| 列号  | 列名                 | 说明                      | 来源             |
| ----- | -------------------- | ------------------------- | ---------------- |
| A     | Timestamp            | 时间戳 `YYYY/MM/DD HH:MM` | 自动生成         |
| B     | Battery_Count        | 电池序号（从1递增）       | 自动生成         |
| C     | Assembly_Time        | 组装时间（分钟）          | 用户填写         |
| D     | Open_Circuit_Voltage | 开路电压(V)               | 用户填写         |
| E     | Pole_Weight          | 极片重量(mg)              | 用户填写         |
| F     | Assembly_Pressure    | 组装压力                  | 用户填写         |
| G     | Battery_Code         | 电池编号                  | 用户填写         |
| H     | Electrolyte_Code     | 电解液编号                | 用户填写         |
| **I** | **集流体质量**       | 集流体质量(mg)            | **必须引导填写** |
| **J** | **活性物质含量**     | 活性物质含量(0-1)         | **必须引导填写** |
| **K** | **克容量mah/g**      | 理论克容量(mAh/g)         | **必须引导填写** |
| L     | 电池体系             | 体系名（决定 XML 模板）   | 用户选择         |
| M     | 设备号               | devid                     | 通道选择         |
| N     | 排号                 | subdevid                  | 通道选择         |
| O     | 通道号               | chlid                     | 通道选择         |

---

## 完整 Checklist

```
Task Progress:
- [ ] Step 1: 确认 ak/sk → 生成 AUTH token
- [ ] Step 2: 确认 --addr → 设置 BASE URL
- [ ] Step 3: GET /edge/lab/info → 获取 lab_uuid
- [ ] Step 4: GET /lab/material/download/{lab_uuid} → 获取资源树
- [ ] Step 5: 解析资源树 → 提取通道状态
- [ ] Step 6: 筛选可用通道 → 按设备号分组展示表格
- [ ] Step 7: 询问测试电池数量
- [ ] Step 8: 询问电池体系
- [ ] Step 9: 建议通道 → 用户确认选择
- [ ] Step 10: 确认模板来源（复用旧模板 / 填写新信息）
- [ ] Step 11: 引导填写电池参数（重点：集流体质量/活性物质含量/克容量）
- [ ] Step 12: 生成 CSV 文件 (GBK 编码) → 用户核验
- [ ] Step 13: 提交实验 notebook (POST #7)
- [ ] Step 14: 查询实验状态 (GET #8) → 确认结果
```
