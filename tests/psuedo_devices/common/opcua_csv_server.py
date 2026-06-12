#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import logging
import signal
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opcua import Server, ua


LOGGER = logging.getLogger("pseudo-opcua-csv-server")

DATA_TYPE_TO_VARIANT = {
    "BOOL": ua.VariantType.Boolean,
    "BOOLEAN": ua.VariantType.Boolean,
    "INT": ua.VariantType.Int16,
    "INT16": ua.VariantType.Int16,
    "DINT": ua.VariantType.Int32,
    "INT32": ua.VariantType.Int32,
    "REAL": ua.VariantType.Float,
    "FLOAT": ua.VariantType.Float,
    "STRING": ua.VariantType.String,
}


@dataclass(frozen=True)
class NodeDefinition:
    name: str
    variant_type: ua.VariantType
    initial_value: Any


class CsvOpcUaServer:
    def __init__(
        self,
        endpoint: str,
        csv_path: str | Path,
        object_name: str,
        namespace_uri: str,
        server_name: str,
        name_column: str,
        data_type_column: str,
        initial_value_column: str,
        initial_values: dict[str, Any],
    ) -> None:
        self.endpoint = endpoint
        self.csv_path = Path(csv_path)
        self.object_name = object_name
        self.initial_values = initial_values
        self.name_column = name_column
        self.data_type_column = data_type_column
        self.initial_value_column = initial_value_column

        self.server = Server()
        self.server.set_endpoint(endpoint)
        self.server.set_server_name(server_name)
        self.idx = self.server.register_namespace(namespace_uri)
        self.device = self.server.get_objects_node().add_object(self.idx, object_name)

        self.nodes: dict[str, Any] = {}
        self.variant_by_name: dict[str, ua.VariantType] = {}
        self._create_nodes(self._load_csv())

    def _load_csv(self) -> list[NodeDefinition]:
        definitions: list[NodeDefinition] = []
        with self.csv_path.open("r", encoding="utf-8-sig", newline="") as file_obj:
            reader = csv.DictReader(file_obj)
            for row in reader:
                name = (row.get(self.name_column) or "").strip()
                data_type = (row.get(self.data_type_column) or "").strip().upper()
                if not name or not data_type:
                    continue

                variant_type = DATA_TYPE_TO_VARIANT.get(data_type)
                if variant_type is None:
                    LOGGER.warning("跳过未知数据类型节点: %s (%s)", name, data_type)
                    continue

                raw_initial_value = self.initial_values.get(name, row.get(self.initial_value_column))
                definitions.append(
                    NodeDefinition(
                        name=name,
                        variant_type=variant_type,
                        initial_value=self._coerce_value(raw_initial_value, variant_type),
                    )
                )

        if not definitions:
            raise ValueError(f"CSV 中没有可用 OPC UA 节点: {self.csv_path}")
        return definitions

    def _create_nodes(self, definitions: list[NodeDefinition]) -> None:
        for definition in definitions:
            node = self.device.add_variable(self.idx, definition.name,
                                            definition.initial_value, definition.variant_type)
            node.set_writable()
            self.nodes[definition.name] = node
            self.variant_by_name[definition.name] = definition.variant_type
        LOGGER.info(
            "已从 %s 创建 %d 个 %s OPC UA 变量",
            self.csv_path,
            len(definitions),
            self.object_name,
        )

    def start(self) -> None:
        self.server.start()
        LOGGER.info("OPC UA 服务已启动: endpoint=%s object=%s", self.endpoint, self.object_name)

    def stop(self) -> None:
        self.server.stop()
        LOGGER.info("OPC UA 服务已停止: object=%s", self.object_name)

    @staticmethod
    def _coerce_value(value: Any, variant_type: ua.VariantType) -> Any:
        if isinstance(value, str):
            value = value.strip()
        if variant_type == ua.VariantType.Boolean:
            if isinstance(value, bool):
                return value
            return str(value or "").upper() in {"ON", "TRUE", "1", "YES"}
        if variant_type in (ua.VariantType.Int16, ua.VariantType.Int32):
            return int(float(value or 0))
        if variant_type == ua.VariantType.Float:
            return float(value or 0.0)
        return "" if value is None else str(value)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="启动 CSV 驱动的测试 OPC UA 服务")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--path", default="/")
    parser.add_argument("--csv", required=True)
    parser.add_argument("--object-name", required=True)
    parser.add_argument("--namespace-uri", default="http://unilabos.com/opcua/test/pseudo-device")
    parser.add_argument("--server-name", default="UniLabOS Test OPC UA Server")
    parser.add_argument("--name-column", default="变量名")
    parser.add_argument("--data-type-column", default="数据类型")
    parser.add_argument("--initial-value-column", default="初始值")
    parser.add_argument("--initial-values-json", default="{}")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s - %(levelname)s - %(message)s")
    logging.getLogger("opcua").setLevel(logging.WARNING)

    endpoint = f"opc.tcp://{args.host}:{args.port}{args.path}"
    server = CsvOpcUaServer(
        endpoint=endpoint,
        csv_path=args.csv,
        object_name=args.object_name,
        namespace_uri=args.namespace_uri,
        server_name=args.server_name,
        name_column=args.name_column,
        data_type_column=args.data_type_column,
        initial_value_column=args.initial_value_column,
        initial_values=json.loads(args.initial_values_json),
    )
    stop_event = threading.Event()

    def request_stop(signum, frame) -> None:
        del frame
        LOGGER.info("收到停止信号 %s，正在关闭 OPC UA 服务", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    server.start()
    try:
        while not stop_event.is_set():
            time.sleep(1)
    finally:
        server.stop()


if __name__ == "__main__":
    main()
