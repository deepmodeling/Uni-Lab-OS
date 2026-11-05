import { describe, expect, it } from 'vitest'
import type { SchedulerResourceSnapshot } from '../api/types'
import lockDemo from '../test/fixtures/scheduler-lock-demo.json'
import { lockKey, lockLabel, shapeScheduler } from './scheduler'

// 真实快照：LabDeviceLockDemo 同时提交 occupy×2 与三个 process_plate（P1 两次、P2 一次）
const snapshot = lockDemo as SchedulerResourceSnapshot

describe('shapeScheduler on a LockDemo snapshot', () => {
  const view = shapeScheduler(snapshot)

  it('lists held locks with their task and queue length', () => {
    expect(view.held.map((row) => row.key)).toEqual([
      'action:lock_probe_a/occupy',
      'material:5a0c4e1d-2b7f-4c93-9e61-000000003101',
      'material:5a0c4e1d-2b7f-4c93-9e61-000000003102',
    ])
    expect(view.held[0].taskUuid).toBe('03e7f891-ccce-4a29-a8b5-8975c1d008b7')
    expect(view.held.map((row) => row.waiters)).toEqual([1, 1, 0])
  })

  it('expands blockers into the owner, its task and the conflicting locks', () => {
    expect(view.waiting).toHaveLength(2)
    const [occupy, plate] = view.waiting
    expect(occupy.request.task_uuid).toBe('a09e91c5-a1c9-4076-a5e7-2dbfc418de22')
    expect(occupy.blockers).toEqual([
      {
        ownerUuid: '4e44be84-eeb7-4466-abfd-4df0d14e7afd',
        taskUuid: '03e7f891-ccce-4a29-a8b5-8975c1d008b7',
        locks: [{ kind: 'action', device_id: 'lock_probe_a', action_name: 'occupy' }],
      },
    ])
    expect(plate.blockers[0].taskUuid).toBe('660ac0ba-ab67-4d13-9b3b-3a12b85d25c9')
    expect(plate.freeLocks).toEqual([])
  })

  it('separates held requests and settled ones', () => {
    expect(view.holding.map((request) => request.task_uuid)).toEqual([
      '03e7f891-ccce-4a29-a8b5-8975c1d008b7',
      '660ac0ba-ab67-4d13-9b3b-3a12b85d25c9',
      'ba1d9e62-4373-44c8-9b23-a72cac6f6be7',
    ])
    expect(view.settledRequests).toBe(1)
    expect(view.sequence).toBe(11)
  })
})

describe('shapeScheduler edge cases', () => {
  it('keeps server blockers it cannot match locally and reports queue-only waits', () => {
    const view = shapeScheduler({
      sequence: 3,
      ownerships: [],
      handoffs: [
        { handoff_uuid: 'h2', from_owner_uuid: 'a', to_owner_uuid: 'b', identifiers: [], status: 'completed', created_sequence: 1, updated_sequence: 2, version: 1 },
        { handoff_uuid: 'h1', from_owner_uuid: 'a', to_owner_uuid: 'c', identifiers: [], status: 'pending', created_sequence: 2, updated_sequence: 2, version: 1 },
      ],
      requests: [
        {
          request_uuid: 'r1',
          owner_uuid: 'o1',
          task_uuid: 't1',
          identifiers: [{ kind: 'material', material_uuid: 'm1' }],
          status: 'waiting',
          blockers: ['ghost'],
          created_sequence: 1,
          updated_sequence: 1,
          version: 1,
        },
      ],
    })
    expect(view.waiting[0].blockers).toEqual([{ ownerUuid: 'ghost', taskUuid: undefined, locks: [] }])
    expect(view.waiting[0].freeLocks).toEqual([{ kind: 'material', material_uuid: 'm1' }])
    expect(view.pendingHandoffs.map((handoff) => handoff.handoff_uuid)).toEqual(['h1'])
    expect(view.finishedHandoffs.map((handoff) => handoff.handoff_uuid)).toEqual(['h2'])
  })

  it('labels locks, preferring material names when known', () => {
    expect(lockKey({ kind: 'action', device_id: 'd', action_name: 'a' })).toBe('action:d/a')
    expect(lockLabel({ kind: 'action', device_id: 'd', action_name: 'a' })).toBe('d / a')
    expect(lockLabel({ kind: 'material', material_uuid: 'm1' }, new Map([['m1', 'Plate P1']]))).toBe('物料 Plate P1')
    expect(lockLabel({ kind: 'material', material_uuid: 'm2' })).toBe('物料 m2')
  })
})
