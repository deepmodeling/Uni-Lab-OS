"""受信模拟 worker 到原生 OS 的显式租约与观察上报；不重发未知结果。"""

import json
from urllib.request import Request, urlopen

from unilabos.runtime.observations import Sample


class ObservationPublisher:
    def __init__(self, url, *, versions, operation_id, source_session, api_key=None):
        from unilabos.config.config import EdgeControlConfig
        self.api_key = api_key or EdgeControlConfig.api_key
        if not self.api_key:
            raise ValueError("workspace_api_key_required")
        self.url = url.rstrip("/")
        connected = self.call(
            "/device-state/observations/provider/connect",
            {
                "operation_id": operation_id,
                "source_session": source_session,
                "versions": versions,
                "mode": "simulation",
            },
        )
        operation = connected["operation"]
        self.nodes = connected["nodes"]
        self.group = operation["group"]
        self.provider = operation["provider"]
        self.lease = {key: operation[key] for key in ("runtime_session", "source_session", "epoch")}
        self.sequence = 0

    def call(self, route, body):
        request = Request(
            self.url + route,
            data=json.dumps(body, allow_nan=False).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.api_key},
        )
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
        if result.get("code") != 0:
            raise RuntimeError("twin_provider_rejected: " + str(result))
        return result["data"]

    def ready(self):
        return self.call("/device-state/observations/provider/ready", self.lease)

    def publish(self, sampled_at, values, *, sample_group, identity, request_id):
        samples = []
        for identifier, properties in values.items():
            node = self.nodes[identifier]
            samples.extend(
                Sample(
                    material_uuid=node["uuid"],
                    generation=node["generation"],
                    descriptor_version=node["descriptor"]["version"],
                    group=self.group,
                    provider=self.provider,
                    **self.lease,
                    property=name,
                    seq=self.sequence,
                    sampled_at=sampled_at,
                    clock="simulation",
                    quality="unknown" if value is None else "good",
                    value=value,
                    sample_group=sample_group,
                    request_id=request_id,
                    task_id=identity.get("task_uuid"),
                    job_id=identity.get("node_job_uuid"),
                ).model_dump()
                for name, value in properties.items()
            )
        self.call("/device-state/observations/provider/samples", {**self.lease, "samples": samples})
        self.sequence += 1

    def settle(self, job_id, receipt_id):
        return self.call(
            "/device-state/observations/provider/settle",
            {**self.lease, "job_id": job_id, "receipt_id": receipt_id, "quiescent": True},
        )

    def close(self):
        return self.call("/device-state/observations/provider/disconnect", self.lease)
