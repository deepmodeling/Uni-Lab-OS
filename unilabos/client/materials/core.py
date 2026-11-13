"""materials.v1 的 Local/HTTP 等价客户端。"""

from __future__ import annotations

import json
from typing import Any, Optional
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from unilabos.client.utils.envelope import APIHTTPError, EnvelopeError, request_json
from unilabos.config.control_token import client_headers

from unilabos.protocol.materials import InventoryChange, InventoryMutation, MutationResult
from unilabos.protocol.materials import (
    InventoryLotInbound,
    InventoryLotRead,
    InventoryReservationCreate,
    InventoryReservationRead,
    InventoryReservationTransition,
    InventoryTaskReservationCreate,
    InventoryTaskReservationRead,
    MaterialAggregateRead,
    MaterialDataWrite,
    MaterialDelete,
    MaterialDeleteResult,
    MaterialDelta,
    MaterialDeltaResult,
    MaterialMove,
    MaterialPatch,
    MaterialPosition,
    MaterialSnapshot,
    MaterialSnapshotDiff,
    MaterialTreeCreate,
    MaterialTreeRead,
    MaterialTransfer,
    MaterialTransferResult,
    ResourceTemplateRead,
    ResourceTemplateWrite,
)
from unilabos.server.services.materials import MaterialsService


def bind_payload(mutation: InventoryMutation, value: Any) -> InventoryMutation:
    payload = (
        value.model_dump(mode="json", exclude_none=False)
        if hasattr(value, "model_dump")
        else dict(value)
    )
    if mutation.payload and mutation.payload != payload:
        raise ValueError("mutation.payload differs from request value")
    return mutation.model_copy(update={"payload": payload})


class LocalMaterialsClient:
    """测试、HostLink 同进程模式使用；方法与 HTTP client 完全一致。"""

    def __init__(self, service: MaterialsService):
        self.service = service

    def put_template(self, mutation, value):
        return self.service.put_template(bind_payload(mutation, value), value)

    def create_template(self, mutation, value):
        if value.template_uuid is not None:
            raise ValueError("create_template requires template_uuid=None")
        return self.service.put_template(bind_payload(mutation, value), value)

    def get_template(self, template_uuid: str):
        return self.service.get_template(template_uuid)

    def list_templates(
        self, *, name: Optional[str] = None, include_definition: bool = False
    ):
        return self.service.list_templates(
            name=name, include_definition=include_definition
        )

    def delete_template(self, mutation, template_uuid: str):
        value = {"template_uuid": template_uuid}
        return self.service.delete_template(bind_payload(mutation, value), template_uuid)

    def inbound_inventory_lot(self, mutation, value):
        return self.service.inbound_inventory_lot(bind_payload(mutation, value), value)

    def get_inventory_lot(self, lot_uuid: str):
        return self.service.get_inventory_lot(lot_uuid)

    def list_inventory_lots(
        self,
        *,
        template_uuid: Optional[str] = None,
        unit: Optional[str] = None,
        include_quarantined: bool = False,
    ):
        return self.service.list_inventory_lots(
            template_uuid=template_uuid,
            unit=unit,
            include_quarantined=include_quarantined,
        )

    def stocktake_inventory_lot(self, mutation, value):
        return self.service.stocktake_inventory_lot(bind_payload(mutation, value), value)

    def correct_inventory_lot(self, mutation, value):
        return self.service.correct_inventory_lot(bind_payload(mutation, value), value)

    def prepare_material_execution(self, value):
        return self.service.executions.prepare(value)

    def validate_material_execution(self, value):
        return self.service.executions.validate(value)

    def settle_material_execution(self, value):
        return self.service.executions.settle(value)

    def list_material_executions(self):
        return self.service.executions.list()

    def get_material_execution(self, job_uuid):
        return self.service.executions.get(job_uuid)

    def release_material_execution(self, job_uuid, value):
        return self.service.executions.release(job_uuid, value)

    def preview_inventory(self, requirements):
        return self.service.preview_inventory(requirements)

    def reserve_inventory(self, mutation, value):
        return self.service.reserve_inventory(bind_payload(mutation, value), value)

    def reserve_task_inventory(self, mutation, value):
        return self.service.reserve_task_inventory(
            bind_payload(mutation, value), value
        )

    def get_inventory_reservation(self, reservation_uuid: str):
        return self.service.get_inventory_reservation(reservation_uuid)

    def get_inventory_reservation_by_job(self, job_uuid: str):
        return self.service.get_inventory_reservation_by_job(job_uuid)

    def list_inventory_reservations(
        self,
        *,
        task_uuid: Optional[str] = None,
        status: Optional[str] = None,
    ):
        return self.service.list_inventory_reservations(
            task_uuid=task_uuid,
            status=status,
        )

    def consume_inventory_reservation(self, mutation, value):
        return self.service.consume_inventory_reservation(
            bind_payload(mutation, value), value
        )

    def release_inventory_reservation(self, mutation, value):
        return self.service.release_inventory_reservation(
            bind_payload(mutation, value), value
        )

    def quarantine_inventory_reservation(self, mutation, value):
        return self.service.quarantine_inventory_reservation(
            bind_payload(mutation, value), value
        )

    def create_tree(self, mutation, value):
        return self.service.create_tree(bind_payload(mutation, value), value)

    def get_material(self, material_uuid: str):
        return self.service.get_material(material_uuid)

    def get_material_by_resource_id(self, resource_id: str):
        return self.service.get_material_by_resource_id(resource_id)

    def search_materials(self, name: str):
        return self.service.search_materials(name)

    def list_materials(self, *, roots_only: bool = False):
        return self.service.list_materials(roots_only=roots_only)

    def get_tree(self, root_material_uuid: str):
        return self.service.get_tree(root_material_uuid)

    def patch_material(self, mutation, material_uuid: str, value):
        return self.service.patch_material(
            bind_payload(mutation, value), material_uuid, value
        )

    def put_position(self, mutation, material_uuid: str, value):
        return self.service.put_position(
            bind_payload(mutation, value), material_uuid, value
        )

    def put_data(self, mutation, material_uuid: str, value):
        return self.service.put_data(bind_payload(mutation, value), material_uuid, value)

    def move_material(self, mutation, value):
        return self.service.move_material(bind_payload(mutation, value), value)

    def transfer_material(self, mutation, value):
        return self.service.transfer_material(bind_payload(mutation, value), value)

    def delete_material(self, mutation, value):
        return self.service.delete_material(bind_payload(mutation, value), value)

    def compare_snapshot(self, value):
        return self.service.compare_snapshot(value)

    def apply_snapshot(self, mutation, value):
        return self.service.apply_snapshot(bind_payload(mutation, value), value)

    def apply_delta(self, mutation, value):
        return self.service.apply_delta(bind_payload(mutation, value), value)

    def changes(self, *, after_sequence: int = 0, limit: int = 100):
        return self.service.changes(after_sequence=after_sequence, limit=limit)

    def acknowledge_changes(self, through_sequence: int) -> int:
        return self.service.acknowledge_changes(through_sequence)


class HostLinkMaterialsClient:
    """Slave 侧 materials client；Host 代发到实际物料权威。"""

    def __init__(self, client: Any):
        self.client = client

    def list_templates(
        self, *, name: Optional[str] = None, include_definition: bool = False
    ) -> list[ResourceTemplateRead]:
        """筛选由 Host 转给权威在服务端完成。默认目录模式（存在性检查 / 按名取 uuid /
        比 definition_hash 都够用）；只有确实要用 definition 正文的调用方才传
        ``include_definition=True``，把全注册表的 definition 拖过链路。"""

        from unilabos.backend.hostlink.protocol import ActionType

        query: dict[str, Any] = {"include_definition": include_definition}
        if name is not None:
            query["name"] = name
        response = self.client.request(ActionType.MATERIAL_TEMPLATE_LIST, query)
        return [ResourceTemplateRead.model_validate(item) for item in response]

    def create_template(
        self,
        mutation: InventoryMutation,
        value: ResourceTemplateWrite,
    ) -> MutationResult[ResourceTemplateRead]:
        from unilabos.backend.hostlink.protocol import ActionType

        bound = bind_payload(mutation, value)
        response = self.client.request(
            ActionType.MATERIAL_TEMPLATE_CREATE,
            bound.model_dump(mode="json", exclude_none=False),
        )
        return MutationResult[ResourceTemplateRead].model_validate(response)

    def create_tree(
        self, mutation: InventoryMutation, value: MaterialTreeCreate
    ) -> MutationResult[MaterialTreeRead]:
        from unilabos.backend.hostlink.protocol import ActionType

        bound = bind_payload(mutation, value)
        response = self.client.request(
            ActionType.MATERIAL_CREATE,
            bound.model_dump(mode="json", exclude_none=False),
        )
        return MutationResult[MaterialTreeRead].model_validate(response)

    def get_tree(self, root_material_uuid: str) -> MaterialTreeRead:
        from unilabos.backend.hostlink.protocol import ActionType

        response = self.client.request(
            ActionType.MATERIAL_GET_TREE,
            {"root_material_uuid": root_material_uuid},
        )
        return MaterialTreeRead.model_validate(response)

    def get_material(self, material_uuid: str) -> MaterialAggregateRead:
        tree = self.get_tree(material_uuid)
        if not tree.nodes:
            raise ValueError("Host 返回了空物料树")
        return tree.nodes[0]

    def get_material_by_resource_id(self, resource_id: str) -> MaterialAggregateRead:
        from unilabos.backend.hostlink.protocol import ActionType

        response = self.client.request(
            ActionType.MATERIAL_GET_BY_RESOURCE_ID,
            {"resource_id": resource_id},
        )
        return MaterialAggregateRead.model_validate(response)

    def search_materials(self, name: str) -> list[MaterialAggregateRead]:
        from unilabos.backend.hostlink.protocol import ActionType

        response = self.client.request(
            ActionType.MATERIAL_SEARCH,
            {"name": name},
        )
        return [MaterialAggregateRead.model_validate(item) for item in response]

    def put_data(
        self,
        mutation: InventoryMutation,
        material_uuid: str,
        value: MaterialDataWrite,
    ) -> MutationResult[MaterialAggregateRead]:
        from unilabos.backend.hostlink.protocol import ActionType

        bound = bind_payload(mutation, value).model_dump(
            mode="json", exclude_none=False
        )
        bound["material_uuid"] = material_uuid
        response = self.client.request(ActionType.MATERIAL_DATA_PUT, bound)
        return MutationResult[MaterialAggregateRead].model_validate(response)

    def move_material(
        self,
        mutation: InventoryMutation,
        value: MaterialMove,
    ) -> MutationResult[MaterialAggregateRead]:
        from unilabos.backend.hostlink.protocol import ActionType

        bound = bind_payload(mutation, value)
        response = self.client.request(
            ActionType.MATERIAL_MOVE,
            bound.model_dump(mode="json", exclude_none=False),
        )
        return MutationResult[MaterialAggregateRead].model_validate(response)

    def transfer_material(
        self,
        mutation: InventoryMutation,
        value: MaterialTransfer,
    ) -> MutationResult[MaterialTransferResult]:
        from unilabos.backend.hostlink.protocol import ActionType

        bound = bind_payload(mutation, value)
        response = self.client.request(
            ActionType.MATERIAL_TRANSFER,
            bound.model_dump(mode="json", exclude_none=False),
        )
        return MutationResult[MaterialTransferResult].model_validate(response)

    def delete_material(
        self,
        mutation: InventoryMutation,
        value: MaterialDelete,
    ) -> MutationResult[MaterialDeleteResult]:
        from unilabos.backend.hostlink.protocol import ActionType

        bound = bind_payload(mutation, value)
        response = self.client.request(
            ActionType.MATERIAL_DELETE,
            bound.model_dump(mode="json", exclude_none=False),
        )
        return MutationResult[MaterialDeleteResult].model_validate(response)

    def compare_snapshot(self, value: MaterialSnapshot) -> MaterialSnapshotDiff:
        from unilabos.backend.hostlink.protocol import ActionType

        response = self.client.request(
            ActionType.MATERIAL_COMPARE_SNAPSHOT,
            value.model_dump(mode="json", exclude_none=False),
        )
        return MaterialSnapshotDiff.model_validate(response)

    def apply_snapshot(
        self, mutation: InventoryMutation, value: MaterialSnapshot
    ) -> MutationResult[MaterialTreeRead]:
        from unilabos.backend.hostlink.protocol import ActionType

        bound = bind_payload(mutation, value)
        response = self.client.request(
            ActionType.MATERIAL_APPLY_SNAPSHOT,
            bound.model_dump(mode="json", exclude_none=False),
        )
        return MutationResult[MaterialTreeRead].model_validate(response)

    def apply_delta(
        self, mutation: InventoryMutation, value: MaterialDelta
    ) -> MutationResult[MaterialDeltaResult]:
        from unilabos.backend.hostlink.protocol import ActionType

        bound = bind_payload(mutation, value)
        response = self.client.request(
            ActionType.MATERIAL_APPLY_DELTA,
            bound.model_dump(mode="json", exclude_none=False),
        )
        return MutationResult[MaterialDeltaResult].model_validate(response)


class MaterialsHTTPError(APIHTTPError):
    """物料 API 失败；``status_code`` 按业务码换算（4001 / 6000 / 6006 … → 404）。"""

    api_name = "materials"

    def __init__(self, status_code: int, detail: str, **kwargs: Any):
        super().__init__(status_code, detail, **kwargs)
        # 与本地物料异常保持一致，HostLink 可识别 ensure 的正常不存在结果。
        self.code = "not_found" if status_code == 404 else "materials_http_error"


#: ``/inventory/*`` 读路由附带的 V1 标识别名；还原 dev DTO 前去掉。
_INSTANCE_ALIASES = ("edge_uuid", "template_id", "barcode", "status", "relation", "content")
_TEMPLATE_ALIASES = ("template_id",)
_LOT_ALIASES = ("lot_id", "template_id")
_RESERVATION_ALIASES = ("reservation_id", "workflow_id", "node_id")


def _strip(item: Any, aliases: tuple[str, ...]) -> Any:
    if not isinstance(item, dict):
        return item
    return {key: value for key, value in item.items() if key not in aliases}


class HTTPMaterialsClient:
    """经物料权威 HTTP API 的网关（与 Local / HostLink 客户端方法一致）。

    写入全部走 V1 统一命令入口 ``POST /api/v1/inventory/commands``（``materials.v1`` 变更，
    按 command_uuid + effect_key 幂等）；读走 V1 Edge 库存面 ``/api/v1/inventory/*``；
    V1 没有对应能力的少数 dev 路由仍在 ``/api/v1/materials/*``。
    """

    def __init__(self, base_url: str, *, timeout: float = 30.0):
        base = base_url.rstrip("/")
        for suffix in ("/api/v1/materials", "/api/v1"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        self.api_root = base + "/api/v1"
        self.base_url = self.api_root + "/materials"
        self.timeout = timeout

    def _request(
        self, method: str, path: str, body: Optional[Any] = None, *, root: bool = False, envelope: bool = True,
    ) -> Any:
        data = None
        headers = {"Accept": "application/json", **client_headers()}
        if body is not None:
            if hasattr(body, "model_dump"):
                body = body.model_dump(mode="json", exclude_none=False)
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            (self.api_root if root else self.base_url) + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            return request_json(request, timeout=self.timeout, opener=urlopen, envelope=envelope)
        except EnvelopeError as exc:
            raise MaterialsHTTPError.from_envelope_error(exc) from exc

    def _get(self, path: str) -> Any:
        return self._request("GET", path, root=True)

    def inventory_command(self, command: dict[str, Any]) -> dict[str, Any]:
        """V1 类型化库存命令（``{command_id, type, payload, expected_version?, actor?}``），原样返回裸结果。

        ``status`` 为 ``completed`` / ``rejected``；同一 ``command_id`` 重放返回首次结果（``replayed``）。
        业务拒绝不抛异常，由调用方按 ``status`` 判断。
        """

        response = self._request("POST", "/inventory/commands", command, root=True, envelope=False)
        if not isinstance(response, dict) or response.get("status") not in {"completed", "rejected"}:
            raise MaterialsHTTPError(502, "库存命令返回了无法解析的响应")
        return response

    def _command(self, mutation: InventoryMutation, value: Any, *, target_uuid: Optional[str] = None) -> Any:
        """``materials.v1`` 变更经统一命令入口；拒绝结果按业务码还原成 HTTP 语义的异常。"""

        body = bind_payload(mutation, value).model_dump(mode="json", exclude_none=False)
        if target_uuid is not None:
            body["target_uuid"] = target_uuid
        response = self._request("POST", "/inventory/commands", body, root=True, envelope=False)
        if not isinstance(response, dict) or response.get("status") not in {"completed", "rejected"}:
            raise MaterialsHTTPError(502, "库存命令返回了无法解析的响应")
        if response["status"] == "rejected":
            raise MaterialsHTTPError.from_envelope_error(
                EnvelopeError(int(response.get("code") or 1), str(response.get("error") or "command rejected"),
                              symbol=response.get("error_code"))
            )
        return response["result"]

    # -- 模板 ------------------------------------------------------

    def put_template(
        self, mutation: InventoryMutation, value: ResourceTemplateWrite
    ) -> MutationResult[ResourceTemplateRead]:
        return MutationResult[ResourceTemplateRead].model_validate(self._command(mutation, value))

    def create_template(
        self, mutation: InventoryMutation, value: ResourceTemplateWrite
    ) -> MutationResult[ResourceTemplateRead]:
        if value.template_uuid is not None:
            raise ValueError("create_template requires template_uuid=None")
        return MutationResult[ResourceTemplateRead].model_validate(self._command(mutation, value))

    def get_template(self, template_uuid: str) -> ResourceTemplateRead:
        return ResourceTemplateRead.model_validate(
            _strip(self._get("/inventory/templates/" + quote(template_uuid, safe="")), _TEMPLATE_ALIASES)
        )

    def list_templates(
        self, *, name: Optional[str] = None, include_definition: bool = False
    ) -> list[ResourceTemplateRead]:
        query: dict[str, str] = {"all": "true"}
        if name is not None:
            query["name"] = name
        if include_definition:
            query["include_definition"] = "true"
        return [
            ResourceTemplateRead.model_validate(_strip(item, _TEMPLATE_ALIASES))
            for item in self._get(f"/inventory/templates?{urlencode(query)}")["templates"]
        ]

    def delete_template(self, mutation, template_uuid: str):
        return MutationResult[ResourceTemplateRead].model_validate(
            self._command(mutation, {"template_uuid": template_uuid})
        )

    # -- 批次 ------------------------------------------------------

    def inbound_inventory_lot(
        self, mutation: InventoryMutation, value: InventoryLotInbound
    ) -> MutationResult[InventoryLotRead]:
        return MutationResult[InventoryLotRead].model_validate(self._command(mutation, value))

    def get_inventory_lot(self, lot_uuid: str) -> InventoryLotRead:
        return InventoryLotRead.model_validate(
            _strip(self._get("/inventory/lots/" + quote(lot_uuid, safe="")), _LOT_ALIASES)
        )

    def list_inventory_lots(
        self,
        *,
        template_uuid: Optional[str] = None,
        unit: Optional[str] = None,
        include_quarantined: bool = False,
    ) -> list[InventoryLotRead]:
        query = urlencode(
            {
                key: value
                for key, value in {
                    "template_uuid": template_uuid,
                    "unit": unit,
                    "include_quarantined": str(include_quarantined).lower(),
                }.items()
                if value is not None
            }
        )
        return [
            InventoryLotRead.model_validate(_strip(item, _LOT_ALIASES))
            for item in self._get(f"/inventory/lots?{query}")["lots"]
        ]

    def stocktake_inventory_lot(self, mutation, value):
        return MutationResult[InventoryLotRead].model_validate(self._command(mutation, value))

    def correct_inventory_lot(self, mutation, value):
        return MutationResult[InventoryLotRead].model_validate(self._command(mutation, value))

    # -- 物理动作执行（dev 独有） -------------------------------------

    def prepare_material_execution(self, value):
        return self._request("POST", "/executions/prepare", value.model_dump(mode="json"))

    def validate_material_execution(self, value):
        return self._request("POST", "/executions/validate", value.model_dump(mode="json"))

    def settle_material_execution(self, value):
        return self._request("POST", "/executions/settle", value.model_dump(mode="json"))

    def list_material_executions(self):
        return self._request("GET", "/executions")

    def get_material_execution(self, job_uuid):
        try:
            return self._request("GET", "/executions/" + quote(job_uuid, safe=""))
        except Exception as error:
            if getattr(error, "code", None) == "not_found":
                return None
            raise

    def release_material_execution(self, job_uuid, value):
        return self._request("POST", "/executions/" + quote(job_uuid, safe="") + "/release", value.model_dump(mode="json"))

    def preview_inventory(self, requirements):
        return self._request("POST", "/inventory/preview", {
            "requirements": [item.model_dump(mode="json") for item in requirements],
        })

    # -- 预留 ------------------------------------------------------

    def reserve_inventory(
        self,
        mutation: InventoryMutation,
        value: InventoryReservationCreate,
    ) -> MutationResult[InventoryReservationRead]:
        return MutationResult[InventoryReservationRead].model_validate(self._command(mutation, value))

    def reserve_task_inventory(
        self,
        mutation: InventoryMutation,
        value: InventoryTaskReservationCreate,
    ) -> MutationResult[InventoryTaskReservationRead]:
        return MutationResult[InventoryTaskReservationRead].model_validate(self._command(mutation, value))

    def get_inventory_reservation(
        self, reservation_uuid: str
    ) -> InventoryReservationRead:
        return InventoryReservationRead.model_validate(
            _strip(self._get("/inventory/reservations/" + quote(reservation_uuid, safe="")), _RESERVATION_ALIASES)
        )

    def get_inventory_reservation_by_job(
        self, job_uuid: str
    ) -> InventoryReservationRead:
        return InventoryReservationRead.model_validate(
            self._request("GET", f"/reservations/by-job/{job_uuid}")
        )

    def list_inventory_reservations(
        self,
        *,
        task_uuid: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[InventoryReservationRead]:
        query = urlencode(
            {
                key: value
                for key, value in {"task_uuid": task_uuid, "status": status}.items()
                if value is not None
            }
        )
        return [
            InventoryReservationRead.model_validate(_strip(item, _RESERVATION_ALIASES))
            for item in self._get(f"/inventory/reservations?{query}")["reservations"]
        ]

    def consume_inventory_reservation(self, mutation, value):
        return MutationResult[InventoryReservationRead].model_validate(self._command(mutation, value))

    def release_inventory_reservation(self, mutation, value):
        return MutationResult[InventoryReservationRead].model_validate(self._command(mutation, value))

    def quarantine_inventory_reservation(self, mutation, value):
        return MutationResult[InventoryReservationRead].model_validate(self._command(mutation, value))

    # -- 物料聚合 --------------------------------------------------

    def create_tree(
        self, mutation: InventoryMutation, value: MaterialTreeCreate
    ) -> MutationResult[MaterialTreeRead]:
        return MutationResult[MaterialTreeRead].model_validate(self._command(mutation, value))

    def get_material(self, material_uuid: str) -> MaterialAggregateRead:
        return MaterialAggregateRead.model_validate(
            _strip(self._get("/inventory/instances/" + quote(material_uuid, safe="")), _INSTANCE_ALIASES)
        )

    def get_material_by_resource_id(self, resource_id: str) -> MaterialAggregateRead:
        return MaterialAggregateRead.model_validate(
            self._request("GET", "/instances/by-resource-id/" + quote(resource_id, safe=""))
        )

    def search_materials(self, name: str) -> list[MaterialAggregateRead]:
        query = urlencode({"name": name, "all": "true"})
        return [
            MaterialAggregateRead.model_validate(_strip(item, _INSTANCE_ALIASES))
            for item in self._get(f"/inventory/instances?{query}")["instances"]
        ]

    def list_materials(self, *, roots_only: bool = False) -> list[MaterialAggregateRead]:
        query = urlencode({"roots_only": str(roots_only).lower(), "all": "true"})
        return [
            MaterialAggregateRead.model_validate(_strip(item, _INSTANCE_ALIASES))
            for item in self._get(f"/inventory/instances?{query}")["instances"]
        ]

    def get_tree(self, root_material_uuid: str) -> MaterialTreeRead:
        return MaterialTreeRead.model_validate(
            self._request("GET", f"/instances/{root_material_uuid}/tree")
        )

    def patch_material(self, mutation, material_uuid: str, value: MaterialPatch):
        return MutationResult[MaterialAggregateRead].model_validate(
            self._command(mutation, value, target_uuid=material_uuid)
        )

    def put_position(self, mutation, material_uuid: str, value: MaterialPosition):
        return MutationResult[MaterialAggregateRead].model_validate(
            self._command(mutation, value, target_uuid=material_uuid)
        )

    def put_data(self, mutation, material_uuid: str, value: MaterialDataWrite):
        return MutationResult[MaterialAggregateRead].model_validate(
            self._command(mutation, value, target_uuid=material_uuid)
        )

    def move_material(self, mutation, value: MaterialMove):
        return MutationResult[MaterialAggregateRead].model_validate(self._command(mutation, value))

    def transfer_material(self, mutation, value: MaterialTransfer):
        return MutationResult[MaterialTransferResult].model_validate(self._command(mutation, value))

    def delete_material(self, mutation, value: MaterialDelete):
        return MutationResult[MaterialDeleteResult].model_validate(self._command(mutation, value))

    def compare_snapshot(self, value: MaterialSnapshot) -> MaterialSnapshotDiff:
        return MaterialSnapshotDiff.model_validate(
            self._request("POST", "/snapshots/compare", value)
        )

    def apply_snapshot(self, mutation, value: MaterialSnapshot):
        return MutationResult[MaterialTreeRead].model_validate(self._command(mutation, value))

    def apply_delta(self, mutation, value: MaterialDelta):
        return MutationResult[MaterialDeltaResult].model_validate(self._command(mutation, value))

    # -- 账本 ------------------------------------------------------

    def changes(
        self, *, after_sequence: int = 0, limit: int = 100
    ) -> list[InventoryChange]:
        query = urlencode({"after_id": after_sequence, "limit": limit})
        return [
            InventoryChange.model_validate(item)
            for item in self._get(f"/inventory/ledger?{query}")["entries"]
        ]

    def acknowledge_changes(self, through_sequence: int) -> int:
        response = self._request(
            "POST", "/changes/ack", {"through_sequence": through_sequence}
        )
        return int(response["acknowledged"])


__all__ = [
    "HTTPMaterialsClient",
    "HostLinkMaterialsClient",
    "LocalMaterialsClient",
    "MaterialsHTTPError",
    "bind_payload",
]
