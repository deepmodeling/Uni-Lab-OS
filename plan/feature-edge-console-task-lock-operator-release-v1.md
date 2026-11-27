---
goal: 将工作流任务执行锁人工处置接入 Uni-Lab-OS 内置 Edge 控制台
version: 1.0
date_created: 2026-09-05
last_updated: 2026-09-05
owner: Uni-Lab-OS
status: 'Completed'
tags: [feature, frontend, execution-lock, operator-safety]
---

# Introduction

![Status: Completed](https://img.shields.io/badge/status-Completed-brightgreen)

`edge-service` 仅保存部署清单；实际由 Edge Workspace Backend 同源下发的控制台位于
`frontend/`。本计划把工作流任务执行锁的读取、人工安全确认和 CAS 释放操作接入该内置
控制台，保持后端已发布的 `/api/v1/workflow-tasks/{task_uuid}/execution-locks` 公共契约。

## 1. Requirements & Constraints

- **REQ-001**: 任务页选中任务后读取并展示当前活动执行锁，按后端返回的任务与作业事实显示状态。
- **REQ-002**: 只有后端返回 `can_release=true` 的锁允许发起人工释放；前端不得绕过安全门禁。
- **REQ-003**: 释放请求必须原样携带 `expected_claim_uuid`、`expected_fencing_token`、非空原因和物理安全确认。
- **SEC-001**: UI 明确警告该操作会释放同一作业的整组执行锁；不允许在不确定 Claim、在途作业或活动设备托管时诱导操作。
- **CON-001**: 仅修改 `~/Uni-Lab-OS` 内置前端和对应测试；`uni-lab-fe` 不再作为实现目标。
- **CON-002**: 前端只能通过 Edge 已公开的 HTTP API 读取和释放执行锁，不直接访问 SQLite 或导入兄弟仓库源码。
- **PAT-001**: 沿用 `edgeClient.ts` 的 Envelope 解包、`TasksPage` 的 TanStack Query 缓存和 `Button` 组件样式。

## 2. Implementation Steps

### Implementation Phase 1

- GOAL-001: 建立内置控制台的执行锁公共读写适配层。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-001 | 在 `frontend/src/types.ts` 声明执行锁快照、租约和释放结果的前端读模型。 | ✅ | 2026-09-05 |
| TASK-002 | 在 `frontend/src/lib/edgeClient.ts` 增加锁列表适配及带 CAS/物理确认的人工释放请求。 | ✅ | 2026-09-05 |

### Implementation Phase 2

- GOAL-002: 在任务详情页提供可审计的执行锁操作面板。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-003 | 在 `frontend/src/pages/TasksPage.tsx` 的选中任务详情区域加载并渲染活动锁，展示阻止原因与整组释放警告。 | ✅ | 2026-09-05 |
| TASK-004 | 实现人工释放表单、原因校验、物理安全勾选、提交中状态和成功后的查询失效/刷新。 | ✅ | 2026-09-05 |
| TASK-005 | 在 `frontend/src/styles.css` 补充锁面板和释放表单的响应式样式。 | ✅ | 2026-09-05 |

### Implementation Phase 3

- GOAL-003: 用页面级测试验证真实 API 路径和安全门禁。

| Task | Description | Completed | Date |
|------|-------------|-----------|------|
| TASK-006 | 增加列表展示、不可释放原因、释放请求 CAS 字段、重复刷新和断开只读场景测试。 | ✅ | 2026-09-05 |
| TASK-007 | 运行 `npm test` 与 `npm run build`，记录结果并完成计划状态更新。 | ✅ | 2026-09-05 |

## 3. Alternatives

- **ALT-001**: 继续扩展独立 `uni-lab-fe`；未采用，因为 Edge 部署实际从 Uni-Lab-OS 同源下发 `frontend/`。
- **ALT-002**: 在任务矩阵每个节点上直接显示锁按钮；未采用，因为执行锁属于 Job/Task 运行事实，集中放在选中任务详情可减少误点并能展示整组释放语义。

## 4. Dependencies

- **DEP-001**: Uni-Lab-OS 后端执行锁查询与人工释放 API 已在 `unilabos/app/workflow_api.py` 发布。
- **DEP-002**: Edge API 使用 `{ code, data, error }` Envelope；前端复用 `edgeClient.ts` 的 `requestData`/`postData`。
- **DEP-003**: Node.js 22.13+、frontend `package-lock.json` 和 Vite/Vitest 工具链。

## 5. Files

- **FILE-001**: `frontend/src/types.ts`，执行锁前端契约。
- **FILE-002**: `frontend/src/lib/edgeClient.ts`，执行锁 API 适配。
- **FILE-003**: `frontend/src/pages/TasksPage.tsx`，任务详情锁面板与操作表单。
- **FILE-004**: `frontend/src/styles.css`，锁面板视觉和响应式样式。
- **FILE-005**: `frontend/src/pages/pages.test.tsx`，任务锁交互测试。

## 6. Testing

- **TEST-001**: 连接状态下选中失败任务能显示活动锁、Job/Claim 状态和释放资格。
- **TEST-002**: 后端返回不可释放原因时按钮禁用且原因可见。
- **TEST-003**: 释放提交包含目标租约、期望 Claim/Fence、原因和物理确认，成功后重新读取列表。
- **TEST-004**: Edge 未连接时面板保持只读，所有写操作禁用。
- **TEST-005**: `npm test`（14 个文件、154 个测试）与 `npm run build` 全部通过。

## 7. Risks & Assumptions

- **RISK-001**: 后端在 CAS 校验期间可能发现页面快照过期；前端必须显示错误并要求重新读取，不能自动重试释放。
- **RISK-002**: API 可能返回未知的锁范围或状态；适配层保留原始字符串并使用安全的未知标签。
- **ASSUMPTION-001**: 任务详情 API 的锁列表字段与后端 `list_task_execution_locks` 返回结构保持兼容。
- **ASSUMPTION-002**: Edge 控制台与 API 同源，前端不需要额外跨域配置。

## 8. Related Specifications / Further Reading

- `plan/feature-workflow-task-lock-operator-release-v1.md`
- `frontend/README.md`
- `unilabos/app/workflow_api.py`
