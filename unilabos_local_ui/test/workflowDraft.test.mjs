import assert from 'node:assert/strict';
import { mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import ts from 'typescript';

async function importTypeScriptModule(path) {
  const source = await readFile(path, 'utf8');
  const transpiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.ES2022,
      target: ts.ScriptTarget.ES2020,
      strict: true,
    },
  });
  const tempDir = await mkdtemp(join(tmpdir(), 'workflow-draft-test-'));
  const tempFile = join(tempDir, 'workflowDraft.mjs');
  await writeFile(tempFile, transpiled.outputText, 'utf8');
  return import(tempFile);
}

const {
  createExecutionEdgeOverlay,
  createExecutionPlan,
  createImportedDraft,
  createWorkflowRequest,
  layoutFlowGraph,
  workflowDraftKey,
} = await importTypeScriptModule(
  new URL('../src/workflowDraft.ts', import.meta.url),
);
const { collectOpcChanges, formatOpcValue } = await importTypeScriptModule(
  new URL('../src/opcChanges.ts', import.meta.url),
);
const { formatUiError, buildWorkspaceSummary, groupActionsByDevice } = await importTypeScriptModule(
  new URL('../src/uiState.ts', import.meta.url),
);
const mainSource = await readFile(new URL('../src/main.tsx', import.meta.url), 'utf8');
const styleSource = await readFile(new URL('../src/styles.css', import.meta.url), 'utf8');
const opcChangesSource = await readFile(new URL('../src/opcChanges.ts', import.meta.url), 'utf8');

const toolbarSource = mainSource.match(/<div className="demo-canvas-toolbar">[\s\S]*?<div className="demo-tabbar"/)?.[0] || '';
assert.equal(
  /onClick=\{runWorkflow\}/.test(toolbarSource),
  false,
  '顶部画布工具栏不应包含运行按钮',
);

const runButtonsSource = mainSource.match(/<div className="demo-run-buttons">[\s\S]*?<\/div>/)?.[0] || '';
assert.equal(
  /校验流程/.test(runButtonsSource),
  false,
  '右侧运行按钮区不应重复展示校验流程',
);
assert.match(
  styleSource,
  /\.demo-run-buttons\s*\{[\s\S]*?gap:\s*(1[2-9]|[2-9]\d)px;/,
  '右侧运行按钮区需要至少 12px 间距，避免按钮挤在一起',
);
assert.match(
  styleSource,
  /\.demo-execution-summary\s*\{[^}]*margin-top:\s*(1[0-9]|[2-9]\d)px;/,
  '运行按钮区和执行摘要之间需要至少 10px 间距',
);
assert.match(
  mainSource,
  /<details className="opc-collapsible" open>[\s\S]*?<summary className="opc-changes-head">[\s\S]*?OPC 采样变量/,
  'OPC 采样变量应放在可折叠区域内',
);
assert.match(
  mainSource,
  /<details className="opc-collapsible" open>[\s\S]*?<summary className="opc-changes-head">[\s\S]*?OPC 变量变化/,
  'OPC 变量变化应放在可折叠区域内',
);
assert.match(
  mainSource,
  /leftPanelCollapsed/,
  '左侧联调入口面板应有折叠状态',
);
assert.match(
  mainSource,
  /aria-label=\{leftPanelCollapsed \? '展开联调入口' : '收起联调入口'\}/,
  '左侧联调入口面板应提供可访问的折叠按钮',
);
assert.match(
  styleSource,
  /\.demo-workbench\.left-collapsed\s*\{[^}]*grid-template-columns:\s*64px minmax\(600px, 1fr\) 340px;/,
  '左侧联调入口收起后应变为窄栏',
);
assert.match(
  opcChangesSource,
  /category\?: string/,
  '结构化日志事件应包含 category 标签',
);
assert.match(
  mainSource,
  /selectedLogCategory/,
  '日志面板应支持按 category 筛选',
);
assert.match(
  mainSource,
  /className="log-category-tabs"/,
  '日志面板应渲染分类标签栏',
);
assert.match(
  mainSource,
  /onToggleBypassed/,
  'ActionNode 应接收直通切换回调',
);
const actionNodeSource = mainSource.match(
  /function ActionNode\([\s\S]*?\nfunction executionStateText/,
)?.[0] || '';
const bypassButtonSource = actionNodeSource.match(
  /<button(?:(?!<button)[\s\S])*?<path d="M4 12h15M14 7l5 5-5 5" \/>[\s\S]*?<\/button>/,
)?.[0] || '';
assert.match(
  bypassButtonSource,
  /aria-label=\{data\.executionBypassed \? '取消直通' : '直通此节点'\}/,
  '直通按钮 aria-label 应表达当前可执行动作',
);
assert.match(
  bypassButtonSource,
  /aria-pressed=\{Boolean\(data\.executionBypassed\)\}/,
  '直通按钮应暴露 aria-pressed 状态',
);
assert.match(
  bypassButtonSource,
  /title=\{data\.executionBypassed \? '取消直通' : '直通此节点'\}/,
  '直通按钮标题应反映当前切换状态',
);
assert.match(
  mainSource,
  /executionBypassed:\s*!node\.data\.executionBypassed[\s\S]*?executionDisabled:\s*false/,
  '切换直通时应无条件清除禁用状态',
);
assert.match(
  mainSource,
  /executionDisabled:\s*!node\.data\.executionDisabled[\s\S]*?executionBypassed:\s*false/,
  '切换禁用时应无条件清除直通状态',
);
assert.match(
  styleSource,
  /\.flow-node\.execution-bypassed\s*\{/,
  '直通节点应提供独立视觉样式',
);
assert.match(
  styleSource,
  /\.execution-badge\.bypassed\s*\{/,
  '直通徽标应提供独立视觉样式',
);
assert.match(
  styleSource,
  /\.node-hover-actions button\.active\s*\{/,
  '直通按钮激活时应有明显样式',
);
assert.match(
  styleSource,
  /\.flow-node:focus-within \.node-hover-actions/,
  '节点工具栏应在内部按钮获得键盘焦点时显示',
);
assert.match(
  styleSource,
  /\.node-hover-actions button:focus-visible\s*\{[^}]*outline:/,
  '节点工具栏按钮应提供明确的键盘焦点轮廓',
);
const renderedEdgesSource = mainSource.match(
  /const renderedEdges = useMemo\([\s\S]*?\n  \}, \[edges, executionPlan\.executableEdges\]\);/,
)?.[0] || '';
assert.match(
  renderedEdgesSource,
  /executionPlan\.executableEdges\.map\(\(edge\) => JSON\.stringify\(\[edge\.source, edge\.target\]\)\)/,
  '画布应使用安全端点键识别可执行原始边',
);
assert.match(
  renderedEdgesSource,
  /executableEdgeEndpoints\.has\(JSON\.stringify\(\[edge\.source, edge\.target\]\)\)/,
  '原始边高亮判断应使用安全端点键',
);
assert.match(
  renderedEdgesSource,
  /createExecutionEdgeOverlay\(edges, executionPlan\.executableEdges\)\.map/,
  '画布应通过纯函数生成缺失的派生边覆盖层',
);
assert.match(
  renderedEdgesSource,
  /className:\s*'execution-derived-edge'[\s\S]*?animated:\s*true[\s\S]*?selectable:\s*false[\s\S]*?deletable:\s*false[\s\S]*?focusable:\s*false/,
  '派生边应动画显示且不可选择、不可删除、不可聚焦',
);
const reactFlowEdgesSource = mainSource.match(
  /<ReactFlow[\s\S]*?edges=\{renderedEdges\}[\s\S]*?nodeTypes=\{nodeTypes\}/,
)?.[0] || '';
assert.match(reactFlowEdgesSource, /edges=\{renderedEdges\}/, 'ReactFlow 应渲染原始边与派生边的组合');
assert.match(
  styleSource,
  /\.react-flow__edge\.execution-derived-edge\s+\.react-flow__edge-path\s*\{[^}]*stroke:[^;}]+;[^}]*stroke-dasharray:/,
  '派生边应有独立且清晰的连线样式',
);

const baseNodes = [
  {
    id: 'load',
    position: { x: 0, y: 0 },
    data: {
      method: 'pick_well_plate_from_loading_rack',
      label: '从上料架取孔板',
      description: '取孔板',
      params: { position: 1 },
      runStatus: 'idle',
    },
  },
];
const runningNodes = [
  {
    ...baseNodes[0],
    data: {
      ...baseNodes[0].data,
      runStatus: 'running',
    },
  },
];
const edges = [{ id: 'e1', source: 'load', target: 'unload' }];

const arrowSafeOriginalEdges = [
  { id: 'original-arrow', source: 'a->b', target: 'c' },
];
const arrowSafeExecutableEdges = [
  { id: 'same-endpoints', source: 'a->b', target: 'c' },
  { id: 'missing-endpoints', source: 'a', target: 'b->c' },
];
const overlayBaseId = 'execution-derived:1:a:4:b->c';
const collidingOriginalEdges = [
  ...arrowSafeOriginalEdges,
  { id: overlayBaseId, source: 'reserved', target: 'edge' },
];
const executionOverlay = createExecutionEdgeOverlay(
  collidingOriginalEdges,
  arrowSafeExecutableEdges,
);
assert.deepEqual(
  executionOverlay.map((edge) => [edge.source, edge.target]),
  [['a', 'b->c']],
  '覆盖层只应返回原图缺失的端点，且包含 -> 的节点 ID 不应发生端点键碰撞',
);
assert.notEqual(
  executionOverlay[0].id,
  overlayBaseId,
  '覆盖边基础 ID 与原始边冲突时应确定性消解',
);
assert.equal(
  new Set([...collidingOriginalEdges, ...executionOverlay].map((edge) => edge.id)).size,
  collidingOriginalEdges.length + executionOverlay.length,
  '原始边与覆盖边合并后 ID 应全局唯一',
);
assert.deepEqual(
  createExecutionEdgeOverlay(collidingOriginalEdges, arrowSafeExecutableEdges),
  executionOverlay,
  '相同输入应稳定生成相同覆盖边 ID',
);

assert.deepEqual(createWorkflowRequest('ai4c', baseNodes, edges), {
  name: 'ai4c',
  nodes: [
    {
      id: 'load',
      position: { x: 0, y: 0 },
      data: {
        method: 'pick_well_plate_from_loading_rack',
        label: '从上料架取孔板',
        description: '取孔板',
        params: { position: 1 },
      },
    },
  ],
  edges: [{ id: 'e1', source: 'load', target: 'unload' }],
});
assert.equal(
  workflowDraftKey('ai4c', baseNodes, edges),
  workflowDraftKey('ai4c', runningNodes, edges),
  '运行状态变化不应改变 workflow 草稿指纹',
);

const changedParamNodes = [
  {
    ...baseNodes[0],
    data: {
      ...baseNodes[0].data,
      params: { position: 2 },
    },
  },
];
assert.notEqual(
  workflowDraftKey('ai4c', baseNodes, edges),
  workflowDraftKey('ai4c', changedParamNodes, edges),
  '参数变化应触发 workflow 草稿重新校验',
);

const actionSpecs = [
  {
    method: 'pick_well_plate_from_loading_rack',
    label: '从上料架取孔板',
    description: '取孔板',
    device_id: 'robot',
    params: [{ name: 'position', label: '位置', type: 'integer', default: 1 }],
  },
  {
    method: 'put_well_plate_to_loading_rack',
    label: '放回上料架',
    description: '放孔板',
    device_id: 'robot',
    params: [{ name: 'position', label: '位置', type: 'integer', default: 2 }],
  },
];

const importedFlow = createImportedDraft(
  {
    name: 'imported_flow',
    rules: [
      {
        actions: [
          {
            action: {
              workflow_node_id: 'load',
              method: 'pick_well_plate_from_loading_rack',
              params: { position: 3 },
            },
          },
          {
            action: {
              workflow_node_id: 'unload',
              method: 'put_well_plate_to_loading_rack',
              params: { position: 4 },
            },
          },
        ],
      },
    ],
  },
  actionSpecs,
);
assert.equal(importedFlow.name, 'imported_flow');
assert.equal(importedFlow.nodes.length, 2, 'flow json 应还原两个节点');
assert.equal(importedFlow.nodes[0].data.label, '从上料架取孔板');
assert.equal(importedFlow.nodes[0].data.params.position, 3);
assert.deepEqual(
  importedFlow.edges.map((edge) => [edge.source, edge.target]),
  [['load', 'unload']],
  'flow json 应按动作顺序生成连线',
);
assert.ok(
  importedFlow.nodes[1].position.x > importedFlow.nodes[0].position.x,
  '导入 flow 后应自动生成递增横向布局',
);
const importedSnakeCaseBypassedFlow = createImportedDraft(
  {
    rules: [{
      actions: [{
        action: {
          workflow_node_id: 'load',
          method: 'pick_well_plate_from_loading_rack',
          params: {},
          execution_bypassed: true,
          execution_disabled: true,
        },
      }],
    }],
  },
  actionSpecs,
);
assert.equal(importedSnakeCaseBypassedFlow.nodes[0].data.executionBypassed, true);
assert.equal(
  importedSnakeCaseBypassedFlow.nodes[0].data.executionDisabled,
  false,
  'pseudo Flow 冲突字段应以 snake_case 直通状态优先',
);
const importedCamelCaseBypassedFlow = createImportedDraft(
  {
    rules: [{
      actions: [{
        action: {
          workflow_node_id: 'load',
          method: 'pick_well_plate_from_loading_rack',
          params: {},
          executionBypassed: true,
          executionDisabled: true,
        },
      }],
    }],
  },
  actionSpecs,
);
assert.equal(importedCamelCaseBypassedFlow.nodes[0].data.executionBypassed, true);
assert.equal(
  importedCamelCaseBypassedFlow.nodes[0].data.executionDisabled,
  false,
  'pseudo Flow 冲突字段应以 camelCase 直通状态优先',
);

const importedDraft = createImportedDraft(
  {
    name: 'canvas_draft',
    nodes: [
      {
        id: 'load',
        position: { x: 10, y: 20 },
        data: {
          method: 'pick_well_plate_from_loading_rack',
          label: '旧标签',
          description: '旧描述',
          params: { position: 5 },
        },
      },
    ],
    edges: [],
  },
  actionSpecs,
  { autoLayout: false },
);
assert.equal(importedDraft.name, 'canvas_draft');
assert.deepEqual(importedDraft.nodes[0].position, { x: 10, y: 20 }, '画布草稿可保留原坐标');
assert.equal(importedDraft.nodes[0].data.label, '从上料架取孔板', 'preset 元数据应覆盖旧标签');
assert.equal(importedDraft.nodes[0].data.params.position, 5, '导入参数应覆盖默认参数');

const restoredDraft = createImportedDraft(createWorkflowRequest('persisted_draft', importedDraft.nodes, importedDraft.edges), actionSpecs, { autoLayout: false });
assert.equal(restoredDraft.name, 'persisted_draft');
assert.equal(restoredDraft.nodes[0].id, 'load');
assert.deepEqual(restoredDraft.nodes[0].position, { x: 10, y: 20 }, '持久化草稿恢复后应保留坐标');
assert.equal(restoredDraft.nodes[0].data.params.position, 5, '持久化草稿恢复后应保留参数');

const restoredDisabledDraft = createImportedDraft(
  createWorkflowRequest(
    'persisted_disabled',
    [{ ...importedDraft.nodes[0], data: { ...importedDraft.nodes[0].data, executionDisabled: true } }],
    [],
  ),
  actionSpecs,
  { autoLayout: false },
);
assert.equal(restoredDisabledDraft.nodes[0].data.executionDisabled, true, '持久化草稿恢复后应保留禁用状态');

const bypassedWorkflowRequest = createWorkflowRequest(
  'persisted_bypassed',
  [{ ...importedDraft.nodes[0], data: { ...importedDraft.nodes[0].data, executionBypassed: true } }],
  [],
);
assert.equal(
  bypassedWorkflowRequest.nodes[0].data.execution_bypassed,
  true,
  '导出草稿时应使用 snake_case 持久化直通状态',
);
const restoredBypassedDraft = createImportedDraft(
  bypassedWorkflowRequest,
  actionSpecs,
  { autoLayout: false },
);
assert.equal(restoredBypassedDraft.nodes[0].data.executionBypassed, true, 'snake_case 草稿应恢复直通状态');
const restoredCamelCaseBypassedDraft = createImportedDraft(
  {
    ...bypassedWorkflowRequest,
    nodes: [{
      ...bypassedWorkflowRequest.nodes[0],
      data: {
        ...bypassedWorkflowRequest.nodes[0].data,
        execution_bypassed: undefined,
        executionBypassed: true,
      },
    }],
  },
  actionSpecs,
  { autoLayout: false },
);
assert.equal(
  restoredCamelCaseBypassedDraft.nodes[0].data.executionBypassed,
  true,
  'camelCase 草稿应恢复直通状态',
);
const restoredConflictingDraft = createImportedDraft(
  {
    ...bypassedWorkflowRequest,
    nodes: [{
      ...bypassedWorkflowRequest.nodes[0],
      data: {
        ...bypassedWorkflowRequest.nodes[0].data,
        execution_disabled: true,
        execution_bypassed: true,
      },
    }],
  },
  actionSpecs,
  { autoLayout: false },
);
assert.equal(restoredConflictingDraft.nodes[0].data.executionBypassed, true);
assert.equal(
  restoredConflictingDraft.nodes[0].data.executionDisabled,
  false,
  '画布草稿冲突字段应以直通状态优先',
);

const linearBypassPlan = createExecutionPlan(
  [
    { ...baseNodes[0], id: 'a', data: { ...baseNodes[0].data } },
    { ...baseNodes[0], id: 'b', data: { ...baseNodes[0].data, executionBypassed: true } },
    { ...baseNodes[0], id: 'c', data: { ...baseNodes[0].data } },
  ],
  [
    { id: 'a-b', source: 'a', target: 'b' },
    { id: 'b-c', source: 'b', target: 'c' },
  ],
);
assert.deepEqual(linearBypassPlan.executableNodes.map((node) => node.id), ['a', 'c']);
assert.deepEqual(
  linearBypassPlan.executableEdges.map((edge) => [edge.source, edge.target]),
  [['a', 'c']],
  '线性流程应越过直通节点重连前驱和后继',
);
assert.equal(linearBypassPlan.nodeStates.b.reason, 'bypassed');

const consecutiveBypassPlan = createExecutionPlan(
  [
    { ...baseNodes[0], id: 'a', data: { ...baseNodes[0].data } },
    { ...baseNodes[0], id: 'b', data: { ...baseNodes[0].data, executionBypassed: true } },
    { ...baseNodes[0], id: 'c', data: { ...baseNodes[0].data, executionBypassed: true } },
    { ...baseNodes[0], id: 'd', data: { ...baseNodes[0].data } },
  ],
  [
    { id: 'a-b', source: 'a', target: 'b' },
    { id: 'b-c', source: 'b', target: 'c' },
    { id: 'c-d', source: 'c', target: 'd' },
  ],
);
assert.deepEqual(consecutiveBypassPlan.executableNodes.map((node) => node.id), ['a', 'd']);
assert.deepEqual(
  consecutiveBypassPlan.executableEdges.map((edge) => [edge.source, edge.target]),
  [['a', 'd']],
  '连续直通节点应收敛为一条重连边',
);
const reorderedConsecutiveBypassPlan = createExecutionPlan(
  [
    { ...baseNodes[0], id: 'd', data: { ...baseNodes[0].data } },
    { ...baseNodes[0], id: 'c', data: { ...baseNodes[0].data, executionBypassed: true } },
    { ...baseNodes[0], id: 'b', data: { ...baseNodes[0].data, executionBypassed: true } },
    { ...baseNodes[0], id: 'a', data: { ...baseNodes[0].data } },
  ],
  [
    { id: 'a-b', source: 'a', target: 'b' },
    { id: 'b-c', source: 'b', target: 'c' },
    { id: 'c-d', source: 'c', target: 'd' },
  ],
);
assert.deepEqual(
  reorderedConsecutiveBypassPlan.executableEdges
    .map((edge) => [edge.source, edge.target, edge.id])
    .sort(),
  consecutiveBypassPlan.executableEdges
    .map((edge) => [edge.source, edge.target, edge.id])
    .sort(),
  '直通节点在 nodes 中重排后，派生边端点和 id 应保持一致',
);

const branchedBypassNodes = [
  { ...baseNodes[0], id: 'p1', data: { ...baseNodes[0].data } },
  { ...baseNodes[0], id: 'p2', data: { ...baseNodes[0].data } },
  { ...baseNodes[0], id: 'x', data: { ...baseNodes[0].data, executionBypassed: true } },
  { ...baseNodes[0], id: 'q1', data: { ...baseNodes[0].data } },
  { ...baseNodes[0], id: 'q2', data: { ...baseNodes[0].data } },
];
const branchedBypassEdges = [
  { id: 'p1-x', source: 'p1', target: 'x' },
  { id: 'p2-x', source: 'p2', target: 'x' },
  { id: 'x-q1', source: 'x', target: 'q1' },
  { id: 'x-q2', source: 'x', target: 'q2' },
  { id: 'bypass:2:p1:2:q2', source: 'p1', target: 'q1' },
  { id: 'x-p1', source: 'x', target: 'p1' },
];
const branchedBypassPlan = createExecutionPlan(branchedBypassNodes, branchedBypassEdges);
assert.deepEqual(
  branchedBypassPlan.executableEdges.map((edge) => `${edge.source}->${edge.target}`).sort(),
  ['p1->q1', 'p1->q2', 'p2->p1', 'p2->q1', 'p2->q2'],
  '分支直通应生成前驱×后继，并去重且排除自环',
);
assert.equal(
  branchedBypassPlan.executableEdges.filter((edge) => edge.source === edge.target).length,
  0,
  '派生执行图不应包含自环',
);
assert.deepEqual(
  new Set(branchedBypassPlan.executableEdges.map((edge) => edge.id)).size,
  branchedBypassPlan.executableEdges.length,
  '输入边 id 与候选派生 id 冲突时，最终执行边 id 仍应唯一',
);

const bypassedStartPlan = createExecutionPlan(
  [
    { ...baseNodes[0], id: 'a', data: { ...baseNodes[0].data } },
    { ...baseNodes[0], id: 'b', data: { ...baseNodes[0].data, executionBypassed: true } },
    { ...baseNodes[0], id: 'c', data: { ...baseNodes[0].data } },
    { ...baseNodes[0], id: 'd', data: { ...baseNodes[0].data } },
  ],
  [
    { id: 'a-b', source: 'a', target: 'b' },
    { id: 'b-c', source: 'b', target: 'c' },
    { id: 'c-d', source: 'c', target: 'd' },
  ],
  'b',
);
assert.equal(bypassedStartPlan.startNodeId, 'b', '直通起点仍应保留为有效选择');
assert.deepEqual(bypassedStartPlan.executableNodes.map((node) => node.id), ['c', 'd']);
assert.equal(bypassedStartPlan.nodeStates.a.reason, 'beforeStart');
assert.equal(bypassedStartPlan.nodeStates.b.reason, 'bypassed');

const executionPlan = createExecutionPlan(
  [
    { ...baseNodes[0], id: 'a', data: { ...baseNodes[0].data, label: 'A' } },
    { ...baseNodes[0], id: 'b', data: { ...baseNodes[0].data, label: 'B' } },
    { ...baseNodes[0], id: 'c', data: { ...baseNodes[0].data, label: 'C', executionDisabled: true } },
    { ...baseNodes[0], id: 'd', data: { ...baseNodes[0].data, label: 'D' } },
  ],
  [
    { id: 'a-b', source: 'a', target: 'b' },
    { id: 'b-c', source: 'b', target: 'c' },
    { id: 'c-d', source: 'c', target: 'd' },
  ],
  'b',
);
assert.deepEqual(executionPlan.executableNodes.map((node) => node.id), ['b']);
assert.deepEqual(executionPlan.executableEdges, []);
assert.equal(executionPlan.nodeStates.a.reason, 'beforeStart');
assert.equal(executionPlan.nodeStates.b.reason, 'willRun');
assert.equal(executionPlan.nodeStates.c.reason, 'disabled');
assert.equal(executionPlan.nodeStates.d.reason, 'blockedByDisabled');
assert.equal(executionPlan.startNodeId, 'b');
assert.equal(executionPlan.disabledNodeId, 'c');

const disabledBeforeStartPlan = createExecutionPlan(
  [
    { ...baseNodes[0], id: 'a', data: { ...baseNodes[0].data, executionDisabled: true } },
    { ...baseNodes[0], id: 'b', data: { ...baseNodes[0].data } },
    { ...baseNodes[0], id: 'c', data: { ...baseNodes[0].data } },
  ],
  [
    { id: 'a-b', source: 'a', target: 'b' },
    { id: 'b-c', source: 'b', target: 'c' },
  ],
  'b',
);
assert.deepEqual(disabledBeforeStartPlan.executableNodes.map((node) => node.id), ['b', 'c']);
assert.equal(disabledBeforeStartPlan.nodeStates.a.reason, 'beforeStart');
assert.equal(disabledBeforeStartPlan.disabledNodeId, null);

const unorderedDagPlan = createExecutionPlan(
  [
    { ...baseNodes[0], id: 'b', data: { ...baseNodes[0].data, label: 'B' } },
    { ...baseNodes[0], id: 'c', data: { ...baseNodes[0].data, label: 'C' } },
    { ...baseNodes[0], id: 'a', data: { ...baseNodes[0].data, label: 'A' } },
  ],
  [
    { id: 'a-b', source: 'a', target: 'b' },
    { id: 'b-c', source: 'b', target: 'c' },
  ],
);
assert.deepEqual(
  unorderedDagPlan.executableNodes.map((node) => node.id),
  ['a', 'b', 'c'],
  '执行计划应按 DAG 拓扑顺序，而不是节点加入顺序',
);

const laidOut = layoutFlowGraph(
  [
    { ...baseNodes[0], id: 'a', position: { x: 999, y: 999 } },
    { ...baseNodes[0], id: 'b', position: { x: 999, y: 999 } },
    { ...baseNodes[0], id: 'c', position: { x: 999, y: 999 } },
  ],
  [
    { id: 'a-b', source: 'a', target: 'b' },
    { id: 'b-c', source: 'b', target: 'c' },
  ],
);
assert.ok(laidOut[1].position.x > laidOut[0].position.x, '线性流程应按 x 轴递增布局');
assert.ok(laidOut[2].position.x > laidOut[1].position.x, '线性流程后续节点应继续右移');

const unorderedDagLayout = layoutFlowGraph(
  [
    { ...baseNodes[0], id: 'b', position: { x: 999, y: 999 } },
    { ...baseNodes[0], id: 'c', position: { x: 999, y: 999 } },
    { ...baseNodes[0], id: 'a', position: { x: 999, y: 999 } },
  ],
  [
    { id: 'a-b', source: 'a', target: 'b' },
    { id: 'b-c', source: 'b', target: 'c' },
  ],
);
assert.deepEqual(
  unorderedDagLayout.map((node) => node.id),
  ['a', 'b', 'c'],
  '自动布局应按 DAG 拓扑顺序重排节点数组，而不是保留加入顺序',
);
assert.ok(unorderedDagLayout[1].position.x > unorderedDagLayout[0].position.x, 'DAG 后继节点应排在前驱右侧');

const gridLayout = layoutFlowGraph(
  [
    { ...baseNodes[0], id: 'first', position: { x: 999, y: 999 } },
    { ...baseNodes[0], id: 'second', position: { x: 999, y: 999 } },
  ],
  [],
);
assert.notDeepEqual(gridLayout[0].position, gridLayout[1].position, '无边节点应分配不同网格位置');

const wrappedLinearLayout = layoutFlowGraph(
  Array.from({ length: 7 }, (_, index) => ({
    ...baseNodes[0],
    id: `node_${index + 1}`,
    position: { x: 999, y: 999 },
  })),
  Array.from({ length: 6 }, (_, index) => ({
    id: `edge_${index + 1}`,
    source: `node_${index + 1}`,
    target: `node_${index + 2}`,
  })),
);
assert.equal(wrappedLinearLayout[6].position.x, wrappedLinearLayout[0].position.x, '第 7 个节点应换行回到行首');
assert.ok(wrappedLinearLayout[6].position.y > wrappedLinearLayout[0].position.y, '第 7 个节点应排到下一行');

const branchedLayout = layoutFlowGraph(
  [
    { ...baseNodes[0], id: 'precheck', position: { x: 999, y: 999 } },
    { ...baseNodes[0], id: 'solvent', position: { x: 999, y: 999 } },
    { ...baseNodes[0], id: 'branch_start', position: { x: 999, y: 999 } },
    { ...baseNodes[0], id: 's05', position: { x: 999, y: 999 } },
    { ...baseNodes[0], id: 'photo', position: { x: 999, y: 999 } },
    { ...baseNodes[0], id: 's11', position: { x: 999, y: 999 } },
    { ...baseNodes[0], id: 's08', position: { x: 999, y: 999 } },
    { ...baseNodes[0], id: 'join', position: { x: 999, y: 999 } },
    { ...baseNodes[0], id: 'post', position: { x: 999, y: 999 } },
  ],
  [
    { id: 'precheck-solvent', source: 'precheck', target: 'solvent' },
    { id: 'solvent-branch', source: 'solvent', target: 'branch_start' },
    { id: 'branch-s05', source: 'branch_start', target: 's05' },
    { id: 'branch-s11', source: 'branch_start', target: 's11' },
    { id: 's05-photo', source: 's05', target: 'photo' },
    { id: 'photo-join', source: 'photo', target: 'join' },
    { id: 's11-s08', source: 's11', target: 's08' },
    { id: 's08-join', source: 's08', target: 'join' },
    { id: 'join-post', source: 'join', target: 'post' },
  ],
);
const branchedById = Object.fromEntries(branchedLayout.map((node) => [node.id, node.position]));
assert.equal(branchedById.solvent.y, branchedById.precheck.y, '分支前主干应保留在同一行');
assert.ok(branchedById.solvent.x > branchedById.precheck.x, '分支前主干应横向递增');
assert.equal(branchedById.branch_start.x, branchedById.precheck.x, '分支入口锚点应另起一行并回到行首');
assert.ok(branchedById.branch_start.y > branchedById.precheck.y, '分支入口锚点应下移到新行');
assert.equal(branchedById.photo.y, branchedById.s05.y, '同一条分支应按横向行排列');
assert.ok(branchedById.photo.x > branchedById.s05.x, '同一条分支后续节点应向右排列');
assert.equal(branchedById.s08.y, branchedById.s11.y, '第二条分支也应按横向行排列');
assert.ok(branchedById.s08.x > branchedById.s11.x, '第二条分支后续节点应向右排列');
assert.ok(branchedById.s11.y > branchedById.s05.y, '不同分支应分到不同横向行');
assert.equal(branchedById.join.y, branchedById.branch_start.y, '分支汇合锚点应回到分支入口所在主干行');
assert.ok(branchedById.join.x > branchedById.branch_start.x, '分支汇合锚点应位于分支入口右侧');
assert.equal(branchedById.post.y, branchedById.branch_start.y, '汇合后的主干应继续沿锚点行排列');
assert.ok(branchedById.post.x > branchedById.join.x, '汇合后的主干应继续向右排列');

const opcRowsWhileRunning = collectOpcChanges([
  {
    sequence: 1,
    message: 'OPC状态采样: 2 个变量',
    level: 'info',
    scope: 'node',
    node_id: 'node_1',
    detail: {
      before: {
        S06允许加工: {
          name: 'S06允许加工',
          label: 'S06允许加工',
          display_name: 'S06允许加工',
          node_id: 'ns=2;i=269',
          value: { success: true, value: true, node_id: 'ns=2;i=269' },
          value_goal: { success: true, value: false, node_id: 'ns=2;i=269' },
        },
        S06加工完成: {
          name: 'S06加工完成',
          label: 'S06加工完成',
          display_name: 'S06加工完成',
          node_id: 'ns=2;i=270',
          value: { success: true, value: false, node_id: 'ns=2;i=270' },
        },
      },
    },
  },
]);
assert.equal(opcRowsWhileRunning.length, 2, '运行中应显示执行前采样到的等待变量');
assert.equal(opcRowsWhileRunning[0].valueBegin.value, true);
assert.equal(opcRowsWhileRunning[0].valueGoal.value, false);
assert.equal(opcRowsWhileRunning[0].valueEnd, undefined);

const opcRowsWithWaitGoal = collectOpcChanges([
  {
    sequence: 1,
    message: 'OPC状态采样: 1 个变量',
    level: 'info',
    scope: 'node',
    node_id: 'node_1',
    detail: {
      before: {
        S06加工完成: {
          name: 'S06加工完成',
          label: 'S06加工完成',
          display_name: 'S06加工完成',
          node_id: 'ns=4;s=S06加工完成',
          value: { success: true, value: false, node_id: 'ns=4;s=S06加工完成' },
        },
      },
    },
  },
  {
    sequence: 2,
    message: '等待 OPC 变量 S06加工完成 == true',
    level: 'info',
    scope: 'node',
    node_id: 'node_1',
    detail: {
      type: 'opc_wait',
      phase: 'start',
      variable: 'S06加工完成',
      expected: true,
      node_id: 'ns=4;s=S06加工完成',
      display_name: 'S06加工完成',
      label: 'S06加工完成 (ns=4;s=S06加工完成)',
    },
  },
  {
    sequence: 3,
    message: 'OPC 变量等待完成 S06加工完成 == true',
    level: 'info',
    scope: 'node',
    node_id: 'node_1',
    detail: {
      type: 'opc_wait',
      phase: 'finish',
      variable: 'S06加工完成',
      expected: true,
      last_value: true,
      node_id: 'ns=4;s=S06加工完成',
      display_name: 'S06加工完成',
      label: 'S06加工完成 (ns=4;s=S06加工完成)',
    },
  },
]);
assert.equal(opcRowsWithWaitGoal.length, 1, 'wait expected 应合并到同一个 OPC 变量行');
assert.equal(opcRowsWithWaitGoal[0].valueBegin.value, false);
assert.equal(opcRowsWithWaitGoal[0].valueGoal, true);
assert.equal(opcRowsWithWaitGoal[0].valueEnd, true);

assert.equal(formatOpcValue({ success: true, value: false, node_id: 'ns=2;i=270' }), 'false');
assert.equal(formatOpcValue({ success: false, error: 'bad node' }), 'bad node');

assert.equal(
  formatUiError(new TypeError('Failed to fetch'), '运行 workflow'),
  '运行 workflow 失败：无法连接本地调试服务，请确认 workflow_ui 后端仍在运行，且当前页面与后端端口一致。',
);
assert.equal(formatUiError(new Error('workflow 不能包含环'), '校验流程'), '校验流程失败：workflow 不能包含环');

const summary = buildWorkspaceSummary({
  nodes: [
    { data: { deviceId: 'szlab_mixer_robot', runStatus: 'success' } },
    { data: { deviceId: 'szlab_mixer_stirrer', runStatus: 'running' } },
    { data: { deviceId: 'szlab_mixer_photoshotting', runStatus: 'idle' } },
  ],
  edges: [{}, {}],
  opcChangeCount: 6,
  runStatus: 'running',
});
assert.deepEqual(summary, {
  totalNodes: 3,
  totalEdges: 2,
  runningNodes: 1,
  completedNodes: 1,
  deviceCount: 3,
  opcChangeCount: 6,
  runStatusText: '运行中',
});

assert.deepEqual(
  groupActionsByDevice([
    {
      method: 'submit_place_to_s04',
      label: '放置到 S04 磁搅位',
      description: '放置到 S04 磁搅位',
      device_id: 'szlab_mixer_robot',
    },
    {
      method: 'run_stirring',
      label: '执行 S04 磁搅加工',
      description: '执行 S04 磁搅加工',
      device_id: 'szlab_mixer_stirrer',
    },
    {
      method: 'take_photo',
      label: '拍照并保存结果',
      description: '拍照并保存结果',
      device_id: 'szlab_mixer_photoshotting',
    },
  ]),
  [
    {
      id: 'szlab_mixer_robot',
      title: '机械臂转运',
      device: 'szlab_mixer_robot',
      actions: [
        {
          method: 'submit_place_to_s04',
          label: '放置到 S04 磁搅位',
          description: '放置到 S04 磁搅位',
          device_id: 'szlab_mixer_robot',
        },
      ],
    },
    {
      id: 'szlab_mixer_stirrer',
      title: 'szlab_mixer_stirrer',
      device: 'szlab_mixer_stirrer',
      actions: [
        {
          method: 'run_stirring',
          label: '执行 S04 磁搅加工',
          description: '执行 S04 磁搅加工',
          device_id: 'szlab_mixer_stirrer',
        },
      ],
    },
    {
      id: 'szlab_mixer_photoshotting',
      title: 'szlab_mixer_photoshotting',
      device: 'szlab_mixer_photoshotting',
      actions: [
        {
          method: 'take_photo',
          label: '拍照并保存结果',
          description: '拍照并保存结果',
          device_id: 'szlab_mixer_photoshotting',
        },
      ],
    },
  ],
  '动作面板应按机械臂和单设备分层显示',
);
