"""Enterprise Standards MCP server (SYNTHETIC demonstration data).

Runs as a stateless streamable-HTTP MCP server on 0.0.0.0:8000/mcp, which is the
contract Amazon Bedrock AgentCore Runtime expects for protocol=MCP.
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
log = logging.getLogger("standards-mcp")

mcp = FastMCP(
    "enterprise-standards",
    instructions="Synthetic ExampleCo enterprise standards. Data is fictional and for demonstration only.",
    host="0.0.0.0",
    port=8000,
    stateless_http=True,
    json_response=True,
)


def _correlation_id(ctx: Context | None) -> str:
    try:
        req = ctx.request_context.request  # starlette Request in streamable HTTP
        return (req.headers.get(CORRELATION_HEADER) or "none")[:64]
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
        # Sanitized: never log argument values (they may contain proposal text).
        log.info(json.dumps({
            "event": "tool_call", "server": tools.SERVER_NAME, "tool": name, "correlation_id": cid,
            "outcome": outcome, "duration_ms": round((time.perf_counter() - start) * 1000, 1),
            "arg_sizes": {k: len(str(v)) for k, v in kwargs.items()},
        }))


@mcp.tool()
def search_standards(query: str, category: str | None = None, max_results: int = 5, ctx: Context = None) -> dict:
    """Search synthetic enterprise standards by keywords.

    Args:
        query: Free-text description of the topic (max 500 chars).
        category: Optional filter: architecture, iam, encryption, logging, privacy,
            ai_governance, disaster_recovery, third_party.
        max_results: 1-10 (default 5).
    """
    return _run("search_standards", ctx, tools.search_standards, query=query, category=category, max_results=max_results)


@mcp.tool()
def get_standard(standard_id: str, ctx: Context = None) -> dict:
    """Get one standard by ID (for example STD-ENC-002), including its linked controls."""
    return _run("get_standard", ctx, tools.get_standard, standard_id=standard_id)


@mcp.tool()
def list_required_controls(risk_level: str, data_classification: str, ctx: Context = None) -> dict:
    """List controls required for a risk level (low|medium|high|critical) and data
    classification (public|internal|confidential|restricted)."""
    return _run("list_required_controls", ctx, tools.list_required_controls,
                risk_level=risk_level, data_classification=data_classification)


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
