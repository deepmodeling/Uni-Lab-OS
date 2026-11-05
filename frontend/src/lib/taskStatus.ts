/**
 * 任务 / 节点状态词汇与控制命令可用性。
 *
 * 可用性镜像后端 `WorkflowStore.apply_task_command`（services/runtime/workflow/store.py）的前置检查，
 * 只用来给按钮置灰和提示；最终以后端返回为准（3003 冲突照常显示）。
 */
import type { TaskCommandType, WorkflowNodeRun, WorkflowTask } from '../api/types'

export const TASK_STATUSES = ['pending', 'running', 'canceling', 'succeeded', 'failed', 'canceled', 'timeout'] as const

export const TERMINAL_TASK_STATUSES = new Set(['succeeded', 'failed', 'canceled', 'timeout'])
export const TERMINAL_NODE_STATUSES = new Set(['succeeded', 'skipped', 'failed', 'timeout', 'canceled'])
/** 停在这些状态的节点要先处理，step / resume 才会被接受。 */
const UNRESOLVED_NODE_STATUSES = new Set([
  'execution_unknown',
  'intervention_required',
  'cancel_requested',
  'failed',
  'timeout',
  'canceled',
])

const STATUS_LABELS: Record<string, string> = {
  pending: '待运行',
  dispatched: '已下发',
  running: '运行中',
  canceling: '取消中',
  cancel_requested: '请求取消',
  succeeded: '已完成',
  failed: '失败',
  canceled: '已取消',
  timeout: '超时',
  skipped: '已跳过',
  intervention_required: '需要干预',
  execution_unknown: '结果待确认',
  waiting: '等待中',
  held: '持有中',
  released: '已释放',
  completed: '已完成',
  open: '待处理',
  selected: '已选择',
  superseded: '已作废',
  approved: '已批准',
  rejected: '已拒绝',
  timed_out: '已超时',
  active: '自动',
  paused: '已暂停',
  waiting_intervention: '等待干预',
  waiting_reconciliation: '等待裁决',
  ready: '就绪',
  not_ready: '未就绪',
}

export function statusLabel(status: string | null | undefined): string {
  if (!status) return '未知'
  return STATUS_LABELS[status] ?? status
}

export type StatusTone = 'info' | 'warn' | 'ok' | 'bad' | 'muted'

export function statusTone(status: string | null | undefined): StatusTone {
  switch (status) {
    case 'running':
    case 'dispatched':
    case 'canceling':
    case 'cancel_requested':
    case 'held':
    case 'active':
      return 'info'
    case 'pending':
    case 'waiting':
    case 'paused':
    case 'open':
    case 'waiting_intervention':
    case 'waiting_reconciliation':
    case 'not_ready':
      return 'warn'
    case 'succeeded':
    case 'completed':
    case 'approved':
    case 'selected':
    case 'ready':
      return 'ok'
    case 'failed':
    case 'timeout':
    case 'canceled':
    case 'rejected':
    case 'timed_out':
    case 'intervention_required':
    case 'execution_unknown':
      return 'bad'
    default:
      return 'muted'
  }
}

export function isTerminalTask(task: Pick<WorkflowTask, 'status'>): boolean {
  return TERMINAL_TASK_STATUSES.has(task.status)
}

export interface CommandAvailability {
  enabled: boolean
  reason: string
}

export const TASK_COMMANDS: TaskCommandType[] = ['step', 'resume', 'pause', 'cancel']

/**
 * 每个控制命令此刻是否可发。`nodeRuns` 缺省时不检查节点状态（列表页没有节点）。
 */
export function commandAvailability(
  task: Pick<WorkflowTask, 'status' | 'execution_kind' | 'run_mode' | 'control_status' | 'control_revision'>,
  nodeRuns?: Pick<WorkflowNodeRun, 'status'>[],
): Record<TaskCommandType, CommandAvailability> {
  const no = (reason: string): CommandAvailability => ({ enabled: false, reason })
  const yes = (reason: string): CommandAvailability => ({ enabled: true, reason })
  const executable = task.status === 'pending' || task.status === 'running'
  const missingRevision = task.control_revision === undefined

  const cancel = !executable
    ? no(task.status === 'canceling' ? '已在取消中' : '任务已结束')
    : missingRevision ? no('缺少 control_revision') : yes('不再派发；在执行的动作收到取消请求，设备返回后落 canceled')

  const result: Record<TaskCommandType, CommandAvailability> = {
    cancel,
    pause: no(''),
    step: no(''),
    resume: no(''),
  }
  const base = (): CommandAvailability | undefined => {
    if (!executable) return no('任务已结束')
    if (task.execution_kind !== 'workflow') return no('单点设备动作只支持取消')
    if (missingRevision) return no('缺少 control_revision')
    if (nodeRuns && nodeRuns.length && nodeRuns.every((run) => TERMINAL_NODE_STATUSES.has(run.status))) {
      return no('所有节点都已结束')
    }
    return undefined
  }
  const blocked = base()
  if (blocked) {
    result.pause = blocked
    result.step = blocked
    result.resume = blocked
    return result
  }

  result.pause = task.run_mode === 'step' && task.control_status === 'paused'
    ? no('已经暂停')
    : yes('转单步并停止派发新动作；已下发的照常跑完')

  const stepModeBlocked = (): CommandAvailability | undefined => {
    if (task.run_mode !== 'step') return no('只有单步模式（run_mode=step）接受 step / resume')
    if (task.control_status !== 'active' && task.control_status !== 'paused') {
      return no('任务需要先处理干预或裁决')
    }
    if (nodeRuns?.some((run) => UNRESOLVED_NODE_STATUSES.has(run.status))) {
      return no('有节点停在失败 / 待确认状态，先处理它')
    }
    return undefined
  }
  const stepBlock = stepModeBlocked()
  result.resume = stepBlock ?? yes('转回自动执行')
  result.step = stepBlock
    ?? (task.control_status !== 'paused' ? no('上一个单步还在执行') : yes('放行一个动作'))
  return result
}

/** 暂停窗口内可以修改入口参数（PATCH /input）。 */
export function canEditInput(task: Pick<WorkflowTask, 'status' | 'control_status' | 'execution_kind'>): boolean {
  return (
    task.execution_kind === 'workflow'
    && (task.status === 'pending' || task.status === 'running')
    && task.control_status === 'paused'
  )
}
