"""库存权威原子派发准入（DispatchPermit）测试。"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from unilabos.app.scheduler.inventory.backend_contract import (
    MATERIAL_ACTIVE_CLAIM_CONFLICT,
    BackendContractError,
    BackendResourceService,
)
from unilabos.app.scheduler.inventory.content_contract import (
    BackendContainerContentService,
)
from unilabos.app.scheduler.inventory.dispatch_admission import (
    AliquotDispatchCondition,
    DispatchAdmissionConflict,
    DispatchAdmissionRequest,
    DispatchResource,
    InventoryMutationConflict,
    OperateInPlaceCondition,
    TransferDispatchCondition,
)
from unilabos.app.scheduler.inventory.domain import (
    InsufficientStock,
    MaterialRequirement,
    MaterialSourceAdmissionRequest,
)
from unilabos.app.scheduler.inventory.service import InventoryService
from unilabos.app.scheduler.inventory.station_resource import (
    AliquotReceipt,
    MaterialAliquotCommand,
    MaterialTransferCommand,
    StationResourceError,
    TransferResourceRequest,
)
from unilabos.app.scheduler.inventory.store import InventoryStore

SOURCE_SITE = "10000000-0000-4000-8000-000000000101"
TARGET_SITE = "10000000-0000-4000-8000-000000000102"
GRIPPER_SITE = "10000000-0000-4000-8000-000000000103"
TARGET_SITE_FALLBACK = "10000000-0000-4000-8000-000000000104"


@pytest.fixture()
def station_inventory(
    tmp_path: Path,
) -> tuple[InventoryStore, InventoryService, dict[str, str]]:
    """建立来源设备、目标设备、机械臂、空夹爪与待搬物料事实。

    参数：``tmp_path`` 是隔离数据库目录。返回：库存存储、业务服务和资源身份。
    异常：夹具构造失败原样传播；结束时关闭数据库。
    """

    store = InventoryStore(str(tmp_path / "dispatch-inventory.db"))
    backend = BackendResourceService(store)
    templates = backend.sync_resource_templates(
        [
            {
                "id": "test.dispatch-device",
                "display_name": "测试设备",
                "registry_type": "resource",
                "class": {},
            },
            {
                "id": "test.dispatch-vessel",
                "display_name": "测试容器",
                "registry_type": "material",
                "class": {},
            },
        ]
    )["templates"]
    template_by_name = {item["name"]: item["uuid"] for item in templates}
    identities: dict[str, str] = {}
    identities["vessel_template"] = template_by_name["test.dispatch-vessel"]
    for key, barcode in (
        ("source_device", "SOURCE-DEVICE"),
        ("target_device", "TARGET-DEVICE"),
        ("robot", "ROBOT"),
    ):
        material = backend.create_material(
            {
                "resource_template_uuid": template_by_name["test.dispatch-device"],
                "barcode": barcode,
                "name": barcode,
            }
        )
        identities[key] = material["uuid"]
    vessel = backend.create_material(
        {
            "resource_template_uuid": template_by_name["test.dispatch-vessel"],
            "parent_uuid": identities["source_device"],
            "barcode": "VESSEL",
            "name": "待搬容器",
        }
    )
    identities["vessel"] = vessel["uuid"]
    with store.transaction() as connection:
        connection.execute(
            "UPDATE material SET type='device' WHERE uuid IN (?,?,?)",
            (
                identities["source_device"],
                identities["target_device"],
                identities["robot"],
            ),
        )
        for site_uuid, owner_uuid, name, occupant, metadata in (
            (
                SOURCE_SITE,
                identities["source_device"],
                "OUT",
                identities["vessel"],
                {},
            ),
            (TARGET_SITE, identities["target_device"], "IN", None, {}),
            (
                GRIPPER_SITE,
                identities["robot"],
                "GRIPPER",
                None,
                {"unilab": {"resource_role": "robot.gripper"}},
            ),
        ):
            connection.execute(
                """
                INSERT INTO site(
                    uuid,create_time,update_time,meta_data,material_uuid,name,
                    sort_order,allowed_resource_template_uuids,
                    occupied_material_uuid,position_x,position_y,position_z,
                    depth,length,width
                ) VALUES (?,?,?,?,?,?,0,'[]',?,0,0,0,0,0,0)
                """,
                (
                    site_uuid,
                    "2026-08-31T00:00:00Z",
                    "2026-08-31T00:00:00Z",
                    json.dumps(metadata),
                    owner_uuid,
                    name,
                    occupant,
                ),
            )
    service = InventoryService(store)
    try:
        yield store, service, identities
    finally:
        store.close()


def _request(
    identities: dict[str, str],
    *,
    job_uuid: str = "40000000-0000-4000-8000-000000000101",
) -> DispatchAdmissionRequest:
    """构造包含转运全部资源和条件快照的准入请求。

    参数：``identities`` 是夹具资源身份；``job_uuid`` 是竞争作业身份。返回：
    冻结参数哈希、预期变更及完整锁集合。异常：无。
    """

    resources = (
        DispatchResource(
            lock_key=f"/devices/{identities['source_device']}",
            scope="device",
            material_uuid=identities["source_device"],
        ),
        DispatchResource(
            lock_key=f"/devices/{identities['target_device']}",
            scope="device",
            material_uuid=identities["target_device"],
        ),
        DispatchResource(
            lock_key=f"/devices/{identities['robot']}",
            scope="device",
            material_uuid=identities["robot"],
        ),
        DispatchResource(
            lock_key=f"material/{identities['vessel']}/exclusive",
            scope="material",
            material_uuid=identities["vessel"],
        ),
        DispatchResource(
            lock_key=(
                f"material/{identities['source_device']}/site/{SOURCE_SITE}/exclusive"
            ),
            scope="material_site",
            material_uuid=identities["source_device"],
            site_uuid=SOURCE_SITE,
        ),
        DispatchResource(
            lock_key=(
                f"material/{identities['target_device']}/site/{TARGET_SITE}/exclusive"
            ),
            scope="material_site",
            material_uuid=identities["target_device"],
            site_uuid=TARGET_SITE,
        ),
        DispatchResource(
            lock_key=(f"material/{identities['robot']}/site/{GRIPPER_SITE}/exclusive"),
            scope="material_site",
            material_uuid=identities["robot"],
            site_uuid=GRIPPER_SITE,
        ),
    )
    return DispatchAdmissionRequest(
        effect_uuid=f"50000000-0000-4000-8000-{job_uuid[-12:]}",
        task_uuid="30000000-0000-4000-8000-000000000101",
        job_uuid=job_uuid,
        attempt=1,
        parameter_hash="sha256:test-parameters",
        expected_change_set={
            "kind": "material_transfer",
            "material_uuid": identities["vessel"],
            "source_site_uuid": SOURCE_SITE,
            "target_site_uuid": TARGET_SITE,
        },
        resources=resources,
        transfer=TransferDispatchCondition(
            material_uuid=identities["vessel"],
            source_owner_material_uuid=identities["source_device"],
            source_site_uuid=SOURCE_SITE,
            target_owner_material_uuid=identities["target_device"],
            target_site_uuid=TARGET_SITE,
            executor_material_uuid=identities["robot"],
            gripper_site_uuid=GRIPPER_SITE,
        ),
    )


def _material_only_request(
    identities: dict[str, str],
    *,
    task_uuid: str,
    job_uuid: str,
) -> DispatchAdmissionRequest:
    """构造一个未声明 MaterialSource、但动作直接使用具体物料的派发请求。"""

    return DispatchAdmissionRequest(
        effect_uuid=f"50000000-0000-4000-8000-{job_uuid[-12:]}",
        task_uuid=task_uuid,
        job_uuid=job_uuid,
        attempt=1,
        parameter_hash=f"sha256:material-only:{job_uuid}",
        expected_change_set={"kind": "no_inventory_change"},
        resources=(
            DispatchResource(
                lock_key=f"material/{identities['vessel']}/exclusive",
                scope="material",
                material_uuid=identities["vessel"],
            ),
        ),
    )


def _admit_vessel_source(
    service: InventoryService,
    identities: dict[str, str],
    *,
    task_uuid: str,
    custody_policy: str,
) -> None:
    """通过公开任务准入入口绑定测试容器。"""

    service.admit_material_sources(
        task_uuid,
        [
            MaterialSourceAdmissionRequest(
                node_id="source-node",
                resource_template_uuid=identities["vessel_template"],
                custody_policy=custody_policy,
                requirement=MaterialRequirement(
                    template_id=identities["vessel_template"],
                    instance_uuid=identities["vessel"],
                ),
            )
        ],
    )


def test_task_exclusive_source_blocks_foreign_action_without_source_declaration(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """Task A 的独占来源必须阻止未声明来源的 Task B 动作使用同一物料。"""

    _store, service, identities = station_inventory
    owner_task_uuid = "30000000-0000-4000-8000-000000000181"
    foreign_task_uuid = "30000000-0000-4000-8000-000000000182"
    _admit_vessel_source(
        service,
        identities,
        task_uuid=owner_task_uuid,
        custody_policy="task_exclusive",
    )

    decision = service.station_resources.acquire_dispatch_permit(
        _material_only_request(
            identities,
            task_uuid=foreign_task_uuid,
            job_uuid="40000000-0000-4000-8000-000000000182",
        )
    )

    assert decision.acquired is False
    assert decision.wait_code == "task_material_claimed"
    assert decision.blocking_task_uuid == owner_task_uuid

    service.release_workflow(owner_task_uuid, reason="task_finished")
    retried = service.station_resources.acquire_dispatch_permit(
        _material_only_request(
            identities,
            task_uuid=foreign_task_uuid,
            job_uuid="40000000-0000-4000-8000-000000000182",
        )
    )
    assert retried.acquired is True


def test_task_exclusive_source_allows_another_action_of_the_owner_task(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """任务级独占不应阻止同一 Task 内不同动作按 JobActiveUse 仲裁。"""

    _store, service, identities = station_inventory
    owner_task_uuid = "30000000-0000-4000-8000-000000000183"
    _admit_vessel_source(
        service,
        identities,
        task_uuid=owner_task_uuid,
        custody_policy="task_exclusive",
    )

    decision = service.station_resources.acquire_dispatch_permit(
        _material_only_request(
            identities,
            task_uuid=owner_task_uuid,
            job_uuid="40000000-0000-4000-8000-000000000183",
        )
    )

    assert decision.acquired is True


def test_shared_source_does_not_block_foreign_action_before_job_lock(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """共享来源没有任务级占有；物料空闲时其他 Task 可直接取得动作锁。"""

    _store, service, identities = station_inventory
    _admit_vessel_source(
        service,
        identities,
        task_uuid="30000000-0000-4000-8000-000000000184",
        custody_policy="shared_source",
    )

    decision = service.station_resources.acquire_dispatch_permit(
        _material_only_request(
            identities,
            task_uuid="30000000-0000-4000-8000-000000000185",
            job_uuid="40000000-0000-4000-8000-000000000185",
        )
    )

    assert decision.acquired is True


def test_active_foreign_material_action_blocks_task_exclusive_source_admission(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """反向竞态也要关闭：活动 JobActiveUse 期间不能新建外国任务独占。"""

    _store, service, identities = station_inventory
    active = service.station_resources.acquire_dispatch_permit(
        _material_only_request(
            identities,
            task_uuid="30000000-0000-4000-8000-000000000186",
            job_uuid="40000000-0000-4000-8000-000000000186",
        )
    )
    assert active.acquired is True

    with pytest.raises(InsufficientStock, match="动作"):
        _admit_vessel_source(
            service,
            identities,
            task_uuid="30000000-0000-4000-8000-000000000187",
            custody_policy="task_exclusive",
        )


@pytest.mark.parametrize("same_task", [False, True])
def test_shared_material_job_active_use_releases_at_action_boundary(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
    same_task: bool,
) -> None:
    """共享物料每次只允许一个动作，前一动作释放后下一动作立即可用。"""

    _store, service, identities = station_inventory
    owner_task_uuid = "30000000-0000-4000-8000-000000000188"
    _admit_vessel_source(
        service,
        identities,
        task_uuid=owner_task_uuid,
        custody_policy="shared_source",
    )
    first = service.station_resources.acquire_dispatch_permit(
        _material_only_request(
            identities,
            task_uuid=owner_task_uuid,
            job_uuid="40000000-0000-4000-8000-000000000188",
        )
    )
    assert first.acquired and first.permit is not None

    next_task_uuid = (
        owner_task_uuid
        if same_task
        else "30000000-0000-4000-8000-000000000189"
    )
    next_request = _material_only_request(
        identities,
        task_uuid=next_task_uuid,
        job_uuid="40000000-0000-4000-8000-000000000189",
    )
    blocked = service.station_resources.acquire_dispatch_permit(next_request)
    assert blocked.acquired is False
    assert blocked.wait_code == "resource_claimed"
    assert blocked.blocking_job_uuid == first.permit.job_uuid

    service.station_resources.transition_dispatch_permit(
        first.permit.claim_uuid,
        target_state="released",
    )
    admitted = service.station_resources.acquire_dispatch_permit(next_request)
    assert admitted.acquired is True


def test_dispatch_admission_request_preserves_legacy_condition_positions() -> None:
    """新增资源字段不能改变三个既有条件参数的位置构造顺序。"""

    transfer = TransferDispatchCondition(
        material_uuid="material",
        source_owner_material_uuid="source-owner",
        source_site_uuid="source-site",
        target_owner_material_uuid="target-owner",
        target_site_uuid="target-site",
        executor_material_uuid="executor",
        gripper_site_uuid="gripper-site",
    )
    operate_in_place = OperateInPlaceCondition(
        material_uuid="material",
        site_owner_material_uuid="site-owner",
        site_uuid="site",
        device_material_uuid="device",
    )
    aliquot = AliquotDispatchCondition(
        source_material_uuid="source",
        target_material_uuids=("target",),
    )

    request = DispatchAdmissionRequest(
        "effect",
        "task",
        "job",
        1,
        "sha256:parameters",
        {},
        (),
        (),
        (),
        transfer,
        operate_in_place,
        aliquot,
    )

    assert request.transfer is transfer
    assert request.operate_in_place is operate_in_place
    assert request.aliquot is aliquot
    assert request.shared_scope_lock_keys == ()
    assert request.reserved_target_site_uuids == ()


@pytest.mark.parametrize("scope", ["device", "material", "material_site"])
def test_dispatch_resource_identity_mismatch_writes_nothing(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
    scope: str,
) -> None:
    """物理描述字段与 canonical key 错配时必须在写 Claim 前失败关闭。"""

    store, service, identities = station_inventory
    request = _request(identities)
    resources = list(request.resources)
    index = next(i for i, resource in enumerate(resources) if resource.scope == scope)
    if scope == "device":
        resources[index] = replace(
            resources[index],
            material_uuid=identities["target_device"],
        )
    elif scope == "material":
        resources[index] = replace(
            resources[index],
            material_uuid=identities["source_device"],
        )
    else:
        resources[index] = replace(
            resources[index],
            material_uuid=identities["target_device"],
            site_uuid=TARGET_SITE,
        )

    with pytest.raises(DispatchAdmissionConflict, match="身份.*lock_key"):
        service.station_resources.acquire_dispatch_permit(
            replace(request, resources=tuple(resources))
        )

    assert store.query_all("SELECT * FROM station_execution_claim") == []
    assert store.query_all("SELECT * FROM station_execution_lock_lease") == []
    assert store.query_all("SELECT * FROM station_execution_fence_counter") == []


@pytest.mark.parametrize("scope", ["device", "material"])
def test_dispatch_non_site_resource_rejects_extra_site_identity(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
    scope: str,
) -> None:
    """Inventory 仍要求 descriptor 完整精确，且拒绝物理键外的 Site 身份。"""

    store, service, identities = station_inventory
    request = _request(identities)
    resources = list(request.resources)
    index = next(i for i, resource in enumerate(resources) if resource.scope == scope)
    resources[index] = replace(resources[index], site_uuid=SOURCE_SITE)

    with pytest.raises(DispatchAdmissionConflict, match="身份.*lock_key"):
        service.station_resources.acquire_dispatch_permit(
            replace(request, resources=tuple(resources))
        )

    assert store.query_all("SELECT * FROM station_execution_claim") == []
    assert store.query_all("SELECT * FROM station_execution_lock_lease") == []
    assert store.query_all("SELECT * FROM station_execution_fence_counter") == []


def test_exact_physical_resource_identities_acquire_one_atomic_permit(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """三类规范物理描述与键身份完全一致时仍可共同签发 Permit。"""

    store, service, identities = station_inventory
    resources = (
        DispatchResource(
            lock_key=f"/devices/{identities['source_device']}",
            scope="device",
            material_uuid=identities["source_device"],
        ),
        DispatchResource(
            lock_key=f"material/{identities['vessel']}/exclusive",
            scope="material",
            material_uuid=identities["vessel"],
        ),
        DispatchResource(
            lock_key=(
                f"material/{identities['source_device']}/site/{SOURCE_SITE}/exclusive"
            ),
            scope="material_site",
            material_uuid=identities["source_device"],
            site_uuid=SOURCE_SITE,
        ),
    )
    request = DispatchAdmissionRequest(
        effect_uuid="50000000-0000-4000-8000-000000000151",
        task_uuid="30000000-0000-4000-8000-000000000151",
        job_uuid="40000000-0000-4000-8000-000000000151",
        attempt=1,
        parameter_hash="sha256:exact-physical-identities",
        expected_change_set={"kind": "no_inventory_change"},
        resources=resources,
    )

    decision = service.station_resources.acquire_dispatch_permit(request)

    assert decision.acquired is True
    assert {fence.lock_key for fence in decision.fences} == {
        resource.lock_key for resource in resources
    }
    assert store.query_one(
        "SELECT COUNT(*) AS count FROM station_execution_lock_lease "
        "WHERE claim_uuid=?",
        (decision.claim_uuid,),
    ) == {"count": 3}


def test_transfer_conditions_and_all_claims_commit_in_one_inventory_transaction(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """门禁 7 应一次产生 Claim、全部 Fence 和可审计预期变更。

    参数：``station_inventory`` 提供完整转运事实。返回：无；断言库存库中的
    Claim 与七项 Fence 使用同一身份。异常：准入失败表示完整资源集未被证明。
    """

    store, service, identities = station_inventory
    permit = service.station_resources.acquire_dispatch_permit(_request(identities))

    assert permit.acquired is True
    assert permit.claim_uuid
    assert permit.effect_uuid.startswith("50000000-")
    assert len(permit.fences) == 7
    assert store.query_one(
        "SELECT state,parameter_hash FROM station_execution_claim WHERE claim_uuid=?",
        (permit.claim_uuid,),
    ) == {"state": "prepared", "parameter_hash": "sha256:test-parameters"}


def test_continuation_admission_releases_only_previous_job_claim_after_handoff(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """连续区间的物理预持有资源允许同任务后继接管，并收敛前一 Job Claim。"""

    store, service, identities = station_inventory
    shared_key = f"/devices/{identities['source_device']}"
    trailing_key = f"/devices/{identities['target_device']}"
    first_job = "40000000-0000-4000-8000-000000000201"
    second_job = "40000000-0000-4000-8000-000000000202"
    task_uuid = "30000000-0000-4000-8000-000000000201"

    first = service.station_resources.acquire_dispatch_permit(
        DispatchAdmissionRequest(
            effect_uuid="50000000-0000-4000-8000-000000000201",
            task_uuid=task_uuid,
            job_uuid=first_job,
            attempt=1,
            parameter_hash="sha256:interval-first",
            expected_change_set={"kind": "no_inventory_change"},
            resources=(
                DispatchResource(
                    lock_key=shared_key,
                    scope="device",
                    material_uuid=identities["source_device"],
                ),
                DispatchResource(
                    lock_key=trailing_key,
                    scope="device",
                    material_uuid=identities["target_device"],
                ),
            ),
        )
    )
    assert first.acquired and first.permit is not None
    service.station_resources.transition_dispatch_permit(
        first.permit.claim_uuid,
        target_state="reserved",
    )
    service.station_resources.transition_dispatch_permit(
        first.permit.claim_uuid,
        target_state="running",
    )
    service.station_resources.retain_dispatch_permit_resources(
        first.permit.claim_uuid,
        keep_lock_keys=(shared_key,),
    )

    second = service.station_resources.acquire_dispatch_permit(
        DispatchAdmissionRequest(
            effect_uuid="50000000-0000-4000-8000-000000000202",
            task_uuid=task_uuid,
            job_uuid=second_job,
            attempt=1,
            parameter_hash="sha256:interval-second",
            expected_change_set={"kind": "no_inventory_change"},
            resources=(
                DispatchResource(
                    lock_key=shared_key,
                    scope="device",
                    material_uuid=identities["source_device"],
                ),
            ),
            preheld_lock_keys=(shared_key,),
            preheld_job_uuids=(first_job,),
        )
    )
    assert second.acquired and second.permit is not None
    assert second.permit.claim_uuid != first.permit.claim_uuid
    service.station_resources.release_preheld_dispatch_claims(
        task_uuid=task_uuid,
        job_uuids=(first_job,),
        lock_keys=(shared_key,),
    )
    assert store.query_one(
        "SELECT state FROM station_execution_claim WHERE claim_uuid=?",
        (first.permit.claim_uuid,),
    ) == {"state": "released"}
    assert store.query_one(
        "SELECT state FROM station_execution_claim WHERE claim_uuid=?",
        (second.permit.claim_uuid,),
    ) == {"state": "prepared"}
    assert store.query_all(
        "SELECT lock_key FROM station_execution_lock_lease "
        "WHERE claim_uuid=? AND state <> 'released'",
        (first.permit.claim_uuid,),
    ) == []


@pytest.mark.parametrize("proved_result", [True, False])
def test_unknown_then_proved_success_restores_continuation_handoff(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
    proved_result: bool,
) -> None:
    """UNKNOWN 后证明成功的保留资源收敛回 running；未证明时后继仍被拒绝。"""

    store, service, identities = station_inventory
    shared_key = f"/devices/{identities['source_device']}"
    trailing_key = f"/devices/{identities['target_device']}"
    first_job = "40000000-0000-4000-8000-000000000231"
    second_job = "40000000-0000-4000-8000-000000000232"
    task_uuid = "30000000-0000-4000-8000-000000000231"
    first = service.station_resources.acquire_dispatch_permit(
        DispatchAdmissionRequest(
            effect_uuid="50000000-0000-4000-8000-000000000231",
            task_uuid=task_uuid,
            job_uuid=first_job,
            attempt=1,
            parameter_hash="sha256:unknown-first",
            expected_change_set={"kind": "no_inventory_change"},
            resources=(
                DispatchResource(
                    lock_key=shared_key,
                    scope="device",
                    material_uuid=identities["source_device"],
                ),
                DispatchResource(
                    lock_key=trailing_key,
                    scope="device",
                    material_uuid=identities["target_device"],
                ),
            ),
        )
    )
    assert first.acquired and first.permit is not None
    claim_uuid = first.permit.claim_uuid
    for state in ("reserved", "running", "uncertain"):
        service.station_resources.transition_dispatch_permit(
            claim_uuid, target_state=state
        )

    service.station_resources.retain_dispatch_permit_resources(
        claim_uuid,
        keep_lock_keys=(shared_key,),
        proved_result=proved_result,
    )

    expected = "running" if proved_result else "uncertain"
    assert store.query_one(
        "SELECT state FROM station_execution_claim WHERE claim_uuid=?",
        (claim_uuid,),
    ) == {"state": expected}
    assert store.query_all(
        "SELECT lock_key, state FROM station_execution_lock_lease "
        "WHERE claim_uuid=? ORDER BY lock_key",
        (claim_uuid,),
    ) == sorted(
        [
            {"lock_key": shared_key, "state": expected},
            {"lock_key": trailing_key, "state": "released"},
        ],
        key=lambda row: row["lock_key"],
    )
    successor = DispatchAdmissionRequest(
        effect_uuid="50000000-0000-4000-8000-000000000232",
        task_uuid=task_uuid,
        job_uuid=second_job,
        attempt=1,
        parameter_hash="sha256:unknown-second",
        expected_change_set={"kind": "no_inventory_change"},
        resources=(
            DispatchResource(
                lock_key=shared_key,
                scope="device",
                material_uuid=identities["source_device"],
            ),
        ),
        preheld_lock_keys=(shared_key,),
        preheld_job_uuids=(first_job,),
    )
    if not proved_result:
        with pytest.raises(DispatchAdmissionConflict, match="缺少活动前驱租约"):
            service.station_resources.acquire_dispatch_permit(successor)
        return
    second = service.station_resources.acquire_dispatch_permit(successor)
    assert second.acquired and second.permit is not None
    service.station_resources.release_preheld_dispatch_claims(
        task_uuid=task_uuid,
        job_uuids=(first_job,),
        lock_keys=(shared_key,),
    )
    assert store.query_one(
        "SELECT state FROM station_execution_claim WHERE claim_uuid=?",
        (claim_uuid,),
    ) == {"state": "released"}


def test_preheld_admission_rejects_missing_active_predecessor_without_writes(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """声明预持有资源却没有活动前驱租约时必须关闭失败且不写新凭据。"""

    store, service, identities = station_inventory
    shared_key = f"/devices/{identities['source_device']}"
    successor_job = "40000000-0000-4000-8000-000000000222"
    before = {
        "claims": store.query_one(
            "SELECT COUNT(*) AS count FROM station_execution_claim"
        ),
        "leases": store.query_one(
            "SELECT COUNT(*) AS count FROM station_execution_lock_lease"
        ),
        "fences": store.query_one(
            "SELECT COUNT(*) AS count FROM station_execution_fence_counter"
        ),
    }

    with pytest.raises(
        DispatchAdmissionConflict,
        match="连续区间预持有资源缺少活动前驱租约",
    ):
        service.station_resources.acquire_dispatch_permit(
            DispatchAdmissionRequest(
                effect_uuid="50000000-0000-4000-8000-000000000222",
                task_uuid="30000000-0000-4000-8000-000000000222",
                job_uuid=successor_job,
                attempt=1,
                parameter_hash="sha256:missing-preheld-predecessor",
                expected_change_set={"kind": "no_inventory_change"},
                resources=(
                    DispatchResource(
                        lock_key=shared_key,
                        scope="device",
                        material_uuid=identities["source_device"],
                    ),
                ),
                preheld_lock_keys=(shared_key,),
                preheld_job_uuids=("40000000-0000-4000-8000-000000000221",),
            )
        )

    assert store.query_one(
        "SELECT claim_uuid FROM station_execution_claim WHERE job_uuid=?",
        (successor_job,),
    ) is None
    assert {
        "claims": store.query_one(
            "SELECT COUNT(*) AS count FROM station_execution_claim"
        ),
        "leases": store.query_one(
            "SELECT COUNT(*) AS count FROM station_execution_lock_lease"
        ),
        "fences": store.query_one(
            "SELECT COUNT(*) AS count FROM station_execution_fence_counter"
        ),
    } == before


def test_preheld_admission_rejects_released_predecessor_without_writes(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """已释放的前驱租约不能证明区间仍连续，也不能推进 Fence。"""

    store, service, identities = station_inventory
    shared_key = f"/devices/{identities['source_device']}"
    task_uuid = "30000000-0000-4000-8000-000000000231"
    predecessor_job = "40000000-0000-4000-8000-000000000231"
    successor_job = "40000000-0000-4000-8000-000000000232"
    predecessor = service.station_resources.acquire_dispatch_permit(
        DispatchAdmissionRequest(
            effect_uuid="50000000-0000-4000-8000-000000000231",
            task_uuid=task_uuid,
            job_uuid=predecessor_job,
            attempt=1,
            parameter_hash="sha256:released-preheld-predecessor",
            expected_change_set={"kind": "no_inventory_change"},
            resources=(
                DispatchResource(
                    lock_key=shared_key,
                    scope="device",
                    material_uuid=identities["source_device"],
                ),
            ),
        )
    )
    assert predecessor.acquired and predecessor.permit is not None
    service.station_resources.transition_dispatch_permit(
        predecessor.permit.claim_uuid,
        target_state="released",
    )
    fence_before = store.query_one(
        "SELECT last_fencing_token FROM station_execution_fence_counter "
        "WHERE lock_key=?",
        (shared_key,),
    )

    with pytest.raises(
        DispatchAdmissionConflict,
        match="连续区间预持有资源缺少活动前驱租约",
    ):
        service.station_resources.acquire_dispatch_permit(
            DispatchAdmissionRequest(
                effect_uuid="50000000-0000-4000-8000-000000000232",
                task_uuid=task_uuid,
                job_uuid=successor_job,
                attempt=1,
                parameter_hash="sha256:released-preheld-successor",
                expected_change_set={"kind": "no_inventory_change"},
                resources=(
                    DispatchResource(
                        lock_key=shared_key,
                        scope="device",
                        material_uuid=identities["source_device"],
                    ),
                ),
                preheld_lock_keys=(shared_key,),
                preheld_job_uuids=(predecessor_job,),
            )
        )

    assert store.query_one(
        "SELECT claim_uuid FROM station_execution_claim WHERE job_uuid=?",
        (successor_job,),
    ) is None
    assert store.query_one(
        "SELECT last_fencing_token FROM station_execution_fence_counter "
        "WHERE lock_key=?",
        (shared_key,),
    ) == fence_before


def test_preheld_admission_requires_the_declared_previous_job_identity(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """同 Task 的其他并行 Job 不能冒充声明前驱来证明连续占用。"""

    store, service, identities = station_inventory
    shared_key = f"/devices/{identities['source_device']}"
    first_job = "40000000-0000-4000-8000-000000000211"
    second_job = "40000000-0000-4000-8000-000000000212"
    task_uuid = "30000000-0000-4000-8000-000000000211"
    first = service.station_resources.acquire_dispatch_permit(
        DispatchAdmissionRequest(
            effect_uuid="50000000-0000-4000-8000-000000000211",
            task_uuid=task_uuid,
            job_uuid=first_job,
            attempt=1,
            parameter_hash="sha256:preheld-first",
            expected_change_set={"kind": "no_inventory_change"},
            resources=(
                DispatchResource(
                    lock_key=shared_key,
                    scope="device",
                    material_uuid=identities["source_device"],
                ),
            ),
        )
    )
    assert first.acquired and first.permit is not None
    service.station_resources.transition_dispatch_permit(
        first.permit.claim_uuid,
        target_state="reserved",
    )
    with pytest.raises(
        DispatchAdmissionConflict,
        match="连续区间预持有资源缺少活动前驱租约",
    ):
        service.station_resources.acquire_dispatch_permit(
            DispatchAdmissionRequest(
                effect_uuid="50000000-0000-4000-8000-000000000212",
                task_uuid=task_uuid,
                job_uuid=second_job,
                attempt=1,
                parameter_hash="sha256:preheld-second",
                expected_change_set={"kind": "no_inventory_change"},
                resources=(
                    DispatchResource(
                        lock_key=shared_key,
                        scope="device",
                        material_uuid=identities["source_device"],
                    ),
                ),
                preheld_lock_keys=(shared_key,),
                preheld_job_uuids=("40000000-0000-4000-8000-000000000299",),
            )
        )
    assert store.query_one(
        "SELECT claim_uuid FROM station_execution_claim WHERE job_uuid=?",
        (second_job,),
    ) is None


def test_gate7_claims_fallback_site_in_same_inventory_transaction(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """首选库位已有 Claim 时 Gate 7 必须在同一事务回退下一候选。"""

    store, service, identities = station_inventory
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO site(
                uuid,create_time,update_time,meta_data,material_uuid,name,
                sort_order,allowed_resource_template_uuids,
                occupied_material_uuid,position_x,position_y,position_z,
                depth,length,width
            ) VALUES (?,?,?,'{}',?,?,1,'[]',NULL,0,0,0,0,0,0)
            """,
            (
                TARGET_SITE_FALLBACK,
                "2026-08-31T00:00:00Z",
                "2026-08-31T00:00:00Z",
                identities["target_device"],
                "IN-FALLBACK",
            ),
        )
    blocker = DispatchAdmissionRequest(
        effect_uuid="50000000-0000-4000-8000-000000000199",
        task_uuid="30000000-0000-4000-8000-000000000199",
        job_uuid="40000000-0000-4000-8000-000000000199",
        attempt=1,
        parameter_hash="sha256:block-target-b",
        expected_change_set={"kind": "no_inventory_change"},
        resources=(
            DispatchResource(
                lock_key=(
                    f"material/{identities['target_device']}/site/"
                    f"{TARGET_SITE}/exclusive"
                ),
                scope="material_site",
                material_uuid=identities["target_device"],
                site_uuid=TARGET_SITE,
            ),
        ),
    )
    service.station_resources.acquire_dispatch_permit(blocker)

    first = _request(identities)
    target_key = f"material/{identities['target_device']}/site/{TARGET_SITE}/exclusive"
    fallback_resources = tuple(
        resource for resource in first.resources if resource.lock_key != target_key
    ) + (
        DispatchResource(
            lock_key=(
                f"material/{identities['target_device']}/site/"
                f"{TARGET_SITE_FALLBACK}/exclusive"
            ),
            scope="material_site",
            material_uuid=identities["target_device"],
            site_uuid=TARGET_SITE_FALLBACK,
        ),
    )
    fallback = replace(
        first,
        effect_uuid="50000000-0000-4000-8000-000000000102",
        parameter_hash="sha256:fallback-parameters",
        expected_change_set={
            "kind": "material_transfer",
            "material_uuid": identities["vessel"],
            "source_site_uuid": SOURCE_SITE,
            "target_site_uuid": TARGET_SITE_FALLBACK,
        },
        resources=fallback_resources,
        transfer=replace(first.transfer, target_site_uuid=TARGET_SITE_FALLBACK),
    )

    decision = service.station_resources.acquire_dispatch_permit_candidates(
        (first, fallback)
    )

    assert decision.acquired is True
    assert decision.selected_candidate_index == 1
    assert decision.permit is not None
    assert decision.permit.parameter_hash == "sha256:fallback-parameters"
    claimed_sites = {
        row["site_uuid"]
        for row in store.query_all(
            "SELECT site_uuid FROM station_execution_lock_lease "
            "WHERE claim_uuid=? AND site_uuid IS NOT NULL",
            (decision.claim_uuid,),
        )
    }
    assert TARGET_SITE_FALLBACK in claimed_sites
    assert TARGET_SITE not in claimed_sites

    service.station_resources.transition_dispatch_permit(
        decision.claim_uuid,
        target_state="released",
    )
    replay = service.station_resources.acquire_dispatch_permit_candidates(
        (first, fallback)
    )

    assert replay.acquired is True
    assert replay.selected_candidate_index == 1
    assert replay.claim_uuid == decision.claim_uuid
    assert [fence.fencing_token for fence in replay.fences] == [
        fence.fencing_token + 1 for fence in decision.fences
    ]


def test_transfer_endpoint_warehouses_lock_their_device_ancestors(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """仓库拥有库位时，门禁应校验其真实设备祖先而非把仓库当设备。"""

    store, service, identities = station_inventory
    source_warehouse = "20000000-0000-4000-8000-000000000101"
    target_warehouse = "20000000-0000-4000-8000-000000000102"
    with store.transaction() as connection:
        for uuid, parent_uuid, name in (
            (source_warehouse, identities["source_device"], "来源仓库"),
            (target_warehouse, identities["target_device"], "目标仓库"),
        ):
            connection.execute(
                """
                INSERT INTO material(
                    uuid,create_time,update_time,deleted_at,description,meta_data,
                    resource_template_uuid,parent_uuid,class,type,barcode,name,
                    config,data
                )
                SELECT ?,create_time,update_time,NULL,description,'{}',
                       resource_template_uuid,?,class,'warehouse',?,?,'{}','{}'
                FROM material WHERE uuid=?
                """,
                (uuid, parent_uuid, f"WAREHOUSE-{uuid[-3:]}", name, parent_uuid),
            )
        connection.execute(
            "UPDATE site SET material_uuid=? WHERE uuid=?",
            (source_warehouse, SOURCE_SITE),
        )
        connection.execute(
            "UPDATE site SET material_uuid=? WHERE uuid=?",
            (target_warehouse, TARGET_SITE),
        )

    request = _request(identities)
    endpoint_site_keys = {
        f"material/{identities['source_device']}/site/{SOURCE_SITE}/exclusive",
        f"material/{identities['target_device']}/site/{TARGET_SITE}/exclusive",
    }
    resources = tuple(
        resource
        for resource in request.resources
        if resource.lock_key not in endpoint_site_keys
    ) + (
        DispatchResource(
            lock_key=f"material/{source_warehouse}/site/{SOURCE_SITE}/exclusive",
            scope="material_site",
            material_uuid=source_warehouse,
            site_uuid=SOURCE_SITE,
        ),
        DispatchResource(
            lock_key=f"material/{target_warehouse}/site/{TARGET_SITE}/exclusive",
            scope="material_site",
            material_uuid=target_warehouse,
            site_uuid=TARGET_SITE,
        ),
    )
    request = replace(
        request,
        resources=resources,
        transfer=replace(
            request.transfer,
            source_owner_material_uuid=source_warehouse,
            target_owner_material_uuid=target_warehouse,
        ),
    )

    permit = service.station_resources.acquire_dispatch_permit(request)

    assert permit.acquired is True
    assert len(permit.fences) == 7


@pytest.mark.parametrize("endpoint", ["source_device", "target_device"])
def test_transfer_endpoint_owner_lock_does_not_claim_unrelated_bottles(station_inventory, endpoint):
    """搬运端点的整仓执行锁不扩展任务保管范围，但整仓修改仍检查内容物。"""
    from uuid import uuid4

    from unilabos.app.scheduler.inventory.dispatch_admission import assert_inventory_mutation_unclaimed
    from unilabos.app.scheduler.resource_lock import material_lock_key

    store, service, identities = station_inventory
    owner = identities[endpoint]
    other = BackendResourceService(store).create_material({
        "resource_template_uuid": identities["vessel_template"],
        "name": "另一任务的粉瓶", "parent_uuid": owner,
    })["uuid"]
    foreign_task = str(uuid4())
    service.admit_material_sources(foreign_task, [MaterialSourceAdmissionRequest(
        node_id=str(uuid4()), resource_template_uuid=identities["vessel_template"],
        custody_policy="task_exclusive",
        requirement=MaterialRequirement(template_id=identities["vessel_template"], instance_uuid=other),
    )])
    request = _request(identities)
    request = replace(request, resources=(*request.resources, DispatchResource(material_lock_key(owner), "material", owner)))
    # 真正使用整个载体、直接使用别人的瓶子，以及公共修改均不得借端点规则放行。
    for material in (owner, other):
        unsafe = replace(request, transfer=None, expected_change_set={},
                         resources=(DispatchResource(material_lock_key(material), "material", material),))
        blocked = service.station_resources.acquire_dispatch_permit(unsafe)
        assert blocked.wait_code == "task_material_claimed"
        assert blocked.blocking_task_uuid == foreign_task
    with store.transaction() as connection, pytest.raises(InventoryMutationConflict):
        assert_inventory_mutation_unclaimed(connection, material_uuids=[owner])

    permit = service.station_resources.acquire_dispatch_permit(request)
    assert permit.acquired, permit.wait_message
    # 另一任务仍不能同时占用同一设备/整仓执行锁。
    competitor = replace(request, effect_uuid=str(uuid4()), job_uuid=str(uuid4()), task_uuid=str(uuid4()))
    assert service.station_resources.acquire_dispatch_permit(competitor).wait_code == "resource_claimed"
    if endpoint == "target_device":
        continuation = replace(request, effect_uuid=str(uuid4()), job_uuid=str(uuid4()),
            transfer=None, expected_change_set={"kind": "no_inventory_change"},
            preheld_lock_keys=tuple(r.lock_key for r in request.resources),
            preheld_job_uuids=(request.job_uuid,), custody_endpoint_site_uuids=(TARGET_SITE,))
        assert service.station_resources.acquire_dispatch_permit(continuation).acquired


def test_transfer_expected_change_must_match_condition_snapshot(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """转运条件与预期变化不一致时不得创建 Claim。"""

    store, service, identities = station_inventory
    request = replace(
        _request(identities),
        expected_change_set={
            "kind": "material_transfer",
            "material_uuid": identities["vessel"],
            "source_site_uuid": SOURCE_SITE,
            "target_site_uuid": "wrong-target-site",
        },
    )

    with pytest.raises(DispatchAdmissionConflict) as raised:
        service.station_resources.acquire_dispatch_permit(request)

    assert "ChangeSet" in str(raised.value)
    assert store.query_all("SELECT * FROM station_execution_claim") == []


def test_changed_target_fact_rolls_back_whole_claim(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """目标库位在门禁窗口被占用时不得留下机械臂或设备的部分 Claim。

    参数：``station_inventory`` 提供预先解析过的请求。返回：无；断言库存事实
    改变后抛稳定等待条件，Claim 与 Lease 均为零。异常：仅预期资源条件冲突。
    """

    store, service, identities = station_inventory
    request = _request(identities)
    with store.transaction() as connection:
        connection.execute(
            "UPDATE site SET occupied_material_uuid=? WHERE uuid=?",
            (identities["robot"], TARGET_SITE),
        )

    with pytest.raises(StationResourceError) as raised:
        service.station_resources.acquire_dispatch_permit(request)

    assert raised.value.code == "site_occupied"
    assert raised.value.resources == (
        {
            "scope": "material_site",
            "material_uuid": identities["target_device"],
            "site_uuid": TARGET_SITE,
        },
    )
    assert store.query_all("SELECT * FROM station_execution_claim") == []
    assert store.query_all("SELECT * FROM station_execution_lock_lease") == []

    # B 的目标条件失败后，C 必须仍能取得机械臂；这直接证明门禁没有发生
    # “先占机械臂、再发现目标库位不满足”的部分取得。
    robot_only = DispatchAdmissionRequest(
        effect_uuid="50000000-0000-4000-8000-000000000103",
        task_uuid="30000000-0000-4000-8000-000000000103",
        job_uuid="40000000-0000-4000-8000-000000000103",
        attempt=1,
        parameter_hash="sha256:robot-only",
        expected_change_set={"kind": "no_inventory_change"},
        resources=(
            DispatchResource(
                lock_key=f"/devices/{identities['robot']}",
                scope="device",
                material_uuid=identities["robot"],
            ),
        ),
    )
    third = service.station_resources.acquire_dispatch_permit(robot_only)

    assert third.acquired is True
    assert len(store.query_all("SELECT * FROM station_execution_claim")) == 1


def test_wait_resource_descriptions_include_material_and_site_names(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """调度等待资源必须由库存权威补齐物料名和库位名。"""

    _store, service, identities = station_inventory

    assert service.station_resources.describe_wait_resources(
        (
            {
                "scope": "device",
                "device_id": identities["robot"],
            },
            {
                "scope": "material",
                "material_uuid": identities["vessel"],
            },
            {
                "scope": "material_site",
                "material_uuid": identities["target_device"],
                "site_uuid": TARGET_SITE,
            },
        )
    ) == (
        {
            "scope": "device",
            "device_id": identities["robot"],
            "device_name": "ROBOT",
        },
        {
            "scope": "material",
            "material_uuid": identities["vessel"],
            "material_name": "待搬容器",
        },
        {
            "scope": "material_site",
            "material_uuid": identities["target_device"],
            "material_name": "TARGET-DEVICE",
            "site_uuid": TARGET_SITE,
            "site_name": "IN",
        },
    )


def test_missing_transfer_source_reports_the_blocked_material(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """来源库位消失时，等待事实应指向待搬物料而不是目标库位。"""

    store, service, identities = station_inventory
    with store.transaction() as connection:
        connection.execute(
            "UPDATE site SET occupied_material_uuid=NULL WHERE uuid=?",
            (SOURCE_SITE,),
        )

    with pytest.raises(StationResourceError) as raised:
        service.station_resources.resolve_transfer_resources(
            TransferResourceRequest(
                resource_material_uuid=identities["vessel"],
                target_site_uuid=TARGET_SITE,
                target_owner_material_uuid=identities["target_device"],
                executor_material_uuid=identities["robot"],
                gripper_site_role="robot.gripper",
                require_device_owners=True,
            )
        )

    assert raised.value.code == "transfer_source_site_missing"
    assert raised.value.resources == (
        {"scope": "material", "material_uuid": identities["vessel"]},
    )


def test_occupied_gripper_reports_the_actual_gripper_site(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """夹爪被占用时，等待事实应指向夹爪库位而不是转运目标库位。"""

    store, service, identities = station_inventory
    with store.transaction() as connection:
        connection.execute(
            "UPDATE site SET occupied_material_uuid=? WHERE uuid=?",
            (identities["target_device"], GRIPPER_SITE),
        )

    with pytest.raises(StationResourceError) as raised:
        service.station_resources.resolve_transfer_resources(
            TransferResourceRequest(
                resource_material_uuid=identities["vessel"],
                target_site_uuid=TARGET_SITE,
                target_owner_material_uuid=identities["target_device"],
                executor_material_uuid=identities["robot"],
                gripper_site_role="robot.gripper",
                require_device_owners=True,
            )
        )

    assert raised.value.code == "gripper_site_occupied"
    assert raised.value.resources == (
        {
            "scope": "material_site",
            "material_uuid": identities["robot"],
            "site_uuid": GRIPPER_SITE,
        },
    )


def test_competing_job_cannot_claim_any_member_of_an_active_resource_set(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """另一个 Task/Job 不能在完整 Claim 存活期间先抢到机械臂。

    参数：``station_inventory`` 提供共享机械臂和位置。返回：无；断言第二次准入
    返回阻塞身份且不创建第二个 Claim。异常：无，资源竞争属于正常等待。
    """

    store, service, identities = station_inventory
    first = service.station_resources.acquire_dispatch_permit(_request(identities))
    second = service.station_resources.acquire_dispatch_permit(
        _request(
            identities,
            job_uuid="40000000-0000-4000-8000-000000000102",
        )
    )

    assert first.acquired is True
    assert second.acquired is False
    assert second.wait_code == "resource_claimed"
    assert second.blocking_job_uuid == "40000000-0000-4000-8000-000000000101"
    assert len(store.query_all("SELECT * FROM station_execution_claim")) == 1


def test_public_site_placement_cannot_bypass_active_claim(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """公共 Backend 物料接口不得在活动 Claim 存活时移除或改放物料。

    参数：``station_inventory`` 提供已占用来源库位。返回：无；断言公共
    ``update_material(site_placement=remove)`` 在同一库存事务内发现活动 Lease，
    返回稳定冲突码且来源占用不变。异常：未阻止时测试保持 RED。
    """

    store, service, identities = station_inventory
    permit = service.station_resources.acquire_dispatch_permit(_request(identities))
    assert permit.acquired is True

    with pytest.raises(BackendContractError) as raised:
        BackendResourceService(store).update_material(
            identities["vessel"],
            {"site_placement": {"action": "remove"}},
        )

    assert raised.value.code == MATERIAL_ACTIVE_CLAIM_CONFLICT
    assert store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?",
        (SOURCE_SITE,),
    ) == {"occupied_material_uuid": identities["vessel"]}


def test_legacy_inventory_move_cannot_bypass_active_claim(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """旧 InventoryService 写入口也必须服从同一活动 Claim 防线。

    参数：``station_inventory`` 提供共享规范表和兼容视图。返回：无；断言
    ``move_instance`` 在写物料/库位前抛稳定领域冲突，且目标仍为空。异常：公开
    命令绕过 Claim 时测试保持 RED。
    """

    store, service, identities = station_inventory
    permit = service.station_resources.acquire_dispatch_permit(_request(identities))
    assert permit.acquired is True

    with pytest.raises(InventoryMutationConflict):
        service.move_instance(
            identities["vessel"],
            identities["target_device"],
            "IN",
            actor="public-command",
        )

    assert store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?",
        (TARGET_SITE,),
    ) == {"occupied_material_uuid": None}


def test_physical_settlement_is_the_only_claim_authorized_inventory_writer(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """Scheduler 只有携带完整 Permit 的 PhysicalSettlement 能提交物理事实。

    参数：``station_inventory`` 提供活动 Claim。返回：无；断言同一 Claim 的
    effect/job/attempt/parameter hash/ChangeSet/Fence 全部匹配后，来源与目标库位在
    一个事务内切换。异常：任何凭据缺失或漂移必须关闭式失败。
    """

    store, service, identities = station_inventory
    request = _request(identities)
    decision = service.station_resources.acquire_dispatch_permit(request)
    assert decision.permit is not None
    permit = decision.permit
    service.station_resources.transition_dispatch_permit(
        permit.claim_uuid,
        target_state="reserved",
    )
    service.station_resources.transition_dispatch_permit(
        permit.claim_uuid,
        target_state="running",
    )

    settled = service.station_resources.settle_material_transfer(
        MaterialTransferCommand(
            material_uuid=identities["vessel"],
            target_owner_material_uuid=identities["target_device"],
            target_site_uuid=TARGET_SITE,
            target_site_name="IN",
            actor="station_scheduler.physical_settlement",
            causation_id=request.job_uuid,
            effect_uuid=permit.effect_uuid,
            claim_uuid=permit.claim_uuid,
            job_uuid=request.job_uuid,
            attempt=request.attempt,
            parameter_hash=request.parameter_hash,
            expected_change_set=request.expected_change_set,
            fences=permit.fences,
        )
    )

    assert settled["edge_uuid"] == identities["vessel"]
    assert store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?",
        (SOURCE_SITE,),
    ) == {"occupied_material_uuid": None}
    assert store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?",
        (TARGET_SITE,),
    ) == {"occupied_material_uuid": identities["vessel"]}
    transfer_ledger = store.query_all(
        "SELECT material_uuid,subject_type,aggregate_type,op_type,delta_json,"
        "workflow_task_uuid,workflow_node_job_uuid FROM inventory_ledger "
        "WHERE op_type='material.transfer'"
    )
    assert len(transfer_ledger) == 1
    assert transfer_ledger[0]["material_uuid"] == identities["vessel"]
    assert transfer_ledger[0]["subject_type"] == "material"
    assert transfer_ledger[0]["aggregate_type"] == "material"
    assert transfer_ledger[0]["workflow_task_uuid"] == request.task_uuid
    assert transfer_ledger[0]["workflow_node_job_uuid"] == request.job_uuid
    changes = json.loads(transfer_ledger[0]["delta_json"])["changes"]
    assert changes["name"] == "待搬容器"
    assert changes["site_name_before"] == "SOURCE-DEVICE / OUT"
    assert changes["site_name_after"] == "TARGET-DEVICE / IN"
    assert store.query_one(
        "SELECT COUNT(*) AS amount FROM sync_outbox WHERE event_type='material.transfer'"
    ) == {"amount": 0}
    service.station_resources.transition_dispatch_permit(
        permit.claim_uuid,
        target_state="released",
    )
    assert (
        service.station_resources.settle_material_transfer(
            MaterialTransferCommand(
                material_uuid=identities["vessel"],
                target_owner_material_uuid=identities["target_device"],
                target_site_uuid=TARGET_SITE,
                target_site_name="IN",
                actor="station_scheduler.physical_settlement",
                causation_id=request.job_uuid,
                effect_uuid=permit.effect_uuid,
                claim_uuid=permit.claim_uuid,
                job_uuid=request.job_uuid,
                attempt=request.attempt,
                parameter_hash=request.parameter_hash,
                expected_change_set=request.expected_change_set,
                fences=permit.fences,
            )
        )
        == settled
    )
    assert store.query_one(
        "SELECT COUNT(*) AS amount FROM inventory_ledger WHERE op_type='material.transfer'"
    ) == {"amount": 1}


def test_failed_transfer_can_settle_at_claimed_source_without_moving_material(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """未执行的失败转运可按同一 Claim 证明物料仍在来源库位。"""

    store, service, identities = station_inventory
    request = _request(identities)
    decision = service.station_resources.acquire_dispatch_permit(request)
    assert decision.permit is not None
    permit = decision.permit
    for state in ("reserved", "running", "uncertain"):
        service.station_resources.transition_dispatch_permit(
            permit.claim_uuid,
            target_state=state,
        )

    settled = service.station_resources.settle_material_transfer(
        MaterialTransferCommand(
            material_uuid=identities["vessel"],
            target_owner_material_uuid=identities["source_device"],
            target_site_uuid=SOURCE_SITE,
            target_site_name="OUT",
            actor="physical_settlement",
            causation_id=request.job_uuid,
            effect_uuid=permit.effect_uuid,
            claim_uuid=permit.claim_uuid,
            job_uuid=request.job_uuid,
            attempt=request.attempt,
            parameter_hash=request.parameter_hash,
            expected_change_set=request.expected_change_set,
            fences=permit.fences,
        )
    )

    assert settled["edge_uuid"] == identities["vessel"]
    assert store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?",
        (SOURCE_SITE,),
    ) == {"occupied_material_uuid": identities["vessel"]}
    assert store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?",
        (TARGET_SITE,),
    ) == {"occupied_material_uuid": None}
    assert store.query_one(
        "SELECT COUNT(*) AS amount FROM inventory_ledger WHERE op_type='material.transfer'"
    ) == {"amount": 0}


def test_failed_transfer_cannot_settle_at_unclaimed_site(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """失败转运对账不能把物料写入原 Claim 未覆盖的库位。"""

    store, service, identities = station_inventory
    unclaimed_site = "10000000-0000-4000-8000-000000000105"
    with store.transaction() as connection:
        connection.execute(
            """
            INSERT INTO site(
                uuid,create_time,update_time,meta_data,material_uuid,name,
                sort_order,allowed_resource_template_uuids,
                occupied_material_uuid,position_x,position_y,position_z,
                depth,length,width
            ) VALUES (?,?,?,'{}',?,?,2,'[]',NULL,0,0,0,0,0,0)
            """,
            (
                unclaimed_site,
                "2026-08-31T00:00:00Z",
                "2026-08-31T00:00:00Z",
                identities["target_device"],
                "UNCLAIMED",
            ),
        )
    request = _request(identities)
    decision = service.station_resources.acquire_dispatch_permit(request)
    assert decision.permit is not None
    permit = decision.permit

    with pytest.raises(StationResourceError, match="实际库位"):
        service.station_resources.settle_material_transfer(
            MaterialTransferCommand(
                material_uuid=identities["vessel"],
                target_owner_material_uuid=identities["target_device"],
                target_site_uuid=unclaimed_site,
                target_site_name="UNCLAIMED",
                effect_uuid=permit.effect_uuid,
                claim_uuid=permit.claim_uuid,
                job_uuid=request.job_uuid,
                attempt=request.attempt,
                parameter_hash=request.parameter_hash,
                expected_change_set=request.expected_change_set,
                fences=permit.fences,
            )
        )


def test_released_claim_without_settlement_evidence_cannot_first_write(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """已释放 Claim 只允许读取既有 effect 证据，不能首次修改库存。"""

    store, service, identities = station_inventory
    request = _request(identities)
    decision = service.station_resources.acquire_dispatch_permit(request)
    assert decision.permit is not None
    permit = decision.permit
    service.station_resources.transition_dispatch_permit(
        permit.claim_uuid,
        target_state="reserved",
    )
    service.station_resources.transition_dispatch_permit(
        permit.claim_uuid,
        target_state="released",
    )

    with pytest.raises(DispatchAdmissionConflict, match="禁止首次修改库存"):
        service.station_resources.settle_material_transfer(
            MaterialTransferCommand(
                material_uuid=identities["vessel"],
                target_owner_material_uuid=identities["target_device"],
                target_site_uuid=TARGET_SITE,
                target_site_name="IN",
                effect_uuid=permit.effect_uuid,
                claim_uuid=permit.claim_uuid,
                job_uuid=request.job_uuid,
                attempt=request.attempt,
                parameter_hash=request.parameter_hash,
                expected_change_set=request.expected_change_set,
                fences=permit.fences,
            )
        )
    assert store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?",
        (SOURCE_SITE,),
    ) == {"occupied_material_uuid": identities["vessel"]}


def test_physical_settlement_rejects_stale_fence_without_partial_write(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """PhysicalSettlement 的任一 Fence 漂移都不得留下部分库存变化。"""

    store, service, identities = station_inventory
    request = _request(identities)
    decision = service.station_resources.acquire_dispatch_permit(request)
    assert decision.permit is not None
    permit = decision.permit
    service.station_resources.transition_dispatch_permit(
        permit.claim_uuid,
        target_state="reserved",
    )
    service.station_resources.transition_dispatch_permit(
        permit.claim_uuid,
        target_state="running",
    )
    stale_fences = (
        replace(permit.fences[0], fencing_token=permit.fences[0].fencing_token + 1),
        *permit.fences[1:],
    )

    with pytest.raises(DispatchAdmissionConflict, match="Fence"):
        service.station_resources.settle_material_transfer(
            MaterialTransferCommand(
                material_uuid=identities["vessel"],
                target_owner_material_uuid=identities["target_device"],
                target_site_uuid=TARGET_SITE,
                target_site_name="IN",
                effect_uuid=permit.effect_uuid,
                claim_uuid=permit.claim_uuid,
                job_uuid=request.job_uuid,
                attempt=request.attempt,
                parameter_hash=request.parameter_hash,
                expected_change_set=request.expected_change_set,
                fences=stale_fences,
            )
        )

    assert store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?",
        (SOURCE_SITE,),
    ) == {"occupied_material_uuid": identities["vessel"]}
    assert store.query_one(
        "SELECT occupied_material_uuid FROM site WHERE uuid=?",
        (TARGET_SITE,),
    ) == {"occupied_material_uuid": None}


def test_operate_in_place_gate_rechecks_device_site_and_material_together(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """原位操作必须原子证明物料仍占用实际执行设备内的精确库位。"""

    _store, service, identities = station_inventory
    request = DispatchAdmissionRequest(
        effect_uuid="50000000-0000-4000-8000-000000000199",
        task_uuid="30000000-0000-4000-8000-000000000199",
        job_uuid="40000000-0000-4000-8000-000000000199",
        attempt=1,
        parameter_hash="sha256:operate-in-place",
        expected_change_set={"kind": "no_inventory_change"},
        resources=(
            DispatchResource(
                lock_key=f"/devices/{identities['source_device']}",
                scope="device",
                material_uuid=identities["source_device"],
            ),
            DispatchResource(
                lock_key=f"material/{identities['vessel']}/exclusive",
                scope="material",
                material_uuid=identities["vessel"],
            ),
            DispatchResource(
                lock_key=(
                    f"material/{identities['source_device']}/site/"
                    f"{SOURCE_SITE}/exclusive"
                ),
                scope="material_site",
                material_uuid=identities["source_device"],
                site_uuid=SOURCE_SITE,
            ),
        ),
        operate_in_place=OperateInPlaceCondition(
            material_uuid=identities["vessel"],
            site_owner_material_uuid=identities["source_device"],
            site_uuid=SOURCE_SITE,
            device_material_uuid=identities["source_device"],
        ),
    )

    decision = service.station_resources.acquire_dispatch_permit(request)
    assert decision.acquired is True
    service.station_resources.transition_dispatch_permit(
        decision.claim_uuid,
        target_state="released",
    )

    wrong_device = replace(
        request,
        effect_uuid="50000000-0000-4000-8000-000000000198",
        job_uuid="40000000-0000-4000-8000-000000000198",
        operate_in_place=replace(
            request.operate_in_place,
            device_material_uuid=identities["target_device"],
        ),
    )
    with pytest.raises(DispatchAdmissionConflict, match="实际执行设备"):
        service.station_resources.acquire_dispatch_permit(wrong_device)


def test_aliquot_claim_and_full_receipt_settle_source_and_targets_atomically(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """分装必须锁定来源和全部目标，成功回执在一个库存事务内完成内容转移。"""

    store, service, identities = station_inventory
    backend = BackendResourceService(store)
    vessel_template = store.query_one(
        "SELECT resource_template_uuid FROM material WHERE uuid=?",
        (identities["vessel"],),
    )["resource_template_uuid"]
    with store.transaction() as connection:
        connection.execute(
            "UPDATE resource_template SET tags='[\"container\"]' WHERE uuid=?",
            (vessel_template,),
        )
    targets = tuple(
        backend.create_material(
            {
                "resource_template_uuid": vessel_template,
                "barcode": f"ALIQUOT-{index}",
                "name": f"分装目标 {index}",
            }
        )["uuid"]
        for index in (1, 2)
    )
    BackendContainerContentService(store).create_current_substance(
        {
            "material_uuid": identities["vessel"],
            "name": "母液",
            "components": [],
            "quantity": 10,
            "quantity_unit": "mL",
            "physical_state": "liquid",
        }
    )
    effect_uuid = "50000000-0000-4000-8000-000000000299"
    request = DispatchAdmissionRequest(
        effect_uuid=effect_uuid,
        task_uuid="30000000-0000-4000-8000-000000000299",
        job_uuid="40000000-0000-4000-8000-000000000299",
        attempt=1,
        parameter_hash="sha256:aliquot",
        expected_change_set={
            "kind": "material_content_aliquot",
            "source_material_uuid": identities["vessel"],
            "target_material_uuids": list(targets),
        },
        resources=tuple(
            DispatchResource(
                lock_key=f"material/{material_uuid}/exclusive",
                scope="material",
                material_uuid=material_uuid,
            )
            for material_uuid in (identities["vessel"], *targets)
        ),
        aliquot=AliquotDispatchCondition(
            source_material_uuid=identities["vessel"],
            target_material_uuids=targets,
        ),
    )
    decision = service.station_resources.acquire_dispatch_permit(request)
    assert decision.permit is not None
    permit = decision.permit
    service.station_resources.transition_dispatch_permit(
        permit.claim_uuid, target_state="reserved"
    )
    command = MaterialAliquotCommand(
        source_material_uuid=identities["vessel"],
        receipts=(
            AliquotReceipt(targets[0], 2.5, "mL"),
            AliquotReceipt(targets[1], 1.5, "mL"),
        ),
        effect_uuid=permit.effect_uuid,
        claim_uuid=permit.claim_uuid,
        job_uuid=request.job_uuid,
        attempt=request.attempt,
        parameter_hash=request.parameter_hash,
        expected_change_set=request.expected_change_set,
        fences=permit.fences,
    )
    settled = service.station_resources.settle_material_aliquot(command)

    assert settled["source_quantity"] == 6.0
    rows = store.query_all(
        "SELECT material_uuid,quantity FROM current_substance ORDER BY material_uuid"
    )
    assert {row["material_uuid"]: row["quantity"] for row in rows} == {
        identities["vessel"]: 6.0,
        targets[0]: 2.5,
        targets[1]: 1.5,
    }

    service.station_resources.transition_dispatch_permit(
        permit.claim_uuid, target_state="released"
    )
    assert service.station_resources.settle_material_aliquot(command) == settled
    assert store.query_one(
        "SELECT COUNT(*) AS amount FROM inventory_ledger "
        "WHERE causation_id=? AND op_type LIKE 'current_substance.aliquot_%'",
        (effect_uuid,),
    ) == {"amount": 3}


def test_startup_releases_only_unprojected_prepared_permits(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """跨库崩溃恢复只释放尚未投影到工作流库的 prepared Permit。"""

    store, service, identities = station_inventory
    orphan = service.station_resources.acquire_dispatch_permit(_request(identities))
    assert orphan.permit is not None

    released = service.station_resources.release_unprojected_dispatch_permits(
        known_claim_uuids=()
    )

    assert released == (orphan.claim_uuid,)
    assert store.query_one(
        "SELECT state FROM station_execution_claim WHERE claim_uuid=?",
        (orphan.claim_uuid,),
    ) == {"state": "released"}


def test_startup_keeps_prepared_permit_already_projected_to_workflow(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """工作流库已经持有同一 Claim 时，启动恢复不得回收其库存 Permit。"""

    store, service, identities = station_inventory
    projected = service.station_resources.acquire_dispatch_permit(_request(identities))

    released = service.station_resources.release_unprojected_dispatch_permits(
        known_claim_uuids=(projected.claim_uuid,)
    )

    assert released == ()
    assert store.query_one(
        "SELECT state FROM station_execution_claim WHERE claim_uuid=?",
        (projected.claim_uuid,),
    ) == {"state": "prepared"}


def test_released_prepared_permit_can_be_reprepared_for_same_job_attempt(
    station_inventory: tuple[InventoryStore, InventoryService, dict[str, str]],
) -> None:
    """门禁后续未通过时，未提交物理边界的同一效果可重新取得新 Fence。"""

    _store, service, identities = station_inventory
    request = _request(identities)
    first = service.station_resources.acquire_dispatch_permit(request)
    service.station_resources.transition_dispatch_permit(
        first.claim_uuid,
        target_state="released",
    )

    replay = service.station_resources.acquire_dispatch_permit(request)

    assert replay.acquired is True
    assert replay.claim_uuid == first.claim_uuid
    assert [fence.fencing_token for fence in replay.fences] == [
        fence.fencing_token + 1 for fence in first.fences
    ]


def test_planning_pick_destination_uses_live_occupancy_without_claiming(station_inventory):
    store, inventory, ids = station_inventory
    station = inventory.station_resources
    station.check_pick_destination(ids["target_device"], TARGET_SITE, ids["vessel"])
    station.check_pick_destination(ids["source_device"], SOURCE_SITE, ids["vessel"])
    with pytest.raises(StationResourceError, match=SOURCE_SITE) as error:
        station.check_pick_destination(ids["source_device"], SOURCE_SITE, "")
    assert error.value.code == "site_occupied"
    assert store.query_all("SELECT * FROM station_execution_claim") == []


def test_split_pick_reserves_empty_final_site_in_same_atomic_claim(station_inventory):
    """即使本次只取到夹爪，也先预留最终放料位。"""
    store, inventory, identities = station_inventory
    request = replace(
        _request(identities),
        transfer=None,
        expected_change_set={"kind": "no_inventory_change"},
        reserved_target_site_uuids=(TARGET_SITE,),
    )
    decision = inventory.station_resources.acquire_dispatch_permit(request)
    assert decision.acquired
    assert any(f.lock_key.endswith(f"/{TARGET_SITE}/exclusive") for f in decision.fences)


def test_occupied_future_site_rolls_back_entire_pick_claim(station_inventory):
    """未来目标被占用时不能先取得机械臂或部分资源。"""
    from unilabos.app.scheduler.inventory.dispatch_admission import TemporaryDispatchCondition

    store, inventory, identities = station_inventory
    request = replace(
        _request(identities),
        transfer=None,
        expected_change_set={"kind": "no_inventory_change"},
        reserved_target_site_uuids=(SOURCE_SITE,),
    )
    with pytest.raises((TemporaryDispatchCondition, StationResourceError)):
        inventory.station_resources.acquire_dispatch_permit(request)
    assert store.query_all("SELECT * FROM station_execution_claim") == []


def test_split_pick_place_settles_via_gripper_and_keeps_destination(station_inventory):
    """两次真实库存结算之间物料位于夹爪，最终目标持续预留。"""
    store, service, identities = station_inventory
    base = _request(identities)
    pick = replace(
        base,
        reserved_target_site_uuids=(TARGET_SITE,),
        transfer=replace(
            base.transfer,
            target_owner_material_uuid=identities["robot"],
            target_site_uuid=GRIPPER_SITE,
        ),
        expected_change_set={
            "kind": "material_transfer",
            "material_uuid": identities["vessel"],
            "source_site_uuid": SOURCE_SITE,
            "target_site_uuid": GRIPPER_SITE,
        },
    )

    def settle(request, owner, site):
        decision = service.station_resources.acquire_dispatch_permit(request)
        assert decision.acquired and decision.permit
        permit = decision.permit
        for state in ["reserved", "running"]:
            service.station_resources.transition_dispatch_permit(
                permit.claim_uuid, target_state=state
            )
        service.station_resources.settle_material_transfer(
            MaterialTransferCommand(
                material_uuid=identities["vessel"],
                target_owner_material_uuid=owner,
                target_site_uuid=site,
                target_site_name=store.query_one("SELECT name FROM site WHERE uuid=?", (site,))[
                    "name"
                ],
                actor="station_scheduler.physical_settlement",
                causation_id=request.job_uuid,
                effect_uuid=permit.effect_uuid,
                claim_uuid=permit.claim_uuid,
                job_uuid=request.job_uuid,
                attempt=request.attempt,
                parameter_hash=request.parameter_hash,
                expected_change_set=request.expected_change_set,
                fences=permit.fences,
            )
        )
        return permit

    first = settle(pick, identities["robot"], GRIPPER_SITE)
    assert (
        store.query_one("SELECT occupied_material_uuid FROM site WHERE uuid=?", (GRIPPER_SITE,))[
            "occupied_material_uuid"
        ]
        == identities["vessel"]
    )
    assert (
        store.query_one("SELECT occupied_material_uuid FROM site WHERE uuid=?", (TARGET_SITE,))[
            "occupied_material_uuid"
        ]
        is None
    )
    kept = tuple(r for r in base.resources if r.material_uuid != identities["source_device"])
    service.station_resources.retain_dispatch_permit_resources(
        first.claim_uuid, keep_lock_keys=tuple(r.lock_key for r in kept)
    )
    place = replace(
        base,
        effect_uuid="50000000-0000-4000-8000-000000000777",
        job_uuid="40000000-0000-4000-8000-000000000777",
        resources=kept,
        preheld_lock_keys=tuple(r.lock_key for r in kept),
        preheld_job_uuids=(pick.job_uuid,),
        transfer=replace(
            base.transfer,
            source_owner_material_uuid=identities["robot"],
            source_site_uuid=GRIPPER_SITE,
            allow_held_material=True,
        ),
        expected_change_set={
            "kind": "material_transfer",
            "material_uuid": identities["vessel"],
            "source_site_uuid": GRIPPER_SITE,
            "target_site_uuid": TARGET_SITE,
        },
    )
    second = settle(place, identities["target_device"], TARGET_SITE)
    service.station_resources.release_preheld_dispatch_claims(
        task_uuid=base.task_uuid, job_uuids=(pick.job_uuid,), lock_keys=place.preheld_lock_keys
    )
    service.station_resources.transition_dispatch_permit(second.claim_uuid, target_state="released")
    assert (
        store.query_one("SELECT occupied_material_uuid FROM site WHERE uuid=?", (GRIPPER_SITE,))[
            "occupied_material_uuid"
        ]
        is None
    )
    assert (
        store.query_one("SELECT occupied_material_uuid FROM site WHERE uuid=?", (TARGET_SITE,))[
            "occupied_material_uuid"
        ]
        == identities["vessel"]
    )


def test_late_transfer_preserves_manual_fact_after_partial_release(station_inventory):
    """人工修正并释放部分逻辑占用后，迟到成功只能报告差异，不能覆盖位置。"""
    store, service, ids = station_inventory
    request = _request(ids)
    permit = service.station_resources.acquire_dispatch_permit(request).permit
    service.station_resources.transition_dispatch_permit(permit.claim_uuid, target_state="reserved")
    service.station_resources.transition_dispatch_permit(permit.claim_uuid, target_state="running")
    snapshot = service.station_resources.error_handling_inventory()
    instance = next(item for item in snapshot["instances"] if item["edge_uuid"] == ids["vessel"])
    site = next(item for item in snapshot["sites"] if item["uuid"] == GRIPPER_SITE)
    service.correct_inventory("manual-correction", {
        "changes": [{"material_uuid": ids["vessel"], "parent_uuid": ids["robot"], "site_uuid": GRIPPER_SITE}],
        "expected_inventory_versions": {ids["vessel"]: instance["version"], GRIPPER_SITE: str(site["update_time"])},
        "operator_id": "tester", "reason": "实物已放在夹爪", "session_id": "session", "source_job_uuid": request.job_uuid,
    })
    released_fence = permit.fences[0]
    service.station_resources.release_error_occupancies([{
        "claim_uuid": permit.claim_uuid, "lock_key": released_fence.lock_key,
        "fencing_token": released_fence.fencing_token,
    }])
    with pytest.raises(StationResourceError) as error:
        service.station_resources.settle_material_transfer(MaterialTransferCommand(
            material_uuid=ids["vessel"], target_owner_material_uuid=ids["target_device"],
            target_site_uuid=TARGET_SITE, target_site_name="IN", actor="test", causation_id=request.job_uuid,
            effect_uuid=permit.effect_uuid, claim_uuid=permit.claim_uuid, job_uuid=request.job_uuid,
            attempt=request.attempt, parameter_hash=request.parameter_hash,
            expected_change_set=request.expected_change_set, fences=permit.fences,
        ))
    assert error.value.code == "inventory_fact_changed"
    service.station_resources.transition_dispatch_permit(permit.claim_uuid, target_state="uncertain")
    assert store.query_one("SELECT state FROM station_execution_lock_lease WHERE claim_uuid=? AND lock_key=?", (permit.claim_uuid, released_fence.lock_key))["state"] == "released"
    assert next(item for item in service.station_resources.error_handling_inventory()["instances"] if item["edge_uuid"] == ids["vessel"])["parent_uuid"] == ids["robot"]


def test_split_pick_can_reserve_its_own_return_site(station_inventory):
    """取头后归还原位：同一原子 Claim 必须锁住即将腾空的来源。"""
    store, service, identities = station_inventory
    base = _request(identities)
    pick = replace(
        base,
        reserved_target_site_uuids=(SOURCE_SITE,),
        transfer=replace(base.transfer, target_owner_material_uuid=identities["robot"], target_site_uuid=GRIPPER_SITE),
        expected_change_set={"kind": "material_transfer", "material_uuid": identities["vessel"],
                             "source_site_uuid": SOURCE_SITE, "target_site_uuid": GRIPPER_SITE},
    )
    decision = service.station_resources.acquire_dispatch_permit(pick)
    assert decision.acquired
    assert any(f.lock_key.endswith(f"/{SOURCE_SITE}/exclusive") for f in decision.fences)


@pytest.mark.parametrize(
    "scenario",
    [
        "valid",
        "single_material",
        "missing_site",
        "other_device",
        "duplicate_material",
        "missing_site_lock",
        "moved_after_planning",
        "missing_custody",
        "partial_proof",
        "changed_fence",
    ],
)
def test_in_place_dispatch(station_inventory, scenario):
    """从冻结资源计划到库存准入和派发复验，每个物料均须到位并受保护。"""
    import hashlib
    from dataclasses import asdict
    from uuid import uuid4

    from unilabos.workflow.device_tenancy import validate_native_operate_custody
    from unilabos.workflow.execution_plan import ExecutionPlanBuilder, ExecutionPlanBuildError
    from unilabos.workflow.inventory_resource_plan import bind_inventory_resource_plan
    from unilabos.workflow.resource_lock_key import device_lock_key, material_lock_key, site_lock_key
    from unilabos.workflow.store import StoreConflict
    from unilabos.workflow.task_scheduler_bridge import _operate_condition_mappings

    store, service, ids = station_inventory
    inventory = service.station_resources
    task_uuid, job_uuid = str(uuid4()), str(uuid4())
    device = ids["source_device"]
    peer = BackendResourceService(store).create_material(
        {
            "resource_template_uuid": ids["vessel_template"],
            "barcode": "SECOND",
            "parent_uuid": device,
            "name": "第二个容器",
        }
    )["uuid"]
    with store.transaction() as connection:
        connection.execute(
            "UPDATE site SET material_uuid=?, occupied_material_uuid=? WHERE uuid=?",
            (
                ids["target_device"] if scenario == "other_device" else device,
                None if scenario == "missing_site" else peer,
                TARGET_SITE,
            ),
        )
    materials = [ids["vessel"]] if scenario == "single_material" else [ids["vessel"], peer]
    service.admit_material_sources(
        task_uuid,
        [
            MaterialSourceAdmissionRequest(
                node_id=f"input-{index}",
                resource_template_uuid=ids["vessel_template"],
                custody_policy="task_exclusive",
                requirement=MaterialRequirement(template_id=ids["vessel_template"], instance_uuid=material),
            )
            for index, material in enumerate(materials)
            if index == 0 or scenario != "missing_custody"
        ],
    )
    params = {"first": {"uuid": ids["vessel"]}}
    if scenario != "single_material":
        params["second"] = {"uuid": peer}
    if scenario == "duplicate_material":
        params["second"] = params["first"]
    template_uuid, node_uuid = str(uuid4()), str(uuid4())
    operation = {"material_param": "first"} if scenario == "single_material" else {"material_params": list(params)}
    contract = {
        "version": 2, "operate_in_place": operation,
        "resource_params": [{"param": name, "role": "material"} for name in params],
    }
    graph = {
        "nodes": [
            {
                "uuid": node_uuid,
                "workflow_node_template_uuid": template_uuid,
                "type": "ILab",
                "action_name": "inspect",
                "action_type": "UniLabJsonCommandAsync",
                "material_uuid": device,
                "param": params,
                "meta_data": {"unilab": {"executor_binding": {"mode": "fixed", "device_id": device}}},
            }
        ],
        "node_templates": [
            {
                "uuid": template_uuid,
                "node_type": "ILab",
                "type": "UniLabJsonCommandAsync",
                "schema": {"type": "object"},
                "meta_data": {
                    "unilab": {
                        "action_contract_schema": {
                            "type": "object",
                            "properties": {
                                "goal": {
                                    "type": "object",
                                    "properties": {
                                        name: {
                                            "type": "object",
                                            "properties": {"uuid": {"type": "string"}},
                                            "required": ["uuid"],
                                        }
                                        for name in params
                                    },
                                }
                            },
                            "x-unilabos-action-contract": {"version": 2, "resource_contract": contract},
                        }
                    }
                },
            }
        ],
        "edges": [],
    }
    plan, jobs = ExecutionPlanBuilder().build(graph, run_mode="normal", target_node_uuid=None)
    assert plan["inventory_resource_binding"] == "pending"
    node = plan["nodes"][0]
    if scenario in {"missing_site", "other_device", "duplicate_material"}:
        with pytest.raises((StationResourceError, ExecutionPlanBuildError), match="库位|互不相同"):
            bind_inventory_resource_plan({"execution_plan": plan, "workflow_snapshot": graph}, jobs, inventory)
        return
    bound = bind_inventory_resource_plan({"execution_plan": plan, "workflow_snapshot": graph}, jobs, inventory)
    assert bind_inventory_resource_plan({"execution_plan": bound}, [], None) == bound
    task = {"uuid": task_uuid, "execution_plan": bound}
    job = {"uuid": job_uuid, "workflow_node_uuid": node["uuid"], "attempt": 1}
    conditions = tuple(
        OperateInPlaceCondition(
            **asdict(
                inventory.resolve_operate_in_place(
                    material_uuid=material,
                    device_material_uuid=device,
                )
            )
        )
        for material in materials
    )
    resources = [DispatchResource(lock_key=device_lock_key(device), scope="device", material_uuid=device)]
    for fact in conditions:
        resources.extend(
            (
                DispatchResource(
                    lock_key=material_lock_key(fact.material_uuid), scope="material", material_uuid=fact.material_uuid
                ),
                DispatchResource(
                    lock_key=site_lock_key(device, fact.site_uuid),
                    scope="material_site",
                    material_uuid=device,
                    site_uuid=fact.site_uuid,
                ),
            )
        )
    assert set(bound["nodes"][0]["inventory_resource_lock_keys"]) == {r.lock_key for r in resources}
    if scenario == "missing_site_lock":
        resources.pop()
    if scenario == "moved_after_planning":
        with store.transaction() as connection:
            connection.execute("UPDATE site SET occupied_material_uuid=NULL WHERE uuid=?", (TARGET_SITE,))
    parameter_hash = hashlib.sha256(json.dumps(params, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    request = DispatchAdmissionRequest(
        effect_uuid=str(uuid4()),
        task_uuid=task_uuid,
        job_uuid=job_uuid,
        attempt=1,
        parameter_hash="sha256:" + parameter_hash,
        expected_change_set={"kind": "no_inventory_change"},
        resources=tuple(resources),
        operate_in_place=conditions,
    )
    if scenario == "missing_site_lock":
        with pytest.raises(DispatchAdmissionConflict, match="缺少完整资源"):
            inventory.acquire_dispatch_permit(request)
        return
    if scenario == "moved_after_planning":
        with pytest.raises(StationResourceError) as error:
            inventory.acquire_dispatch_permit(request)
        assert error.value.code == "operate_in_place_site_changed"
        return
    permit = inventory.acquire_dispatch_permit(request).permit
    assert permit is not None
    facts = list(_operate_condition_mappings([asdict(c) for c in conditions]))
    proof = {**permit.as_mapping(), "fencing_tokens": {f.lock_key: f.fencing_token for f in permit.fences}}
    if scenario == "partial_proof":
        facts.pop()
    if scenario == "changed_fence":
        proof["fencing_tokens"][material_lock_key(peer)] += 1
    requirements = [{"material_uuid": material, "device_lock_key": device_lock_key(device)} for material in materials]
    args = dict(
        task=task,
        job=job,
        requirement=requirements[0] if scenario == "single_material" else requirements,
        condition=facts[0] if scenario == "single_material" else facts,
        resolved_param=params,
        permit=proof,
        authority=inventory,
        plan_id=bound["resource_plan"]["plan_id"],
        interval_ids=bound["nodes"][0]["resource_interval_ids"],
    )
    if scenario in {"missing_custody", "partial_proof", "changed_fence"}:
        with pytest.raises((DispatchAdmissionConflict, StoreConflict), match="保管|完整物料集合|Fence"):
            validate_native_operate_custody(**args)
    else:
        assert validate_native_operate_custody(**args) is True
