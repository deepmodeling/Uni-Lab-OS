---
goal: 为工作流与实验操作接口提供可执行的 FastAPI OpenAPI/Swagger 合同
version: 1.0
date_created: 2026-09-01
last_updated: 2026-09-01
owner: Uni-Lab-OS
status: 'Completed'
tags: [feature, api, openapi, swagger, workflow]
---

# Introduction

![Status: Completed](https://img.shields.io/badge/status-Completed-brightgreen)

本计划让 FastAPI 从实际路由和 Pydantic 模型实时生成工作流、实验操作与实验操作类别接口文档，并在 Swagger UI 中准确展示必填项、选填项、默认值、说明和返回结构。

## 1. Requirements & Constraints

- **REQ-001**: 完整 OS 的 `/api/docs` 与 `/api/openapi.json` 必须展示实时接口合同；隔离工作流应用继续使用 FastAPI 默认的 `/docs` 与 `/openapi.json`。
- **REQ-002**: 工作流创建、更新、列表、详情、删除、反向引用和实验操作类别 CRUD 必须说明请求字段、查询参数、路径参数及返回结构。
- **REQ-003**: OpenAPI 的 `required` 标记必须直接来自真实 Pydantic/FastAPI 合同，不维护第二份手工必填清单。
- **CON-001**: 不修改现有 HTTP 路径、字段名、默认值、状态筛选语义和 Backend 统一业务包络。
- **CON-002**: 不要求前端修改；现有调用方忽略 OpenAPI 元数据时行为保持不变。
- **GUD-001**: 中文描述优先，代码标识保持现有英文 wire 名称。
- **PAT-001**: 以公开 `/openapi.json` 和 `/docs` 为测试接缝，不测试私有 OpenAPI 辅助函数。

## 2. Implementation Steps

### Implementation Phase 1

- GOAL-001: 固化公开 OpenAPI 合同并补齐文档模型。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | 在 `tests/app/test_workflow_openapi.py` 先增加失败测试，验证 Swagger UI、必填/选填标记、参数说明、状态码和返回 schema。 | ✅ | 2026-09-01 |
| TASK-002 | 在独立 OpenAPI 模型模块中定义 Backend 成功/失败包络、工作流读模型、分页结果和实验操作类别读模型。 | ✅ | 2026-09-01 |
| TASK-003 | 为 `workflow_api.py` 与 `operation_category_api.py` 的目标路由补齐 Field、Query、Path、summary、status_code 和 response_model。 | ✅ | 2026-09-01 |

### Implementation Phase 2

- GOAL-002: 验证运行兼容性并完成交付审查。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-004 | 执行 OpenAPI 专项测试、既有工作流/类别合同测试和 `git diff --check`。 | ✅ | 2026-09-01 |
| TASK-005 | 核对完整 OS 的 `/api/docs`、`/api/redoc`、`/api/openapi.json` 路径未变化，并更新本计划状态。 | ✅ | 2026-09-01 |

## 3. Alternatives

- **ALT-001**: 维护独立 Markdown 接口表；未采用，因为必填信息会与 Pydantic DTO 漂移，且不能供 Swagger/客户端生成器直接使用。
- **ALT-002**: 一次性重写全部历史 OS API 文档；未采用，因为本次业务范围是工作流与实验操作，扩大范围会增加回归面。

## 4. Dependencies

- **DEP-001**: 复用仓库已安装的 FastAPI 0.141.1 与 Pydantic 2.13.4，不新增第三方依赖。

## 5. Files

- **FILE-001**: `unilabos/app/workflow_openapi.py`，集中定义只用于公开 HTTP 合同的响应模型。
- **FILE-002**: `unilabos/app/workflow_api.py`，补齐工作流请求字段和目标路由 OpenAPI 元数据。
- **FILE-003**: `unilabos/app/operation_category_api.py`，补齐实验操作类别 OpenAPI 元数据。
- **FILE-004**: `tests/app/test_workflow_openapi.py`，通过公开文档端点验证生成结果。
- **FILE-005**: `plan/feature-workflow-openapi-swagger-v1.md`，记录范围、验证和文件规模决定。

## 6. Testing

- **TEST-001**: `/docs` 可访问且加载 Swagger UI，`/openapi.json` 可访问。
- **TEST-002**: 创建工作流只把 `name` 标为必填，其他字段显示默认值、枚举、说明和示例。
- **TEST-003**: 列表查询与详情路径参数准确标识是否必传，并提供中文说明。
- **TEST-004**: 工作流和实验操作类别成功响应展示真实 Backend 包络及 200/201 状态码。
- **TEST-005**: 既有工作流类型、发布、类别和领域包同步回归不变。

## 7. Risks & Assumptions

- **RISK-001**: 返回模型若写得比实际数据更严格，会误导客户端生成器；因此模型只声明稳定公开字段并允许服务扩展字段。
- **ASSUMPTION-001**: 本次“出入参”指当前正在开发的工作流、实验操作和实验操作类别接口，不包含全部历史 OS 路由。

## 8. Related Specifications / Further Reading

- `CONTEXT.md`
- `plan/feature-experiment-operation-gap-checklist-v1.md`

## 9. File Size Decision

- `unilabos/app/workflow_api.py` 当前 1,681 行。本次只补已有工作流路由的 OpenAPI 元数据；独立响应模型已拆入 132 行的 `workflow_openapi.py`，避免继续把模型职责堆入聚合路由。
- `unilabos/app/operation_category_api.py` 当前 189 行，继续承载同一类别资源的 CRUD 路由。
- `tests/app/test_workflow_openapi.py` 当前 186 行，集中验证同一公开 `/docs` 与 `/openapi.json` 合同。

## 10. Validation Record

- OpenAPI 与工作流/类别专项回归：18 passed（项目 Python 3.11 虚拟环境）；仅有
  `httpx`/Starlette 的弃用提示，不影响结果。
- `python -m compileall`（本次修改的模块与测试）与 `git diff --check`：通过。
- 完整 OS 的 `/api/docs`、`/api/redoc`、`/api/openapi.json`：本次未启动完整生产装配，
  待部署环境做一次端到端冒烟；隔离工作流应用的 `/docs` 与 `/openapi.json` 已由专项
  测试验证 HTTP 200。
