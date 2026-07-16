# task-orchestration

运行前应显式配置写入凭据：

```bash
export TASK_ORCHESTRATION_ADMIN_TOKEN='admin-token'
export TASK_ORCHESTRATION_GATEWAY_TOKEN='gateway-token'
export TASK_ORCHESTRATION_UI_TOKEN='ui-token'
```

- `PUT /workspaces` 是管理写接口，未配置 `TASK_ORCHESTRATION_ADMIN_TOKEN` 时始终拒绝。
- `/opc/snapshots` 仅供网关使用，必须配置
  `TASK_ORCHESTRATION_GATEWAY_TOKEN` 并提供 `Authorization: Bearer <token>`；
  `/workspaces` 全量写入仅供管理员使用，必须配置
  `TASK_ORCHESTRATION_ADMIN_TOKEN`。普通 UI 的模板、实例、排程和
  `PUT /workspaces/scheduled-templates` 必须配置
  `TASK_ORCHESTRATION_UI_TOKEN` 并发送对应 Bearer token；未配置时默认拒绝。
  Vite 开发环境通过 `VITE_TASK_ORCHESTRATION_UI_TOKEN` 注入请求头；不要将该变量
  写入前端持久化状态、日志或版本库。

## Trigger DTO

- CSV OPC 条件：`{"kind":"opc","config":{"provider_id":"default","variable":"变量名","value":<类型化值>}}`。
  默认网关向 `/api/v1/opc/snapshots` 写入相同 `provider_id: "default"` 的快照；
  `test_default_provider_snapshot_starts_frontend_mapped_opc_trigger` 覆盖该路径。
- 系统资源约束：`{"kind":"resource","config":{"resource":"robot"}}`；
  系统工位约束：`{"kind":"workstation","config":{"workstation":"s09"}}`。
  后端调度策略将它们作为实时互斥占用。工位占用键独立于 `Template.resources`，
  即使模板资源列表为空，也不会并发选择同一工位。
- 策略内部占用键分别使用 `resource:<id>` 与 `workstation:<id>` 命名空间。
  `Template.resources` 只能提交未加前缀的业务资源名；提交任一保留前缀会返回 422，
  以避免普通资源与策略键冲突。
- 业务链路：`{"kind":"internal","config":{"key":"稳定业务键","value":<类型化值>}}`。
  仅同一 `key`、`value` 的上游 output 审计事件可满足该输入条件。
- 自动资源/工位输出仅记录审计事件，例如
  `{"kind":"resource","config":{"resource":"robot","event":"released"}}`，
  不用于解锁自动系统约束。
- 旧 `opc_condition` DTO 会以 422 拒绝。
