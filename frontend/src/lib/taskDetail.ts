/** 任务详情页的数据整形：节点名索引、attempt 平铺、状态计数、错误决策归属。 */
import type { ErrorDecisionReport, WorkflowNodeJob, WorkflowNodeRun, WorkflowTask } from '../api/types'

export interface NodeInfo {
  name: string
  type: string
  action?: string
  deviceId?: string
  parentUuid?: string
}

/** 冻结快照里的节点：workflow_node_uuid → 名称 / 类型 / 动作。 */
export function nodeIndex(task: Pick<WorkflowTask, 'workflow_snapshot'> | undefined): Map<string, NodeInfo> {
  const result = new Map<string, NodeInfo>()
  for (const node of task?.workflow_snapshot?.nodes ?? []) {
    const meta = (node.meta_data ?? {}) as Record<string, unknown>
    result.set(node.uuid, {
      name: node.name || node.uuid,
      type: node.type,
      action: node.action_name || undefined,
      deviceId: typeof meta.target_device_id === 'string' ? meta.target_device_id : undefined,
      parentUuid: node.parent_uuid || undefined,
    })
  }
  return result
}

export function sortNodeRuns(runs: WorkflowNodeRun[]): WorkflowNodeRun[] {
  return [...runs].sort(
    (left, right) => left.topological_index - right.topological_index || left.uuid.localeCompare(right.uuid),
  )
}

export interface AttemptRow {
  job: WorkflowNodeJob
  run: WorkflowNodeRun
  /** 这是该节点当前的 attempt。 */
  current: boolean
}

/** 全部 attempt 按开始时间（无则创建时间）平铺，最新的在前。 */
export function flattenAttempts(runs: WorkflowNodeRun[]): AttemptRow[] {
  const rows = runs.flatMap((run) =>
    (run.attempts ?? []).map((job) => ({ job, run, current: run.current_job_uuid === job.uuid })),
  )
  const time = (job: WorkflowNodeJob) => job.started_at || job.create_time || ''
  return rows.sort(
    (left, right) =>
      time(right.job).localeCompare(time(left.job))
      || right.run.topological_index - left.run.topological_index
      || right.job.attempt_no - left.job.attempt_no,
  )
}

export function countByStatus(items: { status: string }[]): [string, number][] {
  const counts = new Map<string, number>()
  for (const item of items) counts.set(item.status, (counts.get(item.status) ?? 0) + 1)
  return [...counts.entries()].sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]))
}

/** 属于该任务的错误决策：按 task_id，或 job_id 落在任务的 attempt 里。 */
export function decisionsForTask(
  decisions: ErrorDecisionReport[],
  taskUuid: string,
  runs: WorkflowNodeRun[],
): ErrorDecisionReport[] {
  const jobIds = new Set(runs.flatMap((run) => (run.attempts ?? []).map((job) => job.uuid)))
  return decisions.filter((decision) => decision.task_id === taskUuid || jobIds.has(decision.job_id))
}

/** 节点运行状态（DAG 叠加用）：workflow_node_uuid → status。 */
export function statusByNode(runs: WorkflowNodeRun[]): Map<string, string> {
  return new Map(runs.map((run) => [run.workflow_node_uuid, run.status]))
}
