import { describe, expect, it } from 'vitest'
import type { WorkflowTask } from '../api/types'
import { canEditInput, commandAvailability, statusTone } from './taskStatus'

type TaskState = Pick<WorkflowTask, 'status' | 'execution_kind' | 'run_mode' | 'control_status' | 'control_revision'>

const task = (overrides: Partial<TaskState> = {}): TaskState => ({
  status: 'running',
  execution_kind: 'workflow',
  run_mode: 'normal',
  control_status: 'active',
  control_revision: 2,
  ...overrides,
})

const enabled = (state: TaskState, runs?: { status: string }[]) =>
  Object.entries(commandAvailability(state, runs))
    .filter(([, value]) => value.enabled)
    .map(([type]) => type)
    .sort()

describe('commandAvailability (mirrors WorkflowStore.apply_task_command)', () => {
  it('running in normal mode: pause and cancel only', () => {
    expect(enabled(task())).toEqual(['cancel', 'pause'])
  })

  it('paused step task: step, resume and cancel', () => {
    expect(enabled(task({ run_mode: 'step', control_status: 'paused' }))).toEqual(['cancel', 'resume', 'step'])
  })

  it('step task with an in-flight step: resume, pause and cancel, no second step', () => {
    const state = task({ run_mode: 'step', control_status: 'active' })
    expect(enabled(state)).toEqual(['cancel', 'pause', 'resume'])
    expect(commandAvailability(state).step.reason).toMatch(/单步/)
  })

  it('unresolved node blocks step / resume but not pause / cancel', () => {
    const state = task({ run_mode: 'step', control_status: 'paused' })
    expect(enabled(state, [{ status: 'succeeded' }, { status: 'execution_unknown' }])).toEqual(['cancel'])
    // 已暂停时 pause 本身不可用
    expect(commandAvailability(state).pause.enabled).toBe(false)
  })

  it('waiting for intervention: pause still allowed, step / resume are not', () => {
    expect(enabled(task({ run_mode: 'step', control_status: 'waiting_intervention' }))).toEqual(['cancel', 'pause'])
  })

  it('all nodes finished: only cancel remains while the task is still running', () => {
    expect(enabled(task(), [{ status: 'succeeded' }, { status: 'skipped' }])).toEqual(['cancel'])
  })

  it('terminal, canceling and ad-hoc tasks', () => {
    expect(enabled(task({ status: 'succeeded' }))).toEqual([])
    expect(commandAvailability(task({ status: 'canceling' })).cancel.reason).toBe('已在取消中')
    expect(enabled(task({ execution_kind: 'ad_hoc_device_action' }))).toEqual(['cancel'])
  })

  it('list projections without control_revision cannot issue commands', () => {
    expect(enabled(task({ control_revision: undefined }))).toEqual([])
  })
})

describe('canEditInput / statusTone', () => {
  it('only inside the paused window of a live workflow task', () => {
    expect(canEditInput({ status: 'running', control_status: 'paused', execution_kind: 'workflow' })).toBe(true)
    expect(canEditInput({ status: 'running', control_status: 'active', execution_kind: 'workflow' })).toBe(false)
    expect(canEditInput({ status: 'failed', control_status: 'paused', execution_kind: 'workflow' })).toBe(false)
    expect(canEditInput({ status: 'pending', control_status: 'paused', execution_kind: 'ad_hoc_device_action' })).toBe(false)
  })

  it('maps statuses to tones', () => {
    expect(statusTone('succeeded')).toBe('ok')
    expect(statusTone('execution_unknown')).toBe('bad')
    expect(statusTone('waiting')).toBe('warn')
    expect(statusTone('held')).toBe('info')
    expect(statusTone('something-new')).toBe('muted')
  })
})
