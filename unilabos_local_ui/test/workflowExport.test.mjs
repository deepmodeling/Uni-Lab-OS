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
  const tempDir = await mkdtemp(join(tmpdir(), 'workflow-export-test-'));
  const tempFile = join(tempDir, `${path.pathname.split('/').pop().replace('.ts', '')}.mjs`);
  await writeFile(tempFile, transpiled.outputText, 'utf8');
  return import(tempFile);
}

const { createPseudoFlowJson } = await importTypeScriptModule(
  new URL('../src/workflowExport.ts', import.meta.url),
);
const { createImportedDraft, createWorkflowRequest } = await importTypeScriptModule(
  new URL('../src/workflowDraft.ts', import.meta.url),
);

const nodes = [
  {
    id: 'stir',
    data: {
      label: '执行磁搅',
      method: 'run_stirring',
      deviceId: 'szlab_mixer_stirrer',
      params: { position: 1 },
    },
  },
  {
    id: 'pick',
    data: {
      label: '抓取孔板',
      method: 'submit_pick_from_stack',
      deviceId: 'szlab_mixer_robot',
      params: { stack: 'A', slot: 1 },
    },
  },
];

assert.deepEqual(createPseudoFlowJson('szlab_flow', nodes, [{ id: 'e1', source: 'pick', target: 'stir' }]), {
  name: 'szlab_flow',
  rules: [
    {
      name: 'szlab_flow',
      trigger: {
        node: '抓取孔板',
        value: true,
        edge: 'rising',
      },
      log_nodes: ['抓取孔板', '执行磁搅'],
      actions: [
        {
          action: {
            index: 1,
            node: '抓取孔板',
            workflow_node_id: 'pick',
            device_id: 'szlab_mixer_robot',
            method: 'submit_pick_from_stack',
            params: { stack: 'A', slot: 1 },
          },
        },
        {
          action: {
            index: 2,
            node: '执行磁搅',
            workflow_node_id: 'stir',
            device_id: 'szlab_mixer_stirrer',
            method: 'run_stirring',
            params: { position: 1 },
          },
        },
      ],
    },
  ],
});

const bypassedFlow = createPseudoFlowJson(
  'bypassed_flow',
  [
    {
      ...nodes[0],
      data: {
        ...nodes[0].data,
        executionBypassed: true,
      },
    },
    nodes[1],
  ],
  [{ source: 'pick', target: 'stir' }],
);
const bypassedActions = bypassedFlow.rules[0].actions.map((item) => item.action);
assert.equal(
  bypassedActions.find((action) => action.workflow_node_id === 'stir').execution_bypassed,
  true,
  '直通 action 应导出 snake_case execution_bypassed',
);
assert.equal(
  Object.hasOwn(bypassedActions.find((action) => action.workflow_node_id === 'pick'), 'execution_bypassed'),
  false,
  '普通 action 不应导出 execution_bypassed',
);

const actions = [
  {
    method: 'move_plate',
    label: '移动孔板',
    description: '移动孔板到目标位置',
    device_id: 'robot',
    params: [{ name: 'position', type: 'integer', default: 1 }],
  },
];

const draft = createWorkflowRequest(
  'roundtrip',
  [
    {
      id: 'node_1',
      position: { x: 24, y: 48 },
      data: {
        deviceId: 'robot',
        method: 'move_plate',
        label: '移动孔板',
        description: '移动孔板到目标位置',
        params: { position: 9 },
      },
    },
  ],
  [],
);

const imported = createImportedDraft(draft, actions, { autoLayout: false });
assert.equal(imported.name, 'roundtrip');
assert.equal(imported.nodes[0].id, 'node_1');
assert.deepEqual(imported.nodes[0].position, { x: 24, y: 48 });
assert.equal(imported.nodes[0].data.deviceId, 'robot');
assert.equal(imported.nodes[0].data.params.position, 9);
