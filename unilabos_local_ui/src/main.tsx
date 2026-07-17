import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import ReactDOM from 'react-dom/client';
import ReactFlow, {
  Background,
  ControlButton,
  Controls,
  Handle,
  MiniMap,
  Position,
  ReactFlowProvider,
  addEdge,
  applyEdgeChanges,
  applyNodeChanges,
  type Connection,
  type Edge,
  type EdgeChange,
  type Node,
  type NodeChange,
  type NodeProps,
} from 'reactflow';
import 'reactflow/dist/style.css';
import './styles.css';
import { collectOpcChanges, formatOpcValue, type LogEvent, type OpcChange } from './opcChanges';
import { buildWorkspaceSummary, groupActionsByDevice } from './uiState';
import {
  createExecutionEdgeOverlay,
  createExecutionPlan,
  createImportedDraft,
  createWorkflowRequest,
  layoutFlowGraph,
  workflowDraftKey,
} from './workflowDraft';
import { createPseudoFlowJson } from './workflowExport';
import { WorkstationDemo } from './WorkstationDemo';
import {
  createDefaultTriggerCondition,
  createTaskTemplateTriggers,
  normalizeTriggerConditions,
  type TriggerCondition,
} from './taskOrchestration';
import {
  createTaskOrchestrationClient,
  fromApiTrigger,
  TaskOrchestrationBusinessError,
  TaskOrchestrationServiceUnavailableError,
  toApiTrigger,
  type ApiWaitingReason,
  type ApiWorkspaceEvent,
  type ApiWorkspaceResponse,
} from './taskOrchestrationApi';

type ActionSpec = {
  method: string;
  label: string;
  description: string;
  device_id?: string;
  needs_position: boolean;
  params?: ParamSpec[];
  opc_variables?: string[];
};

type ParamSpec = {
  name?: string;
  label?: string;
  description?: string;
  type?: string;
  min?: number;
  max?: number;
  default?: unknown;
};

type PresetPayload = {
  id: string;
  title: string;
  default_workflow_name: string;
  default_config: {
    graph?: string;
    url?: string;
    csv?: string;
    timeout?: number;
    write_allowed_timeout?: number;
    no_subscription?: boolean;
    show_csv?: boolean;
  };
  actions: ActionSpec[];
};

type WorkflowJson = {
  name: string;
  nodes: Array<Record<string, unknown>>;
  edges: Array<Record<string, unknown>>;
};

type RunStatus = {
  run_id: string;
  status: string;
  logs: string[];
  log_events?: LogEvent[];
  error?: string | null;
  node_statuses?: Record<string, NodeRunStatus>;
};

type NodeRunStatus = 'idle' | 'preparing' | 'running' | 'success' | 'failed' | 'cancelled';
type Workspace = 'workflow' | 'tasks';
type CanvasTab = 'workflow' | 'sensors';
type TaskTemplate = {
  id: string;
  name: string;
  nodeIds: string[];
  resources: string[];
  gates: string[];
  inputTriggers: TriggerCondition[];
  outputTriggers: TriggerCondition[];
};
type CsvVariable = {
  name: string;
  data_type: string;
  initial_value: string;
  comment: string;
};
type TaskInstanceStatus = 'waiting' | 'pending' | 'running' | 'completed' | 'failed' | 'cancelled';
type TaskInstance = {
  id: string;
  sample: string;
  templateId: string;
  order: number;
  status: TaskInstanceStatus;
  startedAt?: number;
  finishedAt?: number;
};
type TaskWorkspaceState = {
  taskTemplates: TaskTemplate[];
  taskInstances: TaskInstance[];
  taskEvents: string[];
};

function taskTriggerLogLabel(trigger: unknown) {
  if (!trigger || typeof trigger !== 'object') return '条件已满足';
  const item = trigger as { kind?: string; config?: Record<string, unknown> };
  const config = item.config || {};
  if (item.kind === 'resource') return `资源 ${String(config.resource || '')} 可用`;
  if (item.kind === 'workstation') return `工位 ${String(config.workstation || '')} 可用`;
  if (item.kind === 'opc') return `OPC ${String(config.variable || '')} == ${String(config.value)}`;
  if (item.kind === 'internal') return `内部事件 ${String(config.key || '')} 已满足`;
  return `${item.kind || '未知'} 条件已满足`;
}

function taskEventText(event: ApiWorkspaceEvent, fallbackTriggers: unknown[] = []) {
  const timestamp = new Date(event.timestamp).toLocaleTimeString('zh-CN', { hour12: false });
  const satisfiedTriggers = event.payload.satisfied_triggers;
  if (event.kind === 'scheduled') {
    const triggers = Array.isArray(satisfiedTriggers) ? satisfiedTriggers : fallbackTriggers;
    const conditions = triggers.map(taskTriggerLogLabel).join('、');
    return `${timestamp} 已派发：${conditions || '无额外输入条件'}`;
  }
  return `${timestamp} ${event.kind}`;
}

function taskWorkspaceFromApi(response: ApiWorkspaceResponse): TaskWorkspaceState & {
  version: number;
  scheduledTemplateIds: string[];
  isSchedulerRunning: boolean;
  waitingReasons: Record<string, ApiWaitingReason>;
  scheduleEntries: Array<{
    id: string;
    instanceId: string;
    sample: string;
    templateId: string;
    resource: string;
    startAt: number;
    endAt: number;
    state: 'planned' | 'running' | 'done';
  }>;
} {
  const templateById = new Map(response.workspace.templates.map((template) => [template.id, template]));
  return {
    version: response.version,
    taskTemplates: response.workspace.templates.map((template) => ({
      id: template.id,
      name: template.name,
      nodeIds: template.node_ids,
      resources: template.resources,
      gates: [],
      inputTriggers: template.input_triggers.map(fromApiTrigger),
      outputTriggers: template.output_triggers.map(fromApiTrigger),
    })),
    taskInstances: response.workspace.task_instances.map((instance) => ({
      id: instance.id,
      sample: instance.sample_id,
      templateId: instance.template_id,
      order: instance.order,
      status: instance.status,
      startedAt: instance.started_at ?? undefined,
      finishedAt: instance.finished_at ?? undefined,
    })),
    taskEvents: response.workspace.events.map((event) => (
      taskEventText(
        event,
        event.template_id
          ? [
              ...(templateById.get(event.template_id)?.input_triggers || []),
              ...(templateById.get(event.template_id)?.trigger ? [templateById.get(event.template_id)!.trigger] : []),
            ]
          : [],
      )
    )).slice(-20).reverse(),
    scheduledTemplateIds: response.workspace.scheduled_template_ids,
    isSchedulerRunning: !response.workspace.scheduler_paused,
    waitingReasons: response.schedule?.waiting_reasons || {},
    scheduleEntries: response.workspace.schedule_entries.flatMap((entry) => entry.resources.map((resource) => ({
      id: `${entry.instance_id}:${resource}`,
      instanceId: entry.instance_id,
      sample: entry.sample_id,
      templateId: entry.template_id,
      resource,
      startAt: entry.start_at,
      endAt: entry.end_at,
      state: entry.state,
    }))),
  };
}

function taskApiErrorMessage(error: unknown) {
  if (error instanceof TaskOrchestrationServiceUnavailableError) {
    return 'Task 编排服务不可用';
  }
  return error instanceof Error ? error.message : 'Task 编排操作失败';
}

export function createEmptyTaskWorkspaceState() {
  return {
    taskTemplates: [] as TaskTemplate[],
    taskInstances: [] as TaskInstance[],
    taskEvents: [] as string[],
  };
}

export function resetTaskWorkspaceState(_previousState: TaskWorkspaceState) {
  return createEmptyTaskWorkspaceState();
}

export function isRestorableContextMenuFocusTarget(
  element: unknown,
  body: unknown,
  documentElement: unknown,
) {
  return element != null && element !== body && element !== documentElement;
}

export function clampContextMenuPosition(
  x: number,
  y: number,
  viewportWidth: number,
  viewportHeight: number,
  menuWidth = 176,
  menuHeight = 92,
  margin = 8,
) {
  const maxLeft = Math.max(margin, viewportWidth - menuWidth - margin);
  const maxTop = Math.max(margin, viewportHeight - menuHeight - margin);
  return {
    left: Math.max(margin, Math.min(x, maxLeft)),
    top: Math.max(margin, Math.min(y, maxTop)),
  };
}

export function removeTaskTemplateState(
  templateId: string,
  taskTemplates: TaskTemplate[],
  taskInstances: TaskInstance[],
) {
  if (!taskTemplates.some((template) => template.id === templateId)) {
    return { taskTemplates, taskInstances, removedInstanceCount: 0 };
  }
  const nextTaskInstances = taskInstances.filter((task) => task.templateId !== templateId);
  return {
    taskTemplates: taskTemplates.filter((template) => template.id !== templateId),
    taskInstances: nextTaskInstances,
    removedInstanceCount: taskInstances.length - nextTaskInstances.length,
  };
}

type StackSlotPayload = {
  site_key?: string;
  occupied?: boolean | null;
  reagent_id?: string | null;
  qr_code?: string | null;
  remaining_amount?: number | null;
  unit?: string | null;
};

type StackPayload = {
  id: string;
  display_name?: string;
  warehouse_name?: string;
  managed_resource?: string;
  content_type?: string[];
  slots?: Record<string, StackSlotPayload>;
};

type StackStatusPayload = {
  success: boolean;
  schema?: string;
  updated_at?: string;
  message?: string;
  stacks?: Record<string, StackPayload>;
};

type SensorBitPayload = {
  index: number;
  name: string;
  value: boolean | null;
  label?: string;
  address?: string;
  node_id?: string;
};

type SensorArrayGroupPayload = {
  index: number;
  name: string;
  node_id?: string;
  values?: Array<boolean | null>;
  bits?: SensorBitPayload[];
  error?: string | null;
};

type SensorArraysPayload = {
  success: boolean;
  partial?: boolean;
  schema?: string;
  message?: string;
  groups?: SensorArrayGroupPayload[];
};

type StackResourceView = {
  id: string;
  title: string;
  role: string;
  used: number;
  total: number;
  nextSlot: string;
};

type StackSlotView = {
  id: string;
  material: string;
  status: 'empty' | 'occupied' | 'reserved';
};

type OpcVariableView = {
  name: string;
  currentValue?: unknown;
};

type ActionNodeData = {
  deviceId?: string;
  method: string;
  label: string;
  description: string;
  params: Record<string, unknown>;
  paramSpecs?: ParamSpec[];
  opcVariables?: string[];
  executionDisabled?: boolean;
  executionBypassed?: boolean;
  executionState?: 'willRun' | 'beforeStart' | 'disabled' | 'blockedByDisabled' | 'disconnected' | 'bypassed';
  isExecutionStart?: boolean;
  runStatus?: NodeRunStatus;
  onPositionChange?: (nodeId: string, value: number) => void;
  onSetStart?: (nodeId: string) => void;
  onToggleBypassed?: (nodeId: string) => void;
  onToggleDisabled?: (nodeId: string) => void;
  onEditParams?: (nodeId: string) => void;
};

const DEFAULT_CONFIG = {
  graph: '__generated__',
  url: 'opc.tcp://jdht1471820.bohrium.tech:50001',
  csv: '',
  timeout: 300,
  write_allowed_timeout: 5,
  no_subscription: true,
  show_csv: false,
};
const DRAFT_STORAGE_PREFIX = 'unilabos.workflowDraft';
const DEFAULT_SENSOR_GATES: Record<string, { label: string; free: boolean }> = {
  robot: { label: 'Robot 机械臂', free: true },
  s04: { label: 'S04 磁搅位', free: true },
  s05: { label: 'S05 拍照位', free: true },
  s06: { label: 'S06 加液位', free: true },
  s07: { label: 'S07 固体加料位', free: true },
  s08: { label: 'S08 开关盖位', free: true },
  s09: { label: 'S09 移液位', free: true },
};
const LIVE_SENSOR_GATE_BITS: Record<string, Array<[number, number]>> = {
  s04: [[2, 10], [2, 11], [2, 12], [2, 13], [2, 14], [2, 15]],
  s05: [[3, 0]],
  s06: [[3, 1]],
  s07: [[3, 14], [3, 15]],
  s08: [[3, 14], [3, 15]],
  s09: [[4, 7]],
};
const SAMPLE_NAMES = ['Sample A', 'Sample B', 'Sample C', 'Sample D', 'Sample E'];

function uniqueTaskKeys(keys: string[]) {
  return Array.from(new Set(keys.filter(Boolean)));
}

function inferTaskResources(node: Node<ActionNodeData>) {
  const text = `${node.data.deviceId || ''} ${node.data.method} ${node.data.label}`.toLowerCase();
  const resources: string[] = [];
  if (text.includes('robot') || /^submit_(pick|place)_/.test(node.data.method)) {
    resources.push('robot');
  }
  inferStationKeys(text).forEach((station) => resources.push(station));
  return resources.length ? uniqueTaskKeys(resources) : [node.data.deviceId || 'unknown'];
}

function inferTaskGates(node: Node<ActionNodeData>) {
  const text = `${node.data.deviceId || ''} ${node.data.method} ${node.data.label}`.toLowerCase();
  return inferStationKeys(text);
}

function inferStationKeys(text: string) {
  const stations: string[] = [];
  (['s04', 's05', 's06', 's07', 's08', 's09'] as const).forEach((station) => {
    const compact = station.replace('s0', 's');
    if (text.includes(station) || text.includes(compact)) {
      stations.push(station);
    }
  });
  return stations;
}

function orderSelectedNodesByPlan(
  selectedNodes: Node<ActionNodeData>[],
  plannedNodes: Node<ActionNodeData>[],
) {
  const selectedIds = new Set(selectedNodes.map((node) => node.id));
  const ordered = plannedNodes.filter((node) => selectedIds.has(node.id));
  const missing = selectedNodes.filter((node) => !ordered.some((item) => item.id === node.id));
  return [...ordered, ...missing];
}

function summarizeTaskName(taskNodes: Node<ActionNodeData>[]) {
  if (!taskNodes.length) return '未命名 Task';
  const stationNames = uniqueTaskKeys(taskNodes.flatMap((node) => inferTaskGates(node).map((gate) => gate.toUpperCase())));
  if (stationNames.length) return `${stationNames.join(' + ')} 工艺 Task`;
  return taskNodes.length === 1 ? taskNodes[0].data.label : `${taskNodes[0].data.label} 等 ${taskNodes.length} 步`;
}

function chunkNodesForTaskPreview(orderedNodes: Node<ActionNodeData>[]) {
  const chunks: Array<Node<ActionNodeData>[]> = [];
  let index = 0;
  while (index < orderedNodes.length) {
    const current = orderedNodes[index];
    const currentResources = inferTaskResources(current);
    const next = orderedNodes[index + 1];
    if (currentResources.includes('robot') && next) {
      chunks.push([current, next]);
      index += 2;
    } else {
      chunks.push([current]);
      index += 1;
    }
  }
  return chunks;
}

function buildRunningTaskResourceHolders(
  instances: TaskInstance[],
  templates: TaskTemplate[],
) {
  const holders: Record<string, string> = {};
  instances.filter((task) => task.status === 'running').forEach((task) => {
    const template = templates.find((item) => item.id === task.templateId);
    template?.resources.forEach((resource) => {
      holders[resource] = `${task.sample} / ${template.name}`;
    });
  });
  return holders;
}

function unlockNextTaskInstances(instances: TaskInstance[]) {
  return instances.map((task) => {
    if (task.status !== 'waiting') return task;
    const previousDone = instances
      .filter((item) => item.sample === task.sample && item.order < task.order)
      .every((item) => item.status === 'completed');
    return previousDone ? { ...task, status: 'pending' as const } : task;
  });
}

function taskBlockingReasons(
  task: TaskInstance,
  instances: TaskInstance[],
  templates: TaskTemplate[],
  sensorGates: Record<string, boolean>,
) {
  if (task.status === 'completed' || task.status === 'running') return [];
  const template = templates.find((item) => item.id === task.templateId);
  if (!template) return ['缺少 Task 模板'];
  const reasons: string[] = [];
  const previousDone = instances
    .filter((item) => item.sample === task.sample && item.order < task.order)
    .every((item) => item.status === 'completed');
  if (!previousDone) reasons.push('前置 Task 未完成');
  template.gates.forEach((gate) => {
    if (sensorGates[gate] === false) {
      reasons.push(`${DEFAULT_SENSOR_GATES[gate]?.label || gate} 传感器占用`);
    }
  });
  const holders = buildRunningTaskResourceHolders(instances, templates);
  template.resources.forEach((resource) => {
    if (holders[resource]) {
      reasons.push(`${DEFAULT_SENSOR_GATES[resource]?.label || resource} 已被 ${holders[resource]} 锁定`);
    }
  });
  return reasons;
}

function taskVisualState(task: TaskInstance, blockingReasons: string[]) {
  if (task.status === 'completed') return 'done';
  if (task.status === 'running') return 'running';
  return blockingReasons.length ? 'blocked' : 'ready';
}

function taskStatusText(status: ReturnType<typeof taskVisualState>) {
  if (status === 'done') return '完成';
  if (status === 'running') return '运行中';
  if (status === 'blocked') return '阻塞';
  return '可启动';
}

function buildDefaultParams(params: ParamSpec[]) {
  return params.reduce<Record<string, unknown>>((defaults, param) => {
    const name = param.name || '';
    if (!name) return defaults;
    if ('default' in param) {
      defaults[name] = param.default;
    } else if (param.type === 'boolean') {
      defaults[name] = false;
    } else if (param.type === 'integer' || param.type === 'number') {
      defaults[name] = param.min ?? 0;
    } else {
      defaults[name] = '';
    }
    return defaults;
  }, {});
}

function stackRoleText(stack: StackPayload) {
  if (stack.managed_resource === 'reagent') return '试剂';
  if (stack.managed_resource === 'physical_only') return '物理位';
  return stack.managed_resource || '堆栈';
}

function sortSlotIds(ids: string[]) {
  return [...ids].sort((left, right) => left.localeCompare(right, 'zh-CN', { numeric: true }));
}

function stackResourcesFromStatus(status: StackStatusPayload | null): StackResourceView[] {
  const stacks = status?.stacks || {};
  return Object.values(stacks).map((stack) => {
    const slots = stack.slots || {};
    const slotIds = sortSlotIds(Object.keys(slots));
    const used = slotIds.filter((slotId) => slots[slotId]?.occupied === true).length;
    const nextSlot = slotIds.length ? (slotIds.find((slotId) => slots[slotId]?.occupied !== true) || '已满') : '无数据';
    return {
      id: stack.id,
      title: stack.display_name || stack.warehouse_name || stack.id,
      role: stackRoleText(stack),
      used,
      total: slotIds.length,
      nextSlot,
    };
  });
}

function stackSlotsFromPayload(stack: StackPayload | undefined): StackSlotView[] {
  const slots = stack?.slots || {};
  return sortSlotIds(Object.keys(slots)).map((slotId) => {
    const slot = slots[slotId];
    const occupied = slot?.occupied;
    return {
      id: slot.site_key || slotId,
      material: slot?.reagent_id || slot?.qr_code || '',
      status: occupied === true ? 'occupied' : occupied === false ? 'empty' : 'reserved',
    };
  });
}

function uniqueOpcVariables(variables: Array<string | undefined>) {
  return Array.from(new Set(variables.filter((variable): variable is string => Boolean(variable))));
}

const STACK_SENSOR_VARIABLES: Record<string, string> = {
  's10_liquid_reagent:1-1': '传感器状态_上位机[4].NO[12]',
  's10_liquid_reagent:1-2': '传感器状态_上位机[4].NO[13]',
  's10_liquid_reagent:1-3': '传感器状态_上位机[4].NO[14]',
  's10_liquid_reagent:1-4': '传感器状态_上位机[4].NO[15]',
  's10_liquid_reagent:1-5': '传感器状态_上位机[5].NO[0]',
  's10_liquid_reagent:2-1': '传感器状态_上位机[5].NO[1]',
  's10_liquid_reagent:2-2': '传感器状态_上位机[5].NO[2]',
  's10_liquid_reagent:2-3': '传感器状态_上位机[5].NO[3]',
  's10_liquid_reagent:2-4': '传感器状态_上位机[5].NO[4]',
  's10_liquid_reagent:2-5': '传感器状态_上位机[5].NO[5]',
  's10_liquid_reagent:3-1': '传感器状态_上位机[5].NO[6]',
  's10_liquid_reagent:3-2': '传感器状态_上位机[5].NO[7]',
  's10_liquid_reagent:3-3': '传感器状态_上位机[5].NO[8]',
  's10_liquid_reagent:3-4': '传感器状态_上位机[5].NO[9]',
  's10_liquid_reagent:3-5': '传感器状态_上位机[5].NO[10]',
  's10_liquid_reagent:4-1': '传感器状态_上位机[5].NO[11]',
  's10_liquid_reagent:4-2': '传感器状态_上位机[5].NO[12]',
  's10_liquid_reagent:4-3': '传感器状态_上位机[5].NO[13]',
  's10_liquid_reagent:4-4': '传感器状态_上位机[5].NO[14]',
  's10_liquid_reagent:4-5': '传感器状态_上位机[5].NO[15]',
  'powder_container:1-1': '传感器状态_上位机[3].NO[8]',
  'powder_container:1-2': '传感器状态_上位机[3].NO[9]',
  'powder_container:1-3': '传感器状态_上位机[3].NO[10]',
  'powder_container:2-1': '传感器状态_上位机[3].NO[11]',
  'powder_container:2-2': '传感器状态_上位机[3].NO[12]',
  'powder_container:2-3': '传感器状态_上位机[3].NO[13]',
};

function stackSensorValuesFromStatus(status: StackStatusPayload | null) {
  const values: Record<string, unknown> = {};
  Object.entries(status?.stacks || {}).forEach(([stackId, stack]) => {
    Object.entries(stack.slots || {}).forEach(([slotId, slot]) => {
      const variableName = STACK_SENSOR_VARIABLES[`${stackId}:${slotId}`];
      if (variableName) {
        values[variableName] = slot.occupied;
      }
    });
  });
  return values;
}

function sensorBitValue(status: SensorArraysPayload | null, groupIndex: number, bitIndex: number) {
  const group = status?.groups?.find((item) => item.index === groupIndex);
  const bit = group?.bits?.find((item) => item.index === bitIndex);
  return bit?.value ?? group?.values?.[bitIndex] ?? null;
}

function liveSensorGateStates(status: SensorArraysPayload | null) {
  return Object.fromEntries(
    Object.entries(LIVE_SENSOR_GATE_BITS).map(([gate, bitRefs]) => {
      const values = bitRefs.map(([groupIndex, bitIndex]) => sensorBitValue(status, groupIndex, bitIndex));
      const knownValues = values.filter((value): value is boolean => value !== null);
      return [gate, knownValues.length === values.length ? !knownValues.some(Boolean) : null];
    }),
  ) as Record<string, boolean | null>;
}

function App() {
  const [title, setTitle] = useState('szlab 本地调试工具');
  const [actions, setActions] = useState<ActionSpec[]>([]);
  const [nodes, setNodes] = useState<Node<ActionNodeData>[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  const [workflowName, setWorkflowName] = useState('szlab_canvas_workflow');
  const [workflow, setWorkflow] = useState<WorkflowJson | null>(null);
  const [message, setMessage] = useState('');
  const [canvasToast, setCanvasToast] = useState('');
  const [draftReady, setDraftReady] = useState(false);
  const [draftStorageKey, setDraftStorageKey] = useState('');
  const [startNodeId, setStartNodeId] = useState<string | null>(null);
  const [runStatus, setRunStatus] = useState<RunStatus | null>(null);
  const [isRunning, setIsRunning] = useState(false);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [showConfigModal, setShowConfigModal] = useState(false);
  const [editingNodeId, setEditingNodeId] = useState<string | null>(null);
  const [selectedLogNodeId, setSelectedLogNodeId] = useState<string | null>(null);
  const [selectedLogCategory, setSelectedLogCategory] = useState<string | null>(null);
  const [leftTab, setLeftTab] = useState<'devices' | 'stacks'>('devices');
  const [leftPanelCollapsed, setLeftPanelCollapsed] = useState(false);
  const [collapsedActionGroups, setCollapsedActionGroups] = useState<Record<string, boolean>>({});
  const [workspace, setWorkspace] = useState<Workspace>('workflow');
  const [canvasTab, setCanvasTab] = useState<CanvasTab>('workflow');
  const [sideTab, setSideTab] = useState<'control' | 'materials' | 'logs'>('control');
  const [selectedStackId, setSelectedStackId] = useState('');
  const [showStackModal, setShowStackModal] = useState(false);
  const [stackStatus, setStackStatus] = useState<StackStatusPayload | null>(null);
  const [stackError, setStackError] = useState('');
  const [isRefreshingStack, setIsRefreshingStack] = useState(false);
  const [sensorArrays, setSensorArrays] = useState<SensorArraysPayload | null>(null);
  const [sensorArrayError, setSensorArrayError] = useState('');
  const [isRefreshingSensors, setIsRefreshingSensors] = useState(false);
  const [taskTemplates, setTaskTemplates] = useState<TaskTemplate[]>([]);
  const [taskInstances, setTaskInstances] = useState<TaskInstance[]>([]);
  const [taskEvents, setTaskEvents] = useState<string[]>([]);
  const [taskWorkspaceVersion, setTaskWorkspaceVersion] = useState<number | null>(null);
  const [taskWorkspacePath, setTaskWorkspacePath] = useState('szlab_canvas_workflow.json');
  const [taskScheduleEntries, setTaskScheduleEntries] = useState<ReturnType<typeof taskWorkspaceFromApi>['scheduleEntries']>([]);
  const [taskWaitingReasons, setTaskWaitingReasons] = useState<Record<string, ApiWaitingReason>>({});
  const [isTaskDetailModalOpen, setIsTaskDetailModalOpen] = useState(false);
  const [taskServiceError, setTaskServiceError] = useState('');
  const [isTaskWorkspaceLoading, setIsTaskWorkspaceLoading] = useState(false);
  const [taskSampleCount, setTaskSampleCount] = useState(3);
  const [selectedTaskTemplateId, setSelectedTaskTemplateId] = useState<string | null>(null);
  const [scheduledTemplateIds, setScheduledTemplateIds] = useState<string[]>([]);
  const [csvVariables, setCsvVariables] = useState<CsvVariable[]>([]);
  const [activeTriggerSearch, setActiveTriggerSearch] = useState<string | null>(null);
  const [triggerSearchQueries, setTriggerSearchQueries] = useState<Record<string, string>>({});
  const [resourceScheduleHeight, setResourceScheduleHeight] = useState(260);
  const [isSchedulerRunning, setIsSchedulerRunning] = useState(false);
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number } | null>(null);
  const [isTaskTemplateEditing, setIsTaskTemplateEditing] = useState(false);
  const contextMenuFirstActionRef = useRef<HTMLButtonElement | null>(null);
  const contextMenuRef = useRef<HTMLDivElement | null>(null);
  const contextMenuTriggerRef = useRef<HTMLElement | null>(null);
  const contextMenuFocusTargetRef = useRef<HTMLElement | null>(null);
  const canvasWorkspaceRef = useRef<HTMLDivElement | null>(null);
  const taskOrchestrationRef = useRef<HTMLDivElement | null>(null);
  const canvasToastTimerRef = useRef<number | null>(null);
  const taskWorkspaceStateRef = useRef<TaskWorkspaceState>(createEmptyTaskWorkspaceState());
  const taskApiRef = useRef(createTaskOrchestrationClient());
  const taskWorkspaceVersionRef = useRef<number | null>(null);
  const taskRequestQueueRef = useRef<Promise<void>>(Promise.resolve());
  const taskPollingTimerRef = useRef<number | null>(null);
  const taskPollingInFlightRef = useRef(false);
  const taskPollingGenerationRef = useRef(0);
  const applyTaskWorkspace = useCallback((response: ApiWorkspaceResponse) => {
    const next = taskWorkspaceFromApi(response);
    taskWorkspaceStateRef.current = {
      taskTemplates: next.taskTemplates,
      taskInstances: next.taskInstances,
      taskEvents: next.taskEvents,
    };
    setTaskTemplates(next.taskTemplates);
    setTaskInstances(next.taskInstances);
    setTaskEvents(next.taskEvents);
    setTaskWorkspaceVersion(next.version);
    taskWorkspaceVersionRef.current = next.version;
    setScheduledTemplateIds(next.scheduledTemplateIds);
    setTaskWaitingReasons(next.waitingReasons);
    setIsSchedulerRunning(next.isSchedulerRunning);
    setTaskScheduleEntries(next.scheduleEntries);
    setSelectedTaskTemplateId((current) => (
      next.taskTemplates.some((template) => template.id === current)
        ? current
        : next.taskTemplates[0]?.id || null
    ));
    setTaskServiceError('');
  }, []);
  const resetTaskWorkspace = useCallback(() => {
    const emptyTaskWorkspace = resetTaskWorkspaceState(taskWorkspaceStateRef.current);
    taskWorkspaceStateRef.current = emptyTaskWorkspace;
    setTaskTemplates(emptyTaskWorkspace.taskTemplates);
    setTaskInstances(emptyTaskWorkspace.taskInstances);
    setTaskEvents(emptyTaskWorkspace.taskEvents);
    setTaskWaitingReasons({});
    setSelectedTaskTemplateId(null);
    setIsTaskDetailModalOpen(false);
    setScheduledTemplateIds([]);
    setIsSchedulerRunning(false);
    setTaskWorkspaceVersion(null);
    setTaskScheduleEntries([]);
  }, []);
  const closeCanvasContextMenu = useCallback(({ restoreFocus = true }: { restoreFocus?: boolean } = {}) => {
    setContextMenu(null);
    if (!restoreFocus) return;
    window.requestAnimationFrame(() => {
      const trigger = contextMenuFocusTargetRef.current;
      if (trigger?.isConnected && !trigger.matches('[disabled], [inert]')) {
        trigger.focus();
        if (document.activeElement === trigger) return;
      }
      canvasWorkspaceRef.current?.focus();
    });
  }, []);
  const showCanvasToast = useCallback((text: string) => {
    setCanvasToast(text);
    if (canvasToastTimerRef.current !== null) {
      window.clearTimeout(canvasToastTimerRef.current);
    }
    canvasToastTimerRef.current = window.setTimeout(() => {
      setCanvasToast('');
      canvasToastTimerRef.current = null;
    }, 2000);
  }, []);
  const exitTaskTemplateEditing = useCallback(() => {
    setIsTaskTemplateEditing(false);
    setNodes((current) => current.map((node) => ({ ...node, selected: false })));
    closeCanvasContextMenu();
  }, [closeCanvasContextMenu]);
  const openCanvasContextMenu = useCallback((event: React.MouseEvent<HTMLElement>) => {
    if (!isTaskTemplateEditing) return;
    event.preventDefault();
    contextMenuTriggerRef.current = event.currentTarget;
    const activeElement = document.activeElement;
    contextMenuFocusTargetRef.current = activeElement instanceof HTMLElement
      && isRestorableContextMenuFocusTarget(activeElement, document.body, document.documentElement)
      && activeElement.isConnected
      ? activeElement
      : null;

    const nodeElement = event.target instanceof Element
      ? event.target.closest<HTMLElement>('.react-flow__node')
      : null;
    const nodeId = nodeElement?.dataset.id;
    if (nodeId) {
      setNodes((current) => {
        if (current.some((node) => node.id === nodeId && node.selected)) return current;
        return current.map((node) => ({ ...node, selected: node.id === nodeId }));
      });
    }

    const position = clampContextMenuPosition(
      event.clientX,
      event.clientY,
      window.innerWidth,
      window.innerHeight,
    );
    setContextMenu({ x: position.left, y: position.top });
  }, [isTaskTemplateEditing]);
  const importFileRef = useRef<HTMLInputElement | null>(null);
  const [config, setConfig] = useState({
    graph: DEFAULT_CONFIG.graph,
    url: DEFAULT_CONFIG.url,
    csv: DEFAULT_CONFIG.csv,
    timeout: DEFAULT_CONFIG.timeout,
    write_allowed_timeout: DEFAULT_CONFIG.write_allowed_timeout,
    no_subscription: DEFAULT_CONFIG.no_subscription,
    show_csv: DEFAULT_CONFIG.show_csv,
  });

  const nodeTypes = useMemo(() => ({ actionNode: ActionNode }), []);
  const editingNode = useMemo(
    () => nodes.find((node) => node.id === editingNodeId) || null,
    [editingNodeId, nodes],
  );
  const logEvents = useMemo(() => normalizeLogEvents(runStatus), [runStatus]);
  const opcChanges = useMemo(() => collectOpcChanges(logEvents), [logEvents]);
  const draftKey = useMemo(() => workflowDraftKey(workflowName, nodes, edges), [workflowName, nodes, edges]);
  const executionPlan = useMemo(() => createExecutionPlan(nodes, edges, startNodeId), [edges, nodes, startNodeId]);
  const renderedEdges = useMemo(() => {
    const executableEdgeEndpoints = new Set(
      executionPlan.executableEdges.map((edge) => JSON.stringify([edge.source, edge.target])),
    );
    const originalEdges = edges.map((edge) => ({
      ...edge,
      className: executableEdgeEndpoints.has(JSON.stringify([edge.source, edge.target]))
        ? undefined
        : 'execution-skipped-edge',
    }));
    const derivedEdges: Edge[] = createExecutionEdgeOverlay(edges, executionPlan.executableEdges).map(
      (edge) => ({
        ...edge,
        className: 'execution-derived-edge',
        animated: true,
        selectable: false,
        deletable: false,
        focusable: false,
      }),
    );
    return [...originalEdges, ...derivedEdges];
  }, [edges, executionPlan.executableEdges]);
  const actionGroups = useMemo(() => groupActionsByDevice(actions), [actions]);
  const selectedTaskNodes = useMemo(
    () => nodes.filter((node) => node.selected),
    [nodes],
  );
  const nodesById = useMemo(
    () => new Map(nodes.map((node) => [node.id, node])),
    [nodes],
  );
  const selectedTaskTemplate = useMemo(
    () => taskTemplates.find((template) => template.id === selectedTaskTemplateId) || taskTemplates[0] || null,
    [selectedTaskTemplateId, taskTemplates],
  );
  const taskGanttEntries = useMemo(
    () => taskScheduleEntries,
    [taskScheduleEntries],
  );
  const scheduledResources = useMemo(() => [
    ...new Set(
      scheduledTemplateIds.length
        ? taskGanttEntries.map((entry) => entry.resource)
        : [],
    ),
  ], [scheduledTemplateIds.length, taskGanttEntries]);
  const toggleActionGroup = useCallback((groupId: string) => {
    setCollapsedActionGroups((current) => ({
      ...current,
      [groupId]: !current[groupId],
    }));
  }, []);
  const configuredOpcVariables = useMemo(() => {
    const nodeVariables = nodes.flatMap((node) => node.data.opcVariables || []);
    if (nodeVariables.length) return uniqueOpcVariables(nodeVariables);
    return uniqueOpcVariables(actions.flatMap((action) => action.opc_variables || []));
  }, [actions, nodes]);
  const stackSensorValues = useMemo(() => stackSensorValuesFromStatus(stackStatus), [stackStatus]);
  const liveGateStates = useMemo(() => liveSensorGateStates(sensorArrays), [sensorArrays]);
  const effectiveSensorGates = useMemo(
    () => Object.fromEntries(
      Object.entries(sensorGates).map(([gate, manualFree]) => [
        gate,
        liveGateStates[gate] ?? manualFree,
      ]),
    ),
    [liveGateStates, sensorGates],
  );
  const configuredOpcVariableRows = useMemo<OpcVariableView[]>(
    () => configuredOpcVariables.map((name) => ({ name, currentValue: stackSensorValues[name] })),
    [configuredOpcVariables, stackSensorValues],
  );
  useEffect(() => {
    const params = new URLSearchParams();
    if (config.csv) params.set('csv_path', config.csv);
    fetch(`/api/csv-variables?${params.toString()}`)
      .then((response) => response.ok ? response.json() : { variables: [] })
      .then((payload) => setCsvVariables(Array.isArray(payload.variables) ? payload.variables : []))
      .catch(() => setCsvVariables([]));
  }, [config.csv]);
  const loadTaskWorkspace = useCallback(async () => {
    setIsTaskWorkspaceLoading(true);
    try {
      applyTaskWorkspace(await taskApiRef.current.getWorkspace(taskWorkspacePath));
    } catch (error) {
      setTaskServiceError(taskApiErrorMessage(error));
    } finally {
      setIsTaskWorkspaceLoading(false);
    }
  }, [applyTaskWorkspace, taskWorkspacePath]);
  useEffect(() => {
    if (workspace === 'tasks') void loadTaskWorkspace();
  }, [loadTaskWorkspace, workspace]);
  const stackResources = useMemo(() => stackResourcesFromStatus(stackStatus), [stackStatus]);
  const selectedStack = useMemo(
    () => stackResources.find((stack) => stack.id === selectedStackId) || stackResources[0] || null,
    [selectedStackId, stackResources],
  );
  const selectedStackPayload = selectedStack ? stackStatus?.stacks?.[selectedStack.id] : undefined;
  const selectedStackSlots = useMemo(
    () => stackSlotsFromPayload(selectedStackPayload),
    [selectedStackPayload],
  );
  const stationSummary = useMemo(
    () => stackResources.map((stack) => ({
      label: stack.title,
      value: `${stack.total} 槽 / ${stack.used} 已占用`,
      status: stack.used > 0 ? 'ok' : 'empty',
    })),
    [stackResources],
  );
  const workspaceSummary = useMemo(
    () => buildWorkspaceSummary({
      nodes,
      edges,
      opcChangeCount: opcChanges.length,
      runStatus: runStatus?.status,
    }),
    [edges, nodes, opcChanges.length, runStatus?.status],
  );

  useEffect(() => {
    if (selectedLogNodeId && !nodes.some((node) => node.id === selectedLogNodeId)) {
      setSelectedLogNodeId(null);
    }
  }, [nodes, selectedLogNodeId]);

  useEffect(() => {
    taskWorkspaceStateRef.current = { taskTemplates, taskInstances, taskEvents };
  }, [taskEvents, taskInstances, taskTemplates]);

  useEffect(() => {
    return () => {
      if (canvasToastTimerRef.current !== null) {
        window.clearTimeout(canvasToastTimerRef.current);
      }
    };
  }, []);

  useEffect(() => {
    if (!contextMenu) return;
    contextMenuFirstActionRef.current?.focus();
    const closeContextMenuOnEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      event.preventDefault();
      closeCanvasContextMenu();
    };
    document.addEventListener('keydown', closeContextMenuOnEscape);
    return () => document.removeEventListener('keydown', closeContextMenuOnEscape);
  }, [closeCanvasContextMenu, contextMenu]);

  useEffect(() => {
    if (!contextMenu) return;
    const closeContextMenuOnExternalPointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) return;
      if (contextMenuRef.current?.contains(target) || contextMenuTriggerRef.current?.contains(target)) {
        return;
      }
      closeCanvasContextMenu({ restoreFocus: false });
    };
    document.addEventListener('pointerdown', closeContextMenuOnExternalPointerDown, true);
    return () => document.removeEventListener('pointerdown', closeContextMenuOnExternalPointerDown, true);
  }, [closeCanvasContextMenu, contextMenu]);

  useEffect(() => {
    fetch('/api/preset')
      .then((response) => response.json())
      .then((payload: PresetPayload) => {
        const payloadActions = payload.actions || [];
        const storageKey = `${DRAFT_STORAGE_PREFIX}.${payload.id || 'default'}`;
        setTitle(payload.title || 'szlab 本地调试工具');
        setActions(payloadActions);
        setDraftStorageKey(storageKey);
        setTaskWorkspacePath(`${payload.default_workflow_name || 'szlab_canvas_workflow'}.json`);
        const savedDraft = loadSavedDraft(storageKey, payloadActions);
        if (savedDraft) {
          setWorkflowName(savedDraft.name);
          setNodes(savedDraft.nodes);
          setEdges(savedDraft.edges.map((edge) => ({ ...edge, animated: true })));
          setStartNodeId(loadSavedStartNodeId(storageKey, savedDraft.nodes));
        } else {
          setWorkflowName(payload.default_workflow_name || 'szlab_canvas_workflow');
          setStartNodeId(null);
        }
        setConfig((current) => ({
          ...current,
          graph: payload.default_config?.graph ?? DEFAULT_CONFIG.graph,
          url: payload.default_config?.url ?? DEFAULT_CONFIG.url,
          csv: payload.default_config?.csv ?? DEFAULT_CONFIG.csv,
          timeout: payload.default_config?.timeout ?? DEFAULT_CONFIG.timeout,
          write_allowed_timeout: payload.default_config?.write_allowed_timeout ?? DEFAULT_CONFIG.write_allowed_timeout,
          no_subscription: payload.default_config?.no_subscription ?? DEFAULT_CONFIG.no_subscription,
          show_csv: payload.default_config?.show_csv ?? DEFAULT_CONFIG.show_csv,
        }));
        setDraftReady(true);
      })
      .catch((error) => setMessage(`preset 加载失败: ${error.message}`));
  }, [resetTaskWorkspace]);

  const refreshStackStatus = useCallback(async () => {
    setIsRefreshingStack(true);
    try {
      const response = await fetch('/api/stack-status');
      const payload: StackStatusPayload = await response.json();
      setStackStatus(payload);
      setStackError(payload.success ? '' : (payload.message || '堆栈状态暂不可用'));
    } catch (error) {
      setStackError(error instanceof Error ? error.message : String(error));
    } finally {
      setIsRefreshingStack(false);
    }
  }, []);

  const refreshSensorArrays = useCallback(async () => {
    setIsRefreshingSensors(true);
    try {
      const response = await fetch('/api/sensor-arrays');
      const payload: SensorArraysPayload = await response.json();
      setSensorArrays(payload);
      setSensorArrayError(
        payload.success || payload.partial
          ? ''
          : (payload.message || '实机传感器状态暂不可用'),
      );
    } catch (error) {
      setSensorArrayError(error instanceof Error ? error.message : String(error));
    } finally {
      setIsRefreshingSensors(false);
    }
  }, []);

  useEffect(() => {
    let stopped = false;
    let refreshTimer: number | null = null;
    const refresh = async () => {
      await refreshStackStatus();
      if (!stopped) await refreshSensorArrays();
    };
    const scheduleRefresh = () => {
      if (stopped || refreshTimer !== null) return;
      refreshTimer = window.setTimeout(() => {
        refreshTimer = null;
        if (!stopped) void refresh();
      }, 80);
    };

    void refresh();
    const sensorEvents = new EventSource('/api/sensor-events');
    sensorEvents.addEventListener('sensor-change', scheduleRefresh);
    return () => {
      stopped = true;
      sensorEvents.close();
      if (refreshTimer !== null) window.clearTimeout(refreshTimer);
    };
  }, [refreshSensorArrays, refreshStackStatus]);

  useEffect(() => {
    if (!stackResources.length) {
      setSelectedStackId('');
      return;
    }
    if (!stackResources.some((stack) => stack.id === selectedStackId)) {
      setSelectedStackId(stackResources[0].id);
    }
  }, [selectedStackId, stackResources]);

  useEffect(() => {
    if (!draftReady || !draftStorageKey) return;
    try {
      window.localStorage.setItem(draftStorageKey, JSON.stringify(createWorkflowRequest(workflowName, nodes, edges)));
      if (startNodeId && nodes.some((node) => node.id === startNodeId)) {
        window.localStorage.setItem(`${draftStorageKey}.startNodeId`, startNodeId);
      } else {
        window.localStorage.removeItem(`${draftStorageKey}.startNodeId`);
      }
    } catch (error) {
      setMessage(`本地草稿保存失败: ${error instanceof Error ? error.message : String(error)}`);
    }
  }, [draftKey, draftReady, draftStorageKey, nodes, startNodeId]);

  const onNodesChange = useCallback(
    (changes: NodeChange[]) => setNodes((current) => applyNodeChanges(changes, current)),
    [],
  );

  const onEdgesChange = useCallback(
    (changes: EdgeChange[]) => setEdges((current) => applyEdgeChanges(changes, current)),
    [],
  );

  const onConnect = useCallback(
    (connection: Connection) => {
      setEdges((current) =>
        addEdge(
          {
            ...connection,
            id: `${connection.source}-${connection.target}-${Date.now()}`,
            animated: true,
          },
          current,
        ),
      );
    },
    [],
  );

  const addActionNode = (action: ActionSpec) => {
    const count = nodes.length + 1;
    const id = `node_${count}_${Date.now().toString(36)}`;
    const lastNode = nodes[nodes.length - 1];
    const nextPosition = lastNode
      ? { x: lastNode.position.x + 210, y: lastNode.position.y + ((count % 2 === 0) ? 32 : -32) }
      : { x: 80, y: 120 };
    setNodes((current) => [
      ...current,
      {
        id,
        type: 'actionNode',
        position: nextPosition,
        data: {
          deviceId: action.device_id,
          method: action.method,
          label: action.label,
          description: action.description,
          params: buildDefaultParams(action.params || []),
          paramSpecs: action.params || [],
          opcVariables: action.opc_variables || [],
          runStatus: 'idle',
          executionDisabled: false,
          executionBypassed: false,
        },
      },
    ]);
  };

  const updateNodeParam = (nodeId: string, name: string, value: unknown) => {
    setNodes((current) =>
      current.map((node) =>
        node.id === nodeId
          ? { ...node, data: { ...node.data, params: { ...node.data.params, [name]: value } } }
          : node,
      ),
    );
  };

  const setExecutionStart = (nodeId: string) => {
    setStartNodeId((current) => (current === nodeId ? null : nodeId));
    showCanvasToast(startNodeId === nodeId ? '已恢复从头开始运行' : '已设置起始节点');
  };

  const toggleNodeDisabled = (nodeId: string) => {
    setNodes((current) =>
      current.map((node) =>
        node.id === nodeId
          ? {
              ...node,
              data: {
                ...node.data,
                executionDisabled: !node.data.executionDisabled,
                executionBypassed: false,
              },
            }
          : node,
      ),
    );
    showCanvasToast('已更新节点执行范围');
  };

  const toggleNodeBypassed = (nodeId: string) => {
    setNodes((current) =>
      current.map((node) =>
        node.id === nodeId
          ? {
              ...node,
              data: {
                ...node.data,
                executionBypassed: !node.data.executionBypassed,
                executionDisabled: false,
              },
            }
          : node,
      ),
    );
    showCanvasToast('已更新节点直通状态');
  };

  const appendTaskEvent = useCallback((message: string) => {
    const time = new Date().toLocaleTimeString('zh-CN', { hour12: false });
    setTaskEvents((current) => [`${time} ${message}`, ...current].slice(0, 20));
  }, []);
  const mutateTaskWorkspace = useCallback(async (
    operation: (version: number) => Promise<ApiWorkspaceResponse>,
    canApply: () => boolean = () => true,
  ) => {
    const execute = async () => {
      const run = async () => {
        const version = taskWorkspaceVersionRef.current;
        if (version === null) throw new TaskOrchestrationServiceUnavailableError();
        return operation(version);
      };
      try {
        const response = await run();
        if (canApply()) applyTaskWorkspace(response);
      } catch (error) {
        if (error instanceof TaskOrchestrationBusinessError && error.status === 409) {
          try {
            const latest = await taskApiRef.current.getWorkspace(taskWorkspacePath);
            if (canApply()) applyTaskWorkspace(latest);
            const response = await operation(latest.version);
            if (canApply()) applyTaskWorkspace(response);
            return;
          } catch (retryError) {
            setTaskServiceError(taskApiErrorMessage(retryError));
            return;
          }
        }
        setTaskServiceError(taskApiErrorMessage(error));
      }
    };
    const queued = taskRequestQueueRef.current.then(execute, execute);
    taskRequestQueueRef.current = queued.catch(() => undefined);
    return queued;
  }, [applyTaskWorkspace, taskWorkspacePath]);

  const createTaskTemplateFromNodes = useCallback((templateName: string, templateNodes: Node<ActionNodeData>[]) => {
    const orderedNodeIds = orderSelectedNodesByPlan(templateNodes, executionPlan.executableNodes).map((node) => node.id);
    if (!orderedNodeIds.length) {
      setMessage('请先在流程画布中选择节点，再保存为 Task 模板。');
      return;
    }
    const selectedNodesById = new Map(nodes.map((node) => [node.id, node]));
    const orderedNodes = orderedNodeIds.map((nodeId) => selectedNodesById.get(nodeId)).filter(Boolean) as Node<ActionNodeData>[];
    const resources = uniqueTaskKeys(orderedNodes.flatMap(inferTaskResources));
    const gates = uniqueTaskKeys(orderedNodes.flatMap(inferTaskGates));
    const triggers = createTaskTemplateTriggers(resources, gates);
    const taskName = templateName || summarizeTaskName(orderedNodes);
    const templateId = `task_${Date.now().toString(36)}_${taskTemplates.length + 1}`;
    void mutateTaskWorkspace(async (version) => {
      const response = await taskApiRef.current.createTemplate(taskWorkspacePath, version, {
        id: templateId,
        name: taskName,
        workflow_path: taskWorkspacePath,
        node_ids: orderedNodeIds,
        resources,
        trigger: null,
        input_triggers: triggers.inputTriggers.map(toApiTrigger),
        output_triggers: triggers.outputTriggers.map(toApiTrigger),
      });
      setSelectedTaskTemplateId(templateId);
      showCanvasToast('已保存 Task 模板');
      return response;
    });
  }, [executionPlan.executableNodes, mutateTaskWorkspace, nodes, showCanvasToast, taskTemplates.length, taskWorkspacePath]);

  const createTaskTemplateFromSelection = useCallback(() => {
    createTaskTemplateFromNodes(summarizeTaskName(selectedTaskNodes), selectedTaskNodes);
  }, [createTaskTemplateFromNodes, selectedTaskNodes]);

  const createRecommendedTaskTemplates = useCallback(() => {
    const orderedNodes = executionPlan.executableNodes.length ? executionPlan.executableNodes : nodes;
    const chunks = chunkNodesForTaskPreview(orderedNodes);
    if (!chunks.length) {
      setMessage('当前画布没有可切分的节点。');
      return;
    }
    const templates = chunks.map((chunk, index) => {
      const resources = uniqueTaskKeys(chunk.flatMap(inferTaskResources));
      const gates = uniqueTaskKeys(chunk.flatMap(inferTaskGates));
      const triggers = createTaskTemplateTriggers(resources, gates);
      return {
        id: `auto_task_${Date.now().toString(36)}_${index + 1}`,
        name: summarizeTaskName(chunk),
        nodeIds: chunk.map((node) => node.id),
        resources,
        gates,
        ...triggers,
      };
    });
    void (async () => {
      for (const template of templates) {
        await mutateTaskWorkspace((version) => taskApiRef.current.createTemplate(taskWorkspacePath, version, {
            id: template.id,
            name: template.name,
            workflow_path: taskWorkspacePath,
            node_ids: template.nodeIds,
            resources: template.resources,
            trigger: null,
            input_triggers: template.inputTriggers.map(toApiTrigger),
            output_triggers: template.outputTriggers.map(toApiTrigger),
          }));
      }
      setSelectedTaskTemplateId(templates[0]?.id || null);
      showCanvasToast('已生成推荐 Task 切分');
    })();
  }, [executionPlan.executableNodes, mutateTaskWorkspace, nodes, showCanvasToast, taskWorkspacePath]);

  const deleteTaskTemplate = useCallback((template: TaskTemplate) => {
    if (!taskTemplates.some((item) => item.id === template.id)) {
      return;
    }
    const relatedInstanceCount = taskInstances.filter((task) => task.templateId === template.id).length;
    const relatedInstancesHint = relatedInstanceCount
      ? `这将同时删除 ${relatedInstanceCount} 个关联 Task 实例。`
      : '';
    if (!window.confirm(`确定删除 Task 模板「${template.name}」吗？${relatedInstancesHint}`)) {
      return;
    }
    void mutateTaskWorkspace(async (version) => {
      const response = await taskApiRef.current.deleteTemplate(taskWorkspacePath, version, template.id);
      showCanvasToast('已删除 Task 模板');
      return response;
    });
  }, [mutateTaskWorkspace, showCanvasToast, taskInstances, taskTemplates, taskWorkspacePath]);

  const renameSelectedTaskTemplate = useCallback((name: string) => {
    if (!selectedTaskTemplate) return;
    void mutateTaskWorkspace((version) => taskApiRef.current.updateTemplate(
      taskWorkspacePath,
      version,
      selectedTaskTemplate.id,
      { name },
    ));
  }, [mutateTaskWorkspace, selectedTaskTemplate, taskWorkspacePath]);

  const updateSelectedTaskTriggers = useCallback((kind: 'input' | 'output', triggers: TriggerCondition[]) => {
    if (!selectedTaskTemplate) return;
    const normalized = normalizeTriggerConditions(triggers, csvVariables);
    void mutateTaskWorkspace((version) => taskApiRef.current.updateTemplate(
      taskWorkspacePath,
      version,
      selectedTaskTemplate.id,
      kind === 'input'
        ? { input_triggers: normalized.map(toApiTrigger) }
        : { output_triggers: normalized.map(toApiTrigger) },
    ));
  }, [csvVariables, mutateTaskWorkspace, selectedTaskTemplate, taskWorkspacePath]);

  const startResourceScheduleResize = useCallback((event: React.PointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    const startY = event.clientY;
    const startHeight = resourceScheduleHeight;
    const containerHeight = taskOrchestrationRef.current?.clientHeight || window.innerHeight;
    const maxHeight = Math.max(180, containerHeight - 260);
    const onPointerMove = (moveEvent: PointerEvent) => {
      setResourceScheduleHeight(Math.max(180, Math.min(maxHeight, startHeight + startY - moveEvent.clientY)));
    };
    const onPointerUp = () => {
      document.body.style.userSelect = '';
      window.removeEventListener('pointermove', onPointerMove);
      window.removeEventListener('pointerup', onPointerUp);
    };
    document.body.style.userSelect = 'none';
    window.addEventListener('pointermove', onPointerMove);
    window.addEventListener('pointerup', onPointerUp);
  }, [resourceScheduleHeight]);

  const addTemplateToSchedule = useCallback((templateId: string) => {
    const template = taskTemplates.find((item) => item.id === templateId);
    if (!template) return;
    if (scheduledTemplateIds.includes(templateId)) return;
    void mutateTaskWorkspace((version) => taskApiRef.current.updateScheduledTemplates(
      taskWorkspacePath, version, [...scheduledTemplateIds, templateId],
    ));
  }, [mutateTaskWorkspace, scheduledTemplateIds, taskTemplates, taskWorkspacePath]);

  const removeTemplateFromSchedule = useCallback((templateId: string) => {
    void mutateTaskWorkspace((version) => taskApiRef.current.updateScheduledTemplates(
      taskWorkspacePath, version, scheduledTemplateIds.filter((id) => id !== templateId),
    ));
  }, [mutateTaskWorkspace, scheduledTemplateIds, taskWorkspacePath]);

  const createTaskInstances = useCallback(() => {
    if (!scheduledTemplateIds.length) {
      setMessage('请先将 Task Template 拖入 Resource Schedule。');
      return;
    }
    const samples = SAMPLE_NAMES.slice(0, taskSampleCount);
    void mutateTaskWorkspace((version) => taskApiRef.current.generateInstances(
      taskWorkspacePath, version, scheduledTemplateIds, samples,
    ));
  }, [mutateTaskWorkspace, scheduledTemplateIds, taskSampleCount, taskWorkspacePath]);

  const moveTaskInstance = useCallback((taskId: string, direction: -1 | 1) => {
    const task = taskInstances.find((item) => item.id === taskId);
    if (!task || task.status !== 'waiting' && task.status !== 'pending') return;
    const sampleQueue = taskInstances
      .filter((item) => item.sample === task.sample)
      .sort((left, right) => left.order - right.order);
    const targetOrder = sampleQueue.findIndex((item) => item.id === taskId) + direction;
    if (targetOrder < 0 || targetOrder >= sampleQueue.length) return;
    void mutateTaskWorkspace((version) => taskApiRef.current.moveInstance(
      taskWorkspacePath, version, taskId, targetOrder,
    ));
  }, [mutateTaskWorkspace, taskInstances, taskWorkspacePath]);

  const advanceTaskSchedule = useCallback(() => {
    void mutateTaskWorkspace((version) => taskApiRef.current.advance(taskWorkspacePath, version));
  }, [mutateTaskWorkspace, taskWorkspacePath]);

  const setTaskSchedulerPaused = useCallback((paused: boolean) => {
    void mutateTaskWorkspace((version) => taskApiRef.current.plan(taskWorkspacePath, version, paused));
  }, [mutateTaskWorkspace, taskWorkspacePath]);

  useEffect(() => {
    taskPollingGenerationRef.current += 1;
    const generation = taskPollingGenerationRef.current;
    if (taskPollingTimerRef.current !== null) {
      window.clearInterval(taskPollingTimerRef.current);
      taskPollingTimerRef.current = null;
    }
    taskPollingInFlightRef.current = false;
    if (!isSchedulerRunning || taskServiceError) return;
    const poll = () => {
      if (taskPollingInFlightRef.current || generation !== taskPollingGenerationRef.current) return;
      taskPollingInFlightRef.current = true;
      void mutateTaskWorkspace(
        (version) => taskApiRef.current.advance(taskWorkspacePath, version),
        () => generation === taskPollingGenerationRef.current,
      ).finally(() => {
        if (generation === taskPollingGenerationRef.current) {
          taskPollingInFlightRef.current = false;
        }
      });
    };
    poll();
    taskPollingTimerRef.current = window.setInterval(poll, 1500);
    return () => {
      taskPollingGenerationRef.current += 1;
      if (taskPollingTimerRef.current !== null) {
        window.clearInterval(taskPollingTimerRef.current);
        taskPollingTimerRef.current = null;
      }
      taskPollingInFlightRef.current = false;
    };
  }, [isSchedulerRunning, mutateTaskWorkspace, taskServiceError, taskWorkspacePath]);

  const buildWorkflow = useCallback(async () => {
    if (!executionPlan.executableNodes.length) {
      throw new Error('当前没有可执行节点，请调整起始节点或禁用状态');
    }
    const request = createWorkflowRequest(workflowName, executionPlan.executableNodes, executionPlan.executableEdges);
    const response = await fetch('/api/workflow/build-graph', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.detail || '生成 workflow 失败');
    }
    setWorkflow(payload);
    setMessage('');
    return payload as WorkflowJson;
  }, [executionPlan.executableEdges, executionPlan.executableNodes, workflowName]);

  const exportPseudoFlow = () => {
    try {
      const flow = createPseudoFlowJson(workflowName, nodes, edges);
      downloadJson(`${workflowName || 'workflow'}_flow.json`, flow);
      setMessage(`已导出 ${flow.rules.length} 条 pseudo flow 规则`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };

  const autoLayoutNodes = () => {
    setNodes((current) => layoutFlowGraph(current, edges));
    showCanvasToast('已自动优化节点布局');
  };

  const importFlowJson = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.currentTarget.files?.[0];
    event.currentTarget.value = '';
    if (!file) return;

    try {
      const parsed = JSON.parse(await file.text());
      const imported = createImportedDraft(parsed, actions, { autoLayout: true }) as {
        name: string;
        nodes: Node<ActionNodeData>[];
        edges: Edge[];
      };
      setWorkflowName(imported.name);
      setTaskWorkspacePath(file.name);
      setNodes(imported.nodes);
      setEdges(imported.edges.map((edge) => ({ ...edge, animated: true })));
      setStartNodeId(null);
      setWorkflow(null);
      setRunStatus(null);
      setActiveRunId(null);
      setSelectedLogNodeId(null);
      setEditingNodeId(null);
      exitTaskTemplateEditing();
      setMessage(`已导入 ${imported.nodes.length} 个节点，并自动优化布局`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };

  useEffect(() => {
    if (!nodes.length) {
      setWorkflow(null);
      setMessage('');
      return;
    }
    const timer = window.setTimeout(() => {
      buildWorkflow().catch((error) => {
        setWorkflow(null);
        setMessage(error.message);
      });
    }, 250);
    return () => window.clearTimeout(timer);
  }, [draftKey, nodes.length, startNodeId]);

  const runWorkflow = async () => {
    try {
      const builtWorkflow = await buildWorkflow();
      setIsRunning(true);
      setSelectedLogNodeId(null);
      setRunStatus({ run_id: '', status: 'pending', logs: ['启动 workflow...'] });
      setNodes((current) => current.map((node) => ({ ...node, data: { ...node.data, runStatus: 'idle' } })));
      const response = await fetch('/api/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ workflow: builtWorkflow, ...config }),
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.detail || '启动失败');
      }
      setRunStatus(payload);
      setActiveRunId(payload.run_id);
      applyNodeStatuses(payload.node_statuses);
      pollRun(payload.run_id);
    } catch (error) {
      setIsRunning(false);
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };

  const pollRun = (runId: string) => {
    const fetchRunStatus = async () => {
      const response = await fetch(`/api/run/${runId}`);
      const payload: RunStatus = await response.json();
      setRunStatus(payload);
      applyNodeStatuses(payload.node_statuses);
      if (['completed', 'failed', 'cancelled'].includes(payload.status)) {
        window.clearInterval(timer);
        setIsRunning(false);
        setActiveRunId(null);
      }
    };
    const timer = window.setInterval(fetchRunStatus, 1000);
    void fetchRunStatus();
  };

  const cancelWorkflow = async () => {
    if (!activeRunId) return;
    try {
      const response = await fetch(`/api/run/${activeRunId}/cancel`, { method: 'POST' });
      const payload: RunStatus = await response.json();
      if (!response.ok) {
        throw new Error((payload as unknown as { detail?: string }).detail || '终止失败');
      }
      setRunStatus(payload);
      applyNodeStatuses(payload.node_statuses);
      setMessage('');
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    }
  };

  const applyNodeStatuses = (nodeStatuses?: Record<string, NodeRunStatus>) => {
    if (!nodeStatuses) return;
    setNodes((current) => {
      let hasChange = false;
      const next = current.map((node) => {
        const nextStatus = nodeStatuses[node.id] || node.data.runStatus || 'idle';
        if (node.data.runStatus === nextStatus) {
          return node;
        }
        hasChange = true;
        return {
          ...node,
          data: {
            ...node.data,
            runStatus: nextStatus,
          },
        };
      });
      return hasChange ? next : current;
    });
  };

  return (
    <div className="demo-shell demo-tool-shell">
      <header className="demo-tool-header">
        <div>
          <h1>{title}</h1>
        </div>
        <dl className="demo-header-metrics" aria-label="联调状态摘要">
          <div>
            <dt>状态</dt>
            <dd>{workspaceSummary.runStatusText}</dd>
          </div>
          <div>
            <dt>节点</dt>
            <dd>{workspaceSummary.totalNodes}</dd>
          </div>
          <div>
            <dt>设备</dt>
            <dd>{workspaceSummary.deviceCount}</dd>
          </div>
          <div>
            <dt>OPC</dt>
            <dd>{workspaceSummary.opcChangeCount}</dd>
          </div>
        </dl>
      </header>

      <nav className="workspace-navigation" aria-label="一级工作区">
        <button
          aria-pressed={workspace === 'workflow'}
          className={workspace === 'workflow' ? 'active' : ''}
          onClick={() => {
            setWorkspace('workflow');
            exitTaskTemplateEditing();
          }}
          type="button"
        >
          流程设计
        </button>
        <button
          aria-pressed={workspace === 'tasks'}
          className={workspace === 'tasks' ? 'active' : ''}
          onClick={() => {
            setWorkspace('tasks');
            exitTaskTemplateEditing();
          }}
          type="button"
        >
          Task 编排
        </button>
      </nav>

      {workspace === 'workflow' && (
      <main className={`demo-workbench${leftPanelCollapsed ? ' left-collapsed' : ''}`}>
        <aside className={`demo-card demo-action-panel${leftPanelCollapsed ? ' collapsed' : ''}`}>
          <div className="demo-panel-title">
            <h2>联调入口</h2>
            <span>Device / Stack</span>
            <button
              aria-label={leftPanelCollapsed ? '展开联调入口' : '收起联调入口'}
              className="demo-panel-collapse-button"
              onClick={() => setLeftPanelCollapsed((collapsed) => !collapsed)}
              type="button"
            >
              {leftPanelCollapsed ? '›' : '‹'}
            </button>
          </div>
          <div className="demo-tabbar left" role="tablist" aria-label="联调入口切换">
            <button className={leftTab === 'devices' ? 'active' : ''} onClick={() => setLeftTab('devices')} type="button">设备动作</button>
            <button className={leftTab === 'stacks' ? 'active' : ''} onClick={() => setLeftTab('stacks')} type="button">堆栈</button>
          </div>
          <div className="demo-left-sections">
            {leftTab === 'devices' && (
              <section className="demo-left-section">
                <div className="demo-left-section-head">
                  <strong>设备动作</strong>
                  <span>Device actions</span>
                </div>
                <div className="demo-action-tree">
                  {actionGroups.map((group) => {
                    const collapsed = Boolean(collapsedActionGroups[group.id]);
                    return (
                    <section className="demo-action-tree-group" key={group.id}>
                      <button
                        className="demo-action-tree-parent"
                        type="button"
                        aria-expanded={!collapsed}
                        onClick={() => toggleActionGroup(group.id)}
                      >
                        <span className="demo-action-tree-chevron" aria-hidden="true">▼</span>
                        <strong>{group.title}</strong>
                        <code>{group.device}</code>
                        <span>{group.actions.length} 项</span>
                      </button>
                      {!collapsed && (
                      <div className="demo-action-tree-children">
                        {group.actions.map((action) => (
                          <button className="demo-action-row" key={action.method} onClick={() => addActionNode(action)} type="button">
                            <span title={action.label}>{action.label}</span>
                            <code title={action.method}>{action.method}</code>
                            <em>可用</em>
                          </button>
                        ))}
                      </div>
                      )}
                    </section>
                    );
                  })}
                </div>
              </section>
            )}

            {leftTab === 'stacks' && (
              <section className="demo-left-section stack-entry">
                <div className="demo-left-section-head">
                  <strong>堆栈</strong>
                  <span>Stack</span>
                  <button
                    className="demo-table-action"
                    disabled={isRefreshingStack}
                    onClick={() => refreshStackStatus()}
                    type="button"
                  >
                    {isRefreshingStack ? '刷新中' : '刷新堆栈'}
                  </button>
                </div>
                <div className="demo-stack-scroll">
                <table className="demo-stack-resource-table">
                  <thead>
                    <tr>
                      <th>堆栈</th>
                      <th>占用</th>
                      <th>下一槽</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {stackResources.map((stack) => (
                      <tr
                        className={selectedStack?.id === stack.id ? 'active' : ''}
                        key={stack.id}
                        onClick={() => setSelectedStackId(stack.id)}
                        onDoubleClick={() => setShowStackModal(true)}
                      >
                        <td>
                          <strong>{stack.title}</strong>
                          <span>{stack.role}</span>
                        </td>
                        <td>{stack.used}/{stack.total}</td>
                        <td>{stack.nextSlot}</td>
                        <td>
                          <button className="demo-table-action" onClick={() => setShowStackModal(true)} type="button">详情</button>
                        </td>
                      </tr>
                    ))}
                    {!stackResources.length && (
                      <tr>
                        <td colSpan={4}>
                          <strong>等待真实堆栈数据</strong>
                          <span>{stackError || '正在读取 /api/stack-status'}</span>
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
                </div>
              </section>
            )}
          </div>
        </aside>

        <section className="demo-card demo-main-panel">
          <div className="demo-canvas-toolbar">
            <div>
              <strong>{workflowName}</strong>
            </div>
            <div className="demo-toolbar-actions">
              <input
                ref={importFileRef}
                className="visually-hidden"
                type="file"
                accept=".json,application/json"
                onChange={importFlowJson}
              />
              <button onClick={() => importFileRef.current?.click()}>导入 Flow JSON</button>
              <button onClick={() => buildWorkflow().catch((error) => setMessage(error.message))}>校验流程</button>
              <button onClick={exportPseudoFlow} disabled={!nodes.length}>导出 Flow JSON</button>
            </div>
          </div>

          <div className="demo-tabbar canvas-tabs" role="tablist" aria-label="流程设计视图切换">
            <button className={canvasTab === 'workflow' ? 'active' : ''} onClick={() => setCanvasTab('workflow')} type="button">流程画布</button>
            <button className={canvasTab === 'sensors' ? 'active' : ''} onClick={() => { setCanvasTab('sensors'); exitTaskTemplateEditing(); }} type="button">传感器快照</button>
          </div>

          {canvasTab === 'workflow' && (
            <div
              className="demo-canvas real-flow-canvas"
              ref={canvasWorkspaceRef}
              tabIndex={-1}
              onContextMenuCapture={openCanvasContextMenu}
            >
              {isTaskTemplateEditing && <div className="task-template-editing-hint">模板编辑中：拖拽框选节点后右键创建模板</div>}
              <ReactFlow
                nodes={nodes.map((node) => ({
                  ...node,
                  data: {
                    ...node.data,
                    executionState: executionPlan.nodeStates[node.id]?.reason || 'willRun',
                    isExecutionStart: executionPlan.startNodeId === node.id,
                    onPositionChange: (nodeId: string, value: number) => updateNodeParam(nodeId, 'position', value),
                    onSetStart: setExecutionStart,
                    onToggleBypassed: toggleNodeBypassed,
                    onToggleDisabled: toggleNodeDisabled,
                    onEditParams: setEditingNodeId,
                  },
                }))}
                edges={renderedEdges}
                nodeTypes={nodeTypes}
                onNodesChange={onNodesChange}
                onEdgesChange={onEdgesChange}
                onConnect={onConnect}
                onNodeClick={() => closeCanvasContextMenu({ restoreFocus: false })}
                onNodeDoubleClick={(_, node) => setEditingNodeId(node.id)}
                onPaneClick={() => closeCanvasContextMenu({ restoreFocus: false })}
                selectionOnDrag={isTaskTemplateEditing}
                panOnDrag={!isTaskTemplateEditing}
                defaultEdgeOptions={{ type: 'smoothstep', animated: true }}
              >
                <Background />
                <MiniMap />
                <Controls>
                  <ControlButton
                    aria-label="自动布局"
                    title="自动布局"
                    onClick={autoLayoutNodes}
                    disabled={!nodes.length}
                  >
                    <svg className="auto-layout-icon" viewBox="0 0 24 24" aria-hidden="true">
                      <path d="M4 5h5v5H4V5Zm11 0h5v5h-5V5ZM4 14h5v5H4v-5Zm11 0h5v5h-5v-5ZM9 7.5h6M9 16.5h6M6.5 10v4M17.5 10v4" />
                    </svg>
                  </ControlButton>
                  <ControlButton
                    aria-label="切换 Task 模板编辑"
                    aria-pressed={isTaskTemplateEditing}
                    className={isTaskTemplateEditing ? 'active' : ''}
                    title={isTaskTemplateEditing ? '退出 Task 模板编辑' : '进入 Task 模板编辑'}
                    onClick={() => {
                      if (isTaskTemplateEditing) {
                        exitTaskTemplateEditing();
                      } else {
                        setIsTaskTemplateEditing(true);
                      }
                    }}
                  >
                    <svg className="auto-layout-icon" viewBox="0 0 24 24" aria-hidden="true">
                      <path d="M4 6h14M4 12h14M4 18h9M18 17v4M16 19h4" />
                    </svg>
                  </ControlButton>
                </Controls>
              </ReactFlow>
              {isTaskTemplateEditing && contextMenu && (
                <div
                  className="canvas-context-menu"
                  ref={contextMenuRef}
                  role="dialog"
                  aria-label="流程画布操作"
                  style={{ left: contextMenu.x, top: contextMenu.y }}
                >
                  <button
                    disabled={!selectedTaskNodes.length}
                    onClick={() => {
                      createTaskTemplateFromSelection();
                      closeCanvasContextMenu();
                    }}
                    ref={selectedTaskNodes.length ? contextMenuFirstActionRef : undefined}
                    type="button"
                  >
                    设为 Task 模板
                  </button>
                  <button
                    onClick={() => {
                      setNodes((current) => current.map((node) => ({ ...node, selected: false })));
                      closeCanvasContextMenu();
                    }}
                    ref={!selectedTaskNodes.length ? contextMenuFirstActionRef : undefined}
                    type="button"
                  >
                    取消选择
                  </button>
                </div>
              )}
            </div>
          )}

          {canvasTab === 'sensors' && (
            <div className="demo-opc-dock tabbed">
              <SensorArrayPanel
                error={sensorArrayError}
                isRefreshing={isRefreshingSensors}
                onRefresh={refreshSensorArrays}
                status={sensorArrays}
              />
              <OpcChangePanel changes={opcChanges} nodes={nodes} variables={configuredOpcVariableRows} />
            </div>
          )}
          {canvasToast && <div className="canvas-toast workspace-toast" role="status">{canvasToast}</div>}
        </section>

        <aside className="demo-card demo-right-panel">
          <div className="demo-tabbar side" role="tablist" aria-label="右侧信息切换">
            <button className={sideTab === 'control' ? 'active' : ''} onClick={() => setSideTab('control')} type="button">控制</button>
            <button className={sideTab === 'materials' ? 'active' : ''} onClick={() => setSideTab('materials')} type="button">物料</button>
            <button className={sideTab === 'logs' ? 'active' : ''} onClick={() => setSideTab('logs')} type="button">日志</button>
          </div>

          {sideTab === 'control' && (
            <section className="demo-side-tab-panel">
              <div className="demo-panel-title">
                <h2>流程控制</h2>
                <span>Run manager</span>
              </div>
              <div className="demo-run-buttons">
                <button onClick={() => setShowConfigModal(true)}>运行配置</button>
                <button className="primary" onClick={runWorkflow} disabled={isRunning || !workflow || !executionPlan.executableCount}>运行</button>
                <button className="danger" onClick={cancelWorkflow} disabled={!activeRunId}>终止</button>
              </div>
              <div className="demo-execution-summary">
                本次将执行 <strong>{executionPlan.executableCount}</strong> / {executionPlan.totalCount} 个节点
              </div>
              {message && <div className="message">{message}</div>}
              <div className="demo-control-summary">
                <div className="demo-panel-title compact">
                  <h2>站位摘要</h2>
                  <span>Station state</span>
                </div>
                <div className="demo-station-list">
                  {stationSummary.map((station) => (
                    <article className={`demo-station-mini ${station.status}`} key={station.label}>
                      <span>{station.label}</span>
                      <strong>{station.value}</strong>
                    </article>
                  ))}
                  {!stationSummary.length && (
                    <article className="demo-station-mini empty">
                      <span>堆栈状态</span>
                      <strong>{stackError || '等待真实堆栈数据'}</strong>
                    </article>
                  )}
                </div>
              </div>
            </section>
          )}

          {sideTab === 'materials' && (
            <section className="demo-material-section demo-side-tab-panel">
              <div className="demo-panel-title">
                <h2>物料</h2>
              </div>
              <table className="demo-material-table">
                <thead>
                  <tr>
                    <th>物料</th>
                    <th>当前位置</th>
                    <th>下一步</th>
                    <th>状态</th>
                  </tr>
                </thead>
                <tbody>
                  {nodes.map((node, index) => (
                    <tr key={node.id}>
                      <td>
                        <strong>{`node-${index + 1}`}</strong>
                        <span>{node.data.deviceId || '-'}</span>
                      </td>
                      <td>{node.data.method}</td>
                      <td>{node.data.label}</td>
                      <td>{nodeStatusText(node.data.runStatus || 'idle')}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          )}

          {sideTab === 'logs' && (
            <section className="demo-log-section demo-side-tab-panel">
              <div className="demo-panel-title">
                <h2>运行日志</h2>
                <span>Timeline</span>
              </div>
              <LogPanel
                events={logEvents}
                nodes={nodes}
                selectedCategory={selectedLogCategory}
                selectedNodeId={selectedLogNodeId}
                onSelectCategory={setSelectedLogCategory}
                onSelectNode={setSelectedLogNodeId}
              />
            </section>
          )}
        </aside>
      </main>
      )}

      {workspace === 'tasks' && (
        <main className="task-workspace">
          {taskServiceError && (
            <section className="task-service-error" role="alert">
              <strong>{taskServiceError}</strong>
              {taskServiceError === 'Task 编排服务不可用' && (
                <button onClick={() => void loadTaskWorkspace()} type="button">重试</button>
              )}
            </section>
          )}
          <div
            className="task-orchestration"
            ref={taskOrchestrationRef}
            style={{ gridTemplateRows: `minmax(0, 1fr) ${resourceScheduleHeight}px` }}
          >
            <section className="task-column task-recipe-column">
              <div className="task-panel-head">
                <div>
                  <h2>Task Templates</h2>
                  <p>模板仅属于当前 workflow；导入或切换流程后会自动清空。</p>
                </div>
                <span>{taskTemplates.length} 个模板</span>
              </div>
              <div className="task-action-row">
                <button onClick={createRecommendedTaskTemplates} disabled={!nodes.length} type="button">
                  按流程自动切分
                </button>
              </div>
              <div className="task-template-list">
                {taskTemplates.map((template, index) => {
                  const templateNodes = template.nodeIds
                    .map((nodeId) => nodesById.get(nodeId))
                    .filter(Boolean) as Node<ActionNodeData>[];
                  return (
                    <article
                      className="task-template-card"
                      draggable={true}
                      key={template.id}
                      onDragStart={(event) => event.dataTransfer.setData('application/x-unilab-task-template', template.id)}
                    >
                      <button
                        className="task-template-delete"
                        onClick={() => deleteTaskTemplate(template)}
                        aria-label={`删除 Task 模板 ${template.name}`}
                        title="删除 Task 模板"
                        type="button"
                      >
                        <span aria-hidden="true">×</span>
                      </button>
                      <button
                        className={`task-template-select${selectedTaskTemplate?.id === template.id ? ' selected' : ''}`}
                        onClick={() => {
                          setSelectedTaskTemplateId(template.id);
                          setIsTaskDetailModalOpen(true);
                        }}
                        type="button"
                      >
                        <strong>{index + 1}. {template.name}</strong>
                        <span>{templateNodes.map((node) => node.data.label).join(' → ')}</span>
                      </button>
                    </article>
                  );
                })}
                {!taskTemplates.length && <div className="task-empty">暂无模板。可按当前流程自动切分生成一版。</div>}
              </div>
            </section>

            {isTaskDetailModalOpen && selectedTaskTemplate && (
            <section className="task-detail-modal" role="dialog" aria-modal="true" aria-label="Task Template 详情">
              <div className="task-detail-modal-backdrop" onClick={() => setIsTaskDetailModalOpen(false)} />
              <div className="task-column task-detail-column">
                <section className="task-template-detail">
                  <div className="task-detail-head">
                    <div>
                      <span>Template Details</span>
                      <h3>工艺步骤与触发条件</h3>
                    </div>
                    <div>
                      <em>{selectedTaskTemplate.nodeIds.length} steps</em>
                      <button aria-label="关闭模板详情" className="task-detail-close" onClick={() => setIsTaskDetailModalOpen(false)} type="button">×</button>
                    </div>
                  </div>
                  <label className="task-template-name-field">
                    模板名称
                    <input
                      value={selectedTaskTemplate?.name || ''}
                      onBlur={(event) => renameSelectedTaskTemplate(event.target.value)}
                    />
                  </label>
                  <div className="task-detail-section">
                    <strong>子节点</strong>
                    {selectedTaskTemplate.nodeIds.map((nodeId, index) => {
                      const node = nodesById.get(nodeId);
                      return (
                        <div className="task-detail-node" key={nodeId}>
                          <b>{String(index + 1).padStart(2, '0')}</b>
                          <span>
                            <strong>{node?.data.label || `缺失节点 ${nodeId}`}</strong>
                            <small>{node ? `${node.data.deviceId || 'unknown'} · ${node.data.method}` : nodeId}</small>
                          </span>
                        </div>
                      );
                    })}
                  </div>
                  <div className="task-trigger-grid">
                    <div>
                      <strong>输入触发</strong>
                      <div className="task-trigger-editor">
                        {selectedTaskTemplate.inputTriggers.map((trigger, index) => (
                          <div className="task-trigger-condition" key={`input-${index}`}>
                            <input
                              aria-label={`输入条件变量 ${index + 1}`}
                              placeholder="搜索全部 OPC 变量"
                              type="search"
                              value={triggerSearchQueries[`input-${index}`] ?? trigger.variableName}
                              onFocus={() => {
                                setActiveTriggerSearch(`input-${index}`);
                                setTriggerSearchQueries((current) => ({
                                  ...current,
                                  [`input-${index}`]: '',
                                }));
                              }}
                              onChange={(event) => {
                                setTriggerSearchQueries((current) => ({ ...current, [`input-${index}`]: event.target.value }));
                                const variable = csvVariables.find((item) => item.name === event.target.value);
                                if (!variable) return;
                                updateSelectedTaskTriggers('input', selectedTaskTemplate.inputTriggers.map(
                                  (item, itemIndex) => itemIndex === index ? createDefaultTriggerCondition(variable) : item,
                                ));
                              }}
                            />
                            <span aria-hidden="true">==</span>
                            {trigger.dataType === 'BOOL' || trigger.dataType === 'BOOLEAN' ? (
                              <select
                                aria-label={`输入条件值 ${index + 1}`}
                                value={String(trigger.value)}
                                onChange={(event) => updateSelectedTaskTriggers('input', selectedTaskTemplate.inputTriggers.map(
                                  (item, itemIndex) => itemIndex === index ? { ...item, value: event.target.value === 'true' } : item,
                                ))}
                              >
                                <option value="true">true</option>
                                <option value="false">false</option>
                              </select>
                            ) : trigger.dataType === 'INTEGER' || trigger.dataType === 'INT' || trigger.dataType === 'FLOAT' || trigger.dataType === 'DOUBLE' || trigger.dataType === 'NUMBER' ? (
                              <input
                                aria-label={`输入条件值 ${index + 1}`}
                                type="number"
                                step={trigger.dataType === 'INTEGER' || trigger.dataType === 'INT' ? 1 : 'any'}
                                value={String(trigger.value)}
                                onChange={(event) => updateSelectedTaskTriggers('input', selectedTaskTemplate.inputTriggers.map(
                                  (item, itemIndex) => itemIndex === index ? { ...item, value: Number(event.target.value) } : item,
                                ))}
                              />
                            ) : (
                              <input
                                aria-label={`输入条件值 ${index + 1}`}
                                value={String(trigger.value)}
                                onChange={(event) => updateSelectedTaskTriggers('input', selectedTaskTemplate.inputTriggers.map(
                                  (item, itemIndex) => itemIndex === index ? { ...item, value: event.target.value } : item,
                                ))}
                              />
                            )}
                            <button
                              aria-label={`删除输入条件 ${trigger.variableName || index + 1}`}
                              onClick={() => updateSelectedTaskTriggers(
                                'input',
                                selectedTaskTemplate.inputTriggers.filter((_, itemIndex) => itemIndex !== index),
                              )}
                              type="button"
                            >×</button>
                            {activeTriggerSearch === `input-${index}` && (
                              <div className="task-trigger-options">
                                {csvVariables
                                  .filter((variable) => `${variable.name} ${variable.comment}`.toLowerCase().includes((triggerSearchQueries[`input-${index}`] ?? trigger.variableName).toLowerCase()))
                                  .map((variable) => (
                                    <button
                                      className={variable.name === trigger.variableName ? 'selected' : ''}
                                      key={variable.name}
                                      onMouseDown={(event) => event.preventDefault()}
                                      onClick={() => {
                                        updateSelectedTaskTriggers('input', selectedTaskTemplate.inputTriggers.map(
                                          (item, itemIndex) => itemIndex === index ? createDefaultTriggerCondition(variable) : item,
                                        ));
                                        setActiveTriggerSearch(null);
                                        setTriggerSearchQueries((current) => {
                                          const { [`input-${index}`]: _, ...rest } = current;
                                          return rest;
                                        });
                                      }}
                                      type="button"
                                    >
                                      <b>{variable.name}</b>
                                      <small>{variable.data_type}{variable.comment ? ` · ${variable.comment}` : ''}</small>
                                      {variable.name === trigger.variableName && <em aria-label="已选中">✓</em>}
                                    </button>
                                  ))}
                              </div>
                            )}
                          </div>
                        ))}
                        <div className="task-trigger-add">
                          <button
                            disabled={!csvVariables.length}
                            onClick={() => updateSelectedTaskTriggers(
                              'input',
                              [...selectedTaskTemplate.inputTriggers, createDefaultTriggerCondition(csvVariables[0])],
                            )}
                            type="button"
                          >添加输入条件</button>
                        </div>
                      </div>
                    </div>
                    <div>
                      <strong>输出触发</strong>
                      <div className="task-trigger-editor">
                        {selectedTaskTemplate.outputTriggers.map((trigger, index) => (
                          <div className="task-trigger-condition" key={`output-${index}`}>
                            <input
                              aria-label={`输出条件变量 ${index + 1}`}
                              placeholder="搜索全部 OPC 变量"
                              type="search"
                              value={triggerSearchQueries[`output-${index}`] ?? trigger.variableName}
                              onFocus={() => {
                                setActiveTriggerSearch(`output-${index}`);
                                setTriggerSearchQueries((current) => ({
                                  ...current,
                                  [`output-${index}`]: '',
                                }));
                              }}
                              onChange={(event) => {
                                setTriggerSearchQueries((current) => ({ ...current, [`output-${index}`]: event.target.value }));
                                const variable = csvVariables.find((item) => item.name === event.target.value);
                                if (!variable) return;
                                updateSelectedTaskTriggers('output', selectedTaskTemplate.outputTriggers.map(
                                  (item, itemIndex) => itemIndex === index ? createDefaultTriggerCondition(variable) : item,
                                ));
                              }}
                            />
                            <span aria-hidden="true">==</span>
                            {trigger.dataType === 'BOOL' || trigger.dataType === 'BOOLEAN' ? (
                              <select
                                aria-label={`输出条件值 ${index + 1}`}
                                value={String(trigger.value)}
                                onChange={(event) => updateSelectedTaskTriggers('output', selectedTaskTemplate.outputTriggers.map(
                                  (item, itemIndex) => itemIndex === index ? { ...item, value: event.target.value === 'true' } : item,
                                ))}
                              >
                                <option value="true">true</option>
                                <option value="false">false</option>
                              </select>
                            ) : trigger.dataType === 'INTEGER' || trigger.dataType === 'INT' || trigger.dataType === 'FLOAT' || trigger.dataType === 'DOUBLE' || trigger.dataType === 'NUMBER' ? (
                              <input
                                aria-label={`输出条件值 ${index + 1}`}
                                type="number"
                                step={trigger.dataType === 'INTEGER' || trigger.dataType === 'INT' ? 1 : 'any'}
                                value={String(trigger.value)}
                                onChange={(event) => updateSelectedTaskTriggers('output', selectedTaskTemplate.outputTriggers.map(
                                  (item, itemIndex) => itemIndex === index ? { ...item, value: Number(event.target.value) } : item,
                                ))}
                              />
                            ) : (
                              <input
                                aria-label={`输出条件值 ${index + 1}`}
                                value={String(trigger.value)}
                                onChange={(event) => updateSelectedTaskTriggers('output', selectedTaskTemplate.outputTriggers.map(
                                  (item, itemIndex) => itemIndex === index ? { ...item, value: event.target.value } : item,
                                ))}
                              />
                            )}
                            <button
                              aria-label={`删除输出条件 ${trigger.variableName || index + 1}`}
                              onClick={() => updateSelectedTaskTriggers(
                                'output',
                                selectedTaskTemplate.outputTriggers.filter((_, itemIndex) => itemIndex !== index),
                              )}
                              type="button"
                            >×</button>
                            {activeTriggerSearch === `output-${index}` && (
                              <div className="task-trigger-options">
                                {csvVariables
                                  .filter((variable) => `${variable.name} ${variable.comment}`.toLowerCase().includes((triggerSearchQueries[`output-${index}`] ?? trigger.variableName).toLowerCase()))
                                  .map((variable) => (
                                    <button
                                      className={variable.name === trigger.variableName ? 'selected' : ''}
                                      key={variable.name}
                                      onMouseDown={(event) => event.preventDefault()}
                                      onClick={() => {
                                        updateSelectedTaskTriggers('output', selectedTaskTemplate.outputTriggers.map(
                                          (item, itemIndex) => itemIndex === index ? createDefaultTriggerCondition(variable) : item,
                                        ));
                                        setActiveTriggerSearch(null);
                                        setTriggerSearchQueries((current) => {
                                          const { [`output-${index}`]: _, ...rest } = current;
                                          return rest;
                                        });
                                      }}
                                      type="button"
                                    >
                                      <b>{variable.name}</b>
                                      <small>{variable.data_type}{variable.comment ? ` · ${variable.comment}` : ''}</small>
                                      {variable.name === trigger.variableName && <em aria-label="已选中">✓</em>}
                                    </button>
                                  ))}
                              </div>
                            )}
                          </div>
                        ))}
                        <div className="task-trigger-add">
                          <button
                            disabled={!csvVariables.length}
                            onClick={() => updateSelectedTaskTriggers(
                              'output',
                              [...selectedTaskTemplate.outputTriggers, createDefaultTriggerCondition(csvVariables[0])],
                            )}
                            type="button"
                          >添加输出条件</button>
                        </div>
                      </div>
                    </div>
                  </div>
                  <datalist id="csv-variable-options">
                    {csvVariables.map((variable) => (
                      <option key={variable.name} value={variable.name}>
                        {variable.comment ? `${variable.data_type} · ${variable.comment}` : variable.data_type}
                      </option>
                    ))}
                  </datalist>
                </section>
              </div>
            </section>
            )}

            <section className="task-column task-scheduler-column">
              <div className="task-panel-head">
                <div>
                  <h2>Task Queue</h2>
                  <p>按样品顺序与资源门控模拟调度。</p>
                </div>
                <span>{taskInstances.length} 个实例</span>
              </div>
              <div className="task-action-row">
                <label className="task-sample-count">
                  样品数
                  <input
                    type="number"
                    min={1}
                    max={5}
                    step={1}
                    value={taskSampleCount}
                    onChange={(event) => setTaskSampleCount(Math.min(5, Math.max(1, Math.round(Number(event.target.value)) || 1)))}
                  />
                </label>
                <button onClick={createTaskInstances} disabled={!scheduledTemplateIds.length || isTaskWorkspaceLoading} type="button">生成样品任务</button>
                <button
                  className="primary"
                  onClick={() => setTaskSchedulerPaused(isSchedulerRunning)}
                  disabled={!taskInstances.length || isTaskWorkspaceLoading}
                  type="button"
                >
                  {isSchedulerRunning ? '暂停派发' : '运行调度'}
                </button>
                <button onClick={advanceTaskSchedule} disabled={!taskInstances.length || isTaskWorkspaceLoading} type="button">调度一步</button>
              </div>
              <div className="task-queue-list">
                {taskInstances.map((task) => {
                  const template = taskTemplates.find((item) => item.id === task.templateId);
                  const waitingReason = taskWaitingReasons[task.id];
                  const state = task.status === 'completed' ? 'done' : task.status === 'running' ? 'running' : task.status === 'pending' ? 'ready' : 'blocked';
                  const sampleQueue = taskInstances
                    .filter((item) => item.sample === task.sample)
                    .sort((left, right) => left.order - right.order);
                  const queueIndex = sampleQueue.findIndex((item) => item.id === task.id);
                  const canReorder = !isSchedulerRunning && (task.status === 'waiting' || task.status === 'pending');
                  return (
                    <article className={`task-queue-card ${state}`} key={task.id}>
                      <div>
                        <strong>{task.sample} / {template?.name || task.templateId}</strong>
                        <span>{task.startedAt
                          ? `运行记录：${new Date(task.startedAt).toLocaleTimeString('zh-CN', { hour12: false })}`
                          : waitingReason?.message || (task.status === 'waiting' ? '正在检查前置条件' : '等待调度器派发')}</span>
                      </div>
                      <div className="task-queue-actions">
                        <button
                          aria-label={`上移 ${task.sample}/${template?.name || task.templateId}`}
                          disabled={!canReorder || queueIndex === 0}
                          onClick={() => moveTaskInstance(task.id, -1)}
                          title="提前执行"
                          type="button"
                        >↑</button>
                        <button
                          aria-label={`下移 ${task.sample}/${template?.name || task.templateId}`}
                          disabled={!canReorder || queueIndex === sampleQueue.length - 1}
                          onClick={() => moveTaskInstance(task.id, 1)}
                          title="延后执行"
                          type="button"
                        >↓</button>
                        <em>{taskStatusText(state)}</em>
                      </div>
                    </article>
                  );
                })}
                {!taskInstances.length && <div className="task-empty">生成样品任务后，这里显示 pending / running / done 队列。</div>}
              </div>
            </section>

            <section className="task-column task-log-column">
              <div className="task-panel-head compact">
                <div>
                  <h2>等待条件与调度事件</h2>
                  <p>仅展示当前未满足的前置、信号或资源条件。</p>
                </div>
              </div>
              <div className="task-waiting-list">
                {taskInstances
                  .filter((task) => task.status !== 'completed' && task.status !== 'running')
                  .map((task) => {
                    const template = taskTemplates.find((item) => item.id === task.templateId);
                    const waitingReason = taskWaitingReasons[task.id];
                    return (
                      <div key={task.id}>
                        <strong>{task.sample} / {template?.name || task.templateId}</strong>
                        <span>{waitingReason
                          ? `${waitingReason.message}（${waitingReason.code}）`
                          : task.status === 'waiting' ? '正在检查前置条件' : '等待调度器派发'}</span>
                      </div>
                    );
                  })}
                {!taskInstances.some((task) => task.status !== 'completed' && task.status !== 'running') && <div className="task-empty">当前没有等待中的 Task。</div>}
              </div>
              <div className="task-event-list">
                {taskEvents.map((event, index) => <div key={`${event}-${index}`}>{event}</div>)}
                {!taskEvents.length && <div className="task-empty">暂无 Task 事件。</div>}
              </div>
            </section>

            <section className="task-column task-gantt-column">
              <div
                aria-label="调整 Resource Schedule 高度"
                className="task-schedule-resize-handle"
                onPointerDown={startResourceScheduleResize}
                role="separator"
              />
              <div className="task-panel-head">
                <div>
                  <h2>Resource Schedule</h2>
                  <p>由 Task 编排服务返回的排程，按资源泳道展示计划、运行与完成的 Task。</p>
                </div>
                <span>{isSchedulerRunning ? '派发中' : '已暂停'}</span>
              </div>
              <div
                className="task-schedule-dropzone"
                onDragOver={(event) => event.preventDefault()}
                onDrop={(event) => {
                  event.preventDefault();
                  const templateId = event.dataTransfer.getData('application/x-unilab-task-template');
                  addTemplateToSchedule(templateId);
                }}
              >
                <strong>待排模板</strong>
                <span>从左侧 Template 拖入此处，确定本次需要运行的 Task。本次待排 {scheduledTemplateIds.length} / {taskTemplates.length}</span>
                <div className="task-scheduled-template-list">
                  {scheduledTemplateIds.map((templateId, index) => {
                    const template = taskTemplates.find((item) => item.id === templateId);
                    if (!template) return null;
                    return (
                      <div key={templateId}>
                        <b>{index + 1}</b>
                        <span>{template.name}</span>
                        <button
                          aria-label={`移除待排模板 ${template.name}`}
                          onClick={() => removeTemplateFromSchedule(templateId)}
                          type="button"
                        >×</button>
                      </div>
                    );
                  })}
                  {!scheduledTemplateIds.length && <em>拖入 Template 以建立本次运行队列</em>}
                </div>
              </div>
              <div className="task-gantt">
                {scheduledResources.map((resource) => {
                  const entries = taskGanttEntries.filter((entry) => entry.resource === resource);
                  const earliest = Math.min(...taskGanttEntries.map((entry) => entry.startAt), Date.now());
                  const latest = Math.max(...taskGanttEntries.map((entry) => entry.endAt), Date.now() + 60_000);
                  const span = Math.max(1, latest - earliest);
                  return (
                    <div className="task-gantt-row" key={resource}>
                      <strong>{DEFAULT_SENSOR_GATES[resource]?.label || resource}</strong>
                      <div className="task-gantt-track">
                        {entries.map((entry) => (
                          <button
                            className={`task-gantt-bar ${entry.state}`}
                            key={entry.id}
                            onClick={() => setSelectedTaskTemplateId(entry.templateId)}
                            style={{
                              left: `${((entry.startAt - earliest) / span) * 100}%`,
                              width: `${Math.max(5, ((entry.endAt - entry.startAt) / span) * 100)}%`,
                            }}
                            title={`${entry.sample} / ${taskTemplates.find((template) => template.id === entry.templateId)?.name || entry.templateId}`}
                            type="button"
                          >
                            {entry.sample}
                          </button>
                        ))}
                      </div>
                    </div>
                  );
                })}
                {!scheduledResources.length && <div className="task-empty">将 Template 拖入上方待排区后显示其需要的资源泳道。</div>}
              </div>
            </section>
          </div>
        </main>
      )}

      {showStackModal && (
        <div className="demo-modal-backdrop" onMouseDown={() => setShowStackModal(false)}>
          <section className="demo-stack-modal" onMouseDown={(event) => event.stopPropagation()}>
            <div className="demo-modal-head">
              <div>
                <p>Stack detail</p>
                <h2>{selectedStack?.title || '堆栈详情'}</h2>
                <span>{selectedStack?.role || '等待真实数据'}</span>
              </div>
              <button onClick={() => setShowStackModal(false)} type="button">关闭</button>
            </div>
            <div className="demo-stack-modal-grid">
              {selectedStackSlots.map((slot) => (
                <article className={`demo-stack-modal-slot ${slot.status}`} key={slot.id}>
                  <strong>{slot.id}</strong>
                  <small>{slot.status === 'empty' ? '空闲' : '占用'}</small>
                </article>
              ))}
              {!selectedStackSlots.length && (
                <article className="demo-stack-modal-slot empty">
                  <strong>无槽位</strong>
                  <small>等待</small>
                  <p>{stackError || '等待真实堆栈数据'}</p>
                </article>
              )}
            </div>
          </section>
        </div>
      )}

      {editingNode && (
        <div className="modal-backdrop" onMouseDown={() => setEditingNodeId(null)}>
          <div className="config-modal node-modal" onMouseDown={(event) => event.stopPropagation()}>
            <div className="modal-head">
              <div>
                <h2>{editingNode.data.label}</h2>
                <p>{editingNode.data.description}</p>
              </div>
              <button className="icon-button" onClick={() => setEditingNodeId(null)}>关闭</button>
            </div>
            <div className="node-modal-meta">
              <span>节点 ID</span>
              <code>{editingNode.id}</code>
              <span>动作方法</span>
              <code>{editingNode.data.method}</code>
              {editingNode.data.opcVariables?.length ? (
                <>
                  <span>变量名</span>
                  <code>{editingNode.data.opcVariables.join('、')}</code>
                </>
              ) : null}
            </div>
            {editingNode.data.paramSpecs?.length ? (
              <div className="param-grid">
                {editingNode.data.paramSpecs.map((param) => {
                  const name = param.name || '';
                  if (!name) return null;
                  return (
                    <label key={name}>
                      {param.label || name}
                      <input
                        type={param.type === 'boolean' ? 'checkbox' : param.type === 'string' ? 'text' : 'number'}
                        min={param.min}
                        max={param.max}
                        checked={param.type === 'boolean' ? Boolean(editingNode.data.params[name]) : undefined}
                        value={param.type === 'boolean' ? undefined : String(editingNode.data.params[name] ?? '')}
                        onChange={(event) => {
                          const value = param.type === 'boolean'
                            ? event.currentTarget.checked
                            : param.type === 'string'
                              ? event.currentTarget.value
                              : Number(event.currentTarget.value);
                          updateNodeParam(editingNode.id, name, value);
                        }}
                      />
                      {param.description ? <small>{param.description}</small> : null}
                    </label>
                  );
                })}
              </div>
            ) : (
              <div className="empty-state">该动作没有可编辑参数。</div>
            )}
            <div className="modal-actions">
              <button onClick={() => setEditingNodeId(null)}>完成</button>
            </div>
          </div>
        </div>
      )}

      {showConfigModal && (
        <div className="modal-backdrop" onMouseDown={() => setShowConfigModal(false)}>
          <div className="config-modal" onMouseDown={(event) => event.stopPropagation()}>
            <div className="modal-head">
              <div>
                <h2>运行配置</h2>
                <p>配置只影响本地运行，不影响画布中的流程节点。</p>
              </div>
              <button className="icon-button" onClick={() => setShowConfigModal(false)}>关闭</button>
            </div>
            <label>
              Workflow 名称
              <input value={workflowName} onChange={(event) => setWorkflowName(event.target.value)} />
            </label>
            <label>
              OPC UA URL
              <input
                value={config.url}
                onChange={(event) => setConfig({ ...config, url: event.target.value })}
                placeholder="opc.tcp://jdht1471820.bohrium.tech:50001"
              />
            </label>
            {config.show_csv && (
              <label>
                节点 CSV
                <input value={config.csv} onChange={(event) => setConfig({ ...config, csv: event.target.value })} />
              </label>
            )}
            <label>
              超时秒数
              <input type="number" min={1} value={config.timeout} onChange={(event) => setConfig({ ...config, timeout: Number(event.target.value) })} />
            </label>
            <label>
              Robot允许写入等待秒数
              <input type="number" min={1} value={config.write_allowed_timeout} onChange={(event) => setConfig({ ...config, write_allowed_timeout: Number(event.target.value) })} />
            </label>
            <label className="check">
              <input
                type="checkbox"
                checked={config.no_subscription}
                onChange={(event) => setConfig({ ...config, no_subscription: event.target.checked })}
              />
              禁用 OPC UA 订阅
            </label>
            <div className="modal-actions">
              <button onClick={() => setShowConfigModal(false)}>完成</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function downloadJson(filename: string, data: unknown) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.click();
  URL.revokeObjectURL(url);
}

function loadSavedDraft(storageKey: string, actions: ActionSpec[]) {
  try {
    const rawDraft = window.localStorage.getItem(storageKey);
    if (!rawDraft) return null;
    return createImportedDraft(JSON.parse(rawDraft), actions, { autoLayout: false }) as {
      name: string;
      nodes: Node<ActionNodeData>[];
      edges: Edge[];
    };
  } catch {
    window.localStorage.removeItem(storageKey);
    return null;
  }
}

function loadSavedStartNodeId(storageKey: string, nodes: Node<ActionNodeData>[]) {
  const nodeId = window.localStorage.getItem(`${storageKey}.startNodeId`);
  return nodeId && nodes.some((node) => node.id === nodeId) ? nodeId : null;
}

function statusText(status?: string) {
  if (status === 'pending') return '等待中';
  if (status === 'preparing') return '准备中';
  if (status === 'running') return '运行中';
  if (status === 'completed') return '已完成';
  if (status === 'failed') return '失败';
  if (status === 'cancelling') return '终止中';
  if (status === 'cancelled') return '已终止';
  return '未运行';
}

function ActionNode({ id, data, selected }: NodeProps<ActionNodeData>) {
  const runStatus = data.runStatus || 'idle';
  const executionState = data.executionState || 'willRun';
  const executionBadge = executionState === 'bypassed'
    ? executionStateText(executionState)
    : data.isExecutionStart
      ? '起点'
      : executionStateText(executionState);

  return (
    <div className={`flow-node ${selected ? 'selected' : ''} ${runStatus} execution-${executionState} ${data.isExecutionStart ? 'execution-start' : ''}`}>
      <Handle type="target" position={Position.Left} />
      <div className="node-hover-actions">
        <button
          aria-label="从这里开始运行"
          title="从这里开始运行"
          type="button"
          onClick={(event) => {
            event.stopPropagation();
            data.onSetStart?.(id);
          }}
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M5 4v16M7 5h10l-2 4 2 4H7" />
          </svg>
        </button>
        <button
          aria-label={data.executionBypassed ? '取消直通' : '直通此节点'}
          aria-pressed={Boolean(data.executionBypassed)}
          className={data.executionBypassed ? 'active' : ''}
          title={data.executionBypassed ? '取消直通' : '直通此节点'}
          type="button"
          onClick={(event) => {
            event.stopPropagation();
            data.onToggleBypassed?.(id);
          }}
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M4 12h15M14 7l5 5-5 5" />
          </svg>
        </button>
        <button
          aria-label="禁用此节点及后续"
          className="danger"
          title="禁用此节点及后续"
          type="button"
          onClick={(event) => {
            event.stopPropagation();
            data.onToggleDisabled?.(id);
          }}
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M6 6l12 12M18 6 6 18" />
          </svg>
        </button>
        <button
          aria-label="编辑参数"
          title="编辑参数"
          type="button"
          onClick={(event) => {
            event.stopPropagation();
            data.onEditParams?.(id);
          }}
        >
          <svg viewBox="0 0 24 24" aria-hidden="true">
            <path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4 12.5-12.5Z" />
          </svg>
        </button>
      </div>
      <div className="flow-node-topline">
        <span className="flow-node-kicker">AI4C Action</span>
        <span className={`node-status ${runStatus}`}>{nodeStatusText(runStatus)}</span>
      </div>
      <div className="flow-node-title">{data.label}</div>
      {executionBadge && <span className={`execution-badge ${executionState}`}>{executionBadge}</span>}
      <code>{id}</code>
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

function executionStateText(state: ActionNodeData['executionState']) {
  if (state === 'beforeStart') return '起点之前';
  if (state === 'disabled') return '已禁用';
  if (state === 'blockedByDisabled') return '被上游禁用';
  if (state === 'bypassed') return '已直通';
  return '';
}

function nodeStatusText(status: NodeRunStatus) {
  if (status === 'preparing') return '准备中';
  if (status === 'running') return '运行中';
  if (status === 'success') return '成功';
  if (status === 'failed') return '失败';
  if (status === 'cancelled') return '已终止';
  return '待运行';
}

function LogPanel({
  events,
  nodes,
  selectedCategory,
  selectedNodeId,
  onSelectCategory,
  onSelectNode,
}: {
  events: LogEvent[];
  nodes: Node<ActionNodeData>[];
  selectedCategory: string | null;
  selectedNodeId: string | null;
  onSelectCategory: (category: string | null) => void;
  onSelectNode: (nodeId: string | null) => void;
}) {
  const nodeLabels = new Map(nodes.map((node) => [node.id, node.data.label]));
  const categoryTabs = buildLogCategoryTabs(events);
  const eventNodeIds = new Set(events.flatMap((event) => (event.node_id ? [event.node_id] : [])));
  const nodeTabs = [
    ...nodes
      .filter((node) => eventNodeIds.has(node.id))
      .map((node) => ({ id: node.id, label: node.data.label })),
    ...Array.from(eventNodeIds)
      .filter((nodeId) => !nodeLabels.has(nodeId))
      .map((nodeId) => ({ id: nodeId, label: nodeId })),
  ];
  const categoryFilteredEvents = selectedCategory
    ? events.filter((event) => normalizeLogCategory(event) === selectedCategory)
    : events;
  const visibleEvents = selectedNodeId
    ? categoryFilteredEvents.filter((event) => event.node_id === selectedNodeId)
    : categoryFilteredEvents;
  const groupedEvents = groupLogEvents(visibleEvents);

  if (!events.length) {
    return <div className="log-empty">等待运行...</div>;
  }

  return (
    <div className="log-panel">
      <div className="log-category-tabs" role="tablist" aria-label="日志分类">
        <button
          className={!selectedCategory ? 'active' : ''}
          role="tab"
          aria-selected={!selectedCategory}
          onClick={() => onSelectCategory(null)}
        >
          全部
          <span>{events.length}</span>
        </button>
        {categoryTabs.map((tab) => (
          <button
            className={selectedCategory === tab.category ? 'active' : ''}
            key={tab.category}
            role="tab"
            aria-selected={selectedCategory === tab.category}
            onClick={() => onSelectCategory(tab.category)}
          >
            {tab.label}
            <span>{tab.count}</span>
          </button>
        ))}
      </div>
      <div className="log-tabs" role="tablist" aria-label="节点日志">
        <button
          className={!selectedNodeId ? 'active' : ''}
          role="tab"
          aria-selected={!selectedNodeId}
          onClick={() => onSelectNode(null)}
        >
          全部
        </button>
        {nodeTabs.map((tab) => (
          <button
            className={selectedNodeId === tab.id ? 'active' : ''}
            key={tab.id}
            role="tab"
            aria-selected={selectedNodeId === tab.id}
            onClick={() => onSelectNode(tab.id)}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <div className="log-groups">
        {groupedEvents.map((group) => (
          <details className="log-group" key={group.key} open>
            <summary>
              <span>{group.nodeId ? (nodeLabels.get(group.nodeId) || group.nodeId) : 'Workflow'}</span>
              <small>{group.events.length} 条</small>
            </summary>
            <div className="log-lines">
              {group.events.map((event) => (
                <div className={`log-line ${event.level}`} key={event.sequence}>
                  <div className="log-message">
                    <span className="log-sequence">#{event.sequence}</span>
                    <span>{event.message}</span>
                  </div>
                  {event.detail && (
                    <details className="log-detail">
                      <summary>详情</summary>
                      <pre>{JSON.stringify(event.detail, null, 2)}</pre>
                    </details>
                  )}
                </div>
              ))}
            </div>
          </details>
        ))}
        {!groupedEvents.length && (
          <div className="log-empty">当前分类暂无日志</div>
        )}
      </div>
    </div>
  );
}

type CategorizedSensorBit = SensorBitPayload & {
  arrayIndex: number;
  category: string;
  position: string;
};

type CategorizedSensorGroup = {
  name: string;
  bits: CategorizedSensorBit[];
  unmarked: boolean;
};

const HIDDEN_SENSOR_NAMES = new Set([
  '传感器状态_上位机[3].NO[6]',
]);

function sensorLabelParts(label?: string): { category: string; position: string } {
  const normalized = (label || '').trim();
  if (!normalized) return { category: '未备注信号', position: '' };
  const match = normalized.match(/^(.*?)(\d+(?:-\d+)?)$/);
  if (!match) return { category: normalized, position: '' };
  return {
    category: match[1].trim() || normalized,
    position: match[2],
  };
}

function categorizeSensorGroups(groups: SensorArrayGroupPayload[]): CategorizedSensorGroup[] {
  const categories = new Map<string, CategorizedSensorBit[]>();
  groups.forEach((group) => {
    (group.bits || []).forEach((bit) => {
      if (HIDDEN_SENSOR_NAMES.has(bit.name)) return;
      const { category, position } = sensorLabelParts(bit.label);
      const categorizedBit = {
        ...bit,
        arrayIndex: group.index,
        category,
        position,
      };
      categories.set(category, [...(categories.get(category) || []), categorizedBit]);
    });
  });

  return Array.from(categories, ([name, bits]) => ({
    name,
    bits,
    unmarked: name === '未备注信号',
  })).sort((left, right) => Number(left.unmarked) - Number(right.unmarked));
}

function SensorBitRow({ bits }: { bits: CategorizedSensorBit[] }) {
  const positions = bits.map((bit) => {
    const match = bit.position.match(/^(\d+)-(\d+)$/);
    return match ? { row: Number(match[1]), column: Number(match[2]) } : null;
  });
  const isMatrix = positions.length > 0 && positions.every((position) => position !== null);
  const maxRow = isMatrix ? Math.max(...positions.map((position) => position?.row || 1)) : 0;
  const maxColumn = isMatrix ? Math.max(...positions.map((position) => position?.column || 1)) : 0;

  return (
    <div
      className={`sensor-array-bits${isMatrix ? ' matrix' : ''}`}
      style={isMatrix ? {
        gridTemplateColumns: `repeat(${maxColumn}, minmax(78px, 110px))`,
        gridTemplateRows: `repeat(${maxRow}, minmax(66px, auto))`,
      } : undefined}
    >
      {bits.map((bit, bitIndex) => (
        <div
          className={`sensor-array-bit ${bit.value === true ? 'on' : bit.value === false ? 'off' : 'unknown'}`}
          key={`${bit.arrayIndex}-${bit.index}`}
          style={isMatrix ? {
            gridColumn: positions[bitIndex]?.column,
            gridRow: positions[bitIndex]?.row,
          } : undefined}
          title={`${bit.name}\n${bit.label || '未标注'}\n${bit.address || ''}\n${bit.node_id || ''}`}
        >
          <span>{bit.position || '单点'}</span>
          <strong>{bit.value === true ? '1' : bit.value === false ? '0' : '-'}</strong>
          <small>[{bit.arrayIndex}].{bit.index}{bit.address ? ` · ${bit.address}` : ''}</small>
        </div>
      ))}
    </div>
  );
}

function SensorArrayPanel({
  error,
  isRefreshing,
  onRefresh,
  status,
}: {
  error: string;
  isRefreshing: boolean;
  onRefresh: () => Promise<void>;
  status: SensorArraysPayload | null;
}) {
  const groups = status?.groups || [];
  const categorizedGroups = categorizeSensorGroups(groups);
  const groupErrors = groups
    .filter((group) => group.error)
    .map((group) => `[${group.index}] ${group.error}`)
    .join('；');
  const online = status?.success || status?.partial;
  return (
    <section className="sensor-array-panel">
      <div className="sensor-array-head">
        <div>
          <h3>实机传感器阵列</h3>
          <p>按 CSV 备注归类展示；检测到 PLC 信号变化时自动刷新。</p>
        </div>
        <div className="sensor-array-actions">
          <span className={online ? 'online' : 'offline'}>
            {online ? (status?.partial ? 'PARTIAL' : 'ONLINE') : 'OFFLINE'}
          </span>
          <button disabled={isRefreshing} onClick={() => void onRefresh()} type="button">
            {isRefreshing ? '读取中…' : '立即刷新'}
          </button>
        </div>
      </div>
      {(error || status?.message || groupErrors) && (
        <div className="sensor-array-error">{error || status?.message || groupErrors}</div>
      )}
      <div className="sensor-array-groups">
        {categorizedGroups.map((group) => (
          group.unmarked ? (
            <details className="sensor-array-group sensor-array-unmarked" key={group.name}>
              <summary>
                <strong>{group.name}</strong>
                <span>{group.bits.length} 个</span>
              </summary>
              <SensorBitRow bits={group.bits} />
            </details>
          ) : (
            <article className="sensor-array-group" key={group.name}>
              <header>
                <strong>{group.name}</strong>
                <span>{group.bits.length} 个</span>
              </header>
              <SensorBitRow bits={group.bits} />
            </article>
          )
        ))}
        {!categorizedGroups.length && (
          <div className="opc-change-empty">{error || '正在连接实机 OPC UA…'}</div>
        )}
      </div>
    </section>
  );
}

function OpcChangePanel({
  changes,
  nodes,
  variables,
}: {
  changes: OpcChange[];
  nodes: Node<ActionNodeData>[];
  variables: OpcVariableView[];
}) {
  const nodeLabels = new Map(nodes.map((node) => [node.id, node.data.label]));

  return (
    <section className="opc-changes">
      <details className="opc-collapsible" open>
        <summary className="opc-changes-head">
          <h3>OPC 采样变量</h3>
          <span>{variables.length} 个</span>
        </summary>
        {variables.length ? (
          <div className="opc-change-table-wrap">
            <table className="opc-change-table">
              <thead>
                <tr>
                  <th>#</th>
                  <th>Name</th>
                  <th>当前值</th>
                </tr>
              </thead>
              <tbody>
                {variables.map((variable, index) => (
                  <tr key={variable.name}>
                    <td>{index + 1}</td>
                    <td><code>{variable.name}</code></td>
                    <td>{variable.currentValue === undefined ? '-' : formatOpcValue(variable.currentValue)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="opc-change-empty">暂无 OPC 采样变量，请先添加动作节点</div>
        )}
      </details>
      <details className="opc-collapsible" open>
        <summary className="opc-changes-head">
          <h3>OPC 变量变化</h3>
          <span>{changes.length} 条</span>
        </summary>
        {changes.length ? (
          <div className="opc-change-table-wrap">
            <table className="opc-change-table">
              <thead>
                <tr>
                  <th>#</th>
                  <th>Workflow Node</th>
                  <th>NodeID</th>
                  <th>Name</th>
                  <th>Value Begin</th>
                  <th>Value Goal</th>
                  <th>Value End</th>
                </tr>
              </thead>
              <tbody>
                {changes.map((change, index) => (
                  <tr key={`${change.eventSequence}-${change.name}-${index}`}>
                    <td>{index + 1}</td>
                    <td>{change.workflowNodeId ? (nodeLabels.get(change.workflowNodeId) || change.workflowNodeId) : 'Workflow'}</td>
                    <td><code>{change.opcNodeId || '-'}</code></td>
                    <td>
                      <strong>{change.displayName}</strong>
                      <code>{change.name}</code>
                    </td>
                    <td>{formatOpcValue(change.valueBegin)}</td>
                    <td>{formatOpcValue(change.valueGoal)}</td>
                    <td>{formatOpcValue(change.valueEnd)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <div className="opc-change-empty">暂无 OPC 变量变化</div>
        )}
      </details>
    </section>
  );
}

function normalizeLogEvents(runStatus: RunStatus | null): LogEvent[] {
  if (!runStatus) return [];
  if (runStatus.log_events?.length) return runStatus.log_events;
  return (runStatus.logs || []).map((message, index) => ({
    sequence: index + 1,
    message,
    level: 'info',
    category: 'workflow',
    scope: 'workflow',
    node_id: null,
    detail: null,
  }));
}

function buildLogCategoryTabs(events: LogEvent[]) {
  const counts = new Map<string, number>();
  events.forEach((event) => {
    const category = normalizeLogCategory(event);
    counts.set(category, (counts.get(category) || 0) + 1);
  });
  return Array.from(counts.entries())
    .map(([category, count]) => ({ category, count, label: logCategoryLabel(category) }))
    .sort((left, right) => logCategoryOrder(left.category) - logCategoryOrder(right.category));
}

function normalizeLogCategory(event: LogEvent) {
  if (event.category) return event.category;
  if (event.detail?.type === 'opc_wait') return 'opc_wait';
  if (event.message.includes('OPC')) return 'opc';
  if (event.node_id) return 'node';
  return 'workflow';
}

function logCategoryLabel(category: string) {
  const labels: Record<string, string> = {
    workflow: '流程',
    setup: '准备',
    node: '节点',
    opc_sample: 'OPC采样',
    opc_change: 'OPC变化',
    opc_wait: 'OPC等待',
    opc: 'OPC',
    action_result: '结果',
    error: '错误',
  };
  return labels[category] || category;
}

function logCategoryOrder(category: string) {
  const order = ['workflow', 'setup', 'node', 'opc_sample', 'opc_change', 'opc_wait', 'opc', 'action_result', 'error'];
  const index = order.indexOf(category);
  return index === -1 ? order.length : index;
}

function groupLogEvents(events: LogEvent[]) {
  const groups: Array<{ key: string; nodeId: string | null; events: LogEvent[] }> = [];
  const groupByKey = new Map<string, { key: string; nodeId: string | null; events: LogEvent[] }>();

  events.forEach((event) => {
    const nodeId = event.node_id || null;
    const key = nodeId || 'workflow';
    let group = groupByKey.get(key);
    if (!group) {
      group = { key, nodeId, events: [] };
      groupByKey.set(key, group);
      groups.push(group);
    }
    group.events.push(event);
  });

  return groups;
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    {window.location.pathname === '/demo' ? (
      <WorkstationDemo />
    ) : (
      <ReactFlowProvider>
        <App />
      </ReactFlowProvider>
    )}
  </React.StrictMode>,
);
