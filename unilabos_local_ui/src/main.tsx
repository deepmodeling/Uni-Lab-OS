import React, { useCallback, useEffect, useMemo, useState } from 'react';
import ReactDOM from 'react-dom/client';
import ReactFlow, {
  Background,
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
import { createWorkflowRequest, workflowDraftKey } from './workflowDraft';

type ActionSpec = {
  method: string;
  label: string;
  description: string;
  device_id?: string;
  needs_position: boolean;
  params?: Array<Record<string, unknown>>;
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

type ActionNodeData = {
  deviceId?: string;
  method: string;
  label: string;
  description: string;
  params: { position?: number };
  runStatus?: NodeRunStatus;
  onPositionChange?: (nodeId: string, value: number) => void;
};

const DEFAULT_CONFIG = {
  graph: '__generated__',
  url: '',
  csv: 'ai4c_sim_updated.csv',
  timeout: 300,
  no_subscription: true,
  show_csv: true,
};

function App() {
  const [title, setTitle] = useState('szlab 本地调试工具');
  const [actions, setActions] = useState<ActionSpec[]>([]);
  const [nodes, setNodes] = useState<Node<ActionNodeData>[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  const [workflowName, setWorkflowName] = useState('szlab_canvas_workflow');
  const [workflow, setWorkflow] = useState<WorkflowJson | null>(null);
  const [message, setMessage] = useState('');
  const [runStatus, setRunStatus] = useState<RunStatus | null>(null);
  const [isRunning, setIsRunning] = useState(false);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [showConfigModal, setShowConfigModal] = useState(false);
  const [editingNodeId, setEditingNodeId] = useState<string | null>(null);
  const [selectedLogNodeId, setSelectedLogNodeId] = useState<string | null>(null);
  const [config, setConfig] = useState({
    graph: DEFAULT_CONFIG.graph,
    url: DEFAULT_CONFIG.url,
    csv: DEFAULT_CONFIG.csv,
    timeout: DEFAULT_CONFIG.timeout,
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

  useEffect(() => {
    if (selectedLogNodeId && !nodes.some((node) => node.id === selectedLogNodeId)) {
      setSelectedLogNodeId(null);
    }
  }, [nodes, selectedLogNodeId]);

  useEffect(() => {
    fetch('/api/preset')
      .then((response) => response.json())
      .then((payload: PresetPayload) => {
        setTitle(payload.title || 'szlab 本地调试工具');
        setActions(payload.actions || []);
        setWorkflowName(payload.default_workflow_name || 'szlab_canvas_workflow');
        setConfig((current) => ({
          ...current,
          graph: payload.default_config?.graph ?? DEFAULT_CONFIG.graph,
          url: payload.default_config?.url ?? DEFAULT_CONFIG.url,
          csv: payload.default_config?.csv ?? DEFAULT_CONFIG.csv,
          timeout: payload.default_config?.timeout ?? DEFAULT_CONFIG.timeout,
          no_subscription: payload.default_config?.no_subscription ?? DEFAULT_CONFIG.no_subscription,
          show_csv: payload.default_config?.show_csv ?? DEFAULT_CONFIG.show_csv,
        }));
      })
      .catch((error) => setMessage(`preset 加载失败: ${error.message}`));
  }, []);

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
          params: action.needs_position ? { position: 1 } : {},
          runStatus: 'idle',
        },
      },
    ]);
  };

  const updatePosition = (nodeId: string, value: number) => {
    setNodes((current) =>
      current.map((node) =>
        node.id === nodeId
          ? { ...node, data: { ...node.data, params: { ...node.data.params, position: value } } }
          : node,
      ),
    );
  };

  const buildWorkflow = useCallback(async () => {
    const request = createWorkflowRequest(workflowName, nodes, edges);
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
  }, [edges, nodes, workflowName]);

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
  }, [draftKey, nodes.length]);

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
    <div className="app">
      <header className="header">
        <div>
          <h1>{title}</h1>
        </div>
        <div className={`badge ${runStatus?.status || 'idle'}`}>{statusText(runStatus?.status)}</div>
      </header>

      <div className="layout">
        <aside className="panel palette">
          <h2>动作面板</h2>
          {actions.map((action) => (
            <button key={action.method} className="action-card" onClick={() => addActionNode(action)}>
              <strong>{action.label}</strong>
              <span>{action.description}</span>
            </button>
          ))}
        </aside>

        <main className="canvas-panel">
          <div className="flow-canvas">
            {workflow && (
              <div className="canvas-summary">
                已生成流程：{workflow.nodes.length} 个节点，{workflow.edges.length} 条连线
              </div>
            )}
            <ReactFlow
              nodes={nodes.map((node) => ({
                ...node,
                data: { ...node.data, onPositionChange: updatePosition },
              }))}
              edges={edges}
              nodeTypes={nodeTypes}
              onNodesChange={onNodesChange}
              onEdgesChange={onEdgesChange}
              onConnect={onConnect}
              onNodeClick={(_, node) => setEditingNodeId(node.id)}
              defaultEdgeOptions={{ type: 'smoothstep', animated: true }}
            >
              <Background />
              <MiniMap />
              <Controls />
            </ReactFlow>
          </div>
          <OpcChangePanel changes={opcChanges} nodes={nodes} />
        </main>

        <aside className="panel inspector">
          <section className="inspector-section">
            <h2>流程控制</h2>
            <div className="buttons">
              <button onClick={() => setShowConfigModal(true)}>运行配置</button>
              <button onClick={() => buildWorkflow().catch((error) => setMessage(error.message))}>校验流程</button>
              <button className="primary" onClick={runWorkflow} disabled={isRunning || !workflow}>运行</button>
              <button className="danger" onClick={cancelWorkflow} disabled={!activeRunId}>终止</button>
            </div>
            {message && <div className="message">{message}</div>}
          </section>

          <section className="inspector-section logs">
            <h2>运行日志</h2>
            <LogPanel
              events={logEvents}
              nodes={nodes}
              selectedNodeId={selectedLogNodeId}
              onSelectNode={setSelectedLogNodeId}
            />
          </section>
        </aside>
      </div>

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
            </div>
            {'position' in editingNode.data.params ? (
              <label>
                料架位置
                <input
                  type="number"
                  min={1}
                  max={8}
                  value={editingNode.data.params.position || 1}
                  onChange={(event) => updatePosition(editingNode.id, Number(event.target.value))}
                />
              </label>
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
              <input value={config.url} onChange={(event) => setConfig({ ...config, url: event.target.value })} placeholder="例如 opc.tcp://192.168.1.88:4840" />
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

  return (
    <div className={`flow-node ${selected ? 'selected' : ''} ${runStatus}`}>
      <Handle type="target" position={Position.Left} />
      <div className="flow-node-topline">
        <span className="flow-node-kicker">AI4C Action</span>
        <span className={`node-status ${runStatus}`}>{nodeStatusText(runStatus)}</span>
      </div>
      <div className="flow-node-title">{data.label}</div>
      <code>{id}</code>
      <Handle type="source" position={Position.Right} />
    </div>
  );
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
  selectedNodeId,
  onSelectNode,
}: {
  events: LogEvent[];
  nodes: Node<ActionNodeData>[];
  selectedNodeId: string | null;
  onSelectNode: (nodeId: string | null) => void;
}) {
  const nodeLabels = new Map(nodes.map((node) => [node.id, node.data.label]));
  const eventNodeIds = new Set(events.flatMap((event) => (event.node_id ? [event.node_id] : [])));
  const nodeTabs = [
    ...nodes
      .filter((node) => eventNodeIds.has(node.id))
      .map((node) => ({ id: node.id, label: node.data.label })),
    ...Array.from(eventNodeIds)
      .filter((nodeId) => !nodeLabels.has(nodeId))
      .map((nodeId) => ({ id: nodeId, label: nodeId })),
  ];
  const visibleEvents = selectedNodeId
    ? events.filter((event) => event.node_id === selectedNodeId)
    : events;
  const groupedEvents = groupLogEvents(visibleEvents);

  if (!events.length) {
    return <div className="log-empty">等待运行...</div>;
  }

  return (
    <div className="log-panel">
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
      </div>
    </div>
  );
}

function OpcChangePanel({ changes, nodes }: { changes: OpcChange[]; nodes: Node<ActionNodeData>[] }) {
  const nodeLabels = new Map(nodes.map((node) => [node.id, node.data.label]));

  return (
    <section className="opc-changes">
      <div className="opc-changes-head">
        <h3>OPC 变量变化</h3>
        <span>{changes.length} 条</span>
      </div>
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
              <td>{formatOpcValue(change.valueEnd)}</td>
            </tr>
          ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="opc-change-empty">暂无 OPC 变量变化</div>
      )}
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
    scope: 'workflow',
    node_id: null,
    detail: null,
  }));
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
    <ReactFlowProvider>
      <App />
    </ReactFlowProvider>
  </React.StrictMode>,
);
