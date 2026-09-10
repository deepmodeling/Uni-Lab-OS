"""可信工作流创作编译器（Authoring Compiler）的深模块门面。"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from typing import Any

from unilabos.workflow.authoring_ast import (
    AuthoringSyntaxError,
    author_source_map,
    diagnostic_source_range,
    parse_authoring_source,
)
from unilabos.workflow.authoring_graph import (
    AuthoringGraphError,
    build_candidate_graph,
    candidate_changeset,
    semantic_graph_equal,
)
from unilabos.workflow.authoring_kernel import (
    AuthoringCatalogError,
    AuthoringCatalogSnapshot,
)
from unilabos.workflow.authoring_material import MaterialAuthoringError
from unilabos.workflow.authoring_python import render_authoring_python
from unilabos.workflow.candidate_validation import (
    CandidateBundleError,
    validate_candidate_bundle,
)
from unilabos.workflow.composite import CompositeAuthoring
from unilabos.workflow.material_graph_validation import (
    MaterialGraphValidationError,
    validate_material_graph_projection,
)
from unilabos.workflow.material_selector import (
    MaterialSelectorError,
    validate_material_source_node,
)
from unilabos.workflow.models import CandidateCompilation, validate_uuid
from unilabos.workflow.resource_reference import ResourceReferenceResolver
from unilabos.workflow.source_coordinates import require_utf8_text

_COMPILER_VERSION = "unilabos-authoring/f02a-v2"


class WorkflowAuthoringEngine:
    """以不可变目录快照和只读身份端口提供三个可信工作流创作转换。"""

    compiler_version = _COMPILER_VERSION

    def __init__(
        self,
        *,
        catalog: AuthoringCatalogSnapshot,
        resource_reference_resolver: ResourceReferenceResolver | None = None,
        composite_authoring: CompositeAuthoring | None = None,
    ) -> None:
        """创建带不可变目录和可选只读资源身份端口的创作编译器。

        参数说明：``catalog`` 是构造时固定的目录快照（Catalog Snapshot）；编译
        期间只读该快照，不导入作者源码，也不修改外部状态；
        ``resource_reference_resolver`` 只读取库存权威（Inventory Authority）并
        把部署业务 ID 解析为实际物料 UUID，不取得预留或执行占用；
        ``composite_authoring`` 可选提供已发布工作流的只读静态展开。返回：无。
        异常：目录或组合端口类型不合法时抛出 ``TypeError``。
        """

        if not isinstance(catalog, AuthoringCatalogSnapshot):
            raise TypeError("catalog 必须是 AuthoringCatalogSnapshot")
        if composite_authoring is not None and not isinstance(
            composite_authoring,
            CompositeAuthoring,
        ):
            raise TypeError("composite_authoring 必须是 CompositeAuthoring")
        self._catalog = catalog
        self._resource_reference_resolver = resource_reference_resolver
        self._composite_authoring = composite_authoring

    @property
    def template_catalog_fingerprint(self) -> str:
        """返回当前不可变目录快照的 SHA-256 指纹。"""

        return self._catalog.fingerprint

    @contextmanager
    def catalog_snapshot(self) -> Iterator[str]:
        """在一个转换临界区暴露固定目录指纹。

        返回值：上下文中的字符串是本实例唯一目录指纹；因为快照不可变，无需
        额外锁或释放动作。
        """

        yield self._catalog.fingerprint

    def compile(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        python_source: str,
        source_uri: str,
        applied_graph: dict[str, Any],
    ) -> CandidateCompilation:
        """静态编译作者源码为候选结果。

        参数说明：工作流 UUID/修订是服务权威身份，``python_source`` 是不可信
        草稿，``source_uri`` 仅用于诊断来源，``applied_graph`` 是变更集基线。
        返回结构化成功或失败结果；绝不执行作者源码或修改外部状态，仅允许
        注入的资源身份端口读取库存权威（Inventory Authority）。
        异常：预期的语法、目录、图或候选错误收敛为诊断；只读依赖端口发生的
        未声明基础设施异常原样传播。
        """

        try:
            identity, revision = _request_identity(workflow_uuid, workflow_revision)
            _source_contract(python_source, source_uri)
            program = parse_authoring_source(
                python_source=python_source,
                expected_workflow_uuid=identity,
            )
            graph, changeset = build_candidate_graph(
                program=program,
                catalog=self._catalog,
                applied_graph=applied_graph,
                resource_reference_resolver=self._resource_reference_resolver,
                composite_authoring=self._composite_authoring,
            )
            if graph["workflow"].get("revision") != revision:
                raise AuthoringGraphError(
                    "candidate_invalid",
                    "已应用工作流修订与编译请求不一致",
                )
            rendered = render_authoring_python(
                graph=graph,
                catalog=self._catalog,
                function_docstring=program.function_docstring,
            )
            validate_candidate_bundle(
                graph=graph,
                base_graph=applied_graph,
                workflow_uuid=identity,
                revision=revision,
                source_map=rendered.source_map,
                changeset=changeset,
            )
            return CandidateCompilation(
                diagnostics=[],
                graph=graph,
                normalized_python_source=rendered.python_source,
                source_map=rendered.source_map,
                changeset=changeset,
                compiler_version=self.compiler_version,
                template_catalog_fingerprint=self.template_catalog_fingerprint,
            )
        except AuthoringSyntaxError as error:
            return _error_result(
                fingerprint=self.template_catalog_fingerprint,
                code=error.code,
                message=error.message,
                source_range=diagnostic_source_range(error.node, python_source),
            )
        except MaterialAuthoringError as error:
            return _error_result(
                fingerprint=self.template_catalog_fingerprint,
                code=error.code,
                message=error.message,
                source_range=diagnostic_source_range(error.node, python_source),
            )
        except AuthoringGraphError as error:
            return _error_result(
                fingerprint=self.template_catalog_fingerprint,
                code=error.code,
                message=error.message,
            )
        except CandidateBundleError:
            return _error_result(
                fingerprint=self.template_catalog_fingerprint,
                code="candidate_invalid",
                message="生成的候选结果不能通过公共工作流校验",
            )
        except (KeyError, TypeError, UnicodeError, ValueError):
            return _error_result(
                fingerprint=self.template_catalog_fingerprint,
                code="candidate_invalid",
                message="作者源码无法生成可信候选图",
            )

    def preserve_author_source(
        self,
        *,
        compilation: CandidateCompilation,
        workflow_uuid: str,
        workflow_revision: int,
        python_source: str,
        applied_graph: dict[str, Any],
    ) -> CandidateCompilation:
        """把成功编译结果投影回完全相同的作者源码及其源码映射。

        参数说明：``compilation`` 是本引擎刚生成的可信候选；其余参数与
        ``compile`` 的同名输入一致。返回图与变更集不变、但规范源码改为原始
        作者字节且源码映射指向该文本的候选。该能力只供工作区启动激活使用，
        交互式 Apply 仍通过 ``compile`` 的规范化源码展示和写回完整 diff。
        异常：输入不是成功候选，或源码不能再次证明相同图合同，抛出
        ``CandidateBundleError``/``AuthoringSyntaxError``，禁止静默改写作者文件。
        """

        if not compilation.valid or compilation.graph is None:
            raise CandidateBundleError("只有成功编译结果才能保留作者源码")
        if compilation.normalized_python_source == python_source:
            return compilation
        program = parse_authoring_source(
            python_source=python_source,
            expected_workflow_uuid=workflow_uuid,
        )
        source_map = author_source_map(
            program=program,
            python_source=python_source,
        )
        validate_candidate_bundle(
            graph=compilation.graph,
            base_graph=applied_graph,
            workflow_uuid=workflow_uuid,
            revision=workflow_revision,
            source_map=source_map,
            changeset=compilation.changeset,
        )
        return compilation.model_copy(
            update={
                "normalized_python_source": python_source,
                "source_map": source_map,
            }
        )

    def generate_python(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        graph: dict[str, Any],
        source_uri: str,
    ) -> CandidateCompilation:
        """把候选图确定性生成规范 Python 源码。

        参数说明：工作流身份必须与 ``graph`` 一致，``source_uri`` 只校验文本
        合法性；返回保留原图的源码结果，失败时返回结构化诊断。
        """

        try:
            identity, revision = _request_identity(workflow_uuid, workflow_revision)
            _source_contract("", source_uri)
            _assert_graph_identity(graph, identity=identity, revision=revision)
            rendered = render_authoring_python(graph=graph, catalog=self._catalog)
            changeset = candidate_changeset(graph=graph, applied_graph=graph)
            validate_candidate_bundle(
                graph=graph,
                base_graph=graph,
                workflow_uuid=identity,
                revision=revision,
                source_map=rendered.source_map,
                changeset=changeset,
                require_unchanged_graph=True,
            )
            return CandidateCompilation(
                diagnostics=[],
                graph=deepcopy(graph),
                normalized_python_source=rendered.python_source,
                source_map=rendered.source_map,
                changeset=changeset,
                compiler_version=self.compiler_version,
                template_catalog_fingerprint=self.template_catalog_fingerprint,
            )
        except AuthoringGraphError as error:
            return _error_result(
                fingerprint=self.template_catalog_fingerprint,
                code=error.code,
                message=error.message,
            )
        except CandidateBundleError:
            return _error_result(
                fingerprint=self.template_catalog_fingerprint,
                code="candidate_invalid",
                message="候选图不能通过公共工作流校验",
            )
        except (KeyError, TypeError, UnicodeError, ValueError):
            return _error_result(
                fingerprint=self.template_catalog_fingerprint,
                code="candidate_invalid",
                message="候选图无法生成可信作者源码",
            )

    def validate(
        self,
        *,
        workflow_uuid: str,
        workflow_revision: int,
        graph: dict[str, Any],
        python_source: str,
        source_uri: str,
    ) -> CandidateCompilation:
        """共同证明候选图与作者源码达到语义固定点。

        参数说明：``graph`` 与 ``python_source`` 必须描述同一工作流身份和修订；
        返回保留调用方图的成功结果，语义分叉返回 ``round_trip_mismatch``。
        """

        try:
            _validate_material_source_graph(graph, catalog=self._catalog)
            validate_material_graph_projection(graph)
        except MaterialSelectorError as error:
            return _error_result(
                fingerprint=self.template_catalog_fingerprint,
                code=error.code,
                message=error.message,
            )
        except MaterialGraphValidationError as error:
            return _error_result(
                fingerprint=self.template_catalog_fingerprint,
                code=error.code,
                message=error.message,
            )
        compiled = self.compile(
            workflow_uuid=workflow_uuid,
            workflow_revision=workflow_revision,
            python_source=python_source,
            source_uri=source_uri,
            applied_graph=graph,
        )
        if not compiled.valid:
            return compiled
        if not semantic_graph_equal(compiled.graph, graph):
            return _error_result(
                fingerprint=self.template_catalog_fingerprint,
                code="round_trip_mismatch",
                message="作者源码与候选图不能证明语义等价",
            )
        compiled.graph = deepcopy(graph)
        return compiled


def _validate_material_source_graph(
    graph: Mapping[str, Any],
    *,
    catalog: AuthoringCatalogSnapshot,
) -> None:
    """只校验候选图中的物料来源节点。

    参数说明：``graph`` 是共同验证接缝收到的可疑候选图，``catalog`` 是当前
    不可变目录快照。返回：无；物料来源选择器（MaterialSourceSelector）非法时
    抛出 ``MaterialSelectorError``，其他图语义仍交给原往返比较判断。
    """

    nodes = graph.get("nodes") if isinstance(graph, Mapping) else None
    if not isinstance(nodes, list):
        return
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        node_kind = node.get("type")
        template_uuid = node.get("workflow_node_template_uuid")
        if isinstance(template_uuid, str):
            try:
                template = catalog.require_template(template_uuid).template
                node_kind = template.get("node_type") or template.get("type")
            except AuthoringCatalogError:
                continue
        if str(node_kind).strip().lower() == "material_source":
            validate_material_source_node(node)


def _request_identity(workflow_uuid: str, revision: int) -> tuple[str, int]:
    """规范编译请求中的工作流身份和修订。

    参数说明：``workflow_uuid`` 必须是非 nil UUID，``revision`` 必须为正整数；
    返回规范二元组，非法输入抛出 ``ValueError``。
    """

    identity = validate_uuid(workflow_uuid)
    if type(revision) is not int or revision < 1:
        raise ValueError("工作流修订必须是正整数")
    return identity, revision


def _source_contract(python_source: str, source_uri: str) -> None:
    """校验作者源码和来源 URI 的纯文本边界。

    参数说明：两个值都必须是有效 UTF-8 字符串，``source_uri`` 不得为空；成功
    无返回值，失败抛出 ``TypeError`` 或 ``ValueError``。
    """

    require_utf8_text(python_source)
    require_utf8_text(source_uri)
    if not source_uri.strip():
        raise ValueError("作者源码 URI 不能为空")


def _assert_graph_identity(
    graph: Mapping[str, Any],
    *,
    identity: str,
    revision: int,
) -> None:
    """确认候选图属于请求工作流和修订。

    参数说明：``graph`` 是候选图，``identity`` 与 ``revision`` 是规范请求身份；
    不一致时抛出 ``AuthoringGraphError``。
    """

    workflow = graph.get("workflow") if isinstance(graph, Mapping) else None
    if not isinstance(workflow, Mapping):
        raise AuthoringGraphError("candidate_invalid", "候选图缺少工作流身份")
    if validate_uuid(workflow.get("uuid")) != identity or workflow.get("revision") != revision:
        raise AuthoringGraphError("candidate_invalid", "候选图身份或修订不匹配")


def _error_result(
    *,
    fingerprint: str,
    code: str,
    message: str,
    source_range: dict[str, int] | None = None,
) -> CandidateCompilation:
    """构造稳定失败候选结果。

    参数说明：目录 ``fingerprint`` 仍需回传，``code``/``message`` 是诊断，
    ``source_range`` 可选；返回不含图、源码和变更集的失败结果。
    """

    diagnostic: dict[str, Any] = {
        "severity": "error",
        "code": code,
        "message": message,
    }
    if source_range is not None:
        diagnostic["source_range"] = source_range
    return CandidateCompilation(
        diagnostics=[diagnostic],
        graph=None,
        normalized_python_source=None,
        source_map=[],
        changeset=None,
        compiler_version=_COMPILER_VERSION,
        template_catalog_fingerprint=fingerprint,
    )


__all__ = ["WorkflowAuthoringEngine"]
