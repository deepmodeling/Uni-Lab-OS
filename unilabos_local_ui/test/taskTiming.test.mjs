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
  const tempDir = await mkdtemp(join(tmpdir(), 'task-timing-test-'));
  const tempFile = join(tempDir, 'taskTiming.mjs');
  await writeFile(tempFile, transpiled.outputText, 'utf8');
  return import(tempFile);
}

const {
  buildSampleProcessRows,
  compareSampleIds,
  buildExecutionTimingSummaries,
  buildTaskActionProgress,
  compactTaskProgressLabel,
  elapsedDurationMs,
  formatElapsedDurationMs,
  formatTaskActionTimingTitle,
  sampleProcessRowStatus,
  taskActionProgressMinWidth,
  taskWallDurationMs,
} = await importTypeScriptModule(
  new URL('../src/taskOrchestration.ts', import.meta.url),
);

assert.deepEqual(
  ['Sample AA', 'Sample B', 'Sample Z', 'Sample A', 'Sample AB'].sort(compareSampleIds),
  ['Sample A', 'Sample B', 'Sample Z', 'Sample AA', 'Sample AB'],
  '样品应优先按 A-Z，再按 AA-AZ 的顺序排列',
);

assert.equal(elapsedDurationMs(1_000, 4_750, 9_000), 3_750);
assert.equal(elapsedDurationMs(1_000, undefined, 6_500), 5_500);
assert.equal(elapsedDurationMs(undefined, undefined, 6_500), null);

assert.equal(
  taskWallDurationMs({
    status: 'running',
    startedAt: 500,
    actionRecords: [{ startedAt: 1_000 }],
  }, 6_500),
  5_500,
);
assert.equal(
  taskWallDurationMs({
    status: 'completed',
    startedAt: 500,
    finishedAt: 9_000,
    actionRecords: [{ startedAt: 1_000, finishedAt: 8_000 }],
  }, 20_000),
  7_000,
);
assert.equal(taskWallDurationMs({ status: 'running', startedAt: 1_000 }, 6_500), null);
assert.equal(taskWallDurationMs({ status: 'pending' }, 6_500), null);
assert.equal(taskWallDurationMs({ status: 'cancelled', startedAt: 1_000 }, 6_500), null);

assert.equal(formatElapsedDurationMs(59_999), '59s');
assert.equal(formatElapsedDurationMs(61_000), '1m 01s');
assert.equal(formatElapsedDurationMs(null), '—');
assert.equal(taskActionProgressMinWidth(1), 220);
assert.equal(taskActionProgressMinWidth(2), 280);
assert.equal(taskActionProgressMinWidth(4), 560);
assert.equal(
  compactTaskProgressLabel('S06 泵加液完整流程：烧杯检测 → 液位确认 → 写入工艺参数 → 等待加工完成'),
  'S06 泵加液',
);
assert.equal(
  compactTaskProgressLabel('执行 S09 烧杯加液：取 TIP、液体瓶取液、烧杯放液、放 TIP'),
  'S09 烧杯加液',
);
assert.equal(compactTaskProgressLabel('S072 取料 等 2 步'), 'S072 取料 等 2 步');

const actionProgress = buildTaskActionProgress(
  ['node-a', 'node-b', 'node-c'],
  [
    {
      nodeId: 'node-a',
      attempt: 1,
      executionId: 'exec-a-1',
      status: 'failed',
      startedAt: 1_000,
      finishedAt: 2_000,
    },
    {
      nodeId: 'node-a',
      attempt: 2,
      executionId: 'exec-a-2',
      status: 'succeeded',
      startedAt: 3_000,
      finishedAt: 5_500,
    },
    {
      nodeId: 'node-b',
      attempt: 1,
      executionId: 'exec-b-1',
      status: 'running',
      startedAt: 6_000,
    },
  ],
  9_000,
);
assert.deepEqual(
  actionProgress.map((action) => ({
    nodeId: action.nodeId,
    state: action.state,
    durationMs: action.durationMs,
    attempts: action.attempts.map((attempt) => attempt.durationMs),
  })),
  [
    { nodeId: 'node-a', state: 'completed', durationMs: 2_500, attempts: [1_000, 2_500] },
    { nodeId: 'node-b', state: 'running', durationMs: 3_000, attempts: [3_000] },
    { nodeId: 'node-c', state: 'waiting', durationMs: null, attempts: [] },
  ],
);
assert.match(formatTaskActionTimingTitle(actionProgress[0], '动作 A'), /^1\. 动作 A · 已完成/m);
assert.match(formatTaskActionTimingTitle(actionProgress[0], '动作 A'), /尝试 1 · 失败[\s\S]*1s/);
assert.match(formatTaskActionTimingTitle(actionProgress[0], '动作 A'), /尝试 2 · 已完成[\s\S]*2s/);

const processBlock = buildSampleProcessRows(
    [{
      id: 'task-1',
      sample: 'sample-1',
      templateId: 'template-1',
      order: 0,
      status: 'running',
      executionCursor: 1,
      startedAt: 500,
      actionRecords: [{
        nodeId: 'node-a',
        attempt: 1,
        executionId: 'exec-a',
        status: 'succeeded',
        startedAt: 1_000,
        finishedAt: 2_000,
      }],
    }],
    [{ id: 'template-1', name: '工艺一', nodeIds: ['node-a', 'node-b'] }],
    4_500,
  )[0].blocks[0];
assert.deepEqual(
  processBlock.actions.map(({ nodeId, state }) => ({ nodeId, state })),
  [
    { nodeId: 'node-a', state: 'completed' },
    { nodeId: 'node-b', state: 'waiting' },
  ],
);
assert.equal(processBlock.totalDurationMs, 3_500);

const timingSummaries = buildExecutionTimingSummaries(
  [
    {
      id: 'task-robot', sample: 'sample-1', templateId: 'robot-template', order: 0, status: 'completed',
      actionRecords: [{ nodeId: 'robot-node', attempt: 1, executionId: 'robot-exec', status: 'succeeded', startedAt: 1_000, finishedAt: 4_000 }],
    },
    {
      id: 'task-action', sample: 'sample-1', templateId: 'action-template', order: 1, status: 'completed',
      actionRecords: [{ nodeId: 'action-node', attempt: 1, executionId: 'action-exec', status: 'succeeded', startedAt: 5_000, finishedAt: 9_000 }],
    },
    {
      id: 'task-sample-2', sample: 'sample-2', templateId: 'action-template', order: 0, status: 'running',
      actionRecords: [{ nodeId: 'action-node', attempt: 1, executionId: 'action-running', status: 'running', startedAt: 8_000 }],
    },
  ],
  [
    { id: 'robot-template', nodeIds: ['robot-node'] },
    { id: 'action-template', nodeIds: ['action-node'] },
  ],
  [
    { id: 'robot-node', deviceId: 'szlab_mixer_robot', method: 'move' },
    { id: 'action-node', deviceId: 'szlab_mixer_pump', method: 'run' },
  ],
  10_000,
);
assert.deepEqual(timingSummaries.samples.get('sample-1'), {
  totalDurationMs: 8_000,
  robotDurationMs: 3_000,
  actionDurationMs: 4_000,
});
assert.deepEqual(timingSummaries.samples.get('sample-2'), {
  totalDurationMs: 2_000,
  robotDurationMs: 0,
  actionDurationMs: 2_000,
});
assert.deepEqual(timingSummaries.overall, {
  totalDurationMs: 9_000,
  robotDurationMs: 3_000,
  actionDurationMs: 6_000,
});

const blockedProcessBlock = buildSampleProcessRows(
  [{
    id: 'task-2',
    sample: 'sample-2',
    templateId: 'template-1',
    order: 0,
    status: 'running',
    startedAt: 500,
    actionRecords: [],
  }],
  [{ id: 'template-1', name: '工艺一', nodeIds: ['node-a', 'node-b'] }],
  4_500,
)[0].blocks[0];
assert.equal(blockedProcessBlock.state, 'pending');
assert.equal(blockedProcessBlock.totalDurationMs, null);

assert.equal(
  sampleProcessRowStatus([{ state: 'running' }, { state: 'pending' }]),
  'current',
);
assert.equal(
  sampleProcessRowStatus([{ state: 'completed' }, { state: 'running' }]),
  'current',
);
assert.equal(
  sampleProcessRowStatus([{ state: 'completed' }, { state: 'completed' }]),
  'completed',
);
assert.equal(
  sampleProcessRowStatus([{ state: 'completed' }, { state: 'pending' }]),
  'queued',
);
assert.equal(
  sampleProcessRowStatus([{ state: 'failed' }, { state: 'pending' }]),
  'failed',
);

console.log('task timing tests passed');
