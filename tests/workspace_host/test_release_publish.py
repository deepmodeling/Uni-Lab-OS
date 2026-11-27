"""AIW WorkspaceRelease Plan/Apply/Verify public seams."""

from __future__ import annotations

from argparse import ArgumentParser
from pathlib import Path

import pytest

from unilabos.workspace_host.authority_sync import (
    _remap_available_site_template_identities,
)
from unilabos.workspace_host.cli import (
    dispatch_workspace_command,
    register_workspace_subcommands,
)
from unilabos.workspace_host.host import WorkspaceHost
from unilabos.workspace_host.model import WorkspaceHostError, WorkspacePaths
from unilabos.workspace_host.release_publish import (
    DeploymentPlan,
    DeploymentResult,
    ExistingBackendDeploymentTarget,
    VerificationReport,
    WorkspaceRelease,
    WorkspaceReleasePublisher,
    _backend_workflow_projection,
    _bind_backend_material_sources,
    _deployment_template_definition,
    _embed_release_material_shapes,
    _merge_backend_composite_expansions,
    _normalized_workflow,
    _prepare_deployment_templates,
    _remap_imported_workflow_graph,
    _repair_public_node_metadata,
    _restore_public_authoring_params,
    _workflow_import_payload,
    _workflow_node_patches,
)


def test_release_target_inspection_returns_service_origin() -> None:
    target = ExistingBackendDeploymentTarget(
        "http://127.0.0.1:8080/api/v1/",
        "test-token",
    )
    target._paged = lambda *_args, **_kwargs: []  # type: ignore[method-assign]

    inspection = target.inspect()

    assert inspection["targetAddress"] == "http://127.0.0.1:8080"


def test_release_embeds_compiled_material_shape_into_template_model() -> None:
    templates = [{
        "name": "community.example.beaker",
        "model": {"shape": {"format": "unilab.shape/v1", "entry": "shape.yml"}},
    }]
    shape = {
        "schema_version": "unilab.shape/v1",
        "id": "beaker",
        "bundle": "example",
        "categories": ["beaker"],
        "parts": [{"type": "lathe"}],
    }

    _embed_release_material_shapes(
        templates,
        {"community.example.beaker": shape},
    )

    assert templates[0]["model"]["shape"] == {
        "format": "unilab.shape/v1",
        "entry": "shape.yml",
        **shape,
    }


def test_composite_expansion_maps_temporary_root_identity_back_to_source() -> None:
    source = {
        "workflow": {"uuid": "source-workflow"},
        "nodes": [
            {"uuid": "source-root", "type": "workflow", "name": "child"},
            {
                "uuid": "source-private",
                "parent_uuid": "source-root",
                "type": "device_action",
                "name": "pick",
            },
        ],
        "edges": [],
        "node_templates": [],
        "handle_templates": [],
    }
    backend = {
        "workflow": {"uuid": "temporary-workflow"},
        "nodes": [
            {"uuid": "temporary-root", "type": "workflow", "name": "child"},
            {
                "uuid": "temporary-private",
                "parent_uuid": "temporary-root",
                "type": "device_action",
                "name": "pick",
            },
        ],
        "edges": [],
        "node_templates": [],
        "handle_templates": [],
    }

    merged = _merge_backend_composite_expansions(
        source,
        backend,
        roots=(source["nodes"][0],),
        backend_root_identities={"temporary-root": "source-root"},
    )

    nodes = {node["uuid"]: node for node in merged["nodes"]}
    assert set(nodes) == {"source-root", "source-private"}
    assert nodes["source-private"]["parent_uuid"] == "source-root"


def _release() -> WorkspaceRelease:
    return WorkspaceRelease(
        release_id="sha256:release-1",
        source_workspace="/workspace",
        templates=({"uuid": "local-template", "name": "device.pump"},),
        material_graph={"nodes": []},
        workflows=(),
    )


def test_publish_runs_plan_apply_verify_and_records_only_verified_release(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    release = _release()

    class Builder:
        def build(self) -> WorkspaceRelease:
            calls.append("build")
            return release

    class Target:
        def plan(self, candidate: WorkspaceRelease) -> DeploymentPlan:
            calls.append("plan")
            return DeploymentPlan(
                release=candidate,
                target_address="https://backend.example/api/v1",
                template_count=1,
                material_count=0,
                workflow_count=0,
            )

        def apply(self, plan: DeploymentPlan) -> DeploymentResult:
            calls.append("apply")
            return DeploymentResult(
                release_id=plan.release.release_id,
                target_address=plan.target_address,
                template_identities={"device.pump": "backend-template"},
                material_identities={},
                workflow_identities={},
            )

    class Verifier:
        def verify(
            self,
            candidate: WorkspaceRelease,
            result: DeploymentResult,
        ) -> VerificationReport:
            calls.append("verify")
            return VerificationReport(
                verified=True,
                template_count=1,
                material_count=0,
                workflow_count=0,
                diagnostics=(),
            )

    publisher = WorkspaceReleasePublisher(
        Builder(),
        Target(),
        Verifier(),
        deployment_directory=tmp_path,
    )

    receipt = publisher.publish()

    assert calls == ["build", "plan", "apply", "verify"]
    assert receipt["verified"] is True
    assert receipt["releaseId"] == release.release_id
    assert (tmp_path / "sha256-release-1.json").is_file()


def test_publish_accepts_a_prebuilt_release_without_reading_source_twice(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    release = _release()

    class Builder:
        def build(self) -> WorkspaceRelease:
            calls.append("build")
            return release

    class Target:
        def plan(self, candidate: WorkspaceRelease) -> DeploymentPlan:
            calls.append("plan")
            return DeploymentPlan(candidate, "https://backend.example/api/v1", 1, 0, 0)

        def apply(self, plan: DeploymentPlan) -> DeploymentResult:
            calls.append("apply")
            return DeploymentResult(plan.release.release_id, plan.target_address, {}, {}, {})

    class Verifier:
        def verify(self, *_args: object) -> VerificationReport:
            calls.append("verify")
            return VerificationReport(True, 1, 0, 0, ())

    publisher = WorkspaceReleasePublisher(
        Builder(), Target(), Verifier(), deployment_directory=tmp_path
    )
    prepared = publisher.build()
    publisher.publish(prepared)

    assert calls == ["build", "plan", "apply", "verify"]


def test_publish_fails_closed_and_does_not_record_unverified_release(
    tmp_path: Path,
) -> None:
    release = _release()

    class Builder:
        def build(self) -> WorkspaceRelease:
            return release

    class Target:
        def plan(self, candidate: WorkspaceRelease) -> DeploymentPlan:
            return DeploymentPlan(
                release=candidate,
                target_address="https://backend.example/api/v1",
                template_count=1,
                material_count=0,
                workflow_count=0,
            )

        def apply(self, plan: DeploymentPlan) -> DeploymentResult:
            return DeploymentResult(
                release_id=plan.release.release_id,
                target_address=plan.target_address,
                template_identities={},
                material_identities={},
                workflow_identities={},
            )

    class Verifier:
        def verify(self, *_args: object) -> VerificationReport:
            return VerificationReport(
                verified=False,
                template_count=0,
                material_count=0,
                workflow_count=0,
                diagnostics=("template device.pump is missing",),
            )

    publisher = WorkspaceReleasePublisher(
        Builder(),
        Target(),
        Verifier(),
        deployment_directory=tmp_path,
    )

    with pytest.raises(WorkspaceHostError) as caught:
        publisher.publish()

    assert caught.value.code == "release_verification_failed"
    assert not list(tmp_path.iterdir())


def test_restore_authoring_params_preserves_composite_contract_and_private_nodes() -> None:
    current = {
        "workflow": {"uuid": "target-workflow", "revision": 9},
        "nodes": [
            {
                "uuid": "invocation",
                "parent_uuid": None,
                "type": "workflow",
                "param": {"resource": {"uuid": "temporary"}},
                "meta_data": {
                    "unilab": {"composite": {"contract_uuid": "frozen"}},
                    "unilab_release": {"source_node_uuid": "source-invocation"},
                },
            },
            {
                "uuid": "private-child",
                "parent_uuid": "invocation",
                "type": "action",
                "param": {"resource": {"uuid": "temporary"}},
                "meta_data": {
                    "unilab_release": {"source_node_uuid": "source-child"}
                },
            },
        ],
        "edges": [{"uuid": "private-edge"}],
    }
    authoring = {
        "nodes": [
            {
                "uuid": "invocation",
                "parent_uuid": None,
                "param": {},
                "meta_data": {
                    "unilab_release": {"source_node_uuid": "source-invocation"}
                },
            },
            {
                "uuid": "private-child",
                "parent_uuid": "invocation",
                "param": {},
                "meta_data": {
                    "unilab_release": {"source_node_uuid": "source-child"}
                },
            },
        ]
    }

    restored = _restore_public_authoring_params(current, authoring)

    assert restored["nodes"][0]["param"] == {}
    assert restored["nodes"][0]["meta_data"] == current["nodes"][0]["meta_data"]
    assert restored["nodes"][1] == current["nodes"][1]
    assert restored["edges"] == current["edges"]


def test_restore_authoring_params_keeps_workflow_input_defaults() -> None:
    current = {
        "workflow": {"uuid": "target-workflow"},
        "handle_templates": [
            {
                "uuid": "target-volume-handle",
                "handle_key": "volume_pump_1",
                "data_key": "volume_pump_1",
            }
        ],
        "nodes": [
            {
                "uuid": "target-node",
                "parent_uuid": None,
                "param": {
                    "volume_pump_1": 10,
                    "required_without_default": "publication-placeholder",
                },
                "meta_data": {
                    "unilab": {
                        "input_bindings": {
                            "target-volume-handle": {
                                "parameter": "volume_pump_1"
                            }
                        }
                    },
                    "unilab_release": {"source_node_uuid": "source-node"},
                },
            }
        ],
    }
    authoring = {
        "workflow": {
            "uuid": "source-workflow",
            "meta_data": {
                "unilab": {
                    "input_contract": {
                        "parameters": [
                            {"name": "volume_pump_1", "default": 10},
                            {"name": "required_without_default", "required": True},
                        ]
                    }
                }
            },
        },
        "nodes": [
            {
                "uuid": "source-node",
                "parent_uuid": None,
                "param": {},
                "meta_data": {
                    "unilab_release": {"source_node_uuid": "source-node"}
                },
            }
        ],
    }

    restored = _restore_public_authoring_params(current, authoring)

    assert restored["nodes"][0]["param"] == {"volume_pump_1": 10}


def test_repair_public_metadata_remaps_composite_boundary_only() -> None:
    current = {
        "nodes": [
            {
                "uuid": "atomic",
                "parent_uuid": None,
                "type": "device_action",
                "meta_data": {
                    "unilab": {"input_bindings": {"local-handle": {}}},
                    "unilab_release": {"source_node_uuid": "source-atomic"},
                },
            },
            {
                "uuid": "invocation",
                "parent_uuid": None,
                "type": "workflow",
                "meta_data": {
                    "unilab": {"composite": {"contract_uuid": "frozen"}},
                    "unilab_release": {"source_node_uuid": "source-invocation"},
                },
            },
            {
                "uuid": "private",
                "parent_uuid": "invocation",
                "type": "device_action",
                "meta_data": {
                    "unilab": {"input_bindings": {"local-private": {}}},
                    "unilab_release": {"source_node_uuid": "source-private"},
                },
            },
        ],
        "edges": [{"uuid": "private-edge"}],
    }
    remapped = {
        "nodes": [
            {
                "uuid": "atomic",
                "parent_uuid": None,
                "type": "device_action",
                "meta_data": {
                    "unilab": {"input_bindings": {"target-handle": {}}},
                    "unilab_release": {"source_node_uuid": "source-atomic"},
                },
            },
            {
                "uuid": "invocation",
                "parent_uuid": None,
                "type": "workflow",
                "meta_data": {
                    "changed": True,
                    "unilab_release": {
                        "source_node_uuid": "source-invocation"
                    },
                },
            },
            {
                "uuid": "private",
                "parent_uuid": "invocation",
                "type": "device_action",
                "meta_data": {"changed": True},
            },
        ]
    }

    repaired = _repair_public_node_metadata(current, remapped)

    assert "target-handle" in repaired["nodes"][0]["meta_data"]["unilab"]["input_bindings"]
    assert repaired["nodes"][1]["meta_data"] == {
        "changed": True,
        "unilab_release": {"source_node_uuid": "source-invocation"},
    }
    assert repaired["nodes"][2] == current["nodes"][2]
    assert repaired["edges"] == current["edges"]

    patches = _workflow_node_patches(
        current,
        repaired,
        fields=("meta_data",),
    )
    assert patches == [
        (
            "atomic",
            {
                "meta_data": {
                    "unilab": {"input_bindings": {"target-handle": {}}},
                    "unilab_release": {"source_node_uuid": "source-atomic"},
                }
            },
        ),
        (
            "invocation",
            {
                "meta_data": {
                    "changed": True,
                    "unilab_release": {
                        "source_node_uuid": "source-invocation"
                    },
                }
            },
        ),
    ]


def test_imported_composite_metadata_uses_backend_child_identities() -> None:
    source_graph = {
        "workflow": {"uuid": "source-workflow", "name": "workflow"},
        "nodes": [
            {
                "uuid": "source-invocation",
                "type": "workflow",
                "meta_data": {},
            },
            {
                "uuid": "source-child",
                "parent_uuid": "source-invocation",
                "type": "device_action",
                "meta_data": {},
            },
        ],
        "handle_templates": [],
    }
    imported_graph = {
        "workflow": {
            "uuid": "target-workflow",
            "name": "workflow",
            "revision": 1,
            "meta_data": {},
        },
        "nodes": [
            {
                "uuid": "target-invocation",
                "type": "workflow",
                "meta_data": {
                    "unilab": {
                        "composite": {
                            "target_mappings": {
                                "boundary-handle": [
                                    {"workflow_node_uuid": "source-child"}
                                ]
                            },
                            "source_mappings": {
                                "output-handle": {
                                    "kind": "node_output",
                                    "workflow_node_uuid": "source-child",
                                }
                            },
                        }
                    },
                    "unilab_release": {
                        "source_node_uuid": "source-invocation"
                    },
                },
            },
            {
                "uuid": "target-child",
                "parent_uuid": "target-invocation",
                "type": "device_action",
                "meta_data": {
                    "unilab_release": {"source_node_uuid": "source-child"}
                },
            },
        ],
        "handle_templates": [],
        "edges": [],
    }
    release = WorkspaceRelease(
        release_id="sha256:release-composite-identities",
        source_workspace="/workspace",
        templates=(),
        material_graph={"nodes": []},
        workflows=(),
    )

    remapped = _remap_imported_workflow_graph(
        source_graph,
        imported_graph,
        release=release,
        workflow_identities={"source-workflow": "target-workflow"},
        material_identities={},
        resource_template_identities={},
    )

    invocation = next(
        node for node in remapped["nodes"] if node["uuid"] == "target-invocation"
    )
    composite = invocation["meta_data"]["unilab"]["composite"]
    assert composite["target_mappings"]["boundary-handle"][0][
        "workflow_node_uuid"
    ] == "target-child"
    assert composite["source_mappings"]["output-handle"][
        "workflow_node_uuid"
    ] == "target-child"
    assert invocation["meta_data"]["unilab_release"] == {
        "release_id": "sha256:release-composite-identities",
        "source_node_uuid": "source-invocation",
    }


def test_instance_owned_sites_use_deterministic_release_only_template() -> None:
    release = WorkspaceRelease(
        release_id="sha256:release-with-sites",
        source_workspace="/workspace",
        templates=(
            {
                "uuid": "local-template",
                "name": "resource.tip_box",
                "display_name": "TIP 盒",
                "resource_type": "resource",
                "config_info": [],
            },
        ),
        material_graph={
            "nodes": [
                {
                    "material": {
                        "uuid": "local-material",
                        "resource_template_uuid": "local-template",
                        "class": "resource.tip_box",
                        "type": "container",
                        "barcode": "TIP-01",
                        "name": "TIP 盒 01",
                        "config": {
                            "sites": [
                                {
                                    "label": "tip-101",
                                    "position": {"x": 1, "y": 2, "z": 3},
                                    "size": {"width": 4, "height": 5, "depth": 6},
                                    "content_type": ["tip"],
                                }
                            ]
                        },
                        "data": {},
                    },
                    "sites": [
                        {
                            "name": "tip-101",
                            "position_x": 1,
                            "position_y": 2,
                            "position_z": 3,
                            "width": 4,
                            "length": 5,
                            "depth": 6,
                        }
                    ],
                }
            ]
        },
        workflows=(),
    )

    prepared = _prepare_deployment_templates(release)

    assert len(prepared.templates) == 2
    derived_name = prepared.material_template_names["local-material"]
    assert derived_name.startswith("resource.tip_box.__unilab_release__.")
    derived = next(item for item in prepared.templates if item["name"] == derived_name)
    assert derived["display_name"] == "TIP 盒"
    assert derived["config_info"][0]["config"]["sites"][0]["label"] == "tip-101"
    assert derived["available_sites"] == [
        {
            "schema_version": 1,
            "index": 0,
            "label": "tip-101",
            "visible": True,
            "position_x": 1,
            "position_y": 2,
            "position_z": 3,
            "rotation_x": 0,
            "rotation_y": 0,
            "rotation_z": 0,
            "parent_link": "",
            "width": 4,
            "length": 5,
            "depth": 6,
            "content_type": ["tip"],
            "allowed_resource_template_uuids": [],
            "description": None,
            "meta_data": {},
        }
    ]


def test_resource_template_root_uses_material_type_for_backend_admission() -> None:
    release = WorkspaceRelease(
        release_id="sha256:release-root",
        source_workspace="/workspace",
        templates=(
            {
                "uuid": "deck-template",
                "name": "resource.deck",
                "display_name": "实验台",
                "resource_type": "resource",
                "config_info": [],
            },
        ),
        material_graph={
            "nodes": [
                {
                    "material": {
                        "uuid": "deck-material",
                        "resource_template_uuid": "deck-template",
                        "parent_uuid": None,
                        "class": "resource.deck",
                        "type": "deck",
                        "barcode": "DECK-01",
                        "name": "实验台",
                        "config": {},
                        "data": {},
                    },
                    "sites": [],
                }
            ]
        },
        workflows=(),
    )

    prepared = _prepare_deployment_templates(release)
    derived_name = prepared.material_template_names["deck-material"]
    derived = next(item for item in prepared.templates if item["name"] == derived_name)

    assert derived["resource_type"] == "deck"
    assert derived["display_name"] == "实验台"


def test_leaf_material_keeps_canonical_template_when_runtime_type_differs() -> None:
    """Keep scheduler-facing material identity stable for occupied leaf resources."""

    release = WorkspaceRelease(
        release_id="sha256:release-leaf",
        source_workspace="/workspace",
        templates=(
            {
                "uuid": "bottle-template",
                "name": "resource.reagent_bottle",
                "display_name": "试剂瓶",
                "resource_type": "resource",
                "config_info": [],
            },
        ),
        material_graph={
            "nodes": [
                {
                    "material": {
                        "uuid": "bottle-material",
                        "resource_template_uuid": "bottle-template",
                        "parent_uuid": "reagent-stack",
                        "class": "resource.reagent_bottle",
                        "type": "container",
                        "barcode": "BOTTLE-01",
                        "name": "试剂瓶 R1C1",
                        "config": {},
                        "data": {},
                    },
                    "sites": [],
                }
            ]
        },
        workflows=(),
    )

    prepared = _prepare_deployment_templates(release)

    assert len(prepared.templates) == 1
    assert prepared.material_template_names["bottle-material"] == (
        "resource.reagent_bottle"
    )
    assert prepared.templates[0]["resource_type"] == "resource"
    assert prepared.templates[0]["config_info"][0]["type"] == "container"


def test_imported_workflow_metadata_uses_backend_node_and_handle_identities() -> None:
    source_graph = {
        "workflow": {
            "uuid": "source-workflow",
            "name": "workflow",
            "meta_data": {
                "unilab": {
                    "output_bindings": {
                        "result": {
                            "workflow_node_uuid": "source-node",
                            "source_handle_uuid": "source-output",
                        }
                    }
                }
            },
        },
        "nodes": [
            {
                "uuid": "source-node",
                "workflow_node_template_uuid": "source-node-template",
                "param": {
                    "resource": {"uuid": "source-material"},
                    "resource_template_uuid": "source-resource-template",
                },
                "meta_data": {
                    "unilab": {
                        "input_bindings": {
                            "source-input": {
                                "workflow_node_uuid": "source-node",
                                "source_handle_uuid": "source-output",
                            }
                        },
                        "material_passthrough_handles": {
                            "source-output": "source-input"
                        },
                    }
                },
            }
        ],
        "handle_templates": [
            {
                "uuid": "source-input",
                "workflow_node_template_uuid": "source-node-template",
                "handle_key": "resource",
                "io_type": "target",
            },
            {
                "uuid": "source-output",
                "workflow_node_template_uuid": "source-node-template",
                "handle_key": "resource",
                "io_type": "source",
            },
        ],
    }
    imported_graph = {
        "workflow": {
            "uuid": "target-workflow",
            "name": "workflow",
            "revision": 1,
            "meta_data": {},
        },
        "nodes": [
            {
                "uuid": "target-node",
                "workflow_node_template_uuid": "target-node-template",
                "param": {},
                "meta_data": {
                    "unilab_release": {"source_node_uuid": "source-node"}
                },
            }
        ],
        "handle_templates": [
            {
                "uuid": "target-input",
                "workflow_node_template_uuid": "target-node-template",
                "handle_key": "resource",
                "io_type": "target",
            },
            {
                "uuid": "target-output",
                "workflow_node_template_uuid": "target-node-template",
                "handle_key": "resource",
                "io_type": "source",
            },
        ],
        "edges": [],
    }
    release = WorkspaceRelease(
        release_id="sha256:release-identities",
        source_workspace="/workspace",
        templates=(),
        material_graph={"nodes": []},
        workflows=(),
    )

    remapped = _remap_imported_workflow_graph(
        source_graph,
        imported_graph,
        release=release,
        workflow_identities={"source-workflow": "target-workflow"},
        material_identities={"source-material": "target-material"},
        resource_template_identities={
            "source-resource-template": "target-resource-template"
        },
    )

    workflow_unilab = remapped["workflow"]["meta_data"]["unilab"]
    assert workflow_unilab["output_bindings"]["result"] == {
        "workflow_node_uuid": "target-node",
        "source_handle_uuid": "target-output",
    }
    node = remapped["nodes"][0]
    node_unilab = node["meta_data"]["unilab"]
    assert node_unilab["input_bindings"]["target-input"] == {
        "workflow_node_uuid": "target-node",
        "source_handle_uuid": "target-output",
    }
    assert node_unilab["material_passthrough_handles"] == {
        "target-output": "target-input"
    }
    assert node["param"] == {
        "resource": {"uuid": "target-material"},
        "resource_template_uuid": "target-resource-template",
    }
    assert node["meta_data"]["unilab_release"] == {
        "release_id": "sha256:release-identities",
        "source_node_uuid": "source-node",
    }


def test_imported_workflow_can_preserve_temporary_publication_scaffold() -> None:
    source_graph = {
        "workflow": {"uuid": "source-workflow", "name": "workflow"},
        "nodes": [
            {
                "uuid": "source-node",
                "workflow_node_template_uuid": "source-node-template",
                "param": {},
                "meta_data": {},
            }
        ],
        "handle_templates": [],
    }
    imported_graph = {
        "workflow": {
            "uuid": "target-workflow",
            "name": "workflow",
            "revision": 1,
            "meta_data": {},
        },
        "nodes": [
            {
                "uuid": "target-node",
                "workflow_node_template_uuid": "target-node-template",
                "param": {"required_input": "temporary-publication-value"},
                "meta_data": {
                    "unilab_release": {"source_node_uuid": "source-node"}
                },
            }
        ],
        "handle_templates": [],
        "edges": [],
    }
    release = WorkspaceRelease(
        release_id="sha256:release-scaffold",
        source_workspace="/workspace",
        templates=(),
        material_graph={"nodes": []},
        workflows=(),
    )

    publication_graph = _remap_imported_workflow_graph(
        source_graph,
        imported_graph,
        release=release,
        workflow_identities={"source-workflow": "target-workflow"},
        material_identities={},
        resource_template_identities={},
        preserve_imported_param_defaults=True,
    )
    authoring_graph = _remap_imported_workflow_graph(
        source_graph,
        imported_graph,
        release=release,
        workflow_identities={"source-workflow": "target-workflow"},
        material_identities={},
        resource_template_identities={},
    )

    assert publication_graph["nodes"][0]["param"] == {
        "required_input": "temporary-publication-value"
    }
    assert authoring_graph["nodes"][0]["param"] == {}


def test_workflow_import_maps_known_domain_identities_before_backend_validation() -> None:
    release = WorkspaceRelease(
        release_id="sha256:release-preimport",
        source_workspace="/workspace",
        templates=(),
        material_graph={"nodes": []},
        workflows=(),
    )
    graph = {
        "workflow": {
            "uuid": "source-workflow",
            "name": "source",
            "meta_data": {
                "dependency": "source-child-workflow",
            },
        },
        "node_templates": [
            {
                "uuid": "material-source-template",
                "name": "material_source",
            }
        ],
        "handle_templates": [],
        "nodes": [
            {
                "uuid": "source-node",
                "workflow_node_template_uuid": "material-source-template",
                "name": "source",
                "type": "material_source",
                "param": {
                    "resource_template_uuid": "source-resource-template",
                    "resource": {"uuid": "source-material"},
                },
                "meta_data": {
                    "resource_template_uuid": "source-resource-template"
                },
            }
        ],
        "edges": [],
    }

    payload = _workflow_import_payload(
        graph,
        release=release,
        workflow_identities={
            "source-child-workflow": "target-child-workflow"
        },
        material_identities={"source-material": "target-material"},
        source_template_names={},
        resource_template_identities={
            "source-resource-template": "target-resource-template"
        },
    )

    assert payload["meta_data"]["dependency"] == "target-child-workflow"
    assert payload["nodes"][0]["param"] == {
        "resource_template_uuid": "target-resource-template",
        "resource": {"uuid": "target-material"},
    }
    assert payload["nodes"][0]["meta_data"]["resource_template_uuid"] == (
        "target-resource-template"
    )


def test_workflow_import_uses_published_composite_template_identity() -> None:
    release = WorkspaceRelease(
        release_id="sha256:release-composite-template",
        source_workspace="/workspace",
        templates=(),
        material_graph={"nodes": []},
        workflows=(),
    )
    graph = {
        "workflow": {"uuid": "source-parent", "name": "parent"},
        "node_templates": [
            {
                "uuid": "source-composite-template",
                "name": "workflow:source-child",
                "resource_template_uuid": "source-host-template",
                "meta_data": {
                    "unilab": {
                        "workflow_source": {
                            "kind": "package",
                            "module": "szlab_poly_studio.workflows.material_transfer",
                            "symbol": "s_z_lab_标准物料转运",
                            "definition_fqid": (
                                "szlab_poly_studio.workflows.material_transfer."
                                "s_z_lab_标准物料转运"
                            ),
                        }
                    }
                },
            }
        ],
        "handle_templates": [],
        "nodes": [
            {
                "uuid": "composite-node",
                "workflow_node_template_uuid": "source-composite-template",
                "name": "child",
                "type": "workflow",
                "param": {},
                "meta_data": {},
            }
        ],
        "edges": [],
    }

    payload = _workflow_import_payload(
        graph,
        release=release,
        workflow_identities={"source-child": "target-child"},
        workflow_template_identities={
            "source-child": "target-published-node-template"
        },
        material_identities={},
        source_template_names={"source-host-template": "host_node"},
        resource_template_identities={},
    )

    assert payload["nodes"][0]["workflow_node_template_uuid"] == (
        "target-published-node-template"
    )
    assert "template_name" not in payload["nodes"][0]
    assert "resource_name" not in payload["nodes"][0]
    assert payload["nodes"][0]["meta_data"]["unilab"]["workflow_source"] == {
        "kind": "package",
        "module": "szlab_poly_studio.workflows.material_transfer",
        "symbol": "s_z_lab_标准物料转运",
        "definition_fqid": (
            "szlab_poly_studio.workflows.material_transfer."
            "s_z_lab_标准物料转运"
        ),
    }


def test_backend_workflow_projection_removes_visual_groups_and_promotes_children() -> None:
    release = WorkspaceRelease(
        release_id="sha256:release-group",
        source_workspace="/workspace",
        templates=(),
        material_graph={"nodes": []},
        workflows=(),
    )
    graph = {
        "workflow": {"uuid": "source-workflow", "name": "source"},
        "node_templates": [
            {
                "uuid": "group-template",
                "name": "group",
                "resource_template_uuid": "host-template",
            }
        ],
        "handle_templates": [],
        "nodes": [
            {
                "uuid": "group-node",
                "workflow_node_template_uuid": "group-template",
                "name": "group",
                "type": "Group",
                "param": {},
                "meta_data": {},
            },
            {
                "uuid": "nested-group-node",
                "workflow_node_template_uuid": "group-template",
                "parent_uuid": "group-node",
                "name": "nested group",
                "type": "group",
                "param": {},
                "meta_data": {},
            },
            {
                "uuid": "action-node",
                "workflow_node_template_uuid": "action-template",
                "parent_uuid": "nested-group-node",
                "name": "action",
                "type": "device_action",
                "param": {},
                "meta_data": {},
            },
        ],
        "edges": [],
    }

    projected = _backend_workflow_projection(graph)

    assert [node["uuid"] for node in projected["nodes"]] == ["action-node"]
    assert projected["nodes"][0]["parent_uuid"] is None


def test_backend_workflow_projection_rejects_semantic_edges_attached_to_group() -> None:
    graph = {
        "workflow": {"uuid": "source-workflow", "name": "source"},
        "nodes": [
            {"uuid": "group-node", "type": "group"},
            {"uuid": "action-node", "type": "device_action"},
        ],
        "edges": [
            {
                "uuid": "edge-1",
                "source_node_uuid": "group-node",
                "target_node_uuid": "action-node",
            }
        ],
    }

    with pytest.raises(WorkspaceHostError) as raised:
        _backend_workflow_projection(graph)

    assert raised.value.code == "release_source_invalid"


def test_backend_workflow_projection_clears_nonexistent_material_source_selection() -> None:
    graph = {
        "workflow": {"uuid": "source-workflow", "name": "source"},
        "nodes": [
            {
                "uuid": "source-node",
                "type": "material_source",
                "param": {
                    "mode": "existing",
                    "material_uuid": "authoring-placeholder-material",
                    "mount": {"uuid": "known-mount"},
                },
            },
            {
                "uuid": "selected-source-node",
                "type": "material_source",
                "param": {
                    "mode": "existing",
                    "material_uuid": "known-material",
                    "mount": {"uuid": "known-mount"},
                },
            },
        ],
        "edges": [],
    }

    projected = _backend_workflow_projection(
        graph,
        known_material_uuids={"known-mount", "known-material"},
    )

    assert projected["nodes"][0]["param"]["material_uuid"] is None
    assert projected["nodes"][1]["param"]["material_uuid"] == "known-material"


def test_backend_material_sources_bind_distinct_inventory_and_derived_templates() -> None:
    graph = {
        "workflow": {"uuid": "workflow-local", "name": "source"},
        "nodes": [
            {
                "uuid": "source-2",
                "type": "material_source",
                "param": {
                    "mode": "existing",
                    "material_uuid": None,
                    "resource_template_uuid": "template-local",
                    "mount": {"uuid": "mount-local"},
                },
                "meta_data": {"unilab": {"authoring_source_order": 2}},
            },
            {
                "uuid": "source-1",
                "type": "material_source",
                "param": {
                    "mode": "existing",
                    "material_uuid": None,
                    "resource_template_uuid": "template-local",
                    "mount": {"uuid": "mount-local"},
                },
                "meta_data": {"unilab": {"authoring_source_order": 1}},
            },
        ],
    }
    material_graph = {
        "nodes": [
            {
                "material": {
                    "uuid": "mount-local",
                    "resource_template_uuid": "mount-template-local",
                },
                "sites": [
                    {"uuid": "site-2", "name": "T2", "sort_order": 2},
                    {"uuid": "site-1", "name": "T1", "sort_order": 1},
                ],
            },
            {
                "material": {
                    "uuid": "material-2",
                    "resource_template_uuid": "template-local",
                    "parent_uuid": "mount-local",
                },
                "current_site_uuid": "site-2",
                "sites": [{"uuid": "tip-site-2", "name": "A1"}],
            },
            {
                "material": {
                    "uuid": "material-1",
                    "resource_template_uuid": "template-local",
                    "parent_uuid": "mount-local",
                },
                "current_site_uuid": "site-1",
                "sites": [{"uuid": "tip-site-1", "name": "A1"}],
            },
        ]
    }

    bound = _bind_backend_material_sources(
        graph,
        material_graph=material_graph,
        material_identities={
            "mount-local": "mount-target",
            "material-1": "material-target-1",
            "material-2": "material-target-2",
        },
        material_template_names={
            "mount-local": "mount-template",
            "material-1": "derived-template-1",
            "material-2": "derived-template-2",
        },
        source_template_names={"template-local": "canonical-template"},
        target_templates={
            "mount-template": "mount-template-target",
            "derived-template-1": "derived-template-target-1",
            "derived-template-2": "derived-template-target-2",
            "canonical-template": "canonical-template-target",
        },
    )

    by_uuid = {node["uuid"]: node for node in bound["nodes"]}
    assert by_uuid["source-1"]["param"]["material_uuid"] == "material-target-1"
    assert by_uuid["source-1"]["param"]["resource_template_uuid"] == (
        "derived-template-target-1"
    )
    assert by_uuid["source-2"]["param"]["material_uuid"] == "material-target-2"
    assert by_uuid["source-2"]["param"]["resource_template_uuid"] == (
        "derived-template-target-2"
    )
    assert by_uuid["source-1"]["param"]["mount"] == {"uuid": "mount-local"}


def test_backend_material_source_preserves_explicit_material_selection() -> None:
    graph = {
        "workflow": {"uuid": "workflow-local", "name": "source"},
        "nodes": [
            {
                "uuid": "source",
                "type": "material_source",
                "param": {
                    "mode": "existing",
                    "material_uuid": "material-local",
                    "resource_template_uuid": "template-local",
                    "mount": {"uuid": "mount-local"},
                },
            }
        ],
    }
    material_graph = {
        "nodes": [
            {
                "material": {
                    "uuid": "mount-local",
                    "resource_template_uuid": "mount-template-local",
                },
                "sites": [{"uuid": "site-1", "name": "T1", "sort_order": 1}],
            },
            {
                "material": {
                    "uuid": "material-local",
                    "resource_template_uuid": "template-local",
                    "parent_uuid": "mount-local",
                },
                "current_site_uuid": "site-1",
                "sites": [{"uuid": "tip-site", "name": "A1"}],
            },
        ]
    }

    bound = _bind_backend_material_sources(
        graph,
        material_graph=material_graph,
        material_identities={
            "mount-local": "mount-target",
            "material-local": "material-target",
        },
        material_template_names={
            "mount-local": "mount-template",
            "material-local": "derived-template",
        },
        source_template_names={"template-local": "canonical-template"},
        target_templates={
            "mount-template": "mount-template-target",
            "derived-template": "derived-template-target",
            "canonical-template": "canonical-template-target",
        },
    )

    assert bound["nodes"][0]["param"]["material_uuid"] == "material-target"
    assert bound["nodes"][0]["param"]["resource_template_uuid"] == (
        "derived-template-target"
    )


def test_backend_material_source_preserves_unbound_selector_without_inventory() -> None:
    graph = {
        "workflow": {"uuid": "workflow-local", "name": "source"},
        "nodes": [
            {
                "uuid": "source",
                "type": "material_source",
                "param": {
                    "mode": "existing",
                    "material_uuid": None,
                    "resource_template_uuid": "template-local",
                    "mount": {"uuid": "empty-mount-local"},
                    "site": None,
                },
            }
        ],
    }

    bound = _bind_backend_material_sources(
        graph,
        material_graph={"nodes": []},
        material_identities={"empty-mount-local": "empty-mount-target"},
        material_template_names={},
        source_template_names={"template-local": "canonical-template"},
        target_templates={"canonical-template": "canonical-template-target"},
    )

    assert bound["nodes"][0]["param"] == {
        "mode": "existing",
        "material_uuid": None,
        "resource_template_uuid": "canonical-template-target",
        "mount": {"uuid": "empty-mount-local"},
        "site": None,
    }


def test_backend_material_source_remaps_explicit_site_when_inventory_is_unbound() -> None:
    graph = {
        "workflow": {"uuid": "workflow-local", "name": "source"},
        "nodes": [
            {
                "uuid": "source",
                "type": "material_source",
                "param": {
                    "mode": "existing",
                    "material_uuid": None,
                    "resource_template_uuid": "template-local",
                    "mount": {"uuid": "mount-local"},
                    "site": "site-2",
                },
            }
        ],
    }
    material_graph = {
        "nodes": [
            {
                "material": {
                    "uuid": "mount-local",
                    "resource_template_uuid": "mount-template-local",
                },
                "sites": [
                    {"uuid": "site-2", "name": "L1C2", "sort_order": 2}
                ],
            }
        ]
    }
    resolved_sites: list[tuple[str, str]] = []

    def resolve_site(owner_uuid: str, site_name: str) -> str:
        resolved_sites.append((owner_uuid, site_name))
        return "target-site-2"

    bound = _bind_backend_material_sources(
        graph,
        material_graph=material_graph,
        material_identities={"mount-local": "mount-target"},
        material_template_names={"mount-local": "mount-template"},
        source_template_names={"template-local": "canonical-template"},
        target_templates={
            "mount-template": "mount-template-target",
            "canonical-template": "canonical-template-target",
        },
        target_site_resolver=resolve_site,
    )

    assert bound["nodes"][0]["param"] == {
        "mode": "existing",
        "material_uuid": None,
        "resource_template_uuid": "canonical-template-target",
        "mount": {"uuid": "mount-local"},
        "site": "target-site-2",
    }
    assert resolved_sites == [("mount-local", "L1C2")]


def test_backend_material_source_honors_and_remaps_explicit_site() -> None:
    graph = {
        "workflow": {"uuid": "workflow-local", "name": "source"},
        "nodes": [
            {
                "uuid": "source",
                "type": "material_source",
                "param": {
                    "mode": "existing",
                    "material_uuid": None,
                    "resource_template_uuid": "template-local",
                    "mount": {"uuid": "mount-local"},
                    "site": "site-2",
                },
            }
        ],
    }
    material_graph = {
        "nodes": [
            {
                "material": {
                    "uuid": "mount-local",
                    "resource_template_uuid": "mount-template-local",
                },
                "sites": [
                    {"uuid": "site-1", "name": "L1C1", "sort_order": 1},
                    {"uuid": "site-2", "name": "L1C2", "sort_order": 2},
                ],
            },
            {
                "material": {
                    "uuid": "material-1",
                    "resource_template_uuid": "template-local",
                    "parent_uuid": "mount-local",
                },
                "current_site_uuid": "site-1",
                "sites": [],
            },
            {
                "material": {
                    "uuid": "material-2",
                    "resource_template_uuid": "template-local",
                    "parent_uuid": "mount-local",
                },
                "current_site_uuid": "site-2",
                "sites": [],
            },
        ]
    }

    resolved_sites: list[tuple[str, str]] = []

    def resolve_site(owner_uuid: str, site_name: str) -> str:
        resolved_sites.append((owner_uuid, site_name))
        return "target-site-2"

    bound = _bind_backend_material_sources(
        graph,
        material_graph=material_graph,
        material_identities={
            "mount-local": "mount-target",
            "material-1": "material-target-1",
            "material-2": "material-target-2",
        },
        material_template_names={
            "mount-local": "mount-template",
            "material-1": "derived-template-1",
            "material-2": "derived-template-2",
        },
        source_template_names={"template-local": "canonical-template"},
        target_templates={
            "mount-template": "mount-template-target",
            "derived-template-1": "derived-template-target-1",
            "derived-template-2": "derived-template-target-2",
            "canonical-template": "canonical-template-target",
        },
        target_site_resolver=resolve_site,
    )

    assert bound["nodes"][0]["param"]["material_uuid"] == "material-target-2"
    assert bound["nodes"][0]["param"]["site"] == "target-site-2"
    assert resolved_sites == [("mount-local", "L1C2")]


def test_backend_workflow_projection_promotes_material_passthrough_output() -> None:
    graph = {
        "workflow": {
            "uuid": "source-workflow",
            "name": "transfer",
            "meta_data": {
                "unilab": {
                    "output_contract": {
                        "version": 1,
                        "outputs": [
                            {
                                "name": "resource",
                                "implicit": True,
                                "schema": {"$slot": "ResourceSlot"},
                            }
                        ],
                    },
                    "output_bindings": {
                        "resource": {
                            "kind": "workflow_input",
                            "parameter": "resource",
                        }
                    },
                }
            },
        },
        "node_templates": [
            {"uuid": "pick-template", "name": "pick"},
            {"uuid": "commit-template", "name": "transfer_resource"},
        ],
        "handle_templates": [
            {
                "uuid": "pick-resource-output",
                "workflow_node_template_uuid": "pick-template",
                "handle_key": "resource",
                "io_type": "source",
            },
            {
                "uuid": "commit-resource-output",
                "workflow_node_template_uuid": "commit-template",
                "handle_key": "resource",
                "io_type": "source",
            },
        ],
        "nodes": [
            {
                "uuid": "pick-node",
                "workflow_node_template_uuid": "pick-template",
                "type": "device_action",
                "meta_data": {
                    "unilab": {"authoring_source_order": 0}
                },
            },
            {
                "uuid": "commit-node",
                "workflow_node_template_uuid": "commit-template",
                "type": "device_action",
                "meta_data": {
                    "unilab": {"authoring_source_order": 2}
                },
            },
        ],
        "edges": [],
    }

    projected = _backend_workflow_projection(graph)
    unilab = projected["workflow"]["meta_data"]["unilab"]

    assert unilab["output_contract"]["outputs"][0]["implicit"] is False
    assert unilab["output_bindings"]["resource"] == {
        "kind": "node_output",
        "workflow_node_uuid": "commit-node",
        "source_handle_uuid": "commit-resource-output",
    }


def test_workflow_import_omits_visual_group_nodes() -> None:
    release = WorkspaceRelease(
        release_id="sha256:release-group",
        source_workspace="/workspace",
        templates=(),
        material_graph={"nodes": []},
        workflows=(),
    )
    graph = {
        "workflow": {"uuid": "source-workflow", "name": "source"},
        "node_templates": [
            {
                "uuid": "group-template",
                "name": "group",
                "resource_template_uuid": "host-template",
            }
        ],
        "handle_templates": [],
        "nodes": [
            {
                "uuid": "group-node",
                "workflow_node_template_uuid": "group-template",
                "name": "group",
                "type": "Group",
                "param": {},
                "meta_data": {},
            }
        ],
        "edges": [],
    }

    payload = _workflow_import_payload(
        _backend_workflow_projection(graph),
        release=release,
        workflow_identities={},
        material_identities={},
        source_template_names={"host-template": "host_node"},
        resource_template_identities={"host-template": "target-host-template"},
    )

    assert payload["nodes"] == []


def test_workflow_import_promotes_device_action_to_backend_ilab() -> None:
    """验证发布时把遗留设备动作类型升级为带固定执行器绑定的 Backend ILab。"""

    release = WorkspaceRelease(
        release_id="sha256:release-device-action-ilab",
        source_workspace="/workspace",
        templates=(),
        material_graph={"nodes": []},
        workflows=(),
    )
    graph = {
        "workflow": {"uuid": "source-workflow", "name": "source"},
        "node_templates": [
            {
                "uuid": "transfer-template",
                "name": "transfer_resource",
                "executor_kind": "material_transfer",
            }
        ],
        "handle_templates": [],
        "nodes": [
            {
                "uuid": "action-node",
                "workflow_node_template_uuid": "transfer-template",
                "name": "pick",
                "type": "device_action",
                "param": {},
                "meta_data": {
                    "unilab": {
                        "executor_binding": {
                            "mode": "fixed",
                            "device_id": "source-device",
                        }
                    }
                },
            }
        ],
        "edges": [],
    }

    payload = _workflow_import_payload(
        graph,
        release=release,
        workflow_identities={},
        material_identities={"source-device": "target-device"},
        source_template_names={},
        resource_template_identities={},
    )

    assert payload["nodes"][0]["type"] == "ILab"
    assert payload["nodes"][0]["meta_data"]["unilab"]["executor_binding"] == {
        "mode": "fixed",
        "device_id": "target-device",
    }


def test_workflow_import_keeps_regular_device_action_kind() -> None:
    """验证普通设备动作仍由 Edge 调度，不会误升级为 Backend ILab。"""

    release = WorkspaceRelease(
        release_id="sha256:release-regular-device-action",
        source_workspace="/workspace",
        templates=(),
        material_graph={"nodes": []},
        workflows=(),
    )
    graph = {
        "workflow": {"uuid": "source-workflow", "name": "source"},
        "node_templates": [
            {
                "uuid": "device-template",
                "name": "inspect",
                "executor_kind": "device_action",
            }
        ],
        "handle_templates": [],
        "nodes": [
            {
                "uuid": "action-node",
                "workflow_node_template_uuid": "device-template",
                "name": "inspect",
                "type": "device_action",
                "param": {},
                "meta_data": {},
            }
        ],
        "edges": [],
    }

    payload = _workflow_import_payload(
        graph,
        release=release,
        workflow_identities={},
        material_identities={},
        source_template_names={},
        resource_template_identities={},
    )

    assert payload["nodes"][0]["type"] == "device_action"


def test_workflow_verification_treats_local_ilab_as_backend_device_action() -> None:
    source = {
        "workflow": {"name": "source"},
        "handle_templates": [],
        "nodes": [
            {
                "uuid": "source-node",
                "name": "act",
                "type": "ILab",
                "disabled": False,
            }
        ],
        "edges": [],
    }
    target = {
        "workflow": {"name": "source"},
        "handle_templates": [],
        "nodes": [
            {
                "uuid": "target-node",
                "name": "act",
                "type": "device_action",
                "disabled": False,
                "meta_data": {
                    "unilab_release": {"source_node_uuid": "source-node"}
                },
            }
        ],
        "edges": [],
    }

    assert _normalized_workflow(source, source=True) == _normalized_workflow(
        target, source=False
    )


def test_workflow_import_materializes_bound_workflow_parameter_default() -> None:
    release = WorkspaceRelease(
        release_id="sha256:release-input-default",
        source_workspace="/workspace",
        templates=(),
        material_graph={"nodes": []},
        workflows=(),
    )
    graph = {
        "workflow": {
            "uuid": "source-workflow",
            "name": "source",
            "meta_data": {
                "unilab": {
                    "input_contract": {
                        "parameters": [
                            {"name": "target_mass_g", "default": 1.0}
                        ]
                    }
                }
            },
        },
        "node_templates": [],
        "handle_templates": [
            {
                "uuid": "target-mass-handle",
                "handle_key": "target_mass_g",
                "data_key": "target_mass_g",
            }
        ],
        "nodes": [
            {
                "uuid": "action-node",
                "name": "dose",
                "type": "device_action",
                "param": {"powder_site": "P01"},
                "meta_data": {
                    "unilab": {
                        "input_bindings": {
                            "target-mass-handle": {
                                "parameter": "target_mass_g"
                            }
                        }
                    }
                },
            }
        ],
        "edges": [],
    }

    payload = _workflow_import_payload(
        graph,
        release=release,
        workflow_identities={},
        material_identities={},
        source_template_names={},
        resource_template_identities={},
    )

    assert payload["nodes"][0]["param"] == {
        "powder_site": "P01",
        "target_mass_g": 1.0,
    }


def test_workflow_import_scaffolds_required_connected_action_inputs() -> None:
    release = WorkspaceRelease(
        release_id="sha256:release-connected-input",
        source_workspace="/workspace",
        templates=(),
        material_graph={"nodes": []},
        workflows=(),
    )
    graph = {
        "workflow": {"uuid": "source-workflow", "name": "source"},
        "node_templates": [],
        "handle_templates": [
            {
                "uuid": "material-input",
                "workflow_node_template_uuid": "action-template",
                "handle_key": "coarse_powder_cartridge",
                "data_key": "coarse_powder_cartridge",
                "io_type": "target",
                "type": "ResourceSlot",
                "required": True,
                "meta_data": {"unilab": {"value_schema": {"type": "object"}}},
            }
        ],
        "nodes": [
            {
                "uuid": "action-node",
                "workflow_node_template_uuid": "action-template",
                "name": "dose",
                "type": "device_action",
                "param": {},
                "meta_data": {},
            }
        ],
        "edges": [],
    }

    payload = _workflow_import_payload(
        graph,
        release=release,
        workflow_identities={},
        material_identities={"source-material": "target-material"},
        source_template_names={},
        resource_template_identities={},
    )

    assert payload["nodes"][0]["param"] == {
        "coarse_powder_cartridge": {"uuid": "target-material"}
    }


def test_workflow_import_scaffolds_required_bound_inputs_for_publication() -> None:
    release = WorkspaceRelease(
        release_id="sha256:release-required-input",
        source_workspace="/workspace",
        templates=(),
        material_graph={"nodes": []},
        workflows=(),
    )
    graph = {
        "workflow": {
            "uuid": "source-workflow",
            "name": "source",
            "meta_data": {
                "unilab": {
                    "input_contract": {
                        "parameters": [
                            {
                                "name": "target_device",
                                "required": True,
                                "schema": {"type": "string"},
                            },
                            {
                                "name": "resource",
                                "required": True,
                                "schema": {"$slot": "ResourceSlot"},
                            },
                        ]
                    }
                }
            },
        },
        "node_templates": [],
        "handle_templates": [
            {
                "uuid": "target-device-handle",
                "handle_key": "target_device",
                "data_key": "target_device",
            },
            {
                "uuid": "resource-handle",
                "handle_key": "resource",
                "data_key": "resource",
            },
        ],
        "nodes": [
            {
                "uuid": "action-node",
                "name": "transfer",
                "type": "device_action",
                "param": {},
                "meta_data": {
                    "unilab": {
                        "input_bindings": {
                            "target-device-handle": {
                                "parameter": "target_device"
                            },
                            "resource-handle": {"parameter": "resource"},
                        }
                    }
                },
            }
        ],
        "edges": [],
    }

    payload = _workflow_import_payload(
        graph,
        release=release,
        workflow_identities={},
        material_identities={"source-material": "target-material"},
        source_template_names={},
        resource_template_identities={},
    )

    assert payload["nodes"][0]["param"] == {
        "target_device": "unilab-release-input",
        "resource": {"uuid": "target-material"},
    }


def test_workflow_import_scaffolds_composite_parameters_before_identity_remap() -> None:
    """工作流导入须先按参数约束补值，再替换跨工作区物料身份。

    参数：无。返回：无。异常：组合工作流参数没有按 ``parameters`` 约束补齐，
    或补值发生在身份替换之后时由断言暴露。
    """

    release = WorkspaceRelease(
        release_id="sha256:release-composite-input",
        source_workspace="/workspace",
        templates=(),
        material_graph={"nodes": []},
        workflows=(),
    )
    graph = {
        "workflow": {"uuid": "source-parent", "name": "parent"},
        "node_templates": [
            {
                "uuid": "source-composite-template",
                "name": "workflow:source-child",
            }
        ],
        "handle_templates": [],
        "nodes": [
            {
                "uuid": "composite-node",
                "workflow_node_template_uuid": "source-composite-template",
                "name": "child",
                "type": "workflow",
                "param": {},
                "meta_data": {
                    "unilab": {
                        "composite": {
                            "contract_compatibility": {
                                "parameters": [
                                    {
                                        "name": "resource",
                                        "required": True,
                                        "schema": {"$slot": "ResourceSlot"},
                                    }
                                ]
                            }
                        }
                    }
                },
            }
        ],
        "edges": [],
    }

    payload = _workflow_import_payload(
        graph,
        release=release,
        workflow_identities={"source-child": "target-child"},
        workflow_template_identities={
            "source-child": "target-published-node-template"
        },
        material_identities={"source-material": "target-material"},
        source_template_names={},
        resource_template_identities={},
    )

    assert payload["nodes"][0]["param"] == {
        "resource": {"uuid": "target-material"}
    }


def test_local_action_catalog_is_restored_into_backend_sync_definition() -> None:
    definition = _deployment_template_definition(
        {
            "uuid": "device-template",
            "name": "device.robot",
            "display_name": "机械臂",
            "resource_type": "device",
        },
        (
            {
                "template": {
                    "resource_template_uuid": "device-template",
                    "name": "pick",
                    "display_name": "抓取",
                    "type": "UniLabJsonCommand",
                    "node_type": "device_action",
                    "meta_data": {
                        "unilab": {"executor_kind": "material_transfer"}
                    },
                    "goal": {"resource": "resource"},
                    "goal_default": {},
                    "feedback": {},
                    "result": {},
                    "schema": '{"type":"object","properties":{}}',
                },
                "handles": [
                    {
                        "handle_key": "ready",
                        "io_type": "target",
                        "type": "default",
                    },
                    {
                        "handle_key": "resource",
                        "display_name": "物料",
                        "io_type": "target",
                        "type": "resource",
                        "data_source": "goal",
                        "data_key": "resource",
                    },
                ],
            },
        ),
    )

    action = definition["class"]["action_value_mappings"]["pick"]
    assert action["schema"] == {"type": "object", "properties": {}}
    assert action["executor_kind"] == "material_transfer"
    assert action["node_type"] == "ILab"
    assert [handle["handler_key"] for handle in action["handles"]["input"]] == [
        "resource"
    ]


def test_release_preserves_and_remaps_initial_material_template_uuid() -> None:
    """发布定义必须把默认内容模板从本地身份显式转换到目标权威身份。"""

    local_uuid = "10000000-0000-4000-8000-000000000001"
    target_uuid = "20000000-0000-4000-8000-000000000002"
    definition = _deployment_template_definition(
        {
            "uuid": "30000000-0000-4000-8000-000000000003",
            "name": "device.deck",
            "display_name": "Deck",
            "resource_type": "device",
            "available_sites": [
                {
                    "index": "A1",
                    "label": "默认物料位",
                    "allowed_resource_template_uuids": [local_uuid],
                    "initial_material_template_uuid": local_uuid,
                }
            ],
        },
        (),
    )

    changed = _remap_available_site_template_identities(
        [definition],
        {local_uuid: "resource.default"},
        {"resource.default": target_uuid},
        error_code="release_apply_failed",
    )

    site = definition["available_sites"][0]
    assert changed is True
    assert site["allowed_resource_template_uuids"] == [target_uuid]
    assert site["initial_material_template_uuid"] == target_uuid


def test_release_rejects_unresolvable_initial_material_template_uuid() -> None:
    """发布不得静默删除无法映射到目标权威的默认内容模板。"""

    definition = {
        "id": "device.deck",
        "available_sites": [
            {
                "index": "A1",
                "label": "默认物料位",
                "initial_material_template_uuid": (
                    "10000000-0000-4000-8000-000000000001"
                ),
            }
        ],
    }

    with pytest.raises(WorkspaceHostError, match="无法跨权威解析"):
        _remap_available_site_template_identities(
            [definition],
            {},
            {},
            error_code="release_source_invalid",
        )



def test_workspace_host_release_publish_stages_authority_without_manufacturing_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """release.publish 直接拒绝，不再向 Backend 发布。"""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    host = WorkspaceHost(paths, "host-token", readiness_timeout=1)

    with pytest.raises(WorkspaceHostError, match="不再向 Backend 发布或同步数据"):
        host._dispatch(
            "release.publish",
            {
                "backendUrl": "https://backend.example",
                "activate": True,
                "verify": True,
            },
        )
    host.close()


def test_workspace_host_release_publish_restores_idle_edge_after_import_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """发布失败路径同样停在 release.publish，不再进入 Backend 导入。"""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    host = WorkspaceHost(paths, "host-token", readiness_timeout=1)

    with pytest.raises(WorkspaceHostError, match="不再向 Backend 发布或同步数据"):
        host._dispatch(
            "release.publish",
            {
                "backendUrl": "https://backend.example",
                "activate": True,
                "verify": True,
            },
        )
    host.close()


def test_reset_publish_does_not_clear_backend_when_activation_preflight_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重置发布不再尝试切换权威，release.publish 直接拒绝。"""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    host = WorkspaceHost(paths, "host-token", readiness_timeout=1)

    with pytest.raises(WorkspaceHostError, match="不再向 Backend 发布或同步数据"):
        host._dispatch(
            "release.publish",
            {
                "backendUrl": "https://backend.example",
                "activate": True,
                "verify": True,
            },
        )
    host.close()


def test_reset_publish_restarts_managed_edge_after_target_materials_are_recreated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """目标物料重建后的发布同样被 release.publish 拒绝。"""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    paths = WorkspacePaths.resolve(workspace)
    paths.prepare()
    host = WorkspaceHost(paths, "host-token", readiness_timeout=1)

    with pytest.raises(WorkspaceHostError, match="不再向 Backend 发布或同步数据"):
        host._dispatch(
            "release.publish",
            {
                "backendUrl": "https://backend.example",
                "activate": True,
                "verify": True,
            },
        )
    host.close()


def test_unilab_workspace_publish_dispatches_the_same_host_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """workspace publish 子命令已移除，argparse 直接拒绝。"""

    parser = ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    register_workspace_subcommands(subparsers)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(SystemExit) as raised:
        parser.parse_args(
            [
                "workspace",
                "publish",
                "--workspace",
                str(workspace),
                "--backend-url",
                "https://backend.example",
                "--activate",
                "--json",
            ]
        )
    assert raised.value.code == 2
