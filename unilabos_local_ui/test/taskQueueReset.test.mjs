import assert from 'node:assert/strict';
import { mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import ts from 'typescript';

const apiSource = await readFile(new URL('../src/taskOrchestrationApi.ts', import.meta.url), 'utf8');
const mainSource = await readFile(new URL('../src/main.tsx', import.meta.url), 'utf8');
const benchSource = await readFile(new URL('../src/TaskSchedulerBench.tsx', import.meta.url), 'utf8');

assert.match(
  benchSource,
  /暂停后续派发[\s\S]*?清除残留并复用[\s\S]*?清空队列/,
  '重置并复用入口必须位于暂停和清空之间',
);
assert.match(
  mainSource,
  /const resetTaskQueueProgress = useCallback[\s\S]*?已停止并断开[\s\S]*?failAction[\s\S]*?resetInstancesProgress/,
  '存在残留动作时必须强确认，先标记失败再重置',
);
assert.doesNotMatch(
  mainSource,
  /resetProgressDisabled=\{[\s\S]{0,300}(?:isTaskExecutionDraining|hasActiveServerExecution)/,
  '服务端已暂停时，残留动作不应永久锁死恢复入口',
);

const transpiled = ts.transpileModule(apiSource, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2020,
  },
});
const tempDir = await mkdtemp(join(tmpdir(), 'task-queue-reset-test-'));
const tempFile = join(tempDir, 'taskOrchestrationApi.mjs');
await writeFile(tempFile, transpiled.outputText, 'utf8');
const { createTaskOrchestrationClient } = await import(tempFile);

const requests = [];
const client = createTaskOrchestrationClient({
  baseUrl: 'http://scheduler.test/api/v1',
  fetchImpl: async (url, init) => {
    requests.push({ url, init });
    return new Response(JSON.stringify({
      version: 6,
      workspace: {
        workflow_path: '/tmp/demo.json',
        templates: [],
        task_instances: [],
        events: [],
        scheduled_template_ids: [],
        scheduler_paused: true,
        schedule_entries: [],
        opc_snapshots: [],
        plc_registrations: [],
      },
    }), { status: 200, headers: { 'content-type': 'application/json' } });
  },
});

await client.resetInstancesProgress('/tmp/demo.json', 5);
assert.equal(requests[0].url, 'http://scheduler.test/api/v1/instances:reset-progress');
assert.deepEqual(
  JSON.parse(requests[0].init.body),
  { workflow_path: '/tmp/demo.json', expected_version: 5 },
);

await client.failAction(
  '/tmp/demo.json',
  6,
  'instance-1',
  'node-1',
  'execution-1',
  { code: 'manual_stale_action_clear' },
);
assert.equal(requests[1].url, 'http://scheduler.test/api/v1/actions:fail');
assert.deepEqual(
  JSON.parse(requests[1].init.body),
  {
    workflow_path: '/tmp/demo.json',
    expected_version: 6,
    instance_id: 'instance-1',
    node_id: 'node-1',
    execution_id: 'execution-1',
    error: { code: 'manual_stale_action_clear' },
  },
);
