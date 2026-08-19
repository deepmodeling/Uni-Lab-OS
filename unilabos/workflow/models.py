"""Backend-shaped value objects for the local Workflow authority.

The models in this module are deliberately transport-independent.  They use
the frozen Backend field spelling, validate stable UUID identities at the
boundary, and never carry legacy Run identifiers.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from unilabos.workflow.json_codec import MAX_BACKEND_JSON_DEPTH

JsonObject = Dict[str, Any]
JsonArray = List[Any]


def validate_json_value(value: Any) -> Any:
    """Return a recursively valid, finite JSON value."""

    active: set[int] = set()
    stack: list[tuple[Any, int, bool]] = [(value, 0, False)]
    while stack:
        item, depth, leaving = stack.pop()
        if leaving:
            active.remove(id(item))
            continue
        if item is None or isinstance(item, (bool, int, str)):
            continue
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("JSON numbers must be finite")
            continue
        if isinstance(item, (dict, list)):
            if depth + 1 > MAX_BACKEND_JSON_DEPTH:
                raise ValueError("JSON value is nested too deeply")
            identity = id(item)
            if identity in active:
                raise ValueError("JSON values must not contain cycles")
            active.add(identity)
            stack.append((item, depth, True))
            if isinstance(item, dict):
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise ValueError("JSON object keys must be strings")
                    stack.append((child, depth + 1, False))
            else:
                for child in item:
                    stack.append((child, depth + 1, False))
            continue
        raise ValueError(f"{type(item).__name__} is not a JSON value")
    return value


def normalize_json_object(value: Any) -> JsonObject:
    """Mirror Backend JSONObject: explicit null becomes an empty object."""

    normalized = {} if value is None else value
    if not isinstance(normalized, dict):
        raise ValueError("value is not a JSON object")
    validate_json_value(normalized)
    return normalized


def normalize_json_array(value: Any) -> JsonArray:
    """Mirror Backend JSONArray: explicit null becomes an empty array."""

    normalized = [] if value is None else value
    if not isinstance(normalized, list):
        raise ValueError("value is not a JSON array")
    validate_json_value(normalized)
    return normalized


def validate_uuid(value: str) -> str:
    """Return the canonical spelling of one non-nil UUID."""

    parsed = UUID(str(value))
    if parsed.int == 0:
        raise ValueError("UUID must not be nil")
    return str(parsed)


class WorkflowNodeWrite(BaseModel):
    """Complete WorkflowNode payload used by full-graph reconciliation."""

    model_config = ConfigDict(extra="ignore")

    uuid: str
    workflow_node_template_uuid: Optional[str] = None
    parent_uuid: Optional[str] = None
    material_uuid: Optional[str] = None
    name: str
    # Backend 自 c35d821 起已移除 WorkflowNode.status，d552078 仍保持该语义；保留的默认值仅供旧本地
    # Store 内部兼容，公共读写 DTO 不要求或返回该字段。
    status: str = "idle"
    type: str
    icon: Optional[str] = None
    pose: JsonObject = Field(default_factory=dict)
    param: Optional[JsonObject] = None
    footer: Optional[str] = None
    action_name: Optional[str] = None
    action_type: Optional[str] = None
    execution_policy: JsonObject = Field(default_factory=dict)
    disabled: bool = Field(default=False, strict=True)
    minimized: bool = Field(default=False, strict=True)
    script: Optional[str] = None
    description: Optional[str] = None
    meta_data: JsonObject = Field(default_factory=dict)

    @field_validator(
        "pose",
        "execution_policy",
        "meta_data",
        mode="before",
    )
    @classmethod
    def _json_object(cls, value: Any) -> JsonObject:
        return normalize_json_object(value)

    @field_validator("param")
    @classmethod
    def _optional_json_object(
        cls,
        value: Optional[JsonObject],
    ) -> Optional[JsonObject]:
        return None if value is None else normalize_json_object(value)

    @field_validator(
        "uuid",
        "workflow_node_template_uuid",
        "parent_uuid",
        "material_uuid",
    )
    @classmethod
    def _valid_uuid(cls, value: Optional[str]) -> Optional[str]:
        return None if value is None else validate_uuid(value)

    @field_validator("name", "type")
    @classmethod
    def _required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("value must not be blank")
        return normalized

    @field_validator(
        "icon",
        "footer",
        "action_name",
        "action_type",
        "script",
        "description",
    )
    @classmethod
    def _optional_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class WorkflowEdgeWrite(BaseModel):
    """Complete WorkflowEdge payload used by full-graph reconciliation."""

    model_config = ConfigDict(extra="ignore")

    uuid: str
    source_node_uuid: str
    target_node_uuid: str
    source_handle_uuid: str
    target_handle_uuid: str
    description: Optional[str] = None
    meta_data: JsonObject = Field(default_factory=dict)

    @field_validator("meta_data", mode="before")
    @classmethod
    def _json_object(cls, value: Any) -> JsonObject:
        return normalize_json_object(value)

    @field_validator(
        "uuid",
        "source_node_uuid",
        "target_node_uuid",
        "source_handle_uuid",
        "target_handle_uuid",
    )
    @classmethod
    def _valid_uuid(cls, value: str) -> str:
        return validate_uuid(value)

    @field_validator("description")
    @classmethod
    def _optional_text(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class CandidateCompilation(BaseModel):
    """One compiler result before the service issues a Candidate hash."""

    model_config = ConfigDict(extra="forbid")

    diagnostics: Any = Field(default_factory=list)
    graph: Optional[JsonObject] = None
    normalized_python_source: Optional[str] = None
    source_map: Any = Field(default_factory=list)
    changeset: Optional[Any] = None
    compiler_version: str
    template_catalog_fingerprint: str

    @property
    def valid(self) -> bool:
        return (
            self.graph is not None
            and self.normalized_python_source is not None
            and self.changeset is not None
            and isinstance(self.diagnostics, list)
            and all(isinstance(item, dict) for item in self.diagnostics)
            and not any(
                str(item.get("severity", "")).strip().lower() == "error"
                for item in self.diagnostics
            )
        )


class DiagnosticSourceRange(BaseModel):
    """One optional source range attached to a compiler diagnostic."""

    model_config = ConfigDict(extra="forbid")

    start_line: int = Field(ge=1, strict=True)
    start_column: int = Field(ge=1, strict=True)
    end_line: int = Field(ge=1, strict=True)
    end_column: int = Field(ge=1, strict=True)

    @model_validator(mode="after")
    def _ordered_range(self) -> DiagnosticSourceRange:
        if (self.end_line, self.end_column) < (
            self.start_line,
            self.start_column,
        ):
            raise ValueError("source range end precedes its start")
        return self


class CandidateDiagnostic(BaseModel):
    """One stable compiler diagnostic exposed by the Authoring contract."""

    model_config = ConfigDict(extra="forbid")

    severity: str
    code: str
    message: str
    source_range: Optional[DiagnosticSourceRange] = None

    @field_validator("severity")
    @classmethod
    def _normalized_severity(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("diagnostic text must not be blank")
        return value.strip()

    @field_validator("code", "message")
    @classmethod
    def _required_text(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("diagnostic text must not be blank")
        return value


class CandidateSourceMapEntry(BaseModel):
    """One exact D-077 Python-source range."""

    model_config = ConfigDict(extra="forbid")

    workflow_node_uuid: str
    start_line: int = Field(ge=1, strict=True)
    start_column: int = Field(ge=1, strict=True)
    end_line: int = Field(ge=1, strict=True)
    end_column: int = Field(ge=1, strict=True)

    @field_validator("workflow_node_uuid")
    @classmethod
    def _valid_uuid(cls, value: str) -> str:
        return validate_uuid(value)

    @model_validator(mode="after")
    def _ordered_range(self) -> CandidateSourceMapEntry:
        if (self.end_line, self.end_column) < (
            self.start_line,
            self.start_column,
        ):
            raise ValueError("source range end precedes its start")
        return self


class CandidateChangeset(BaseModel):
    """The complete graph/source-only changeset frozen by D-077."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["graph", "source_only"]
    created_node_uuids: List[str]
    updated_node_uuids: List[str]
    deleted_node_uuids: List[str]
    created_edge_uuids: List[str]
    updated_edge_uuids: List[str]
    deleted_edge_uuids: List[str]
    reserved_metadata_changed: bool = Field(strict=True)

    @field_validator(
        "created_node_uuids",
        "updated_node_uuids",
        "deleted_node_uuids",
        "created_edge_uuids",
        "updated_edge_uuids",
        "deleted_edge_uuids",
        mode="before",
    )
    @classmethod
    def _uuid_array(cls, value: Any) -> List[str]:
        if not isinstance(value, list):
            raise ValueError("changeset UUID collection must be an array")
        result = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError("changeset UUID must be a string")
            result.append(validate_uuid(item))
        return result

    @model_validator(mode="after")
    def _source_only_has_no_graph_changes(self) -> CandidateChangeset:
        if self.kind == "source_only" and (
            self.created_node_uuids
            or self.updated_node_uuids
            or self.deleted_node_uuids
            or self.created_edge_uuids
            or self.updated_edge_uuids
            or self.deleted_edge_uuids
            or self.reserved_metadata_changed
        ):
            raise ValueError("source-only changeset must not contain graph changes")
        return self


__all__ = [
    "CandidateChangeset",
    "CandidateCompilation",
    "CandidateDiagnostic",
    "CandidateSourceMapEntry",
    "DiagnosticSourceRange",
    "JsonArray",
    "JsonObject",
    "WorkflowEdgeWrite",
    "WorkflowNodeWrite",
    "normalize_json_array",
    "normalize_json_object",
    "validate_json_value",
    "validate_uuid",
]
