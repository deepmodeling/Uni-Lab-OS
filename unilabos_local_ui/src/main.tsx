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
  useReactFlow,
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
  expandLayoutToWidth,
  layoutFlowGraph,
  workflowDraftKey,
} from './workflowDraft';
import { createPseudoFlowJson } from './workflowExport';
import { WorkstationDemo } from './WorkstationDemo';
import './taskSchedulerBench.css';
import { TaskSchedulerBench } from './TaskSchedulerBench';
import { TaskSchedulerHeaderActions } from './TaskSchedulerBench';
import { OpcSimulatorDialog } from './OpcSimulatorDialog';
import { OpcProfileSpecDialog } from './OpcProfileSpecDialog';
import {
  beginOpcSimulatorControlOperation,
  buildOpcActionCatalog,
  buildOpcVariableTypeCatalog,
  collectOpcProfileVariableNames,
  formatOpcProfileGenerateError,
  canonicalProfileJson,
  collectScheduledTemplateIds,
  createLatestOperationGate,
  createOpcSimulatorClient,
  createProfileSaveSnapshot,
  DEFAULT_OPC_SIMULATOR_URL,
  defaultOpcSimulatorFileName,
  finishOpcSimulatorControlOperation,
  OPC_PROFILE_FORCE_REVISION,
  OPC_REFERENCE_TEMPLATE_FILE,
  isSimulatorStartAllowed,
  describeSimulatorStartBlock,
  restoreStatusNeedsConfirm,
  isProfileSaveSnapshotCurrent,
  opcSimulatorStopMessage,
  validateOpcSimulatorProfile,
  type OpcProfileApiResponse,
  OpcSimulatorHttpError,
  type OpcSimulatorProfile,
  type OpcSimulatorStatus,
} from './opcSimulatorProfile';
import {
  buildTaskProcessLogLines,
  buildTaskVariableRows,
  fetchTaskActionLogs,
  groupTaskActionLogsByNode,
  mergeTaskActionLogs,
  type TaskActionLogEntry,
} from './taskActionLog';
import {
  buildTaskLogLines,
  type TaskLogSession,
  type TaskWorkspaceLogEvent,
} from './taskLogSession';
import { TASK_EXECUTION_POLL_INTERVAL_MS } from './taskPolling';
import {
  buildSampleProcessRows,
  buildTaskGanttEntries,
  canDeleteTaskTemplate,
  createOperationGenerationController,
  createSynchronousActionGate,
  createTaskTemplateDraft,
  createTaskTemplateId,
  createWorkspaceEpochController,
  formatElapsedDurationMs,
  formatTaskActionTimingTitle,
  isTaskWaitingStatus,
  renameTaskTemplate,
  resolveTaskTemplateNameDraft,
  resolveTemplateNodes,
  taskActionProgressMinWidth,
  taskLocalWaitingReason,
  updateScheduledTemplateDraft,
} from './taskOrchestration';
import type { TaskActionExecutionRecord, TriggerCondition } from './taskOrchestration';
import {
  createTaskExecutionController,
  createTaskExecutionStatus,
  createTaskOrchestrationClient,
  fromApiTrigger,
  pauseTaskSchedulerReliably,
  runTaskExecutionHarvestCycle,
  runTaskExecutionCycle,
  runTaskSchedulerTransition,
  TaskOrchestrationBusinessError,
  TaskOrchestrationServiceUnavailableError,
  toApiTrigger,
  type TaskExecutionStatus,
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
  unit?: string;
  options?: Array<{ value: string | number | boolean; label: string }>;
};

type PresetPayload = {
  id: string;
  title: string;
  default_workflow_name: string;
  default_config: {
    graph?: string;
    url?: string;
    csv?: string;
    task_sample_start_interval_seconds?: number;
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
  live_statuses?: Record<string, Record<string, LiveStatus>>;
};

type LiveStatus = {
  label: string;
  value?: number | null;
  unit?: string;
  state?: 'ok' | 'error' | 'final' | string;
  message?: string;
  updated_at?: number;
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
  plcDeviceId?: string;
  display_name?: string;
  aliases?: string[];
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
  executionCursor?: number;
  actionRecords: TaskActionExecutionRecord[];
  nodeParameters: Record<string, Record<string, unknown>>;
};
type TaskWorkspaceState = {
  taskTemplates: TaskTemplate[];
  taskInstances: TaskInstance[];
  taskEvents: string[];
  taskEventRecords: TaskWorkspaceLogEvent[];
};
type TaskPlcStatus = {
  device_id: string;
  url?: string;
  connected: boolean;
  registered_variables: string[];
  variable_aliases?: Record<string, string>;
};

function taskTriggerForExport(condition: TriggerCondition) {
  const plcDeviceId = condition.plcDeviceId?.trim();
  return {
    kind: 'opc' as const,
    config: {
      ...(plcDeviceId ? { plc_device_id: plcDeviceId } : {}),
      variable: condition.variableName,
      value: condition.value,
    },
  };
}

function taskTriggerLogLabel(trigger: unknown) {
  if (!trigger || typeof trigger !== 'object') return '条件已满足';
  const item = trigger as { kind?: string; config?: Record<string, unknown> };
  const config = item.config || {};
  if (item.kind === 'opc') return `OPC ${String(config.variable || '')} == ${String(config.value)}`;
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

function taskWaitingText(reason?: ApiWaitingReason) {
  if (!reason) return '正在检查前置条件';
  const context = reason.context || {};
  const variable = context.variable;
  if (!variable) return reason.message || reason.code;
  const expected = context.expected === undefined ? '-' : formatOpcValue(context.expected);
  const actual = context.actual === undefined ? '-' : formatOpcValue(context.actual);
  const plc = String(context.plc_device_id || 'PLC');
  const updatedAt = typeof context.updated_at === 'number'
    ? `，更新于 ${new Date(context.updated_at).toLocaleTimeString('zh-CN', { hour12: false })}`
    : '';
  return `${reason.message || reason.code} · ${plc} / ${String(variable)}：期望 ${expected}，当前 ${actual}${updatedAt}`;
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
  const taskEventRecords = response.workspace.events.map((event) => ({
    kind: event.kind,
    timestamp: event.timestamp,
    text: taskEventText(
      event,
      event.template_id
        ? [
            ...(templateById.get(event.template_id)?.input_triggers || []),
          ]
        : [],
    ),
  }));
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
      executionCursor: instance.execution_state?.cursor,
      actionRecords: (instance.execution_state?.records || []).map((record) => ({
        nodeId: record.node_id,
        attempt: record.attempt,
        executionId: record.execution_id,
        status: record.status,
        startedAt: record.started_at ?? undefined,
        finishedAt: record.finished_at ?? undefined,
      })),
      nodeParameters: instance.payload?.node_parameters || {},
    })),
    taskEvents: taskEventRecords.map((event) => event.text).slice(-20).reverse(),
    taskEventRecords,
    scheduledTemplateIds: response.workspace.scheduled_template_ids,
    isSchedulerRunning: !response.workspace.scheduler_paused,
    waitingReasons: response.schedule?.waiting_reasons || {},
    scheduleEntries: buildTaskGanttEntries(response.workspace.schedule_entries),
  };
}

function taskApiErrorMessage(error: unknown) {
  if (error instanceof TaskOrchestrationServiceUnavailableError) {
    return 'Task 编排服务不可用';
  }
  if (error instanceof TaskOrchestrationBusinessError && error.status === 404) {
    return 'Task API 未找到该接口，请重启 task-orchestration 服务（8091）后再试';
  }
  if (error instanceof TaskOrchestrationBusinessError && error.status === 409) {
    if (error.message.includes('pause scheduler')) {
      return '请先点击「暂停派发」后再清空队列';
    }
    if (error.message.includes('explicit recovery')) {
      return '存在失败动作导致的暂停，请先「清空队列」后再点「运行调度」';
    }
    return `Task 状态已变化，请刷新后重试：${error.message}`;
  }
  return error instanceof Error ? error.message : 'Task 编排操作失败';
}

export function createEmptyTaskWorkspaceState() {
  return {
    taskTemplates: [] as TaskTemplate[],
    taskInstances: [] as TaskInstance[],
    taskEvents: [] as string[],
    taskEventRecords: [] as TaskWorkspaceLogEvent[],
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
  plc?: {
    device_id: string;
    url?: string | null;
    connected: boolean;
    registered_variables: string[];
    variable_aliases?: Record<string, string>;
  };
  task_orchestration?: {
    distributed: boolean;
    message?: string;
    variable_count?: number;
  };
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
  url: DEFAULT_OPC_SIMULATOR_URL,
  csv: '',
  no_subscription: true,
  show_csv: false,
};
const DRAFT_STORAGE_PREFIX = 'unilabos.workflowDraft';
// Hard code!!!
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
  return taskNodes.length === 1 ? taskNodes[0].data.label : `${taskNodes[0].data.label} 等 ${taskNodes.length} 步`;
}

function taskStatusText(status: 'done' | 'running' | 'blocked' | 'ready' | 'failed' | 'cancelled') {
  if (status === 'done') return '完成';
  if (status === 'running') return '运行中';
  if (status === 'failed') return '失败';
  if (status === 'cancelled') return '已取消';
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

const FLOW_FIT_VIEW_OPTIONS = { padding: 0.06, maxZoom: 1.2, duration: 180 };

function FlowViewportFitter({ enabled, fitKey }: { enabled: boolean; fitKey: number }) {
  const { fitView } = useReactFlow();

  useEffect(() => {
    if (!enabled) return;
    const frame = window.requestAnimationFrame(() => {
      void fitView(FLOW_FIT_VIEW_OPTIONS);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [enabled, fitKey, fitView]);

  return null;
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
  const [viewportFitKey, setViewportFitKey] = useState(0);
  const bumpViewportFit = useCallback(() => setViewportFitKey((current) => current + 1), []);
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
  const [sensorGates, setSensorGates] = useState<Record<string, boolean>>(
    () => Object.fromEntries(Object.entries(DEFAULT_SENSOR_GATES).map(([key, gate]) => [key, gate.free])),
  );
  const [taskEventRecords, setTaskEventRecords] = useState<TaskWorkspaceLogEvent[]>([]);
  const [taskWorkspaceVersion, setTaskWorkspaceVersion] = useState<number | null>(null);
  const [taskWorkspacePath, setTaskWorkspacePath] = useState('szlab_canvas_workflow.json');
  const [taskScheduleEntries, setTaskScheduleEntries] = useState<ReturnType<typeof taskWorkspaceFromApi>['scheduleEntries']>([]);
  const [taskWaitingReasons, setTaskWaitingReasons] = useState<Record<string, ApiWaitingReason>>({});
  const [isTaskDetailModalOpen, setIsTaskDetailModalOpen] = useState(false);
  const [taskTemplateNameDraft, setTaskTemplateNameDraft] = useState('');
  const [isTaskTemplateCreating, setIsTaskTemplateCreating] = useState(false);
  const [taskMutationInFlightCount, setTaskMutationInFlightCount] = useState(0);
  const [taskServiceError, setTaskServiceError] = useState('');
  const [isTaskWorkspaceLoading, setIsTaskWorkspaceLoading] = useState(false);
  const [taskOpcUrl, setTaskOpcUrl] = useState(DEFAULT_CONFIG.url);
  const [taskOpcStatus, setTaskOpcStatus] = useState<TaskPlcStatus | null>(null);
  const [taskOpcMessage, setTaskOpcMessage] = useState('');
  const [isTaskOpcConnecting, setIsTaskOpcConnecting] = useState(false);
  const [taskSampleCount, setTaskSampleCount] = useState(3);
  const [taskSampleStartIntervalSeconds, setTaskSampleStartIntervalSeconds] = useState(0);
  const [selectedTaskTemplateId, setSelectedTaskTemplateId] = useState<string | null>(null);
  const [selectedTaskInstanceId, setSelectedTaskInstanceId] = useState<string | null>(null);
  const [scheduledTemplateIds, setScheduledTemplateIds] = useState<string[]>([]);
  const [taskUtilityDrawer, setTaskUtilityDrawer] = useState<'opc-connection' | null>(null);
  const [taskExecutionEnvironment, setTaskExecutionEnvironment] = useState<'simulated' | 'real'>('simulated');
  const [taskTemplateDrawerTab, setTaskTemplateDrawerTab] = useState<'templates' | 'scheduled'>('templates');
  const [taskLogTab, setTaskLogTab] = useState<'waiting' | 'events' | 'action'>('waiting');
  const [isOpcSimulatorDrawerOpen, setIsOpcSimulatorDrawerOpen] = useState(false);
  const [csvVariables, setCsvVariables] = useState<CsvVariable[]>([]);
  const [taskActionLogs, setTaskActionLogs] = useState<TaskActionLogEntry[]>([]);
  const [taskLogError, setTaskLogError] = useState('');
  const [taskLogSession, setTaskLogSession] = useState<TaskLogSession | null>(null);
  const taskLogAfterSeqRef = useRef(0);
  const taskLogBootstrappedRef = useRef(false);
  const [isSchedulerRunning, setIsSchedulerRunning] = useState(false);
  const [isSchedulerTransitioning, setIsSchedulerTransitioning] = useState(false);
  const [isTaskExecutionDraining, setIsTaskExecutionDraining] = useState(false);
  const [hasActiveServerExecution, setHasActiveServerExecution] = useState(false);
  const [taskExecutionWorkflow, setTaskExecutionWorkflow] = useState<WorkflowJson | null>(null);
  const [taskExecutionStatus, setTaskExecutionStatus] = useState<TaskExecutionStatus>(
    createTaskExecutionStatus(),
  );
  const [showOpcSimulatorDialog, setShowOpcSimulatorDialog] = useState(false);
  const [showOpcSimulatorReferenceDialog, setShowOpcSimulatorReferenceDialog] = useState(false);
  const [showOpcSimulatorSpecDialog, setShowOpcSimulatorSpecDialog] = useState(false);
  const [opcSimulatorSpecMarkdown, setOpcSimulatorSpecMarkdown] = useState('');
  const [opcSimulatorReferenceProfile, setOpcSimulatorReferenceProfile] = useState<OpcSimulatorProfile | null>(null);
  const [opcSimulatorProfile, setOpcSimulatorProfile] = useState<OpcSimulatorProfile | null>(null);
  const [opcSimulatorFileName, setOpcSimulatorFileName] = useState('opc-simulator-profile.json');
  const [opcSimulatorProfileFiles, setOpcSimulatorProfileFiles] = useState<string[]>([]);
  const [opcSimulatorConfigDir, setOpcSimulatorConfigDir] = useState('task-orchestration/configs');
  const [opcSimulatorBackendErrors, setOpcSimulatorBackendErrors] = useState<string[]>([]);
  const [opcSimulatorRevision, setOpcSimulatorRevision] = useState<string | null>(null);
  const [opcSimulatorDirty, setOpcSimulatorDirty] = useState(false);
  const [opcSimulatorBusy, setOpcSimulatorBusy] = useState(false);
  const [opcSimulatorMessage, setOpcSimulatorMessage] = useState('');
  const [opcSimulatorStatus, setOpcSimulatorStatus] = useState<OpcSimulatorStatus | null>(null);
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number } | null>(null);
  const [isTaskTemplateEditing, setIsTaskTemplateEditing] = useState(false);
  const contextMenuFirstActionRef = useRef<HTMLButtonElement | null>(null);
  const contextMenuRef = useRef<HTMLDivElement | null>(null);
  const contextMenuTriggerRef = useRef<HTMLElement | null>(null);
  const contextMenuFocusTargetRef = useRef<HTMLElement | null>(null);
  const canvasWorkspaceRef = useRef<HTMLDivElement | null>(null);
  const didInitialCanvasExpandRef = useRef(false);
  const taskOrchestrationRef = useRef<HTMLDivElement | null>(null);
  const canvasToastTimerRef = useRef<number | null>(null);
  const taskWorkspaceStateRef = useRef<TaskWorkspaceState>(createEmptyTaskWorkspaceState());
  const taskTemplatesRef = useRef<TaskTemplate[]>([]);
  const taskInstancesRef = useRef<TaskInstance[]>([]);
  const scheduledTemplateIdsRef = useRef<string[]>([]);
  const selectedTaskTemplateIdRef = useRef<string | null>(null);
  const taskApiRef = useRef(createTaskOrchestrationClient());
  const taskWorkspaceVersionRef = useRef<number | null>(null);
  const taskWorkspacePathRef = useRef(taskWorkspacePath);
  const taskWorkspaceEpochRef = useRef(createWorkspaceEpochController());
  const taskMutationGenerationRef = useRef(createOperationGenerationController());
  const taskActionInFlightCountRef = useRef(0);
  const taskTemplateCreateGateRef = useRef(createSynchronousActionGate());
  const taskTemplateIdCounterRef = useRef(0);
  const taskRuntimeBusyRef = useRef({
    schedulerBusy: false,
    actionInFlight: false,
  });
  const taskRequestQueueRef = useRef<Promise<void>>(Promise.resolve());
  const taskSchedulerTransitionRef = useRef(false);
  const taskExecutionControllerRef = useRef<ReturnType<typeof createTaskExecutionController> | null>(null);
  const opcSimulatorClientRef = useRef(createOpcSimulatorClient());
  const ownedOpcSimulatorRunIdRef = useRef<string | null>(null);
  const opcSimulatorStatusRef = useRef<OpcSimulatorStatus | null>(null);
  const opcSimulatorStatusGateRef = useRef(createLatestOperationGate());
  const csvVariablesGateRef = useRef(createLatestOperationGate());
  const opcSimulatorGenerateInFlightRef = useRef(false);
  const opcSimulatorGenerateGateRef = useRef(createLatestOperationGate());
  const opcSimulatorControlInFlightRef = useRef(false);
  const opcSimulatorControlTokenRef = useRef(0);
  const opcSimulatorSaveTokenRef = useRef(0);
  const opcSimulatorSaveInFlightRef = useRef(false);
  const opcSimulatorProfileRef = useRef<OpcSimulatorProfile | null>(null);
  const opcSimulatorFileNameRef = useRef(opcSimulatorFileName);
  const actionsRef = useRef<ActionSpec[]>(actions);
  const csvVariablesRef = useRef<CsvVariable[]>(csvVariables);
  opcSimulatorProfileRef.current = opcSimulatorProfile;
  opcSimulatorFileNameRef.current = opcSimulatorFileName;
  actionsRef.current = actions;
  csvVariablesRef.current = csvVariables;
  taskTemplatesRef.current = taskTemplates;
  taskInstancesRef.current = taskInstances;
  scheduledTemplateIdsRef.current = scheduledTemplateIds;
  selectedTaskTemplateIdRef.current = selectedTaskTemplateId;
  taskWorkspacePathRef.current = taskWorkspacePath;
  taskRuntimeBusyRef.current = {
    schedulerBusy: isSchedulerRunning
      || isSchedulerTransitioning
      || isTaskExecutionDraining
      || taskExecutionStatus.tick.active > 0
      || taskExecutionStatus.tick.in_flight > 0
      || taskExecutionStatus.tick.claimed > 0,
    actionInFlight: taskActionInFlightCountRef.current > 0,
  };
  const applyTaskWorkspace = useCallback((response: ApiWorkspaceResponse) => {
    if (response.workspace.workflow_path !== taskWorkspacePathRef.current) return;
    const next = taskWorkspaceFromApi(response);
    taskWorkspaceStateRef.current = {
      taskTemplates: next.taskTemplates,
      taskInstances: next.taskInstances,
      taskEvents: next.taskEvents,
      taskEventRecords: next.taskEventRecords,
    };
    setTaskTemplates(next.taskTemplates);
    taskTemplatesRef.current = next.taskTemplates;
    setTaskInstances(next.taskInstances);
    taskInstancesRef.current = next.taskInstances;
    setTaskEvents(next.taskEvents);
    setTaskEventRecords(next.taskEventRecords);
    setTaskWorkspaceVersion(next.version);
    taskWorkspaceVersionRef.current = next.version;
    scheduledTemplateIdsRef.current = next.scheduledTemplateIds;
    setScheduledTemplateIds(next.scheduledTemplateIds);
    setTaskWaitingReasons(next.waitingReasons);
    setIsSchedulerRunning(next.isSchedulerRunning);
    setHasActiveServerExecution(
      response.workspace.task_instances.some(
        (instance) => (
          instance.status === 'running'
          && Boolean(instance.execution_state?.active_execution_id)
        ),
      ),
    );
    setTaskScheduleEntries(next.scheduleEntries);
    setSelectedTaskTemplateId((current) => (
      next.taskTemplates.some((template) => template.id === current)
        ? current
        : next.taskTemplates[0]?.id || null
    ));
    setTaskServiceError('');
  }, []);
  if (taskExecutionControllerRef.current === null) {
    taskExecutionControllerRef.current = createTaskExecutionController({
      runCycle: (options) => runTaskExecutionCycle({
        ...options,
        workflow: options.workflow,
        fetcher: fetch,
        taskClient: taskApiRef.current,
      }),
      runHarvestCycle: (options) => runTaskExecutionHarvestCycle({
        ...options,
        fetcher: fetch,
        taskClient: taskApiRef.current,
      }),
      applyWorkspace: (response) => {
        if (
          taskActionInFlightCountRef.current === 0
          || response.workspace.scheduler_paused
        ) {
          applyTaskWorkspace(response);
        }
      },
      pauseScheduler: (workflowPath, expectedVersion) => (
        pauseTaskSchedulerReliably({
          taskClient: taskApiRef.current,
          workflowPath,
          expectedVersion,
        })
      ),
      onStatus: setTaskExecutionStatus,
      onError: setTaskServiceError,
      onDrainingChange: setIsTaskExecutionDraining,
      getLatestVersion: () => taskWorkspaceVersionRef.current,
    });
  }
  const resetTaskWorkspace = useCallback(() => {
    taskExecutionControllerRef.current?.pause();
    const emptyTaskWorkspace = resetTaskWorkspaceState(taskWorkspaceStateRef.current);
    taskWorkspaceStateRef.current = emptyTaskWorkspace;
    taskTemplatesRef.current = [];
    taskInstancesRef.current = [];
    setTaskTemplates(emptyTaskWorkspace.taskTemplates);
    setTaskInstances(emptyTaskWorkspace.taskInstances);
    setTaskEvents(emptyTaskWorkspace.taskEvents);
    setTaskEventRecords(emptyTaskWorkspace.taskEventRecords);
    setTaskLogSession(null);
    setTaskWaitingReasons({});
    setSelectedTaskTemplateId(null);
    setIsTaskDetailModalOpen(false);
    setScheduledTemplateIds([]);
    scheduledTemplateIdsRef.current = [];
    setIsSchedulerRunning(false);
    setTaskWorkspaceVersion(null);
    setTaskScheduleEntries([]);
    setTaskExecutionWorkflow(null);
    setTaskExecutionStatus(createTaskExecutionStatus());
    setIsTaskExecutionDraining(false);
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
  const taskNodeDescriptors = useMemo(
    () => nodes.map((node) => ({
      id: node.id,
      deviceId: node.data.deviceId,
      method: node.data.method,
    })),
    [nodes],
  );
  const selectedTaskTemplate = useMemo(
    () => taskTemplates.find((template) => template.id === selectedTaskTemplateId) || taskTemplates[0] || null,
    [selectedTaskTemplateId, taskTemplates],
  );
  useEffect(() => {
    setTaskTemplateNameDraft(selectedTaskTemplate?.name || '');
  }, [selectedTaskTemplate?.id, selectedTaskTemplate?.name]);
  const sampleProcessRows = useMemo(
    () => buildSampleProcessRows(taskInstances, taskTemplates),
    [taskInstances, taskTemplates],
  );
  const selectedTaskInstance = useMemo(
    () => taskInstances.find((item) => item.id === selectedTaskInstanceId) || null,
    [selectedTaskInstanceId, taskInstances],
  );
  const selectedTaskInstanceLogs = useMemo(
    () => (
      selectedTaskInstanceId
        ? taskActionLogs.filter((entry) => entry.instance_id === selectedTaskInstanceId)
        : []
    ),
    [selectedTaskInstanceId, taskActionLogs],
  );
  const selectedTaskLogSections = useMemo(() => {
    const template = taskTemplates.find((item) => item.id === selectedTaskInstance?.templateId);
    return groupTaskActionLogsByNode(
      selectedTaskInstanceLogs,
      template?.nodeIds || [],
    );
  }, [selectedTaskInstance?.templateId, selectedTaskInstanceLogs, taskTemplates]);
  const selectedTaskVariableRows = useMemo(
    () => selectedTaskLogSections.flatMap((section) => buildTaskVariableRows(section.entries)),
    [selectedTaskLogSections],
  );
  const selectedTaskProcessLines = useMemo(
    () => selectedTaskLogSections.flatMap((section) => buildTaskProcessLogLines(section.entries)),
    [selectedTaskLogSections],
  );
  const taskLogLines = useMemo(
    () => taskLogSession
      ? buildTaskLogLines({
          events: taskEventRecords,
          actionEntries: taskActionLogs,
          session: taskLogSession,
        })
      : [],
    [taskActionLogs, taskEventRecords, taskLogSession],
  );
  const scheduledOpcTemplateIds = useMemo(
    () => collectScheduledTemplateIds(scheduledTemplateIds),
    [scheduledTemplateIds],
  );
  const opcSimulatorLocalErrors = useMemo(
    () => opcSimulatorProfile ? validateOpcSimulatorProfile(opcSimulatorProfile) : [],
    [opcSimulatorProfile],
  );
  const canStartOpcSimulator = Boolean(opcSimulatorProfile) && isSimulatorStartAllowed({
    profile: opcSimulatorProfile!,
    localErrors: opcSimulatorLocalErrors,
    backendErrors: opcSimulatorBackendErrors,
    fileName: opcSimulatorFileName,
    revision: opcSimulatorRevision,
    dirty: opcSimulatorDirty,
    managerState: opcSimulatorStatus?.state || 'idle',
    managerRestoreStatus: opcSimulatorStatus?.restore_status || 'not_started',
  });
  const opcSimulatorStartBlockReason = describeSimulatorStartBlock({
    profile: opcSimulatorProfile,
    localErrors: opcSimulatorLocalErrors,
    backendErrors: opcSimulatorBackendErrors,
    fileName: opcSimulatorFileName,
    revision: opcSimulatorRevision,
    dirty: opcSimulatorDirty,
    managerState: opcSimulatorStatus?.state || 'idle',
    managerRestoreStatus: opcSimulatorStatus?.restore_status || 'not_started',
    busy: opcSimulatorBusy,
  });
  const configuredOpcUrl = opcSimulatorProfile?.opc.url || '';
  const runningOpcUrl = ['starting', 'running', 'stopping'].includes(opcSimulatorStatus?.state || '')
    ? opcSimulatorStatus?.opc_url || ''
    : '';
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
  const toggleSensorGate = useCallback((gate: string) => {
    if (liveGateStates[gate] !== undefined) return;
    setSensorGates((current) => ({ ...current, [gate]: !current[gate] }));
    setTaskEvents((current) => [
      ...current,
      `${DEFAULT_SENSOR_GATES[gate]?.label || gate} 手动门控已切换。`,
    ]);
  }, [liveGateStates]);
  const taskPlcStatus = taskOpcStatus || stackStatus?.plc;
  const configuredOpcVariableRows = useMemo<OpcVariableView[]>(
    () => configuredOpcVariables.map((name) => ({ name, currentValue: stackSensorValues[name] })),
    [configuredOpcVariables, stackSensorValues],
  );
  useEffect(() => {
    const params = new URLSearchParams();
    if (config.csv) params.set('csv_path', config.csv);
    const request = csvVariablesGateRef.current.begin();
    fetch(`/api/csv-variables?${params.toString()}`, { signal: request.signal })
      .then((response) => response.ok ? response.json() : { variables: [] })
      .then((payload) => {
        if (!csvVariablesGateRef.current.isCurrent(request.generation)) return;
        const variables = Array.isArray(payload.variables) ? payload.variables : [];
        if (opcSimulatorGenerateInFlightRef.current) {
          opcSimulatorGenerateGateRef.current.invalidate();
          opcSimulatorGenerateInFlightRef.current = false;
          setOpcSimulatorBusy(false);
        }
        csvVariablesRef.current = variables;
        setCsvVariables(variables);
      })
      .catch((error) => {
        if (
          error instanceof DOMException
          && error.name === 'AbortError'
        ) return;
        if (!csvVariablesGateRef.current.isCurrent(request.generation)) return;
        if (opcSimulatorGenerateInFlightRef.current) {
          opcSimulatorGenerateGateRef.current.invalidate();
          opcSimulatorGenerateInFlightRef.current = false;
          setOpcSimulatorBusy(false);
        }
        csvVariablesRef.current = [];
        setCsvVariables([]);
      });
    return () => csvVariablesGateRef.current.invalidate();
  }, [config.csv]);
  useEffect(() => {
    if (!opcSimulatorGenerateInFlightRef.current) return;
    opcSimulatorGenerateGateRef.current.invalidate();
    opcSimulatorGenerateInFlightRef.current = false;
    setOpcSimulatorBusy(false);
  }, [actions, csvVariables]);
  useEffect(() => {
    taskMutationGenerationRef.current.invalidate();
    taskWorkspaceEpochRef.current.begin(taskWorkspacePath);
    taskWorkspaceVersionRef.current = null;
    setTaskWorkspaceVersion(null);
    setIsTaskWorkspaceLoading(false);
    return () => taskWorkspaceEpochRef.current.abort();
  }, [taskWorkspacePath]);
  const loadTaskWorkspace = useCallback(async () => {
    const epoch = taskWorkspaceEpochRef.current.current();
    if (!epoch || epoch.path !== taskWorkspacePath) return;
    setIsTaskWorkspaceLoading(true);
    try {
      const response = await taskApiRef.current.getWorkspace(taskWorkspacePath, epoch.signal);
      if (taskWorkspaceEpochRef.current.isCurrent(epoch)) applyTaskWorkspace(response);
    } catch (error) {
      if (taskWorkspaceEpochRef.current.isCurrent(epoch) && !epoch.signal.aborted) {
        setTaskServiceError(taskApiErrorMessage(error));
      }
    } finally {
      if (taskWorkspaceEpochRef.current.isCurrent(epoch)) setIsTaskWorkspaceLoading(false);
    }
  }, [applyTaskWorkspace, taskWorkspacePath]);
  useEffect(() => {
    if (workspace === 'tasks') void loadTaskWorkspace();
  }, [loadTaskWorkspace, workspace]);
  useEffect(() => {
    setTaskOpcStatus(null);
    setTaskOpcMessage('');
  }, [taskWorkspacePath]);
  useEffect(() => {
    if (workspace !== 'tasks') return;
    let cancelled = false;
    taskLogBootstrappedRef.current = false;
    setTaskActionLogs([]);
    setTaskLogError('');
    void fetchTaskActionLogs(fetch, taskWorkspacePath, { afterSeq: 0 })
      .then((result) => {
        if (cancelled) return;
        taskLogAfterSeqRef.current = result.latest_seq;
        taskLogBootstrappedRef.current = true;
      })
      .catch(() => {
        if (cancelled) return;
        taskLogAfterSeqRef.current = 0;
        taskLogBootstrappedRef.current = true;
        setTaskLogError('日志暂不可用');
      });
    return () => {
      cancelled = true;
      taskLogBootstrappedRef.current = false;
    };
  }, [taskWorkspacePath, workspace]);
  useEffect(() => {
    if (workspace !== 'tasks' || !taskLogBootstrappedRef.current) return;
    const poll = () => {
      void fetchTaskActionLogs(fetch, taskWorkspacePath, {
        afterSeq: taskLogAfterSeqRef.current,
      })
        .then((result) => {
          setTaskLogError('');
          if (result.entries.length) {
            setTaskActionLogs((previous) => mergeTaskActionLogs(previous, result.entries));
          }
          if (result.latest_seq >= taskLogAfterSeqRef.current) {
            taskLogAfterSeqRef.current = result.latest_seq;
          }
        })
        .catch(() => {
          setTaskLogError('日志暂不可用');
        });
    };
    poll();
    const timer = window.setInterval(poll, 1000);
    return () => window.clearInterval(timer);
  }, [taskWorkspacePath, workspace]);
  useEffect(() => {
    if (workspace !== 'tasks' || !selectedTaskInstanceId || !taskLogBootstrappedRef.current) {
      return;
    }
    let cancelled = false;
    void fetchTaskActionLogs(fetch, taskWorkspacePath, {
      afterSeq: 0,
      instanceId: selectedTaskInstanceId,
    })
      .then((result) => {
        if (cancelled) return;
        setTaskLogError('');
        setTaskActionLogs((previous) => {
          const retained = previous.filter((entry) => entry.instance_id !== selectedTaskInstanceId);
          return mergeTaskActionLogs(retained, result.entries);
        });
      })
      .catch(() => {
        if (!cancelled) setTaskLogError('日志暂不可用');
      });
    return () => {
      cancelled = true;
    };
  }, [selectedTaskInstanceId, taskWorkspacePath, workspace]);
  const connectTaskOpc = useCallback(async () => {
    const url = taskOpcUrl.trim();
    if (!url) {
      setTaskOpcMessage('请填写 OPC UA URL');
      return;
    }
    if (!taskWorkspacePath) {
      setTaskOpcMessage('缺少当前 workflow 路径');
      return;
    }
    setIsTaskOpcConnecting(true);
    setTaskOpcMessage('');
    try {
      const response = await fetch('/api/task-opc/connect', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          url,
          task_workspace_path: taskWorkspacePath,
        }),
      });
      const payload = await response.json() as {
        success?: boolean;
        message?: string;
        plc?: TaskPlcStatus;
        task_orchestration?: { distributed?: boolean; message?: string };
      };
      if (!response.ok || !payload.success) {
        const detail = payload.message?.trim()
          || payload.task_orchestration?.message?.trim()
          || (response.ok ? 'PLC 连接失败，请查看 workflow_ui 后端日志' : `PLC 连接失败（HTTP ${response.status}）`);
        throw new Error(detail);
      }
      if (payload.plc) setTaskOpcStatus(payload.plc);
      setTaskOpcMessage(
        payload.task_orchestration?.distributed
          ? `已连接并分发 ${payload.plc?.registered_variables.length || 0} 个 PLC 注册变量`
          : payload.task_orchestration?.message || 'PLC 已连接，但变量快照未分发',
      );
      applyTaskWorkspace(await taskApiRef.current.getWorkspace(taskWorkspacePath));
    } catch (error) {
      setTaskOpcMessage(error instanceof Error ? error.message : 'PLC 连接失败');
    } finally {
      setIsTaskOpcConnecting(false);
    }
  }, [applyTaskWorkspace, taskOpcUrl, taskWorkspacePath]);
  const resetInvalidTaskWorkspace = useCallback(async () => {
    if (!window.confirm('仅清空当前 workflow 的 Task 模板、实例、事件、排程和 PLC 快照，是否继续？')) {
      return;
    }
    taskExecutionControllerRef.current?.pause();
    setTaskExecutionWorkflow(null);
    setTaskExecutionStatus(createTaskExecutionStatus());
    setIsTaskWorkspaceLoading(true);
    try {
      applyTaskWorkspace(await taskApiRef.current.resetWorkspace(taskWorkspacePath));
      setTaskServiceError('');
      setTaskOpcMessage('当前 Task 工作区已重置');
    } catch (error) {
      setTaskServiceError(taskApiErrorMessage(error));
    } finally {
      setIsTaskWorkspaceLoading(false);
    }
  }, [applyTaskWorkspace, taskWorkspacePath]);
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
    taskWorkspaceStateRef.current = {
      taskTemplates,
      taskInstances,
      taskEvents,
      taskEventRecords,
    };
  }, [taskEventRecords, taskEvents, taskInstances, taskTemplates]);

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
    bumpViewportFit();
  }, [bumpViewportFit, leftPanelCollapsed]);

  useEffect(() => {
    if (!draftReady || !nodes.length || didInitialCanvasExpandRef.current) return;
    const frame = window.requestAnimationFrame(() => {
      const canvasWidth = canvasWorkspaceRef.current?.clientWidth ?? 0;
      if (!canvasWidth) return;
      didInitialCanvasExpandRef.current = true;
      setNodes((current) => expandLayoutToWidth(current, canvasWidth));
      bumpViewportFit();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [bumpViewportFit, draftReady, nodes.length]);

  useEffect(() => {
    fetch('/api/preset')
      .then((response) => response.json())
      .then((payload: PresetPayload) => {
        const payloadActions = payload.actions || [];
        const storageKey = `${DRAFT_STORAGE_PREFIX}.${payload.id || 'default'}`;
        setTitle(payload.title || 'szlab 本地调试工具');
        if (opcSimulatorGenerateInFlightRef.current) {
          opcSimulatorGenerateGateRef.current.invalidate();
          opcSimulatorGenerateInFlightRef.current = false;
          setOpcSimulatorBusy(false);
        }
        actionsRef.current = payloadActions;
        setActions(payloadActions);
        setDraftStorageKey(storageKey);
        setTaskWorkspacePath(`${payload.default_workflow_name || 'szlab_canvas_workflow'}.json`);
        const savedDraft = loadSavedDraft(storageKey, payloadActions);
        if (savedDraft) {
          setWorkflowName(savedDraft.name);
          setNodes(savedDraft.nodes);
          setEdges(savedDraft.edges.map((edge) => ({ ...edge, animated: true })));
          setStartNodeId(loadSavedStartNodeId(storageKey, savedDraft.nodes));
          setViewportFitKey((current) => current + 1);
        } else {
          setWorkflowName(payload.default_workflow_name || 'szlab_canvas_workflow');
          setStartNodeId(null);
        }
        setConfig((current) => ({
          ...current,
          graph: payload.default_config?.graph ?? DEFAULT_CONFIG.graph,
          url: payload.default_config?.url ?? DEFAULT_CONFIG.url,
          csv: payload.default_config?.csv ?? DEFAULT_CONFIG.csv,
          no_subscription: payload.default_config?.no_subscription ?? DEFAULT_CONFIG.no_subscription,
          show_csv: payload.default_config?.show_csv ?? DEFAULT_CONFIG.show_csv,
        }));
        setTaskOpcUrl(payload.default_config?.url ?? DEFAULT_CONFIG.url);
        const configuredInterval = Number(
          payload.default_config?.task_sample_start_interval_seconds ?? 0,
        );
        setTaskSampleStartIntervalSeconds(
          Number.isFinite(configuredInterval) && configuredInterval >= 0
            ? configuredInterval
            : 0,
        );
        setDraftReady(true);
      })
      .catch((error) => setMessage(`preset 加载失败: ${error.message}`));
  }, [resetTaskWorkspace]);

  const refreshStackStatus = useCallback(async () => {
    setIsRefreshingStack(true);
    try {
      const params = new URLSearchParams();
      if (taskWorkspacePath && taskWorkspaceVersion !== null) {
        params.set('task_workspace_path', taskWorkspacePath);
        params.set('task_workspace_version', String(taskWorkspaceVersion));
      }
      const response = await fetch(`/api/stack-status${params.size ? `?${params}` : ''}`);
      const payload: StackStatusPayload = await response.json();
      setStackStatus(payload);
      setStackError(payload.success ? '' : (payload.message || '堆栈状态暂不可用'));
    } catch (error) {
      setStackError(error instanceof Error ? error.message : String(error));
    } finally {
      setIsRefreshingStack(false);
    }
  }, [taskWorkspacePath, taskWorkspaceVersion]);

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
    if (workspace === 'tasks') return;
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
  }, [refreshSensorArrays, refreshStackStatus, workspace]);

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
  ) => {
    const path = taskWorkspacePathRef.current;
    const epoch = taskWorkspaceEpochRef.current.current();
    const generation = taskMutationGenerationRef.current.begin();
    taskActionInFlightCountRef.current += 1;
    taskRuntimeBusyRef.current = {
      ...taskRuntimeBusyRef.current,
      actionInFlight: true,
    };
    setTaskMutationInFlightCount(taskActionInFlightCountRef.current);
    const isCurrent = () => Boolean(
      epoch
      && taskWorkspaceEpochRef.current.isCurrent(epoch)
      && taskWorkspacePathRef.current === path
      && taskMutationGenerationRef.current.isCurrent(generation)
    );
    const execute = async () => {
      if (!epoch || !taskWorkspaceEpochRef.current.isCurrent(epoch) || taskWorkspacePathRef.current !== path) return;
      const run = async () => {
        const version = taskWorkspaceVersionRef.current;
        if (version === null) throw new TaskOrchestrationServiceUnavailableError();
        return operation(version);
      };
      try {
        const response = await run();
        if (isCurrent()) applyTaskWorkspace(response);
      } catch (error) {
        if (error instanceof TaskOrchestrationBusinessError && error.status === 409) {
          try {
            const latest = await taskApiRef.current.getWorkspace(path, epoch.signal);
            if (!taskWorkspaceEpochRef.current.isCurrent(epoch)) return;
            const response = await operation(latest.version);
            if (isCurrent()) applyTaskWorkspace(response);
            return;
          } catch (retryError) {
            if (isCurrent()) setTaskServiceError(taskApiErrorMessage(retryError));
            return;
          }
        }
        if (isCurrent()) setTaskServiceError(taskApiErrorMessage(error));
      }
    };
    const queued = taskRequestQueueRef.current.then(execute, execute);
    taskRequestQueueRef.current = queued.catch(() => undefined);
    try {
      await queued;
    } finally {
      taskActionInFlightCountRef.current = Math.max(0, taskActionInFlightCountRef.current - 1);
      taskRuntimeBusyRef.current = {
        ...taskRuntimeBusyRef.current,
        actionInFlight: taskActionInFlightCountRef.current > 0,
      };
      setTaskMutationInFlightCount(taskActionInFlightCountRef.current);
    }
  }, [applyTaskWorkspace]);

  const createTaskTemplateFromNodes = useCallback(async (templateName: string, templateNodes: Node<ActionNodeData>[]) => {
    if (!taskTemplateCreateGateRef.current.tryStart()) return;
    setIsTaskTemplateCreating(true);
    try {
    const orderedNodeIds = orderSelectedNodesByPlan(templateNodes, executionPlan.executableNodes).map((node) => node.id);
    if (!orderedNodeIds.length) {
      setMessage('请先在流程画布中选择节点，再保存为 Task 模板。');
      return;
    }
    const selectedNodesById = new Map(nodes.map((node) => [node.id, node]));
    const orderedNodes = orderedNodeIds.map((nodeId) => selectedNodesById.get(nodeId)).filter(Boolean) as Node<ActionNodeData>[];
    const taskNodes = orderedNodes.map((node) => ({
      id: node.id,
      deviceId: node.data.deviceId,
      method: node.data.method,
      opcVariables: node.data.opcVariables || [],
    }));
    const taskName = templateName || summarizeTaskName(orderedNodes);
    const templateId = createTaskTemplateId(++taskTemplateIdCounterRef.current);
    const draft = createTaskTemplateDraft(templateId, taskName, taskNodes);
    await mutateTaskWorkspace(async (version) => {
      const response = await taskApiRef.current.createTemplate(taskWorkspacePath, version, {
        id: draft.id,
        name: draft.name,
        workflow_path: taskWorkspacePath,
        node_ids: draft.nodeIds,
        resources: [],
        input_triggers: [],
        output_triggers: [],
      });
      return response;
    });
    if (taskWorkspacePathRef.current === taskWorkspacePath && taskTemplatesRef.current.some((item) => item.id === templateId)) {
      setSelectedTaskTemplateId(templateId);
      showCanvasToast('已保存 Task 模板');
    }
    } finally {
      taskTemplateCreateGateRef.current.finish();
      setIsTaskTemplateCreating(false);
    }
  }, [executionPlan.executableNodes, mutateTaskWorkspace, nodes, showCanvasToast, taskWorkspacePath]);

  const createTaskTemplateFromSelection = useCallback(() => {
    void createTaskTemplateFromNodes(summarizeTaskName(selectedTaskNodes), selectedTaskNodes);
  }, [createTaskTemplateFromNodes, selectedTaskNodes]);

  const deleteTaskTemplate = useCallback((template: TaskTemplate) => {
    if (!taskTemplatesRef.current.some((item) => item.id === template.id)) {
      return;
    }
    const isAllowed = () => canDeleteTaskTemplate(
      template.id,
      taskInstancesRef.current,
      taskRuntimeBusyRef.current,
    );
    if (!isAllowed()) {
      showCanvasToast('模板仍有关联运行任务或调度操作，暂不能删除');
      return;
    }
    const relatedInstanceCount = taskInstancesRef.current.filter((task) => task.templateId === template.id).length;
    const relatedInstancesHint = relatedInstanceCount
      ? `这将同时删除 ${relatedInstanceCount} 个关联 Task 实例。`
      : '';
    if (!window.confirm(`确定删除 Task 模板「${template.name}」吗？${relatedInstancesHint}`)) {
      return;
    }
    if (!isAllowed()) {
      showCanvasToast('模板状态已变化，请等待相关任务或操作结束后重试');
      return;
    }
    void mutateTaskWorkspace(async (version) => {
      const response = await taskApiRef.current.deleteTemplate(taskWorkspacePath, version, template.id);
      showCanvasToast('已删除 Task 模板');
      return response;
    });
  }, [mutateTaskWorkspace, showCanvasToast, taskWorkspacePath]);

  const commitSelectedTaskTemplateName = useCallback(() => {
    const templateId = selectedTaskTemplateIdRef.current;
    const template = taskTemplatesRef.current.find((item) => item.id === templateId);
    if (!template) return;
    const name = resolveTaskTemplateNameDraft(taskTemplateNameDraft, template.name);
    setTaskTemplateNameDraft(name);
    if (name === template.name) return;
    const nextTemplates = renameTaskTemplate(taskTemplatesRef.current, template.id, name);
    taskTemplatesRef.current = nextTemplates;
    setTaskTemplates(nextTemplates);
    void mutateTaskWorkspace((version) => taskApiRef.current.updateTemplate(
      taskWorkspacePath,
      version,
      template.id,
      { name },
    ));
  }, [mutateTaskWorkspace, taskTemplateNameDraft, taskWorkspacePath]);

  const addTemplateToSchedule = useCallback((templateId: string) => {
    if (!taskTemplatesRef.current.some((item) => item.id === templateId)) return;
    const next = updateScheduledTemplateDraft(scheduledTemplateIdsRef.current, templateId, 'add');
    if (next === scheduledTemplateIdsRef.current) return;
    scheduledTemplateIdsRef.current = next;
    setScheduledTemplateIds(next);
    void mutateTaskWorkspace((version) => taskApiRef.current.updateScheduledTemplates(
      taskWorkspacePath, version, next,
    ));
  }, [mutateTaskWorkspace, taskWorkspacePath]);

  const removeTemplateFromSchedule = useCallback((templateId: string) => {
    const next = updateScheduledTemplateDraft(scheduledTemplateIdsRef.current, templateId, 'remove');
    if (next === scheduledTemplateIdsRef.current) return;
    scheduledTemplateIdsRef.current = next;
    setScheduledTemplateIds(next);
    void mutateTaskWorkspace((version) => taskApiRef.current.updateScheduledTemplates(
      taskWorkspacePath, version, next,
    ));
  }, [mutateTaskWorkspace, taskWorkspacePath]);

  const createTaskInstances = useCallback(() => {
    const templateIds = scheduledTemplateIdsRef.current;
    if (!templateIds.length) {
      setMessage('请先将 Task Template 拖入 Resource Schedule。');
      return;
    }
    const samples = SAMPLE_NAMES.slice(0, taskSampleCount);
    void mutateTaskWorkspace((version) => taskApiRef.current.generateInstances(
      taskWorkspacePath,
      version,
      templateIds,
      samples,
      taskSampleStartIntervalSeconds,
    ));
  }, [
    mutateTaskWorkspace,
    taskSampleCount,
    taskSampleStartIntervalSeconds,
    taskWorkspacePath,
  ]);

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

  const updateTaskInstanceParameters = useCallback((
    taskId: string,
    nodeParameters: Record<string, Record<string, unknown>>,
  ) => {
    void mutateTaskWorkspace((version) => taskApiRef.current.updateInstanceParameters(
      taskWorkspacePath,
      version,
      taskId,
      nodeParameters,
    ));
  }, [mutateTaskWorkspace, taskWorkspacePath]);

  const advanceTaskSchedule = useCallback(() => {
    void mutateTaskWorkspace((version) => taskApiRef.current.advance(taskWorkspacePath, version));
  }, [mutateTaskWorkspace, taskWorkspacePath]);

  const clearTaskQueue = useCallback(() => {
    if (!taskInstances.length) {
      return;
    }
    if (isSchedulerRunning) {
      showCanvasToast('请先暂停派发后再清空 Task Queue');
      return;
    }
    if (!window.confirm('清空当前 workflow 的全部 Task 实例（含失败/已完成/卡在运行中）？模板与 Resource Schedule 不会删除。')) {
      return;
    }
    taskExecutionControllerRef.current?.pause();
    setTaskExecutionWorkflow(null);
    setTaskExecutionStatus(createTaskExecutionStatus());
    void mutateTaskWorkspace((version) => taskApiRef.current.clearInstances(
      taskWorkspacePath,
      version,
    ));
  }, [
    isSchedulerRunning,
    mutateTaskWorkspace,
    showCanvasToast,
    taskInstances.length,
    taskWorkspacePath,
  ]);

  const downloadTaskTemplates = useCallback((templateIds: string[]) => {
    const selectedIds = new Set(templateIds);
    const templates = taskTemplatesRef.current.filter((template) => selectedIds.has(template.id));
    if (!templates.length) {
      showCanvasToast('请先选择要下载的 Task 模板');
      return;
    }
    const exportedAt = new Date().toISOString();
    const isSingle = templates.length === 1;
    const safeName = isSingle
      ? templates[0].name.trim().replace(/[^\p{L}\p{N}._-]+/gu, '-').replace(/^-+|-+$/g, '') || templates[0].id
      : `selected-${templates.length}`;
    downloadJson(`task-template-${safeName}.json`, {
      schema: 'unilabos.task-templates',
      schema_version: 1,
      exported_at: exportedAt,
      source_workflow_path: taskWorkspacePath,
      templates: templates.map((template) => ({
        id: template.id,
        name: template.name,
        workflow_path: taskWorkspacePath,
        node_ids: template.nodeIds,
        resources: template.resources,
        input_triggers: template.inputTriggers.map(taskTriggerForExport),
        output_triggers: template.outputTriggers.map(taskTriggerForExport),
      })),
    });
    showCanvasToast(isSingle ? '已下载 Task 模板' : `已下载 ${templates.length} 个 Task 模板`);
  }, [showCanvasToast, taskWorkspacePath]);

  const deleteTaskTemplates = useCallback((
    templateIds: string[],
    mode: 'single' | 'selected' | 'all',
  ) => {
    const requestedIds = new Set(templateIds);
    const templates = taskTemplatesRef.current.filter((template) => requestedIds.has(template.id));
    if (!templates.length) return;
    const schedulerBusy = isSchedulerRunning
      || isSchedulerTransitioning
      || isTaskExecutionDraining
      || taskExecutionStatus.tick.active > 0
      || taskExecutionStatus.tick.in_flight > 0
      || taskExecutionStatus.tick.claimed > 0;
    if (schedulerBusy || taskMutationInFlightCount > 0) {
      showCanvasToast('请等待当前调度操作结束后再删除 Task 模板');
      return;
    }
    const deletedIds = new Set(templates.map((template) => template.id));
    const relatedInstanceCount = taskInstancesRef.current.filter(
      (instance) => deletedIds.has(instance.templateId),
    ).length;
    const instanceHint = relatedInstanceCount
      ? `，以及关联的 ${relatedInstanceCount} 个 Task 实例`
      : '';
    const target = mode === 'single'
      ? `Task 模板「${templates[0].name}」`
      : mode === 'all'
        ? `全部 ${templates.length} 个历史 Task 模板`
        : `选中的 ${templates.length} 个 Task 模板`;
    if (!window.confirm(`确定删除${target}${instanceHint}吗？此操作不可撤销。`)) {
      return;
    }
    taskExecutionControllerRef.current?.pause();
    setTaskExecutionWorkflow(null);
    setTaskExecutionStatus(createTaskExecutionStatus());
    void mutateTaskWorkspace(async (version) => {
      const response = await taskApiRef.current.deleteTemplates(
        taskWorkspacePath,
        version,
        templates.map((template) => template.id),
      );
      showCanvasToast(mode === 'all'
        ? `已清空 ${templates.length} 个 Task 模板`
        : `已删除 ${templates.length} 个 Task 模板`);
      return response;
    });
  }, [
    isSchedulerRunning,
    isSchedulerTransitioning,
    isTaskExecutionDraining,
    mutateTaskWorkspace,
    showCanvasToast,
    taskExecutionStatus.tick.active,
    taskExecutionStatus.tick.claimed,
    taskExecutionStatus.tick.in_flight,
    taskMutationInFlightCount,
    taskWorkspacePath,
  ]);

  const clearTaskTemplates = useCallback(() => {
    deleteTaskTemplates(
      taskTemplatesRef.current.map((template) => template.id),
      'all',
    );
  }, [deleteTaskTemplates]);

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

  const generateOpcSimulatorProfile = useCallback(async () => {
    if (opcSimulatorGenerateInFlightRef.current) return;
    if (!scheduledOpcTemplateIds.length) return;
    if (
      opcSimulatorRevision
      && !window.confirm(
        '已存在已保存的 OPC 模拟配置。重新生成会覆盖当前工作台内容（需再次保存才会写盘）。继续？',
      )
    ) {
      return;
    }
    opcSimulatorGenerateInFlightRef.current = true;
    const request = opcSimulatorGenerateGateRef.current.begin();
    opcSimulatorSaveTokenRef.current += 1;
    setOpcSimulatorBusy(true);
    setOpcSimulatorMessage('');
    try {
      const workflowPayload = workflow || await buildWorkflow();
      if (!opcSimulatorGenerateGateRef.current.isCurrent(request.generation)) return;
      const actionCatalog = buildOpcActionCatalog(actionsRef.current);
      const referencedVariableNames = collectOpcProfileVariableNames({
        workflow: workflowPayload,
        templates: taskTemplates.map((template) => ({
          id: template.id,
          nodeIds: template.nodeIds,
        })),
        scheduledTemplateIds: scheduledOpcTemplateIds,
        actionCatalog,
      });
      const fileName = defaultOpcSimulatorFileName(workflowName);
      const response = await fetch('/api/opc-simulator/profiles:generate', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          workflow: workflowPayload,
          templates: taskTemplates.map((template) => ({
            id: template.id,
            name: template.name,
            workflow_path: taskWorkspacePath,
            node_ids: template.nodeIds,
            resources: template.resources,
            input_triggers: [],
            output_triggers: [],
          })),
          scheduled_template_ids: scheduledOpcTemplateIds,
          action_catalog: actionCatalog,
          variable_catalog: buildOpcVariableTypeCatalog(csvVariablesRef.current, referencedVariableNames),
          name: `${workflowName || 'Workflow'} OPC Simulator`,
          file_name: fileName,
          opc_url: taskOpcUrl || DEFAULT_CONFIG.url,
        }),
        signal: request.signal,
      });
      const result = await response.json().catch(() => null) as OpcProfileApiResponse | { detail?: unknown } | null;
      if (!opcSimulatorGenerateGateRef.current.isCurrent(request.generation)) return;
      if (!response.ok || !result || !('profile' in result)) {
        const detail = result && 'detail' in result ? result.detail : null;
        throw new Error(formatOpcProfileGenerateError(response.status, detail));
      }
      opcSimulatorProfileRef.current = result.profile;
      opcSimulatorFileNameRef.current = result.file_name || fileName;
      setOpcSimulatorProfile(result.profile);
      setOpcSimulatorFileName(result.file_name || fileName);
      setOpcSimulatorBackendErrors(result.validation_errors || []);
      setOpcSimulatorRevision(null);
      setOpcSimulatorDirty(true);
      setShowOpcSimulatorDialog(true);
    } catch (error) {
      if (!opcSimulatorGenerateGateRef.current.isCurrent(request.generation)) return;
      setOpcSimulatorMessage(error instanceof Error ? error.message : '生成 OPC 模拟配置失败');
    } finally {
      if (opcSimulatorGenerateGateRef.current.isCurrent(request.generation)) {
        opcSimulatorGenerateInFlightRef.current = false;
        setOpcSimulatorBusy(false);
      }
    }
  }, [
    buildWorkflow,
    opcSimulatorRevision,
    scheduledOpcTemplateIds,
    taskOpcUrl,
    taskTemplates,
    taskWorkspacePath,
    workflow,
    workflowName,
  ]);

  const updateOpcSimulatorProfile = useCallback((profile: OpcSimulatorProfile) => {
    opcSimulatorProfileRef.current = profile;
    setOpcSimulatorProfile(profile);
    setOpcSimulatorDirty(true);
    setOpcSimulatorBackendErrors([]);
    setOpcSimulatorMessage('');
  }, []);

  const loadOpcSimulatorProfile = useCallback(async (
    fileName = opcSimulatorFileNameRef.current || defaultOpcSimulatorFileName(workflowName),
    options?: { openDialog?: boolean; successMessage?: string },
  ): Promise<OpcProfileApiResponse | false> => {
    if (!fileName) return false;
    opcSimulatorFileNameRef.current = fileName;
    setOpcSimulatorFileName(fileName);
    setOpcSimulatorBusy(true);
    try {
      const result = await opcSimulatorClientRef.current.load(fileName);
      opcSimulatorProfileRef.current = result.profile;
      setOpcSimulatorProfile(result.profile);
      setOpcSimulatorBackendErrors(result.validation_errors || []);
      setOpcSimulatorRevision(result.revision || null);
      setOpcSimulatorDirty(false);
      setOpcSimulatorMessage(options?.successMessage || '已重新载入后端版本');
      if (options?.openDialog) setShowOpcSimulatorDialog(true);
      return result;
    } catch (error) {
      setOpcSimulatorMessage(error instanceof Error ? error.message : '载入 OPC 模拟配置失败');
      return false;
    } finally {
      setOpcSimulatorBusy(false);
    }
  }, [workflowName]);

  const refreshOpcSimulatorProfileFiles = useCallback(async (): Promise<string[]> => {
    try {
      const result = await opcSimulatorClientRef.current.listProfiles();
      setOpcSimulatorProfileFiles(result.files);
      setOpcSimulatorConfigDir(result.config_dir);
      const defaultName = defaultOpcSimulatorFileName(workflowName);
      const currentName = opcSimulatorFileNameRef.current;
      if (result.files.includes(currentName)) return result.files;
      if (result.files.includes(defaultName)) {
        opcSimulatorFileNameRef.current = defaultName;
        setOpcSimulatorFileName(defaultName);
        return result.files;
      }
      if (result.files.length && !opcSimulatorProfileRef.current) {
        opcSimulatorFileNameRef.current = result.files[0];
        setOpcSimulatorFileName(result.files[0]);
      }
      return result.files;
    } catch {
      return [];
    }
  }, [workflowName]);

  const openOpcSimulatorWorkbench = useCallback(async () => {
    const files = await refreshOpcSimulatorProfileFiles();
    const fileName = opcSimulatorFileNameRef.current || defaultOpcSimulatorFileName(workflowName);
    if (opcSimulatorProfileRef.current && opcSimulatorDirty) {
      opcSimulatorFileNameRef.current = fileName;
      setOpcSimulatorFileName(fileName);
      setShowOpcSimulatorDialog(true);
      return;
    }
    if (!files.length && !opcSimulatorProfileRef.current) {
      setOpcSimulatorMessage(`请先将 JSON 放入 ${opcSimulatorConfigDir}，再打开配置工作台`);
      return;
    }
    const targetName = files.includes(fileName) ? fileName : files[0] || fileName;
    const loaded = await loadOpcSimulatorProfile(targetName, {
      openDialog: true,
      successMessage: '',
    });
    if (loaded) return;
    if (opcSimulatorProfileRef.current) {
      setShowOpcSimulatorDialog(true);
      return;
    }
    setOpcSimulatorMessage(`未找到已保存配置，请检查 ${opcSimulatorConfigDir}`);
  }, [
    loadOpcSimulatorProfile,
    opcSimulatorConfigDir,
    opcSimulatorDirty,
    refreshOpcSimulatorProfileFiles,
    workflowName,
  ]);

  const handleOpcConfigFileChange = useCallback(async (fileName: string) => {
    if (!fileName || fileName === opcSimulatorFileNameRef.current) return;
    if (
      opcSimulatorDirty
      && !window.confirm('当前修改未保存，切换配置将丢弃修改。继续？')
    ) {
      return;
    }
    await loadOpcSimulatorProfile(fileName);
  }, [loadOpcSimulatorProfile, opcSimulatorDirty]);

  const openOpcSimulatorReferenceTemplate = useCallback(async () => {
    setOpcSimulatorBusy(true);
    setOpcSimulatorMessage('');
    try {
      const result = await opcSimulatorClientRef.current.loadReferenceTemplate();
      setOpcSimulatorReferenceProfile(result.profile);
      setShowOpcSimulatorReferenceDialog(true);
    } catch (error) {
      setOpcSimulatorMessage(error instanceof Error ? error.message : '载入 OPC 配置模板失败');
    } finally {
      setOpcSimulatorBusy(false);
    }
  }, []);

  const openOpcSimulatorProfileSpec = useCallback(async () => {
    setOpcSimulatorBusy(true);
    setOpcSimulatorMessage('');
    try {
      const result = await opcSimulatorClientRef.current.loadProfileSpec();
      setOpcSimulatorSpecMarkdown(result.markdown);
      setShowOpcSimulatorSpecDialog(true);
    } catch (error) {
      setOpcSimulatorMessage(error instanceof Error ? error.message : '载入 OPC 生成规范失败');
    } finally {
      setOpcSimulatorBusy(false);
    }
  }, []);

  const saveOpcSimulatorProfile = useCallback(async (
    status: 'draft' | 'runnable',
    download: boolean,
  ) => {
    if (!opcSimulatorProfile || opcSimulatorSaveInFlightRef.current) return;
    opcSimulatorSaveInFlightRef.current = true;
    const token = ++opcSimulatorSaveTokenRef.current;
    const snapshot = createProfileSaveSnapshot(opcSimulatorProfile, status, opcSimulatorFileName);
    setOpcSimulatorBusy(true);
    setOpcSimulatorMessage('');
    try {
      const validation = await opcSimulatorClientRef.current.validate(snapshot.profile, snapshot.fileName);
      if (token !== opcSimulatorSaveTokenRef.current) return;
      const snapshotIsCurrent = () => (
        opcSimulatorProfileRef.current !== null
        && isProfileSaveSnapshotCurrent(
          snapshot,
          opcSimulatorProfileRef.current,
          opcSimulatorFileNameRef.current,
        )
      );
      if (status === 'runnable' && validation.validation_errors.length) {
        if (!snapshotIsCurrent()) {
          setOpcSimulatorDirty(true);
          setOpcSimulatorMessage('旧版本已保存，请重新保存当前修改');
          return;
        }
        opcSimulatorProfileRef.current = validation.profile;
        setOpcSimulatorProfile(validation.profile);
        setOpcSimulatorBackendErrors(validation.validation_errors || []);
        setOpcSimulatorDirty(true);
        setOpcSimulatorMessage('后端校验未通过，请按精确路径修正');
        return;
      }
      const persistProfile = async (revision: string | null) => (
        opcSimulatorClientRef.current.save(
          validation.profile,
          snapshot.fileName,
          revision,
        )
      );
      let saved: OpcProfileApiResponse;
      try {
        saved = await persistProfile(opcSimulatorRevision);
      } catch (error) {
        if (
          error instanceof OpcSimulatorHttpError
          && error.status === 409
          && opcSimulatorRevision === null
        ) {
          saved = await persistProfile(OPC_PROFILE_FORCE_REVISION);
        } else {
          throw error;
        }
      }
      if (token !== opcSimulatorSaveTokenRef.current) return;
      if (!snapshotIsCurrent()) {
        setOpcSimulatorDirty(true);
        setOpcSimulatorMessage('旧版本已保存，请重新保存当前修改');
        return;
      }
      opcSimulatorProfileRef.current = saved.profile;
      setOpcSimulatorProfile(saved.profile);
      setOpcSimulatorBackendErrors(saved.validation_errors || []);
      setOpcSimulatorRevision(saved.revision || null);
      setOpcSimulatorDirty(false);
      setOpcSimulatorMessage(status === 'runnable' ? '可运行配置已保存' : '草稿已保存');
      void refreshOpcSimulatorProfileFiles();
      if (
        download
        && saved.profile.status === 'runnable'
        && !saved.validation_errors.length
        && saved.revision
      ) downloadText(saved.file_name, canonicalProfileJson(saved.profile));
    } catch (error) {
      if (token !== opcSimulatorSaveTokenRef.current) return;
      if (error instanceof OpcSimulatorHttpError && error.status === 409) {
        await loadOpcSimulatorProfile(snapshot.fileName, {
          successMessage: '版本冲突：已载入后端最新版本，请确认后再次保存',
        });
      } else {
        const payload = error instanceof OpcSimulatorHttpError ? error.payload : null;
        const detail = payload && typeof payload === 'object' && 'detail' in payload
          ? (payload as { detail?: unknown }).detail
          : null;
        const result = detail && typeof detail === 'object' && 'validation_errors' in detail
          ? detail as { validation_errors?: unknown; profile?: unknown }
          : null;
        if (Array.isArray(result?.validation_errors)) {
          setOpcSimulatorBackendErrors(result.validation_errors.filter((item): item is string => typeof item === 'string'));
        }
        setOpcSimulatorMessage(error instanceof Error ? error.message : '保存 OPC 模拟配置失败');
      }
    } finally {
      opcSimulatorSaveInFlightRef.current = false;
      setOpcSimulatorBusy(false);
    }
  }, [loadOpcSimulatorProfile, opcSimulatorFileName, opcSimulatorProfile, opcSimulatorRevision, refreshOpcSimulatorProfileFiles]);

  const applyOpcSimulatorStatus = useCallback((status: OpcSimulatorStatus) => {
    const ownedRunId = ownedOpcSimulatorRunIdRef.current;
    if (
      ownedRunId
      && (
        status.run_id !== ownedRunId
        || !['starting', 'running', 'stopping'].includes(status.state)
      )
    ) {
      ownedOpcSimulatorRunIdRef.current = null;
    }
    opcSimulatorStatusRef.current = status;
    setOpcSimulatorStatus(status);
  }, []);

  const refreshOpcSimulatorStatus = useCallback(async () => {
    if (opcSimulatorControlInFlightRef.current) return;
    const request = opcSimulatorStatusGateRef.current.begin();
    try {
      const status = await opcSimulatorClientRef.current.status(request.signal);
      if (
        !opcSimulatorControlInFlightRef.current
        && opcSimulatorStatusGateRef.current.isCurrent(request.generation)
      ) applyOpcSimulatorStatus(status);
    } catch (error) {
      if (
        error instanceof DOMException && error.name === 'AbortError'
        || !opcSimulatorStatusGateRef.current.isCurrent(request.generation)
      ) return;
      setOpcSimulatorMessage(error instanceof Error ? error.message : '读取模拟器状态失败');
    }
  }, [applyOpcSimulatorStatus]);

  const startOpcSimulator = useCallback(async () => {
    if (opcSimulatorControlInFlightRef.current || opcSimulatorBusy) return;
    if (taskExecutionEnvironment === 'real') {
      setOpcSimulatorMessage('真实执行环境禁止从页面启动 OPC 模拟器，请切回「模拟 OPC」环境后再操作。');
      return;
    }
    if (!opcSimulatorFileName) return;
    let profile = opcSimulatorProfileRef.current;
    let revision = opcSimulatorRevision;
    let backendErrors = opcSimulatorBackendErrors;
    let dirty = opcSimulatorDirty;
    if (
      !profile
      || !revision
      || dirty
      || opcSimulatorFileNameRef.current !== opcSimulatorFileName
    ) {
      const loaded = await loadOpcSimulatorProfile(opcSimulatorFileName, {
        successMessage: '',
      });
      if (!loaded) return;
      profile = loaded.profile;
      revision = loaded.revision || null;
      backendErrors = loaded.validation_errors || [];
      dirty = false;
    }
    if (!profile || !revision) return;
    const localErrors = validateOpcSimulatorProfile(profile);
    if (
      !isSimulatorStartAllowed({
        profile,
        localErrors,
        backendErrors,
        fileName: opcSimulatorFileName,
        revision,
        dirty,
        managerState: opcSimulatorStatus?.state || 'idle',
        managerRestoreStatus: opcSimulatorStatus?.restore_status || 'not_started',
      })
    ) {
      setOpcSimulatorMessage(
        describeSimulatorStartBlock({
          profile,
          localErrors,
          backendErrors,
          fileName: opcSimulatorFileName,
          revision,
          dirty,
          managerState: opcSimulatorStatus?.state || 'idle',
          managerRestoreStatus: opcSimulatorStatus?.restore_status || 'not_started',
        }) || '当前无法启动 OPC 模拟器',
      );
      return;
    }
    const allowUnsafeUrl = profile.opc.url !== DEFAULT_OPC_SIMULATOR_URL;
    const restoreStatus = opcSimulatorStatus?.restore_status || 'not_started';
    const managerState = opcSimulatorStatus?.state || 'idle';
    if (
      restoreStatusNeedsConfirm(managerState, restoreStatus)
      && !window.confirm(
        `上次停止后 OPC 恢复状态为 ${restoreStatus}。请确认现场 OPC 变量已安全，再继续启动模拟器。`,
      )
    ) {
      return;
    }
    if (
      allowUnsafeUrl
      && !window.confirm(
        `风险确认：即将连接并写入非默认 OPC 地址：\n${profile.opc.url}`
        + '\n\n该地址未经系统默认授权，错误配置可能修改真实设备。确认继续？',
      )
    ) return;
    const operation = beginOpcSimulatorControlOperation(
      opcSimulatorControlInFlightRef,
      opcSimulatorControlTokenRef,
    );
    if (operation === null) return;
    opcSimulatorStatusGateRef.current.invalidate();
    let refreshAfterFailure = false;
    setOpcSimulatorBusy(true);
    try {
      const status = await opcSimulatorClientRef.current.start(
        opcSimulatorFileName,
        revision,
        allowUnsafeUrl,
      );
      if (operation !== opcSimulatorControlTokenRef.current) return;
      ownedOpcSimulatorRunIdRef.current = status.run_id;
      applyOpcSimulatorStatus(status);
      setOpcSimulatorMessage('模拟器已启动；不会自动启动 Run Schedule');
    } catch (error) {
      if (operation !== opcSimulatorControlTokenRef.current) return;
      setOpcSimulatorMessage(error instanceof Error ? error.message : '启动 OPC 模拟器失败');
      refreshAfterFailure = true;
    } finally {
      finishOpcSimulatorControlOperation(
        opcSimulatorControlInFlightRef,
        opcSimulatorControlTokenRef,
        operation,
      );
      setOpcSimulatorBusy(false);
      if (refreshAfterFailure) void refreshOpcSimulatorStatus();
    }
  }, [
    loadOpcSimulatorProfile,
    opcSimulatorBackendErrors,
    opcSimulatorBusy,
    opcSimulatorDirty,
    opcSimulatorFileName,
    opcSimulatorRevision,
    opcSimulatorStatus,
    taskExecutionEnvironment,
    applyOpcSimulatorStatus,
    refreshOpcSimulatorStatus,
  ]);

  const stopOpcSimulator = useCallback(async () => {
    if (opcSimulatorControlInFlightRef.current) return;
    if (!opcSimulatorStatus || !['starting', 'running'].includes(opcSimulatorStatus.state)) return;
    const ownedRunId = ownedOpcSimulatorRunIdRef.current;
    if (
      !ownedRunId
      && !window.confirm('当前模拟器不是由本标签页启动。确认停止其他客户端启动的运行？')
    ) return;
    const operation = beginOpcSimulatorControlOperation(
      opcSimulatorControlInFlightRef,
      opcSimulatorControlTokenRef,
    );
    if (operation === null) return;
    opcSimulatorStatusGateRef.current.invalidate();
    let refreshAfterFailure = false;
    setOpcSimulatorBusy(true);
    try {
      const status = await opcSimulatorClientRef.current.stop(ownedRunId);
      if (operation !== opcSimulatorControlTokenRef.current) return;
      applyOpcSimulatorStatus(status);
      setOpcSimulatorMessage(opcSimulatorStopMessage(status));
    } catch (error) {
      if (operation !== opcSimulatorControlTokenRef.current) return;
      setOpcSimulatorMessage(error instanceof Error ? error.message : '停止模拟器失败，恢复结果不确定');
      refreshAfterFailure = true;
    } finally {
      finishOpcSimulatorControlOperation(
        opcSimulatorControlInFlightRef,
        opcSimulatorControlTokenRef,
        operation,
      );
      setOpcSimulatorBusy(false);
      if (refreshAfterFailure) void refreshOpcSimulatorStatus();
    }
  }, [applyOpcSimulatorStatus, opcSimulatorStatus, refreshOpcSimulatorStatus]);

  useEffect(() => {
    if (workspace !== 'tasks') return;
    void refreshOpcSimulatorProfileFiles();
  }, [refreshOpcSimulatorProfileFiles, workspace]);

  useEffect(() => {
    if (workspace !== 'tasks') return;
    void refreshOpcSimulatorStatus();
    const timer = window.setInterval(refreshOpcSimulatorStatus, 1000);
    return () => {
      window.clearInterval(timer);
      opcSimulatorStatusGateRef.current.invalidate();
    };
  }, [refreshOpcSimulatorStatus, workspace]);

  useEffect(() => {
    if (workspace !== 'tasks') return;
    const defaultName = defaultOpcSimulatorFileName(workflowName);
    if (opcSimulatorProfileRef.current || opcSimulatorDirty) return;
    opcSimulatorFileNameRef.current = defaultName;
    setOpcSimulatorFileName(defaultName);
  }, [opcSimulatorDirty, workflowName, workspace]);

  useEffect(() => {
    opcSimulatorStatusGateRef.current.mount();
    return () => {
      opcSimulatorControlTokenRef.current += 1;
      opcSimulatorStatusGateRef.current.unmount();
      csvVariablesGateRef.current.unmount();
      opcSimulatorGenerateGateRef.current.unmount();
      opcSimulatorGenerateInFlightRef.current = false;
    };
  }, []);

  useEffect(() => {
    const stopOwnedSimulatorOnPageHide = () => {
      const ownedRunId = ownedOpcSimulatorRunIdRef.current;
      const latestStatus = opcSimulatorStatusRef.current;
      if (
        !ownedRunId
        || !latestStatus
        || latestStatus.run_id !== ownedRunId
        || !['starting', 'running', 'stopping'].includes(latestStatus.state)
      ) return;
      ownedOpcSimulatorRunIdRef.current = null;
      void fetch('/api/opc-simulator/stop', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ expected_run_id: ownedRunId }),
        keepalive: true,
      });
    };
    window.addEventListener('pagehide', stopOwnedSimulatorOnPageHide);
    return () => window.removeEventListener('pagehide', stopOwnedSimulatorOnPageHide);
  }, []);

  const performTaskSchedulerPause = useCallback(async () => {
    const version = taskWorkspaceVersionRef.current;
    if (version === null) return;
    try {
      await taskExecutionControllerRef.current?.pauseAndDrain({
        workflowPath: taskWorkspacePath,
        expectedVersion: version,
      });
      const latest = await taskApiRef.current.getWorkspace(taskWorkspacePath);
      applyTaskWorkspace(latest);
    } catch (error) {
      setTaskServiceError(taskApiErrorMessage(error));
    }
  }, [applyTaskWorkspace, taskWorkspacePath]);

  const handleTaskSchedulerPause = useCallback(() => runTaskSchedulerTransition(
    taskSchedulerTransitionRef,
    setIsSchedulerTransitioning,
    performTaskSchedulerPause,
  ), [performTaskSchedulerPause]);

  const handleTaskSchedulerToggle = useCallback(() => runTaskSchedulerTransition(
    taskSchedulerTransitionRef,
    setIsSchedulerTransitioning,
    async () => {
      if (isSchedulerRunning || isTaskExecutionDraining) {
        await performTaskSchedulerPause();
        return;
      }
      taskExecutionControllerRef.current?.pause();
      setTaskExecutionWorkflow(null);
      setTaskExecutionStatus(createTaskExecutionStatus());
      setTaskServiceError('');
      let builtWorkflow: WorkflowJson;
      try {
        builtWorkflow = await buildWorkflow();
      } catch (error) {
        setTaskServiceError(error instanceof Error ? error.message : '构建 Task workflow 失败');
        return;
      }
      setTaskExecutionWorkflow(builtWorkflow);
      const version = taskWorkspaceVersionRef.current;
      if (version === null) {
        setTaskServiceError('Task 工作区尚未加载');
        return;
      }
      try {
        const hasRunningPeer = taskInstancesRef.current.some(
          (instance) => instance.status === 'running',
        );
        const plannedWorkspace = await taskApiRef.current.plan(
          taskWorkspacePath,
          version,
          false,
          hasRunningPeer,
        );
        applyTaskWorkspace(plannedWorkspace);
        const advancedWorkspace = await taskApiRef.current.advance(
          taskWorkspacePath,
          plannedWorkspace.version,
        );
        applyTaskWorkspace(advancedWorkspace);
        setTaskLogSession({
          startedAt: Date.now(),
          actionAfterSeq: taskLogAfterSeqRef.current,
        });
      } catch (error) {
        const message = taskApiErrorMessage(error);
        try {
          applyTaskWorkspace(await taskApiRef.current.getWorkspace(taskWorkspacePath));
        } catch {
          // 保留固定 workflow；服务端可能已启动，后续刷新仍可恢复执行循环。
        }
        setTaskServiceError(message);
      }
    },
  ), [
    applyTaskWorkspace,
    buildWorkflow,
    isSchedulerRunning,
    isTaskExecutionDraining,
    performTaskSchedulerPause,
    taskWorkspacePath,
  ]);

  const shouldRunTaskExecutionLoop = (
    isSchedulerRunning
    || isTaskExecutionDraining
    || hasActiveServerExecution
  );
  useEffect(() => {
    if (!shouldRunTaskExecutionLoop || (!isTaskExecutionDraining && !hasActiveServerExecution && !taskExecutionWorkflow)) return;
    const controller = taskExecutionControllerRef.current;
    if (!controller) return;
    if (!controller.isRunning()) controller.start();
    const runCycle = () => controller.run({
      workflowPath: taskWorkspacePath,
      workflow: (taskExecutionWorkflow ?? undefined) as Record<string, unknown> | undefined,
      expectedVersion: taskWorkspaceVersionRef.current ?? 0,
    });
    void runCycle();
    const timer = window.setInterval(runCycle, TASK_EXECUTION_POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [
    hasActiveServerExecution,
    isTaskExecutionDraining,
    shouldRunTaskExecutionLoop,
    taskExecutionWorkflow,
    taskWorkspacePath,
  ]);

  useEffect(() => () => taskExecutionControllerRef.current?.pause(), []);

  useEffect(() => {
    if (workspace !== 'tasks' && (isSchedulerRunning || isTaskExecutionDraining)) {
      void handleTaskSchedulerPause();
    }
  }, [handleTaskSchedulerPause, isSchedulerRunning, isTaskExecutionDraining, workspace]);

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
    const canvasWidth = canvasWorkspaceRef.current?.clientWidth ?? 0;
    setNodes((current) => expandLayoutToWidth(layoutFlowGraph(current, edges), canvasWidth));
    bumpViewportFit();
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
      const canvasWidth = canvasWorkspaceRef.current?.clientWidth ?? 0;
      const laidOutNodes = expandLayoutToWidth(imported.nodes, canvasWidth);
      setWorkflowName(imported.name);
      setTaskWorkspacePath(file.name);
      setNodes(laidOutNodes);
      setEdges(imported.edges.map((edge) => ({ ...edge, animated: true })));
      bumpViewportFit();
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
        body: JSON.stringify({
          workflow: builtWorkflow,
          task_workspace_path: taskWorkspacePath,
          ...config,
        }),
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
        {workspace === 'workflow' ? (
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
        ) : (
          <TaskSchedulerHeaderActions
            environment={taskExecutionEnvironment}
            isRunning={isSchedulerRunning}
            isTransitioning={isSchedulerTransitioning}
            onConnectOpc={() => void connectTaskOpc()}
            onEnvironmentChange={(environment) => {
              setTaskExecutionEnvironment(environment);
              if (environment === 'real') setIsOpcSimulatorDrawerOpen(false);
            }}
            onOpenSimulator={() => {
              if (taskExecutionEnvironment === 'real') return;
              if (!opcSimulatorProfile && scheduledOpcTemplateIds.length) {
                void generateOpcSimulatorProfile();
                return;
              }
              void openOpcSimulatorWorkbench();
            }}
            onOpcUrlChange={setTaskOpcUrl}
            onStartSimulator={() => {
              if (taskExecutionEnvironment === 'real') return;
              if (!opcSimulatorProfile || opcSimulatorDirty || !opcSimulatorRevision) {
                void openOpcSimulatorWorkbench();
                return;
              }
              void startOpcSimulator();
            }}
            onStopSimulator={() => void stopOpcSimulator()}
            onToggleRun={handleTaskSchedulerToggle}
            opcConnected={Boolean(taskOpcStatus?.connected)}
            opcMessage={taskOpcMessage}
            opcUrl={taskOpcUrl}
            simulatorRunning={opcSimulatorStatus?.state === 'running'}
          />
        )}
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
                fitView
                fitViewOptions={FLOW_FIT_VIEW_OPTIONS}
              >
                <FlowViewportFitter enabled={canvasTab === 'workflow' && nodes.length > 0} fitKey={viewportFitKey} />
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
                    disabled={!selectedTaskNodes.length || isTaskTemplateCreating}
                    onClick={() => {
                      createTaskTemplateFromSelection();
                      closeCanvasContextMenu();
                    }}
                    ref={selectedTaskNodes.length ? contextMenuFirstActionRef : undefined}
                    type="button"
                  >
                    {isTaskTemplateCreating ? '创建中…' : '设为 Task 模板'}
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
              <div className="live-status-slot">
                <LiveStatusPanel statuses={runStatus?.live_statuses} nodes={nodes} />
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
        <TaskSchedulerBench
          environment={taskExecutionEnvironment}
          events={taskEvents}
          isRunning={isSchedulerRunning}
          isTransitioning={isSchedulerTransitioning}
          onAdvance={advanceTaskSchedule}
          onClear={clearTaskQueue}
          onClearTemplates={clearTaskTemplates}
          onDeleteSelectedTemplates={() => deleteTaskTemplates(scheduledTemplateIds, 'selected')}
          onDeleteTemplate={(templateId) => deleteTaskTemplates([templateId], 'single')}
          onDownloadSelectedTemplates={() => downloadTaskTemplates(scheduledTemplateIds)}
          onDownloadTemplate={(templateId) => downloadTaskTemplates([templateId])}
          clearTemplatesDisabled={
            !taskTemplates.length
            || isTaskWorkspaceLoading
            || isSchedulerRunning
            || isSchedulerTransitioning
            || isTaskExecutionDraining
            || taskMutationInFlightCount > 0
          }
          templateActionsDisabled={
            isTaskWorkspaceLoading
            || isSchedulerRunning
            || isSchedulerTransitioning
            || isTaskExecutionDraining
            || taskMutationInFlightCount > 0
          }
          onEnvironmentChange={(environment) => {
            setTaskExecutionEnvironment(environment);
            if (environment === 'real') setIsOpcSimulatorDrawerOpen(false);
          }}
          onGenerate={createTaskInstances}
          onConnectOpc={() => void connectTaskOpc()}
          onOpenSimulator={() => {
            if (taskExecutionEnvironment === 'real') return;
            if (!opcSimulatorProfile && scheduledOpcTemplateIds.length) {
              void generateOpcSimulatorProfile();
              return;
            }
            void openOpcSimulatorWorkbench();
          }}
          onSampleCountChange={(value) => setTaskSampleCount(Math.min(5, Math.max(1, Math.round(value) || 1)))}
          onSelectTask={(task) => {
            setSelectedTaskInstanceId(task.id);
            setSelectedTaskTemplateId(task.templateId);
          }}
          onUpdateTaskParameters={updateTaskInstanceParameters}
          onOpcUrlChange={setTaskOpcUrl}
          onStartSimulator={() => {
            if (taskExecutionEnvironment === 'real') return;
            if (!opcSimulatorProfile || opcSimulatorDirty || !opcSimulatorRevision) {
              void openOpcSimulatorWorkbench();
              return;
            }
            void startOpcSimulator();
          }}
          onStopSimulator={() => void stopOpcSimulator()}
          onToggleRun={handleTaskSchedulerToggle}
          onToggleTemplate={(templateId) => {
            if (scheduledTemplateIds.includes(templateId)) removeTemplateFromSchedule(templateId);
            else addTemplateToSchedule(templateId);
          }}
          sampleCount={taskSampleCount}
          scheduledTemplateIds={scheduledTemplateIds}
          selectedTaskId={selectedTaskInstanceId}
          actionNodes={nodes.map((node) => ({
            id: node.id,
            label: node.data.label,
            method: node.data.method,
            params: node.data.params,
            paramSpecs: node.data.paramSpecs,
          }))}
          tasks={taskInstances}
          templates={taskTemplates}
          simulatorRunning={opcSimulatorStatus?.state === 'running'}
          simulatorMessage={opcSimulatorMessage}
          simulatorStatus={opcSimulatorStatus}
          opcConnected={Boolean(taskOpcStatus?.connected)}
          opcMessage={taskOpcMessage}
          opcUrl={taskOpcUrl}
          processLines={selectedTaskProcessLines}
          logLines={taskLogLines}
          variableRows={selectedTaskVariableRows}
          waitingReasons={taskWaitingReasons}
        />
      )}

      {workspace === 'tasks' && (
        <main className="task-workspace legacy-task-workspace">
          <header className="task-workspace-commandbar">
            <div className="task-workspace-heading">
              <div>
                <span>Task orchestration</span>
                <h2>运行排程</h2>
              </div>
            </div>
            <div className="task-workspace-actions">
              <div className="task-execution-environment" role="group" aria-label="Task 执行环境">
                <button
                  aria-pressed={taskExecutionEnvironment === 'simulated'}
                  className={taskExecutionEnvironment === 'simulated' ? 'active' : ''}
                  disabled={isSchedulerRunning || isTaskExecutionDraining}
                  onClick={() => setTaskExecutionEnvironment('simulated')}
                  type="button"
                >模拟 OPC</button>
                <button
                  aria-pressed={taskExecutionEnvironment === 'real'}
                  className={taskExecutionEnvironment === 'real' ? 'active real' : ''}
                  disabled={isSchedulerRunning || isTaskExecutionDraining}
                  onClick={() => {
                    setTaskExecutionEnvironment('real');
                    setIsOpcSimulatorDrawerOpen(false);
                  }}
                  type="button"
                >真实执行</button>
              </div>
              <button onClick={() => setTaskUtilityDrawer('opc-connection')} type="button">
                Task OPC <i className={taskPlcStatus?.connected ? 'online' : ''} aria-hidden="true" />
              </button>
              <button
                aria-haspopup="dialog"
                disabled={taskExecutionEnvironment === 'real'}
                onClick={() => setIsOpcSimulatorDrawerOpen(true)}
                title={taskExecutionEnvironment === 'real' ? '真实执行时禁止从页面启动 OPC 模拟器' : '打开 OPC 模拟器'}
                type="button"
              >
                OPC 模拟器 <i className={opcSimulatorStatus?.state === 'running' ? 'online' : ''} aria-hidden="true" />
              </button>
            </div>
          </header>
          {taskServiceError && (
            <section className="task-service-error" role="alert">
              <strong>{taskServiceError}</strong>
              {taskServiceError === 'Task 编排服务不可用' && (
                <button onClick={() => void loadTaskWorkspace()} type="button">重试</button>
              )}
              {taskServiceError === 'invalid task workspace sidecar' && (
                <button
                  disabled={isTaskWorkspaceLoading}
                  onClick={() => void resetInvalidTaskWorkspace()}
                  type="button"
                >重置当前 Task 工作区</button>
              )}
            </section>
          )}
          <button
            aria-label="关闭 Task 工具抽屉"
            className={`task-utility-drawer-backdrop${taskUtilityDrawer ? ' open' : ''}`}
            onClick={() => setTaskUtilityDrawer(null)}
            tabIndex={taskUtilityDrawer ? 0 : -1}
            type="button"
          />
          <section
            aria-hidden={taskUtilityDrawer !== 'opc-connection'}
            aria-label="Task OPC 连接"
            aria-modal="true"
            className={`task-opc-connection-drawer task-utility-drawer${taskUtilityDrawer === 'opc-connection' ? ' open' : ''}`}
            role="dialog"
          >
            <header className="task-utility-drawer-head">
              <div>
                <span>Task connection</span>
                <h2>Task OPC 连接</h2>
                <p>配置 Task 调度器读取的 PLC 连接与变量注册。</p>
              </div>
              <button aria-label="关闭 Task OPC 连接" onClick={() => setTaskUtilityDrawer(null)} type="button">×</button>
            </header>
            <div className="task-utility-drawer-body">
              <div className="task-plc-status">
                <strong>连接配置</strong>
                <label className="task-opc-connect-field">
                  OPC UA URL
                  <input
                    aria-label="Task OPC UA URL"
                    onChange={(event) => setTaskOpcUrl(event.target.value)}
                    placeholder="opc.tcp://host:4840"
                    value={taskOpcUrl}
                  />
                </label>
                <button
                  disabled={isTaskOpcConnecting}
                  onClick={() => void connectTaskOpc()}
                  type="button"
                >{isTaskOpcConnecting ? '连接中…' : taskPlcStatus?.connected ? '重新连接' : '连接 OPC'}</button>
                <span>{taskPlcStatus?.device_id || '未注册 PLC'} · {taskPlcStatus?.connected ? '已连接' : '未连接'}</span>
                <small>{taskPlcStatus?.url || '尚未连接'} · 已注册 {taskPlcStatus?.registered_variables.length || 0} 个变量</small>
                {taskOpcMessage && <em>{taskOpcMessage}</em>}
                {stackStatus?.task_orchestration?.message && (
                  <em>{stackStatus.task_orchestration.message}</em>
                )}
              </div>
            </div>
          </section>
          <button
            aria-label="关闭 OPC 模拟器"
            className={`task-opc-drawer-backdrop${isOpcSimulatorDrawerOpen ? ' open' : ''}`}
            onClick={() => setIsOpcSimulatorDrawerOpen(false)}
            tabIndex={isOpcSimulatorDrawerOpen ? 0 : -1}
            type="button"
          />
          <section
            aria-hidden={!isOpcSimulatorDrawerOpen}
            aria-label="OPC 模拟器控制"
            aria-modal="true"
            className={`task-opc-drawer${isOpcSimulatorDrawerOpen ? ' open' : ''}`}
            role="dialog"
          >
            <header className="task-opc-drawer-head">
              <div>
                <span>Simulation control</span>
                <h2>OPC 模拟器</h2>
                <p>独立运行，不随 Task 调度自动启动。</p>
              </div>
              <button
                aria-label="关闭 OPC 模拟器"
                onClick={() => setIsOpcSimulatorDrawerOpen(false)}
                type="button"
              >×</button>
            </header>
            <div className="opc-simulator-console-head">
              <div>
                <p>SIMULATION CONTROL / INDEPENDENT</p>
                <strong>OPC 模拟器</strong>
                <span>配置与 Resource Schedule 同源；不会自动随 Run Schedule 启动。</span>
              </div>
              <div className="opc-simulator-console-actions">
                <div className="opc-simulator-profile-picker">
                  <label className="opc-simulator-profile-picker-label">
                    <span>配置文件 · {opcSimulatorConfigDir}</span>
                    <select
                      disabled={opcSimulatorBusy}
                      onChange={(event) => {
                        opcSimulatorFileNameRef.current = event.target.value;
                        setOpcSimulatorFileName(event.target.value);
                      }}
                      value={opcSimulatorFileName}
                    >
                      {!opcSimulatorProfileFiles.includes(opcSimulatorFileName) && opcSimulatorFileName && (
                        <option value={opcSimulatorFileName}>{opcSimulatorFileName}（未保存）</option>
                      )}
                      {opcSimulatorProfileFiles.map((file) => (
                        <option key={file} value={file}>{file}</option>
                      ))}
                      {!opcSimulatorProfileFiles.length && !opcSimulatorFileName && (
                        <option value="">暂无配置</option>
                      )}
                    </select>
                  </label>
                  <button
                    disabled={opcSimulatorBusy}
                    onClick={() => void openOpcSimulatorWorkbench()}
                    title="打开所选配置文件的工作台"
                    type="button"
                  >打开配置工作台</button>
                </div>
                <button
                  disabled={!scheduledOpcTemplateIds.length || opcSimulatorBusy}
                  onClick={() => void generateOpcSimulatorProfile()}
                  title={scheduledOpcTemplateIds.length ? '按已排模板生成 profile' : '请先把 Template 拖入 Resource Schedule'}
                  type="button"
                >生成 OPC 模拟配置</button>
                <button
                  disabled={opcSimulatorBusy}
                  onClick={() => void openOpcSimulatorReferenceTemplate()}
                  title="打开仓库内置六节点 runnable 示例，对照填写 channel/trigger/on_complete"
                  type="button"
                >查看配置模板</button>
                <button
                  disabled={opcSimulatorBusy}
                  onClick={() => void openOpcSimulatorProfileSpec()}
                  title="打开 schema v2 JSON 生成规范，供大模型或人工参照"
                  type="button"
                >查看生成规范</button>
                <button
                  className="opc-start-button"
                  disabled={Boolean(opcSimulatorStartBlockReason)}
                  onClick={() => void startOpcSimulator()}
                  title={opcSimulatorStartBlockReason || '启动所选配置的 OPC 模拟器'}
                  type="button"
                >启动 OPC 模拟</button>
                <button
                  className="opc-stop-button"
                  disabled={
                    opcSimulatorBusy
                    || !opcSimulatorStatus
                    || !['starting', 'running'].includes(opcSimulatorStatus.state)
                  }
                  onClick={() => void stopOpcSimulator()}
                  type="button"
                >停止并恢复 OPC</button>
              </div>
            </div>
            {!scheduledOpcTemplateIds.length && (
              <p className="opc-simulator-hint">未排程：先将 Task Template 拖入 Resource Schedule，才能生成模拟配置。</p>
            )}
            {opcSimulatorStartBlockReason && (
              <p className="opc-simulator-hint">{opcSimulatorStartBlockReason}</p>
            )}
            {opcSimulatorMessage && <p className="opc-simulator-message" role="status">{opcSimulatorMessage}</p>}
            {(configuredOpcUrl || opcSimulatorFileName) && (
              <dl className="opc-simulator-config-summary">
                <div>
                  <dt>配置文件</dt>
                  <dd>{opcSimulatorFileName || '—'}</dd>
                </div>
                <div>
                  <dt>配置 URL{opcSimulatorDirty ? '（未保存）' : ''}</dt>
                  <dd>{configuredOpcUrl || '—'}</dd>
                </div>
                <div>
                  <dt>运行 URL</dt>
                  <dd>{runningOpcUrl || '—'}</dd>
                </div>
              </dl>
            )}
            {opcSimulatorDirty && opcSimulatorRevision && (
              <p className="opc-simulator-hint">工作台有未保存修改；启动模拟器将使用磁盘上已保存的配置（含 URL）。</p>
            )}
            {opcSimulatorStatus && (
              <dl className="opc-simulator-status-grid">
                <div><dt>STATE</dt><dd>{opcSimulatorStatus.state}</dd></div>
                <div><dt>PID</dt><dd>{opcSimulatorStatus.pid ?? '—'}</dd></div>
                <div><dt>ELAPSED</dt><dd>{opcSimulatorStatus.elapsed_seconds.toFixed(1)} s</dd></div>
                <div><dt>FILE</dt><dd>{opcSimulatorStatus.file_name || '—'}</dd></div>
                <div><dt>RETURN</dt><dd>{opcSimulatorStatus.return_code ?? '—'}</dd></div>
                <div><dt>RESTORE</dt><dd>{opcSimulatorStatus.restore_status}</dd></div>
                <div><dt>LAST ERROR</dt><dd>{opcSimulatorStatus.last_error || '—'}</dd></div>
              </dl>
            )}
            {opcSimulatorStatus
              && (
                opcSimulatorStatus.restore_status === 'uncertain'
                || opcSimulatorStatus.restore_status === 'error'
              )
              && opcSimulatorStatus.state === 'stopped'
              && (
                <div className="opc-simulator-critical" role="alert">
                  上次停止后 OPC 恢复未完成（{opcSimulatorStatus.restore_status}）。确认现场安全后可再次启动；启动前会二次确认。
                </div>
              )}
            {opcSimulatorStatus?.recent_logs.length ? (
              <details className="opc-simulator-logs">
                <summary>Recent logs · {opcSimulatorStatus.recent_logs.length}</summary>
                <pre>{opcSimulatorStatus.recent_logs.join('\n')}</pre>
              </details>
            ) : null}
          </section>
          <div
            className="task-orchestration task-focus-layout task-debug-bench"
            ref={taskOrchestrationRef}
          >
            <section className="task-column task-test-config">
              <div className="task-panel-head">
                <div>
                  <h2>本次测试配置</h2>
                  <p>定义要生成的测试队列</p>
                </div>
                <div className="task-panel-head-actions">
                  <span>草稿已保存</span>
                </div>
              </div>
              <div className="task-config-section">
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
                <button onClick={createTaskInstances} disabled={!scheduledTemplateIds.length || isTaskWorkspaceLoading} type="button">生成队列</button>
              </div>
              <div className="task-config-section task-template-config">
                <span className="task-config-label">选择 Task 模板（按顺序执行）</span>
              <div className="task-template-drawer-tabs" role="tablist" aria-label="模板与排程">
                <button
                  aria-selected={taskTemplateDrawerTab === 'templates'}
                  className={taskTemplateDrawerTab === 'templates' ? 'active' : ''}
                  onClick={() => setTaskTemplateDrawerTab('templates')}
                  role="tab"
                  type="button"
                >Task 模板</button>
                <button
                  aria-selected={taskTemplateDrawerTab === 'scheduled'}
                  className={taskTemplateDrawerTab === 'scheduled' ? 'active' : ''}
                  onClick={() => setTaskTemplateDrawerTab('scheduled')}
                  role="tab"
                  type="button"
                >待排模板 · {scheduledTemplateIds.length}</button>
              </div>
              <div className={`task-template-list${taskTemplateDrawerTab === 'templates' ? ' active' : ''}`}>
                {taskTemplates.map((template, index) => {
                  const templateNodes = resolveTemplateNodes(template.nodeIds, taskNodeDescriptors)
                    .map((resolved) => (resolved.node ? nodesById.get(resolved.node.id) : null))
                    .filter(Boolean) as Node<ActionNodeData>[];
                  const deleteDisabled = !canDeleteTaskTemplate(template.id, taskInstances, {
                    schedulerBusy: isSchedulerRunning
                      || isSchedulerTransitioning
                      || isTaskExecutionDraining
                      || taskExecutionStatus.tick.active > 0
                      || taskExecutionStatus.tick.in_flight > 0
                      || taskExecutionStatus.tick.claimed > 0,
                    actionInFlight: taskMutationInFlightCount > 0,
                  });
                  return (
                    <article
                      className="task-template-card"
                      draggable={true}
                      key={template.id}
                      onDragStart={(event) => event.dataTransfer.setData('application/x-unilab-task-template', template.id)}
                    >
                      <button
                        className="task-template-delete"
                        disabled={deleteDisabled}
                        onClick={() => deleteTaskTemplate(template)}
                        aria-label={`删除 Task 模板 ${template.name}`}
                        title={deleteDisabled ? '有关联运行任务或调度操作，暂不能删除' : '删除 Task 模板'}
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
                      <button
                        className="task-template-schedule-button"
                        disabled={scheduledTemplateIds.includes(template.id)}
                        onClick={() => addTemplateToSchedule(template.id)}
                        type="button"
                      >
                        {scheduledTemplateIds.includes(template.id) ? '已加入本次排程' : '加入本次排程'}
                      </button>
                    </article>
                  );
                })}
                {!taskTemplates.length && <div className="task-empty">暂无模板。请在画布中选择节点并设为 Task 模板。</div>}
              </div>
              <div
                className={`task-template-schedule-panel${taskTemplateDrawerTab === 'scheduled' ? ' active' : ''}`}
                onDragOver={(event) => event.preventDefault()}
                onDrop={(event) => {
                  event.preventDefault();
                  const templateId = event.dataTransfer.getData('application/x-unilab-task-template');
                  addTemplateToSchedule(templateId);
                }}
              >
                <div className="task-panel-head compact">
                  <div>
                    <h2>待排模板</h2>
                    <p>按当前顺序生成每个样品的 Task 实例。</p>
                  </div>
                  <span>本次待排 {scheduledTemplateIds.length} / {taskTemplates.length}</span>
                </div>
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
                  {!scheduledTemplateIds.length && <em>在「Task 模板」页签中加入本次排程</em>}
                </div>
              </div>
              </div>
              <div className="task-config-section task-opc-environment">
                <span className="task-config-label">OPC 环境</span>
                <p>
                  {taskExecutionEnvironment === 'simulated'
                    ? '模拟器配置、条件编辑与脚本生成在右上角「OPC 模拟器」抽屉中完成。'
                    : '真实执行模式：排程页只读取 PLC 状态；设备控制写入仅由 Task Action / 设备驱动发起。'}
                </p>
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
                      <h3>工艺步骤</h3>
                    </div>
                    <div>
                      <em>{selectedTaskTemplate.nodeIds.length} steps</em>
                      <button aria-label="关闭模板详情" className="task-detail-close" onClick={() => setIsTaskDetailModalOpen(false)} type="button">×</button>
                    </div>
                  </div>
                  <label className="task-template-name-field">
                    模板名称
                    <input
                      value={taskTemplateNameDraft}
                      onChange={(event) => setTaskTemplateNameDraft(event.target.value)}
                      onBlur={commitSelectedTaskTemplateName}
                      onKeyDown={(event) => {
                        if (event.key === 'Enter') {
                          event.preventDefault();
                          commitSelectedTaskTemplateName();
                          event.currentTarget.blur();
                        }
                      }}
                    />
                  </label>
                  <div className="task-detail-section">
                    <strong>子节点</strong>
                    {selectedTaskTemplate.nodeIds.map((nodeId, index) => {
                      const resolved = resolveTemplateNodes([nodeId], taskNodeDescriptors)[0];
                      const node = resolved?.node ? nodesById.get(resolved.node.id) : null;
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
                </section>
              </div>
            </section>
            )}

            <section className="task-column task-queue-column">
              <div className="task-panel-head">
                <div>
                  <h2>Task Queue</h2>
                  <p>按样品顺序与 Action / OPC 握手条件调度；从此处选择实例查看 Action 日志。</p>
                </div>
                <div className="task-panel-head-actions">
                  <span>{taskInstances.length} 个实例</span>
                </div>
              </div>
              <div className="task-action-row">
                <button
                  onClick={clearTaskQueue}
                  disabled={!taskInstances.length || isTaskWorkspaceLoading || isSchedulerRunning || isTaskExecutionDraining}
                  title={isSchedulerRunning || isTaskExecutionDraining ? '请先暂停派发' : '删除队列中的全部 Task 实例'}
                  type="button"
                >
                  清空队列
                </button>
                <button
                  className="primary"
                  onClick={handleTaskSchedulerToggle}
                  disabled={!taskInstances.length || isTaskWorkspaceLoading || isSchedulerTransitioning}
                  type="button"
                >
                  {isSchedulerTransitioning
                    ? '切换中…'
                    : isSchedulerRunning || isTaskExecutionDraining ? '暂停派发' : '运行调度'}
                </button>
                <button onClick={advanceTaskSchedule} disabled={!taskInstances.length || isTaskWorkspaceLoading} type="button">调度一步</button>
              </div>
              <div
                aria-live="polite"
                className={`task-execution-status ${taskExecutionStatus.phase}`}
                role="status"
              >
                <strong>执行状态：{taskExecutionStatus.label}</strong>
                <span>
                  tick active {taskExecutionStatus.tick.active}
                  {' · '}in_flight {taskExecutionStatus.tick.in_flight}
                  {' · '}claimed {taskExecutionStatus.tick.claimed}
                  {' · '}completed {taskExecutionStatus.tick.completed}
                  {' · '}failed {taskExecutionStatus.tick.failed}
                </span>
              </div>
              <div className="task-queue-table-head" aria-hidden="true">
                <span>样品</span>
                <span>当前 Task</span>
                <span>状态 / 等待原因</span>
              </div>
              <div className="task-queue-list">
                {taskInstances.map((task) => {
                  const template = taskTemplates.find((item) => item.id === task.templateId);
                  const waitingReason = taskWaitingReasons[task.id];
                  const localWaitingReason = taskLocalWaitingReason(task, taskInstances, taskTemplates);
                  const state = task.status === 'completed'
                    ? 'done'
                    : task.status === 'running'
                      ? 'running'
                      : task.status === 'pending'
                        ? 'ready'
                        : task.status === 'failed'
                          ? 'failed'
                          : task.status === 'cancelled'
                            ? 'cancelled'
                            : 'blocked';
                  const statusDetail = task.status === 'failed'
                    ? '执行失败'
                    : task.status === 'cancelled'
                      ? '已取消'
                      : task.status === 'completed'
                        ? '已完成'
                        : waitingReason
                          ? taskWaitingText(waitingReason)
                          : localWaitingReason || (task.status === 'waiting' ? '正在检查前置条件' : '等待调度器派发');
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
                          : statusDetail}</span>
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
                        <button
                          aria-label={`查看 ${task.sample}/${template?.name || task.templateId} 的 Action 日志`}
                          className="task-queue-inspect"
                          onClick={() => {
                            setSelectedTaskInstanceId(task.id);
                            setSelectedTaskTemplateId(task.templateId);
                            setTaskLogTab('action');
                          }}
                          title="查看 Action 日志"
                          type="button"
                        >日志</button>
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
                  <h2>Sensor Gates</h2>
                  <p>优先显示实机传感器；实机信号不可用时可点击切换手动模拟状态。</p>
                </div>
              </div>
              <div className="task-gate-list">
                {Object.entries(DEFAULT_SENSOR_GATES).map(([gate, meta]) => {
                  const hasLiveValue = liveGateStates[gate] !== undefined;
                  const free = effectiveSensorGates[gate] ?? true;
                  return (
                    <button
                      className={`task-gate-card ${free ? 'free' : 'busy'}`}
                      disabled={hasLiveValue}
                      key={gate}
                      onClick={() => toggleSensorGate(gate)}
                      type="button"
                    >
                      <span>
                        <strong>{meta.label}</strong>
                        <small>{hasLiveValue ? '实机传感器状态' : '实机信号不可用，当前为手动模拟'}</small>
                      </span>
                      <em>{free ? 'FREE' : 'BUSY'}</em>
                    </button>
                  );
                })}
              </div>
              <div className="task-panel-head compact">
                <div>
                  <h2>运行日志</h2>
                  <p>调度、Action 与 OPC 条件的实时输出</p>
                </div>
                <span className="task-log-realtime">实时</span>
              </div>
              <div className="task-live-log-head">
                <span>● 正在跟随最新日志</span>
                <button type="button" onClick={() => setTaskLogTab('events')}>查看调度事件</button>
              </div>
              <div className="task-log-tabs" role="tablist" aria-label="运行日志分类">
                <button
                  aria-selected={taskLogTab === 'waiting'}
                  className={taskLogTab === 'waiting' ? 'active' : ''}
                  onClick={() => setTaskLogTab('waiting')}
                  role="tab"
                  type="button"
                >等待 · {taskInstances.filter((task) => isTaskWaitingStatus(task.status)).length}</button>
                <button
                  aria-selected={taskLogTab === 'events'}
                  className={taskLogTab === 'events' ? 'active' : ''}
                  onClick={() => setTaskLogTab('events')}
                  role="tab"
                  type="button"
                >事件 · {taskEvents.length}</button>
                <button
                  aria-selected={taskLogTab === 'action'}
                  className={taskLogTab === 'action' ? 'active' : ''}
                  onClick={() => setTaskLogTab('action')}
                  role="tab"
                  type="button"
                >Action 日志</button>
              </div>
              {taskLogTab === 'waiting' && <div className="task-waiting-list task-log-tab-panel" role="tabpanel">
                {taskInstances
                  .filter((task) => isTaskWaitingStatus(task.status))
                  .map((task) => {
                    const template = taskTemplates.find((item) => item.id === task.templateId);
                    const waitingReason = taskWaitingReasons[task.id];
                    const localWaitingReason = taskLocalWaitingReason(task, taskInstances, taskTemplates);
                    return (
                      <div key={task.id}>
                        <strong>{task.sample} / {template?.name || task.templateId}</strong>
                        <span>{waitingReason
                          ? `${taskWaitingText(waitingReason)}（${waitingReason.code}）`
                          : localWaitingReason || (task.status === 'waiting' ? '正在检查前置条件' : '等待调度器派发')}</span>
                      </div>
                    );
                  })}
                {!taskInstances.some((task) => isTaskWaitingStatus(task.status)) && <div className="task-empty">当前没有等待中的 Task。</div>}
              </div>}
              {taskLogTab === 'events' && <div className="task-event-list task-log-tab-panel" role="tabpanel">
                {taskEvents.map((event, index) => <div key={`${event}-${index}`}>{event}</div>)}
                {!taskEvents.length && <div className="task-empty">暂无 Task 事件。</div>}
              </div>}
              {taskLogTab === 'action' && <section className="task-action-inspector task-log-tab-panel" role="tabpanel">
                <div className="task-panel-head compact">
                  <div>
                    <h2>工艺变量与过程日志</h2>
                    <p>
                      {selectedTaskInstance
                        ? `${selectedTaskInstance.sample} / ${
                          taskTemplates.find((item) => item.id === selectedTaskInstance.templateId)?.name
                            || selectedTaskInstance.templateId
                        }`
                        : '在 Task Queue 中点击「日志」查看该 Task 的变量检查与提交过程'}
                    </p>
                  </div>
                  {taskLogError ? <span className="task-log-error">{taskLogError}</span> : null}
                </div>
                {!selectedTaskInstance && (
                  <div className="task-empty">未选择 Task 实例。</div>
                )}
                {selectedTaskInstance && selectedTaskLogSections.map((section) => {
                  const variableRows = buildTaskVariableRows(section.entries);
                  const processLines = buildTaskProcessLogLines(section.entries);
                  const nodeLabel = nodes.find((node) => node.id === section.nodeId)?.data.label
                    || section.nodeId;
                  return (
                    <div className="task-action-log-section" key={section.nodeId}>
                      <h3>{nodeLabel}</h3>
                      {variableRows.length ? (
                        <table className="task-variable-log-table">
                          <thead>
                            <tr>
                              <th>阶段</th>
                              <th>变量</th>
                              <th>期望</th>
                              <th>当前</th>
                              <th>结果</th>
                            </tr>
                          </thead>
                          <tbody>
                            {variableRows.map((row) => (
                              <tr key={row.key}>
                                <td>{row.phase}</td>
                                <td>{row.variable}</td>
                                <td>{row.expected}</td>
                                <td>{row.current}</td>
                                <td>{row.result}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      ) : (
                        <div className="task-empty">该 Action 暂无变量检查记录。</div>
                      )}
                      {processLines.length ? (
                        <div className="task-process-log-list">
                          {processLines.map((line) => (
                            <div key={`${line.seq}-${line.message}`}>
                              {new Date(line.timestamp).toLocaleTimeString()}
                              {' · '}
                              {line.message}
                            </div>
                          ))}
                        </div>
                      ) : null}
                    </div>
                  );
                })}
                {selectedTaskInstance && !selectedTaskLogSections.length && (
                  <div className="task-empty">该工艺尚未产生运行日志，派发 Action 后会在此显示。</div>
                )}
              </section>}
            </section>

            <section className="task-column task-sample-strip">
              <div className="task-panel-head compact">
                <div>
                  <h2>样品进度缩略图</h2>
                  <p>只读概览，按样品与 Task 顺序显示当前工艺状态。</p>
                </div>
                <span>{isSchedulerRunning ? '派发中' : '已暂停'}</span>
              </div>
              <div className="task-sample-schedule">
                {sampleProcessRows.map((row) => (
                  <div className="task-sample-row" key={row.sample}>
                    <strong>{row.sample}</strong>
                    <div className="task-sample-track">
                      {row.blocks.map((block) => {
                        const template = taskTemplates.find((item) => item.id === block.templateId);
                        const resolvedNodes = template
                          ? resolveTemplateNodes(template.nodeIds, taskNodeDescriptors)
                          : [];
                        const activeAction = block.actions.find((action) => action.state === 'running');
                        const activeResolvedNode = activeAction
                          ? resolvedNodes[activeAction.index]?.node
                          : null;
                        const activeNode = activeResolvedNode ? nodesById.get(activeResolvedNode.id) : null;
                        return (
                          <div
                            className={[
                              'task-sample-block',
                              block.state,
                            ].filter(Boolean).join(' ')}
                            key={block.id}
                            style={{ minWidth: taskActionProgressMinWidth(block.actionTotal) }}
                            title={`${block.templateName} · ${block.actionDone}/${block.actionTotal}`}
                          >
                            <div className="task-sample-block-head">
                              <span>{block.templateName}</span>
                              <small>
                                {activeNode ? `当前：${activeNode.data.label}` : `${block.actionDone}/${block.actionTotal}`}
                                {' · '}
                                {formatElapsedDurationMs(block.totalDurationMs)}
                              </small>
                            </div>
                            <div
                              aria-label={`${block.templateName} 动作进度`}
                              aria-valuemax={block.actionTotal}
                              aria-valuemin={0}
                              aria-valuenow={block.actionDone}
                              className="task-action-progress"
                              role="progressbar"
                            >
                              {block.actions.map((action) => {
                                const resolvedNode = resolvedNodes[action.index]?.node;
                                const node = resolvedNode ? nodesById.get(resolvedNode.id) : null;
                                const label = node?.data.label || resolvedNode?.method || action.nodeId;
                                return (
                                  <i
                                    className={`task-action-progress-segment ${action.state}`}
                                    key={`${action.nodeId}:${action.index}`}
                                    title={formatTaskActionTimingTitle(action, label)}
                                  >
                                    <span>{action.index + 1}</span>
                                    <b>{label}</b>
                                  </i>
                                );
                              })}
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                ))}
                {!sampleProcessRows.length && (
                  <div className="task-empty">创建 Task 实例后显示各 Sample 的工艺进度。</div>
                )}
              </div>
            </section>
          </div>
        </main>
      )}

      {showOpcSimulatorSpecDialog && opcSimulatorSpecMarkdown && (
        <OpcProfileSpecDialog
          busy={opcSimulatorBusy}
          markdown={opcSimulatorSpecMarkdown}
          onClose={() => setShowOpcSimulatorSpecDialog(false)}
        />
      )}

      {showOpcSimulatorReferenceDialog && opcSimulatorReferenceProfile && (
        <OpcSimulatorDialog
          backendErrors={[]}
          busy={opcSimulatorBusy}
          dirty={false}
          errors={validateOpcSimulatorProfile(opcSimulatorReferenceProfile)}
          fileName={OPC_REFERENCE_TEMPLATE_FILE}
          onClose={() => setShowOpcSimulatorReferenceDialog(false)}
          onProfileChange={() => {}}
          onReload={() => {}}
          onSave={() => {}}
          profile={opcSimulatorReferenceProfile}
          readOnly
          revision={null}
          subtitle="仓库示例 scripts/config/szlab_task_opc_simulator.json：含 robot / S07 / S06 的 channel、trigger、on_trigger、on_complete、reset_when 完整写法，可逐节点对照自己的配置工作台。"
        />
      )}

      {showOpcSimulatorDialog && opcSimulatorProfile && (
        <OpcSimulatorDialog
          backendErrors={opcSimulatorBackendErrors}
          busy={opcSimulatorBusy}
          configDir={opcSimulatorConfigDir}
          dirty={opcSimulatorDirty}
          errors={opcSimulatorLocalErrors}
          fileName={opcSimulatorFileName}
          onClose={() => {
            setShowOpcSimulatorDialog(false);
          }}
          onConfigFileChange={(fileName) => void handleOpcConfigFileChange(fileName)}
          onProfileChange={updateOpcSimulatorProfile}
          profileFiles={opcSimulatorProfileFiles}
          onReload={() => void loadOpcSimulatorProfile(opcSimulatorFileName)}
          onSave={(status, download) => void saveOpcSimulatorProfile(status, download)}
          profile={opcSimulatorProfile}
          revision={opcSimulatorRevision}
        />
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
                  const currentValue = editingNode.data.params[name];
                  return (
                    <label key={name}>
                      <span className="param-label">
                        {param.label || name}
                        {param.unit ? <em>{param.unit}</em> : null}
                      </span>
                      {param.options?.length ? (
                        <select
                          value={String(currentValue ?? '')}
                          onChange={(event) => {
                            const selected = param.options?.find(
                              (option) => String(option.value) === event.currentTarget.value,
                            );
                            updateNodeParam(editingNode.id, name, selected?.value ?? event.currentTarget.value);
                          }}
                        >
                          {param.options.map((option) => (
                            <option key={String(option.value)} value={String(option.value)}>
                              {option.value} — {option.label}
                            </option>
                          ))}
                        </select>
                      ) : (
                        <input
                          type={param.type === 'boolean' ? 'checkbox' : param.type === 'string' ? 'text' : 'number'}
                          min={param.min}
                          max={param.max}
                          checked={param.type === 'boolean' ? Boolean(currentValue) : undefined}
                          value={param.type === 'boolean' ? undefined : String(currentValue ?? '')}
                          onChange={(event) => {
                            const value = param.type === 'boolean'
                              ? event.currentTarget.checked
                              : param.type === 'string'
                                ? event.currentTarget.value
                                : Number(event.currentTarget.value);
                            updateNodeParam(editingNode.id, name, value);
                          }}
                        />
                      )}
                      <small>{param.description || `${name} 动作参数`}</small>
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
                placeholder={DEFAULT_OPC_SIMULATOR_URL}
              />
            </label>
            {config.show_csv && (
              <label>
                节点 CSV
                <input value={config.csv} onChange={(event) => setConfig({ ...config, csv: event.target.value })} />
              </label>
            )}
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
  downloadText(filename, JSON.stringify(data, null, 2));
}

function downloadText(filename: string, text: string) {
  const blob = new Blob([text], { type: 'application/json;charset=utf-8' });
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

function LiveStatusPanel({
  statuses,
  nodes,
}: {
  statuses?: Record<string, Record<string, LiveStatus>>;
  nodes: Node<ActionNodeData>[];
}) {
  if (!statuses) return null;
  const nodeLabels = new Map(nodes.map((node) => [node.id, node.data.label]));
  const entries = Object.entries(statuses).flatMap(([nodeId, nodeStatuses]) =>
    Object.entries(nodeStatuses).map(([key, status]) => ({ nodeId, key, status })),
  );
  if (!entries.length) return null;

  return (
    <div className="live-status-panel" aria-live="polite">
      {entries.map(({ nodeId, key, status }) => {
        const hasValue = typeof status.value === 'number' && Number.isFinite(status.value);
        const displayValue = hasValue ? Number(status.value).toFixed(3) : '--';
        return (
          <div className={`live-status-row ${status.state === 'error' ? 'error' : ''}`} key={`${nodeId}:${key}`}>
            <span className="live-status-pulse" aria-hidden="true" />
            <span className="live-status-label">
              {nodeLabels.get(nodeId) || nodeId} · {status.label}
            </span>
            <strong>{displayValue} {status.unit || ''}</strong>
            <span className="live-status-note">
              {status.state === 'error' ? status.message || '读取暂时失败' : '每 2 秒刷新'}
            </span>
          </div>
        );
      })}
    </div>
  );
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
