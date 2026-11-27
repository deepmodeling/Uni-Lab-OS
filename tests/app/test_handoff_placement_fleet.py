"""精确库位计划与自动选车的接收位置、续期事实保持一致。"""

from copy import deepcopy

from tests.app.test_handoff_placement import placement, second_item
from tests.app.test_station_handoff import package, setup  # noqa: F401
from unilabos.center_protocol.handoff import validate_handoff_intent


def test_planned_batch_renewal_preserves_exact_sites_and_current_reservation_versions(setup):
    state = setup
    selection = placement(state)
    second_item(state, selection)
    prepared = state["auth"]["target"].prepare_incoming(
        package(state["payload"]),
        container_details=state["details"],
        plans=state["plans"],
        include_transport_endpoints=True,
    )
    proof = prepared["payload"]["placement_reservations"]
    state["payload"].update(
        status="prepared", revision=2,
        prepared={
            "event_epoch_uuid": state["ids"]["target_epoch"],
            **{key: value for key, value in prepared["payload"].items()
               if key not in {"kind", "expected_revision"}},
        },
    )
    renewed = state["auth"]["target"].renew_incoming(package(state["payload"]), ttl_seconds=300)
    validate_handoff_intent(renewed)
    current = renewed["payload"]["placement_reservations"]
    assert current["selection_digest"] == proof["selection_digest"]
    before = {item["container_uuid"]: item for item in proof["items"]}
    endpoints = {item["container_uuid"]: item for item in renewed["payload"]["endpoint_snapshot"]["items"]}
    assert len(current["items"]) == 2
    for item in current["items"]:
        old = before[item["container_uuid"]]
        assert item == {**old, "reservation_revision": old["reservation_revision"] + 1}
        endpoint = endpoints[item["container_uuid"]]
        assert endpoint["site_uuid"] == item["target_site_uuid"]
        assert endpoint["reservation"]["reservation_uuid"] == item["reservation_uuid"]
        assert endpoint["reservation"]["revision"] == item["reservation_revision"]
    original = deepcopy(renewed)
    assert state["auth"]["target"].renew_incoming(package(state["payload"]), ttl_seconds=300) == original
