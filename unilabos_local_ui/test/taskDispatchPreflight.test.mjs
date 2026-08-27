import assert from 'node:assert/strict';
import { mkdtemp, readFile, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import ts from 'typescript';

const source = await readFile(
  new URL('../src/taskDispatchPreflight.ts', import.meta.url),
  'utf8',
);
const transpiled = ts.transpileModule(source, {
  compilerOptions: {
    module: ts.ModuleKind.ES2022,
    target: ts.ScriptTarget.ES2020,
  },
});
const tempDir = await mkdtemp(join(tmpdir(), 'task-dispatch-preflight-test-'));
const tempFile = join(tempDir, 'taskDispatchPreflight.mjs');
await writeFile(tempFile, transpiled.outputText, 'utf8');
const {
  TaskDispatchPreflightHttpError,
  canStartTaskDispatch,
  currentTaskDispatchReadiness,
  preflightTaskDispatch,
  taskDispatchButtonTitle,
  taskDispatchIssueResolution,
  taskDispatchReadinessLabel,
} = await import(tempFile);

const issue = {
  category: 'dispatch_preflight',
  code: 'task_node_missing',
  message: 'Task「样品处理」引用的动作节点不在当前 workflow 中: task-node',
  severity: 'error',
  phase: 'preflight',
  template_id: 'template-1',
  template_name: '样品处理',
  node_id: 'task-node',
  device_id: '',
  action_name: '',
  instance_ids: ['instance-1'],
  detail: {},
};
const validResult = {
  valid: true,
  errors: [],
  warnings: [],
  workspace_version: 7,
  workflow_fingerprint: 'sha256:valid',
};
const invalidResult = {
  ...validResult,
  valid: false,
  errors: [issue],
  workflow_fingerprint: 'sha256:invalid',
};

const requests = [];
const valid = await preflightTaskDispatch({
  workflowPath: 'task-flow.json',
  expectedVersion: 7,
  workflow: { nodes: [], edges: [] },
  fetcher: async (url, init) => {
    requests.push({ url, init });
    return new Response(JSON.stringify(validResult), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    });
  },
});

assert.deepEqual(valid, validResult);
assert.equal(requests[0].url, '/api/task-execution/preflight');
assert.deepEqual(JSON.parse(requests[0].init.body), {
  task_workspace_path: 'task-flow.json',
  expected_version: 7,
  workflow: { nodes: [], edges: [] },
});

const invalid = await preflightTaskDispatch({
  workflowPath: 'task-flow.json',
  expectedVersion: 7,
  workflow: { nodes: [] },
  fetcher: async () => new Response(JSON.stringify({ detail: invalidResult }), {
    status: 422,
    headers: { 'content-type': 'application/json' },
  }),
});
assert.deepEqual(invalid, invalidResult, '结构化 422 应作为预检结果返回，而不是网络异常');

await assert.rejects(
  preflightTaskDispatch({
    workflowPath: 'task-flow.json',
    expectedVersion: 7,
    workflow: { nodes: [] },
    fetcher: async () => new Response(JSON.stringify({
      detail: { code: 'version_conflict', message: 'workspace changed' },
    }), {
      status: 409,
      headers: { 'content-type': 'application/json' },
    }),
  }),
  (error) => (
    error instanceof TaskDispatchPreflightHttpError
    && error.status === 409
    && error.code === 'version_conflict'
    && error.message === 'workspace changed'
  ),
);

const oldReady = { status: 'ready', key: 'old', result: validResult };
assert.deepEqual(
  currentTaskDispatchReadiness(oldReady, 'new'),
  { status: 'stale', key: 'new' },
  '语义键变化后旧的绿色结果必须立即失效',
);
assert.equal(
  taskDispatchReadinessLabel({ status: 'invalid', key: 'key', result: invalidResult }),
  '派发准备失败 · 1 项',
);
assert.equal(
  canStartTaskDispatch({ status: 'ready', key: 'key', result: validResult }),
  true,
  '只有服务端明确返回 valid 的 ready 状态才允许开始派发',
);
for (const readiness of [
  { status: 'stale', key: 'key' },
  { status: 'validating', key: 'key' },
  { status: 'invalid', key: 'key', result: invalidResult },
  { status: 'unavailable', key: 'key' },
  { status: 'ready', key: 'key', result: invalidResult },
]) {
  assert.equal(canStartTaskDispatch(readiness), false);
}
assert.match(
  taskDispatchButtonTitle({ status: 'invalid', key: 'key', result: invalidResult }),
  /预检未通过.*动作节点不在当前 workflow/,
);
assert.match(
  taskDispatchIssueResolution(issue),
  /将动作节点.*task-node.*加入当前可执行 workflow.*样品处理/,
  '节点缺失应明确提示修复 workflow 或 Task 引用',
);
for (const code of [
  'task_dispatch_scope_empty',
  'workspace_recovery_required',
  'task_template_missing',
  'task_template_empty',
  'task_node_missing',
  'task_node_not_executable',
  'task_node_invalid',
  'task_device_missing',
  'task_action_unsupported',
]) {
  assert.ok(
    taskDispatchIssueResolution({ ...issue, code }).length > 10,
    `${code} 应提供可执行的修复建议`,
  );
}

const mainSource = await readFile(new URL('../src/main.tsx', import.meta.url), 'utf8');
const benchSource = await readFile(
  new URL('../src/TaskSchedulerBench.tsx', import.meta.url),
  'utf8',
);
const benchStyles = await readFile(
  new URL('../src/taskSchedulerBench.css', import.meta.url),
  'utf8',
);
assert.match(
  mainSource,
  /const runTaskDispatchPreflight = useCallback[\s\S]*?preflightTaskDispatch\([\s\S]*?error\.status !== 409[\s\S]*?getWorkspace\([\s\S]*?preflightTaskDispatch\(/,
  '预检调用应统一在 workspace 版本冲突时刷新后重试一次',
);
assert.match(
  mainSource,
  /setTaskDispatchReadiness\(\{ status: 'validating', key \}\)[\s\S]*?runTaskDispatchPreflight\(\s*workflow,/,
  '语义变化后应自动预检，并在 workspace 版本冲突时刷新后重试一次',
);
assert.match(
  mainSource,
  /retryTaskDispatchPreflight = useCallback[\s\S]*?setTaskDispatchPreflightRevision\(\(current\) => current \+ 1\)[\s\S]*?taskDispatchPreflightRevision,/,
  '手动重试应使同一份派发内容重新进入自动预检',
);
assert.match(
  mainSource,
  /builtWorkflow = await buildWorkflow\(\)[\s\S]*?开始派发前正在执行最终确认[\s\S]*?finalPreflight = await runTaskDispatchPreflight\(builtWorkflow\)[\s\S]*?if \(!finalPreflight\.valid\)[\s\S]*?return;[\s\S]*?setTaskExecutionWorkflow\(builtWorkflow\)[\s\S]*?const version = finalPreflight\.workspace_version/,
  '点击开始派发后必须使用刚构建的 workflow 最终预检，并绑定通过时的 workspace 版本',
);
assert.match(
  benchSource,
  /scheduler-bench__dispatch-readiness[\s\S]*?taskDispatchReadinessLabel[\s\S]*?result\?\.errors\.map/,
  'Task 页面应展示派发准备状态和结构化阻断项',
);
assert.match(
  benchSource,
  /处理建议[\s\S]*?taskDispatchIssueResolution\(issue\)/,
  '每个结构化阻断项都应展示对应修复建议',
);
assert.match(
  benchSource,
  /onClick=\{\(\) => props\.onInspectDispatchIssue\(issue\)\}[\s\S]*?定位并处理此阻断项/,
  '结构化阻断项应提供可点击的定位入口',
);
assert.match(
  mainSource,
  /inspectTaskDispatchIssue = useCallback[\s\S]*?node\.id === issue\.node_id[\s\S]*?setWorkspace\('workflow'\)[\s\S]*?setCanvasTab\('workflow'\)[\s\S]*?setSelectedTaskTemplateId\(issue\.template_id\)[\s\S]*?setIsTaskDetailModalOpen\(true\)/,
  '阻断项应优先定位现存画布节点，节点缺失时打开对应 Task 模板',
);
assert.match(
  benchSource,
  /status === 'invalid'[\s\S]*?status === 'unavailable'[\s\S]*?onRetryDispatchPreflight[\s\S]*?重新检查/,
  '预检失败或暂不可用时应提供明确的重新检查入口',
);
assert.match(
  benchSource,
  /disabled=\{props\.isTransitioning \|\| \(!props\.isRunning && !dispatchReady\)\}[\s\S]*?开始派发/,
  '开始派发按钮在非运行状态下必须等待预检通过',
);
assert.match(
  benchStyles,
  /\.scheduler-btn--dispatch-blocked[\s\S]*?\.scheduler-btn--dispatch-ready/,
  '预检通过后的派发按钮应获得绿色 ready 样式',
);
assert.match(
  mainSource,
  /if \(!canStartTaskDispatch\(visibleTaskDispatchReadiness\)\) \{[\s\S]*?派发预检尚未通过/,
  '派发事件处理函数本身也必须拒绝绕过按钮的调用',
);
