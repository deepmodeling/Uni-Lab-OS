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
