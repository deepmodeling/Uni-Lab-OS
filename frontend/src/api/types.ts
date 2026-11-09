/**
 * 响应 DTO。
 *
 * 物料域在 OpenAPI 里有 Pydantic 模型，直接取生成类型；workflow 信封域和诊断路由的响应在
 * OpenAPI 里是无模式的 `{}`，这里按后端实现手写（字段来源见各类型注释里的 Python 位置）。
 * 请求体一律来自生成类型（`components['schemas']`）。
 */
import type { components } from './generated'

type Schemas = components['schemas']

export type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue }
export type JsonObject = { [key: string]: JsonValue }

// ── 请求体（生成类型）────────────────────────────────────────────────

export type TaskCommandBody = Schemas['WorkflowTaskCommandRequest']
export type TaskCommandType = TaskCommandBody['type']
export type TaskInputEditBody = Schemas['WorkflowTaskInputEditRequest']
export type TaskCreateBody = Schemas['WorkflowTaskCreateRequest']
export type ManualConfirmationDecisionBody = Schemas['ManualConfirmationDecisionRequest']
export type ErrorDecisionBody = Schemas['ErrorDecision']

// ── 诊断（server/api/runtime/diagnostics.py）──────────────────────────

export interface HealthStatus {
  status: string
  scheduler: 'local' | 'remote' | (string & {})
  /** ready / restarting / disabled */
  execution: string
}

/** `/readiness`：200 = ready，503 = not_ready，正文同形。 */
export interface ReadinessSummary {
  status: 'ready' | 'not_ready' | (string & {})
  scheduler: 'local' | 'remote' | (string & {})
  /** ready / starting / restarting / disabled */
  execution: string
  reasons: string[]
}

/** `HostLinkServer.peers()` 的一项：Slave 握手信息 + 计算出的 online。 */
export interface HostlinkPeer {
  node_id?: string
  online: boolean
  connected?: boolean
  last_seen?: number
  device_ids?: string[]
  [key: string]: unknown
}

export interface HostlinkSnapshot {
  role: 'host' | 'slave' | 'disabled' | (string & {})
  peers: HostlinkPeer[]
  client: {
    online: boolean
    host: string
    port: number
    node_id: string
    device_ids: string[]
    capabilities: unknown
  } | null
  owner?: string | null
  host_id?: string | null
  host_node_id?: string | null
  protocol_version?: string | number | null
  ros?: unknown
}

/** `/error-decisions` 的一项（执行面挂起失败 / 调度器重启遗留 attempt，两者同形）。 */
export interface ErrorDecisionReport {
  decision_id: string
  job_id: string
  task_id?: string
  device_id?: string
  action_name?: string
  node_id?: string
  exception_type?: string
  error_message?: string
  options: { action: string; label?: string; description?: string; [key: string]: unknown }[]
  retry_count?: number
  max_retries?: number
  category?: string
  severity?: string
  created_at?: number
  [key: string]: unknown
}

// ── 调度资源（server/backend/scheduler/models.py）──────────────────────

export interface LockIdentifier {
  kind: 'action' | 'material'
  device_id?: string
  action_name?: string
  material_uuid?: string
}

export interface ResourceRequestRecord {
  request_uuid: string
  owner_uuid: string
  task_uuid: string
  identifiers: LockIdentifier[]
  status: 'waiting' | 'held' | 'released' | 'canceled'
  release_requested?: boolean
  /** 持有冲突锁的 owner_uuid。 */
  blockers?: string[]
  created_sequence: number
  updated_sequence: number
  version: number
}

export interface LockOwnership {
  identifier: LockIdentifier
  owner_uuid: string
  acquired_sequence: number
  version: number
}

export interface MaterialHandleTransferProof {
  material_uuid: string
  workflow_edge_uuid: string
  source_task_uuid: string
  source_output_handle_uuid: string
  target_task_uuid: string
  target_input_handle_uuid: string
}

export interface LockHandoffRecord {
  handoff_uuid: string
  from_owner_uuid: string
  to_owner_uuid: string
  identifiers: LockIdentifier[]
  material_handle_proofs?: MaterialHandleTransferProof[]
  reason?: string
  status: 'pending' | 'completed' | 'canceled'
  created_sequence: number
  updated_sequence: number
  version: number
}

export interface SchedulerResourceSnapshot {
  sequence: number
  requests: ResourceRequestRecord[]
  ownerships: LockOwnership[]
  handoffs: LockHandoffRecord[]
}

// ── Workflow 信封域（server/api/runtime/workflow.py + workflow/store.py）────

export interface Page<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

interface Base {
  uuid: string
  create_time: string
  update_time: string
  meta_data: JsonObject
  description?: string
}

export interface Workflow extends Base {
  name: string
  tags: JsonValue[]
  revision: number
}

export interface WorkflowNode extends Base {
  workflow_uuid: string
  name: string
  type: string
  pose: JsonObject
  param: JsonObject
  execution_policy: JsonObject
  disabled: boolean
  minimized?: boolean
  workflow_node_template_uuid?: string
  parent_uuid?: string
  material_uuid?: string
  action_name?: string
  action_type?: string
}

export interface WorkflowEdge extends Base {
  source_node_uuid: string
  target_node_uuid: string
  source_handle_uuid: string
  target_handle_uuid: string
}

export interface WorkflowHandleTemplate extends Base {
  workflow_node_template_uuid: string
  handle_key: string
  io_type: string
  display_name: string
  type: string
  required: boolean
  data_key?: string
}

export interface WorkflowGraph {
  workflow: Workflow
  nodes: WorkflowNode[]
  edges: WorkflowEdge[]
  node_templates: (Base & { name: string; display_name?: string; [key: string]: unknown })[]
  handle_templates: WorkflowHandleTemplate[]
}

export interface WorkflowParameter {
  name: string
  schema: JsonObject
  required?: boolean
  default?: JsonValue
  title?: string
  description?: string
}

export interface WorkflowOutputDescriptor {
  name: string
  schema: JsonObject
  implicit?: boolean
  title?: string
  description?: string
}

/** 一次发布：冻结 (workflow_uuid, revision)。 */
export interface WorkflowPublication {
  uuid: string
  workflow_uuid: string
  revision: number
  name: string
  source_hash: string
  contract_digest: string
  create_time: string
  inputs: WorkflowParameter[]
  outputs: WorkflowOutputDescriptor[]
  [key: string]: unknown
}

export type TaskStatus = 'pending' | 'running' | 'canceling' | 'succeeded' | 'failed' | 'canceled' | 'timeout'
export type ControlStatus = 'active' | 'paused' | 'waiting_intervention' | 'waiting_reconciliation'
export type RunMode = 'normal' | 'step' | 'single_node'

export interface WorkflowTask extends Base {
  workflow_uuid: string | null
  execution_kind: 'workflow' | 'ad_hoc_device_action' | (string & {})
  status: TaskStatus | (string & {})
  /** 冻结的执行图：与 /workflows/{uuid}/graph 同形。 */
  workflow_snapshot: Partial<WorkflowGraph>
  execution_plan: JsonObject
  run_mode: RunMode | (string & {})
  control_status: ControlStatus | (string & {})
  /** 详情 / 命令响应才有；列表投影不含。 */
  control_revision?: number
  cleanup_status: string
  trace_context: JsonObject
  input?: JsonObject
  /** 只在单任务详情里返回。 */
  output?: JsonObject
  error_info: JsonValue[]
  target_node_uuid?: string
  timeout_at?: string
  attention_reason?: string
  started_at?: string
  finished_at?: string
}

export type NodeJobStatus =
  | 'pending'
  | 'dispatched'
  | 'running'
  | 'intervention_required'
  | 'cancel_requested'
  | 'execution_unknown'
  | 'succeeded'
  | 'failed'
  | 'skipped'
  | 'canceled'
  | 'timeout'

/** 一次 attempt（物理执行）。 */
export interface WorkflowNodeJob extends Base {
  workflow_node_run_uuid: string
  workflow_task_uuid: string
  workflow_node_uuid: string
  attempt_no: number
  trigger: string
  feedback_sequence: number
  status: NodeJobStatus | (string & {})
  param: JsonObject
  feedback_data: JsonObject
  return_info: JsonObject
  error_resolution: JsonObject
  control_data: JsonObject
  error_info: JsonValue[]
  retry_of_job_uuid?: string
  edge_uuid?: string
  edge_command_uuid?: string
  uncertainty_reason?: string
  started_at?: string
  finished_at?: string
}

/** 每个节点一条：status / return_info 是当前 attempt，attempts 是全部历史。 */
export interface WorkflowNodeRun extends Base {
  workflow_task_uuid: string
  workflow_node_uuid: string
  topological_index: number
  executor_kind: string
  execution_policy: JsonObject
  execution_timeout_seconds: number
  param: JsonObject
  status: NodeJobStatus | (string & {})
  current_job_uuid: string | null
  attempt_count: number
  return_info: JsonObject
  error_info: JsonValue[]
  feedback_data: JsonObject
  control_data: JsonObject
  attempts: WorkflowNodeJob[]
  material_uuid?: string
  started_at?: string
  finished_at?: string
}

export interface ManualConfirmation extends Base {
  workflow_task_uuid: string
  workflow_node_job_uuid: string
  status: 'pending' | 'approved' | 'rejected' | 'timed_out' | 'canceled' | (string & {})
  assignee_user_ids: string[]
  param: JsonObject
  opened_at: string
  confirmed_by?: string
  comment?: string
  deadline_at?: string
  decided_at?: string
}

export interface Intervention extends Base {
  workflow_task_uuid: string
  workflow_node_job_uuid: string
  revision: number
  status: 'open' | 'selected' | 'superseded' | (string & {})
  options: JsonObject[]
  resume_control_status: string
  selected_option: JsonObject
  opened_at: string
  selected_option_id?: string
  decided_at?: string
}

// ── 物料（生成类型）──────────────────────────────────────────────────

export type MaterialAggregate = Schemas['MaterialAggregateRead']
export type MaterialIdentity = Schemas['MaterialIdentityRead']
export type MaterialSite = Schemas['SiteRead']

/** 候选运行预检（server/services/runtime/workflow/run_preflight.py）。 */
export interface RunPreflight {
  workflow_uuid: string
  workflow_revision: number
  status: 'invalid' | 'temporarily_unavailable' | 'runnable_now'
  can_run: boolean
  checked_at: string
  checks: { type: string; status: string; code: string; message: string; blocking: boolean; node_uuid?: string; details: JsonObject }[]
  summary: Record<string, number>
}
export type RunPreflightBody = Schemas['WorkflowPreflightRequest']
