"""高频轨迹的 MCAP 文件：二进制帧，一个时刻一条消息。

在数据库之外保存原始节拍的关节、位姿等高频流，可与回放记录（``unilab.replay-bundle/1``）互转，
Foxglove 等通用工具可以直接打开：

* 帧通道 ``/observations/frame``：消息编码 ``protobuf``，类型
  ``unilab.ObservedValues {string event = 1; repeated float values = 2;}``（float32 定长数组）。
  通道 metadata ``layout`` 给出数组布局 ``[[节点, 属性, 宽度, dtype], ...]``（标量宽 1、matrix4 宽 16），
  ``descriptors`` 给出各节点的观测描述；本帧没有更新的分量填 NaN（压缩后几乎不占空间）。
  录制中出现新节点或新属性时，以扩展后的布局另注册一个同名通道。
* 状态通道 ``/observations/state``：字符串、布尔或空值，``json`` 编码
  ``{"event", "values": {节点: {属性: 值}}}``，低频。
* 文件级 metadata ``unilab.replay``：回放表头里除 devices/descriptors 外的字段（commands 等）。
* 消息时间为墙钟纳秒；读回时以第一条消息为 0，并在表头写 ``origin_ms``，便于与数据库记录对齐。

实测（环化 180 秒、10 Hz、每帧 226 个数）：JSON 编码 216 KB，按节点分通道的二进制 182 KB，
帧通道二进制约 72 KB。也能读旧版（每节点一个 json 通道）的文件。
"""

from __future__ import annotations

import base64
import json
import math
import struct
import time
from pathlib import Path
from typing import Any

CHANNEL_PREFIX = "/observations/"
STATE_SUFFIX = "/state"
METADATA_NAME = "unilab.replay"
PROTO_TYPE = "unilab.ObservedValues"
# FileDescriptorSet of: syntax="proto3"; package unilab; message ObservedValues {string event=1; repeated float values=2;}
PROTO_SCHEMA = base64.b64decode(
    "Cl8KHHVuaWxhYi9vYnNlcnZlZF92YWx1ZXMucHJvdG8SBnVuaWxhYiIvCg5PYnNlcnZlZFZhbHVlcxINCgVldmVudBgBIAEoCRIOCgZ2YWx1ZXMYAiADKAJiBnByb3RvMw=="
)
STATE_SCHEMA = {"type": "object", "properties": {"event": {"type": "string"}, "values": {"type": "object"}},
                "required": ["values"]}
# 兼容旧名
MESSAGE_SCHEMA = STATE_SCHEMA


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        bits = value & 0x7F
        value >>= 7
        out.append(bits | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _read_varint(data: bytes, index: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        byte = data[index]
        index += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, index
        shift += 7


def encode_values(event: str, values: list[float]) -> bytes:
    text = (event or "").encode("utf-8")
    packed = struct.pack(f"<{len(values)}f", *values)
    return (b"\x0a" + _varint(len(text)) + text if text else b"") + b"\x12" + _varint(len(packed)) + packed


def decode_values(data: bytes) -> tuple[str, list[float]]:
    event, values, index = "", [], 0
    while index < len(data):
        key, index = _read_varint(data, index)
        field, wire = key >> 3, key & 7
        if wire == 2:
            length, index = _read_varint(data, index)
            chunk = data[index:index + length]
            index += length
            if field == 1:
                event = chunk.decode("utf-8")
            elif field == 2:
                values.extend(struct.unpack(f"<{length // 4}f", chunk))
        elif wire == 5:  # 未打包的单个 float
            if field == 2:
                values.append(struct.unpack("<f", data[index:index + 4])[0])
            index += 4
        elif wire == 0:
            _, index = _read_varint(data, index)
        else:
            raise ValueError(f"不支持的 protobuf 线型 {wire}")
    return event, values


def _f32(value: float) -> float:
    return float(f"{value:.7g}")


def is_numeric(spec: dict[str, Any] | None, value: Any = None) -> bool:
    dtype = (spec or {}).get("dtype")
    if dtype is not None:
        return dtype in ("number", "integer", "matrix4")
    if isinstance(value, bool):
        return False
    return isinstance(value, (int, float)) or (isinstance(value, list) and len(value) == 16)


def layout_of(descriptor: dict[str, Any] | None, names: list[str], sample: dict[str, Any] | None = None) -> list[list]:
    """数值通道的数组布局：按属性名排序，matrix4 宽 16。"""
    properties = (descriptor or {}).get("properties") or {}
    layout = []
    for name in sorted(names):
        spec = properties.get(name)
        value = (sample or {}).get(name)
        if not is_numeric(spec, value):
            continue
        dtype = (spec or {}).get("dtype") or ("matrix4" if isinstance(value, list) else "number")
        layout.append([name, 16 if dtype == "matrix4" else 1, dtype])
    return layout


FRAME_TOPIC = CHANNEL_PREFIX + "frame"
STATE_TOPIC = CHANNEL_PREFIX + "state"


class FrameChannels:
    """整份录制共用的帧通道（数值）与状态通道（非数值）。"""

    def __init__(self, writer, schema_ids: tuple[int, int]):
        self.writer, self.schemas = writer, schema_ids
        self.layout: list[list] = []          # [[node, prop, width, dtype], ...]
        self.index: dict[tuple[str, str], int] = {}
        self.width = 0
        self.descriptors: dict[str, Any] = {}
        self.names: dict[str, list[str]] = {}
        self.frame_channel: int | None = None
        self.state_channel: int | None = None

    def ensure(self, node: str, descriptor: dict[str, Any] | None, names: list[str],
               sample: dict[str, Any] | None = None) -> None:
        known = self.names.setdefault(node, [])
        known.extend(name for name in names if name not in known)
        if descriptor and node not in self.descriptors:
            self.descriptors[node] = descriptor
        added = False
        for name, width, dtype in layout_of(descriptor, names, sample):
            if (node, name) not in self.index:
                self.index[(node, name)] = self.width
                self.layout.append([node, name, width, dtype])
                self.width += width
                added = True
        if added:
            self._dirty = True

    def _channel(self) -> int:
        # 布局变化后在下一次写入时才注册新通道，避免一次性登记多个节点时产生一串中间通道。
        if self.frame_channel is None or getattr(self, "_dirty", False):
            self._dirty = False
            self.frame_channel = self.writer.register_channel(
                topic=FRAME_TOPIC, message_encoding="protobuf", schema_id=self.schemas[0],
                metadata={"layout": json.dumps(self.layout, ensure_ascii=False),
                          "descriptors": json.dumps(self.descriptors, ensure_ascii=False),
                          "properties": json.dumps(self.names, ensure_ascii=False)})
        return self.frame_channel

    def write(self, stamp: int, sequence: int, event: str, values: dict[str, dict[str, Any]]) -> int:
        """一个时刻多个节点的值；先对每个节点调用过 ``ensure``。"""
        flat = None
        rest: dict[str, dict[str, Any]] = {}
        for node, properties in values.items():
            for name, value in properties.items():
                offset = self.index.get((node, name))
                if offset is None or value is None:
                    rest.setdefault(node, {})[name] = value
                    continue
                if flat is None:
                    flat = [math.nan] * self.width
                if isinstance(value, list):
                    flat[offset:offset + 16] = [float(item) for item in value]
                else:
                    flat[offset] = float(value)
        written = 0
        if flat is not None:
            self.writer.add_message(channel_id=self._channel(), log_time=stamp, publish_time=stamp,
                                    sequence=sequence, data=encode_values(event, flat))
            written += 1
        if rest:
            if self.state_channel is None:
                self.state_channel = self.writer.register_channel(
                    topic=STATE_TOPIC, message_encoding="json", schema_id=self.schemas[1], metadata={})
            self.writer.add_message(channel_id=self.state_channel, log_time=stamp, publish_time=stamp,
                                    sequence=sequence, data=json.dumps({"event": event, "values": rest},
                                                                       ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            written += 1
        return written


def open_writer(stream):
    from mcap.writer import CompressionType, Writer

    writer = Writer(stream, compression=CompressionType.ZSTD)
    writer.start(profile="", library="unilabos")
    values_schema = writer.register_schema(name=PROTO_TYPE, encoding="protobuf", data=PROTO_SCHEMA)
    state_schema = writer.register_schema(name="unilab.ObservedState", encoding="jsonschema",
                                          data=json.dumps(STATE_SCHEMA).encode("utf-8"))
    return writer, (values_schema, state_schema)


def write_mcap(header: dict[str, Any], rows: list[dict[str, Any]], path: Path, *, start_ns: int | None = None,
               nodes: set[str] | None = None) -> int:
    """把回放记录（或其中 ``nodes`` 这些节点）写成 MCAP；返回消息数。"""
    start_ns = start_ns if start_ns is not None else time.time_ns()
    selected = [node for node in header["devices"] if nodes is None or node in nodes]
    first: dict[str, dict[str, Any]] = {}
    for row in rows:
        for node, values in (row.get("values") or {}).items():
            for name, value in values.items():
                first.setdefault(node, {}).setdefault(name, value)
    count = 0
    with Path(path).open("wb") as stream:
        writer, schemas = open_writer(stream)
        frames = FrameChannels(writer, schemas)
        for node in selected:
            frames.ensure(node, (header.get("descriptors") or {}).get(node), header["devices"][node], first.get(node))
        extra = {key: value for key, value in header.items() if key not in ("devices", "descriptors", "origin_ms")}
        writer.add_metadata(METADATA_NAME, {"header": json.dumps(extra, ensure_ascii=False)})
        for sequence, row in enumerate(rows):
            values = {node: properties for node, properties in (row.get("values") or {}).items() if node in selected}
            if values:
                stamp = start_ns + int(round(float(row["t"]) * 1e9))
                count += frames.write(stamp, sequence, row.get("event") or "", values)
        writer.finish()
    return count


def read_mcap(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """读回成回放记录（表头 + 按时间排好的行）；数值转回 Python 数，NaN 表示该条未更新。"""
    from mcap.reader import make_reader

    with Path(path).open("rb") as stream:
        reader = make_reader(stream)
        header: dict[str, Any] = {"devices": {}, "descriptors": {}}
        for record in reader.iter_metadata():
            if record.name == METADATA_NAME:
                header.update(json.loads(record.metadata.get("header", "{}")))
        layouts: dict[int, list] = {}
        summary = reader.get_summary()
        for channel in (summary.channels.values() if summary else []):
            if not channel.topic.startswith(CHANNEL_PREFIX):
                continue
            if channel.topic == FRAME_TOPIC:
                layouts[channel.id] = json.loads(channel.metadata.get("layout", "[]"))
                for node, names in json.loads(channel.metadata.get("properties", "{}")).items():
                    merged = header["devices"].setdefault(node, [])
                    merged.extend(name for name in names if name not in merged)
                header["descriptors"].update(json.loads(channel.metadata.get("descriptors", "{}")))
                continue
            if channel.topic == STATE_TOPIC:
                continue
            node = channel.metadata.get("node") or channel.topic[len(CHANNEL_PREFIX):]   # 旧版：每节点一个 json 通道
            if channel.metadata.get("properties"):
                names = header["devices"].setdefault(node, [])
                names.extend(name for name in json.loads(channel.metadata["properties"]) if name not in names)
            if channel.metadata.get("descriptor"):
                header["descriptors"][node] = json.loads(channel.metadata["descriptor"])
        rows: list[dict[str, Any]] = []
        origin = None
        for _, channel, message in reader.iter_messages(log_time_order=True):
            if not channel.topic.startswith(CHANNEL_PREFIX):
                continue
            if channel.id in layouts:
                event, flat = decode_values(message.data)
                values: dict[str, dict[str, Any]] = {}
                index = 0
                for node, name, width, dtype in layouts[channel.id]:
                    chunk = flat[index:index + width]
                    index += width
                    if not chunk or math.isnan(chunk[0]):
                        continue
                    # float32 只有约 7 位有效数字；按 7 位还原，避免 0.1 读成 0.10000000149。
                    if width == 16:
                        value = [_f32(item) for item in chunk]
                    elif dtype == "integer":
                        value = int(round(chunk[0]))
                    else:
                        value = _f32(chunk[0])
                    values.setdefault(node, {})[name] = value
            elif channel.topic == STATE_TOPIC:
                body = json.loads(message.data)
                event, values = body.get("event") or "", body.get("values") or {}
            else:
                body = json.loads(message.data)
                node = channel.metadata.get("node") or channel.topic[len(CHANNEL_PREFIX):]
                event, values = body.get("event") or "", {node: body.get("values") or {}}
            if not values:
                continue
            origin = message.log_time if origin is None else origin
            t = round((message.log_time - origin) / 1e9, 6)
            event = event or "idle"
            if not rows or rows[-1]["t"] != t or rows[-1]["event"] != event:
                rows.append({"t": t, "event": event, "values": {}})
            for node, properties in values.items():
                rows[-1]["values"].setdefault(node, {}).update(properties)
    if origin is not None:
        header["origin_ms"] = origin / 1e6
    if not header["descriptors"]:
        header.pop("descriptors")
    return header, rows


def merge_records(*records: tuple[dict[str, Any], list[dict[str, Any]]]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """合并多份记录：节点/属性取并集，行按时间归并；表头其它字段先到先得。

    每份都带 ``origin_ms``（该记录 t=0 对应的墙钟毫秒）时按墙钟对齐，否则视为同一时间原点。
    """
    header: dict[str, Any] = {"devices": {}, "descriptors": {}}
    rows: list[dict[str, Any]] = []
    origins = [part_header.get("origin_ms") for part_header, _ in records]
    aligned = all(origin is not None for origin in origins)
    base = min(origins) if aligned else None
    for index, (part_header, part_rows) in enumerate(records):
        shift = (origins[index] - base) / 1000.0 if aligned else 0.0
        if shift:
            part_rows = [{**row, "t": round(float(row["t"]) + shift, 6)} for row in part_rows]
        for key, value in part_header.items():
            if key in ("devices", "descriptors", "origin_ms"):
                continue
            if key == "commands" and shift:
                value = [{**span, "t_start": round(float(span["t_start"]) + shift, 6),
                          "t_end": round(float(span["t_end"]) + shift, 6)} for span in value]
            header.setdefault(key, value)
        for node, names in part_header["devices"].items():
            merged = header["devices"].setdefault(node, [])
            merged.extend(name for name in names if name not in merged)
        for node, body in (part_header.get("descriptors") or {}).items():
            current = header["descriptors"].setdefault(node, {"version": body.get("version"), "properties": {}})
            current["properties"].update(body.get("properties") or {})
            if body.get("joints"):
                current.setdefault("joints", {}).update(body["joints"])
        rows.extend(part_rows)
    rows.sort(key=lambda row: float(row["t"]))
    if aligned:
        header["origin_ms"] = base
    if not header["descriptors"]:
        header.pop("descriptors")
    return header, rows
