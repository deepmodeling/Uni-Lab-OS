"""公开 protocol → MCP 工具/资源。业务执行全部回到当前微后端 ASGI 路由。"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from dataclasses import asdict
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx
from fastapi import FastAPI
from jsonschema import Draft202012Validator
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents

from .catalog import OPERATIONS, Operation
from .guide import GUIDE, INSTRUCTIONS
from .schema import input_schema, normalize_body


def _object(properties: dict, required: tuple[str, ...] = ()) -> dict:
    value = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        value["required"] = list(required)
    return value


def _result(value: dict, *, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(value, ensure_ascii=False))],
        structuredContent=value,
        isError=error,
    )


class ProtocolMCP:
    """一个微后端一份目录；缓存有界且只在本进程内保留十分钟。"""

    INLINE_CHARS = 40_000
    CACHE_CHARS = 16_000_000
    CACHE_ENTRIES = 32
    CACHE_TTL = 600.0

    def __init__(self, app: FastAPI, document: dict[str, Any]) -> None:
        self.app = app
        self.document = document
        self.operations = {operation.tool_name: operation for operation in OPERATIONS}
        self.server = Server("unilabos", instructions=INSTRUCTIONS, version="1.0.0")
        self.cache: OrderedDict[str, tuple[float, str, Any]] = OrderedDict()
        self.tools = [self._tool(operation) for operation in OPERATIONS]
        self.tools.extend([
            types.Tool(name="protocol_guide", description="接入指南、流程与异常/物料/锁约定（先读）。",
                       inputSchema=_object({}), annotations=types.ToolAnnotations(readOnlyHint=True)),
            types.Tool(name="protocol_search", description="按关键词/域检索公开操作，返回工具名和用途，不执行。",
                       inputSchema=_object({"query": {"type": "string", "default": ""}}),
                       annotations=types.ToolAnnotations(readOnlyHint=True)),
            types.Tool(name="protocol_inspect", description="查看工具的完整请求 schema 和对应 HTTP 定义。",
                       inputSchema=_object({"tool_name": {"type": "string"}}, ("tool_name",)),
                       annotations=types.ToolAnnotations(readOnlyHint=True)),
            types.Tool(name="protocol_result_read", description="读取大响应缓存。pointer 为 JSON Pointer；数组按 offset/limit 分页。",
                       inputSchema=_object({"result_id": {"type": "string"},
                                            "pointer": {"type": "string", "default": ""},
                                            "offset": {"type": "integer", "minimum": 0, "default": 0},
                                            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10}},
                                           ("result_id",)),
                       annotations=types.ToolAnnotations(readOnlyHint=True)),
            types.Tool(name="protocol_wait_task", description="有界等待任务终态或人工待办；最多 20 秒，不做任何决策/重试。",
                       inputSchema=_object({"task_uuid": {"type": "string"},
                                            "timeout_seconds": {"type": "number", "minimum": 0,
                                                                "maximum": 20, "default": 10}}, ("task_uuid",)),
                       annotations=types.ToolAnnotations(readOnlyHint=True)),
            types.Tool(name="protocol_batch", description="并发调用最多 16 个公开业务操作，例如同时提交锁竞争任务。各操作独立提交，非原子事务；不重试。仅执行用户已授权操作。",
                       inputSchema=_object({"requests": {"type": "array", "minItems": 1, "maxItems": 16,
                                            "items": _object({"tool_name": {"type": "string"},
                                                              "arguments": {"type": "object"}},
                                                             ("tool_name", "arguments"))}}, ("requests",)),
                       annotations=types.ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)),
        ])
        if len({tool.name for tool in self.tools}) != len(self.tools):
            raise ValueError("MCP 工具名称冲突")
        self.server.list_tools()(self.list_tools)
        self.server.call_tool()(self.call_tool)
        self.server.list_resources()(self.list_resources)
        self.server.read_resource()(self.read_resource)

    def _tool(self, operation: Operation) -> types.Tool:
        description = f"{operation.summary}。{operation.method} {operation.path}；角色 {operation.role}。"
        if operation.mutates:
            description += "写操作：仅在用户授权范围内执行，可能操作真实设备。"
        return types.Tool(
            name=operation.tool_name, description=description,
            inputSchema=input_schema(self.document, operation),
            annotations=types.ToolAnnotations(
                readOnlyHint=not operation.mutates,
                destructiveHint=operation.mutates,
                idempotentHint=not operation.mutates,
                openWorldHint=operation.mutates,
            ),
        )

    async def list_tools(self) -> list[types.Tool]:
        return self.tools

    async def list_resources(self) -> list[types.Resource]:
        return [
            types.Resource(uri="unilab://protocol/guide", name="接入指南", mimeType="text/markdown"),
            types.Resource(uri="unilab://protocol/operations", name="公开操作目录", mimeType="application/json"),
        ]

    async def read_resource(self, uri: Any) -> list[ReadResourceContents]:
        if str(uri) == "unilab://protocol/guide":
            return [ReadResourceContents(GUIDE, mime_type="text/markdown")]
        if str(uri) == "unilab://protocol/operations":
            return [ReadResourceContents(json.dumps(self.search(""), ensure_ascii=False), mime_type="application/json")]
        raise ValueError("未知资源 URI；本服务不读取任意文件或 URL")

    def search(self, query: str) -> list[dict]:
        terms = query.casefold().split()
        return [
            {**asdict(operation), "tool_name": operation.tool_name, "mutates": operation.mutates}
            for operation in OPERATIONS
            if all(term in str(asdict(operation)).casefold() or term in operation.tool_name for term in terms)
        ]

    def pack(self, value: dict, *, error: bool = False) -> types.CallToolResult:
        encoded = json.dumps(value, ensure_ascii=False)
        if len(encoded) <= self.INLINE_CHARS:
            return _result(value, error=error)
        self._expire_cache()
        if len(encoded) > self.CACHE_CHARS:
            return _result({"error": "响应超过 MCP 缓存上限，请缩小原 HTTP 查询范围",
                            "characters": len(encoded), "http_status": value.get("http_status"),
                            "request_may_have_completed": True}, error=True)
        while self.cache and (
            len(self.cache) >= self.CACHE_ENTRIES
            or sum(len(item[1]) for item in self.cache.values()) + len(encoded) > self.CACHE_CHARS
        ):
            self.cache.popitem(last=False)
        result_id = str(uuid4())
        self.cache[result_id] = (time.monotonic(), encoded, value)
        return _result({"http_status": value.get("http_status"), "result_id": result_id,
                        "truncated": True, "characters": len(encoded), "expires_in_seconds": self.CACHE_TTL,
                        "preview": encoded[:1200],
                        "outline": self.outline(value),
                        "next": "protocol_result_read，按 outline 选择 pointer（如 /body/data/0/device_routes），limit 最大 50"}, error=error)

    @classmethod
    def outline(cls, value: Any, depth: int = 3) -> Any:
        if isinstance(value, dict):
            if depth <= 0:
                return {"type": "object", "keys": list(value)[:40]}
            return {key: cls.outline(child, depth - 1) for key, child in list(value.items())[:40]}
        if isinstance(value, list):
            return {"type": "array", "length": len(value),
                    "item_0": cls.outline(value[0], depth - 1) if depth > 0 and value else None}
        return {"type": type(value).__name__}

    def _expire_cache(self) -> None:
        now = time.monotonic()
        for key, (created, _, _) in list(self.cache.items()):
            if now - created > self.CACHE_TTL:
                del self.cache[key]

    def read_result(self, arguments: dict) -> dict:
        self._expire_cache()
        entry = self.cache.get(arguments["result_id"])
        if entry is None:
            raise ValueError("结果不存在或已过期；读取操作可重新查询，写操作不要盲目重发")
        value = entry[2]
        pointer = arguments.get("pointer", "")
        if pointer:
            if not pointer.startswith("/"):
                raise ValueError("pointer 必须是空串或以 / 开始的 JSON Pointer")
            for part in pointer[1:].split("/"):
                key = part.replace("~1", "/").replace("~0", "~")
                value = value[int(key)] if isinstance(value, list) else value[key]
        offset, limit = arguments.get("offset", 0), arguments.get("limit", 10)
        if isinstance(value, list):
            result = {"result_id": arguments["result_id"], "pointer": pointer, "total": len(value),
                    "offset": offset, "next_offset": offset + limit if offset + limit < len(value) else None,
                    "items": value[offset:offset + limit]}
            if len(json.dumps(result, ensure_ascii=False)) > self.INLINE_CHARS:
                result["items"] = [
                    {"pointer": f"{pointer}/{index}", "outline": self.outline(item, depth=1)}
                    for index, item in enumerate(value[offset:offset + limit], start=offset)
                ]
                result["truncated"] = True
                result["next"] = "单个元素过大，沿 items 的 pointer 和字段继续读取；仍使用原 result_id"
            return result
        encoded = json.dumps(value, ensure_ascii=False)
        if len(encoded) > self.INLINE_CHARS:
            # 不对大对象反复建新缓存；给出字段目录，引导缩小 JSON Pointer。
            return {"result_id": arguments["result_id"], "pointer": pointer, "characters": len(encoded),
                    "keys": list(value) if isinstance(value, dict) else None,
                    "text_offset": offset, "text": encoded[offset:offset + self.INLINE_CHARS // 2],
                    "next_text_offset": offset + self.INLINE_CHARS // 2 if offset + self.INLINE_CHARS // 2 < len(encoded) else None}
        return {"value": value}

    @staticmethod
    def _path(operation: Operation, arguments: dict) -> str:
        path = operation.path
        for name, value in arguments.get("path", {}).items():
            text = str(value)
            if not text or text in {".", ".."} or any(char in text for char in "/\\%?#"):
                raise ValueError(f"非法路径参数 {name}，不得包含路径分隔符或转义")
            path = path.replace("{" + name + "}", quote(text, safe=""))
        if "{" in path:
            raise ValueError("缺少必填 path 参数")
        return path

    async def request(self, operation: Operation, arguments: dict) -> tuple[dict, bool]:
        path = self._path(operation, arguments)
        query = {key: value for key, value in arguments.get("query", {}).items() if value is not None}
        from unilabos.config.control_token import client_headers

        # 进程内 ASGI 调用同样经过写操作的 token 检查：MCP 端点本身只接受本机、拒绝浏览器 Origin
        kwargs: dict[str, Any] = {"params": query, "headers": {"accept": "application/json", **client_headers()}}
        if "body" in arguments:
            kwargs["json"] = normalize_body(operation, arguments["body"])
        # ASGI 调用仍经过应用中间件、原来的参数验证、路由与调度/物料服务。
        # 不监听第二个端口，不提供任意 URL，不重试写入，也不绕过 Host 控制面代理。
        transport = httpx.ASGITransport(app=self.app, client=("127.0.0.1", 0), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            try:
                async with asyncio.timeout(45):
                    response = await client.request(operation.method, path, **kwargs)
            except TimeoutError:
                return {"http_status": None, "error": "业务请求超时；请查询执行状态，不要盲目重试写入",
                        "request_may_have_completed": operation.mutates}, True
        try:
            body = response.json()
        except ValueError:
            body = {"text": response.text[:self.INLINE_CHARS]}
        error = response.is_error or (isinstance(body, dict) and body.get("code", 0) != 0)
        return {"http_status": response.status_code, "body": body}, error

    async def wait_task(self, task_uuid: str, timeout: float) -> types.CallToolResult:
        # ASGITransport 的 timeout 参数不会中断进程内处理；外层时限需显式约束整段等待。
        try:
            async with asyncio.timeout(max(0.1, timeout)):
                return await self._wait_task(task_uuid, timeout)
        except TimeoutError:
            return _result({"reason": "timeout", "task_uuid": task_uuid,
                            "next": "任务仍可能运行，继续查询 workflow_task_get；未作任何写入"})

    async def _wait_task(self, task_uuid: str, timeout: float) -> types.CallToolResult:
        deadline = time.monotonic() + timeout
        while True:
            response, error = await self.request(self.operations["workflow_task_get"], {"path": {"task_uuid": task_uuid}})
            if error:
                return self.pack(response, error=True)
            task = response["body"].get("data", response["body"])
            if task.get("status") in {"succeeded", "failed", "canceled", "cancelled", "completed"}:
                return self.pack({**response, "reason": "terminal"})
            decisions, failed = await self.request(self.operations["decisions_error_decisions_list"], {})
            if not failed:
                data = decisions["body"].get("data", decisions["body"])
                pending = [item for item in data.get("items", []) if item.get("task_id") == task_uuid]
                if pending:
                    return self.pack({**response, "reason": "decision_required", "decisions": pending})
            confirmations, failed = await self.request(
                self.operations["workflow_task_manual_confirmations"], {"path": {"task_uuid": task_uuid}}
            )
            if not failed:
                data = confirmations["body"].get("data", confirmations["body"])
                items = data if isinstance(data, list) else data.get("items", [])
                pending = [item for item in items if item.get("status") == "pending"]
                if pending:
                    return self.pack({**response, "reason": "confirmation_required", "confirmations": pending})
            if time.monotonic() >= deadline:
                return self.pack({**response, "reason": "timeout"})
            await asyncio.sleep(min(0.5, max(0, deadline - time.monotonic())))

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        try:
            if name == "protocol_guide":
                return _result({"guide": GUIDE})
            if name == "protocol_search":
                return self.pack({"operations": self.search(arguments.get("query", ""))})
            if name == "protocol_inspect":
                operation = self.operations[arguments["tool_name"]]
                return self.pack({"operation": asdict(operation), "input_schema": input_schema(self.document, operation),
                                  "http": self.document["paths"][operation.path][operation.method.lower()]})
            if name == "protocol_result_read":
                return self.pack(self.read_result(arguments))
            if name == "protocol_wait_task":
                return await self.wait_task(arguments["task_uuid"], arguments.get("timeout_seconds", 10))
            if name == "protocol_batch":
                entries = arguments["requests"]
                # 整批先校验，避免参数错误造成半批意外写入；业务失败仍按每条实际结果返回。
                for entry in entries:
                    operation = self.operations[entry["tool_name"]]
                    validator = Draft202012Validator(input_schema(self.document, operation))
                    errors = list(validator.iter_errors(entry["arguments"]))
                    if errors:
                        raise ValueError(f"{entry['tool_name']} 参数不合法：{errors[0].message}")
                    self._path(operation, entry["arguments"])
                    if "body" in entry["arguments"]:
                        # protocol 的跨字段校验不全能用 JSON Schema 表达，也必须在任何写入前完成。
                        normalize_body(operation, entry["arguments"]["body"])
                responses = await asyncio.gather(*[
                    self.request(self.operations[entry["tool_name"]], entry["arguments"]) for entry in entries
                ])
                return self.pack({"atomic": False, "results": [
                    {"tool_name": entry["tool_name"], "isError": failed, **response}
                    for entry, (response, failed) in zip(entries, responses)
                ]}, error=any(failed for _, failed in responses))
            response, error = await self.request(self.operations[name], arguments)
            return self.pack(response, error=error)
        except (KeyError, ValueError, IndexError, TypeError) as exc:
            return _result({"error": str(exc), "tool_name": name}, error=True)
