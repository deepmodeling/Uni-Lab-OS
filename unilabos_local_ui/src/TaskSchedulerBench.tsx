import React from 'react';
import './taskSchedulerBench.css';
import {
  automaticActionParameterDescription,
  isAutomaticallyManagedActionParameter,
  stripAutomaticallyManagedActionParameters,
} from './automaticActionParameters';
import type { TaskProcessLogLine, TaskVariableRow } from './taskActionLog';
import {
  canStartTaskDispatch,
  taskDispatchButtonTitle,
  taskDispatchIssueResolution,
  taskDispatchReadinessLabel,
  type TaskDispatchPreflightIssue,
  type TaskDispatchReadiness,
} from './taskDispatchPreflight';
import type { OpcSimulatorStatus } from './opcSimulatorProfile';
import {
  countTaskErrorStates,
  countTaskLogCategories,
  formatTaskLogMetadata,
  filterTaskErrorLines,
  filterTaskLogLines,
  isTaskLogRecovery,
  taskLogCategoryLabel,
  taskLogPhaseLabel,
  type TaskErrorStateFilter,
  type TaskLogCategory,
  type TaskLogLine,
} from './taskLogSession';
import {
  buildSampleProcessRows,
  buildExecutionTimingSummaries,
  compactTaskProgressLabel,
  formatElapsedDurationMs,
  formatTaskActionTimingTitle,
  resolveTemplateNodes,
  sampleProcessRowStatus,
  taskActionProgressMinWidth,
} from './taskOrchestration';
import type { SampleProcessRowStatus, TaskActionExecutionRecord } from './taskOrchestration';

type Template = { id: string; name: string; nodeIds: string[] };
type Task = {
  id: string;
  sample: string;
  templateId: string;
  order: number;
  status: string;
  executionCursor?: number;
  actionRecords?: TaskActionExecutionRecord[];
  startedAt?: number;
  finishedAt?: number;
  nodeParameters: Record<string, Record<string, unknown>>;
};
type ActionNode = {
  id: string;
  deviceId?: string;
  label: string;
  method: string;
  params: Record<string, unknown>;
  paramSpecs?: Array<{
    name?: string;
    label?: string;
    description?: string;
    type?: string;
    min?: number;
    max?: number;
  }>;
};
type ResolvedActionNode = ActionNode & { templateNodeId: string };
type S09TipStatus = {
  initialized: boolean;
  tip_count?: number;
  max_use_count?: number;
  tips?: Record<string, { status: string; solvent_key?: string | null; current_box?: number; use_count?: number }>;
  solvents?: Record<string, { active_tip_index?: number | null }>;
  last_operation: null | {
    solvent_batch_id: string;
    liquid_station_index: number;
    liquid_tip_index: number;
    density_tip_index: number;
  };
};

type PowderAddition = {
  coarse_position: number | string;
  fine_position: number | string;
  target_weight: number | string;
  recipe_name: string;
};

type LiquidAddition = {
  liquid_station_index: number | string;
  solvent_batch_id: string;
  volume: number | string;
  reuse_tip: boolean;
};

const S07_POWDER_PARAMETERS = new Set([
  'coarse_position', 'fine_position', 'target_weight', 'recipe_name', 'params_json', 'powder_count', 'powder_additions',
]);
const S09_LIQUID_PARAMETERS = new Set([
  'liquid_station_index', 'solvent_batch_id', 'volume', 'reuse_tip', 'liquid_count', 'liquid_additions',
  'initialize_tip_inventory', 'initial_used_tip_count',
]);

function s09TipSummary(status: S09TipStatus | null) {
  if (!status?.initialized) return ['TIP 库存尚未初始化'];
  const bindings = Object.entries(status.solvents || {}).flatMap(([solventKey, binding]) => {
    const index = binding.active_tip_index;
    const tip = index == null ? undefined : status.tips?.[String(index)];
    return index == null ? [] : [`${solventKey} → TIP ${index}（盒${tip?.current_box ?? '-'}，已用 ${tip?.use_count ?? 0} 次）`];
  });
  const nextTip = Object.entries(status.tips || {}).find(([, tip]) => tip.status === 'unused')?.[0];
  return [
    ...(bindings.length ? bindings : ['当前暂无溶剂与 TIP 绑定']),
    `下一支可用新 TIP：${nextTip ? `TIP ${nextTip}` : '无'}`,
  ];
}

function powderAdditionsFromValues(values: Record<string, unknown>): PowderAddition[] {
  if (Array.isArray(values.powder_additions) && values.powder_additions.length) {
    return values.powder_additions as PowderAddition[];
  }
  const additions: PowderAddition[] = [{
    coarse_position: Number(values.coarse_position ?? 1),
    fine_position: Number(values.fine_position ?? 2),
    target_weight: Number(values.target_weight ?? 0),
    recipe_name: String(values.recipe_name ?? 'default'),
  }];
  const count = Math.max(1, Math.floor(Number(values.powder_count) || 1));
  while (additions.length < count) {
    additions.push({ coarse_position: 1, fine_position: 2, target_weight: '', recipe_name: 'default' });
  }
  return additions;
}

function numberValue(value: unknown) {
  return value === '' ? value : Number(value);
}

function liquidAdditionsFromValues(values: Record<string, unknown>): LiquidAddition[] {
  if (Array.isArray(values.liquid_additions) && values.liquid_additions.length) {
    return values.liquid_additions as LiquidAddition[];
  }
  const additions: LiquidAddition[] = [{
    liquid_station_index: Number(values.liquid_station_index ?? 1),
    solvent_batch_id: String(values.solvent_batch_id ?? ''),
    volume: Number(values.volume ?? 1),
    reuse_tip: true,
  }];
  const count = Math.max(1, Math.floor(Number(values.liquid_count) || 1));
  while (additions.length < count) additions.push({ liquid_station_index: 1, solvent_batch_id: '', volume: '', reuse_tip: true });
  return additions;
}

type Props = {
  templates: Template[];
  tasks: Task[];
  scheduledTemplateIds: string[];
  events: string[];
  waitingReasons: Record<string, { message?: string }>;
  sampleCount: number;
  rememberedParameterCount: number;
  isRunning: boolean;
  isTransitioning: boolean;
  dispatchReadiness: TaskDispatchReadiness;
  environment: 'simulated' | 'real';
  selectedTaskId: string | null;
  onSampleCountChange: (value: number) => void;
  onToggleTemplate: (templateId: string) => void;
  onSelectAllTemplates: (selected: boolean) => void;
  onGenerate: () => void;
  onClearRememberedParameters: () => void;
  onClear: () => void;
  onResetProgress: () => void;
  resetProgressDisabled: boolean;
  resetProgressRecoveryRequired: boolean;
  onClearTemplates: () => void;
  clearTemplatesDisabled: boolean;
  templateActionsDisabled: boolean;
  onDownloadTemplate: (templateId: string) => void;
  onDeleteTemplate: (templateId: string) => void;
  onDownloadSelectedTemplates: () => void;
  onDeleteSelectedTemplates: () => void;
  onToggleRun: () => void;
  onRetryDispatchPreflight: () => void;
  onInspectDispatchIssue: (issue: TaskDispatchPreflightIssue) => void;
  onAdvance: () => void;
  onSelectTask: (task: Task) => void;
  onUpdateTaskParameters: (
    taskId: string,
    nodeParameters: Record<string, Record<string, unknown>>,
  ) => void;
  onEnvironmentChange: (environment: 'simulated' | 'real') => void;
  onOpenSimulator: () => void;
  onStartSimulator: () => void;
  onStopSimulator: () => void;
  simulatorRunning: boolean;
  simulatorMessage: string;
  simulatorStatus: OpcSimulatorStatus | null;
  opcConnected: boolean;
  opcMessage: string;
  opcUrl: string;
  onOpcUrlChange: (url: string) => void;
  onConnectOpc: () => void;
  processLines: TaskProcessLogLine[];
  variableRows: TaskVariableRow[];
  logLines: TaskLogLine[];
  logError: string;
  actionNodes: ActionNode[];
};

function stateLabel(status: string) {
  if (status === 'running') return '运行中';
  if (status === 'completed') return '已完成';
  if (status === 'failed') return '失败';
  if (status === 'cancelled') return '已取消';
  if (status === 'waiting') return '等待条件';
  return '待派发';
}

function sampleRowStatusLabel(status: SampleProcessRowStatus) {
  if (status === 'current') return '当前';
  if (status === 'completed') return '已完成';
  if (status === 'failed') return '失败';
  if (status === 'cancelled') return '已取消';
  return '排队';
}

function taskLogContext(
  line: TaskLogLine,
  taskById: Map<string, Task>,
  templateById: Map<string, Template>,
  actionById: Map<string, ActionNode>,
  actionByTemplateNodeId: Map<string, ActionNode>,
) {
  const task = line.instanceId ? taskById.get(line.instanceId) : undefined;
  const templateId = line.templateId || task?.templateId;
  const template = templateId ? templateById.get(templateId) : undefined;
  const action = line.nodeId
    ? actionById.get(line.nodeId)
      || (templateId ? actionByTemplateNodeId.get(`${templateId}:${line.nodeId}`) : undefined)
    : undefined;
  if (!line.sampleId && !task && !templateId && !line.nodeId && !line.executionId) return null;
  return {
    sampleLabel: line.sampleId || task?.sample,
    taskLabel: template?.name || templateId,
    actionLabel: action?.label || action?.method || line.nodeId,
  };
}

type HeaderActionsProps = Pick<
  Props,
  | 'environment'
  | 'isRunning'
  | 'isTransitioning'
  | 'dispatchReadiness'
  | 'onEnvironmentChange'
  | 'onOpenSimulator'
  | 'onStartSimulator'
  | 'onStopSimulator'
  | 'simulatorRunning'
  | 'opcConnected'
  | 'opcMessage'
  | 'opcUrl'
  | 'onOpcUrlChange'
  | 'onConnectOpc'
  | 'onToggleRun'
>;

export function TaskSchedulerHeaderActions(props: HeaderActionsProps) {
  const [isOpcConnectionOpen, setIsOpcConnectionOpen] = React.useState(false);
  const dispatchReady = canStartTaskDispatch(props.dispatchReadiness);
  const dispatchButtonState = props.isRunning
    ? 'active'
    : dispatchReady
      ? 'ready'
      : 'blocked';

  return (
    <>
      <div className="scheduler-header-bar demo-tool-header__task-actions" role="toolbar" aria-label="Task 排程控制">
        <div className="scheduler-header-bar__group">
          <div className="scheduler-bench__environment" role="group" aria-label="执行环境">
            <button className={props.environment === 'simulated' ? 'active' : ''} onClick={() => props.onEnvironmentChange('simulated')} type="button">模拟 OPC</button>
            <button className={props.environment === 'real' ? 'active' : ''} onClick={() => props.onEnvironmentChange('real')} type="button">真实执行</button>
          </div>
        </div>
        <div className="scheduler-header-bar__divider" aria-hidden="true" />
        <div className="scheduler-header-bar__group">
          <button className="scheduler-btn scheduler-btn--ghost" disabled={props.environment === 'real'} onClick={props.onOpenSimulator} type="button">OPC 模拟器</button>
          <button
            className="scheduler-btn scheduler-btn--accent"
            disabled={props.environment === 'real'}
            onClick={props.simulatorRunning ? props.onStopSimulator : props.onStartSimulator}
            type="button"
          >{props.simulatorRunning ? '停止并恢复' : '启动模拟'}</button>
        </div>
        <div className="scheduler-header-bar__divider" aria-hidden="true" />
        <div className="scheduler-header-bar__group">
          <button className="scheduler-btn scheduler-btn--ghost scheduler-btn--status" onClick={() => setIsOpcConnectionOpen(true)} type="button">
            <i className={props.opcConnected ? 'online' : ''} aria-hidden="true" />
            {props.opcConnected ? 'OPC 已连接' : '配置 OPC 连接'}
          </button>
          <button
            aria-describedby="task-dispatch-readiness"
            className={`scheduler-btn scheduler-btn--dispatch scheduler-btn--dispatch-${dispatchButtonState}`}
            data-dispatch-state={dispatchButtonState}
            disabled={props.isTransitioning || (!props.isRunning && !dispatchReady)}
            onClick={props.onToggleRun}
            title={props.isRunning ? '暂停后续 Task 派发' : taskDispatchButtonTitle(props.dispatchReadiness)}
            type="button"
          >
            {props.isRunning ? '暂停派发' : '开始派发'}
          </button>
        </div>
      </div>
      {isOpcConnectionOpen && (
        <div className="scheduler-bench__modal-backdrop" onMouseDown={() => setIsOpcConnectionOpen(false)}>
          <section aria-label="Task OPC 连接" className="scheduler-bench__modal" onMouseDown={(event) => event.stopPropagation()} role="dialog">
            <header><div><span>Task connection</span><h2>Task OPC 连接</h2><p>选择排程条件检查使用的 OPC UA 地址。</p></div><button onClick={() => setIsOpcConnectionOpen(false)} type="button">×</button></header>
            <label>OPC UA URL<input onChange={(event) => props.onOpcUrlChange(event.target.value)} placeholder="opc.tcp://host:4840" value={props.opcUrl} /></label>
            <div className="scheduler-bench__modal-actions"><button className="scheduler-btn scheduler-btn--primary" onClick={props.onConnectOpc} type="button">{props.opcConnected ? '重新连接' : '连接 OPC'}</button><span>{props.opcMessage || (props.opcConnected ? '已连接，可用于排程条件检查。' : '未连接')}</span></div>
          </section>
        </div>
      )}
    </>
  );
}

export function TaskSchedulerBench(props: Props) {
  const [editingTask, setEditingTask] = React.useState<Task | null>(null);
  const [parameterDraft, setParameterDraft] = React.useState<Record<string, Record<string, unknown>>>({});
  const [s09TipStatus, setS09TipStatus] = React.useState<S09TipStatus | null>(null);
  const [logFilter, setLogFilter] = React.useState<TaskLogCategory>('all');
  const [errorStateFilter, setErrorStateFilter] = React.useState<TaskErrorStateFilter>('all');
  const [isFollowingLogs, setIsFollowingLogs] = React.useState(true);
  const [timingNowMs, setTimingNowMs] = React.useState(() => Date.now());
  const logContainerRef = React.useRef<HTMLDivElement>(null);
  const selected = props.tasks.find((task) => task.id === props.selectedTaskId) || props.tasks[0];
  const selectedTemplate = props.templates.find((template) => template.id === selected?.templateId);
  const taskById = React.useMemo(
    () => new Map(props.tasks.map((task) => [task.id, task])),
    [props.tasks],
  );
  const templateById = React.useMemo(
    () => new Map(props.templates.map((template) => [template.id, template])),
    [props.templates],
  );
  const actionById = React.useMemo(
    () => new Map(props.actionNodes.map((action) => [action.id, action])),
    [props.actionNodes],
  );
  const actionByTemplateNodeId = React.useMemo(() => {
    const resolvedActions = new Map<string, ActionNode>();
    for (const template of props.templates) {
      for (const resolved of resolveTemplateNodes(template.nodeIds, props.actionNodes)) {
        if (resolved.node) {
          resolvedActions.set(`${template.id}:${resolved.templateNodeId}`, resolved.node);
        }
      }
    }
    return resolvedActions;
  }, [props.actionNodes, props.templates]);
  const hasRunningTasks = props.tasks.some((task) => task.status === 'running');
  const sampleProcessRows = React.useMemo(
    () => buildSampleProcessRows(props.tasks, props.templates, timingNowMs),
    [props.tasks, props.templates, timingNowMs],
  );
  const timingSummaries = React.useMemo(
    () => buildExecutionTimingSummaries(
      props.tasks,
      props.templates,
      props.actionNodes,
      timingNowMs,
    ),
    [props.tasks, props.templates, props.actionNodes, timingNowMs],
  );
  const visibleLogLines = logFilter === 'error'
    ? filterTaskErrorLines(props.logLines, errorStateFilter)
    : filterTaskLogLines(props.logLines, logFilter);
  const errorStateCounts = countTaskErrorStates(props.logLines);
  const logCategoryCounts = countTaskLogCategories(props.logLines);
  const editingTemplate = props.templates.find((template) => template.id === editingTask?.templateId);
  const editingNodes: ResolvedActionNode[] = editingTemplate
    ? resolveTemplateNodes(editingTemplate.nodeIds, props.actionNodes)
      .filter((entry): entry is { templateNodeId: string; node: ActionNode } => Boolean(entry.node))
      .map((entry) => ({ ...entry.node, templateNodeId: entry.templateNodeId }))
    : [];
  const parametersEditable = editingTask?.status === 'waiting' || editingTask?.status === 'pending';
  const editingS09Parameters = editingNodes.some((node) => node.method === 'add_liquid_with_reusable_tip');
  const allTemplatesSelected = props.templates.length > 0
    && props.templates.every((template) => props.scheduledTemplateIds.includes(template.id));
  const dispatchReady = canStartTaskDispatch(props.dispatchReadiness);
  const dispatchButtonState = props.isRunning
    ? 'active'
    : dispatchReady
      ? 'ready'
      : 'blocked';
  const dispatchButtonTitle = props.isRunning
    ? '暂停后续 Task 派发'
    : taskDispatchButtonTitle(props.dispatchReadiness);
  const dispatchReadinessDescription = props.dispatchReadiness.message || (
    props.dispatchReadiness.status === 'validating'
      ? '正在核对 Task 模板、可执行流程、设备和动作。'
      : props.dispatchReadiness.status === 'ready'
        ? '当前 Task 与可执行 workflow 匹配。'
        : props.dispatchReadiness.status === 'invalid'
          ? '请修复以下阻断项后重新派发。'
          : props.dispatchReadiness.status === 'unavailable'
            ? '暂时无法确认当前流程是否可以安全派发。'
            : '流程或 Task 内容变化后会自动重新验证。'
  );

  React.useEffect(() => {
    if (isFollowingLogs && logContainerRef.current) {
      logContainerRef.current.scrollTop = logContainerRef.current.scrollHeight;
    }
  }, [isFollowingLogs, visibleLogLines.length]);

  React.useEffect(() => {
    setTimingNowMs(Date.now());
    if (!hasRunningTasks) return undefined;
    const timer = window.setInterval(() => setTimingNowMs(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [hasRunningTasks]);

  React.useEffect(() => {
    if (!editingTask || !editingS09Parameters) return;
    fetch('/api/s09-tip-status')
      .then((response) => response.json())
      .then((payload: S09TipStatus) => setS09TipStatus(payload))
      .catch(() => setS09TipStatus(null));
  }, [editingTask?.id, editingS09Parameters]);

  const exportLogs = () => {
    const text = visibleLogLines.map((line) => {
      const context = taskLogContext(
        line,
        taskById,
        templateById,
        actionById,
        actionByTemplateNodeId,
      );
      const contextText = [
        context?.sampleLabel ? `[样品 ${context.sampleLabel}; sample_id=${line.sampleId || ''}]` : '',
        context?.taskLabel ? `[Task ${context.taskLabel}; instance_id=${line.instanceId || ''}]` : '',
        context?.actionLabel
          ? `[Action ${context.actionLabel}; node_id=${line.nodeId || ''}; execution_id=${line.executionId || ''}]`
          : '',
      ].filter(Boolean).join(' ');
      const sequence = line.seq === undefined ? '' : ` #${line.seq}`;
      return `${new Date(line.timestamp).toLocaleString('zh-CN', { hour12: false })}${sequence} ${formatTaskLogMetadata(line)}${contextText ? ` ${contextText}` : ''} ${line.message}`;
    }).join('\n');
    void navigator.clipboard?.writeText(text).catch(() => undefined);
    const link = document.createElement('a');
    link.href = URL.createObjectURL(new Blob([text], { type: 'text/plain;charset=utf-8' }));
    link.download = `task-log-${new Date().toISOString().replace(/:/g, '-')}.txt`;
    link.click();
    URL.revokeObjectURL(link.href);
  };

  const openParameterEditor = (task: Task) => {
    setEditingTask(task);
    setParameterDraft(Object.fromEntries(
      Object.entries(task.nodeParameters).map(([nodeId, parameters]) => [
        nodeId,
        stripAutomaticallyManagedActionParameters(undefined, parameters, nodeId),
      ]),
    ));
  };

  const updateParameter = (nodeId: string, parameter: string, value: unknown) => {
    setParameterDraft((current) => ({
      ...current,
      [nodeId]: { ...current[nodeId], [parameter]: value },
    }));
  };

  const updatePowderAdditions = (nodeId: string, additions: PowderAddition[]) => {
    setParameterDraft((current) => ({
      ...current,
      [nodeId]: {
        ...current[nodeId],
        powder_count: additions.length,
        powder_additions: additions,
      },
    }));
  };

  const updateLiquidAdditions = (nodeId: string, additions: LiquidAddition[]) => {
    setParameterDraft((current) => ({
      ...current,
      [nodeId]: { ...current[nodeId], liquid_count: additions.length, liquid_additions: additions },
    }));
  };

  return (
    <main className="scheduler-bench">
      {props.simulatorMessage && <p className="scheduler-bench__simulator-message" role="status">{props.simulatorMessage}</p>}

      <section className="scheduler-bench__layout">
        <aside className="scheduler-bench__panel scheduler-bench__config">
          <PanelHead title="本次测试配置" sub="定义要生成的测试队列" badge="草稿已保存" />
          <div className="scheduler-bench__section">
            <label>样品数</label>
            <div className="scheduler-bench__sample-input"><input min="1" max="999" type="number" value={props.sampleCount} onChange={(event) => props.onSampleCountChange(Number(event.target.value))} /><button className="scheduler-btn scheduler-btn--primary" onClick={props.onGenerate} type="button">生成队列</button></div>
            <div className="scheduler-bench__parameter-memory">
              <span>
                {props.rememberedParameterCount
                  ? `生成时将按样品沿用 ${props.rememberedParameterCount} 组最近实例入参`
                  : '保存实例入参后，下次生成队列会自动沿用'}
              </span>
              <button
                disabled={!props.rememberedParameterCount}
                onClick={props.onClearRememberedParameters}
                type="button"
              >清除记忆</button>
            </div>
          </div>
          <div className="scheduler-bench__section">
            <div className="scheduler-bench__template-head">
              <label>选择 Task 模板（按顺序执行）</label>
              <span>已选 {props.scheduledTemplateIds.length}</span>
            </div>
            <div className="scheduler-bench__template-batch-actions">
              <button
                disabled={!props.templates.length}
                onClick={() => props.onSelectAllTemplates(!allTemplatesSelected)}
                type="button"
              >{allTemplatesSelected ? '取消全选' : '全选'}</button>
              <button disabled={!props.scheduledTemplateIds.length} onClick={props.onDownloadSelectedTemplates} type="button">下载已选</button>
              <button
                className="danger"
                disabled={!props.scheduledTemplateIds.length || props.templateActionsDisabled}
                onClick={props.onDeleteSelectedTemplates}
                type="button"
              >删除已选</button>
              <button
                className="danger"
                disabled={props.clearTemplatesDisabled}
                onClick={props.onClearTemplates}
                title={props.clearTemplatesDisabled ? '暂无可清空模板或当前正在执行调度操作' : '清空全部历史 Task 模板'}
                type="button"
              >清空全部</button>
            </div>
            {props.templates.map((template, index) => {
              const checked = props.scheduledTemplateIds.includes(template.id);
              return (
                <div className="scheduler-bench__template" key={template.id}>
                  <label className="scheduler-bench__template-choice">
                    <input checked={checked} onChange={() => props.onToggleTemplate(template.id)} type="checkbox" />
                    <span><strong>{index + 1}. {template.name}</strong><small>{template.nodeIds.length} 个工艺节点</small></span>
                  </label>
                  <div className="scheduler-bench__template-actions">
                    <button aria-label={`下载 Task 模板 ${template.name}`} onClick={() => props.onDownloadTemplate(template.id)} title="下载 JSON" type="button">下载</button>
                    <button
                      aria-label={`删除 Task 模板 ${template.name}`}
                      className="danger"
                      disabled={props.templateActionsDisabled}
                      onClick={() => props.onDeleteTemplate(template.id)}
                      title="删除模板"
                      type="button"
                    >删除</button>
                  </div>
                </div>
              );
            })}
            {!props.templates.length && <p className="scheduler-bench__empty">请先在流程设计中创建 Task 模板。</p>}
            <p className="scheduler-bench__hint">仅用于测试顺序：一次只派发一个可执行 Task，不做资源优化。</p>
          </div>
          <div className="scheduler-bench__section">
            <label>OPC 环境</label>
            {props.environment === 'simulated' ? (
              <>
                <p className="scheduler-bench__hint">
                  模拟器配置、条件编辑与脚本生成在
                  <button className="scheduler-bench__hint-link" onClick={props.onOpenSimulator} type="button">「OPC 模拟器」</button>
                  中完成。
                </p>
                <dl className="scheduler-bench__simulator-status" aria-label="OPC 模拟器状态">
                  <div><dt>状态</dt><dd>{props.simulatorStatus?.state || '读取中'}</dd></div>
                  <div><dt>运行 URL</dt><dd title={props.simulatorStatus?.opc_url || ''}>{props.simulatorStatus?.opc_url || '—'}</dd></div>
                  <div><dt>运行时长</dt><dd>{props.simulatorStatus ? `${props.simulatorStatus.elapsed_seconds.toFixed(1)} s` : '—'}</dd></div>
                  <div><dt>恢复状态</dt><dd>{props.simulatorStatus?.restore_status || '—'}</dd></div>
                  {props.simulatorStatus?.last_error && <div className="error"><dt>错误</dt><dd>{props.simulatorStatus.last_error}</dd></div>}
                </dl>
                {props.simulatorStatus?.recent_logs.length ? (
                  <details className="scheduler-bench__simulator-logs" open={Boolean(props.simulatorStatus.last_error)}>
                    <summary>模拟器日志 · 最近 {props.simulatorStatus.recent_logs.length} 条</summary>
                    <pre>{props.simulatorStatus.recent_logs.join('\n')}</pre>
                  </details>
                ) : null}
              </>
            ) : (
              <p className="scheduler-bench__hint">真实执行时仅由 Task Action / 设备驱动执行控制写入。</p>
            )}
          </div>
        </aside>

        <section className="scheduler-bench__middle">
          <section className="scheduler-bench__panel">
            <PanelHead title="Task 测试队列" sub="点击一行查看其 OPC 条件和过程日志" badge="顺序派发" />
            <div className="scheduler-bench__queue-actions">
              <span>共 {props.tasks.length} 个 Task · {props.isRunning ? '正在派发' : '当前未派发'}</span>
              <div className="scheduler-bench__queue-buttons">
                <button
                  aria-describedby="task-dispatch-readiness"
                  className="scheduler-btn scheduler-btn--ghost"
                  disabled={props.isRunning || props.isTransitioning || !dispatchReady}
                  onClick={props.onAdvance}
                  title={dispatchReady ? '仅派发下一个可执行 Task' : taskDispatchButtonTitle(props.dispatchReadiness)}
                  type="button"
                >调度一步</button>
                <button
                  aria-describedby="task-dispatch-readiness"
                  className={`scheduler-btn scheduler-btn--dispatch scheduler-btn--dispatch-${dispatchButtonState}`}
                  data-dispatch-state={dispatchButtonState}
                  disabled={props.isTransitioning || (!props.isRunning && !dispatchReady)}
                  onClick={props.onToggleRun}
                  title={dispatchButtonTitle}
                  type="button"
                >{props.isRunning ? '暂停后续派发' : '开始派发'}</button>
                <button
                  className="scheduler-btn scheduler-btn--ghost"
                  disabled={props.resetProgressDisabled}
                  onClick={props.onResetProgress}
                  title={
                    props.resetProgressDisabled
                      ? '请先暂停派发'
                      : props.resetProgressRecoveryRequired
                        ? '确认真机动作已停止且 Task OPC 已断开后，清除残留执行并重置'
                        : '保留 Task、顺序和参数，仅清除执行进度'
                  }
                  type="button"
                >
                  {props.resetProgressRecoveryRequired ? '清除残留并复用' : '重置并复用'}
                </button>
                <button className="scheduler-btn scheduler-btn--danger" onClick={props.onClear} type="button">清空队列</button>
              </div>
            </div>
            <div
              aria-live="polite"
              className={`scheduler-bench__dispatch-readiness ${props.dispatchReadiness.status}`}
              id="task-dispatch-readiness"
              role={props.dispatchReadiness.status === 'invalid' ? 'alert' : 'status'}
            >
              <div>
                <i aria-hidden="true" />
                <span>
                  <strong>{taskDispatchReadinessLabel(props.dispatchReadiness)}</strong>
                  <small>{dispatchReadinessDescription}</small>
                </span>
              </div>
              {Boolean(props.dispatchReadiness.result?.errors.length) && (
                <ul>
                  {props.dispatchReadiness.result?.errors.map((issue, index) => (
                    <li key={`${issue.code}:${issue.template_id}:${issue.node_id}:${index}`}>
                      <button
                        className="scheduler-bench__dispatch-issue"
                        onClick={() => props.onInspectDispatchIssue(issue)}
                        title="定位并处理此阻断项"
                        type="button"
                      >
                        <code>{issue.code}</code>
                        <div className="scheduler-bench__dispatch-issue-content">
                          <span>{issue.message}</span>
                          <small>处理建议：{taskDispatchIssueResolution(issue)}</small>
                        </div>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
              {Boolean(props.dispatchReadiness.result?.warnings.length) && (
                <p>另有 {props.dispatchReadiness.result?.warnings.length} 项警告，不阻止派发。</p>
              )}
              {(props.dispatchReadiness.status === 'invalid'
                || props.dispatchReadiness.status === 'unavailable') && (
                <button
                  className="scheduler-btn scheduler-btn--ghost scheduler-bench__dispatch-retry"
                  disabled={props.isRunning || props.isTransitioning}
                  onClick={props.onRetryDispatchPreflight}
                  type="button"
                >重新检查</button>
              )}
            </div>
            <div className="scheduler-bench__queue-wrap">
            <table className="scheduler-bench__queue"><thead><tr><th>样品</th><th>当前 Task</th><th>状态</th><th>等待原因</th></tr></thead><tbody>{props.tasks.map((task) => {
              const template = props.templates.find((item) => item.id === task.templateId);
              const waiting = props.waitingReasons[task.id]?.message || (task.status === 'waiting' ? '正在检查前置条件' : '—');
              return <tr className={task.id === selected?.id ? 'selected' : ''} key={task.id} onClick={() => props.onSelectTask(task)} onDoubleClick={() => openParameterEditor(task)}><td><strong>{task.sample}</strong></td><td>{template?.name || task.templateId}</td><td><span className={`scheduler-bench__state ${task.status}`}>{stateLabel(task.status)}</span></td><td className="scheduler-bench__queue-waiting">{waiting}</td></tr>;
            })}</tbody></table>
            </div>
          </section>
          <section className="scheduler-bench__panel scheduler-bench__progress">
            <PanelHead title="样品进度缩略图" badge="仅显示" />
            <div className="scheduler-bench__progress-body">
              {sampleProcessRows.map((row) => {
              const rowStatus = sampleProcessRowStatus(row.blocks);
              const timing = timingSummaries.samples.get(row.sample);
              return (
                <div className="scheduler-bench__progress-row" key={row.sample}>
                <div className="scheduler-bench__progress-sample">
                  <strong>{row.sample}</strong>
                  <small>总 {formatElapsedDurationMs(timing?.totalDurationMs)}</small>
                  <small>机械臂 {formatElapsedDurationMs(timing?.robotDurationMs)}</small>
                  <small>动作 {formatElapsedDurationMs(timing?.actionDurationMs)}</small>
                </div>
                <div className="scheduler-bench__progress-track">
                  {row.blocks.map((block) => {
                    const template = props.templates.find((item) => item.id === block.templateId);
                    const resolvedNodes = template
                      ? resolveTemplateNodes(template.nodeIds, props.actionNodes)
                      : [];
                    const activeAction = block.actions.find((action) => action.state === 'running');
                    const activeNode = activeAction ? resolvedNodes[activeAction.index]?.node : null;
                    const compactTemplateName = compactTaskProgressLabel(block.templateName);
                    return (
                      <div
                        className={`scheduler-bench__progress-task ${block.state}`}
                        key={block.id}
                        style={{ minWidth: taskActionProgressMinWidth(block.actionTotal) }}
                        title={block.templateName}
                      >
                        <div className="scheduler-bench__progress-task-head">
                          <span>{compactTemplateName}</span>
                          <small>
                            {activeNode
                              ? `当前：${compactTaskProgressLabel(activeNode.label)}`
                              : stateLabel(block.state)}
                            {' · '}
                            {formatElapsedDurationMs(block.totalDurationMs)}
                          </small>
                        </div>
                        <div
                          aria-label={`${block.templateName} 动作进度`}
                          className="scheduler-bench__action-progress"
                          role="progressbar"
                          aria-valuemax={block.actionTotal}
                          aria-valuemin={0}
                          aria-valuenow={block.actionDone}
                        >
                          {block.actions.map((action) => {
                            const node = resolvedNodes[action.index]?.node;
                            const label = node?.label || node?.method || action.nodeId;
                            const compactLabel = compactTaskProgressLabel(label);
                            return (
                              <i
                                className={`scheduler-bench__action-segment ${action.state}`}
                                key={`${action.nodeId}:${action.index}`}
                                title={formatTaskActionTimingTitle(action, label)}
                              >
                                <span>{action.index + 1}</span>
                                <b>{compactLabel}</b>
                              </i>
                            );
                          })}
                        </div>
                      </div>
                    );
                  })}
                </div>
                  <small className={`scheduler-bench__progress-summary ${rowStatus}`}>
                    {sampleRowStatusLabel(rowStatus)}
                  </small>
                </div>
              );
              })}
              {!sampleProcessRows.length && (
                <p className="scheduler-bench__empty">生成队列后显示各样品的工艺进度。</p>
              )}
            </div>
          </section>
        </section>

        <aside className="scheduler-bench__right">
          <section className="scheduler-bench__panel">
            <PanelHead title="运行日志" sub="调度、Action 与 OPC 条件的实时输出" badge="实时" />
            <div className="scheduler-bench__log-head"><button className="scheduler-bench__follow" onClick={() => setIsFollowingLogs((value) => !value)} type="button">● {isFollowingLogs ? '正在跟随最新日志' : '已暂停自动跟随'}</button><button onClick={exportLogs} type="button">导出</button></div>
            {props.logError && <div className="scheduler-bench__log-error" role="alert">日志读取异常：{props.logError}</div>}
            <div className="scheduler-bench__filters">{([
              ['all', '全部', logCategoryCounts.all],
              ['schedule', '调度', logCategoryCounts.schedule],
              ['action', 'Action', logCategoryCounts.action],
              ['opc', 'OPC', logCategoryCounts.opc],
              ['result', '结果', logCategoryCounts.result],
              ['error', '错误', logCategoryCounts.error],
            ] as const).map(([filter, label, count]) => <button className={logFilter === filter ? 'active' : ''} data-active-errors={filter === 'error' && logCategoryCounts.activeErrors > 0 ? 'true' : undefined} key={filter} onClick={() => setLogFilter(filter)} type="button"><span>{label}</span><b>{count}</b>{filter === 'error' && logCategoryCounts.activeErrors > 0 && <em>{logCategoryCounts.activeErrors} 未恢复</em>}</button>)}</div>
            {logFilter === 'error' && <div className="scheduler-bench__filters scheduler-bench__error-filters" role="group" aria-label="错误状态筛选">{([
              ['all', '全部错误', errorStateCounts.total],
              ['active', '当前异常', errorStateCounts.active],
              ['recovered', '已恢复', errorStateCounts.recovered],
            ] as const).map(([filter, label, count]) => <button aria-pressed={errorStateFilter === filter} className={errorStateFilter === filter ? 'active' : ''} data-error-state={filter} key={filter} onClick={() => setErrorStateFilter(filter)} type="button">{label} <b>{count}</b></button>)}</div>}
            <div className="scheduler-bench__log" onScroll={(event) => {
              const element = event.currentTarget;
              setIsFollowingLogs(element.scrollHeight - element.scrollTop - element.clientHeight < 12);
            }} ref={logContainerRef}>
              {visibleLogLines.map((line) => {
                const context = taskLogContext(
                  line,
                  taskById,
                  templateById,
                  actionById,
                  actionByTemplateNodeId,
                );
                const isRecovery = isTaskLogRecovery(line);
                return <div className={`scheduler-bench__log-line ${line.category}${line.isError ? ' error' : ''}`} key={line.id}>
                  {context && <div className="scheduler-bench__log-context">
                    {context.sampleLabel && <span title={`sample_id: ${line.sampleId || ''}`}>样品 · {context.sampleLabel}</span>}
                    {context.taskLabel && <span title={`instance_id: ${line.instanceId || ''}`}>Task · {context.taskLabel}</span>}
                    {context.actionLabel && <span title={`node_id: ${line.nodeId || ''}\nexecution_id: ${line.executionId || ''}`}>Action · {context.actionLabel}</span>}
                  </div>}
                  <div className="scheduler-bench__log-content">
                    {line.seq === undefined ? null : <span className="scheduler-bench__log-sequence">#{line.seq}</span>}
                    <time>{new Date(line.timestamp).toLocaleTimeString('zh-CN', { hour12: false })}</time>
                    <span className="scheduler-bench__log-meta category" title={`category: ${line.category}`}>{taskLogCategoryLabel(line.category)}</span>
                    <span className={`scheduler-bench__log-meta level ${line.level.toLowerCase()}`}>{line.level.toUpperCase()}</span>
                    {line.code && <code className="scheduler-bench__log-meta code" title={`code: ${line.code}`}>{line.code}</code>}
                    {line.phase && !isRecovery && <span className="scheduler-bench__log-meta phase" title={`phase: ${line.phase}`}>{taskLogPhaseLabel(line.phase)}</span>}
                    {line.errorState === 'active' && <span className="scheduler-bench__log-meta active-error">当前异常</span>}
                    {line.errorState === 'recovered' && <span className="scheduler-bench__log-meta recovered" title={`恢复时间: ${new Date(line.recoveredAt || line.timestamp).toLocaleTimeString('zh-CN', { hour12: false })}`}>后来已恢复</span>}
                    {isRecovery && <span className="scheduler-bench__log-meta recovered" title={`phase: ${line.phase || 'recovered'}`}>已恢复</span>}
                    <span className="scheduler-bench__log-message">{line.message}</span>
                  </div>
                  {line.detail && Object.keys(line.detail).length ? <details>
                    <summary>详情</summary>
                    <pre>{JSON.stringify(line.detail, null, 2)}</pre>
                  </details> : null}
                </div>;
              })}
              {!visibleLogLines.length && <div>本次启动后暂无匹配日志。</div>}
            </div>
            <div className="scheduler-bench__log-foot">本次启动后 {props.logLines.length} 条{logFilter === 'all' ? '' : ` · 当前筛选 ${visibleLogLines.length} 条`} · {isFollowingLogs ? '自动滚动' : '滚动已暂停'}</div>
          </section>
          <section className="scheduler-bench__panel scheduler-bench__detail">
            <div className="scheduler-bench__detail-tabs"><button className="active" type="button">选中 Task 条件</button></div>
            {selected ? <div className="scheduler-bench__detail-body"><div><strong>{selected.sample} / {selectedTemplate?.name || selected.templateId}</strong><small>执行 ID：{selected.id}</small></div><span className={`scheduler-bench__state ${selected.status}`}>{stateLabel(selected.status)}</span>
              {props.variableRows.length ? <table className="scheduler-bench__variable-table"><thead><tr><th>阶段</th><th>变量</th><th>期望</th><th>当前</th><th>结果</th></tr></thead><tbody>{props.variableRows.map((row) => <tr key={row.key}><td>{row.phase}</td><td>{row.variable}</td><td>{row.expected}</td><td>{row.current}</td><td>{row.result}</td></tr>)}</tbody></table> : <p><b>当前等待原因</b><span>{props.waitingReasons[selected.id]?.message || '暂无变量检查记录；Action 执行后将在此显示。'}</span></p>}
            </div> : <div className="scheduler-bench__empty">从队列选择一个 Task 查看条件。</div>}
            <div className="scheduler-bench__timing-summary">
              <strong>所有样品耗时</strong>
              <span>总耗时 <b>{formatElapsedDurationMs(timingSummaries.overall.totalDurationMs)}</b></span>
              <span>机械臂 <b>{formatElapsedDurationMs(timingSummaries.overall.robotDurationMs)}</b></span>
              <span>动作 <b>{formatElapsedDurationMs(timingSummaries.overall.actionDurationMs)}</b></span>
            </div>
          </section>
        </aside>
      </section>
      {editingTask && (
        <div className="scheduler-bench__modal-backdrop" onMouseDown={() => setEditingTask(null)}>
          <section aria-label="Task 实例入参" className="scheduler-bench__modal scheduler-bench__parameter-modal" onMouseDown={(event) => event.stopPropagation()} role="dialog">
            <header>
              <div>
                <span>Task instance parameters</span>
                <h2>{editingTask.sample} / {editingTemplate?.name || editingTask.templateId}</h2>
                <p>{parametersEditable ? '本次修改应用于当前实例，并作为该模板下次生成队列的初始值。' : `当前状态为「${stateLabel(editingTask.status)}」，入参仅可查看。`}</p>
              </div>
              <button aria-label="关闭参数编辑" onClick={() => setEditingTask(null)} type="button">×</button>
            </header>
            <div className="scheduler-bench__parameter-body">
              {editingNodes.map((node, index) => {
                const values = { ...node.params, ...parameterDraft[node.templateNodeId] };
                const specs: NonNullable<ActionNode['paramSpecs']> = node.paramSpecs?.filter((spec) => spec.name)
                  || Object.keys(values).map((name) => ({ name }));
                return <section className="scheduler-bench__parameter-group" key={node.templateNodeId}>
                  <h3>{String(index + 1).padStart(2, '0')} · {node.label}</h3>
                  <small>{node.method} · {node.templateNodeId}</small>
                  {node.method === 'add_liquid_with_reusable_tip' ? (
                    <div className="scheduler-bench__s09-binding" role="status">
                      {s09TipSummary(s09TipStatus).map((line) => <div key={line}>{line}</div>)}
                      <div>{s09TipStatus?.last_operation
                        ? `上一次操作：工位 ${s09TipStatus.last_operation.liquid_station_index} · ${s09TipStatus.last_operation.solvent_batch_id} · 加液 TIP ${s09TipStatus.last_operation.liquid_tip_index} · 测密度 TIP ${s09TipStatus.last_operation.density_tip_index}`
                        : '上一次操作：暂无记录'}</div>
                    </div>
                  ) : null}
                  {node.method === 'dose_powder' ? (() => {
                    const additions = powderAdditionsFromValues(values);
                    return <div className="scheduler-bench__powder-additions">
                      <label>
                        <span>固体粉末种类数 · 同一样品需要依次加入的粉末数量</span>
                        <input
                          disabled={!parametersEditable}
                          min={1}
                          onChange={(event) => {
                            const count = Math.max(1, Math.floor(Number(event.target.value) || 1));
                            const next = additions.slice(0, count);
                            while (next.length < count) next.push({ coarse_position: 1, fine_position: 2, target_weight: '', recipe_name: 'default' });
                            updatePowderAdditions(node.templateNodeId, next);
                          }}
                          step={1}
                          type="number"
                          value={additions.length}
                        />
                      </label>
                      {additions.map((addition, additionIndex) => (
                        <fieldset key={additionIndex}>
                          <legend>粉末 {additionIndex + 1}</legend>
                          {([
                            ['coarse_position', '粗加粉罐位', 'number'],
                            ['fine_position', '细加粉罐位', 'number'],
                            ['target_weight', '单独目标重量（g）', 'number'],
                            ['recipe_name', '加粉策略', 'text'],
                          ] as const).map(([field, label, type]) => <label key={field}>
                            <span>{label}</span>
                            <input
                              disabled={!parametersEditable}
                              min={type === 'number' ? 0 : undefined}
                              max={type === 'number' && field !== 'target_weight' ? 10 : undefined}
                              onChange={(event) => {
                                const next = additions.map((item, index) => index === additionIndex
                                  ? { ...item, [field]: event.target.value }
                                  : item);
                                updatePowderAdditions(node.templateNodeId, next);
                              }}
                              step={type === 'number' && field !== 'target_weight' ? 1 : 'any'}
                              type={type}
                              value={String(addition[field] ?? '')}
                            />
                          </label>)}
                        </fieldset>
                      ))}
                    </div>;
                  })() : null}
                  {node.method === 'add_liquid_with_reusable_tip' ? (() => {
                    const additions = liquidAdditionsFromValues(values);
                    return <div className="scheduler-bench__powder-additions">
                      <label>
                        <span>液体种类数 · 本次加液动作需要依次加入的液体数量</span>
                        <input disabled={!parametersEditable} min={1} onChange={(event) => {
                          const count = Math.max(1, Math.floor(Number(event.target.value) || 1));
                          const next = additions.slice(0, count);
                          while (next.length < count) next.push({ liquid_station_index: 1, solvent_batch_id: '', volume: '', reuse_tip: true });
                          updateLiquidAdditions(node.templateNodeId, next);
                        }} step={1} type="number" value={additions.length} />
                      </label>
                      {additions.map((addition, additionIndex) => <fieldset key={additionIndex}>
                        <legend>液体 {additionIndex + 1}</legend>
                        {([
                          ['solvent_batch_id', '溶剂批次', 'text'],
                          ['liquid_station_index', '加工工位', 'number'],
                          ['volume', '独立加液体积', 'number'],
                        ] as const).map(([field, label, type]) => <label key={field}>
                          <span>{label}</span>
                          <input disabled={!parametersEditable} min={type === 'number' ? (field === 'volume' ? 0 : 1) : undefined}
                            max={field === 'liquid_station_index' ? 5 : undefined}
                            onChange={(event) => updateLiquidAdditions(node.templateNodeId, additions.map((item, index) => index === additionIndex
                              ? { ...item, [field]: event.target.value } : item))}
                            step={field === 'liquid_station_index' ? 1 : 'any'} type={type} value={String(addition[field] ?? '')} />
                        </label>)}
                        <label>
                          <span>复用此液体 TIP · 取消勾选时使用新 TIP</span>
                          <input checked={addition.reuse_tip} disabled={!parametersEditable}
                            onChange={(event) => updateLiquidAdditions(node.templateNodeId, additions.map((item, index) => index === additionIndex
                              ? { ...item, reuse_tip: event.target.checked } : item))} type="checkbox" />
                        </label>
                      </fieldset>)}
                      <label>
                        <span>执行前初始化 TIP 库存 · 会覆盖当前 TIP 使用和绑定记录</span>
                        <input checked={Boolean(values.initialize_tip_inventory)} disabled={!parametersEditable}
                          onChange={(event) => updateParameter(node.templateNodeId, 'initialize_tip_inventory', event.target.checked)} type="checkbox" />
                      </label>
                      {values.initialize_tip_inventory ? <label>
                        <span>初始化时已使用 TIP 数量</span>
                        <input disabled={!parametersEditable} min={0}
                          onChange={(event) => updateParameter(node.templateNodeId, 'initial_used_tip_count', event.target.value)}
                          step={1} type="number" value={String(values.initial_used_tip_count ?? 0)} />
                      </label> : null}
                    </div>;
                  })() : null}
                  {specs.filter((spec) => (
                    (node.method !== 'dose_powder' || !S07_POWDER_PARAMETERS.has(String(spec.name)))
                    && (node.method !== 'add_liquid_with_reusable_tip' || !S09_LIQUID_PARAMETERS.has(String(spec.name)))
                  )).map((spec) => {
                    const name = spec.name as string;
                    const value = values[name];
                    if (isAutomaticallyManagedActionParameter(node.method, name, node.templateNodeId)) {
                      return <p className="scheduler-bench__inherited-parameter" key={name}>
                        {automaticActionParameterDescription(node.method, name, node.templateNodeId)}
                      </p>;
                    }
                    const inputType = spec.type === 'boolean' ? 'checkbox' : spec.type === 'integer' || spec.type === 'number' ? 'number' : 'text';
                    return <label key={name}>
                      <span>{spec.label || name}{spec.description ? ` · ${spec.description}` : ''}</span>
                      {inputType === 'checkbox' ? (
                        <input checked={Boolean(value)} disabled={!parametersEditable} onChange={(event) => updateParameter(node.templateNodeId, name, event.target.checked)} type="checkbox" />
                      ) : (
                        <input
                          disabled={!parametersEditable}
                          max={spec.max}
                          min={spec.min}
                          onChange={(event) => updateParameter(
                            node.templateNodeId,
                            name,
                            event.target.value,
                          )}
                          onBlur={(event) => {
                            if (inputType === 'number' && event.target.value !== '') {
                              updateParameter(node.templateNodeId, name, Number(event.target.value));
                            }
                          }}
                          step={spec.type === 'integer' ? 1 : 'any'}
                          type={inputType}
                          value={typeof value === 'object' ? JSON.stringify(value) : String(value ?? '')}
                        />
                      )}
                    </label>;
                  })}
                </section>;
              })}
              {!editingNodes.length && editingTemplate && (
                <p className="scheduler-bench__empty">
                  该 Task 未找到可编辑的 Action 节点。
                  {props.actionNodes.length === 0
                    ? '请先在流程设计画布导入 Flow JSON（如 szlab_robot_action_workflow.json）。'
                    : `模板节点（${editingTemplate.nodeIds.join('、')}）与当前画布节点 ID 不一致；请重新导入对应 Flow JSON 或检查画布是否为空。`}
                </p>
              )}
            </div>
            <div className="scheduler-bench__modal-actions">
              {parametersEditable && <button className="scheduler-btn scheduler-btn--primary" onClick={() => {
                const numericParameters = new Map(editingNodes.map((node) => [
                  node.templateNodeId,
                  new Set((node.paramSpecs || [])
                    .filter((spec) => spec.type === 'integer' || spec.type === 'number')
                    .map((spec) => String(spec.name))),
                ]));
                const normalizedDraft = Object.fromEntries(Object.entries(parameterDraft).map(([nodeId, parameters]) => [
                  nodeId,
                  Object.fromEntries(Object.entries(parameters).map(([name, value]) => [
                    name,
                    name === 'powder_additions' && Array.isArray(value)
                      ? value.map((addition) => ({
                        ...(addition as PowderAddition),
                        coarse_position: numberValue((addition as PowderAddition).coarse_position),
                        fine_position: numberValue((addition as PowderAddition).fine_position),
                        target_weight: numberValue((addition as PowderAddition).target_weight),
                      }))
                      : name === 'liquid_additions' && Array.isArray(value)
                        ? value.map((addition) => ({
                          ...(addition as LiquidAddition),
                          liquid_station_index: numberValue((addition as LiquidAddition).liquid_station_index),
                          volume: numberValue((addition as LiquidAddition).volume),
                        }))
                        : numericParameters.get(nodeId)?.has(name) ? numberValue(value) : value,
                  ])),
                ]));
                props.onUpdateTaskParameters(editingTask.id, normalizedDraft);
                setEditingTask(null);
              }} type="button">保存并记住实例入参</button>}
              <span>实例 ID：{editingTask.id}</span>
            </div>
          </section>
        </div>
      )}
    </main>
  );
}

function PanelHead({ title, sub, badge }: { title: string; sub?: string; badge?: string }) {
  return <div className="scheduler-bench__panel-head"><div><h2>{title}</h2>{sub && <p>{sub}</p>}</div>{badge && <span>{badge}</span>}</div>;
}
