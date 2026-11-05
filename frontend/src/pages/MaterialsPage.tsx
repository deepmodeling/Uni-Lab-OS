/** 物料：只读层级树 + 选中物料的原始 JSON（GET /materials/instances/{uuid}，可选整棵子树）。 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'
import { api } from '../api'
import { MaterialHierarchyTree } from '../components/MaterialHierarchyTree'
import { Button, Degraded, EmptyState, JsonView, PageHeader, Panel, PanelHeader, PropertyList, RefreshButton, Uuid } from '../components/ui'
import { formatTime } from '../lib/format'
import { materialLabel } from '../lib/materialTree'

export function MaterialsPage() {
  const [params, setParams] = useSearchParams()
  const selected = params.get('material') ?? undefined
  const materials = useQuery({ queryKey: ['materials'], queryFn: () => api.materials.list(), refetchInterval: 10000 })
  const select = (uuid: string) => {
    const next = new URLSearchParams(params)
    next.set('material', uuid)
    setParams(next, { replace: true })
  }

  return (
    <>
      <PageHeader
        eyebrow="MATERIALS"
        title="物料"
        description="GET /api/v1/materials/instances：结构父子按 parent_material_uuid，库位占用按 sites[].occupied_material_uuid。只读。"
        actions={<RefreshButton onClick={() => void materials.refetch()} busy={materials.isFetching} />}
      />
      {materials.error ? <Degraded error={materials.error} /> : null}
      <div className="materials-layout">
        {materials.data ? <MaterialHierarchyTree materials={materials.data} selectedId={selected} onSelect={select} /> : <Panel><EmptyState title="正在读取物料" /></Panel>}
        {selected ? <MaterialDetail uuid={selected} /> : <Panel><EmptyState title="选择一个物料" description="左侧树里点物料名查看它的权威记录。" /></Panel>}
      </div>
    </>
  )
}

function MaterialDetail({ uuid }: { uuid: string }) {
  const [showTree, setShowTree] = useState(false)
  const material = useQuery({ queryKey: ['material', uuid], queryFn: () => api.materials.get(uuid) })
  const tree = useQuery({ queryKey: ['material', uuid, 'tree'], queryFn: () => api.materials.tree(uuid), enabled: showTree })
  if (material.error) return <Panel><Degraded error={material.error} /></Panel>
  if (!material.data) return <Panel><EmptyState title="正在读取" /></Panel>
  const identity = material.data.material
  const label = materialLabel(material.data)
  return (
    <Panel>
      <PanelHeader
        title={label.primary}
        description={<><code>{identity.material_uuid}</code> · {label.secondary}</>}
        action={<><Button onClick={() => setShowTree((current) => !current)}>{showTree ? '隐藏子树' : '读取子树 /tree'}</Button><RefreshButton onClick={() => void material.refetch()} busy={material.isFetching} /></>}
      />
      <div className="panel-body stack">
        <PropertyList
          items={[
            ['resource_id', <code>{identity.resource_id}</code>],
            ['name / display_name', `${identity.name} / ${identity.display_name || '—'}`],
            ['resource_type / class_name', `${identity.resource_type || '—'} / ${identity.class_name || '—'}`],
            ['template', `${identity.template_name} (${identity.template_uuid})`],
            ['parent', <Uuid value={identity.parent_material_uuid} length={36} />],
            ['lifecycle', identity.lifecycle_status || '—'],
            ['barcode', identity.barcode || '—'],
            ['version / state_hash', `${identity.version} / ${material.data.state_hash.slice(0, 16)}…`],
            ['updated', formatTime(identity.updated_at_ms)],
          ]}
        />
        {material.data.sites?.length ? (
          <div className="table-scroll">
            <table className="data-table static">
              <thead><tr><th>库位</th><th>index</th><th>占用</th><th>site_uuid</th></tr></thead>
              <tbody>
                {material.data.sites.map((site) => (
                  <tr key={site.site_uuid}>
                    <td>{site.label}</td>
                    <td>{String(site.site_index)}</td>
                    <td><Uuid value={site.occupied_material_uuid} /></td>
                    <td><Uuid value={site.site_uuid} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : null}
        <JsonView title="GET /materials/instances/{uuid}" value={material.data} maxHeight={520} />
        {showTree ? (tree.error ? <Degraded error={tree.error} /> : <JsonView title="GET /materials/instances/{uuid}/tree" value={tree.data ?? '读取中…'} maxHeight={520} />) : null}
      </div>
    </Panel>
  )
}
