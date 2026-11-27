---
name: batch-insert-reagent
description: Batch insert reagents into Uni-Lab platform — add chemicals with CAS, SMILES, supplier info. Use when the user wants to add reagents, insert chemicals, batch register reagents, or mentions 录入试剂/添加试剂/试剂入库/reagent.
---

# 批量录入试剂 Skill

通过云端 API 批量录入试剂信息，支持逐条或批量操作。

## 前置条件（缺一不可）

使用本 skill 前，**必须**先确认以下信息。如果缺少任何一项，**立即向用户询问并终止**，等补齐后再继续。

### 1. ak / sk → AUTH

询问用户的启动参数，从 `--ak` `--sk` 或 config.py 中获取。

生成 AUTH token（任选一种方式）：

```bash
# 方式一：Python 一行生成
python -c "import base64,sys; print('Authorization: Lab ' + base64.b64encode(f'{sys.argv[1]}:{sys.argv[2]}'.encode()).decode())" <ak> <sk>

# 方式二：手动计算
# base64(ak:sk) → Authorization: Lab <token>
```

### 2. --addr → BASE URL

| `--addr` 值  | BASE                                |
| ------------ | ----------------------------------- |
| `test`       | `https://leap-lab.test.bohrium.com` |
| `uat`        | `https://leap-lab.uat.bohrium.com`  |
| `local`      | `http://127.0.0.1:48197`            |
| 不传（默认） | `https://leap-lab.bohrium.com`      |

确认后设置：

```bash
BASE="<根据 addr 确定的 URL>"
AUTH="Authorization: Lab <gen_auth.py 输出的 token>"
```

**两项全部就绪后才可发起 API 请求。**

## Session State

- `lab_uuid` — 实验室 UUID（首次通过 API #1 自动获取，**不需要问用户**）

## 请求约定

所有请求使用 `curl -s`，POST 需加 `Content-Type: application/json`。

> **Windows 平台**必须使用 `curl.exe`（而非 PowerShell 的 `curl` 别名），示例中的 `curl` 均指 `curl.exe`。

---

## API Endpoints

### 1. 获取实验室信息（自动获取 lab_uuid）

```bash
curl -s -X GET "$BASE/api/v1/edge/lab/info" -H "$AUTH"
```

返回：

```json
{ "code": 0, "data": { "uuid": "xxx", "name": "实验室名称" } }
```

记住 `data.uuid` 为 `lab_uuid`。

### 2. 录入试剂

```bash
curl -s -X POST "$BASE/api/v1/lab/reagent" \
  -H "$AUTH" -H "Content-Type: application/json" \
  -d '{
    "lab_uuid": "<lab_uuid>",
    "cas": "<CAS号>",
    "name": "<试剂名称>",
    "molecular_formula": "<分子式>",
    "smiles": "<SMILES>",
    "stock_in_quantity": <入库数量>,
    "unit": "<单位字符串>",
    "supplier": "<供应商>",
    "production_date": "<生产日期 ISO 8601>",
    "expiry_date": "<过期日期 ISO 8601>"
  }'
```

返回成功时包含试剂 UUID：

```json
{"code": 0, "data": {"uuid": "xxx", ...}}
```

---

## 试剂字段说明

| 字段                | 类型   | 必填 | 说明                          | 示例                     |
| ------------------- | ------ | ---- | ----------------------------- | ------------------------ |
| `lab_uuid`          | string | 是   | 实验室 UUID（从 API #1 获取） | `"8511c672-..."`         |
| `cas`               | string | 是   | CAS 注册号                    | `"7732-18-3"`            |
| `name`              | string | 是   | 试剂中文/英文名称             | `"水"`                   |
| `molecular_formula` | string | 是   | 分子式                        | `"H2O"`                  |
| `smiles`            | string | 是   | SMILES 表示                   | `"O"`                    |
| `stock_in_quantity` | number | 是   | 入库数量                      | `10`                     |
| `unit`              | string | 是   | 单位（字符串，见下表）        | `"mL"`                   |
| `supplier`          | string | 否   | 供应商名称                    | `"国药集团"`             |
| `production_date`   | string | 否   | 生产日期（ISO 8601）          | `"2025-11-18T00:00:00Z"` |
| `expiry_date`       | string | 否   | 过期日期（ISO 8601）          | `"2026-11-18T00:00:00Z"` |

### unit 单位值

| 值     | 单位 |
| ------ | ---- |
| `"mL"` | 毫升 |
| `"L"`  | 升   |
| `"g"`  | 克   |
| `"kg"` | 千克 |
| `"瓶"` | 瓶   |

> 根据试剂状态选择：液体用 `"mL"` / `"L"`，固体用 `"g"` / `"kg"`。

---

## 批量录入策略

### 方式一：用户提供 JSON 数组

用户一次性给出多条试剂数据：

```json
[
  {
    "cas": "7732-18-3",
    "name": "水",
    "molecular_formula": "H2O",
    "smiles": "O",
    "stock_in_quantity": 10,
    "unit": "mL"
  },
  {
    "cas": "64-17-5",
    "name": "乙醇",
    "molecular_formula": "C2H6O",
    "smiles": "CCO",
    "stock_in_quantity": 5,
    "unit": "L"
  }
]
```

Agent 自动为每条补充 `lab_uuid`、`production_date`、`expiry_date` 等字段后逐条提交。

Agent 循环调用 API #2 逐条录入，每条记录一次 API 调用。

### 方式二：用户逐个描述

用户口头描述试剂（如「帮我录入 500mL 的无水乙醇，Sigma 的」），agent 自行补全字段：

1. 根据名称查找 CAS 号、分子式、SMILES（参考下方速查表或自行推断）
2. 构建完整的请求体
3. 向用户确认后提交

### 方式三：从 CSV/表格批量导入

用户提供 CSV 或表格文件路径，agent 读取并解析：

```bash
# 期望的 CSV 格式（首行为表头）
cas,name,molecular_formula,smiles,stock_in_quantity,unit,supplier,production_date,expiry_date
7732-18-3,水,H2O,O,10,mL,农夫山泉,2025-11-18T00:00:00Z,2026-11-18T00:00:00Z
```

### 日期格式规则（重要）

所有日期字段（`production_date`、`expiry_date`）**必须**使用 ISO 8601 完整格式：`YYYY-MM-DDTHH:MM:SSZ`。

- 用户输入 `2025-03-01` → 转换为 `"2025-03-01T00:00:00Z"`
- 用户输入 `2025/9/1` → 转换为 `"2025-09-01T00:00:00Z"`
- 用户未提供日期 → 使用当天日期 + `T00:00:00Z`，有效期默认 +1 年

**禁止**发送不带时间部分的日期字符串（如 `"2025-03-01"`），API 会拒绝。

### 执行与汇报

每次 API 调用后：

1. 检查返回 `code`（0 = 成功）
2. 记录成功/失败数量
3. 全部完成后汇总：「共录入 N 条试剂，成功 X 条，失败 Y 条」
4. 如有失败，列出失败的试剂名称和错误信息

---

## 常见试剂速查表

| 名称                  | CAS       | 分子式     | SMILES                               |
| --------------------- | --------- | ---------- | ------------------------------------ |
| 水                    | 7732-18-3 | H2O        | O                                    |
| 乙醇                  | 64-17-5   | C2H6O      | CCO                                  |
| 乙酸                  | 64-19-7   | C2H4O2     | CC(O)=O                              |
| 甲醇                  | 67-56-1   | CH4O       | CO                                   |
| 丙酮                  | 67-64-1   | C3H6O      | CC(C)=O                              |
| 二甲基亚砜(DMSO)      | 67-68-5   | C2H6OS     | CS(C)=O                              |
| 乙酸乙酯              | 141-78-6  | C4H8O2     | CCOC(C)=O                            |
| 二氯甲烷              | 75-09-2   | CH2Cl2     | ClCCl                                |
| 四氢呋喃(THF)         | 109-99-9  | C4H8O      | C1CCOC1                              |
| N,N-二甲基甲酰胺(DMF) | 68-12-2   | C3H7NO     | CN(C)C=O                             |
| 氯仿                  | 67-66-3   | CHCl3      | ClC(Cl)Cl                            |
| 乙腈                  | 75-05-8   | C2H3N      | CC#N                                 |
| 甲苯                  | 108-88-3  | C7H8       | Cc1ccccc1                            |
| 正己烷                | 110-54-3  | C6H14      | CCCCCC                               |
| 异丙醇                | 67-63-0   | C3H8O      | CC(C)O                               |
| 盐酸                  | 7647-01-0 | HCl        | Cl                                   |
| 硫酸                  | 7664-93-9 | H2SO4      | OS(O)(=O)=O                          |
| 氢氧化钠              | 1310-73-2 | NaOH       | [Na]O                                |
| 碳酸钠                | 497-19-8  | Na2CO3     | [Na]OC([O-])=O.[Na+]                 |
| 氯化钠                | 7647-14-5 | NaCl       | [Na]Cl                               |
| 乙二胺四乙酸(EDTA)    | 60-00-4   | C10H16N2O8 | OC(=O)CN(CCN(CC(O)=O)CC(O)=O)CC(O)=O |

> 此表仅供快速参考。对于不在表中的试剂，agent 应根据化学知识推断或提示用户补充。

---

## 完整工作流 Checklist

```
Task Progress:
- [ ] Step 1: 确认 ak/sk → 生成 AUTH token
- [ ] Step 2: 确认 --addr → 设置 BASE URL
- [ ] Step 3: GET /edge/lab/info → 获取 lab_uuid
- [ ] Step 4: 收集试剂信息（用户提供列表/逐个描述/CSV文件）
- [ ] Step 5: 补全缺失字段（CAS、分子式、SMILES 等）
- [ ] Step 6: 向用户确认待录入的试剂列表
- [ ] Step 7: 循环调用 POST /lab/reagent 逐条录入（每条需含 lab_uuid）
- [ ] Step 8: 汇总结果（成功/失败数量及详情）
```

---

## 完整示例

用户说：「帮我录入 3 种试剂：500mL 无水乙醇、1kg 氯化钠、2L 去离子水」

Agent 构建的请求序列：

```json
// 第 1 条
{"lab_uuid": "8511c672-...", "cas": "64-17-5", "name": "无水乙醇", "molecular_formula": "C2H6O", "smiles": "CCO", "stock_in_quantity": 500, "unit": "mL", "supplier": "国药集团", "production_date": "2025-01-01T00:00:00Z", "expiry_date": "2026-01-01T00:00:00Z"}

// 第 2 条
{"lab_uuid": "8511c672-...", "cas": "7647-14-5", "name": "氯化钠", "molecular_formula": "NaCl", "smiles": "[Na]Cl", "stock_in_quantity": 1, "unit": "kg", "supplier": "", "production_date": "2025-01-01T00:00:00Z", "expiry_date": "2026-01-01T00:00:00Z"}

// 第 3 条
{"lab_uuid": "8511c672-...", "cas": "7732-18-3", "name": "去离子水", "molecular_formula": "H2O", "smiles": "O", "stock_in_quantity": 2, "unit": "L", "supplier": "", "production_date": "2025-01-01T00:00:00Z", "expiry_date": "2026-01-01T00:00:00Z"}
```
