import assert from 'node:assert/strict';
import test from 'node:test';
import {
  buildSampleProcessRows,
} from '../src/taskOrchestration.ts';
import {
  buildTaskVariableRows,
} from '../src/taskActionLog.ts';

test('buildSampleProcessRows groups by sample and order', () => {
  const rows = buildSampleProcessRows(
    [
      {
        id: 'b-0',
        sample: 'Sample B',
        templateId: 'tpl-s07',
        order: 0,
        status: 'running',
        executionCursor: 1,
      },
      {
        id: 'a-0',
        sample: 'Sample A',
        templateId: 'tpl-s07',
        order: 0,
        status: 'waiting',
        executionCursor: 0,
      },
    ],
    [{ id: 'tpl-s07', name: 'S07 固体加料', nodeIds: ['n1', 'n2', 'n3'] }],
  );
  assert.equal(rows.length, 2);
  assert.equal(rows[0].sample, 'Sample A');
  assert.equal(rows[1].sample, 'Sample B');
  assert.equal(rows[1].blocks[0].actionDone, 1);
  assert.equal(rows[1].blocks[0].actionTotal, 3);
});

test('buildTaskVariableRows renders opc wait detail', () => {
  const rows = buildTaskVariableRows([
    {
      seq: 1,
      timestamp: 1,
      instance_id: 'inst',
      node_id: 'node_001',
      execution_id: 'exec',
      sample_id: 'Sample A',
      level: 'info',
      message: 'wait',
      detail: {
        type: 'opc_wait',
        phase: 'change',
        variable: 'Robot_Home',
        expected: true,
        last_value: false,
      },
    },
  ]);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].variable, 'Robot_Home');
  assert.equal(rows[0].expected, 'true');
  assert.equal(rows[0].current, 'false');
});

test('buildTaskVariableRows uses parent finish success for sensor_conditions', () => {
  const rows = buildTaskVariableRows([
    {
      seq: 1,
      timestamp: 1,
      instance_id: 'inst',
      node_id: 'node_001',
      execution_id: 'exec',
      sample_id: 'Sample B',
      level: 'info',
      message: 'change',
      detail: {
        type: 'opc_wait',
        wait_kind: 'sensor_conditions',
        phase: 'change',
        context: 'pre',
        conditions: [
          {
            variable: '传感器状态_上位机[0].NO[6]',
            display_name: 'S03未使用烧杯',
            expected: true,
            actual: false,
            satisfied: false,
          },
        ],
      },
    },
    {
      seq: 2,
      timestamp: 2,
      instance_id: 'inst',
      node_id: 'node_001',
      execution_id: 'exec',
      sample_id: 'Sample B',
      level: 'info',
      message: 'finish',
      detail: {
        type: 'opc_wait',
        wait_kind: 'sensor_conditions',
        phase: 'finish',
        context: 'pre',
        success: true,
        conditions: [
          {
            variable: '传感器状态_上位机[0].NO[6]',
            display_name: 'S03未使用烧杯',
            expected: true,
            actual: false,
            satisfied: false,
          },
        ],
      },
    },
  ]);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].result, '满足');
  assert.equal(rows[0].current, 'true');
});

test('buildTaskVariableRows opc_wait finish overrides stale change state', () => {
  const rows = buildTaskVariableRows([
    {
      seq: 1,
      timestamp: 1,
      instance_id: 'inst',
      node_id: 'node_001',
      execution_id: 'exec',
      sample_id: 'Sample A',
      level: 'info',
      message: 'change',
      detail: {
        type: 'opc_wait',
        phase: 'change',
        variable: 'Robot_Home',
        expected: true,
        last_value: false,
        satisfied: false,
      },
    },
    {
      seq: 2,
      timestamp: 2,
      instance_id: 'inst',
      node_id: 'node_001',
      execution_id: 'exec',
      sample_id: 'Sample A',
      level: 'info',
      message: 'finish',
      detail: {
        type: 'opc_wait',
        phase: 'finish',
        variable: 'Robot_Home',
        expected: true,
        success: true,
        last_value: true,
      },
    },
  ]);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].result, '满足');
  assert.equal(rows[0].current, 'true');
});
