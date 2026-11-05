/**
 * 工作流 DAG（移植自 v1.0 components/WorkflowDag.tsx 的画布部分）：蛇形自动布局、
 * 区域框（循环 / 分组 / 组合调用）、可拖动节点、点选连线看两端。
 * dev 没有 v1.0 的条件 / repeat_until 控制域与物料源节点，相关投影没有移植。
 */
import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type MouseEvent, type PointerEvent } from 'react'
import { Braces, Layers3, Repeat2, RotateCcw, TriangleAlert, X } from 'lucide-react'
import type { DagEdge, DagEdgeKind, DagModel } from '../lib/workflowGraph'
import {
  LAYOUT_COLUMN_WIDTH,
  applyNodeOffsets,
  layoutDag,
  type NodeOffset,
  type PositionedNode,
} from '../lib/workflowGraphLayout'
import { statusLabel, statusTone } from '../lib/taskStatus'

const edgeKindMetadata: Record<DagEdgeKind, { label: string; description: string }> = {
  data: { label: '数据连线', description: 'Handle 连线：上游输出作为下游输入' },
  dependency: { label: '执行依赖', description: 'execution_policy.depends_on：上游结束后下游才派发' },
}

interface DragSession {
  pointerId: number
  startClientX: number
  startClientY: number
  nodeUuids: string[]
  initialOffsets: Record<string, NodeOffset>
}

function NodeCard({
  positioned,
  endpoint,
  dimmed,
  dragging,
  selected,
  onPointerDown,
  onPointerMove,
  onPointerUp,
}: {
  positioned: PositionedNode
  endpoint?: 'source' | 'target'
  dimmed: boolean
  dragging: boolean
  selected: boolean
  onPointerDown: (event: PointerEvent<HTMLElement>) => void
  onPointerMove: (event: PointerEvent<HTMLElement>) => void
  onPointerUp: (event: PointerEvent<HTMLElement>) => void
}) {
  const { node } = positioned
  return (
    <article
      className={[
        'workflow-dag-node',
        node.disabled ? 'disabled' : '',
        endpoint ? `edge-${endpoint}` : '',
        dimmed ? 'edge-dimmed' : '',
        dragging ? 'dragging' : '',
        selected ? 'selected' : '',
        node.status ? `run-${statusTone(node.status)}` : '',
      ].filter(Boolean).join(' ')}
      data-node-uuid={node.uuid}
      aria-label={[node.name, node.subtitle, node.status ? statusLabel(node.status) : ''].filter(Boolean).join('，')}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
      style={{ left: positioned.x, top: positioned.y, width: positioned.width, height: positioned.height }}
      title={`${node.name}\n${node.subtitle}\n${node.uuid}\n拖动调整位置，单击查看`}
    >
      <span className="workflow-dag-node-icon">
        <Braces size={17} />
      </span>
      <span className="workflow-dag-node-copy">
        <strong>{node.name}</strong>
        <small>{node.subtitle}</small>
        {node.status ? <i className={`node-status tone-${statusTone(node.status)}`}>{statusLabel(node.status)}</i> : null}
      </span>
      {node.disabled ? <em>已禁用</em> : null}
    </article>
  )
}

export function WorkflowDag({
  model,
  selectedNodeUuid,
  onSelectNode,
}: {
  model: DagModel
  selectedNodeUuid?: string
  onSelectNode?: (uuid: string) => void
}) {
  const regionRef = useRef<HTMLElement>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const dragRef = useRef<DragSession | null>(null)
  const movedRef = useRef(false)
  const [ranksPerBand, setRanksPerBand] = useState(4)
  const [offsets, setOffsets] = useState<Record<string, NodeOffset>>({})
  const [dragging, setDragging] = useState<Set<string>>(new Set())
  const [selectedEdgeUuid, setSelectedEdgeUuid] = useState<string>()

  const automatic = useMemo(() => layoutDag(model.nodes, model.edges, { ranksPerBand }), [model, ranksPerBand])
  const layout = useMemo(() => applyNodeOffsets(automatic, offsets), [automatic, offsets])
  const nodeByUuid = useMemo(() => new Map(model.nodes.map((node) => [node.uuid, node])), [model])
  const selectedEdge: DagEdge | undefined = selectedEdgeUuid
    ? model.edges.find((edge) => edge.uuid === selectedEdgeUuid)
    : undefined

  useEffect(() => {
    const region = regionRef.current
    if (!region) return
    const update = () => {
      if (!region.clientWidth) return
      setRanksPerBand(Math.max(2, Math.min(7, Math.floor((region.clientWidth + 76) / LAYOUT_COLUMN_WIDTH))))
    }
    update()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(update)
    observer.observe(region)
    return () => observer.disconnect()
  }, [])

  const startDrag = (event: PointerEvent<HTMLElement>, nodeUuids: string[]) => {
    if (event.button !== 0 || !nodeUuids.length) return
    event.preventDefault()
    movedRef.current = false
    dragRef.current = {
      pointerId: event.pointerId,
      startClientX: event.clientX,
      startClientY: event.clientY,
      nodeUuids,
      initialOffsets: Object.fromEntries(nodeUuids.map((uuid) => [uuid, offsets[uuid] ?? { x: 0, y: 0 }])),
    }
    setDragging(new Set(nodeUuids))
    event.currentTarget.setPointerCapture?.(event.pointerId)
  }

  const moveDrag = (event: PointerEvent<HTMLElement>) => {
    const session = dragRef.current
    if (!session || session.pointerId !== event.pointerId) return
    const dx = event.clientX - session.startClientX
    const dy = event.clientY - session.startClientY
    if (Math.abs(dx) + Math.abs(dy) > 3) movedRef.current = true
    setOffsets((current) => {
      const next = { ...current }
      session.nodeUuids.forEach((uuid) => {
        const initial = session.initialOffsets[uuid]
        next[uuid] = { x: initial.x + dx, y: initial.y + dy }
      })
      return next
    })
  }

  const endDrag = (event: PointerEvent<HTMLElement>, clickedNode?: string) => {
    const session = dragRef.current
    if (!session || session.pointerId !== event.pointerId) return
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId)
    dragRef.current = null
    setDragging(new Set())
    if (!movedRef.current && clickedNode) onSelectNode?.(clickedNode)
  }

  const selectEdge = (uuid: string) => setSelectedEdgeUuid((current) => (current === uuid ? undefined : uuid))
  const selectEdgeWithKeyboard = (event: KeyboardEvent<SVGGElement>, uuid: string) => {
    if (event.key !== 'Enter' && event.key !== ' ') return
    event.preventDefault()
    selectEdge(uuid)
  }
  const endpointFor = (uuid: string) =>
    selectedEdge?.source === uuid ? 'source' : selectedEdge?.target === uuid ? 'target' : undefined
  const clearFromBlank = (event: MouseEvent<HTMLElement>) => {
    if (!(event.target instanceof Element)) return
    if (event.target.closest('.workflow-dag-edge-control, .workflow-dag-node, .workflow-dag-group-title')) return
    setSelectedEdgeUuid(undefined)
  }

  const dataEdges = model.edges.filter((edge) => edge.kind === 'data').length
  const regions = layout.groups.length
  const warnings = [...model.warnings, ...layout.warnings]

  return (
    <section ref={regionRef} className="workflow-dag-region" aria-label="工作流 DAG">
      <div className="workflow-dag-summary">
        <span><strong>{layout.nodes.length}</strong> 个可执行节点</span>
        <span><strong>{regions}</strong> 个区域</span>
        <span><strong>{model.edges.length - dataEdges}</strong> 条执行依赖</span>
        <span><strong>{dataEdges}</strong> 条数据连线</span>
        <em>蛇形布局 · 每行最多 {layout.ranksPerBand} 层</em>
        <button
          type="button"
          className="workflow-dag-view-toggle"
          disabled={!Object.keys(offsets).length}
          onClick={() => setOffsets({})}
        >
          <RotateCcw size={13} />恢复布局
        </button>
      </div>
      <div className="workflow-dag-legend">
        <span className="dependency">执行依赖</span>
        <span className="data">数据连线</span>
        <small>指向区域的依赖已映射到区域的入口 / 出口成员</small>
      </div>
      {selectedEdge ? (
        <div className={`workflow-dag-relation ${selectedEdge.kind}`} role="status">
          <strong>{edgeKindMetadata[selectedEdge.kind].label}</strong>
          <span>
            {nodeByUuid.get(selectedEdge.source)?.name ?? selectedEdge.source} → {nodeByUuid.get(selectedEdge.target)?.name ?? selectedEdge.target}
          </span>
          <small>{edgeKindMetadata[selectedEdge.kind].description}</small>
          <button type="button" aria-label="清除连线选择" onClick={() => setSelectedEdgeUuid(undefined)}><X size={13} /></button>
        </div>
      ) : null}
      {!model.nodes.length ? <div className="workflow-dag-state">图里没有节点。</div> : (
        <div ref={scrollRef} className="workflow-dag-scroll" onClick={clearFromBlank}>
          <div className="workflow-dag-stage" style={{ width: layout.width, height: layout.height }}>
            <svg className="workflow-dag-edges" width={layout.width} height={layout.height}>
              <defs>
                {(Object.keys(edgeKindMetadata) as DagEdgeKind[]).map((kind) => (
                  <marker
                    className={`workflow-dag-arrow ${kind}`}
                    id={`workflow-dag-arrow-${kind}`}
                    key={kind}
                    viewBox="0 0 8 8"
                    refX="7"
                    refY="4"
                    markerWidth="7"
                    markerHeight="7"
                    orient="auto-start-reverse"
                  >
                    <path d="M 0 0 L 8 4 L 0 8 z" />
                  </marker>
                ))}
              </defs>
              {layout.edges.map(({ edge, source, target, path }) => (
                <g
                  className={`workflow-dag-edge-control ${edge.kind} ${edge.uuid === selectedEdgeUuid ? 'selected' : ''}`}
                  data-edge-uuid={edge.uuid}
                  key={edge.uuid}
                  role="button"
                  tabIndex={0}
                  aria-label={`${edgeKindMetadata[edge.kind].label}：${source.node.name} → ${target.node.name}`}
                  onClick={() => selectEdge(edge.uuid)}
                  onKeyDown={(event) => selectEdgeWithKeyboard(event, edge.uuid)}
                >
                  <path className="workflow-dag-edge-hit" d={path} aria-hidden="true" />
                  <path className="workflow-dag-edge" d={path} markerEnd={`url(#workflow-dag-arrow-${edge.kind})`} aria-hidden="true" />
                </g>
              ))}
            </svg>
            {layout.groups.map((group) => (
              <div
                className={`workflow-dag-group ${group.node.type === 'loop' ? 'loop' : ''} ${group.children.some((child) => dragging.has(child.node.uuid)) ? 'dragging' : ''}`}
                key={group.node.uuid}
                data-node-uuid={group.node.uuid}
                role="group"
                aria-label={`区域：${group.node.name}`}
              >
                {group.frames.map((frame, index) => (
                  <div
                    className={`workflow-dag-group-frame ${selectedNodeUuid === group.node.uuid ? 'selected' : ''}`}
                    key={`${group.node.uuid}-${index}`}
                    style={{ left: frame.x, top: frame.y, width: frame.width, height: frame.height }}
                  >
                    <div
                      className="workflow-dag-group-title"
                      onPointerDown={(event) => startDrag(event, group.children.map((child) => child.node.uuid))}
                      onPointerMove={moveDrag}
                      onPointerUp={(event) => endDrag(event, group.node.uuid)}
                      onPointerCancel={(event) => endDrag(event)}
                      title={`${group.node.name}\n${group.node.subtitle}\n${group.node.uuid}`}
                    >
                      {group.node.type === 'loop' ? <Repeat2 size={14} /> : <Layers3 size={14} />}
                      <strong>{group.node.name}</strong>
                      <small>{group.node.status ? statusLabel(group.node.status) : group.node.type}</small>
                    </div>
                  </div>
                ))}
              </div>
            ))}
            {layout.nodes.map((positioned) => {
              const endpoint = endpointFor(positioned.node.uuid)
              const group = positioned.groupUuid ? automatic.groups.find((item) => item.node.uuid === positioned.groupUuid) : undefined
              return (
                <NodeCard
                  key={positioned.node.uuid}
                  positioned={positioned}
                  endpoint={endpoint}
                  dimmed={Boolean(selectedEdge) && !endpoint}
                  dragging={dragging.has(positioned.node.uuid)}
                  selected={selectedNodeUuid === positioned.node.uuid}
                  onPointerDown={(event) => startDrag(event, event.altKey && group ? group.children.map((child) => child.node.uuid) : [positioned.node.uuid])}
                  onPointerMove={moveDrag}
                  onPointerUp={(event) => endDrag(event, positioned.node.uuid)}
                />
              )
            })}
          </div>
        </div>
      )}
      {warnings.length ? (
        <div className="workflow-dag-warning"><TriangleAlert size={14} />{[...new Set(warnings)].join(' ')}</div>
      ) : null}
    </section>
  )
}
