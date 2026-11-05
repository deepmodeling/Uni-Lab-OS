import { describe, expect, it } from 'vitest'
import { countByType, eventSubject, matchesFilter, prependBounded, toEntry, type EventFilter } from './eventLog'

const filter = (overrides: Partial<EventFilter> = {}): EventFilter => ({ types: new Set(), text: '', hideLogNotices: false, ...overrides })

describe('eventSubject', () => {
  it('finds the aggregate for each workflow event payload shape', () => {
    expect(eventSubject({ workflow_task_uuid: 't1' })).toEqual({ kind: 'task', uuid: 't1' })
    expect(eventSubject({ uuid: 'mc', task_uuid: 't2', workflow_task_uuid: 't2' })).toEqual({ kind: 'task', uuid: 't2' })
    expect(eventSubject({ workflow_node_job_uuid: 'j', workflow_node_run_uuid: 'r', attempt_no: 1 })).toEqual({ kind: 'job', uuid: 'j' })
    expect(eventSubject({ workflow_uuid: 'w', workflow_revision: 3 })).toEqual({ kind: 'workflow', uuid: 'w' })
    expect(eventSubject({ reason: 'cursor_ahead' })).toBeUndefined()
    expect(eventSubject('text')).toBeUndefined()
  })
})

describe('toEntry', () => {
  it('parses JSON data and does not attribute the carried-over id to transient log notices', () => {
    const persistent = toEntry({ id: '9', event: 'workflow.task.changed', data: '{"workflow_task_uuid":"t"}' }, 1, 100)
    expect(persistent).toMatchObject({ id: '9', data: { workflow_task_uuid: 't' }, subject: { kind: 'task', uuid: 't' } })
    const notice = toEntry({ id: '9', event: 'runtime.logs.changed', data: '{"source_ids":[]}' }, 2, 101)
    expect(notice.id).toBeUndefined()
    expect(toEntry({ event: 'message', data: 'not json' }, 3, 102).data).toBe('not json')
  })
})

describe('buffer and filter', () => {
  const entries = [
    toEntry({ id: '1', event: 'workflow.task.changed', data: '{"workflow_task_uuid":"aaa"}' }, 1, 0),
    toEntry({ event: 'runtime.logs.changed', data: '{"source_ids":["host"]}' }, 2, 0),
    toEntry({ id: '2', event: 'workflow.node_run.changed', data: '{"workflow_task_uuid":"bbb","workflow_node_uuid":"n"}' }, 3, 0),
  ]

  it('keeps the newest first and caps the buffer', () => {
    const buffer = prependBounded([], entries, 2)
    expect(buffer.map((entry) => entry.seq)).toEqual([3, 2])
    expect(prependBounded(buffer, [], 2)).toBe(buffer)
  })

  it('filters by type, text and the log-notice toggle', () => {
    expect(entries.filter((entry) => matchesFilter(entry, filter({ hideLogNotices: true }))).map((entry) => entry.seq)).toEqual([1, 3])
    expect(entries.filter((entry) => matchesFilter(entry, filter({ types: new Set(['workflow.node_run.changed']) }))).map((entry) => entry.seq)).toEqual([3])
    expect(entries.filter((entry) => matchesFilter(entry, filter({ text: 'AAA' }))).map((entry) => entry.seq)).toEqual([1])
    expect(countByType(entries)).toEqual([
      ['runtime.logs.changed', 1],
      ['workflow.node_run.changed', 1],
      ['workflow.task.changed', 1],
    ])
  })
})
