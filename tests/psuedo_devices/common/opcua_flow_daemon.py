#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import signal
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from opcua import Client


LOGGER = logging.getLogger("pseudo-opcua-flow-daemon")


def describe_flow(flow_path: str | Path) -> None:
    flow = json.loads(Path(flow_path).read_text(encoding="utf-8"))
    print(f"Flow: {flow.get('name', '<unnamed>')}")
    for rule_index, rule in enumerate(flow.get("rules", []), 1):
        trigger = rule.get("trigger", {})
        print(f"Rule {rule_index}: {rule.get('name', '<unnamed>')}")
        print(
            "  Trigger: {node} == {value!r} on {edge} edge".format(
                node=trigger.get("node"),
                value=trigger.get("value", True),
                edge=trigger.get("edge", "rising"),
            )
        )
        log_nodes = rule.get("log_nodes", [])
        if log_nodes:
            print(f"  Observe: {', '.join(log_nodes)}")
        print("  Actions:")
        for action_index, action in enumerate(rule.get("actions", []), 1):
            condition = action.get("when")
            condition_text = f" when {condition}" if condition else ""
            if "write" in action:
                write = action["write"]
                print(f"    {action_index}. write {write.get('node')} = {write.get('value')!r}{condition_text}")
            elif "sleep" in action:
                print(f"    {action_index}. sleep {action['sleep']}s{condition_text}")
            else:
                print(f"    {action_index}. unsupported action: {action}")


def browse_object_nodes(url: str, object_name: str) -> tuple[Client, dict[str, Any]]:
    client = Client(url)
    client.connect()
    objects = client.get_objects_node()
    for child in objects.get_children():
        if child.get_browse_name().Name == object_name:
            nodes = {node.get_browse_name().Name: node for node in child.get_children()}
            return client, nodes
    client.disconnect()
    raise RuntimeError(f"OPC UA 中未找到对象: {object_name}")


def connect_with_retry(url: str, object_name: str, timeout: float, interval: float) -> tuple[Client, dict[str, Any]]:
    started_at = time.time()
    last_error = ""
    while time.time() - started_at < timeout:
        try:
            client, nodes = browse_object_nodes(url, object_name)
            LOGGER.info("daemon 已连接 OPC UA: url=%s object=%s variables=%s", url, object_name, sorted(nodes))
            return client, nodes
        except Exception as exc:
            last_error = str(exc)
            LOGGER.info("等待 OPC UA: url=%s object=%s error=%s", url, object_name, last_error)
            time.sleep(interval)
    raise TimeoutError(f"等待 OPC UA 对象超时: {last_error}")


class FlowDaemon:
    def __init__(
        self,
        url: str,
        object_name: str,
        flow_path: str | Path,
        poll_interval: float,
        stop_requested,
    ) -> None:
        self.url = url
        self.object_name = object_name
        self.flow_path = Path(flow_path)
        self.poll_interval = poll_interval
        self.stop_requested = stop_requested
        self.flow = json.loads(self.flow_path.read_text(encoding="utf-8"))
        self.previous_values: dict[str, Any] = {}

    def run(self) -> None:
        client, nodes = connect_with_retry(url=self.url, object_name=self.object_name, timeout=20.0, interval=0.5)
        try:
            rules = self.flow.get("rules", [])
            self._validate_rules(rules, nodes)
            for rule_index, rule in enumerate(rules):
                trigger_node = rule["trigger"]["node"]
                self.previous_values[self._rule_key(rule_index, rule)] = nodes[trigger_node].get_value()
            LOGGER.info("daemon 启动监听: flow=%s rules=%s", self.flow_path, [rule.get("name") for rule in rules])

            while not self.stop_requested():
                for rule_index, rule in enumerate(rules):
                    self._run_rule_if_triggered(rule_index, rule, nodes)
                time.sleep(self.poll_interval)
        finally:
            with suppress(Exception):
                client.disconnect()
            LOGGER.info("daemon 已断开 OPC UA 连接")

    def _validate_rules(self, rules: list[dict[str, Any]], nodes: dict[str, Any]) -> None:
        missing: set[str] = set()
        for rule in rules:
            missing.update(self._referenced_nodes(rule) - set(nodes))
        if missing:
            raise RuntimeError(f"flow 引用了不存在的 OPC UA 节点: {sorted(missing)}")

    @staticmethod
    def _referenced_nodes(rule: dict[str, Any]) -> set[str]:
        node_names = {rule["trigger"]["node"]}
        for node_name in rule.get("log_nodes", []):
            node_names.add(node_name)
        for action in rule.get("actions", []):
            if "write" in action:
                node_names.add(action["write"]["node"])
            node_names.update(FlowDaemon._condition_nodes(action.get("when")))
        return node_names

    def _run_rule_if_triggered(self, rule_index: int, rule: dict[str, Any], nodes: dict[str, Any]) -> None:
        trigger = rule["trigger"]
        trigger_node = trigger["node"]
        expected = trigger.get("value", True)
        edge = trigger.get("edge", "rising")
        rule_key = self._rule_key(rule_index, rule)

        try:
            current = nodes[trigger_node].get_value()
        except Exception as exc:
            if self.stop_requested():
                return
            LOGGER.warning("读取 trigger 失败，跳过本轮: node=%s error=%s", trigger_node, exc)
            return

        previous = self.previous_values.get(rule_key)
        self.previous_values[rule_key] = current
        if not self._triggered(current=current, previous=previous, expected=expected, edge=edge):
            return

        log_values = {node_name: nodes[node_name].get_value() for node_name in rule.get("log_nodes", [])}
        LOGGER.info("触发 flow 规则: name=%s trigger=%s values=%s", rule.get("name"), trigger_node, log_values)

        for action in rule.get("actions", []):
            if not self._conditions_match(action.get("when"), nodes):
                LOGGER.info("跳过未满足条件的 action: when=%s action=%s", action.get("when"), action)
                continue
            if "sleep" in action:
                time.sleep(float(action["sleep"]))
                continue
            if "write" in action:
                write = action["write"]
                nodes[write["node"]].set_value(write.get("value"))
                LOGGER.info("写入 OPC UA 变量: node=%s value=%s", write["node"], write.get("value"))

    @staticmethod
    def _condition_nodes(condition: Any) -> set[str]:
        if not condition:
            return set()
        if isinstance(condition, dict):
            return {str(condition["node"])} if "node" in condition else set()
        if isinstance(condition, list):
            return {
                str(item["node"])
                for item in condition
                if isinstance(item, dict) and "node" in item
            }
        raise ValueError(f"不支持的 when 条件: {condition}")

    @classmethod
    def _conditions_match(cls, condition: Any, nodes: dict[str, Any]) -> bool:
        if not condition:
            return True
        conditions = condition if isinstance(condition, list) else [condition]
        for item in conditions:
            if not isinstance(item, dict) or "node" not in item:
                raise ValueError(f"不支持的 when 条件: {condition}")
            node_name = item["node"]
            expected = item.get("value", True)
            if nodes[node_name].get_value() != expected:
                return False
        return True

    @staticmethod
    def _rule_key(rule_index: int, rule: dict[str, Any]) -> str:
        return f"{rule_index}:{rule.get('name', '')}:{rule['trigger']['node']}"

    @staticmethod
    def _triggered(current: Any, previous: Any, expected: Any, edge: str) -> bool:
        current_matches = current == expected
        previous_matches = previous == expected
        if edge == "rising":
            return current_matches and not previous_matches
        if edge == "falling":
            return (not current_matches) and previous_matches
        if edge == "level":
            return current_matches
        raise ValueError(f"不支持的 trigger edge: {edge}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="启动 JSON flow 驱动的测试 OPC UA 守护进程")
    parser.add_argument("--url")
    parser.add_argument("--object-name")
    parser.add_argument("--flow", required=True)
    parser.add_argument("--describe-only", action="store_true")
    parser.add_argument("--poll-interval", type=float, default=0.02)
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s - %(levelname)s - %(message)s")
    logging.getLogger("opcua").setLevel(logging.WARNING)

    if args.describe_only:
        describe_flow(args.flow)
        return
    if not args.url or not args.object_name:
        raise SystemExit("--url and --object-name are required unless --describe-only is used")

    stopped = False

    def request_stop(signum, frame) -> None:
        nonlocal stopped
        del frame
        LOGGER.info("收到停止信号 %s，正在关闭 daemon", signum)
        stopped = True

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    FlowDaemon(
        url=args.url,
        object_name=args.object_name,
        flow_path=args.flow,
        poll_interval=args.poll_interval,
        stop_requested=lambda: stopped,
    ).run()


if __name__ == "__main__":
    main()
