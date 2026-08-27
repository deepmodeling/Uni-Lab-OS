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

function isResult(value: unknown): value is TaskDispatchPreflightResult {
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
  const resultCandidate = isResult(payload)
    ? payload
    : isResult(detail)
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
