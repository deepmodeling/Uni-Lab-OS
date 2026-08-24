"""materials.v1 的 Local/HTTP 等价客户端。"""

from __future__ import annotations

import json
from typing import Any, Optional
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from unilabos.server.protocol.common import (
    InventoryChange,
    InventoryMutation,
    MutationResult,
)
from unilabos.server.protocol.materials import (
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

    def list_templates(self):
        return self.service.list_templates()

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

    def changes(self, *, after_sequence: int = 0, limit: int = 100):
        return self.service.changes(after_sequence=after_sequence, limit=limit)

    def acknowledge_changes(self, through_sequence: int) -> int:
        return self.service.acknowledge_changes(through_sequence)


class HostLinkMaterialsClient:
    """Slave 侧 materials client；Host 代发到实际物料权威。"""

    def __init__(self, client: Any):
        self.client = client

    def list_templates(self) -> list[ResourceTemplateRead]:
        from unilabos.hostlink.protocol import ActionType

        response = self.client.request(ActionType.MATERIAL_TEMPLATE_LIST, {})
        return [ResourceTemplateRead.model_validate(item) for item in response]

    def create_template(
        self,
        mutation: InventoryMutation,
        value: ResourceTemplateWrite,
    ) -> MutationResult[ResourceTemplateRead]:
        from unilabos.hostlink.protocol import ActionType

        bound = bind_payload(mutation, value)
        response = self.client.request(
            ActionType.MATERIAL_TEMPLATE_CREATE,
            bound.model_dump(mode="json", exclude_none=False),
        )
        return MutationResult[ResourceTemplateRead].model_validate(response)

    def create_tree(
        self, mutation: InventoryMutation, value: MaterialTreeCreate
    ) -> MutationResult[MaterialTreeRead]:
        from unilabos.hostlink.protocol import ActionType

        bound = bind_payload(mutation, value)
        response = self.client.request(
            ActionType.MATERIAL_CREATE,
            bound.model_dump(mode="json", exclude_none=False),
        )
        return MutationResult[MaterialTreeRead].model_validate(response)

    def get_tree(self, root_material_uuid: str) -> MaterialTreeRead:
        from unilabos.hostlink.protocol import ActionType

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
        from unilabos.hostlink.protocol import ActionType

        response = self.client.request(
            ActionType.MATERIAL_GET_BY_RESOURCE_ID,
            {"resource_id": resource_id},
        )
        return MaterialAggregateRead.model_validate(response)

    def put_data(
        self,
        mutation: InventoryMutation,
        material_uuid: str,
        value: MaterialDataWrite,
    ) -> MutationResult[MaterialAggregateRead]:
        from unilabos.hostlink.protocol import ActionType

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
        from unilabos.hostlink.protocol import ActionType

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
        from unilabos.hostlink.protocol import ActionType

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
        from unilabos.hostlink.protocol import ActionType

        bound = bind_payload(mutation, value)
        response = self.client.request(
            ActionType.MATERIAL_DELETE,
            bound.model_dump(mode="json", exclude_none=False),
        )
        return MutationResult[MaterialDeleteResult].model_validate(response)

    def compare_snapshot(self, value: MaterialSnapshot) -> MaterialSnapshotDiff:
        from unilabos.hostlink.protocol import ActionType

        response = self.client.request(
            ActionType.MATERIAL_COMPARE_SNAPSHOT,
            value.model_dump(mode="json", exclude_none=False),
        )
        return MaterialSnapshotDiff.model_validate(response)

    def apply_snapshot(
        self, mutation: InventoryMutation, value: MaterialSnapshot
    ) -> MutationResult[MaterialTreeRead]:
        from unilabos.hostlink.protocol import ActionType

        bound = bind_payload(mutation, value)
        response = self.client.request(
            ActionType.MATERIAL_APPLY_SNAPSHOT,
            bound.model_dump(mode="json", exclude_none=False),
        )
        return MutationResult[MaterialTreeRead].model_validate(response)


class MaterialsHTTPError(RuntimeError):
    def __init__(self, status_code: int, detail: str):
        super().__init__(f"materials API returned {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class HTTPMaterialsClient:
    def __init__(self, base_url: str, *, timeout: float = 30.0):
        base = base_url.rstrip("/")
        if base.endswith("/api/v1/materials"):
            self.base_url = base
        elif base.endswith("/api/v1"):
            self.base_url = base + "/materials"
        else:
            self.base_url = base + "/api/v1/materials"
        self.timeout = timeout

    def _request(
        self, method: str, path: str, body: Optional[Any] = None
    ) -> Any:
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            if hasattr(body, "model_dump"):
                body = body.model_dump(mode="json", exclude_none=False)
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(
            self.base_url + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raw = exc.read().decode("utf-8")
            try:
                detail = json.loads(raw).get("detail", raw)
            except ValueError:
                detail = raw
            raise MaterialsHTTPError(exc.code, str(detail)) from exc

    def put_template(
        self, mutation: InventoryMutation, value: ResourceTemplateWrite
    ) -> MutationResult[ResourceTemplateRead]:
        response = self._request(
            "PUT",
            f"/templates/{value.template_uuid}",
            bind_payload(mutation, value),
        )
        return MutationResult[ResourceTemplateRead].model_validate(response)

    def create_template(
        self, mutation: InventoryMutation, value: ResourceTemplateWrite
    ) -> MutationResult[ResourceTemplateRead]:
        if value.template_uuid is not None:
            raise ValueError("create_template requires template_uuid=None")
        response = self._request(
            "POST", "/templates", bind_payload(mutation, value)
        )
        return MutationResult[ResourceTemplateRead].model_validate(response)

    def get_template(self, template_uuid: str) -> ResourceTemplateRead:
        return ResourceTemplateRead.model_validate(
            self._request("GET", f"/templates/{template_uuid}")
        )

    def list_templates(self) -> list[ResourceTemplateRead]:
        return [
            ResourceTemplateRead.model_validate(item)
            for item in self._request("GET", "/templates")
        ]

    def delete_template(self, mutation, template_uuid: str):
        bound = bind_payload(mutation, {"template_uuid": template_uuid})
        return MutationResult[ResourceTemplateRead].model_validate(
            self._request("DELETE", f"/templates/{template_uuid}", bound)
        )

    def inbound_inventory_lot(
        self, mutation: InventoryMutation, value: InventoryLotInbound
    ) -> MutationResult[InventoryLotRead]:
        return MutationResult[InventoryLotRead].model_validate(
            self._request("POST", "/lots/inbound", bind_payload(mutation, value))
        )

    def get_inventory_lot(self, lot_uuid: str) -> InventoryLotRead:
        return InventoryLotRead.model_validate(
            self._request("GET", f"/lots/{lot_uuid}")
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
            InventoryLotRead.model_validate(item)
            for item in self._request("GET", f"/lots?{query}")
        ]

    def reserve_inventory(
        self,
        mutation: InventoryMutation,
        value: InventoryReservationCreate,
    ) -> MutationResult[InventoryReservationRead]:
        return MutationResult[InventoryReservationRead].model_validate(
            self._request("POST", "/reservations", bind_payload(mutation, value))
        )

    def reserve_task_inventory(
        self,
        mutation: InventoryMutation,
        value: InventoryTaskReservationCreate,
    ) -> MutationResult[InventoryTaskReservationRead]:
        return MutationResult[InventoryTaskReservationRead].model_validate(
            self._request(
                "POST",
                "/reservations/batch",
                bind_payload(mutation, value),
            )
        )

    def get_inventory_reservation(
        self, reservation_uuid: str
    ) -> InventoryReservationRead:
        return InventoryReservationRead.model_validate(
            self._request("GET", f"/reservations/{reservation_uuid}")
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
            InventoryReservationRead.model_validate(item)
            for item in self._request("GET", f"/reservations?{query}")
        ]

    def _transition_inventory_reservation(
        self,
        mutation: InventoryMutation,
        value: InventoryReservationTransition,
        action: str,
    ) -> MutationResult[InventoryReservationRead]:
        return MutationResult[InventoryReservationRead].model_validate(
            self._request(
                "POST",
                f"/reservations/{value.reservation_uuid}/{action}",
                bind_payload(mutation, value),
            )
        )

    def consume_inventory_reservation(self, mutation, value):
        return self._transition_inventory_reservation(mutation, value, "consume")

    def release_inventory_reservation(self, mutation, value):
        return self._transition_inventory_reservation(mutation, value, "release")

    def quarantine_inventory_reservation(self, mutation, value):
        return self._transition_inventory_reservation(
            mutation, value, "quarantine"
        )

    def create_tree(
        self, mutation: InventoryMutation, value: MaterialTreeCreate
    ) -> MutationResult[MaterialTreeRead]:
        return MutationResult[MaterialTreeRead].model_validate(
            self._request("POST", "/trees", bind_payload(mutation, value))
        )

    def get_material(self, material_uuid: str) -> MaterialAggregateRead:
        return MaterialAggregateRead.model_validate(
            self._request("GET", f"/instances/{material_uuid}")
        )

    def get_material_by_resource_id(self, resource_id: str) -> MaterialAggregateRead:
        return MaterialAggregateRead.model_validate(
            self._request("GET", f"/instances/by-resource-id/{resource_id}")
        )

    def list_materials(self, *, roots_only: bool = False) -> list[MaterialAggregateRead]:
        query = urlencode({"roots_only": str(roots_only).lower()})
        return [
            MaterialAggregateRead.model_validate(item)
            for item in self._request("GET", f"/instances?{query}")
        ]

    def get_tree(self, root_material_uuid: str) -> MaterialTreeRead:
        return MaterialTreeRead.model_validate(
            self._request("GET", f"/instances/{root_material_uuid}/tree")
        )

    def patch_material(self, mutation, material_uuid: str, value: MaterialPatch):
        return MutationResult[MaterialAggregateRead].model_validate(
            self._request(
                "PATCH",
                f"/instances/{material_uuid}",
                bind_payload(mutation, value),
            )
        )

    def put_position(self, mutation, material_uuid: str, value: MaterialPosition):
        return MutationResult[MaterialAggregateRead].model_validate(
            self._request(
                "PUT",
                f"/instances/{material_uuid}/position",
                bind_payload(mutation, value),
            )
        )

    def put_data(self, mutation, material_uuid: str, value: MaterialDataWrite):
        return MutationResult[MaterialAggregateRead].model_validate(
            self._request(
                "PUT",
                f"/instances/{material_uuid}/data",
                bind_payload(mutation, value),
            )
        )

    def move_material(self, mutation, value: MaterialMove):
        return MutationResult[MaterialAggregateRead].model_validate(
            self._request("POST", "/move", bind_payload(mutation, value))
        )

    def transfer_material(self, mutation, value: MaterialTransfer):
        return MutationResult[MaterialTransferResult].model_validate(
            self._request("POST", "/transfer", bind_payload(mutation, value))
        )

    def delete_material(self, mutation, value: MaterialDelete):
        return MutationResult[MaterialDeleteResult].model_validate(
            self._request(
                "DELETE",
                f"/instances/{value.material_uuid}",
                bind_payload(mutation, value),
            )
        )

    def compare_snapshot(self, value: MaterialSnapshot) -> MaterialSnapshotDiff:
        return MaterialSnapshotDiff.model_validate(
            self._request("POST", "/snapshots/compare", value)
        )

    def apply_snapshot(self, mutation, value: MaterialSnapshot):
        return MutationResult[MaterialTreeRead].model_validate(
            self._request(
                "POST", "/snapshots/apply", bind_payload(mutation, value)
            )
        )

    def changes(
        self, *, after_sequence: int = 0, limit: int = 100
    ) -> list[InventoryChange]:
        query = urlencode({"after_sequence": after_sequence, "limit": limit})
        return [
            InventoryChange.model_validate(item)
            for item in self._request("GET", f"/changes?{query}")
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
