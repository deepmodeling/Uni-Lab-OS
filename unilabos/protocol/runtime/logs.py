"""进程日志只读协议；游标由读取端独立持有，不消耗日志。"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class RuntimeLogSource(BaseModel):
    source_id: str
    name: str
    role: Literal["host", "slave"]
    machine_name: str
    node_id: str = ""
    pid: int | None = None
    device_ids: list[str] = Field(default_factory=list)
    online: bool
    managed: bool = False
    supported: bool = True
    detail: str = ""


class RuntimeLogSources(BaseModel):
    sources: list[RuntimeLogSource]


class RuntimeLogLine(BaseModel):
    # 当前日志文件内的字节位置；不是时间戳，也不是可跨文件比较的顺序。
    offset: int
    text: str


class RuntimeLogBatch(BaseModel):
    source_id: str
    stream_id: str
    cursor: str
    lines: list[RuntimeLogLine]
    has_more: bool = False
    reset: bool = False
    truncated: bool = False
    path: str = ""
    pid: int | None = None


class RuntimeLogQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cursor: str = Field(default="", max_length=96, pattern=r"^(?:[0-9a-f]{24}:[0-9]{1,16})?$")
    limit: int = Field(default=300, ge=1, le=1000, strict=True)


class RuntimeLogNotice(BaseModel):
    """瞬时失效通知，无日志正文、不写数据库；重连用 HTTP 游标校准。"""

    model_config = ConfigDict(extra="forbid")
    source_ids: list[Annotated[str, Field(min_length=1, max_length=512)]] = Field(default_factory=list, max_length=256)
    sources_changed: bool = False
    all_sources: bool = False
