"""宿主就绪标记通过设备注册表提供给所有工作流。"""

from unilabos.registry.registry import Registry
from unilabos.registry.template_projection import RegistryTemplateProjection
from unilabos.ros.nodes.presets.host_node import HostNode


def test_host_checkpoint_has_only_ready_handles(monkeypatch):
    registry = Registry()
    monkeypatch.setattr(registry, "_setup_called", False)
    monkeypatch.setattr(registry, "_startup_executor", None)
    monkeypatch.setattr(registry, "device_type_registry", {})
    monkeypatch.setattr(registry, "resource_type_registry", {})
    registry.setup(external_only=True)

    projection = RegistryTemplateProjection.in_memory(
        authority_id="host-checkpoint",
        resource_template_identity_resolver=lambda _key: "90000000-0000-4000-8000-000000000001",
    )
    try:
        action = projection.refresh(registry).require_action(
            "unilabos.ros.nodes.presets.host_node:HostNode", "debug_checkpoint",
        )
        assert {(handle["io_type"], handle["handle_key"]) for handle in action.handles} == {
            ("source", "ready"), ("target", "ready"),
        }
        assert action.template["meta_data"]["unilab"]["always_free"]
        assert HostNode.debug_checkpoint(object()) is None
    finally:
        projection.close()
