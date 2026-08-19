"""物料快照的规范化、逐 section 对比和变更计划。"""

from __future__ import annotations

from typing import Any

from unilabos.server.protocol.common import canonical_hash
from unilabos.server.protocol.materials import (
    MaterialAggregateRead,
    MaterialSnapshot,
    MaterialSnapshotChange,
    MaterialSnapshotDiff,
    MaterialTreeRead,
    SiteRead,
)


_IDENTITY_VOLATILE = {"created_at_ms", "updated_at_ms", "deleted_at_ms", "version"}
_DATA_VOLATILE = {
    "content_version",
    "state_hash",
    "updated_at_ms",
    "version",
    "source_event_uuid",
    "source_job_uuid",
    "source_command_uuid",
    "observed_at_ms",
}
_SITE_VOLATILE = {
    "changed_by_job_uuid",
    "changed_by_command_uuid",
    "changed_at_ms",
    "created_at_ms",
    "updated_at_ms",
    "deleted_at_ms",
    "version",
}


def _semantic(model: Any, excluded: set[str] | None = None) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude=excluded or set(), exclude_none=False)


def material_sections(node: MaterialAggregateRead) -> dict[str, dict[str, Any]]:
    """返回真正参与 snapshot 比较的 Material sections。"""

    return {
        "identity": _semantic(node.material, _IDENTITY_VOLATILE),
        "position": _semantic(node.position),
        "data": _semantic(node.data, _DATA_VOLATILE),
    }


def site_semantic(site: SiteRead) -> dict[str, Any]:
    return _semantic(site, _SITE_VOLATILE)


def snapshot_state_hash(value: MaterialTreeRead | MaterialSnapshot) -> str:
    nodes = sorted(value.nodes, key=lambda item: item.material.material_uuid)
    return canonical_hash(
        [
            {
                "material_uuid": node.material.material_uuid,
                "sections": material_sections(node),
                "sites": [
                    site_semantic(site)
                    for site in sorted(node.sites, key=lambda item: item.site_uuid)
                ],
            }
            for node in nodes
        ]
    )


def _changed_fields(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    return sorted(
        key for key in before.keys() | after.keys() if before.get(key) != after.get(key)
    )


def compare_material_snapshot(
    authoritative: MaterialTreeRead, observed: MaterialSnapshot
) -> MaterialSnapshotDiff:
    """按 Material section 和 Site 聚合生成确定性 diff。"""

    if authoritative.root_material_uuid != observed.root_material_uuid:
        raise ValueError("snapshot root does not match authoritative tree")
    current_nodes = {
        item.material.material_uuid: item for item in authoritative.nodes
    }
    observed_nodes = {item.material.material_uuid: item for item in observed.nodes}
    if len(observed_nodes) != len(observed.nodes):
        raise ValueError("snapshot contains duplicate material_uuid")

    changes: list[MaterialSnapshotChange] = []
    for material_uuid in sorted(current_nodes.keys() | observed_nodes.keys()):
        before_node = current_nodes.get(material_uuid)
        after_node = observed_nodes.get(material_uuid)
        if before_node is None or after_node is None:
            changes.append(
                MaterialSnapshotChange(
                    aggregate_type="material",
                    aggregate_uuid=material_uuid,
                    section="topology",
                    before_hash=(
                        canonical_hash(material_sections(before_node))
                        if before_node is not None
                        else None
                    ),
                    after_hash=(
                        canonical_hash(material_sections(after_node))
                        if after_node is not None
                        else None
                    ),
                    changed_fields=["presence"],
                )
            )
            continue
        before_sections = material_sections(before_node)
        after_sections = material_sections(after_node)
        for section in ("identity", "position", "data"):
            before = before_sections[section]
            after = after_sections[section]
            if before != after:
                changes.append(
                    MaterialSnapshotChange(
                        aggregate_type="material",
                        aggregate_uuid=material_uuid,
                        section=section,
                        before_hash=canonical_hash(before),
                        after_hash=canonical_hash(after),
                        changed_fields=_changed_fields(before, after),
                    )
                )

        before_sites = {site.site_uuid: site for site in before_node.sites}
        after_sites = {site.site_uuid: site for site in after_node.sites}
        if len(after_sites) != len(after_node.sites):
            raise ValueError("snapshot contains duplicate site_uuid")
        for site_uuid in sorted(before_sites.keys() | after_sites.keys()):
            before_site = before_sites.get(site_uuid)
            after_site = after_sites.get(site_uuid)
            before = site_semantic(before_site) if before_site is not None else None
            after = site_semantic(after_site) if after_site is not None else None
            if before != after:
                changes.append(
                    MaterialSnapshotChange(
                        aggregate_type="site",
                        aggregate_uuid=site_uuid,
                        section="site",
                        before_hash=canonical_hash(before) if before is not None else None,
                        after_hash=canonical_hash(after) if after is not None else None,
                        changed_fields=(
                            ["presence"]
                            if before is None or after is None
                            else _changed_fields(before, after)
                        ),
                    )
                )

    return MaterialSnapshotDiff(
        root_material_uuid=authoritative.root_material_uuid,
        base_state_hash=snapshot_state_hash(authoritative),
        observed_state_hash=snapshot_state_hash(observed),
        changes=changes,
    )


__all__ = [
    "compare_material_snapshot",
    "material_sections",
    "site_semantic",
    "snapshot_state_hash",
]
