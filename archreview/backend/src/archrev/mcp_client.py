"""MCP client for MCP servers hosted on Amazon Bedrock AgentCore Runtime.

* Uses the official MCP Python SDK streamable-HTTP transport.
* Authenticates every HTTP request with AWS SigV4 (service 'bedrock-agentcore') using the
  caller's IAM role, so no credentials or runtime endpoints ever reach the browser.
* Propagates the correlation ID in an allow-listed custom header and derives the runtime
  session ID from it.
* Each tool call opens a short-lived stateless MCP session, with timeout and retry.
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from urllib.parse import quote

import httpx

from . import obs

CORRELATION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Correlation-Id"
SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"


def runtime_url(runtime_arn: str, region: str | None = None) -> str:
    region = region or runtime_arn.split(":")[3]
    return (f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/"
            f"{quote(runtime_arn, safe='')}/invocations?qualifier=DEFAULT")


class SigV4HttpxAuth(httpx.Auth):
    """httpx auth flow that signs each request with botocore SigV4."""

    requires_request_body = True

    def __init__(self, region: str, service: str = "bedrock-agentcore", session=None):
        import boto3  # lazy: keeps unit tests light
        self._session = session or boto3.Session()
        self._region = region
        self._service = service

    def auth_flow(self, request: httpx.Request):
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        creds = self._session.get_credentials().get_frozen_credentials()
        signed_headers = {k: v for k, v in request.headers.items()
                          if k.lower() in ("host", "content-type") or k.lower().startswith("x-amz")}
        aws_req = AWSRequest(method=request.method, url=str(request.url), data=request.content, headers=signed_headers)
        SigV4Auth(creds, self._service, self._region).add_auth(aws_req)
        for k, v in aws_req.headers.items():
            request.headers[k] = v
        yield request


@dataclass
class CallRecord:
    server: str
    tool: str
    ok: bool
    duration_ms: float
    attempts: int
    error: str | None = None
    record_ids: list[str] = field(default_factory=list)


class McpToolError(RuntimeError):
    pass


class AgentCoreMcpClient:
    """Calls tools on one MCP server hosted in AgentCore Runtime (or any streamable-HTTP URL)."""

    def __init__(self, server_name: str, url: str, correlation_id: str, *, auth: httpx.Auth | None = None,
                 timeout_s: float = 25.0, max_attempts: int = 3):
        self.server_name = server_name
        self.url = url
        self.correlation_id = correlation_id
        self.auth = auth
        self.timeout_s = timeout_s
        self.max_attempts = max_attempts
        sid = f"{correlation_id}-{server_name}"
        self.session_id = (sid + "-" + "0" * 40)[:64] if len(sid) < 33 else sid[:100]

    def _headers(self) -> dict[str, str]:
        return {CORRELATION_HEADER: self.correlation_id, SESSION_HEADER: self.session_id}

    async def _once(self, op: str, name: str | None = None, args: dict[str, Any] | None = None) -> Any:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(self.url, headers=self._headers(), timeout=self.timeout_s,
                                         sse_read_timeout=self.timeout_s, auth=self.auth,
                                         terminate_on_close=False) as (r, w, _):
            async with ClientSession(r, w, read_timeout_seconds=timedelta(seconds=self.timeout_s)) as session:
                await session.initialize()
                if op == "list_tools":
                    return await session.list_tools()
                return await session.call_tool(name, args or {})

    async def _with_retry(self, op: str, name: str | None = None, args: dict[str, Any] | None = None):
        last: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return await asyncio.wait_for(self._once(op, name, args), timeout=self.timeout_s + 5), attempt
            except Exception as exc:  # network, 5xx, throttling, timeout
                last = exc
                if attempt < self.max_attempts:
                    await asyncio.sleep(min(4.0, 0.4 * 2 ** (attempt - 1)) + random.random() * 0.2)
        raise McpToolError(f"{type(last).__name__}: {_short(last)}") from last

    async def list_tools(self) -> list[dict[str, Any]]:
        res, _ = await self._with_retry("list_tools")
        return [{"name": t.name, "description": t.description, "inputSchema": t.inputSchema} for t in res.tools]

    async def call_tool(self, name: str, args: dict[str, Any]) -> tuple[dict[str, Any], CallRecord]:
        start = time.perf_counter()
        try:
            res, attempts = await self._with_retry("call_tool", name, args)
        except McpToolError as exc:
            rec = CallRecord(self.server_name, name, False, _ms(start), self.max_attempts, str(exc)[:300])
            _log_call(rec, self.correlation_id)
            raise
        payload = _payload(res)
        if getattr(res, "isError", False):
            rec = CallRecord(self.server_name, name, False, _ms(start), attempts, str(payload.get("error", ""))[:300])
            _log_call(rec, self.correlation_id)
            return {"error": payload.get("error") or payload}, rec
        rec = CallRecord(self.server_name, name, True, _ms(start), attempts, record_ids=extract_record_ids(payload))
        _log_call(rec, self.correlation_id)
        return payload, rec


def _payload(res: Any) -> dict[str, Any]:
    sc = getattr(res, "structuredContent", None)
    if isinstance(sc, dict):
        return sc.get("result", sc) if set(sc.keys()) == {"result"} else sc
    texts = [c.text for c in getattr(res, "content", []) if getattr(c, "type", "") == "text"]
    joined = "\n".join(texts)
    try:
        return json.loads(joined)
    except Exception:
        return {"error": joined[:1000]} if getattr(res, "isError", False) else {"text": joined[:4000]}


ID_KEYS = ("record_id", "standard_id", "control_id", "service_id", "pattern_id")


def extract_record_ids(obj: Any) -> list[str]:
    """Record IDs actually returned by a server (keys in ID_KEYS, plus 'source_standards')."""
    found: list[str] = []

    def walk(o: Any) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ID_KEYS and isinstance(v, str):
                    found.append(v)
                elif k == "source_standards" and isinstance(v, list):
                    found.extend(x for x in v if isinstance(x, str))
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(obj)
    return sorted(set(found))


def _ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000, 1)


def _short(exc: Exception | None) -> str:
    if exc is None:
        return ""
    if isinstance(exc, BaseExceptionGroup):  # anyio task groups wrap transport errors
        return "; ".join(_short(e) for e in exc.exceptions)[:300]
    return str(exc)[:300]


def _log_call(rec: CallRecord, cid: str) -> None:
    obs.log("mcp_call", level="INFO" if rec.ok else "WARN", correlation_id=cid, server=rec.server, tool=rec.tool,
            ok=rec.ok, attempts=rec.attempts, duration_ms=rec.duration_ms, record_count=len(rec.record_ids),
            error=rec.error)
    obs.metric("McpCallLatency", rec.duration_ms, "Milliseconds", Server=rec.server)
    obs.metric("McpCallErrors" if not rec.ok else "McpCalls", 1, "Count", Server=rec.server)
