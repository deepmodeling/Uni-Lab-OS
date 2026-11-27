"""OS 设备观测服务：身份、描述、连续采样和查询；不负责作业执行。"""

from __future__ import annotations

import json
from typing import Any, Mapping

from unilabos.runtime.observations import Descriptor, Sample, Selector, StateError


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class DeviceObservationService:
    def __init__(self, context):
        self.context = context

    @property
    def session(self):
        return self.context.session

    @staticmethod
    def _select(
        nodes: Mapping[str, dict[str, Any]], selector: Selector
    ) -> dict[str, Any]:
        field = "id" if selector.by == "id" else "name"
        found = [node for node in nodes.values() if node[field] == selector.value]
        if len(found) != 1:
            raise StateError("identity_missing" if not found else "identity_ambiguous")
        return found[0]

    def resolve(self, selector: Selector) -> dict[str, Any]:
        with self.context.transaction() as connection:
            return self._select(self.context.native(connection), selector)

    def describe(self, selector: Selector, descriptor: Descriptor) -> dict[str, Any]:
        """由可信包发现路径登记描述，不创建或修改任何物料。"""
        with self.context.transaction() as connection:
            node = self._select(self.context.native(connection), selector)
            body = descriptor.model_dump_json()
            previous = self.context.db.execute(
                "SELECT body,generation FROM observation_descriptor WHERE material_uuid=?",
                (node["uuid"],),
            ).fetchone()
            if previous and previous["generation"] == node["generation"]:
                if previous["body"] == body:
                    return node
                # 有效描述不能在已绑定组中被原地替换。
                for row in self.context.db.execute(
                    "SELECT body FROM observation_source_group"
                ):
                    if node["uuid"] in json.loads(row[0])["members"]:
                        raise StateError("version_incompatible")
            self.context.db.execute(
                "INSERT INTO observation_descriptor(material_uuid,generation,body) VALUES (?,?,?) ON CONFLICT(material_uuid) DO UPDATE SET generation=excluded.generation,body=excluded.body",
                (node["uuid"], node["generation"], body),
            )
            self.context.descriptor_cache.clear()
            self.context.event("descriptor.changed", {"material_uuid": node["uuid"]})
            return node

    def _descriptor(self, node: dict[str, Any]) -> Descriptor:
        key = (
            node["uuid"],
            node["generation"],
            self.context.native_revision,
            self.context.inventory.catalog_fingerprint,
        )
        if key in self.context.descriptor_cache:
            descriptor = self.context.descriptor_cache[key]
            if descriptor is None:
                raise StateError("descriptor_missing")
            return descriptor
        row = self.context.db.execute(
            "SELECT generation,body FROM observation_descriptor WHERE material_uuid=?",
            (node["uuid"],),
        ).fetchone()
        metadata = self.context.inventory.template_metadata(
            node["resource_template_uuid"]
        )
        cached = (
            Descriptor.model_validate_json(row["body"])
            if row and row["generation"] == node["generation"]
            else None
        )
        declaration = metadata.get("observations", metadata.get("twin"))
        descriptor = (
            Descriptor.model_validate(declaration)
            if declaration is not None
            else cached
        )
        if descriptor is None:
            self.context.descriptor_cache[key] = None
            raise StateError("descriptor_missing")
        if descriptor != cached:
            # 活动组的契约不能在执行中被模板编辑静默替换。
            if cached and any(
                node["uuid"] in json.loads(group["body"])["members"]
                for group in self.context.db.execute(
                    "SELECT body FROM observation_source_group"
                )
            ):
                raise StateError("version_incompatible")
            self.context.db.execute(
                "INSERT INTO observation_descriptor(material_uuid,generation,body) VALUES (?,?,?) ON CONFLICT(material_uuid) DO UPDATE SET generation=excluded.generation,body=excluded.body",
                (node["uuid"], node["generation"], descriptor.model_dump_json()),
            )
            self.context.event("descriptor.changed", {"material_uuid": node["uuid"]})
        self.context.descriptor_cache[key] = descriptor
        return descriptor

    def publish(self, sample: Sample) -> dict[str, Any]:
        """可信 provider 提交一个分量；显示端不能调用目标写入。"""
        return self.publish_batch([sample])[0]

    def publish_batch(self, samples: list[Sample]) -> list[dict[str, Any]]:
        """一个采样批次原子应用；任一分量无效时整批回滚，完整记录不降采样。"""
        if not samples or len(samples) > 4096:
            raise StateError("invalid_request")
        from unilabos.simulation import mcap_recorder

        recording = mcap_recorder.current() is not None
        with self.context.transaction() as connection:
            nodes = self.context.native(connection)
            results = [self._publish(sample, nodes) for sample in samples]
            descriptors = ({uuid: self._descriptor(nodes[uuid]) for uuid in {s.material_uuid for s in samples}}
                           if recording else {})
        # 提交后再写高频录制（MCAP）；没有进行中的录制时不做任何事。
        if recording:
            accepted = [sample for sample, result in zip(samples, results) if result.get("accepted")]
            mcap_recorder.tap(accepted, nodes, lambda node: descriptors.get(node["uuid"]))
        return results

    def _publish(self, sample: Sample, nodes: dict[str, Any]) -> dict[str, Any]:
        node = nodes.get(sample.material_uuid)
        if not node or node["generation"] != sample.generation:
            raise StateError("identity_changed")
        descriptor = self._descriptor(node)
        if sample.descriptor_version != descriptor.version:
            raise StateError("version_incompatible")
        prop = descriptor.properties.get(sample.property)
        if prop is None:
            raise StateError("property_missing")
        group = self.context.group(sample.group)
        if (
            sample.runtime_session != self.context.session
            or sample.runtime_session != group["runtime_session"]
            or sample.epoch != group["epoch"]
            or sample.source_session != group["source_session"]
            or sample.provider != group["provider"]
        ):
            raise StateError("stale_source")
        if group["members"].get(sample.material_uuid) != sample.generation:
            raise StateError("identity_changed")
        if not group["ready"] or group["recovery_required"]:
            raise StateError("group_not_ready")
        value = sample.value
        if value is not None:
            valid = {
                "number": type(value) in (float, int),
                "integer": type(value) is int,
                "boolean": type(value) is bool,
                "string": type(value) is str,
                "matrix4": isinstance(value, list)
                and len(value) == 16
                and all(type(x) in (float, int) for x in value),
            }[prop.dtype]
            if not valid:
                raise StateError("invalid_value")
            if prop.dtype == "matrix4" and any(
                abs(a - b) > 1e-7 for a, b in zip(value[12:], (0, 0, 0, 1))
            ):
                raise StateError("invalid_transform")
        outside = (
            isinstance(value, (int, float))
            and prop.dtype in ("number", "integer")
            and (
                (prop.lower is not None and value < prop.lower)
                or (prop.upper is not None and value > prop.upper)
            )
        )
        if outside and prop.semantic != "observed":
            raise StateError("target_out_of_range")
        previous = self.context.db.execute(
            "SELECT body FROM device_observation_latest WHERE material_uuid=? AND property=?",
            (sample.material_uuid, sample.property),
        ).fetchone()
        if previous:
            prior = json.loads(previous[0])["sample"]
            same_source = all(
                prior[key] == getattr(sample, key)
                for key in ("runtime_session", "source_session", "epoch", "generation")
            )
            if same_source and sample.seq <= prior["seq"]:
                if sample.model_dump() == prior:
                    return {"accepted": False, "duplicate": True}
                raise StateError("sample_out_of_order")
            if same_source and (
                sample.clock != prior["clock"]
                or sample.sampled_at < prior["sampled_at"]
            ):
                raise StateError("sample_time_invalid")
        record = {"sample": sample.model_dump(), "out_of_range": bool(outside)}
        now = self.context.clock()
        self.context.db.execute(
            "INSERT INTO device_observation_latest(material_uuid,property,body,received_at) VALUES (?,?,?,?) ON CONFLICT(material_uuid,property) DO UPDATE SET body=excluded.body,received_at=excluded.received_at",
            (sample.material_uuid, sample.property, _json(record), now),
        )
        cursor = self.context.event("property.sample", {**record, "received_at": now})
        # 回放发出的样本只供显示，不进录制，避免把回放当成一次新的运行。
        if sample.provider != "replay":
            self.context.db.execute(
                "INSERT INTO device_observation_record(body) VALUES (?)",
                (_json({**record, "received_at": now}),),
            )
        return {
            "accepted": True,
            "duplicate": False,
            "cursor": cursor,
            "out_of_range": bool(outside),
        }

    def records(self, after: int = 0, limit: int = 1000) -> dict[str, Any]:
        """读取完整已接收采样；独立于可裁剪的显示增量窗口。"""
        if (
            type(after) is not int
            or after < 0
            or type(limit) is not int
            or not 1 <= limit <= 1000
        ):
            raise StateError("invalid_request")
        with self.context.transaction():
            rows = self.context.db.execute(
                "SELECT id,body FROM device_observation_record WHERE id>? ORDER BY id LIMIT ?",
                (after, limit),
            ).fetchall()
            return {
                "records": [
                    {"id": row["id"], **json.loads(row["body"])} for row in rows
                ],
                "cursor": rows[-1]["id"] if rows else after,
            }

    def snapshot(self) -> dict[str, Any]:
        with self.context.transaction() as connection:
            nodes = self.context.native(connection)
            now = self.context.clock()
            result = []
            groups = {
                row["name"]: json.loads(row["body"])
                for row in self.context.db.execute(
                    "SELECT * FROM observation_source_group"
                )
            }
            for node in nodes.values():
                try:
                    descriptor = self._descriptor(node)
                except StateError as error:
                    if error.reason != "descriptor_missing":
                        raise
                    result.append({**node, "descriptor": None, "properties": {}})
                    continue
                properties = {}
                for name, prop in descriptor.properties.items():
                    row = self.context.db.execute(
                        "SELECT body,received_at FROM device_observation_latest WHERE material_uuid=? AND property=?",
                        (node["uuid"], name),
                    ).fetchone()
                    record = json.loads(row["body"]) if row else None
                    sample = record["sample"] if record else None
                    group = groups.get(sample["group"], {}) if sample else {}
                    current = (
                        sample
                        and sample["generation"] == node["generation"]
                        and sample["descriptor_version"] == descriptor.version
                        and all(
                            sample[key] == group.get(key)
                            for key in ("epoch", "source_session", "runtime_session")
                        )
                    )
                    if not current:
                        properties[name] = {"value": None, "quality": "unknown"}
                    else:
                        assert sample is not None and record is not None
                        quality = sample["quality"]
                        if quality == "good" and (
                            now - row["received_at"] > prop.ttl_seconds
                            or not group.get("ready")
                        ):
                            quality = "stale"
                        properties[name] = {
                            **record,
                            "value": sample["value"],
                            "quality": quality,
                            "received_at": row["received_at"],
                        }
                result.append(
                    {
                        **node,
                        "descriptor": descriptor.model_dump(),
                        "properties": properties,
                    }
                )
            cursor = self.context.db.execute(
                "SELECT COALESCE(MAX(cursor),0) FROM device_observation_event"
            ).fetchone()[0]
            return {
                "schema_version": 1,
                "runtime_session": self.context.session,
                "cursor": cursor,
                "graph_version": self.context.db.execute(
                    "SELECT value FROM device_observation_meta WHERE key='graph'"
                ).fetchone()[0],
                "nodes": result,
                "groups": groups,
            }

    def changes(
        self, runtime_session: str, after: int, limit: int = 1000
    ) -> dict[str, Any]:
        if type(after) is not int or after < 0 or not 1 <= limit <= 1000:
            raise StateError("invalid_request")
        with self.context.transaction() as connection:
            self.context.native(connection)
            first, last = self.context.db.execute(
                "SELECT MIN(cursor),MAX(cursor) FROM device_observation_event"
            ).fetchone()
            if (
                runtime_session != self.context.session
                or after < first - 1
                or after > last
            ):
                raise StateError("resync_required")
            rows = self.context.db.execute(
                "SELECT * FROM device_observation_event WHERE cursor>? ORDER BY cursor LIMIT ?",
                (after, limit),
            ).fetchall()
            return {
                "events": [
                    {
                        "cursor": row["cursor"],
                        "kind": row["kind"],
                        "data": json.loads(row["body"]),
                    }
                    for row in rows
                ],
                "cursor": rows[-1]["cursor"] if rows else after,
                "has_more": bool(rows and rows[-1]["cursor"] < last),
            }
