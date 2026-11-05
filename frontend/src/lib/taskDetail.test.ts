import { describe, expect, it } from 'vitest'
import type { ErrorDecisionReport, WorkflowNodeJob, WorkflowNodeRun, WorkflowTask } from '../api/types'
import { countByStatus, decisionsForTask, flattenAttempts, nodeIndex, sortNodeRuns, statusByNode } from './taskDetail'

const job = (uuid: string, attempt: number, started: string, status = 'succeeded') =>
  ({ uuid, attempt_no: attempt, started_at: started, create_time: started, status }) as WorkflowNodeJob
const run = (uuid: string, node: string, index: number, attempts: WorkflowNodeJob[], status = 'succeeded') =>
  ({
    uuid,
    workflow_node_uuid: node,
    topological_index: index,
    status,
    attempts,
    current_job_uuid: attempts.at(-1)?.uuid ?? null,
  }) as WorkflowNodeRun

const runs = [
  run('r2', 'n2', 1, [job('j2a', 1, '2026-11-05T01:00:02Z', 'failed'), job('j2b', 2, '2026-11-05T01:00:05Z')]),
  run('r1', 'n1', 0, [job('j1', 1, '2026-11-05T01:00:00Z')]),
  run('r3', 'n3', 2, [], 'pending'),
]

describe('task detail shaping', () => {
  it('sorts node runs topologically and flattens attempts newest first', () => {
    expect(sortNodeRuns(runs).map((item) => item.uuid)).toEqual(['r1', 'r2', 'r3'])
    expect(flattenAttempts(runs).map((row) => [row.job.uuid, row.current])).toEqual([
      ['j2b', true],
      ['j2a', false],
      ['j1', true],
    ])
  })

  it('counts statuses and maps node → status', () => {
    expect(countByStatus(runs)).toEqual([['succeeded', 2], ['pending', 1]])
    expect(statusByNode(runs).get('n3')).toBe('pending')
  })

  it('picks the error decisions that belong to the task by task_id or job id', () => {
    const decisions = [
      { decision_id: 'd1', task_id: 'task', job_id: 'other', options: [] },
      { decision_id: 'd2', job_id: 'j2a', options: [] },
      { decision_id: 'd3', task_id: 'someone-else', job_id: 'x', options: [] },
    ] as ErrorDecisionReport[]
    expect(decisionsForTask(decisions, 'task', runs).map((item) => item.decision_id)).toEqual(['d1', 'd2'])
  })

  it('indexes the frozen snapshot nodes', () => {
    const task = {
      workflow_snapshot: {
        nodes: [
          { uuid: 'n1', name: '复位', type: 'device_action', action_name: 'reset', meta_data: { target_device_id: 'reactor' } },
          { uuid: 'n2', name: '', type: 'loop', parent_uuid: 'n1', meta_data: {} },
        ],
      },
    } as unknown as WorkflowTask
    const index = nodeIndex(task)
    expect(index.get('n1')).toEqual({ name: '复位', type: 'device_action', action: 'reset', deviceId: 'reactor', parentUuid: undefined })
    expect(index.get('n2')?.name).toBe('n2')
    expect(nodeIndex(undefined).size).toBe(0)
  })
})
