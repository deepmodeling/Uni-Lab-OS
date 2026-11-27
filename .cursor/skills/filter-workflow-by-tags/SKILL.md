---
name: filter-workflow-by-tags
description: Query backend workflow list, aggregate all tags, and filter workflows by domain/scenario requirements using tags. Use when the user wants to search workflows, find workflows by tags, list available workflow tags, filter workflows by category/domain/scenario, or mentions 工作流筛选/标签查询/workflow tags/按领域查找工作流.
---
# Uni-Lab 工作流标签筛选指南

通过 Uni-Lab 云端 API 查询工作流列表，汇总所有可用标签（tags），并根据领域和场景要求筛选工作流。

> **重要**：本指南中的 `Authorization: Lab <token>` 是 **Uni-Lab 平台专用的认证方式**，`Lab` 是 Uni-Lab 的 auth scheme 关键字，**不是** HTTP Basic 认证。请勿将其替换为 `Basic`。

## 使用模式识别

**用户可能一开始就给出场景目标**（如"我要做有机合成实验"、"找柱层析相关的 protocol"）。此时：

1. **识别场景关键词** → 映射到可能的 tags（如 synthesis、organic、chromatography、purification）
2. **直接执行完整流程**（获取 ak/sk/addr → 拉取所有工作流 → 汇总 tags → 按场景筛选）
3. **展示筛选结果** → 引导用户从候选 workflow 中**选择明确的实验 protocol**
4. **如果用户确认某个 workflow** → 记录 `workflow_uuid`，准备对接“与其他 Skill 的协作”

**如果用户未给场景目标**，则按标准 checklist 询问筛选条件。

---

## 前置条件

使用本指南前，**必须**先确认以下信息。如果缺少任何一项，**立即向用户询问并终止**，等补齐后再继续。

### 1. ak / sk → AUTH

询问用户的启动参数，从 `--ak` `--sk` 或 config.py 中获取。

生成 AUTH token：

```bash
python -c "import base64,sys; print('Authorization: Lab ' + base64.b64encode(f'{sys.argv[1]}:{sys.argv[2]}'.encode()).decode())" <ak> <sk>
```

### 2. --addr → BASE URL

| `--addr` 值 | BASE                                  |
| ------------- | ------------------------------------- |
| `test`      | `https://leap-lab.test.bohrium.com` |
| `uat`       | `https://leap-lab.uat.bohrium.com`  |
| `local`     | `http://127.0.0.1:48197`            |
| 不传（默认）  | `https://leap-lab.bohrium.com`      |

确认后设置：

```bash
BASE="<根据 addr 确定的 URL>"
AUTH="Authorization: Lab <上面命令输出的 token>"
```

### 3. lab_uuid（实验室 UUID）

如果用户未提供 `lab_uuid`，通过以下 API 自动获取：

```bash
curl -s -X GET "$BASE/api/v1/edge/lab/info" -H "$AUTH"
```

返回 `data.uuid` 即为 `lab_uuid`。

**三项全部就绪后才可开始。**

## Session State

在整个对话过程中，agent 需要记住以下状态：

- `lab_uuid` — 实验室 UUID
- `all_workflows` — 完整工作流列表（分页获取后缓存到内存或临时文件）
- `all_tags` — 所有工作流的标签汇总

---

## API 端点

### 查询工作流列表（支持分页）

```
GET $BASE/api/v1/lab/workflow/owner/list?page=<page>&page_size=<page_size>&lab_uuid=$lab_uuid
```

**参数：**

- `page` — 页码，从 1 开始
- `page_size` — 每页数量，建议 1000
- `lab_uuid` — 实验室 UUID

**返回结构：**

```json
{
  "code": 0,
  "data": {
    "has_more": true,
    "data": [
      {
        "uuid": "9661bba2-1b9f-4687-a63d-910245df174b",
        "name": "Untitled",
        "description": "",
        "user_id": "114211",
        "published": false,
        "tags": null
      },
      {
        "uuid": "e0436638-190b-46bc-b1a1-2711d9602f6a",
        "name": "Synthesis v2",
        "user_id": "114211",
        "published": true,
        "tags": ["synthesis", "organic"]
      }
    ]
  }
}
```

**字段说明：**

- `has_more` — 若为 `true`，需要继续请求 `page+1`
- `tags` — 可能为 `null`、空数组或字符串数组；聚合时必须容忍 `null`

### 启动工作流（直接运行）

```
POST $BASE/api/v1/lab/workflow/<workflow_uuid>/run
```

**用途：** 直接启动一个 workflow 的默认执行（使用模板中预设的参数），无需创建 notebook。适用于快速测试或无参数变化的重复执行。

**请求体：** 空 JSON `{}` 或省略

**返回：**

```json
{
  "code": 0,
  "data": "<run_uuid>"
}
```

- `run_uuid` — 本次执行的唯一标识（不是 notebook UUID）

**注意：**

- 该接口会使用 workflow 模板中保存的默认参数直接执行
- 返回的 `run_uuid` 可直接传入下方「查询任务状态」接口查询实时进度

### 查询任务状态

```
GET $BASE/api/v1/lab/mcp/task/<task_uuid>
```

**用途：** 查询由 `POST /lab/workflow/<uuid>/run` 返回的 `run_uuid`（即 task_uuid）的实时执行状态，包括整体状态和每个节点（JOS：Job On Station）的执行详情。

**路径参数：**

- `task_uuid` — 等同于启动工作流接口返回的 `run_uuid`

**返回：**

```json
{
  "code": 0,
  "data": {
    "status": "running",
    "jos_status": [
      {
        "uuid": "d0e24bfe-8d99-450e-b19d-f25849dfbaad",
        "node_name": "PRCXI_BioER_96_wellplate_slot_1",
        "action_name": "create_resource",
        "status": "success",
        "return_info": {
          "suc": true,
          "error": "",
          "return_value": { ... }
        }
      },
      {
        "uuid": "...",
        "node_name": "...",
        "action_name": "transfer_liquid",
        "status": "pending",
        "return_info": null
      }
    ]
  }
}
```

**字段说明：**

- `data.status` — 整体任务状态
  - `running` — 正在执行（至少一个节点 pending 或 running）
  - `success` — 全部节点成功
  - `failed` — 有节点失败
- `data.jos_status[]` — 节点级执行列表（按执行顺序）
  - `uuid` — 节点执行实例 UUID
  - `node_name` — 节点名称（资源/设备名或工位名）
  - `action_name` — 动作类型（`create_resource`、`transfer_liquid`、`centrifuge`、等）
  - `status` — 节点状态：`success`、`pending`、`running`、`failed`
  - `return_info` — 执行返回，失败时 `suc=false` 且 `error` 有错误信息

**注意：**

- 此接口的 `task_uuid` **是** `POST /lab/workflow/<uuid>/run` 返回的 `run_uuid`，二者为同一个 ID 的不同称呼
- `jos_status` 数组按节点执行顺序给出；从 pending 数量可以估算剩余进度
- 返回体可能较大（`return_info.return_value` 中可能包含完整 resource tree），可在脚本中只提取 `status` + `node_name` + `action_name` 做摘要

**状态轮询示例：**

```bash
# 每 5 秒轮询一次直至完成
TASK="b183d97e-d2b5-4b24-b14b-820df04d87c0"
while :; do
  st=$(curl -s -X GET "$BASE/api/v1/lab/mcp/task/$TASK" -H "$AUTH" \
    | python3 -c "import json,sys; d=json.load(sys.stdin)['data']; \
      print(d['status'], '|', sum(1 for j in d['jos_status'] if j['status']=='success'), '/', len(d['jos_status']))")
  echo "$(date +%H:%M:%S) $st"
  [[ "$st" == success* || "$st" == failed* ]] && break
  sleep 5
done
```

---

## 完整工作流 Checklist

```
Task Progress:
- [ ] Step 0: 识别用户是否已给出场景目标（如"有机合成"、"柱层析"）
       - 若已给出 → 记录场景关键词，自动进入后续步骤
       - 若未给出 → 在 Step 6 询问用户
- [ ] Step 1: 确认 ak/sk → 生成 AUTH token
- [ ] Step 2: 确认 --addr → 设置 BASE URL
- [ ] Step 3: GET /edge/lab/info → 获取 lab_uuid（如用户未提供）
- [ ] Step 4: 分页获取所有工作流（从 page=1 开始直到 has_more=false）
- [ ] Step 5: 汇总所有非空 tags → 生成 all_tags（去重、排序、附出现次数）
- [ ] Step 6: 根据场景关键词（Step 0 或新询问）在 all_tags 中做语义映射 → 确定候选 tags
       - 若语义映射不唯一，列出候选 tags 让用户确认
- [ ] Step 7: 按候选 tags 筛选工作流（默认 any 模式，召回优先）
- [ ] Step 8: 展示筛选结果（uuid、name、description、tags、published）
- [ ] Step 9: 引导用户从结果中选择**明确的实验 protocol**
       - 若结果只有 1 条 → 直接确认该 workflow_uuid
       - 若结果 2–10 条 → 让用户按编号选择
       - 若结果过多 → 提示收紧条件（加 tag、切换 all 模式、仅 published）
       - 若结果为空 → 放宽条件（去掉最稀有 tag）或提示用户换关键词
- [ ] Step 10: 记录用户选中的 workflow_uuid，并提示提交实验或查看详情
```

---

## 推荐路径：使用脚本

同目录下提供 `scripts/filter_workflows.py`，一次完成分页抓取、标签聚合与筛选：

```bash
# 1. 仅汇总标签（不筛选）
python scripts/filter_workflows.py \
  --auth "<Lab base64token>" \
  --base "$BASE" \
  --lab-uuid "$lab_uuid" \
  --summary-only

# 2. 按标签筛选（ANY 模式：包含任一）
python scripts/filter_workflows.py \
  --auth "<Lab base64token>" \
  --base "$BASE" \
  --lab-uuid "$lab_uuid" \
  --tags synthesis organic \
  --mode any

# 3. 按标签筛选（ALL 模式：必须同时包含）
python scripts/filter_workflows.py \
  --auth "<Lab base64token>" \
  --base "$BASE" \
  --lab-uuid "$lab_uuid" \
  --tags synthesis organic \
  --mode all \
  --output filtered.json

# 4. 仅筛选已发布
python scripts/filter_workflows.py \
  --auth "<Lab base64token>" \
  --base "$BASE" \
  --lab-uuid "$lab_uuid" \
  --tags synthesis \
  --published-only
```

**`--auth` 参数说明**：传入 `Authorization` 头中 `Lab` 之后的 base64 token（不带 `Lab ` 前缀），脚本内部会自动补上 scheme。

**输出结构：**

```json
{
  "total_workflows": 150,
  "tag_counts": {"synthesis": 12, "organic": 8, "analysis": 5},
  "all_tags": ["analysis", "organic", "synthesis"],
  "filter": {"tags": ["synthesis", "organic"], "mode": "any"},
  "filtered_workflows": [
    {"uuid": "...", "name": "...", "description": "...", "tags": [...], "published": true}
  ]
}
```

---

## 手动路径：curl + jq

如果脚本不可用或环境缺少 Python，可用 shell 实现。

### 1. 分页抓取（写入 `all_workflows.json`）

```bash
page=1
echo "[]" > all_workflows.json

while :; do
  resp=$(curl -s -X GET \
    "$BASE/api/v1/lab/workflow/owner/list?page=$page&page_size=1000&lab_uuid=$lab_uuid" \
    -H "$AUTH")

  page_data=$(echo "$resp" | jq -c '.data.data // []')
  jq -c --argjson p "$page_data" '. + $p' all_workflows.json > _tmp.json && mv _tmp.json all_workflows.json

  has_more=$(echo "$resp" | jq -r '.data.has_more')
  [ "$has_more" != "true" ] && break
  page=$((page + 1))
done

echo "Total: $(jq 'length' all_workflows.json)"
```

### 2. 汇总所有标签（含出现次数）

```bash
jq '[.[].tags // [] | .[]] | group_by(.) | map({tag: .[0], count: length}) | sort_by(-.count)' \
  all_workflows.json
```

### 3. 按标签筛选

```bash
# ANY：包含任一指定标签
jq --argjson want '["synthesis","organic"]' \
  '[.[] | select((.tags // []) | any(. as $t | $want | index($t)))]' \
  all_workflows.json

# ALL：同时包含所有指定标签
jq --argjson want '["synthesis","organic"]' \
  '[.[] | select(($want | all(. as $w | (.tags // []) | index($w))))]' \
  all_workflows.json
```

---

## 筛选策略

agent 拿到用户的「领域 + 场景」自然语言描述时，按如下顺序选择 tag：

1. **优先用户显式指定的 tags**：若用户明确给出标签词，直接精确匹配。
2. **从 all_tags 中做语义映射**：若用户描述是自然语言（如"有机合成、柱层析"），在 all_tags 中找语义相关项（如 `synthesis`、`organic`、`chromatography`）。必要时展示候选 tag 让用户确认。
3. **模式选择**：
   - 默认 `any`（更多召回），给出 tag 集合的并集匹配
   - 用户强调"必须同时满足"时用 `all`
4. **空结果兜底**：如果筛选为空，放宽条件（去掉最稀有 tag、切换 any 模式），并提醒用户。

---

## 引导到明确的 Protocol

筛选完成后，**最终目标是让用户确认一个具体的 workflow_uuid**，而不是停留在"一堆候选"上。按结果数量采取不同策略：

| 结果数量  | 策略                                                                                                                               |
| --------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| 0 条      | 放宽筛选（去掉最稀有 tag → 切换 any 模式 → 去掉 `--published-only`）。仍为空则提示换关键词，或列出 `all_tags` 让用户重新选。 |
| 1 条      | 直接确认："找到唯一匹配：`<name>` (uuid `<uuid>`)，是否用它？"用户确认后记录 `workflow_uuid`。                               |
| 2–10 条  | 编号列表展示，让用户选编号。每项给出 name、tags、description 摘要、published 状态。                                                |
| 10–30 条 | 先展示 tag 分布帮助用户进一步收紧：列出匹配结果中最常见的子标签，提示"加一个 tag 可将结果缩小到 N 条"。                            |
| >30 条    | 强制要求用户补充条件：仅 published、指定具体 tag 组合、或按名称关键词过滤。                                                        |

**确认 workflow 后**：

1. 将 `workflow_uuid` 写入 session state
2. 提示用户下一步可用的 skill：
   - 提交实验 → 引导到“与其他 Skill 的协作”
   - 查看 workflow 详细节点 → `GET /api/v1/lab/workflow/template/detail/<workflow_uuid>`
3. 若用户想换一个，回到筛选步骤。

---

## 展示结果

推荐格式（表格 + 汇总统计）：

```
共 150 个工作流，其中 32 个匹配筛选条件 [tags: synthesis OR organic]

| UUID (短) | 名称                     | Tags                         | 已发布 |
|-----------|--------------------------|------------------------------|--------|
| e0436638  | Synthesis v2             | synthesis, organic           | ✓      |
| 5b60dbb8  | Grignard Protocol        | synthesis, organometallic    | ✓      |
| ...       | ...                      | ...                          | ...    |

所有可用标签（按频次）：
  synthesis (12), organic (8), analysis (5), purification (4), ...
```

如果用户下一步想执行某工作流 → 引导到“与其他 Skill 的协作”。

---

## 常见问题

### Q: tags 为 null 的工作流要不要展示？

默认**不展示**在筛选结果中（因为无法按 tag 匹配）。但在 `--summary-only` 或无筛选条件时，这些工作流仍会计入总数，并在输出中单独列出"未打标签"计数。

### Q: 如何按名称/描述做模糊匹配？

脚本未内置，但可在 jq 中组合：

```bash
jq '[.[] | select((.name + " " + (.description // "")) | test("organic"; "i"))]' all_workflows.json
```

### Q: `page_size=1000` 是否会被服务端限制？

接口通常允许最大 1000；如果返回量少于 1000 且 `has_more=false`，说明已到末页。极端情况下若服务端返回错误，可降到 200 或 500 再试。

### Q: 工作流数量极大（>10k）怎么办？

1. 先跑 `--summary-only` 了解 tag 分布
2. 提示用户先限定 `--published-only` 或指定 tag
3. 考虑将 `all_workflows.json` 缓存到本地，下次直接复用

---

## 与其他 Skill 的协作

- 正常情况下，找到 workflow 之后可以直接用它提交实验（启动工作流的 api 端点 POST $BASE/api/v1/lab/workflow/<workflow_uuid>/run，不用别的 skill）
- **仅当需要进行多次实验时，使用 batch-submit-experiment** — 筛选到目标工作流后，`workflow_uuid` 直接用于实验提交

## 脚本依赖

`scripts/filter_workflows.py` 仅使用 Python 标准库（`urllib`、`json`、`argparse`），无需额外安装。
