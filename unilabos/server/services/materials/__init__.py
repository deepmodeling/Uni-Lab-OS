"""materials.db 域的领域服务（物料 + 拓扑图 + 快照对比）。

- ``core``：MaterialsService，物料/位点/库存事务权威；
- ``graph``：GraphService，拓扑边与 lab graph 快照；
- ``snapshot``：物料快照的规范化与逐 section 对比。
"""

from unilabos.server.services.materials.core import (
    InsufficientInventoryError,
    MaterialConflictError,
    MaterialNoChangeError,
    MaterialNotFoundError,
    MaterialTransferSyncError,
    MaterialValidationError,
    MaterialsService,
    MaterialsServiceError,
    RejectedMutationError,
    SITE_INGRESS_RESERVED,
    material_link_uuid,
)
from unilabos.server.services.materials.graph import (
    GRAPH_NAMESPACE,
    GraphError,
    GraphService,
    graph_uuid_for_name,
    link_payload,
    validate_graph_payload,
)
from unilabos.server.services.materials.snapshot import (
    compare_material_snapshot,
    material_sections,
    site_semantic,
    snapshot_state_hash,
)

__all__ = [
    "GRAPH_NAMESPACE",
    "GraphError",
    "GraphService",
    "InsufficientInventoryError",
    "MaterialConflictError",
    "MaterialNoChangeError",
    "MaterialNotFoundError",
    "MaterialTransferSyncError",
    "MaterialValidationError",
    "MaterialsService",
    "MaterialsServiceError",
    "RejectedMutationError",
    "SITE_INGRESS_RESERVED",
    "compare_material_snapshot",
    "graph_uuid_for_name",
    "link_payload",
    "material_link_uuid",
    "material_sections",
    "site_semantic",
    "snapshot_state_hash",
    "validate_graph_payload",
]
