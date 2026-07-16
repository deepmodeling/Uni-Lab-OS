import type { TriggerCondition } from './taskOrchestration';

export type ApiTrigger = {
  kind: 'opc' | 'internal' | 'resource' | 'workstation';
  config: {
    provider_id?: string;
    variable?: string;
    key?: string;
    resource?: string;
    workstation?: string;
    event?: string;
    value?: string | number | boolean;
  };
};

export type ApiTemplate = {
  id: string;
  name: string;
  workflow_path: string;
  node_ids: string[];
  resources: string[];
  trigger: ApiTrigger | null;
  input_triggers: ApiTrigger[];
  output_triggers: ApiTrigger[];
};

export type ApiTaskInstance = {
  id: string;
  template_id: string;
  status: 'waiting' | 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';
  sample_id: string;
  order: number;
  started_at: number | null;
  finished_at: number | null;
};

export type ApiWorkspaceEvent = {
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
  schedule_entries: ApiScheduleEntry[];
  // 服务端公开响应只提供快照元数据，前端绝不接收或处理 raw OPC values。
  opc_snapshots: Array<{
    provider_id: string;
    sequence: number;
    variable_count: number;
    updated_at_by_variable: Record<string, number>;
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

export function resolveTaskOrchestrationUiToken(
  env: Record<string, string | undefined> = import.meta.env || {},
  location: Pick<Location, 'hostname' | 'port'> | undefined = globalThis.location,
) {
  if (env.VITE_TASK_ORCHESTRATION_UI_TOKEN) {
    return env.VITE_TASK_ORCHESTRATION_UI_TOKEN;
  }
  return location?.hostname === '127.0.0.1' && location.port === '8014'
    ? 'local-task-ui-dev'
    : undefined;
}

export function toApiTrigger(condition: TriggerCondition): ApiTrigger {
  const resource = condition.variableName.match(/^(?:系统资源可用|资源锁可获取)：(.+)$/)?.[1];
  if (resource) {
    return {
      kind: 'resource',
      config: { resource },
    };
  }
  const workstation = condition.variableName.match(/^(?:系统工位可用|工位条件满足)：(.+)$/)?.[1];
  if (workstation) {
    return {
      kind: 'workstation',
      config: { workstation },
    };
  }
  const releasedResource = condition.variableName.match(/^(?:系统资源释放|资源锁释放)：(.+)$/)?.[1];
  if (releasedResource) {
    return {
      kind: 'resource',
      config: { resource: releasedResource, event: 'released' },
    };
  }
  const completedWorkstation = condition.variableName.match(/^(?:系统工位完成|工位任务完成)：(.+)$/)?.[1];
  if (completedWorkstation) {
    return {
      kind: 'workstation',
      config: { workstation: completedWorkstation, event: 'completed' },
    };
  }
  if (condition.variableName.startsWith('业务内部：')) {
    return {
      kind: 'internal',
      config: { key: condition.variableName.slice('业务内部：'.length), value: condition.value },
    };
  }
  return {
    kind: 'opc',
    config: {
      provider_id: 'default',
      variable: condition.variableName,
      value: condition.value,
    },
  };
}

export function fromApiTrigger(trigger: ApiTrigger): TriggerCondition {
  const value = trigger.config.value ?? true;
  return {
    variableName: String(
      trigger.kind === 'internal'
        ? `业务内部：${trigger.config.key || ''}`
        : trigger.kind === 'resource'
          ? `${trigger.config.event ? '系统资源释放' : '系统资源可用'}：${trigger.config.resource || ''}`
          : trigger.kind === 'workstation'
            ? `${trigger.config.event ? '系统工位完成' : '系统工位可用'}：${trigger.config.workstation || ''}`
            : trigger.config.variable || '',
    ),
    dataType: typeof value === 'boolean' ? 'BOOL' : typeof value === 'number' ? 'FLOAT' : 'STRING',
    value,
  };
}

type FetchLike = typeof fetch;

type ClientOptions = {
  baseUrl?: string;
  fetchImpl?: FetchLike;
  uiToken?: string;
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
    && Array.isArray(value.opc_snapshots);
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
  const uiToken = options.uiToken ?? resolveTaskOrchestrationUiToken();

  const request = async (
    path: string,
    init: RequestInit = {},
  ) => {
    try {
      return await parseResponse(await fetchImpl(`${baseUrl}${path}`, {
        ...init,
        headers: {
          'content-type': 'application/json',
          ...(uiToken ? { Authorization: `Bearer ${uiToken}` } : {}),
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
    getWorkspace: (workflowPath: string) => request(`/workspaces?workflow_path=${encodeURIComponent(workflowPath)}`, { method: 'GET' }),
    createTemplate: (workflowPath: string, expectedVersion: number, template: ApiTemplate) => (
      request('/templates', body({ workflow_path: workflowPath, expected_version: expectedVersion, template }))
    ),
    updateTemplate: (
      workflowPath: string,
      expectedVersion: number,
      templateId: string,
      patch: Pick<Partial<ApiTemplate>, 'name' | 'trigger' | 'input_triggers' | 'output_triggers'>,
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
    generateInstances: (workflowPath: string, expectedVersion: number, templateIds: string[], sampleIds: string[]) => (
      request('/instances:generate', body({
        workflow_path: workflowPath,
        expected_version: expectedVersion,
        template_ids: templateIds,
        sample_ids: sampleIds,
      }))
    ),
    moveInstance: (workflowPath: string, expectedVersion: number, instanceId: string, order: number) => (
      request(`/instances/${encodeURIComponent(instanceId)}:move`, body({
        workflow_path: workflowPath,
        expected_version: expectedVersion,
        order,
      }))
    ),
    plan: (workflowPath: string, expectedVersion: number, paused?: boolean) => (
      request('/schedule:plan', body({
        workflow_path: workflowPath,
        expected_version: expectedVersion,
        ...(paused === undefined ? {} : { paused }),
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
