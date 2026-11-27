"""工站侧的中心客户端：会话、领取、回传的编排。

三条设计约束，每条都有验收上的理由：

1. **重发不改变请求内容。** 断网、重启、重复投递之后中心必须能认出"这是同一个调用
   的同一件事"。因此重试只重发原样的请求体；事件的 `event_uuid` 与调用的
   `local_task_uuid` 都是持久化身份，不是每次调用新生成的随机值。

2. **会话失效可以重试，事件身份不可以重算。** 会话（process/session/epoch）是传输层
   的租约，过期后重新握手并重发同一请求是安全的——中心按事件身份去重。但如果重试时
   顺手换了 `event_uuid`，去重就失效了，同一个结果会被记成两件事。

3. **`REPLAYED` 与 `APPLIED` 都是成功。** 中心已经收下这件事，只是这次没有产生新记录。
   把它当失败会让工站反复重发，而每一次重发都还是被去重挡回——看起来像卡住。真正需要
   人工处理的是"事件身份相同但内容不同"，那由中心报冲突，工站必须停下来。
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping

from unilabos.app.center_client.config import CenterClientConfig
from unilabos.app.center_client.transport import (
    SESSION_HEADERS,
    CenterTransport,
    CenterTransportError,
    RequestHoldStore,
)
from unilabos.center_protocol.task_package import TaskPackageError, validate_task_package

logger = logging.getLogger(__name__)

#: 应该重新握手并重发同一请求的中心侧错误码。
SESSION_LOST_CODES = frozenset({"SESSION_EXPIRED"})
#: 中心当前用 STALE_SESSION 表示控制权已经失效；不得自动尝试抢回。
SESSION_FENCED_CODES = frozenset({"SESSION_FENCED", "STALE_SESSION"})

#: 协议版本。中心的 `SessionService::open_session` 只接受 `"1.0"`，不匹配会得到
#: `PROTOCOL_INCOMPATIBLE`。它会写进会话请求摘要，因此两端必须一致。
PROTOCOL_VERSION = "1.0"


class CenterClientError(RuntimeError):
    """客户端无法继续。"""


class SessionFencedError(CenterClientError):
    """本会话已被更新的会话取代。

    这不是需要重试的错误：抢回会话会让两台施工站同时执行同一批调用。正确做法是停止
    领取并交给管理员核实控制权，单纯重启不授予接管许可。
    """


@dataclass
class ControlSession:
    process_uuid: str
    session_uuid: str
    session_epoch: int
    station_uuid: str
    instance_uuid: str

    def headers(self) -> dict[str, str]:
        return {
            SESSION_HEADERS["process"]: self.process_uuid,
            SESSION_HEADERS["session"]: self.session_uuid,
            SESSION_HEADERS["epoch"]: str(self.session_epoch),
        }


@dataclass
class TaskPackage:
    """中心冻结给这个调用的一份输入。"""

    station_invocation_uuid: str
    global_task_uuid: str
    contract_uuid: str
    input: Mapping[str, Any]
    payload_digest: str
    raw: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_response(cls, body: Mapping[str, Any]) -> "TaskPackage":
        try:
            package = validate_task_package(body)
        except TaskPackageError as error:
            raise CenterClientError(str(error)) from error
        raw = package["payload"]
        return cls(
            station_invocation_uuid=raw["station_invocation_uuid"],
            global_task_uuid=raw["global_task_uuid"],
            contract_uuid=raw["contract_uuid"], input=raw["input"],
            payload_digest=package["payload_digest"], raw=raw,
        )


class CenterClient:
    """对中心机器接口的编排。线程安全（传输层每线程一个连接）。"""

    def __init__(
        self,
        config: CenterClientConfig,
        transport: CenterTransport | None = None,
        *,
        process_uuid: str | None = None,
    ) -> None:
        self._config = config
        self._lock = threading.RLock()
        self._fenced = False
        self._evidence_session: ControlSession | None = None
        self._pending_handshake: dict[str, Any] | None = None
        self._transport = transport or CenterTransport(config)
        # 显式声明的前驱会话 (session_uuid, epoch)，用于恢复接入或进程重启恢复。
        # 普通重试不设置它；是否允许恢复始终由中心裁决。
        self._takeover: tuple[str, int] | None = None
        # 进程身份标识"这台工站上的这次运行"。重启会得到新值，中心据此把旧会话判为
        # 已退休，而不是误认为同一进程换了会话。
        self._process_uuid = process_uuid or str(uuid.uuid4())
        self._session: ControlSession | None = None

    def reload_credentials(self, config: CenterClientConfig, *, recover_control: bool = False) -> None:
        """续期保留会话；仅在恢复接入码已成功换证后重新申请控制会话。"""
        with self._lock:
            if (config.station_uuid, config.instance_uuid) != (self._config.station_uuid, self._config.instance_uuid):
                raise ValueError("续期不能更换工站身份")
            self._transport.reload_credentials(config)
            self._config = config
            if recover_control:
                # 管理员授权换证不等于取得控制权：携带原会话前驱，由中心重新裁决。
                # 保留进程身份，避免将仍在运行的工站伪装成新进程绕过接管保护。
                previous = self._session or self._evidence_session
                self._takeover = ((previous.session_uuid, previous.session_epoch)
                                  if previous is not None and previous.process_uuid == self._process_uuid else None)
                self._session = None
                self._pending_handshake = None
                self._fenced = False

    @property
    def process_uuid(self) -> str:
        return self._process_uuid

    def install_request_holds(self, store: RequestHoldStore) -> None:
        """仅真实网络传输需要持久重传暂停；测试协议端口保持原有边界。"""
        if isinstance(self._transport, CenterTransport):
            self._transport.install_request_holds(store)

    @property
    def session(self) -> ControlSession | None:
        return self._session

    def fence_session(self, expected: ControlSession) -> bool:
        """只锁定提示对应的当前会话；旧连接迟到消息不能撤销已更新会话。"""
        with self._lock:
            if self._session != expected:
                return False
            self._evidence_session = self._session
            self._session = None
            self._fenced = True
            return True

    def restore_evidence_session(self, session: ControlSession) -> None:
        """恢复补报用的历史会话，不授予新进程控制权。"""
        try:
            for value in (session.process_uuid, session.session_uuid):
                uuid.UUID(value)
            if (session.station_uuid != self._config.station_uuid
                    or session.instance_uuid != self._config.instance_uuid
                    or type(session.session_epoch) is not int
                    or not 1 <= session.session_epoch <= 2**53 - 1):
                raise ValueError("历史会话身份不匹配")
        except (ValueError, TypeError, AttributeError) as error:
            raise CenterClientError("历史补报会话无效") from error
        with self._lock:
            self._evidence_session = session

    # ------------------------------------------------------------- 会话

    def open_session(self, event_epoch_uuid: str) -> ControlSession:
        with self._lock:
            if self._fenced:
                raise SessionFencedError("控制会话已失效，禁止自动抢回")
            return self._open_session(event_epoch_uuid)

    def _open_session(self, event_epoch_uuid: str) -> ControlSession:
        """与中心握手，取得控制会话。

        `event_epoch_uuid` 是工站本地事件纪元的身份：中心用它判断工站是否换了一次
        "事件账本"，从而决定是否需要人工对账。它必须由调用方持久化，不能在每次握手时
        重新生成——那样中心每次都会以为工站换了一个新纪元。
        """
        body = {
            "process_uuid": self._process_uuid,
            "event_epoch_uuid": event_epoch_uuid,
            "protocol_version": PROTOCOL_VERSION,
        }
        previous = self._session
        if previous is not None:
            # 带上次会话的 fence：中心据此判定"同进程续期"还是"被接管"。
            body["previous_session_uuid"] = previous.session_uuid
            body["previous_session_epoch"] = previous.session_epoch
        elif self._takeover is not None:
            # 进程重启后接管：中心要求接管者声明它要接管的是哪一次会话。
            body["previous_session_uuid"], body["previous_session_epoch"] = self._takeover
        if self._pending_handshake is not None:
            if self._pending_handshake["event_epoch_uuid"] != event_epoch_uuid:
                raise CenterClientError("握手重试期间不能更换事件纪元")
            body = self._pending_handshake
        else:
            self._pending_handshake = dict(body)
        try:
            response = self._transport.request("POST", "/api/v1/stations/session", json_body=body)
        except CenterTransportError as error:
            if not error.retryable:
                self._pending_handshake = None
            raise
        if set(response) != {"session_uuid", "session_epoch", "station_uuid", "instance_uuid"}:
            raise CenterClientError("会话响应字段缺失或包含未知字段")
        try:
            for key in ("session_uuid", "station_uuid", "instance_uuid"):
                uuid.UUID(response[key])
            epoch = response["session_epoch"]
            if type(epoch) is not int or not 1 <= epoch <= 2**53 - 1:
                raise ValueError("epoch 无效")
        except (ValueError, TypeError, AttributeError) as error:
            raise CenterClientError("会话响应身份或 epoch 无效") from error
        session = ControlSession(
            process_uuid=self._process_uuid,
            session_uuid=str(response["session_uuid"]),
            session_epoch=int(response["session_epoch"]),
            station_uuid=str(response["station_uuid"]),
            instance_uuid=str(response["instance_uuid"]),
        )
        # 中心从证书取身份；本地声明的身份与它不一致说明部署配错了证书。
        expected = self._config.station_uuid
        if (session.station_uuid != expected
                or session.instance_uuid != self._config.instance_uuid):
            raise CenterClientError(
                "证书身份与本地配置不一致："
                f"中心认为 station={session.station_uuid}，本地配置为 {expected}"
            )
        self._session = session
        self._evidence_session = session
        self._pending_handshake = None
        self._takeover = None
        logger.info(
            "已与中心建立控制会话 session=%s epoch=%s",
            session.session_uuid,
            session.session_epoch,
        )
        return session

    def resume_after_restart(
        self, event_epoch_uuid: str, active: "ActiveSession"
    ) -> ControlSession:
        """显式提交前驱 fence；不同进程仍须中心管理员完成恢复确认。

        process_uuid 每个进程重新生成，绝不能通过复用旧进程身份绕过 SESSION_ACTIVE。
        """
        with self._lock:
            self._takeover = (active.session_uuid, active.session_epoch)
            try:
                return self.open_session(event_epoch_uuid)
            finally:
                self._takeover = None

    def _ensure_session(self, event_epoch_uuid: str) -> ControlSession:
        if self._session is None:
            return self.open_session(event_epoch_uuid)
        return self._session

    def _with_session_retry(self, event_epoch_uuid: str, operation, *, evidence: bool = False) -> dict[str, Any]:
        with self._lock:
            if evidence and self._session is None and self._evidence_session is not None:
                # 重启或接管后只补报原 owner 事实，不能为此抢回控制会话。
                return operation(self._evidence_session)
            if self._fenced:
                raise SessionFencedError("控制会话已失效，禁止继续领取")
            self._ensure_session(event_epoch_uuid)
            assert self._session is not None
            try:
                return operation(self._session)
            except CenterTransportError as error:
                if error.code in SESSION_FENCED_CODES:
                    self._fenced = True
                    self._session = None
                    raise SessionFencedError("控制会话已被接管，停止领取") from error
                if error.code not in SESSION_LOST_CODES:
                    raise
                # 保留前驱 fence；响应丢失时 open_session 会重发同一握手请求。
                self.open_session(event_epoch_uuid)
                assert self._session is not None
                return operation(self._session)

    # ------------------------------------------------------- 领取与回传

    def authorize_transfer_plan(self, token: str, request_digest: str, event_epoch_uuid: str) -> None:
        """用已有 mTLS 身份向中心核验只读检查凭证，不在工站共享中心签名密钥。"""
        def operation(session: ControlSession) -> dict[str, Any]:
            return self._transport.request(
                "POST", "/api/v1/stations/transfer-plan/authorize",
                json_body={"token": token, "request_digest": request_digest},
                session_headers=session.headers(), expected_status=200,
            )
        if self._with_session_retry(event_epoch_uuid, operation) != {"authorized": True}:
            raise CenterClientError("中心未授权此搬运检查")

    def task_package(
        self, invocation_uuid: str, event_epoch_uuid: str
    ) -> TaskPackage:
        """取任务包。读取本身**不**授予执行许可。"""

        def operation(session: ControlSession) -> dict[str, Any]:
            return self._transport.request(
                "GET",
                f"/api/v1/station-invocations/{invocation_uuid}/task-package",
                session_headers=session.headers(),
            )

        package = TaskPackage.from_response(self._with_session_retry(event_epoch_uuid, operation))
        if (package.station_invocation_uuid != invocation_uuid
                or package.raw["station_uuid"] != self._config.station_uuid):
            raise CenterClientError("任务包的调用或工站身份与请求不匹配")
        return package

    def claim(
        self, invocation_uuid: str, payload_digest: str, event_epoch_uuid: str
    ) -> bool:
        """领取调用。返回是否被识别为重放（重放同样是成功）。"""

        def operation(session: ControlSession) -> dict[str, Any]:
            return self._transport.request(
                "POST",
                f"/api/v1/station-invocations/{invocation_uuid}/claim",
                json_body={"payload_digest": payload_digest},
                session_headers=session.headers(),
            )

        response = self._with_session_retry(event_epoch_uuid, operation)
        if (set(response) != {"invocation_uuid", "owner_instance_uuid", "replayed"}
                or response["invocation_uuid"] != invocation_uuid
                or response["owner_instance_uuid"] != self._config.instance_uuid
                or type(response["replayed"]) is not bool):
            raise CenterClientError("领取确认的调用或 owner 身份不匹配")
        return response["replayed"]

    def allocate_material_lineages(self, invocation_uuid: str, request: dict[str, Any], event_epoch_uuid: str) -> dict[str, Any]:
        """原 owner 的终态物料申请；历史补报会话不授予执行权。"""
        from unilabos.center_protocol.material import canonical_uuid, validate_allocation_receipt

        canonical_uuid(invocation_uuid)

        def operation(session: ControlSession) -> dict[str, Any]:
            return self._transport.request(
                "POST", f"/api/v1/station-invocations/{invocation_uuid}/material-lineages",
                json_body=request, session_headers=session.headers(),
            )

        response = self._with_session_retry(event_epoch_uuid, operation, evidence=True)
        return validate_allocation_receipt(response, invocation_uuid=invocation_uuid,
            instance_uuid=self._config.instance_uuid, station_uuid=self._config.station_uuid, request=request)

    def settle_canceled_material(self, invocation_uuid: str, request: dict[str, Any], event_epoch_uuid: str) -> dict[str, Any]:
        """原 owner 的取消历史结算；不取得新执行、运输或解除未知位置保护的许可。"""
        from unilabos.app.center_client.material_settlement import validate_receipt
        self._reconciliation_invocation(invocation_uuid)
        def operation(session: ControlSession):
            return self._transport.request('POST', f'/api/v1/station-invocations/{invocation_uuid}/material-cancel-settlement',
                json_body=request, session_headers=session.headers())
        response = self._with_session_retry(event_epoch_uuid, operation, evidence=True)
        return validate_receipt(response, invocation_uuid, self._config.instance_uuid, request)

    def publish_material_boundary(self, invocation_uuid: str, request: dict[str, Any], event_epoch_uuid: str) -> dict[str, Any]:
        """补报完整容器边界；使用原 owner 证据通道，不取得运输或执行许可。"""
        from unilabos.center_protocol.material import canonical_uuid, validate_boundary_receipt

        canonical_uuid(invocation_uuid)

        def operation(session: ControlSession) -> dict[str, Any]:
            return self._transport.request(
                "POST", f"/api/v1/station-invocations/{invocation_uuid}/material-boundary",
                json_body=request, session_headers=session.headers(),
            )

        response = self._with_session_retry(event_epoch_uuid, operation, evidence=True)
        return validate_boundary_receipt(response, invocation_uuid=invocation_uuid,
            instance_uuid=self._config.instance_uuid, request=request)

    def report_material_input_issue(self, invocation_uuid: str, request: dict[str, Any],
                                    event_epoch_uuid: str) -> dict[str, Any]:
        """当前控制实例报告原冻结输入无法执行；不伪造本地 Task 事件。"""
        from unilabos.center_protocol.material import canonical_uuid
        from unilabos.center_protocol.material_input import validate_issue_receipt, validate_issue_request

        canonical_uuid(invocation_uuid)
        body = validate_issue_request(request)

        def operation(session: ControlSession) -> dict[str, Any]:
            return self._transport.request(
                "POST", f"/api/v1/station-invocations/{invocation_uuid}/material-input-issue",
                json_body=body, session_headers=session.headers(),
            )

        response = self._with_session_retry(event_epoch_uuid, operation)
        return validate_issue_receipt(response, invocation_uuid=invocation_uuid,
            instance_uuid=self._config.instance_uuid, request=body)

    def event_history(self, request: dict[str, Any]) -> dict[str, Any]:
        """显式读取原收件证据并恢复连续前缀，不取得新执行许可。"""
        from unilabos.center_protocol.event_history import validate_request, validate_response

        body = validate_request(request)

        def operation(session: ControlSession) -> dict[str, Any]:
            return self._transport.request(
                "POST", f"/api/v1/stations/{self._config.station_uuid}/event-history",
                json_body=body, session_headers=session.headers(),
            )

        response = self._with_session_retry(body["event_epoch_uuid"], operation, evidence=True)
        return validate_response(response, body, station_uuid=self._config.station_uuid,
                                 instance_uuid=self._config.instance_uuid)

    def recovery_checkpoint(self, checkpoint_uuid: str, *, event_epoch_uuid: str, recovery_uuid: str) -> dict[str, Any]:
        """基线正文只能从中心取回；本地核实文件只引用其 UUID。"""
        from unilabos.center_protocol.material import canonical_uuid
        from unilabos.center_protocol.recovery_checkpoint import validate_checkpoint

        canonical_uuid(checkpoint_uuid)
        if self._evidence_session is None:
            raise CenterClientError("读取恢复基线须保留原历史会话")
        body = self._transport.request(
            "GET", f"/api/v1/stations/{self._config.station_uuid}/recovery-checkpoints/{checkpoint_uuid}",
            session_headers=self._evidence_session.headers(),
        )
        return validate_checkpoint(body, checkpoint_uuid=checkpoint_uuid, station_uuid=self._config.station_uuid,
                                   instance_uuid=self._config.instance_uuid, event_epoch_uuid=event_epoch_uuid,
                                   recovery_uuid=recovery_uuid)

    def retain_recovery_evidence(self, envelope: dict[str, Any]) -> dict[str, Any]:
        """仅保存原证据；精确保管回执不能作为普通事件 ACK。"""
        from unilabos.center_protocol.recovery_evidence import validate_envelope, validate_receipt

        event = validate_envelope(envelope)
        if event["station_uuid"] != self._config.station_uuid or event["instance_uuid"] != self._config.instance_uuid:
            raise CenterClientError("保管证据不属于本实例")
        if self._evidence_session is None:
            raise CenterClientError("保管证据必须使用持久原会话，禁止新建控制会话")

        def operation(session: ControlSession) -> dict[str, Any]:
            return self._transport.request(
                "POST", f"/api/v1/stations/{self._config.station_uuid}/recovery-evidence",
                json_body={"envelope": event}, session_headers=session.headers(), expected_status=202,
            )

        receipt = operation(self._evidence_session)
        return validate_receipt(receipt, event)

    def report(self, envelope: Mapping[str, Any]) -> str:
        """发送已持久化的完整事件信封，逐字段核对 ACK 后返回入账结果。

        调用者必须保留原 UUID、纪元、序号、正文、时间及摘要；这里不分配任何
        事件身份。conflict 表示证据已被接收但存在冲突，调用方应隔离并停止热重传，
        不能把它当作 applied。旧的散字段调用格式不再发送到网络。
        """
        from unilabos.app.center_client.events import EventEnvelopeError, validate_ack, validate_event

        try:
            body = validate_event(envelope)
            if body["station_uuid"] != self._config.station_uuid or body["instance_uuid"] != self._config.instance_uuid:
                raise EventEnvelopeError("事件工站或实例与本地凭据不匹配")
        except EventEnvelopeError as error:
            raise CenterClientError(str(error)) from error

        def operation(session: ControlSession) -> dict[str, Any]:
            return self._transport.request(
                "POST", f"/api/v1/station-invocations/{body['station_invocation_uuid']}/events",
                json_body=body, session_headers=session.headers(), expected_status=202,
            )

        response = self._with_session_retry(body["event_epoch_uuid"], operation, evidence=True)
        try:
            return validate_ack(response, body)
        except EventEnvelopeError as error:
            raise CenterClientError(str(error)) from error

    def report_preparation(self, event: dict[str, Any]) -> dict[str, Any]:
        """独立准备事实沿原站身份、纪元与共享事件序号确认。"""
        from unilabos.center_protocol.events import validate_ack
        from unilabos.center_protocol.transport_preparation import validate_event
        event = validate_event(event)
        if event['station_uuid'] != self._config.station_uuid or event['instance_uuid'] != self._config.instance_uuid:
            raise CenterClientError('准备事件不属于本站凭据')
        def operation(session):
            return self._transport.request('POST','/api/v1/transport-preparation-events',json_body=event,
                                           session_headers=session.headers())
        response = self._with_session_retry(event['event_epoch_uuid'],operation,evidence=True)
        validate_ack(response,event)
        return response

    def handoff_package(self, handoff_uuid: str, revision: int | None,
                        event_epoch_uuid: str) -> dict[str, Any]:
        """读取通知指定的固定交接包；latest 仅用于发现当前状态。"""
        from unilabos.center_protocol.handoff import HandoffProtocolError, validate_handoff_package

        self._reconciliation_invocation(handoff_uuid)
        if revision is not None and (type(revision) is not int or not 1 <= revision <= 2**53 - 1):
            raise CenterClientError("交接修订必须为安全正整数")
        suffix = "package" if revision is None else f"packages/{revision}"

        def operation(session: ControlSession):
            return self._transport.request("GET", f"/api/v1/handoffs/{handoff_uuid}/{suffix}",
                                           session_headers=session.headers())

        try:
            result = validate_handoff_package(
                self._with_session_retry(event_epoch_uuid, operation, evidence=True),
                station_uuid=self._config.station_uuid, instance_uuid=self._config.instance_uuid,
                expected_revision=revision,
            )
            if result["payload"]["handoff_uuid"] != handoff_uuid:
                raise HandoffProtocolError("交接包身份与请求不一致")
            return result
        except HandoffProtocolError as error:
            raise CenterClientError(str(error)) from error

    def attest_material_inputs(self, container_uuid: str, request: dict[str, Any], event_epoch_uuid: str) -> dict[str, Any]:
        """由本站当前机器会话证明已知谱系的实际完整树，不分配新身份。"""
        from unilabos.center_protocol.digest import digest
        self._reconciliation_invocation(container_uuid)
        request_digest = digest(request)
        def operation(session: ControlSession):
            return self._transport.request("POST", f"/api/v1/containers/{container_uuid}/material-inputs",
                json_body=request, session_headers={**session.headers(), "Idempotency-Key": request_digest})
        result = self._with_session_retry(event_epoch_uuid, operation)
        if result != {"container_uuid": container_uuid, "container_state_version": request["container_state_version"],
                      "request_digest": request_digest}:
            raise CenterClientError("本站历史物料输入证明回执与原请求不一致")
        return result

    def register_container(self, request: dict[str, Any], event_epoch_uuid: str) -> dict[str, Any]:
        """复用机器控制会话登记原 c；幂等键绑定持久容器身份。"""
        from unilabos.center_protocol.handoff import validate_container

        identity = request["container_uuid"]
        self._reconciliation_invocation(identity)
        def operation(session: ControlSession):
            return self._transport.request("POST", "/api/v1/containers", json_body=request,
                session_headers={**session.headers(), "Idempotency-Key": identity})
        return validate_container(self._with_session_retry(event_epoch_uuid, operation), identity)

    def container(self, container_uuid: str, event_epoch_uuid: str, *,
                  handoff_uuid: str | None = None) -> dict[str, Any]:
        """按当前归属或具体交接授权读取真实条码，不从手工输入猜测全局身份。"""
        from unilabos.center_protocol.handoff import HandoffProtocolError, validate_container

        self._reconciliation_invocation(container_uuid)
        prefix = "/api/v1"
        if handoff_uuid is not None:
            self._reconciliation_invocation(handoff_uuid)
            prefix += f"/handoffs/{handoff_uuid}"

        def operation(session: ControlSession):
            return self._transport.request("GET", f"{prefix}/containers/{container_uuid}",
                                           session_headers=session.headers())

        try:
            return validate_container(self._with_session_retry(event_epoch_uuid, operation), container_uuid)
        except HandoffProtocolError as error:
            raise CenterClientError(str(error)) from error

    def report_handoff(self, envelope: Mapping[str, Any]) -> dict[str, Any]:
        """只补报已提交的交接事实；精确确认不表示库存冲突已经解除。"""
        from unilabos.center_protocol.handoff import HandoffProtocolError, validate_handoff_ack, validate_handoff_event

        try:
            event = validate_handoff_event(envelope)
            if event["station_uuid"] != self._config.station_uuid or event["instance_uuid"] != self._config.instance_uuid:
                raise HandoffProtocolError("交接事件不属于本机原实例")
        except HandoffProtocolError as error:
            raise CenterClientError(str(error)) from error

        def operation(session: ControlSession):
            return self._transport.request("POST", f"/api/v1/handoffs/{event['handoff_uuid']}/events",
                                           json_body={"actor_kind": "station", "event": event},
                                           session_headers=session.headers())

        # prepared/released 是控制承诺；收货/撤销回执是原实例的历史物理证据。
        response = self._with_session_retry(event["event_epoch_uuid"], operation,
                                            evidence=event["payload"]["kind"] not in {"prepared", "released"})
        try:
            return validate_handoff_ack(response, event)
        except HandoffProtocolError as error:
            raise CenterClientError(str(error)) from error

    def pull_commands(self, after_sequence: int, event_epoch_uuid: str, *, limit: int = 100) -> dict[str, Any]:
        """拉取有序通知；持久化及摘要校验由运行时负责，读取不授予执行权。"""
        if type(after_sequence) is not int or not 0 <= after_sequence <= 2**53 - 1:
            raise CenterClientError("命令游标无效")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise CenterClientError("命令分页大小无效")

        def operation(session: ControlSession) -> dict[str, Any]:
            return self._transport.request(
                "GET", f"/api/v1/stations/{self._config.station_uuid}/commands?after_sequence={after_sequence}&limit={limit}",
                session_headers=session.headers(),
            )

        return self._with_session_retry(event_epoch_uuid, operation)

    def ack_command(self, command_uuid: str, command_sequence: int,
                    command_digest: str, event_epoch_uuid: str) -> None:
        """仅在本地收件箱提交后调用；服务端前缀必须覆盖本条，不能用于跳过本地记录。"""
        def operation(session: ControlSession) -> dict[str, Any]:
            return self._transport.request(
                "POST", f"/api/v1/stations/{self._config.station_uuid}/commands/ack",
                json_body={"command_uuid": command_uuid, "command_sequence": command_sequence,
                           "command_digest": command_digest},
                session_headers=session.headers(),
            )

        response = self._with_session_retry(event_epoch_uuid, operation)
        if (set(response) != {"acknowledged_sequence"}
                or type(response["acknowledged_sequence"]) is not int
                or not command_sequence <= response["acknowledged_sequence"] <= 2**53 - 1):
            raise CenterClientError("命令 ACK 不匹配，保留收件箱等待重传")

    def reconciliation_state(self, invocation_uuid: str, event_epoch_uuid: str) -> dict[str, Any]:
        """读取原 owner 可核实的证据快照；历史会话仅用于读取或补证据。"""
        from unilabos.center_protocol.reconciliation import validate_snapshot

        self._reconciliation_invocation(invocation_uuid)
        def operation(session: ControlSession):
            return self._transport.request(
                "GET", f"/api/v1/station-invocations/{invocation_uuid}/reconciliation-state",
                session_headers=session.headers(),
            )
        response = validate_snapshot(self._with_session_retry(event_epoch_uuid, operation, evidence=True), invocation_uuid)
        if response["station_uuid"] != self._config.station_uuid:
            raise CenterClientError("核实快照工站与本地配置不匹配")
        return response

    @staticmethod
    def _reconciliation_invocation(invocation_uuid: str) -> None:
        try:
            if not isinstance(invocation_uuid, str) or str(uuid.UUID(invocation_uuid)) != invocation_uuid:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise CenterClientError("核实调用必须使用规范 UUID") from None

    def reconcile(self, invocation_uuid: str, body: dict[str, Any], *, idempotency_key: str,
                  event_epoch_uuid: str) -> dict[str, Any]:
        """发送调用方已持久保存的核实正文；恢复必须使用当前原 owner 控制会话。"""
        from unilabos.center_protocol.reconciliation import validate_receipt, validate_request

        self._reconciliation_invocation(invocation_uuid)
        body = validate_request(body)
        resume = body["resolution"]["kind"] == "resume_original_task"
        if resume and body["resolution"]["event_epoch_uuid"] != event_epoch_uuid:
            raise CenterClientError("恢复核实不能复用旧事件纪元")
        if (not isinstance(idempotency_key, str) or not idempotency_key.strip()
                or len(idempotency_key.encode("utf-8")) > 200):
            raise CenterClientError("核实幂等键无效")

        def operation(session: ControlSession):
            return self._transport.request(
                "POST", f"/api/v1/station-invocations/{invocation_uuid}/reconciliations", json_body=body,
                session_headers={**session.headers(), "Idempotency-Key": idempotency_key},
            )
        receipt = validate_receipt(self._with_session_retry(event_epoch_uuid, operation, evidence=not resume),
                                   invocation_uuid, body, actor_instance_uuid=self._config.instance_uuid)
        if (receipt["snapshot"]["station_uuid"] != self._config.station_uuid
                or (resume and receipt["snapshot"]["state"]["owner_instance_uuid"] != self._config.instance_uuid)):
            raise CenterClientError("核实回执不属于当前工站原 owner")
        return receipt

    def current_catalog(self, event_epoch_uuid: str) -> dict[str, Any] | None:
        from unilabos.app.center_client.catalog import validate_receipt

        def operation(session: ControlSession):
            return self._transport.request(
                "GET", f"/api/v1/stations/{self._config.station_uuid}/workflow-catalog/current",
                session_headers=session.headers(), allow_null_response=True,
            )
        return validate_receipt(self._with_session_retry(event_epoch_uuid, operation), self._config.station_uuid)

    def upload_catalog(self, upload: Mapping[str, Any], event_epoch_uuid: str) -> dict[str, Any]:
        """上传持久分块再提交；任意确认不匹配都保留原版本重试。"""
        from unilabos.app.center_client.catalog import validate_receipt

        manifest = upload["manifest"]
        prefix = f"/api/v1/stations/{self._config.station_uuid}/workflow-catalog/{manifest['catalog_version']}"
        for chunk in upload["chunks"]:
            def operation(session: ControlSession, body=chunk):
                return self._transport.request(
                    "PUT", f"{prefix}/chunks/{body['chunk_index']}", json_body=body,
                    session_headers=session.headers(),
                )
            response = self._with_session_retry(event_epoch_uuid, operation)
            expected = {key: chunk[key] for key in ("station_uuid", "catalog_version", "chunk_index", "chunk_digest")}
            if response != expected:
                raise CenterClientError("目录分块确认与冻结正文不一致")

        def commit(session: ControlSession):
            return self._transport.request(
                "POST", f"{prefix}/commit", json_body=manifest,
                session_headers={**session.headers(), "Idempotency-Key": manifest["catalog_version"]},
            )
        receipt = validate_receipt(self._with_session_retry(event_epoch_uuid, commit), self._config.station_uuid)
        if receipt != upload["receipt"]:
            raise CenterClientError("目录提交回执与冻结 manifest 不一致")
        return receipt


@dataclass(frozen=True)
class ActiveSession:
    """中心告知的当前会话，用于接管。"""

    session_uuid: str
    session_epoch: int
