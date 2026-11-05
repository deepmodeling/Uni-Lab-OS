/** 可搜索的物料 / 库位层级目录（移植自 v1.0 components/MaterialHierarchyTree.tsx，只读）。 */
import { useEffect, useMemo, useState, type CSSProperties } from 'react'
import { ChevronRight, Search } from 'lucide-react'
import type { MaterialAggregate } from '../api/types'
import {
  ancestorsOf,
  buildMaterialTree,
  countTree,
  filterMaterialTree,
  materialLabel,
  materialUuid,
  type MaterialTreeNode,
} from '../lib/materialTree'

export function MaterialHierarchyTree({
  materials,
  selectedId,
  onSelect,
}: {
  materials: MaterialAggregate[]
  selectedId?: string
  onSelect: (uuid: string) => void
}) {
  const roots = useMemo(() => buildMaterialTree(materials), [materials])
  const [query, setQuery] = useState('')
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set(roots.map((node) => materialUuid(node.material))))
  useEffect(() => {
    if (!selectedId) return
    const ancestors = ancestorsOf(materials, selectedId)
    setExpanded((current) => (ancestors.every((uuid) => current.has(uuid)) ? current : new Set([...current, ...ancestors])))
  }, [selectedId, materials])
  const visible = useMemo(() => filterMaterialTree(roots, query), [roots, query])
  const toggle = (uuid: string) =>
    setExpanded((current) => {
      const next = new Set(current)
      if (next.has(uuid)) next.delete(uuid)
      else next.add(uuid)
      return next
    })

  return (
    <aside className="electron-material-tree" aria-label="物料目录">
      <header>
        <div>
          <strong>物料树</strong>
          <span>({query.trim() ? `${countTree(visible)} / ${materials.length}` : materials.length})</span>
        </div>
        <button type="button" className="link-button" onClick={() => setExpanded(new Set(materials.map(materialUuid)))}>全部展开</button>
        <button type="button" className="link-button" onClick={() => setExpanded(new Set())}>收起</button>
      </header>
      <label>
        <Search size={16} />
        <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="名称、resource_id、uuid、库位" />
      </label>
      <div className="electron-tree-legend" aria-label="库位状态说明">
        <span><i className="occupied" />有物料</span>
        <span><i className="empty" />空库位</span>
      </div>
      <div className="electron-tree-rows" role="tree">
        {visible.map((node) => (
          <TreeRow
            key={materialUuid(node.material)}
            node={node}
            depth={0}
            queryActive={Boolean(query.trim())}
            expanded={expanded}
            selectedId={selectedId}
            onSelect={onSelect}
            onToggle={toggle}
          />
        ))}
        {!visible.length ? <p className="tree-empty">{materials.length ? '没有匹配的物料' : '物料权威里还没有物料'}</p> : null}
      </div>
    </aside>
  )
}

function TreeRow({
  node,
  depth,
  queryActive,
  expanded,
  selectedId,
  onSelect,
  onToggle,
}: {
  node: MaterialTreeNode
  depth: number
  queryActive: boolean
  expanded: Set<string>
  selectedId?: string
  onSelect: (uuid: string) => void
  onToggle: (uuid: string) => void
}) {
  const uuid = materialUuid(node.material)
  const label = materialLabel(node.material)
  const hasContent = node.children.length > 0 || node.sites.length > 0
  const open = hasContent && (queryActive || expanded.has(uuid))
  const childByUuid = new Map(node.children.map((child) => [materialUuid(child.material), child]))
  const placed = new Set(node.sites.map((site) => site.occupied_material_uuid).filter(Boolean))
  const unplaced = node.children.filter((child) => !placed.has(materialUuid(child.material)))
  const rowProps = { depth: depth + 1, queryActive, expanded, selectedId, onSelect, onToggle }
  return (
    <>
      <div
        className={`electron-tree-row ${selectedId === uuid ? 'selected' : ''}`}
        role="treeitem"
        aria-level={depth + 1}
        aria-expanded={hasContent ? open : undefined}
        style={{ '--tree-depth': depth } as CSSProperties}
      >
        <span className="tree-grip">⠿</span>
        {hasContent ? (
          <button type="button" className={`tree-toggle ${open ? 'open' : ''}`} aria-label={`${open ? '收起' : '展开'} ${label.primary}`} onClick={() => onToggle(uuid)}>
            <ChevronRight size={14} />
          </button>
        ) : <span className="tree-toggle-spacer" />}
        <button type="button" className="tree-label" title={`${label.text}\n${uuid}`} onClick={() => onSelect(uuid)}>
          <strong>{label.primary}</strong>
          {label.secondary ? <small>{label.secondary}</small> : null}
        </button>
        <span />
      </div>
      {open ? (
        <>
          {node.sites.map((site) => {
            const occupant = site.occupied_material_uuid ? childByUuid.get(site.occupied_material_uuid) : undefined
            if (occupant) return <TreeRow key={`${site.site_uuid}:${materialUuid(occupant.material)}`} node={occupant} {...rowProps} />
            return (
              <div
                key={site.site_uuid}
                className="electron-tree-row site-row"
                role="treeitem"
                aria-level={depth + 2}
                style={{ '--tree-depth': depth + 1 } as CSSProperties}
                title={`${site.label}\n${site.site_uuid}${site.occupied_material_uuid ? `\n占用：${site.occupied_material_uuid}（不在列表里）` : ''}`}
              >
                <span className="tree-grip" />
                <span className="tree-toggle-spacer" />
                <span className="tree-label"><strong>{site.label || String(site.site_index)}</strong></span>
                <i className={`site-dot ${site.occupied_material_uuid ? 'occupied' : 'empty'}`} />
              </div>
            )
          })}
          {unplaced.map((child) => <TreeRow key={materialUuid(child.material)} node={child} {...rowProps} />)}
        </>
      ) : null}
    </>
  )
}
