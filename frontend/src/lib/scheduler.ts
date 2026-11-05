/**
 * `/scheduler/resources` 快照整形：持有的锁、等待中的请求（把 blockers 展开成
 * 「谁、属于哪个任务、挡住了哪几把锁」）与交接。
 */
import type {
  LockHandoffRecord,
  LockIdentifier,
  LockOwnership,
  ResourceRequestRecord,
  SchedulerResourceSnapshot,
} from '../api/types'

export function lockKey(identifier: LockIdentifier): string {
  return identifier.kind === 'action'
    ? `action:${identifier.device_id ?? ''}/${identifier.action_name ?? ''}`
    : `material:${identifier.material_uuid ?? ''}`
}

/** `materialNames` 可选：material_uuid → 展示名（来自物料列表）。 */
export function lockLabel(identifier: LockIdentifier, materialNames?: Map<string, string>): string {
  if (identifier.kind === 'action') return `${identifier.device_id ?? '?'} / ${identifier.action_name ?? '?'}`
  const uuid = identifier.material_uuid ?? '?'
  const name = materialNames?.get(uuid)
  return name ? `物料 ${name}` : `物料 ${uuid}`
}

export interface HeldLockRow {
  key: string
  ownership: LockOwnership
  /** owner 对应请求所属任务；交接后或请求已清理时可能查不到。 */
  taskUuid?: string
  /** 有几个等待请求排在这把锁后面。 */
  waiters: number
}

export interface BlockerDetail {
  ownerUuid: string
  taskUuid?: string
  /** 该 owner 持有、且本请求也要的锁。 */
  locks: LockIdentifier[]
}

export interface WaitingRow {
  request: ResourceRequestRecord
  blockers: BlockerDetail[]
  /** 请求的锁里目前没人持有的（等待原因是排队顺序而不是冲突）。 */
  freeLocks: LockIdentifier[]
}

export interface SchedulerView {
  sequence: number
  held: HeldLockRow[]
  waiting: WaitingRow[]
  holding: ResourceRequestRecord[]
  pendingHandoffs: LockHandoffRecord[]
  finishedHandoffs: LockHandoffRecord[]
  /** 非 waiting / held 的请求条数（released / canceled，快照里一般很少）。 */
  settledRequests: number
}

export function shapeScheduler(snapshot: SchedulerResourceSnapshot): SchedulerView {
  const requests = snapshot.requests ?? []
  const ownerships = snapshot.ownerships ?? []
  const handoffs = snapshot.handoffs ?? []

  const taskByOwner = new Map<string, string>()
  for (const request of requests) taskByOwner.set(request.owner_uuid, request.task_uuid)

  const ownershipByKey = new Map(ownerships.map((ownership) => [lockKey(ownership.identifier), ownership]))
  const waitingRequests = requests
    .filter((request) => request.status === 'waiting')
    .sort((left, right) => left.created_sequence - right.created_sequence)

  const waitersByKey = new Map<string, number>()
  for (const request of waitingRequests) {
    for (const identifier of request.identifiers) {
      const key = lockKey(identifier)
      waitersByKey.set(key, (waitersByKey.get(key) ?? 0) + 1)
    }
  }

  const held: HeldLockRow[] = ownerships
    .map((ownership) => {
      const key = lockKey(ownership.identifier)
      return { key, ownership, taskUuid: taskByOwner.get(ownership.owner_uuid), waiters: waitersByKey.get(key) ?? 0 }
    })
    .sort((left, right) => left.ownership.acquired_sequence - right.ownership.acquired_sequence)

  const waiting: WaitingRow[] = waitingRequests.map((request) => {
    const byOwner = new Map<string, LockIdentifier[]>()
    const freeLocks: LockIdentifier[] = []
    for (const identifier of request.identifiers) {
      const ownership = ownershipByKey.get(lockKey(identifier))
      if (!ownership || ownership.owner_uuid === request.owner_uuid) {
        if (!ownership) freeLocks.push(identifier)
        continue
      }
      byOwner.set(ownership.owner_uuid, [...(byOwner.get(ownership.owner_uuid) ?? []), identifier])
    }
    // 服务端给的 blockers 为准；快照之间锁可能刚交接，这里补上服务端列出但本地没算到的 owner
    for (const owner of request.blockers ?? []) {
      if (!byOwner.has(owner)) byOwner.set(owner, [])
    }
    const blockers = [...byOwner.entries()]
      .map(([ownerUuid, locks]) => ({ ownerUuid, taskUuid: taskByOwner.get(ownerUuid), locks }))
      .sort((left, right) => left.ownerUuid.localeCompare(right.ownerUuid))
    return { request, blockers, freeLocks }
  })

  const holding = requests
    .filter((request) => request.status === 'held')
    .sort((left, right) => left.created_sequence - right.created_sequence)

  return {
    sequence: snapshot.sequence,
    held,
    waiting,
    holding,
    pendingHandoffs: handoffs.filter((handoff) => handoff.status === 'pending'),
    finishedHandoffs: handoffs.filter((handoff) => handoff.status !== 'pending'),
    settledRequests: requests.length - waitingRequests.length - holding.length,
  }
}
