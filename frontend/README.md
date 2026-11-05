# Uni-Lab-OS 调试控制台（React）

这是给开发者用的**调试界面**：直接对着微后端的 HTTP / SSE 接口看状态、发控制命令。
推荐给实验人员使用的界面是 [OpenLab（OpenLabSite）](https://xuwznln.github.io/OpenLabSite/)，
它在独立仓库维护；本目录不依赖它，也不随 Python 包发布（`frontend/` 不在
`setup.py` 的包发现范围内）。界面布局与 DAG / 物料树组件沿用 v1.0 的 React 控制台。

| 页面 | 接口 |
| --- | --- |
| 总览 | `/readiness`（200 / 503 同形）、`/health`、`/hostlink/peers`、按状态的任务计数 |
| 任务 | `/workflow-tasks` 列表；详情含节点运行与 attempt、冻结图 DAG、step / resume / pause / cancel（带 `control_revision` 与幂等键）、暂停时修改入口参数、人工确认、干预、错误决策 |
| 调度锁 | `/scheduler/resources`：持有的锁、等待请求及其 blockers、交接；自动刷新 |
| 事件流 | `/events` SSE：过滤、暂停、从 `Last-Event-ID: 0` 回放 |
| 物料 | `/materials/instances` 只读树 + 单个物料的原始 JSON |
| 工作流 | `/workflows` 列表、DAG（handle 连线 + `execution_policy.depends_on`）、节点、发布、提交任务 |

## 运行

需要 Node.js 22+。先起一个微后端（默认 `http://127.0.0.1:8002`），再：

```bash
cd frontend
npm ci                      # 国内镜像：npm ci --registry=https://registry.npmmirror.com
npm run dev                 # http://127.0.0.1:5174
UNILAB_BACKEND_URL=http://127.0.0.1:18620 npm run dev   # 换后端地址
```

页面只请求同源 `/api`，由 Vite dev / preview 服务器代理到 `UNILAB_BACKEND_URL`
（也可以写进 `frontend/.env.local`）。`npm run build` 产出 `dist/`，用 `npm run preview`
在同一套代理下查看；不提交 `dist/`。

后端的写操作需要控制面 token：在运行 unilab 的机器上执行 `unilab token`，填进侧栏「控制令牌」，
或用 `http://127.0.0.1:5174/?token=<token>` 打开（读到后存进本浏览器并从地址栏抹掉）。请求带
`Authorization: Bearer <token>`。

## 检查

```bash
npm test          # vitest：传输层、SSE 解析、数据整形、DAG 布局、页面冒烟
npm run build     # tsc -b + vite build
```

## API 层

- `openapi/unilabos-openapi.json`：后端离线导出的契约快照（`python -m unilabos.server.openapi_export`）。
- `src/api/generated.ts`：由快照经 `openapi-typescript` 生成，请求体与查询参数都取自这里；
  `src/api/endpoints.ts` 的路径类型受它约束，`endpoints.test.ts` 再逐条核对方法。
- `src/api/http.ts`：一个 fetch 包装。workflow / graphs / chemicals / registry 走
  `{"code", "data" | "error": {"msg"}}` 信封（业务码见 `unilabos/server/api/envelope.py`：
  0 / 1000 / 3002 / 3003 / 5001 / 1，业务失败也是 HTTP 200）；其余域直出 DTO，失败为 HTTP 状态码 +
  `{"detail"}`。
- `src/api/sse.ts`：用 fetch 流读 `/events`，首连即可带 `Last-Event-ID`，断线按 `retry:` 续传。
- `src/api/types.ts`：OpenAPI 里没有响应模型的 workflow / 诊断响应，按后端实现手写。

后端路由或模型变了之后：

```bash
npm run api:sync      # 重新导出快照并生成类型（需要能 import unilabos 的 Python；UNILABOS_PYTHON 可指定解释器）
npm run api:check     # 快照与新导出、类型与快照不一致时失败
```

`tests/frontend/test_debug_console_api.py` 做同样的对账：有 `node` 时检查快照，装了
`frontend/node_modules` 时再检查生成类型，否则跳过。
