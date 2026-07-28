export type JsonScalar = string | number | boolean | null;
export const DEFAULT_OPC_SIMULATOR_URL = 'opc.tcp://127.0.0.1:4840';
export const OPC_PROFILE_FORCE_REVISION = '*';
export const OPC_REFERENCE_TEMPLATE_FILE = 'szlab_task_opc_simulator.json';

export function defaultOpcSimulatorFileName(workflowName: string) {
  const safeStem = (workflowName || 'opc-simulator-profile')
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 64) || 'opc-simulator-profile';
  return `${safeStem}-opc-simulator.json`;
}

export function safeOpcProfileFileName(rawName: string, fallbackStem: string) {
  const trimmed = rawName.trim();
  const withoutExt = trimmed.toLowerCase().endsWith('.json')
    ? trimmed.slice(0, -5)
    : trimmed;
  const safeStem = withoutExt
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 64) || fallbackStem;
  return `${safeStem}.json`;
}
export type OpcVariableDirection = 'pc_to_plc' | 'plc_to_pc' | 'unknown';
export type OpcVariableDataType = 'bool' | 'int' | 'float' | 'string' | 'unknown';
export type OpcVariableSource = 'action_node' | 'action_sensor' | 'task_input' | 'task_output' | 'manual';

export type OpcSimulatorVariable = {
  name: string;
  direction: OpcVariableDirection;
  data_type: OpcVariableDataType;
  initial_value?: JsonScalar;
  source: OpcVariableSource;
};

export type OpcSimulatorCondition = {
  variable: string;
  operator: 'eq';
  value: JsonScalar;
  edge: 'rising' | 'falling' | 'level';
};

export type OpcSimulatorWrite = {
  variable: string;
  value: JsonScalar;
};

export type OpcSimulatorConditionGroup = { all: OpcSimulatorCondition[] };
export type OpcSimulatorPhase = { writes: OpcSimulatorWrite[] };
export type OpcSimulatorDelayedPhase = OpcSimulatorPhase & { delay: number };

export type OpcSimulatorNode = {
  workflow_node_id: string;
  task_template_ids: string[];
  device_id: string;
  method: string;
  params: Record<string, unknown>;
  channel: string;
  trigger: OpcSimulatorConditionGroup;
  on_trigger: OpcSimulatorPhase;
  on_complete: OpcSimulatorDelayedPhase;
  reset_when: OpcSimulatorConditionGroup | null;
  after_reset: OpcSimulatorDelayedPhase | null;
};

export type OpcSimulatorProfile = {
  schema_version: 2;
  status: 'draft' | 'runnable';
  name: string;
  opc: {
    url: string;
    poll_interval: number;
    io_timeout: number;
  };
  variables: OpcSimulatorVariable[];
  nodes: OpcSimulatorNode[];
};

export type OpcProfileValidationError = {
  path: string;
  message: string;
  nodeId?: string;
};

export type OpcProfileApiResponse = {
  profile: OpcSimulatorProfile;
  validation_errors: string[];
  file_name: string;
  revision?: string;
};

export type OpcActionCatalogEntry = {
  device_id: string;
  method: string;
  opc_variables: string[];
};

export type OpcSimulatorState =
  | 'idle'
  | 'starting'
  | 'running'
  | 'stopping'
  | 'stopped'
  | 'failed';

export type OpcSimulatorStatus = {
  state: OpcSimulatorState;
  pid: number | null;
  file_name: string | null;
  revision: string | null;
  run_id: string | null;
  opc_url: string | null;
  started_at: number | null;
  ended_at: number | null;
  ended_monotonic: number | null;
  elapsed_seconds: number;
  return_code: number | null;
  restore_status: 'not_started' | 'pending' | 'succeeded' | 'error' | 'uncertain' | string;
  last_error: string | null;
  recent_logs: string[];
};

export function opcSimulatorStopMessage(status: OpcSimulatorStatus) {
  if (status.state === 'stopped' && status.restore_status === 'succeeded') {
    return '模拟器已停止并完成恢复';
  }
  if (status.state === 'stopping' || status.restore_status === 'pending') {
    return '模拟器正在停止，仍在恢复 OPC 状态，请等待';
  }
  const detail = status.last_error ? `：${status.last_error}` : '';
  return `停止未安全完成，恢复状态为 ${status.restore_status}；已阻断再次启动${detail}`;
}

export class OpcSimulatorHttpError extends Error {
  constructor(message: string, readonly status: number, readonly payload: unknown) {
    super(message);
    this.name = 'OpcSimulatorHttpError';
  }
}

type FetchLike = typeof fetch;
type ConditionField = 'trigger' | 'reset_when';
type PhaseField = 'on_trigger' | 'on_complete' | 'after_reset';
const MAX_ADVANCED_JSON_BYTES = 2 * 1024 * 1024;
const MAX_ADVANCED_JSON_DEPTH = 100;
const MAX_PROFILE_COLLECTION_ITEMS = 500;
const MAX_PARAMS_DEPTH = 20;
const MAX_PARAMS_SCALARS = 10_000;
const DANGEROUS_KEYS = new Set(['__proto__', 'prototype', 'constructor']);
const ROOT_KEYS = new Set(['schema_version', 'status', 'name', 'opc', 'variables', 'nodes']);
const VARIABLE_KEYS = new Set(['name', 'direction', 'data_type', 'initial_value', 'source']);
const NODE_KEYS = new Set([
  'workflow_node_id', 'task_template_ids', 'device_id', 'method', 'params',
  'channel', 'trigger', 'on_trigger', 'on_complete', 'reset_when', 'after_reset',
]);
const CONDITION_KEYS = new Set(['variable', 'operator', 'value', 'edge']);
const WRITE_KEYS = new Set(['variable', 'value']);

export function buildOpcVariableTypeCatalog(
  variables: Array<{ name: string; data_type: string }>,
  referencedNames?: ReadonlySet<string> | readonly string[],
) {
  const referenceFilter = referencedNames instanceof Set
    ? referencedNames
    : referencedNames
      ? new Set(referencedNames)
      : null;
  const canonicalType = (value: string) => {
    const normalized = value.trim().toUpperCase();
    if (normalized === 'BOOL' || normalized === 'BOOLEAN') return 'bool';
    if (normalized === 'INT' || normalized === 'INTEGER') return 'int';
    if (normalized === 'FLOAT' || normalized === 'DOUBLE' || normalized === 'NUMBER') return 'float';
    if (normalized === 'STRING') return 'string';
    return null;
  };
  const counts = new Map<string, number>();
  variables.forEach((variable) => {
    if (typeof variable.name === 'string' && variable.name.trim()) {
      const name = variable.name.trim();
      counts.set(name, (counts.get(name) || 0) + 1);
    }
  });
  return variables.flatMap((variable) => {
    const name = typeof variable.name === 'string' ? variable.name.trim() : '';
    const dataType = typeof variable.data_type === 'string'
      ? canonicalType(variable.data_type)
      : null;
    if (referenceFilter && !referenceFilter.has(name)) return [];
    return name && counts.get(name) === 1 && dataType
      ? [{ name, data_type: dataType }]
      : [];
  });
}

type ApiTriggerLike = {
  kind?: unknown;
  config?: {
    variable?: unknown;
    value?: unknown;
    plc_device_id?: unknown;
  };
};

type WorkflowNodeLike = {
  workflow_node_id?: unknown;
  uuid?: unknown;
  id?: unknown;
  device_id?: unknown;
  method?: unknown;
  opc_variables?: unknown;
  data?: {
    opc_variables?: unknown;
  };
};

type TemplateLike = {
  id?: unknown;
  nodeIds?: unknown;
  node_ids?: unknown;
  inputTriggers?: Array<{ variableName?: unknown }>;
  input_triggers?: ApiTriggerLike[];
  outputTriggers?: Array<{ variableName?: unknown }>;
  output_triggers?: ApiTriggerLike[];
};

function readWorkflowNodeId(node: WorkflowNodeLike) {
  for (const key of ['workflow_node_id', 'uuid', 'id'] as const) {
    const value = node[key];
    if (typeof value === 'string' && value.trim()) return value.trim();
  }
  return '';
}

function readStringList(value: unknown) {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => (
    typeof item === 'string' && item.trim() ? [item.trim()] : []
  ));
}

function readNodeOpcVariables(node: WorkflowNodeLike) {
  const direct = readStringList(node.opc_variables);
  if (direct.length) return direct;
  return readStringList(node.data?.opc_variables);
}

function readTemplateNodeIds(template: TemplateLike) {
  return readStringList(template.nodeIds ?? template.node_ids);
}

function readTriggerVariableNames(triggers: unknown) {
  if (!Array.isArray(triggers)) return [];
  return triggers.flatMap((trigger) => {
    if (!trigger || typeof trigger !== 'object') return [];
    const config = (trigger as ApiTriggerLike).config;
    const variable = config?.variable;
    return typeof variable === 'string' && variable.trim() ? [variable.trim()] : [];
  });
}

export function collectOpcProfileVariableNames(options: {
  workflow: unknown;
  templates: TemplateLike[];
  scheduledTemplateIds: readonly string[];
  actionCatalog: OpcActionCatalogEntry[];
  templateTriggers?: Array<Pick<TemplateLike, 'inputTriggers' | 'outputTriggers' | 'input_triggers' | 'output_triggers'>>;
}) {
  const names = new Set<string>();
  const scheduledIds = new Set(options.scheduledTemplateIds);
  const catalogByKey = new Map(
    options.actionCatalog.map((entry) => [`${entry.device_id}\0${entry.method}`, entry]),
  );
  const workflowRoot = options.workflow && typeof options.workflow === 'object'
    ? options.workflow as { nodes?: unknown; rules?: unknown }
    : null;
  const workflowNodes = Array.isArray(workflowRoot?.nodes)
    ? workflowRoot.nodes as WorkflowNodeLike[]
    : [];
  const workflowNodeById = new Map(
    workflowNodes.flatMap((node) => {
      const nodeId = readWorkflowNodeId(node);
      return nodeId ? [[nodeId, node] as const] : [];
    }),
  );

  options.templates.forEach((template, index) => {
    const templateId = typeof template.id === 'string' ? template.id : '';
    if (!scheduledIds.has(templateId)) return;
    readTemplateNodeIds(template).forEach((nodeId) => {
      const workflowNode = workflowNodeById.get(nodeId);
      if (workflowNode) {
        readNodeOpcVariables(workflowNode).forEach((name) => names.add(name));
        const deviceId = typeof workflowNode.device_id === 'string' ? workflowNode.device_id.trim() : '';
        const method = typeof workflowNode.method === 'string' ? workflowNode.method.trim() : '';
        catalogByKey.get(`${deviceId}\0${method}`)?.opc_variables.forEach((name) => names.add(name));
      }
    });
    const triggerSource = options.templateTriggers?.[index] || template;
    [
      ...readTriggerVariableNames(triggerSource.inputTriggers?.map((trigger) => ({
        kind: 'opc',
        config: { variable: trigger.variableName },
      }))),
      ...readTriggerVariableNames(triggerSource.outputTriggers?.map((trigger) => ({
        kind: 'opc',
        config: { variable: trigger.variableName },
      }))),
      ...readTriggerVariableNames(triggerSource.input_triggers),
      ...readTriggerVariableNames(triggerSource.output_triggers),
    ].forEach((name) => names.add(name));
  });

  return names;
}

export function formatOpcProfileGenerateError(
  status: number,
  detail: unknown,
  fallback = '生成 OPC 模拟配置失败',
) {
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (detail && typeof detail === 'object') {
    const validationErrors = (detail as { validation_errors?: unknown }).validation_errors;
    if (Array.isArray(validationErrors) && validationErrors.length) {
      return `生成失败（HTTP ${status}）：${validationErrors.slice(0, 5).join('、')}`;
    }
    const nestedErrors = (detail as { detail?: { validation_errors?: unknown } }).detail?.validation_errors;
    if (Array.isArray(nestedErrors) && nestedErrors.length) {
      return `生成失败（HTTP ${status}）：${nestedErrors.slice(0, 5).join('、')}`;
    }
  }
  return `生成失败（HTTP ${status}）`;
}

export function buildOpcActionCatalog(
  actions: Array<{
    device_id?: unknown;
    method?: unknown;
    opc_variables?: unknown;
  }>,
): OpcActionCatalogEntry[] {
  return actions.map((action, index) => {
    const method = typeof action.method === 'string' ? action.method.trim() : '';
    if (!method) {
      throw new Error(`动作 ${index + 1} 缺少 method，无法生成 OPC 模拟配置`);
    }
    const deviceId = typeof action.device_id === 'string' ? action.device_id.trim() : '';
    if (!deviceId) {
      throw new Error(`动作 ${method} 缺少 device_id，无法生成 OPC 模拟配置`);
    }
    const rawOpcVariables = action.opc_variables ?? [];
    if (!Array.isArray(rawOpcVariables)) {
      throw new Error(`动作 ${method} 的 opc_variables 不是数组`);
    }
    const opcVariables: string[] = [];
    rawOpcVariables.forEach((value) => {
      if (typeof value !== 'string' || !value.trim()) {
        throw new Error(`动作 ${method} 的 opc_variables 含空值`);
      }
      const variable = value.trim();
      if (!opcVariables.includes(variable)) opcVariables.push(variable);
    });
    return {
      device_id: deviceId,
      method,
      opc_variables: opcVariables,
    };
  });
}

type LatestOperation = { generation: number; signal: AbortSignal };
type MutableRef<T> = { current: T };

export function beginOpcSimulatorControlOperation(
  inFlightRef: MutableRef<boolean>,
  tokenRef: MutableRef<number>,
) {
  if (inFlightRef.current) return null;
  inFlightRef.current = true;
  tokenRef.current += 1;
  return tokenRef.current;
}

export function finishOpcSimulatorControlOperation(
  inFlightRef: MutableRef<boolean>,
  tokenRef: MutableRef<number>,
  operation: number,
) {
  if (operation !== tokenRef.current) return false;
  inFlightRef.current = false;
  return true;
}

export function createLatestOperationGate() {
  let generation = 0;
  let controller: AbortController | null = null;
  let mounted = true;
  return {
    begin(): LatestOperation {
      controller?.abort();
      controller = new AbortController();
      return { generation: ++generation, signal: controller.signal };
    },
    invalidate() {
      generation += 1;
      controller?.abort();
      controller = null;
    },
    isCurrent(candidate: number) {
      return mounted && candidate === generation;
    },
    mount() {
      mounted = true;
    },
    unmount() {
      mounted = false;
      generation += 1;
      controller?.abort();
      controller = null;
    },
  };
}

export type ProfileSaveSnapshot = {
  canonical: string;
  fileName: string;
  status: OpcSimulatorProfile['status'];
  profile: OpcSimulatorProfile;
};

export function createProfileSaveSnapshot(
  profile: OpcSimulatorProfile,
  status: OpcSimulatorProfile['status'],
  fileName: string,
): ProfileSaveSnapshot {
  const requestProfile = { ...profile, status };
  return {
    canonical: canonicalProfileJson(requestProfile),
    fileName,
    status,
    profile: requestProfile,
  };
}

export function isProfileSaveSnapshotCurrent(
  snapshot: ProfileSaveSnapshot,
  profile: OpcSimulatorProfile,
  fileName: string,
) {
  return fileName === snapshot.fileName
    && canonicalProfileJson({ ...profile, status: snapshot.status }) === snapshot.canonical;
}

export function collectScheduledTemplateIds(ids: string[]) {
  return Array.from(new Set(ids.map((id) => id.trim()).filter(Boolean)));
}

export function parseJsonScalar(text: string): JsonScalar {
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    throw new Error('请输入严格 JSON scalar');
  }
  if (
    value === null
    || typeof value === 'string'
    || typeof value === 'boolean'
    || (typeof value === 'number' && Number.isFinite(value))
  ) return value;
  throw new Error('请输入严格 JSON scalar');
}

export function formatJsonScalar(value: JsonScalar) {
  return JSON.stringify(value);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function assertJsonData(
  value: unknown,
  path = '$',
  ancestors = new WeakSet<object>(),
): void {
  if (
    value === null
    || typeof value === 'string'
    || typeof value === 'boolean'
    || (typeof value === 'number' && Number.isFinite(value))
  ) return;
  if (typeof value !== 'object' || value === null) {
    throw new Error(`${path} 不是 JSON data`);
  }
  if (ancestors.has(value)) throw new Error(`${path} 不是 JSON data：存在循环引用`);
  const prototype = Object.getPrototypeOf(value);
  if (!Array.isArray(value) && prototype !== Object.prototype && prototype !== null) {
    throw new Error(`${path} 不是 JSON data`);
  }
  ancestors.add(value);
  if (Array.isArray(value)) {
    value.forEach((item, index) => assertJsonData(item, `${path}[${index}]`, ancestors));
  } else {
    for (const [key, descriptor] of Object.entries(Object.getOwnPropertyDescriptors(value))) {
      if (DANGEROUS_KEYS.has(key)) throw new Error(`${path}.${key} 是禁止字段`);
      if (descriptor.get || descriptor.set) throw new Error(`${path}.${key} 不是 JSON data`);
      assertJsonData(descriptor.value, `${path}.${key}`, ancestors);
    }
  }
  ancestors.delete(value);
}

export function canonicalProfileJson(profile: OpcSimulatorProfile) {
  assertJsonData(profile);
  return `${JSON.stringify(profile, null, 2)}\n`;
}

export function createCanonicalProfileBlob(profile: OpcSimulatorProfile) {
  return new Blob([canonicalProfileJson(profile)], {
    type: 'application/json;charset=utf-8',
  });
}

function assertAdvancedJsonTextLimits(text: string) {
  if (new TextEncoder().encode(text).byteLength > MAX_ADVANCED_JSON_BYTES) {
    throw new Error('高级 JSON 超过 2 MiB');
  }
  let depth = 0;
  let inString = false;
  let escaped = false;
  for (const character of text) {
    if (inString) {
      if (escaped) escaped = false;
      else if (character === '\\') escaped = true;
      else if (character === '"') inString = false;
      continue;
    }
    if (character === '"') inString = true;
    else if (character === '{' || character === '[') {
      depth += 1;
      if (depth > MAX_ADVANCED_JSON_DEPTH) throw new Error('高级 JSON 嵌套超过 100 层');
    } else if (character === '}' || character === ']') {
      depth = Math.max(0, depth - 1);
    }
  }
}

function assertAllowedKeys(value: Record<string, unknown>, allowed: Set<string>, path: string) {
  Object.keys(value).forEach((key) => {
    if (DANGEROUS_KEYS.has(key)) throw new Error(`${path ? `${path}.` : ''}${key} 是禁止字段`);
    if (!allowed.has(key)) throw new Error(`${path ? `${path}.` : ''}${key} 不是允许字段`);
  });
}

function inspectParamsLimits(value: unknown) {
  let scalars = 0;
  const pending: Array<{ value: unknown; depth: number }> = [{ value, depth: 0 }];
  while (pending.length) {
    const current = pending.pop()!;
    if (current.depth > MAX_PARAMS_DEPTH) throw new Error('高级 JSON params 嵌套超过 20 层');
    if (Array.isArray(current.value)) {
      current.value.forEach((item) => pending.push({ value: item, depth: current.depth + 1 }));
    } else if (isRecord(current.value)) {
      Object.values(current.value).forEach((item) => pending.push({ value: item, depth: current.depth + 1 }));
    } else {
      scalars += 1;
      if (scalars > MAX_PARAMS_SCALARS) throw new Error('高级 JSON params 标量超过 10000');
    }
  }
}

function assertProfileStructureLimits(root: Record<string, unknown>) {
  const variables = root.variables;
  const nodes = root.nodes;
  if (Array.isArray(variables) && variables.length > MAX_PROFILE_COLLECTION_ITEMS) {
    throw new Error('高级 JSON variables 超过 500 项');
  }
  if (Array.isArray(nodes) && nodes.length > MAX_PROFILE_COLLECTION_ITEMS) {
    throw new Error('高级 JSON nodes 超过 500 项');
  }
  if (Array.isArray(nodes)) {
    nodes.forEach((node) => {
      if (isRecord(node)) inspectParamsLimits(node.params);
    });
  }
}

function assertProfileAllowedKeys(root: Record<string, unknown>) {
  assertAllowedKeys(root, ROOT_KEYS, '');
  if (isRecord(root.opc)) {
    assertAllowedKeys(
      root.opc,
      new Set(['url', 'poll_interval', 'io_timeout']),
      'opc',
    );
  }
  if (Array.isArray(root.variables)) {
    root.variables.forEach((item, index) => {
      if (isRecord(item)) assertAllowedKeys(item, VARIABLE_KEYS, `variables[${index}]`);
    });
  }
  if (!Array.isArray(root.nodes)) return;
  root.nodes.forEach((item, nodeIndex) => {
    if (!isRecord(item)) return;
    const base = `nodes[${nodeIndex}]`;
    assertAllowedKeys(item, NODE_KEYS, base);
    const inspectGroup = (group: unknown, path: string) => {
      if (!isRecord(group)) return;
      assertAllowedKeys(group, new Set(['all']), path);
      if (Array.isArray(group.all)) {
        group.all.forEach((condition, index) => {
          if (isRecord(condition)) assertAllowedKeys(condition, CONDITION_KEYS, `${path}.all[${index}]`);
        });
      }
    };
    const inspectPhase = (phase: unknown, path: string, delayed: boolean) => {
      if (!isRecord(phase)) return;
      assertAllowedKeys(phase, new Set(delayed ? ['writes', 'delay'] : ['writes']), path);
      if (Array.isArray(phase.writes)) {
        phase.writes.forEach((write, index) => {
          if (isRecord(write)) assertAllowedKeys(write, WRITE_KEYS, `${path}.writes[${index}]`);
        });
      }
    };
    inspectGroup(item.trigger, `${base}.trigger`);
    inspectGroup(item.reset_when, `${base}.reset_when`);
    inspectPhase(item.on_trigger, `${base}.on_trigger`, false);
    inspectPhase(item.on_complete, `${base}.on_complete`, true);
    inspectPhase(item.after_reset, `${base}.after_reset`, true);
  });
}

export function parseOpcSimulatorProfileJson(text: string): OpcSimulatorProfile {
  assertAdvancedJsonTextLimits(text);
  let payload: unknown;
  try {
    payload = JSON.parse(text);
  } catch {
    throw new Error('高级 JSON 不是有效的 profile');
  }
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new Error('高级 JSON 不是有效的 profile 对象');
  }
  assertJsonData(payload);
  const root = payload as Record<string, unknown>;
  assertProfileAllowedKeys(root);
  assertProfileStructureLimits(root);
  if (
    root.schema_version !== 2
    || (root.status !== 'draft' && root.status !== 'runnable')
    || typeof root.name !== 'string'
    || !root.opc
    || typeof root.opc !== 'object'
    || typeof (root.opc as Record<string, unknown>).url !== 'string'
    || typeof (root.opc as Record<string, unknown>).poll_interval !== 'number'
    || typeof (root.opc as Record<string, unknown>).io_timeout !== 'number'
    || !Array.isArray(root.variables)
    || !Array.isArray(root.nodes)
    || root.variables.some((item) => {
      if (!item || typeof item !== 'object' || Array.isArray(item)) return true;
      const variable = item as Record<string, unknown>;
      return typeof variable.name !== 'string'
        || typeof variable.direction !== 'string'
        || typeof variable.data_type !== 'string'
        || !['action_node', 'action_sensor', 'task_input', 'task_output', 'manual'].includes(String(variable.source));
    })
    || root.nodes.some((item) => {
      if (!item || typeof item !== 'object' || Array.isArray(item)) return true;
      const node = item as Record<string, unknown>;
      const groupIsSafe = (group: unknown, optional = false) => (
        optional && group === null
          ? true
          : Boolean(group)
            && typeof group === 'object'
            && !Array.isArray(group)
            && Array.isArray((group as Record<string, unknown>).all)
            && ((group as Record<string, unknown>).all as unknown[]).every((condition) => (
              Boolean(condition)
              && typeof condition === 'object'
              && !Array.isArray(condition)
              && typeof (condition as Record<string, unknown>).variable === 'string'
              && typeof (condition as Record<string, unknown>).operator === 'string'
              && typeof (condition as Record<string, unknown>).edge === 'string'
            ))
      );
      const phaseIsSafe = (phase: unknown, optional = false) => (
        optional && phase === null
          ? true
          : Boolean(phase)
            && typeof phase === 'object'
            && !Array.isArray(phase)
            && Array.isArray((phase as Record<string, unknown>).writes)
            && ((phase as Record<string, unknown>).writes as unknown[]).every((write) => (
              Boolean(write)
              && typeof write === 'object'
              && !Array.isArray(write)
              && typeof (write as Record<string, unknown>).variable === 'string'
            ))
      );
      return typeof node.workflow_node_id !== 'string'
        || typeof node.device_id !== 'string'
        || typeof node.method !== 'string'
        || typeof node.channel !== 'string'
        || !Array.isArray(node.task_template_ids)
        || node.task_template_ids.some((templateId) => typeof templateId !== 'string')
        || !node.params
        || typeof node.params !== 'object'
        || Array.isArray(node.params)
        || !groupIsSafe(node.trigger)
        || !groupIsSafe(node.reset_when, true)
        || !phaseIsSafe(node.on_trigger)
        || !phaseIsSafe(node.on_complete)
        || !phaseIsSafe(node.after_reset, true);
    })
  ) {
    throw new Error('高级 JSON 缺少完整 profile v2 结构');
  }
  return payload as OpcSimulatorProfile;
}

function scalarMatches(dataType: OpcVariableDataType, value: JsonScalar) {
  if (dataType === 'bool') return typeof value === 'boolean';
  if (dataType === 'int') return typeof value === 'number' && Number.isInteger(value);
  if (dataType === 'float') return typeof value === 'number' && Number.isFinite(value);
  if (dataType === 'string') return typeof value === 'string';
  return false;
}

function errorMessage(path: string) {
  if (path.endsWith('.channel')) return 'channel 不能为空';
  if (path.endsWith('.trigger.all')) return 'trigger.all 至少需要一条条件';
  if (path.includes('.variable')) return '变量不存在或方向不允许';
  if (path.includes('.value')) return '值与变量类型不匹配';
  return '字段缺失或格式无效';
}

function isLegalOpcTcpUrl(value: unknown) {
  if (typeof value !== 'string' || !value.trim() || value !== value.trim()) return false;
  try {
    const parsed = new URL(value);
    return parsed.protocol === 'opc.tcp:'
      && Boolean(parsed.hostname)
      && !parsed.username
      && !parsed.password
      && !parsed.search
      && !parsed.hash;
  } catch {
    return false;
  }
}

export function validationPathsToErrors(
  paths: string[],
  profile: unknown,
): OpcProfileValidationError[] {
  const nodes = isRecord(profile) && Array.isArray(profile.nodes) ? profile.nodes : [];
  return paths.map((path) => {
    const index = /^nodes\[(\d+)]/.exec(path)?.[1];
    const rawNode = index === undefined ? undefined : nodes[Number(index)];
    const nodeId = isRecord(rawNode) && typeof rawNode.workflow_node_id === 'string'
      ? rawNode.workflow_node_id
      : undefined;
    return { path, message: errorMessage(path), ...(nodeId ? { nodeId } : {}) };
  });
}

export function validateOpcSimulatorProfile(profile: unknown): OpcProfileValidationError[] {
  const paths: string[] = [];
  const add = (path: string) => {
    if (!paths.includes(path)) paths.push(path);
  };
  const addUnknownKeys = (value: Record<string, unknown>, allowed: Set<string>, path: string) => {
    Object.keys(value).forEach((key) => {
      if (!allowed.has(key) || DANGEROUS_KEYS.has(key)) add(path ? `${path}.${key}` : key);
    });
  };
  if (!isRecord(profile)) return [{ path: '$', message: errorMessage('$') }];
  addUnknownKeys(profile, ROOT_KEYS, '');
  if (profile.schema_version !== 2) add('schema_version');
  if (profile.status !== 'draft' && profile.status !== 'runnable') add('status');
  if (typeof profile.name !== 'string' || !profile.name.trim()) add('name');
  if (!isRecord(profile.opc)) add('opc');
  else {
    addUnknownKeys(
      profile.opc,
      new Set(['url', 'poll_interval', 'io_timeout']),
      'opc',
    );
    if (!isLegalOpcTcpUrl(profile.opc.url)) add('opc.url');
    if (
      typeof profile.opc.poll_interval !== 'number'
      || !Number.isFinite(profile.opc.poll_interval)
      || profile.opc.poll_interval < 0.05
      || profile.opc.poll_interval > 60
    ) add('opc.poll_interval');
    if (
      typeof profile.opc.io_timeout !== 'number'
      || !Number.isFinite(profile.opc.io_timeout)
      || profile.opc.io_timeout < 0.1
      || profile.opc.io_timeout > 60
    ) add('opc.io_timeout');
  }
  const rawVariables = Array.isArray(profile.variables) ? profile.variables : [];
  if (!Array.isArray(profile.variables) || !rawVariables.length || rawVariables.length > MAX_PROFILE_COLLECTION_ITEMS) {
    add('variables');
  }
  const variables = new Map<string, OpcSimulatorVariable>();
  rawVariables.forEach((rawVariable, index) => {
    const path = `variables[${index}]`;
    if (!isRecord(rawVariable)) {
      add(path);
      return;
    }
    addUnknownKeys(rawVariable, VARIABLE_KEYS, path);
    const variable = rawVariable as OpcSimulatorVariable;
    if (typeof variable.name !== 'string' || !variable.name.trim() || variables.has(variable.name)) add(`${path}.name`);
    else variables.set(variable.name, variable);
    if (!['pc_to_plc', 'plc_to_pc'].includes(variable.direction)) add(`${path}.direction`);
    if (!['bool', 'int', 'float', 'string'].includes(variable.data_type)) add(`${path}.data_type`);
    if (!['action_node', 'action_sensor', 'task_input', 'task_output', 'manual'].includes(variable.source)) {
      add(`${path}.source`);
    }
    if (
      variable.initial_value !== undefined
      && (variable.direction !== 'plc_to_pc' || !scalarMatches(variable.data_type, variable.initial_value))
    ) add(`${path}.initial_value`);
  });
  const rawNodes = Array.isArray(profile.nodes) ? profile.nodes : [];
  if (!Array.isArray(profile.nodes) || !rawNodes.length || rawNodes.length > MAX_PROFILE_COLLECTION_ITEMS) add('nodes');
  const workflowNodeIds = new Set<string>();
  let paramsScalars = 0;
  const validateParams = (value: unknown, path: string, depth = 0, ancestors = new WeakSet<object>()) => {
    if (depth > MAX_PARAMS_DEPTH) add(path);
    if (
      value === null
      || typeof value === 'string'
      || typeof value === 'boolean'
      || (typeof value === 'number' && Number.isFinite(value))
    ) {
      paramsScalars += 1;
      if (paramsScalars > MAX_PARAMS_SCALARS) add(path);
      return;
    }
    if (typeof value !== 'object' || value === null || ancestors.has(value)) {
      add(path);
      return;
    }
    ancestors.add(value);
    if (Array.isArray(value)) {
      value.forEach((item, index) => validateParams(item, `${path}[${index}]`, depth + 1, ancestors));
    } else {
      Object.entries(value).forEach(([key, item]) => {
        const itemPath = `${path}.${key}`;
        if (DANGEROUS_KEYS.has(key)) add(itemPath);
        validateParams(item, itemPath, depth + 1, ancestors);
      });
    }
    ancestors.delete(value);
  };
  rawNodes.forEach((rawNode, nodeIndex) => {
    const base = `nodes[${nodeIndex}]`;
    if (!isRecord(rawNode)) {
      add(base);
      return;
    }
    addUnknownKeys(rawNode, NODE_KEYS, base);
    const node = rawNode as OpcSimulatorNode;
    if (
      typeof node.workflow_node_id !== 'string'
      || !node.workflow_node_id.trim()
      || workflowNodeIds.has(node.workflow_node_id)
    ) add(`${base}.workflow_node_id`);
    else workflowNodeIds.add(node.workflow_node_id);
    if (!Array.isArray(node.task_template_ids) || !node.task_template_ids.length) add(`${base}.task_template_ids`);
    else node.task_template_ids.forEach((templateId, index) => {
      if (typeof templateId !== 'string' || !templateId.trim()) add(`${base}.task_template_ids[${index}]`);
    });
    if (typeof node.device_id !== 'string' || !node.device_id.trim()) add(`${base}.device_id`);
    if (typeof node.method !== 'string' || !node.method.trim()) add(`${base}.method`);
    if (typeof node.channel !== 'string' || !node.channel.trim()) add(`${base}.channel`);
    if (!isRecord(node.params)) add(`${base}.params`);
    else validateParams(node.params, `${base}.params`);
    const validateGroup = (group: unknown, path: string, optional: boolean) => {
      if (optional && group === null) return;
      if (!isRecord(group)) {
        add(path);
        return;
      }
      addUnknownKeys(group, new Set(['all']), path);
      if (!Array.isArray(group.all) || !group.all.length) {
        add(`${path}.all`);
        return;
      }
      group.all.forEach((rawCondition, index) => {
        const itemPath = `${path}.all[${index}]`;
        if (!isRecord(rawCondition)) {
          add(itemPath);
          return;
        }
        addUnknownKeys(rawCondition, CONDITION_KEYS, itemPath);
        const condition = rawCondition as OpcSimulatorCondition;
        const variable = variables.get(condition.variable);
        if (!variable) add(`${itemPath}.variable`);
        if (condition.operator !== 'eq') add(`${itemPath}.operator`);
        if (!['rising', 'falling', 'level'].includes(condition.edge)) add(`${itemPath}.edge`);
        if (
          !Object.prototype.hasOwnProperty.call(rawCondition, 'value')
          || (variable && !scalarMatches(variable.data_type, condition.value))
        ) add(`${itemPath}.value`);
      });
    };
    const validatePhase = (phase: unknown, path: string, delayed: boolean, optional = false) => {
      if (optional && phase === null) return;
      if (!isRecord(phase)) {
        add(path);
        return;
      }
      addUnknownKeys(phase, new Set(delayed ? ['writes', 'delay'] : ['writes']), path);
      if (delayed && (
        !Object.prototype.hasOwnProperty.call(phase, 'delay')
        || typeof phase.delay !== 'number'
        || !Number.isFinite(phase.delay)
        || phase.delay < 0
      )) add(`${path}.delay`);
      if (!Array.isArray(phase.writes)) {
        add(`${path}.writes`);
        return;
      }
      phase.writes.forEach((rawWrite, index) => {
        const itemPath = `${path}.writes[${index}]`;
        if (!isRecord(rawWrite)) {
          add(itemPath);
          return;
        }
        addUnknownKeys(rawWrite, WRITE_KEYS, itemPath);
        const write = rawWrite as OpcSimulatorWrite;
        const variable = variables.get(write.variable);
        if (!variable || variable.direction !== 'plc_to_pc') add(`${itemPath}.variable`);
        if (
          !Object.prototype.hasOwnProperty.call(rawWrite, 'value')
          || (variable && !scalarMatches(variable.data_type, write.value))
        ) add(`${itemPath}.value`);
      });
    };
    validateGroup(node.trigger, `${base}.trigger`, false);
    validateGroup(node.reset_when, `${base}.reset_when`, true);
    validatePhase(node.on_trigger, `${base}.on_trigger`, false);
    validatePhase(node.on_complete, `${base}.on_complete`, true);
    validatePhase(node.after_reset, `${base}.after_reset`, true, true);
    if (node.after_reset && !node.reset_when) add(`${base}.after_reset`);
  });
  return validationPathsToErrors(paths, profile);
}

export function countNodeMissingFields(
  node: OpcSimulatorNode,
  variables: OpcSimulatorVariable[],
) {
  const profile: OpcSimulatorProfile = {
    schema_version: 2,
    status: 'draft',
    name: 'count',
    opc: {
      url: 'opc.tcp://count:4840',
      poll_interval: 0.2,
      io_timeout: 2,
    },
    variables,
    nodes: [node],
  };
  return validateOpcSimulatorProfile(profile).filter((error) => error.nodeId === node.workflow_node_id).length;
}

function updateNode(
  profile: OpcSimulatorProfile,
  nodeIndex: number,
  updater: (node: OpcSimulatorNode) => OpcSimulatorNode,
) {
  return {
    ...profile,
    nodes: profile.nodes.map((node, index) => index === nodeIndex ? updater(node) : node),
  };
}

export function addProfileVariable(profile: OpcSimulatorProfile, variable: OpcSimulatorVariable) {
  return { ...profile, variables: [...profile.variables, { ...variable }] };
}

export function updateProfileVariable(
  profile: OpcSimulatorProfile,
  index: number,
  patch: Partial<OpcSimulatorVariable>,
) {
  return {
    ...profile,
    variables: profile.variables.map((variable, itemIndex) => {
      if (itemIndex !== index) return variable;
      const updated = { ...variable } as Record<string, unknown>;
      Object.entries(patch).forEach(([key, value]) => {
        if (value === undefined) delete updated[key];
        else updated[key] = value;
      });
      return updated as OpcSimulatorVariable;
    }),
  };
}

export function clearProfileVariableInitialValue(
  profile: OpcSimulatorProfile,
  index: number,
) {
  return {
    ...profile,
    variables: profile.variables.map((variable, itemIndex) => {
      if (itemIndex !== index) return variable;
      const { initial_value: _removed, ...withoutInitialValue } = variable;
      return withoutInitialValue;
    }),
  };
}

export function removeProfileVariable(profile: OpcSimulatorProfile, index: number) {
  return { ...profile, variables: profile.variables.filter((_, itemIndex) => itemIndex !== index) };
}

function conditionGroup(node: OpcSimulatorNode, field: ConditionField) {
  return node[field] || { all: [] };
}

export function addProfileCondition(
  profile: OpcSimulatorProfile,
  nodeIndex: number,
  field: ConditionField,
  condition: OpcSimulatorCondition,
) {
  return updateNode(profile, nodeIndex, (node) => ({
    ...node,
    [field]: { all: [...conditionGroup(node, field).all, { ...condition }] },
  }));
}

export function updateProfileCondition(
  profile: OpcSimulatorProfile,
  nodeIndex: number,
  field: ConditionField,
  conditionIndex: number,
  patch: Partial<OpcSimulatorCondition>,
) {
  return updateNode(profile, nodeIndex, (node) => ({
    ...node,
    [field]: {
      all: conditionGroup(node, field).all.map((condition, index) => (
        index === conditionIndex ? { ...condition, ...patch } : condition
      )),
    },
  }));
}

export function removeProfileCondition(
  profile: OpcSimulatorProfile,
  nodeIndex: number,
  field: ConditionField,
  conditionIndex: number,
) {
  return updateNode(profile, nodeIndex, (node) => ({
    ...node,
    [field]: { all: conditionGroup(node, field).all.filter((_, index) => index !== conditionIndex) },
  }));
}

function phase(node: OpcSimulatorNode, field: PhaseField): OpcSimulatorPhase | OpcSimulatorDelayedPhase {
  return node[field] || (field === 'on_trigger' ? { writes: [] } : { delay: 0, writes: [] });
}

export function addProfileWrite(
  profile: OpcSimulatorProfile,
  nodeIndex: number,
  field: PhaseField,
  write: OpcSimulatorWrite,
) {
  return updateNode(profile, nodeIndex, (node) => ({
    ...node,
    [field]: { ...phase(node, field), writes: [...phase(node, field).writes, { ...write }] },
  }));
}

export function updateProfileWrite(
  profile: OpcSimulatorProfile,
  nodeIndex: number,
  field: PhaseField,
  writeIndex: number,
  patch: Partial<OpcSimulatorWrite>,
) {
  return updateNode(profile, nodeIndex, (node) => ({
    ...node,
    [field]: {
      ...phase(node, field),
      writes: phase(node, field).writes.map((write, index) => (
        index === writeIndex ? { ...write, ...patch } : write
      )),
    },
  }));
}

export function removeProfileWrite(
  profile: OpcSimulatorProfile,
  nodeIndex: number,
  field: PhaseField,
  writeIndex: number,
) {
  return updateNode(profile, nodeIndex, (node) => ({
    ...node,
    [field]: { ...phase(node, field), writes: phase(node, field).writes.filter((_, index) => index !== writeIndex) },
  }));
}

export function restoreStatusNeedsConfirm(
  managerState: OpcSimulatorState,
  managerRestoreStatus: string,
) {
  if (managerRestoreStatus === 'pending' || managerState === 'stopping') return false;
  return ['error', 'uncertain'].includes(managerRestoreStatus);
}

export function isSimulatorStartAllowed({
  profile,
  localErrors,
  backendErrors,
  fileName,
  revision,
  dirty,
  managerState,
  managerRestoreStatus = 'not_started',
}: {
  profile: OpcSimulatorProfile;
  localErrors: OpcProfileValidationError[];
  backendErrors: string[];
  fileName: string | null;
  revision: string | null;
  dirty: boolean;
  managerState: OpcSimulatorState;
  managerRestoreStatus?: string;
}) {
  return profile.status === 'runnable'
    && !localErrors.length
    && !backendErrors.length
    && Boolean(fileName)
    && Boolean(revision)
    && !dirty
    && !['starting', 'running', 'stopping'].includes(managerState)
    && managerRestoreStatus !== 'pending';
}

export function describeSimulatorStartBlock({
  profile,
  localErrors,
  backendErrors,
  fileName,
  revision,
  dirty,
  managerState,
  managerRestoreStatus = 'not_started',
  busy = false,
}: {
  profile: OpcSimulatorProfile | null;
  localErrors: OpcProfileValidationError[];
  backendErrors: string[];
  fileName: string | null;
  revision: string | null;
  dirty: boolean;
  managerState: OpcSimulatorState;
  managerRestoreStatus?: string;
  busy?: boolean;
}): string | null {
  if (busy) return '模拟器操作进行中，请稍候';
  if (!fileName) return '请在下拉框选择配置文件';
  if (!profile || !revision) return null;
  if (dirty) return '配置有未保存修改，请先在工作台保存后再启动';
  if (profile.status !== 'runnable') return '配置 status 必须为 runnable 才能启动模拟器';
  if (localErrors.length) return `配置存在 ${localErrors.length} 项校验问题，请在工作台修正并保存`;
  if (backendErrors.length) return `后端校验 ${backendErrors.length} 项问题，请在工作台修正并保存`;
  if (['starting', 'running', 'stopping'].includes(managerState)) {
    return `模拟器当前为 ${managerState}，无法重复启动`;
  }
  if (managerRestoreStatus === 'pending') return '模拟器正在停止/恢复 OPC，请稍候';
  return null;
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    return null;
  }
}

export function httpErrorMessage(payload: unknown, status: number, fallback: string) {
  if (payload && typeof payload === 'object' && 'detail' in payload) {
    const detail = (payload as { detail?: unknown }).detail;
    if (typeof detail === 'string') return detail;
    if (detail && typeof detail === 'object' && 'message' in detail) {
      return String((detail as { message?: unknown }).message || fallback);
    }
  }
  return `${fallback}（HTTP ${status}）`;
}

export function createOpcSimulatorClient(fetcher: FetchLike = fetch) {
  const isProfileResponse = (payload: unknown): payload is OpcProfileApiResponse => {
    if (!payload || typeof payload !== 'object') return false;
    const value = payload as Record<string, unknown>;
    return typeof value.file_name === 'string'
      && Array.isArray(value.validation_errors)
      && Boolean(value.profile)
      && typeof value.profile === 'object'
      && (value.profile as Record<string, unknown>).schema_version === 2;
  };
  const isStatusResponse = (payload: unknown): payload is OpcSimulatorStatus => {
    if (!payload || typeof payload !== 'object') return false;
    const value = payload as Record<string, unknown>;
    const active = ['starting', 'running', 'stopping'].includes(String(value.state));
    const validRunId = value.run_id === null
      || (typeof value.run_id === 'string' && /^[0-9a-f]{32}$/.test(value.run_id));
    return typeof value.state === 'string'
      && typeof value.elapsed_seconds === 'number'
      && typeof value.restore_status === 'string'
      && validRunId
      && (!active || typeof value.run_id === 'string')
      && Array.isArray(value.recent_logs);
  };
  const request = async <T>(
    path: string,
    init: RequestInit | undefined,
    validate: (payload: unknown) => payload is T,
  ): Promise<T> => {
    const response = await fetcher(path, init);
    const payload = await readJson(response);
    if (!response.ok) {
      throw new OpcSimulatorHttpError(
        httpErrorMessage(payload, response.status, 'OPC 模拟器请求失败'),
        response.status,
        payload,
      );
    }
    if (!validate(payload)) throw new Error('OPC 模拟器返回了无效响应');
    return payload;
  };
  const json = (method: string, body: unknown, headers?: HeadersInit): RequestInit => ({
    method,
    headers: { 'content-type': 'application/json', ...headers },
    body: JSON.stringify(body),
  });
  return {
    generate: (payload: {
      workflow: Record<string, unknown>;
      templates: unknown[];
      scheduled_template_ids: string[];
      action_catalog: OpcActionCatalogEntry[];
      variable_catalog: Array<{
        name: string;
        data_type: 'bool' | 'int' | 'float' | 'string';
      }>;
      name: string;
      file_name: string;
      opc_url: string;
    }) => request('/api/opc-simulator/profiles:generate', json('POST', payload), isProfileResponse),
    validate: (profile: OpcSimulatorProfile, fileName: string) => (
      request('/api/opc-simulator/profiles:validate', json('POST', { profile, file_name: fileName }), isProfileResponse)
    ),
    save: (profile: OpcSimulatorProfile, fileName: string, revision: string | null) => (
      request(
        `/api/opc-simulator/profiles/${encodeURIComponent(fileName)}`,
        json(
          'PUT',
          { profile },
          revision !== null ? { 'If-Match': revision } : undefined,
        ),
        isProfileResponse,
      )
    ),
    load: (fileName: string) => request(
      `/api/opc-simulator/profiles/${encodeURIComponent(fileName)}`,
      undefined,
      isProfileResponse,
    ),
    listProfiles: () => request(
      '/api/opc-simulator/profiles',
      undefined,
      (payload): payload is { config_dir: string; files: string[] } => (
        Boolean(payload)
        && typeof payload === 'object'
        && typeof (payload as { config_dir?: unknown }).config_dir === 'string'
        && Array.isArray((payload as { files?: unknown }).files)
        && (payload as { files: unknown[] }).files.every((item) => typeof item === 'string')
      ),
    ),
    loadReferenceTemplate: () => request(
      '/api/opc-simulator/profiles/reference/template',
      undefined,
      isProfileResponse,
    ),
    loadProfileSpec: () => request(
      '/api/opc-simulator/profile-spec',
      undefined,
      (payload): payload is { schema_version: number; path: string; markdown: string } => (
        Boolean(payload)
        && typeof payload === 'object'
        && (payload as { schema_version?: unknown }).schema_version === 2
        && typeof (payload as { path?: unknown }).path === 'string'
        && typeof (payload as { markdown?: unknown }).markdown === 'string'
      ),
    ),
    status: (signal?: AbortSignal) => request(
      '/api/opc-simulator/status',
      signal ? { signal } : undefined,
      isStatusResponse,
    ),
    start: (
      fileName: string,
      revision: string,
      allowUnsafeUrl = false,
    ) => request(
      '/api/opc-simulator/start',
      json('POST', {
        file_name: fileName,
        expected_revision: revision,
        allow_unsafe_url: allowUnsafeUrl,
      }),
      isStatusResponse,
    ),
    stop: (expectedRunId: string | null = null, keepalive = false) => request(
      '/api/opc-simulator/stop',
      {
        ...json(
          'POST',
          expectedRunId ? { expected_run_id: expectedRunId } : {},
        ),
        keepalive,
      },
      isStatusResponse,
    ),
  };
}
