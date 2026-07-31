import React from 'react';
import './taskSchedulerBench.css';
import type { TaskProcessLogLine, TaskVariableRow } from './taskActionLog';
import type { OpcSimulatorStatus } from './opcSimulatorProfile';
import type { TaskLogCategory, TaskLogLine } from './taskLogSession';
import {
  buildSampleProcessRows,
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

type Props = {
  templates: Template[];
  tasks: Task[];
  scheduledTemplateIds: string[];
  events: string[];
  waitingReasons: Record<string, { message?: string }>;
  sampleCount: number;
  isRunning: boolean;
  isTransitioning: boolean;
  environment: 'simulated' | 'real';
  selectedTaskId: string | null;
  onSampleCountChange: (value: number) => void;
  onToggleTemplate: (templateId: string) => void;
  onGenerate: () => void;
  onClear: () => void;
  onClearTemplates: () => void;
  clearTemplatesDisabled: boolean;
  templateActionsDisabled: boolean;
  onDownloadTemplate: (templateId: string) => void;
  onDeleteTemplate: (templateId: string) => void;
  onDownloadSelectedTemplates: () => void;
  onDeleteSelectedTemplates: () => void;
  onToggleRun: () => void;
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

type HeaderActionsProps = Pick<
  Props,
  | 'environment'
  | 'isRunning'
  | 'isTransitioning'
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
          <button className="scheduler-btn scheduler-btn--primary" disabled={props.isTransitioning} onClick={props.onToggleRun} type="button">
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
  const [logFilter, setLogFilter] = React.useState<TaskLogCategory>('all');
  const [isFollowingLogs, setIsFollowingLogs] = React.useState(true);
  const [timingNowMs, setTimingNowMs] = React.useState(() => Date.now());
  const logContainerRef = React.useRef<HTMLDivElement>(null);
  const selected = props.tasks.find((task) => task.id === props.selectedTaskId) || props.tasks[0];
  const selectedTemplate = props.templates.find((template) => template.id === selected?.templateId);
  const hasRunningTasks = props.tasks.some((task) => task.status === 'running');
  const sampleProcessRows = React.useMemo(
    () => buildSampleProcessRows(props.tasks, props.templates, timingNowMs),
    [props.tasks, props.templates, timingNowMs],
  );
  const visibleLogLines = props.logLines.filter((line) => logFilter === 'all' || line.category === logFilter);
  const editingTemplate = props.templates.find((template) => template.id === editingTask?.templateId);
  const editingNodes: ResolvedActionNode[] = editingTemplate
    ? resolveTemplateNodes(editingTemplate.nodeIds, props.actionNodes)
      .filter((entry): entry is { templateNodeId: string; node: ActionNode } => Boolean(entry.node))
      .map((entry) => ({ ...entry.node, templateNodeId: entry.templateNodeId }))
    : [];
  const parametersEditable = editingTask?.status === 'waiting' || editingTask?.status === 'pending';

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

  const exportLogs = () => {
    const text = visibleLogLines.map((line) => (
      `${new Date(line.timestamp).toLocaleString('zh-CN', { hour12: false })} [${line.category}] [${line.level}] ${line.message}`
    )).join('\n');
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
        { ...parameters },
      ]),
    ));
  };

  const updateParameter = (nodeId: string, parameter: string, value: unknown) => {
    setParameterDraft((current) => ({
      ...current,
      [nodeId]: { ...current[nodeId], [parameter]: value },
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
            <div className="scheduler-bench__sample-input"><input min="1" max="5" type="number" value={props.sampleCount} onChange={(event) => props.onSampleCountChange(Number(event.target.value))} /><button className="scheduler-btn scheduler-btn--primary" onClick={props.onGenerate} type="button">生成队列</button></div>
          </div>
          <div className="scheduler-bench__section">
            <div className="scheduler-bench__template-head">
              <label>选择 Task 模板（按顺序执行）</label>
              <span>已选 {props.scheduledTemplateIds.length}</span>
            </div>
            <div className="scheduler-bench__template-batch-actions">
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
                <button className="scheduler-btn scheduler-btn--ghost" onClick={props.onAdvance} type="button">调度一步</button>
                <button className="scheduler-btn scheduler-btn--primary" onClick={props.onToggleRun} type="button">{props.isRunning ? '暂停后续派发' : '开始派发'}</button>
                <button className="scheduler-btn scheduler-btn--danger" onClick={props.onClear} type="button">清空队列</button>
              </div>
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
            {sampleProcessRows.map((row) => {
              const rowStatus = sampleProcessRowStatus(row.blocks);
              return (
                <div className="scheduler-bench__progress-row" key={row.sample}>
                <strong>{row.sample}</strong>
                <div className="scheduler-bench__progress-track">
                  {row.blocks.map((block) => {
                    const template = props.templates.find((item) => item.id === block.templateId);
                    const resolvedNodes = template
                      ? resolveTemplateNodes(template.nodeIds, props.actionNodes)
                      : [];
                    const activeAction = block.actions.find((action) => action.state === 'running');
                    const activeNode = activeAction ? resolvedNodes[activeAction.index]?.node : null;
                    return (
                      <div
                        className={`scheduler-bench__progress-task ${block.state}`}
                        key={block.id}
                        style={{ minWidth: taskActionProgressMinWidth(block.actionTotal) }}
                      >
                        <div className="scheduler-bench__progress-task-head">
                          <span>{block.templateName}</span>
                          <small>
                            {activeNode ? `当前：${activeNode.label}` : stateLabel(block.state)}
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
                            return (
                              <i
                                className={`scheduler-bench__action-segment ${action.state}`}
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
                  <small className={`scheduler-bench__progress-summary ${rowStatus}`}>
                    {sampleRowStatusLabel(rowStatus)}
                  </small>
                </div>
              );
            })}
          </section>
        </section>

        <aside className="scheduler-bench__right">
          <section className="scheduler-bench__panel">
            <PanelHead title="运行日志" sub="调度、Action 与 OPC 条件的实时输出" badge="实时" />
            <div className="scheduler-bench__log-head"><button className="scheduler-bench__follow" onClick={() => setIsFollowingLogs((value) => !value)} type="button">● {isFollowingLogs ? '正在跟随最新日志' : '已暂停自动跟随'}</button><button onClick={exportLogs} type="button">导出</button></div>
            <div className="scheduler-bench__filters">{([
              ['all', '全部'],
              ['schedule', '调度'],
              ['action', 'Action'],
              ['opc', 'OPC'],
              ['error', '动作结果'],
            ] as const).map(([filter, label]) => <button className={logFilter === filter ? 'active' : ''} key={filter} onClick={() => setLogFilter(filter)} type="button">{label}</button>)}</div>
            <div className="scheduler-bench__log" onScroll={(event) => {
              const element = event.currentTarget;
              setIsFollowingLogs(element.scrollHeight - element.scrollTop - element.clientHeight < 12);
            }} ref={logContainerRef}>
              {visibleLogLines.map((line) => <div className={`scheduler-bench__log-line ${line.category}`} key={line.id}><time>{new Date(line.timestamp).toLocaleTimeString('zh-CN', { hour12: false })}</time> [{line.level}] {line.message}</div>)}
              {!visibleLogLines.length && <div>本次启动后暂无匹配日志。</div>}
            </div>
            <div className="scheduler-bench__log-foot">本次启动后 {visibleLogLines.length} 条 · {isFollowingLogs ? '自动滚动' : '滚动已暂停'}</div>
          </section>
          <section className="scheduler-bench__panel scheduler-bench__detail">
            <div className="scheduler-bench__detail-tabs"><button className="active" type="button">选中 Task 条件</button></div>
            {selected ? <div className="scheduler-bench__detail-body"><div><strong>{selected.sample} / {selectedTemplate?.name || selected.templateId}</strong><small>执行 ID：{selected.id}</small></div><span className={`scheduler-bench__state ${selected.status}`}>{stateLabel(selected.status)}</span>
              {props.variableRows.length ? <table className="scheduler-bench__variable-table"><thead><tr><th>阶段</th><th>变量</th><th>期望</th><th>当前</th><th>结果</th></tr></thead><tbody>{props.variableRows.map((row) => <tr key={row.key}><td>{row.phase}</td><td>{row.variable}</td><td>{row.expected}</td><td>{row.current}</td><td>{row.result}</td></tr>)}</tbody></table> : <p><b>当前等待原因</b><span>{props.waitingReasons[selected.id]?.message || '暂无变量检查记录；Action 执行后将在此显示。'}</span></p>}
            </div> : <div className="scheduler-bench__empty">从队列选择一个 Task 查看条件。</div>}
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
                <p>{parametersEditable ? '修改仅应用于当前 Task 实例。' : `当前状态为「${stateLabel(editingTask.status)}」，入参仅可查看。`}</p>
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
                  {specs.map((spec) => {
                    const name = spec.name as string;
                    const value = values[name];
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
                            inputType === 'number' && event.target.value !== '' ? Number(event.target.value) : event.target.value,
                          )}
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
                    ? '请先在流程设计画布导入 Flow JSON（如 szlab_robot_action_workflow_flow.json）。'
                    : `模板节点（${editingTemplate.nodeIds.join('、')}）与当前画布节点 ID 不一致；请重新导入对应 Flow JSON 或检查画布是否为空。`}
                </p>
              )}
            </div>
            <div className="scheduler-bench__modal-actions">
              {parametersEditable && <button className="scheduler-btn scheduler-btn--primary" onClick={() => {
                props.onUpdateTaskParameters(editingTask.id, parameterDraft);
                setEditingTask(null);
              }} type="button">保存当前实例入参</button>}
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
