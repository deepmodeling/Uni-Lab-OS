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
      id: 'process_devices',
      title: '设备工艺',
      device: 'S04 / S05',
      actions: [
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
      ],
    },
  ],
  '动作面板应按机械臂和设备工艺分层显示',
);
