import assert from 'node:assert/strict';
import { mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import ts from 'typescript';

const source = await readFile(
  new URL('../src/taskOrchestrationApi.ts', import.meta.url),
  'utf8',
);
const transpiled = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2020,
  },
});
const tempDir = await mkdtemp(join(tmpdir(), 'task-execution-preflight-gate-test-'));
const tempFile = join(tempDir, 'taskOrchestrationApi.mjs');
await writeFile(tempFile, transpiled.outputText, 'utf8');
const {
  createTaskExecutionController,
  runTaskExecutionCycle,
  TaskExecutionPreflightRejectedError,
} = await import(tempFile);
const originalConsoleInfo = console.info;
console.info = () => {};

const preflight = {
  valid: false,
  errors: [{
    category: 'dispatch_preflight',
    code: 'task_node_missing',
    message: 'Task 引用的动作节点不在当前 workflow 中',
    severity: 'error',
    phase: 'preflight',
    template_id: 'template-1',
    template_name: '样品处理',
    node_id: 'missing-node',
    device_id: '',
    action_name: '',
    instance_ids: ['instance-1'],
    detail: {},
  }],
  warnings: [],
  workspace_version: 9,
  workflow_fingerprint: 'sha256:invalid',
};
const activeWorkspace = {
  version: 7,
  workspace: {
    workflow_path: 'task-flow.json',
    templates: [],
    task_instances: [{
      id: 'instance-1',
      template_id: 'template-1',
      status: 'running',
      sample_id: 'sample-1',
      order: 0,
      not_before: null,
      started_at: null,
      finished_at: null,
    }],
    events: [],
    scheduled_template_ids: ['template-1'],
    scheduler_paused: false,
    pause_reason: null,
    schedule_entries: [],
    opc_snapshots: [],
    plc_registrations: [],
  },
};
const pausedWorkspace = {
  ...activeWorkspace,
  version: 10,
  workspace: { ...activeWorkspace.workspace, scheduler_paused: true },
};

const originalFetch = globalThis.fetch;
globalThis.fetch = undefined;
try {
  let caught;
  try {
    await runTaskExecutionCycle({
      fetcher: async (url) => new Response(JSON.stringify(
        url === '/api/task-opc/poll'
          ? { success: true, active: true }
          : {
            success: false,
            code: 'task_dispatch_preflight_failed',
            message: '派发预检未通过',
            preflight,
          },
      ), { status: 200 }),
      taskClient: {
        getWorkspace: async () => activeWorkspace,
        advance: async () => ({ ...activeWorkspace, version: 8 }),
      },
      workflowPath: 'task-flow.json',
      workflow: { nodes: [], edges: [] },
      expectedVersion: 7,
    });
  } catch (error) {
    caught = error;
  }
  assert.ok(caught instanceof TaskExecutionPreflightRejectedError);
  assert.equal(caught.code, 'task_dispatch_preflight_failed');
  assert.deepEqual(caught.preflight, preflight);
  assert.match(caught.message, /Task action tick 失败.*派发预检未通过/);
} finally {
  globalThis.fetch = originalFetch;
}

const events = [];
let activeCycleCalls = 0;
let harvestCycleCalls = 0;
const controller = createTaskExecutionController({
  runCycle: async () => {
    activeCycleCalls += 1;
    throw new TaskExecutionPreflightRejectedError('服务端复核失败', preflight);
  },
  runHarvestCycle: async () => {
    harvestCycleCalls += 1;
    return {
      active: false,
      workspace: pausedWorkspace,
      tick: { active: 0, in_flight: 0, claimed: 0, completed: 0, failed: 0 },
    };
  },
  applyWorkspace: (workspace) => events.push(`apply:${workspace.version}`),
  pauseScheduler: async (_workflowPath, expectedVersion) => {
    events.push(`pause:${expectedVersion}`);
    return pausedWorkspace;
  },
  onStatus: (status) => events.push(`status:${status.phase}`),
  onError: (message) => events.push(`error:${message}`),
  onPreflightRejected: (result, message) => {
    events.push(`rejected:${message}`);
    assert.equal(result, preflight);
  },
  onDrainingChange: (draining) => events.push(`draining:${draining}`),
});

controller.start();
assert.equal(await controller.run({
  workflowPath: 'task-flow.json',
  workflow: { nodes: [], edges: [] },
  expectedVersion: 7,
}), false);
assert.equal(activeCycleCalls, 1);
assert.equal(controller.isRunning(), true, '硬门禁拒绝后应进入 harvest-only 收尾状态');
assert.deepEqual(events.slice(0, 7), [
  'draining:false',
  'status:dispatching',
  'rejected:服务端复核失败',
  'pause:7',
  'apply:10',
  'draining:true',
  'status:failed',
]);
assert.match(events[7], /^error:服务端复核失败/);

await controller.run({
  workflowPath: 'task-flow.json',
  workflow: undefined,
  expectedVersion: 10,
});
assert.equal(harvestCycleCalls, 1);
assert.equal(activeCycleCalls, 1, '硬门禁拒绝后不得自动重试 active cycle');
assert.equal(controller.isRunning(), false, '在途动作收尾完成后应停止循环');

let ordinaryPauseCalls = 0;
const ordinaryController = createTaskExecutionController({
  runCycle: async () => { throw new Error('临时网络失败'); },
  applyWorkspace: () => {},
  pauseScheduler: async () => {
    ordinaryPauseCalls += 1;
    return pausedWorkspace;
  },
  onStatus: () => {},
  onError: () => {},
});
ordinaryController.start();
await ordinaryController.run({
  workflowPath: 'task-flow.json',
  workflow: { nodes: [], edges: [] },
  expectedVersion: 7,
});
assert.equal(ordinaryPauseCalls, 0, '普通网络错误仍应保留原来的自动重试策略');
assert.equal(ordinaryController.isRunning(), true);
ordinaryController.pause();

const mainSource = await readFile(new URL('../src/main.tsx', import.meta.url), 'utf8');
assert.match(
  mainSource,
  /onPreflightRejected:[\s\S]*?isTaskDispatchPreflightResult\(preflight\)[\s\S]*?source: 'dispatch'/,
  '服务端硬门禁结果应恢复为前端结构化 invalid 状态',
);
assert.match(
  mainSource,
  /isTaskExecutionDraining[\s\S]*?visible\.source === 'dispatch'/,
  '在途动作收尾期间应保留服务端返回的阻断项',
);
console.info = originalConsoleInfo;
