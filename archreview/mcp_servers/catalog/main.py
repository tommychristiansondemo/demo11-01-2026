"""Approved Service Catalog MCP server (SYNTHETIC demonstration data).

Stateless streamable-HTTP MCP server on 0.0.0.0:8000/mcp for Amazon Bedrock AgentCore Runtime.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

import tools

CORRELATION_HEADER = "x-amzn-bedrock-agentcore-runtime-custom-correlation-id"

logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
log = logging.getLogger("catalog-mcp")

mcp = FastMCP(
    "approved-service-catalog",
    instructions="Synthetic ExampleCo approved service catalog and reference patterns. Fictional demonstration data.",
    host="0.0.0.0",
    port=8000,
    stateless_http=True,
    json_response=True,
)


def _correlation_id(ctx: Context | None) -> str:
    try:
        return (ctx.request_context.request.headers.get(CORRELATION_HEADER) or "none")[:64]
    except Exception:
        return "none"


def _run(name: str, ctx: Context | None, fn, **kwargs: Any) -> dict[str, Any]:
    start = time.perf_counter()
    cid = _correlation_id(ctx)
    outcome = "ok"
    try:
        result = fn(**kwargs)
        result["correlation_id"] = cid
        return result
    except tools.ToolInputError:
        outcome = "invalid_input"
        raise
    except Exception:
        outcome = "error"
        raise
    finally:
        log.info(json.dumps({
            "event": "tool_call", "server": tools.SERVER_NAME, "tool": name, "correlation_id": cid,
            "outcome": outcome, "duration_ms": round((time.perf_counter() - start) * 1000, 1),
            "arg_sizes": {k: len(str(v)) for k, v in kwargs.items()},
        }))


@mcp.tool()
def search_services(requirement: str, max_results: int = 6, ctx: Context = None) -> dict:
    """Search the synthetic approved service catalog for services matching a requirement (max 500 chars)."""
    return _run("search_services", ctx, tools.search_services, requirement=requirement, max_results=max_results)


@mcp.tool()
def get_service_profile(service_id: str, ctx: Context = None) -> dict:
    """Get the full catalog profile (status, constraints, owner, cost band) for a service ID such as SVC-LAMBDA."""
    return _run("get_service_profile", ctx, tools.get_service_profile, service_id=service_id)


@mcp.tool()
def compare_services(service_ids: list[str], ctx: Context = None) -> dict:
    """Compare 2-5 catalog services side by side (status, cost band, constraints, classification limits)."""
    return _run("compare_services", ctx, tools.compare_services, service_ids=service_ids)


@mcp.tool()
def find_reference_pattern(use_case: str, max_results: int = 3, ctx: Context = None) -> dict:
    """Find approved reference architecture patterns for a use case (max 500 chars)."""
    return _run("find_reference_pattern", ctx, tools.find_reference_pattern, use_case=use_case, max_results=max_results)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
