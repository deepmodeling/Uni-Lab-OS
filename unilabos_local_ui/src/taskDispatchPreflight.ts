export type TaskDispatchPreflightIssue = {
  category: string;
  code: string;
  message: string;
  severity: 'error' | 'warning';
  phase: string;
  template_id: string;
  template_name: string;
  node_id: string;
  device_id: string;
  action_name: string;
  instance_ids: string[];
  detail: Record<string, unknown>;
};

export type TaskDispatchPreflightResult = {
  valid: boolean;
  errors: TaskDispatchPreflightIssue[];
  warnings: TaskDispatchPreflightIssue[];
  workspace_version: number;
  workflow_fingerprint: string;
};

export type TaskDispatchReadiness = {
  status: 'stale' | 'validating' | 'ready' | 'invalid' | 'unavailable';
  key: string;
  result?: TaskDispatchPreflightResult;
  message?: string;
  source?: 'automatic' | 'dispatch';
};

export class TaskDispatchPreflightHttpError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code: string,
  ) {
    super(message);
    this.name = 'TaskDispatchPreflightHttpError';
  }
}

type FetchLike = typeof fetch;

function isIssue(value: unknown): value is TaskDispatchPreflightIssue {
  if (!value || typeof value !== 'object') return false;
  const issue = value as Record<string, unknown>;
  return typeof issue.code === 'string'
    && typeof issue.message === 'string'
    && typeof issue.category === 'string'
    && typeof issue.phase === 'string';
}

export function isTaskDispatchPreflightResult(
  value: unknown,
): value is TaskDispatchPreflightResult {
  if (!value || typeof value !== 'object') return false;
  const result = value as Record<string, unknown>;
  return typeof result.valid === 'boolean'
    && Array.isArray(result.errors)
    && result.errors.every(isIssue)
    && Array.isArray(result.warnings)
    && result.warnings.every(isIssue)
    && Number.isInteger(result.workspace_version)
    && typeof result.workflow_fingerprint === 'string';
}

function errorDetail(payload: unknown) {
  if (!payload || typeof payload !== 'object') return null;
  const detail = (payload as Record<string, unknown>).detail;
  return detail && typeof detail === 'object'
    ? detail as Record<string, unknown>
    : null;
}

export async function preflightTaskDispatch(options: {
  workflowPath: string;
  expectedVersion: number;
  workflow: Record<string, unknown>;
  fetcher?: FetchLike;
  signal?: AbortSignal;
}): Promise<TaskDispatchPreflightResult> {
  const fetcher = options.fetcher || fetch;
  const response = await fetcher('/api/task-execution/preflight', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({
      task_workspace_path: options.workflowPath,
      expected_version: options.expectedVersion,
      workflow: options.workflow,
    }),
    signal: options.signal,
  });
  const payload = await response.json().catch(() => null) as unknown;
  const detail = errorDetail(payload);
  const resultCandidate = isTaskDispatchPreflightResult(payload)
    ? payload
    : isTaskDispatchPreflightResult(detail)
      ? detail
      : null;
  if (response.status === 422 && resultCandidate) return resultCandidate;
  if (!response.ok) {
    const code = typeof detail?.code === 'string' ? detail.code : 'preflight_failed';
    const message = typeof detail?.message === 'string'
      ? detail.message
      : `派发预检失败（HTTP ${response.status}）`;
    throw new TaskDispatchPreflightHttpError(message, response.status, code);
  }
  if (!resultCandidate) {
    throw new TaskDispatchPreflightHttpError(
      '派发预检返回了无效响应',
      response.status,
      'preflight_response_invalid',
    );
  }
  return resultCandidate;
}

export function currentTaskDispatchReadiness(
  readiness: TaskDispatchReadiness,
  key: string,
): TaskDispatchReadiness {
  if (readiness.key === key) return readiness;
  return { status: 'stale', key };
}

export function taskDispatchReadinessLabel(readiness: TaskDispatchReadiness) {
  if (readiness.status === 'validating') return '正在验证派发条件';
  if (readiness.status === 'ready') return '派发准备通过';
  if (readiness.status === 'invalid') {
    const count = readiness.result?.errors.length || 0;
    return `派发准备失败${count ? ` · ${count} 项` : ''}`;
  }
  if (readiness.status === 'unavailable') return '派发预检暂不可用';
  return '等待派发预检';
}

export function canStartTaskDispatch(readiness: TaskDispatchReadiness) {
  return readiness.status === 'ready' && readiness.result?.valid === true;
}

export function taskDispatchIssueResolution(issue: TaskDispatchPreflightIssue) {
  const template = issue.template_name || issue.template_id || '对应 Task';
  const node = issue.node_id || '对应动作节点';
  const device = issue.device_id || '对应设备';
  const action = issue.action_name || '对应动作';
  if (issue.code === 'task_dispatch_scope_empty') {
    return '先选择至少一个 Task 模板，并生成或保留待派发的 Task 实例。';
  }
  if (issue.code === 'workspace_recovery_required') {
    return '先使用“重置并复用”清除失败暂停，再重新检查。';
  }
  if (issue.code === 'task_template_missing') {
    return '删除引用该模板的残留 Task，或重新创建对应模板。';
  }
  if (issue.code === 'task_template_empty') {
    return `在 Task「${template}」中至少选择一个当前流程动作节点。`;
  }
  if (issue.code === 'task_node_missing') {
    return `将动作节点「${node}」加入当前可执行 workflow，或从 Task「${template}」中移除该引用。`;
  }
  if (issue.code === 'task_node_not_executable') {
    return `启用动作节点「${node}」，并确认它位于当前起始节点可达的执行路径中。`;
  }
  if (issue.code === 'task_node_invalid') {
    return `修正动作节点「${node}」的重复 ID、设备或动作配置。`;
  }
  if (issue.code === 'task_device_missing') {
    return `确认设备「${device}」已注册并可用，再重新检查。`;
  }
  if (issue.code === 'task_action_unsupported') {
    return `为设备「${device}」选择受支持的动作；当前动作是「${action}」。`;
  }
  return '根据阻断信息修正 Task、流程或设备配置后重新检查。';
}

export function taskDispatchButtonTitle(readiness: TaskDispatchReadiness) {
  if (canStartTaskDispatch(readiness)) return '预检已通过，可以开始派发';
  if (readiness.status === 'validating') return '正在验证派发条件，请稍候';
  if (readiness.status === 'invalid') {
    const firstError = readiness.result?.errors[0]?.message;
    return firstError ? `预检未通过：${firstError}` : '预检未通过，请先修复阻断项';
  }
  if (readiness.status === 'unavailable') {
    return readiness.message || '派发预检暂不可用，请稍后重试';
  }
  return readiness.message || '等待流程与 Task 内容完成预检';
}
