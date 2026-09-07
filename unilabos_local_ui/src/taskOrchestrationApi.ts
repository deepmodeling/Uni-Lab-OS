import type { TriggerCondition } from './taskOrchestration';

export type ApiTrigger = {
  kind: 'opc';
  config: {
    plc_device_id?: string;
    variable?: string;
    value?: string | number | boolean;
  };
};

export type ApiTaskDependency = {
  template_id: string;
  node_id?: string | null;
};

export type ApiTemplate = {
  id: string;
  name: string;
  workflow_path: string;
  node_ids: string[];
  resources: string[];
  input_triggers: ApiTrigger[];
  output_triggers: ApiTrigger[];
  dependencies?: ApiTaskDependency[] | null;
};

export type ApiNodeExecutionRecord = {
  node_id: string;
  attempt: number;
  execution_id: string;
  status: 'pending' | 'running' | 'succeeded' | 'failed';
  started_at: number | null;
  finished_at: number | null;
  error?: Record<string, unknown> | null;
};

export type ApiTaskInstance = {
  id: string;
  template_id: string;
  status: 'waiting' | 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';
  sample_id: string;
  order: number;
  not_before: number | null;
  started_at: number | null;
  finished_at: number | null;
  payload?: {
    node_parameters?: Record<string, Record<string, unknown>>;
  };
  execution_state?: {
    cursor?: number;
    records?: ApiNodeExecutionRecord[];
    active_node_id?: string | null;
    active_execution_id?: string | null;
  };
};

export type ApiWorkspaceEvent = {
  id?: string;
  kind: string;
  timestamp: number;
  instance_id: string | null;
  template_id: string | null;
  payload: Record<string, unknown>;
};

export type ApiScheduleEntry = {
  instance_id: string;
  template_id: string;
  sample_id: string;
  start_at: number;
  end_at: number;
  resources: string[];
  state: 'planned' | 'running' | 'done';
};

export type ApiWaitingReason = {
  code: string;
  message: string;
  context: Record<string, unknown>;
};

export type ApiWorkspace = {
  workflow_path: string;
  templates: ApiTemplate[];
  task_instances: ApiTaskInstance[];
  events: ApiWorkspaceEvent[];
  scheduled_template_ids: string[];
  scheduler_paused: boolean;
  pause_reason?: Record<string, unknown> | null;
  schedule_entries: ApiScheduleEntry[];
  opc_snapshots: Array<{
    plc_device_id: string;
    sequence: number;
    values: Record<string, unknown>;
    updated_at_by_variable: Record<string, number>;
  }>;
  plc_registrations: Array<{
    plc_device_id: string;
    runtime_url: string;
    variables: string[];
    aliases: Record<string, string>;
  }>;
};

export type ApiWorkspaceResponse = {
  version: number;
  workspace: ApiWorkspace;
  schedule?: {
    entries: ApiScheduleEntry[];
    waiting_reasons: Record<string, ApiWaitingReason>;
  };
};

export type TaskExecutionTickStats = {
  active: number;
  in_flight: number;
  claimed: number;
  completed: number;
  failed: number;
};

export type TaskExecutionCycleResult = {
  active: boolean;
  workspace: ApiWorkspaceResponse;
  tick: TaskExecutionTickStats | null;
};

export type TaskExecutionPhase = 'idle' | 'dispatching' | 'running' | 'completed' | 'failed';

export type TaskExecutionStatus = {
  phase: TaskExecutionPhase;
  label: '空闲' | '采样派发' | '执行中' | '已完成' | '故障';
  tick: TaskExecutionTickStats;
};

const EMPTY_TASK_EXECUTION_TICK: TaskExecutionTickStats = {
  active: 0,
  in_flight: 0,
  claimed: 0,
  completed: 0,
  failed: 0,
};

export function createTaskExecutionStatus(
  tick?: Partial<TaskExecutionTickStats> | null,
  workspace?: ApiWorkspaceResponse | null,
  phase?: TaskExecutionPhase,
): TaskExecutionStatus {
  const normalized = {
    active: Number(tick?.active || 0),
    in_flight: Number(tick?.in_flight || 0),
    claimed: Number(tick?.claimed || 0),
    completed: Number(tick?.completed || 0),
    failed: Number(tick?.failed || 0),
  };
  const taskInstances = workspace?.workspace.task_instances || [];
  const allCompleted = taskInstances.length > 0
    && taskInstances.every((instance) => instance.status === 'completed');
  const nextPhase = phase || (
    normalized.failed > 0 || Boolean(workspace?.workspace.pause_reason)
      ? 'failed'
      : normalized.in_flight > 0
        ? 'running'
        : normalized.claimed > 0
          ? 'dispatching'
          : allCompleted
            ? 'completed'
            : 'idle'
  );
  const labels: Record<TaskExecutionPhase, TaskExecutionStatus['label']> = {
    idle: '空闲',
    dispatching: '采样派发',
    running: '执行中',
    completed: '已完成',
    failed: '故障',
  };
  return { phase: nextPhase, label: labels[nextPhase], tick: normalized };
}

export class TaskExecutionCycleCancelledError extends Error {
  readonly cause?: unknown;

  constructor(message = 'Task 执行周期已取消', cause?: unknown) {
    super(message);
    this.name = 'TaskExecutionCycleCancelledError';
    this.cause = cause;
  }
}

export class TaskExecutionPreflightRejectedError extends Error {
  readonly code = 'task_dispatch_preflight_failed';

  constructor(
    message: string,
    readonly preflight: unknown,
  ) {
    super(message);
    this.name = 'TaskExecutionPreflightRejectedError';
  }
}

export class TaskOrchestrationBusinessError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
    this.name = 'TaskOrchestrationBusinessError';
  }
}

export class TaskOrchestrationServiceUnavailableError extends Error {
  constructor(message = 'Task 编排服务不可用') {
    super(message);
    this.name = 'TaskOrchestrationServiceUnavailableError';
  }
}

export function resolveTaskOrchestrationApiUrl(
  env: Record<string, string | undefined> = import.meta.env || {},
  location: Pick<Location, 'protocol' | 'hostname' | 'port'> | undefined = globalThis.location,
) {
  if (env.VITE_TASK_ORCHESTRATION_API_URL) {
    return env.VITE_TASK_ORCHESTRATION_API_URL.replace(/\/+$/, '');
  }
  if (location?.hostname === '127.0.0.1' && location.port === '8014') {
    return `${location.protocol}//${location.hostname}:8091/api/v1`;
  }
  return '/task-api/api/v1';
}

export function toApiTrigger(condition: TriggerCondition): ApiTrigger {
  const plcDeviceId = condition.plcDeviceId?.trim();
  if (!plcDeviceId) {
    throw new Error(`条件 ${condition.variableName || '未命名变量'} 缺少 PLC 设备 ID`);
  }
  return {
    kind: 'opc',
    config: {
      plc_device_id: plcDeviceId,
      variable: condition.variableName,
      value: condition.value,
    },
  };
}

export function fromApiTrigger(trigger: ApiTrigger): TriggerCondition {
  const value = trigger.config.value ?? true;
  return {
    ...(trigger.config.plc_device_id
      ? { plcDeviceId: String(trigger.config.plc_device_id) }
      : {}),
    variableName: String(
      trigger.config.variable || '',
    ),
    dataType: typeof value === 'boolean' ? 'BOOL' : typeof value === 'number' ? 'FLOAT' : 'STRING',
    value,
  };
}

type FetchLike = typeof fetch;

type ClientOptions = {
  baseUrl?: string;
  fetchImpl?: FetchLike;
};

function isWorkspaceResponse(payload: unknown): payload is ApiWorkspaceResponse {
  if (!payload || typeof payload !== 'object') return false;
  const candidate = payload as Record<string, unknown>;
  const workspace = candidate.workspace;
  if (!Number.isInteger(candidate.version) || !workspace || typeof workspace !== 'object') return false;
  const value = workspace as Record<string, unknown>;
  return typeof value.workflow_path === 'string'
    && Array.isArray(value.templates)
    && Array.isArray(value.task_instances)
    && Array.isArray(value.events)
    && Array.isArray(value.scheduled_template_ids)
    && typeof value.scheduler_paused === 'boolean'
    && Array.isArray(value.schedule_entries)
    && Array.isArray(value.opc_snapshots)
    && Array.isArray(value.plc_registrations);
}

async function parseResponse(response: Response): Promise<ApiWorkspaceResponse> {
  const payload = await response.json().catch(() => null) as ApiWorkspaceResponse | { detail?: unknown } | null;
  if (!response.ok) {
    if (response.status >= 500) {
      throw new TaskOrchestrationServiceUnavailableError();
    }
    const detail = payload && typeof payload === 'object' && 'detail' in payload ? payload.detail : null;
    const message = typeof detail === 'string'
      ? detail
      : detail && typeof detail === 'object' && 'message' in detail
        ? String(detail.message)
        : `请求失败（HTTP ${response.status}）`;
    throw new TaskOrchestrationBusinessError(message, response.status);
  }
  if (!isWorkspaceResponse(payload)) {
    throw new TaskOrchestrationServiceUnavailableError('Task 编排服务返回了无效响应');
  }
  return payload as ApiWorkspaceResponse;
}

export function createTaskOrchestrationClient(options: ClientOptions = {}) {
  const baseUrl = options.baseUrl || resolveTaskOrchestrationApiUrl();
  const fetchImpl = options.fetchImpl || fetch;

  const request = async (
    path: string,
    init: RequestInit = {},
  ) => {
    try {
      return await parseResponse(await fetchImpl(`${baseUrl}${path}`, {
        ...init,
        headers: {
          'content-type': 'application/json',
          ...init.headers,
        },
      }));
    } catch (error) {
      if (
        error instanceof TaskOrchestrationBusinessError
        || error instanceof TaskOrchestrationServiceUnavailableError
      ) throw error;
      throw new TaskOrchestrationServiceUnavailableError();
    }
  };

  const body = (payload: unknown) => ({ method: 'POST', body: JSON.stringify(payload) });

  return {
    getWorkspace: (workflowPath: string, signal?: AbortSignal) => request(`/workspaces?workflow_path=${encodeURIComponent(workflowPath)}`, {
      method: 'GET',
      signal,
    }),
    resetWorkspace: (workflowPath: string) => (
      request('/workspaces/reset', body({ workflow_path: workflowPath }))
    ),
    createTemplate: (workflowPath: string, expectedVersion: number, template: ApiTemplate) => (
      request('/templates', body({ workflow_path: workflowPath, expected_version: expectedVersion, template }))
    ),
    updateTemplate: (
      workflowPath: string,
      expectedVersion: number,
      templateId: string,
      patch: Pick<Partial<ApiTemplate>, 'name' | 'input_triggers' | 'output_triggers' | 'dependencies'>,
    ) => request(`/templates/${encodeURIComponent(templateId)}`, {
      method: 'PATCH',
      body: JSON.stringify({
        workflow_path: workflowPath,
        expected_version: expectedVersion,
        ...patch,
      }),
    }),
    deleteTemplate: (workflowPath: string, expectedVersion: number, templateId: string) => (
      request(`/templates/${encodeURIComponent(templateId)}?workflow_path=${encodeURIComponent(workflowPath)}&expected_version=${expectedVersion}`, { method: 'DELETE' })
    ),
    deleteTemplates: (workflowPath: string, expectedVersion: number, templateIds: string[]) => (
      request('/templates:delete', body({
        workflow_path: workflowPath,
        expected_version: expectedVersion,
        template_ids: templateIds,
      }))
    ),
    updateScheduledTemplates: (workflowPath: string, expectedVersion: number, templateIds: string[]) => (
      request('/workspaces/scheduled-templates', {
        method: 'PUT',
        body: JSON.stringify({
          workflow_path: workflowPath,
          expected_version: expectedVersion,
          template_ids: templateIds,
        }),
      })
    ),
    generateInstances: (
      workflowPath: string,
      expectedVersion: number,
      templateIds: string[],
      sampleIds: string[],
      sampleStartIntervalSeconds = 0,
      sampleTemplateNodeParameters: Record<
        string,
        Record<string, Record<string, Record<string, unknown>>>
      > = {},
    ) => (
      request('/instances:generate', body({
        workflow_path: workflowPath,
        expected_version: expectedVersion,
        template_ids: templateIds,
        sample_ids: sampleIds,
        sample_start_interval_seconds: sampleStartIntervalSeconds,
        sample_template_node_parameters: sampleTemplateNodeParameters,
      }))
    ),
    clearInstances: (workflowPath: string, expectedVersion: number) => (
      request('/instances:clear', body({
        workflow_path: workflowPath,
        expected_version: expectedVersion,
      }))
    ),
    resetInstancesProgress: (workflowPath: string, expectedVersion: number) => (
      request('/instances:reset-progress', body({
        workflow_path: workflowPath,
        expected_version: expectedVersion,
      }))
    ),
    failAction: (
      workflowPath: string,
      expectedVersion: number,
      instanceId: string,
      nodeId: string,
      executionId: string,
      error: Record<string, unknown>,
    ) => (
      request('/actions:fail', body({
        workflow_path: workflowPath,
        expected_version: expectedVersion,
        instance_id: instanceId,
        node_id: nodeId,
        execution_id: executionId,
        error,
      }))
    ),
    moveInstance: (workflowPath: string, expectedVersion: number, instanceId: string, order: number) => (
      request(`/instances/${encodeURIComponent(instanceId)}:move`, body({
        workflow_path: workflowPath,
        expected_version: expectedVersion,
        order,
      }))
    ),
    updateInstanceParameters: (
      workflowPath: string,
      expectedVersion: number,
      instanceId: string,
      nodeParameters: Record<string, Record<string, unknown>>,
    ) => request(`/instances/${encodeURIComponent(instanceId)}/parameters`, {
      method: 'PATCH',
      body: JSON.stringify({
        workflow_path: workflowPath,
        expected_version: expectedVersion,
        node_parameters: nodeParameters,
      }),
    }),
    plan: (
      workflowPath: string,
      expectedVersion: number,
      paused?: boolean,
      acknowledgePeerFailure?: boolean,
    ) => (
      request('/schedule:plan', body({
        workflow_path: workflowPath,
        expected_version: expectedVersion,
        ...(paused === undefined ? {} : { paused }),
        ...(acknowledgePeerFailure ? { acknowledge_peer_failure: true } : {}),
      }))
    ),
    advance: (workflowPath: string, expectedVersion: number, completedInstanceIds: string[] = []) => (
      request('/schedule:advance', body({
        workflow_path: workflowPath,
        expected_version: expectedVersion,
        completed_instance_ids: completedInstanceIds,
      }))
    ),
  };
}

type TaskExecutionCycleClient = {
  advance: (
    workflowPath: string,
    expectedVersion: number,
    completedInstanceIds?: string[],
  ) => Promise<ApiWorkspaceResponse>;
  getWorkspace: (workflowPath: string) => Promise<ApiWorkspaceResponse>;
};

type TaskExecutionCycleOptions = {
  fetcher: FetchLike;
  taskClient: TaskExecutionCycleClient;
  workflowPath: string;
  workflow: Record<string, unknown> | undefined;
  expectedVersion: number;
  signal?: AbortSignal;
  isCurrent?: () => boolean;
};

type BackendResult = {
  success?: boolean;
  active?: boolean | number;
  code?: string;
  message?: string;
  preflight?: unknown;
  in_flight?: number;
  claimed?: number;
  completed?: number;
  failed?: number;
};

let nextTaskExecutionTimingId = 0;
const taskExecutionTimingBuffers = new Map<number, Record<string, unknown>[]>();

function persistTaskExecutionTimings(
  workflowPath: string,
  entries: Record<string, unknown>[],
) {
  if (entries.length === 0 || typeof globalThis.fetch !== 'function') return;
  void globalThis.fetch('/api/task-execution/timings', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ task_workspace_path: workflowPath, entries }),
    keepalive: true,
  }).catch((error) => {
    console.warn('[task-execution-timing] 持久化失败', error);
  });
}

function taskExecutionTimingLog(
  cycleId: number,
  workflowPath: string,
  step: string,
  phase: 'start' | 'finish' | 'error' | 'skipped',
  startedAt?: number,
) {
  const now = Date.now();
  const entry = {
    cycle_id: cycleId,
    workflow_path: workflowPath,
    step,
    phase,
    timestamp: new Date(now).toISOString(),
    timestamp_ms: now,
    ...(startedAt === undefined ? {} : { duration_ms: now - startedAt }),
  };
  console.info('[task-execution-timing]', entry);
  const entries = taskExecutionTimingBuffers.get(cycleId) ?? [];
  entries.push(entry);
  taskExecutionTimingBuffers.set(cycleId, entries);
  if (step === 'cycle' || step === 'harvest_cycle') {
    if (phase === 'finish' || phase === 'error') {
      taskExecutionTimingBuffers.delete(cycleId);
      persistTaskExecutionTimings(workflowPath, entries);
    }
  }
}

async function timedTaskExecutionStep<T>(
  cycleId: number,
  workflowPath: string,
  step: string,
  operation: () => Promise<T>,
): Promise<T> {
  const startedAt = Date.now();
  taskExecutionTimingLog(cycleId, workflowPath, step, 'start');
  try {
    const result = await operation();
    taskExecutionTimingLog(cycleId, workflowPath, step, 'finish', startedAt);
    return result;
  } catch (error) {
    taskExecutionTimingLog(cycleId, workflowPath, step, 'error', startedAt);
    throw error;
  }
}

async function requestExecutionBackend(
  fetcher: FetchLike,
  path: '/api/task-opc/poll' | '/api/task-execution/tick',
  payload: {
    task_workspace_path: string;
    workflow?: Record<string, unknown>;
    harvest_only?: boolean;
  },
  failureLabel: string,
  signal?: AbortSignal,
): Promise<BackendResult> {
  try {
    const response = await fetcher(path, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload),
      signal,
    });
    let result: BackendResult | null = null;
    try {
      result = await response.json() as BackendResult;
    } catch (error) {
      if (error && typeof error === 'object' && 'name' in error && error.name === 'AbortError') {
        throw error;
      }
    }
    if (!response.ok || !result?.success) {
      const message = result?.message || '后端返回无效响应';
      const status = response.ok ? '' : `（HTTP ${response.status}）`;
      if (result?.code === 'task_dispatch_preflight_failed') {
        throw new TaskExecutionPreflightRejectedError(
          `${failureLabel}：${message}${status}`,
          result.preflight,
        );
      }
      throw new Error(`${failureLabel}：${message}${status}`);
    }
    return result;
  } catch (error) {
    if (error && typeof error === 'object' && 'name' in error && error.name === 'AbortError') {
      throw new TaskExecutionCycleCancelledError('Task 执行周期请求已取消', error);
    }
    throw error;
  }
}

async function runTaskExecutionCycleInternal({
  fetcher,
  taskClient,
  workflowPath,
  workflow,
  expectedVersion,
  signal,
  isCurrent,
}: TaskExecutionCycleOptions, cycleId: number): Promise<TaskExecutionCycleResult> {
  if (!workflow || typeof workflow !== 'object') {
    throw new Error('缺少当前 workflow JSON');
  }
  const assertCurrent = () => {
    if (signal?.aborted || isCurrent?.() === false) {
      throw new TaskExecutionCycleCancelledError();
    }
  };
  const payload = { task_workspace_path: workflowPath, workflow };
  assertCurrent();
  let workspaceSnapshot = await timedTaskExecutionStep(
    cycleId, workflowPath, 'get_workspace_initial',
    () => taskClient.getWorkspace(workflowPath),
  );
  assertCurrent();
  if (
    workspaceSnapshot.workspace.scheduler_paused
    || workspaceSnapshot.workspace.pause_reason
  ) {
    return {
      active: false,
      workspace: workspaceSnapshot,
      tick: null,
    };
  }
  const poll = await timedTaskExecutionStep(
    cycleId, workflowPath, 'opc_poll',
    () => requestExecutionBackend(
      fetcher,
      '/api/task-opc/poll',
      payload,
      'Task OPC 采样失败',
      signal,
    ),
  );
  assertCurrent();
  if (!poll.active) {
    assertCurrent();
    const workspace = await timedTaskExecutionStep(
      cycleId, workflowPath, 'get_workspace_inactive',
      () => taskClient.getWorkspace(workflowPath),
    );
    assertCurrent();
    return {
      active: false,
      workspace,
      tick: null,
    };
  }

  try {
    await timedTaskExecutionStep(
      cycleId, workflowPath, 'advance',
      () => taskClient.advance(workflowPath, expectedVersion),
    );
  } catch (error) {
    if (!(error instanceof TaskOrchestrationBusinessError) || error.status !== 409) {
      throw error;
    }
    assertCurrent();
    workspaceSnapshot = await timedTaskExecutionStep(
      cycleId, workflowPath, 'get_workspace_after_conflict',
      () => taskClient.getWorkspace(workflowPath),
    );
    assertCurrent();
    if (
      workspaceSnapshot.workspace.scheduler_paused
      || workspaceSnapshot.workspace.pause_reason
    ) {
      return {
        active: false,
        workspace: workspaceSnapshot,
        tick: null,
      };
    }
    await timedTaskExecutionStep(
      cycleId, workflowPath, 'advance_after_conflict',
      () => taskClient.advance(workflowPath, workspaceSnapshot.version),
    );
  }
  assertCurrent();
  const tick = await timedTaskExecutionStep(
    cycleId, workflowPath, 'execution_tick',
    () => requestExecutionBackend(
      fetcher,
      '/api/task-execution/tick',
      payload,
      'Task action tick 失败',
      signal,
    ),
  );
  assertCurrent();
  const workspace = await timedTaskExecutionStep(
    cycleId, workflowPath, 'get_workspace_final',
    () => taskClient.getWorkspace(workflowPath),
  );
  assertCurrent();
  return {
    active: true,
    workspace,
    tick: {
      active: Number(tick.active || 0),
      in_flight: Number(tick.in_flight || 0),
      claimed: Number(tick.claimed || 0),
      completed: Number(tick.completed || 0),
      failed: Number(tick.failed || 0),
    },
  };
}

export async function runTaskExecutionCycle(
  options: TaskExecutionCycleOptions,
): Promise<TaskExecutionCycleResult> {
  const cycleId = ++nextTaskExecutionTimingId;
  return timedTaskExecutionStep(
    cycleId,
    options.workflowPath,
    'cycle',
    () => runTaskExecutionCycleInternal(options, cycleId),
  );
}

type TaskExecutionHarvestClient = {
  getWorkspace: (workflowPath: string) => Promise<ApiWorkspaceResponse>;
};

type TaskExecutionHarvestOptions = {
  fetcher: FetchLike;
  taskClient: TaskExecutionHarvestClient;
  workflowPath: string;
  signal?: AbortSignal;
  isCurrent?: () => boolean;
};

async function runTaskExecutionHarvestCycleInternal({
  fetcher,
  taskClient,
  workflowPath,
  signal,
  isCurrent,
}: TaskExecutionHarvestOptions, cycleId: number): Promise<TaskExecutionCycleResult> {
  const assertCurrent = () => {
    if (signal?.aborted || isCurrent?.() === false) {
      throw new TaskExecutionCycleCancelledError();
    }
  };
  assertCurrent();
  const tick = await timedTaskExecutionStep(
    cycleId, workflowPath, 'harvest_execution_tick',
    () => requestExecutionBackend(
      fetcher,
      '/api/task-execution/tick',
      { task_workspace_path: workflowPath, harvest_only: true },
      'Task action tick 失败',
      signal,
    ),
  );
  assertCurrent();
  const workspace = await timedTaskExecutionStep(
    cycleId, workflowPath, 'harvest_get_workspace',
    () => taskClient.getWorkspace(workflowPath),
  );
  assertCurrent();
  const stats = {
    active: Number(tick.active || 0),
    in_flight: Number(tick.in_flight || 0),
    claimed: Number(tick.claimed || 0),
    completed: Number(tick.completed || 0),
    failed: Number(tick.failed || 0),
  };
  return { active: stats.in_flight > 0, workspace, tick: stats };
}

export async function runTaskExecutionHarvestCycle(
  options: TaskExecutionHarvestOptions,
): Promise<TaskExecutionCycleResult> {
  const cycleId = ++nextTaskExecutionTimingId;
  return timedTaskExecutionStep(
    cycleId,
    options.workflowPath,
    'harvest_cycle',
    () => runTaskExecutionHarvestCycleInternal(options, cycleId),
  );
}

type TaskSchedulerPlanClient = {
  plan: (
    workflowPath: string,
    expectedVersion: number,
    paused?: boolean,
  ) => Promise<ApiWorkspaceResponse>;
  getWorkspace: (workflowPath: string) => Promise<ApiWorkspaceResponse>;
};

export async function pauseTaskSchedulerReliably({
  taskClient,
  workflowPath,
  expectedVersion,
}: {
  taskClient: TaskSchedulerPlanClient;
  workflowPath: string;
  expectedVersion: number;
}): Promise<ApiWorkspaceResponse> {
  try {
    return await taskClient.plan(workflowPath, expectedVersion, true);
  } catch (error) {
    if (!(error instanceof TaskOrchestrationBusinessError) || error.status !== 409) {
      throw error;
    }
  }
  const latest = await taskClient.getWorkspace(workflowPath);
  return taskClient.plan(workflowPath, latest.version, true);
}

export async function runTaskSchedulerTransition(
  lock: { current: boolean },
  onTransitioningChange: (transitioning: boolean) => void,
  operation: () => Promise<void>,
): Promise<boolean> {
  if (lock.current) return false;
  lock.current = true;
  onTransitioningChange(true);
  try {
    await operation();
    return true;
  } finally {
    lock.current = false;
    onTransitioningChange(false);
  }
}

type TaskExecutionControllerCycleOptions = {
  workflowPath: string;
  workflow?: Record<string, unknown>;
  expectedVersion: number;
  signal: AbortSignal;
  isCurrent: () => boolean;
};

type TaskExecutionControllerOptions = {
  runCycle: (options: TaskExecutionControllerCycleOptions) => Promise<TaskExecutionCycleResult>;
  runHarvestCycle?: (options: TaskExecutionControllerCycleOptions) => Promise<TaskExecutionCycleResult>;
  applyWorkspace: (workspace: ApiWorkspaceResponse) => void;
  pauseScheduler: (workflowPath: string, expectedVersion: number) => Promise<ApiWorkspaceResponse>;
  onStatus: (status: TaskExecutionStatus) => void;
  onError: (message: string) => void;
  onPreflightRejected?: (preflight: unknown, message: string) => void;
  onDrainingChange?: (draining: boolean) => void;
};

export function createTaskExecutionController({
  runCycle,
  runHarvestCycle,
  applyWorkspace,
  pauseScheduler,
  onStatus,
  onError,
  onPreflightRejected,
  onDrainingChange,
}: TaskExecutionControllerOptions) {
  type Generation = {
    id: number;
    mode: 'active' | 'harvest' | 'transition';
    running: boolean;
    inFlight: boolean;
    abortController: AbortController | null;
  };
  let nextGeneration = 0;
  let currentGeneration: Generation | null = null;
  let lastTick = { ...EMPTY_TASK_EXECUTION_TICK };
  let lastWorkspace: ApiWorkspaceResponse | null = null;
  let fatalError: string | null = null;

  const isCurrent = (generation: Generation) => (
    currentGeneration === generation && generation.running
  );
  const stopGeneration = (generation: Generation | null) => {
    if (!generation) return;
    generation.running = false;
    generation.abortController?.abort();
    generation.abortController = null;
    if (currentGeneration === generation) currentGeneration = null;
  };
  const beginGeneration = (mode: Generation['mode']) => {
    stopGeneration(currentGeneration);
    const generation: Generation = {
      id: ++nextGeneration,
      mode,
      running: true,
      inFlight: false,
      abortController: null,
    };
    currentGeneration = generation;
    return generation;
  };
  const finishPausedTransition = (
    generation: Generation,
    workspace: ApiWorkspaceResponse,
    phase?: TaskExecutionPhase,
  ) => {
    if (!isCurrent(generation)) throw new TaskExecutionCycleCancelledError();
    applyWorkspace(workspace);
    lastWorkspace = workspace;
    generation.mode = 'harvest';
    generation.inFlight = false;
    onDrainingChange?.(true);
    onStatus(createTaskExecutionStatus(lastTick, workspace, phase));
    return true;
  };
  const hasUnfinishedInstances = (workspace: ApiWorkspaceResponse) => (
    workspace.workspace.task_instances.some((instance) => (
      instance.status === 'waiting'
      || instance.status === 'pending'
      || instance.status === 'running'
    ))
  );

  const controller = {
    start() {
      lastTick = { ...EMPTY_TASK_EXECUTION_TICK };
      lastWorkspace = null;
      fatalError = null;
      beginGeneration('active');
      onDrainingChange?.(false);
      onStatus(createTaskExecutionStatus(lastTick, null, 'dispatching'));
    },
    pause() {
      stopGeneration(currentGeneration);
      onDrainingChange?.(false);
    },
    isRunning() {
      return Boolean(currentGeneration?.running);
    },
    async pauseAndDrain({
      workflowPath,
      expectedVersion,
    }: {
      workflowPath: string;
      expectedVersion: number;
    }) {
      const generation = beginGeneration('transition');
      try {
        const workspace = await pauseScheduler(workflowPath, expectedVersion);
        return finishPausedTransition(generation, workspace);
      } catch (error) {
        if (error instanceof TaskExecutionCycleCancelledError) return false;
        stopGeneration(generation);
        onDrainingChange?.(false);
        throw error;
      }
    },
    async run(options: Omit<TaskExecutionControllerCycleOptions, 'signal' | 'isCurrent'>) {
      const generation = currentGeneration;
      if (!generation?.running || generation.inFlight || generation.mode === 'transition') {
        const timestampMs = Date.now();
        const skippedTiming = {
          generation_id: generation?.id ?? null,
          workflow_path: options.workflowPath,
          step: 'controller_run',
          phase: 'skipped',
          reason: !generation?.running
            ? 'not_running'
            : generation.inFlight
              ? 'in_flight'
              : 'transition',
          timestamp: new Date(timestampMs).toISOString(),
          timestamp_ms: timestampMs,
        };
        console.info('[task-execution-timing]', skippedTiming);
        persistTaskExecutionTimings(options.workflowPath, [skippedTiming]);
        return false;
      }
      generation.abortController = new AbortController();
      generation.inFlight = true;
      try {
        const runner = generation.mode === 'harvest' ? runHarvestCycle : runCycle;
        if (!runner) throw new Error('缺少 Task harvest-only 周期实现');
        const result = await runner({
          ...options,
          signal: generation.abortController.signal,
          isCurrent: () => isCurrent(generation),
        });
        if (!isCurrent(generation)) throw new TaskExecutionCycleCancelledError();
        applyWorkspace(result.workspace);
        lastWorkspace = result.workspace;
        if (result.tick) lastTick = result.tick;
        if (generation.mode === 'harvest') {
          if (lastTick.in_flight === 0) {
            stopGeneration(generation);
            onDrainingChange?.(false);
          }
          onStatus(createTaskExecutionStatus(
            lastTick,
            result.workspace,
            fatalError ? 'failed' : undefined,
          ));
          if (fatalError) onError(fatalError);
          return true;
        }
        fatalError = null;
        if (
          !result.active
          && (
            result.workspace.workspace.scheduler_paused
            || Boolean(result.workspace.workspace.pause_reason)
          )
        ) {
          finishPausedTransition(generation, result.workspace);
          return true;
        }
        if (!result.active && !hasUnfinishedInstances(result.workspace)) {
          const pausedWorkspace = await pauseScheduler(
            options.workflowPath,
            result.workspace.version,
          );
          finishPausedTransition(generation, pausedWorkspace);
          return true;
        }
        onStatus(createTaskExecutionStatus(lastTick, result.workspace));
        return true;
      } catch (error) {
        if (error instanceof TaskExecutionCycleCancelledError) return false;
        const message = error instanceof Error ? error.message : 'Task 执行循环失败';
        fatalError = message;
        if (
          generation.mode === 'active'
          && error instanceof TaskExecutionPreflightRejectedError
        ) {
          onPreflightRejected?.(error.preflight, message);
          try {
            const pausedWorkspace = await pauseScheduler(
              options.workflowPath,
              lastWorkspace?.version ?? options.expectedVersion,
            );
            finishPausedTransition(generation, pausedWorkspace, 'failed');
            onError(message);
            return false;
          } catch (pauseError) {
            if (pauseError instanceof TaskExecutionCycleCancelledError) return false;
            const pauseMessage = pauseError instanceof Error
              ? pauseError.message
              : '未知错误';
            stopGeneration(generation);
            onDrainingChange?.(false);
            onStatus(createTaskExecutionStatus(lastTick, lastWorkspace, 'failed'));
            onError(`${message}；自动暂停失败：${pauseMessage}`);
            return false;
          }
        }
        if (generation.mode === 'harvest') {
          stopGeneration(generation);
          onDrainingChange?.(false);
          onStatus(createTaskExecutionStatus(lastTick, lastWorkspace, 'failed'));
          onError(message);
          return false;
        }
        // 普通轮询/网络异常不应改变服务端调度状态；保留当前 generation，
        // 由下一次定时周期自动重试。明确的动作失败由服务端 pause_reason 负责暂停。
        onStatus(createTaskExecutionStatus(lastTick, lastWorkspace, 'failed'));
        onError(message);
        return false;
      } finally {
        generation.inFlight = false;
        if (generation.abortController?.signal.aborted) {
          generation.abortController = null;
        }
      }
    },
  };
  return controller;
}
