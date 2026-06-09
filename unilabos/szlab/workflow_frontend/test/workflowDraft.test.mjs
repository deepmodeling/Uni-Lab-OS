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

const { createWorkflowRequest, workflowDraftKey } = await importTypeScriptModule(
  new URL('../src/workflowDraft.ts', import.meta.url),
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
