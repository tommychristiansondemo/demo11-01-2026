"""Contract tests for both MCP servers over the real MCP streamable-HTTP protocol.

Local mode (default): starts each server as a subprocess on a free port (same code that runs in
AgentCore, which listens on 0.0.0.0:8000/mcp) and talks to it with the official MCP client.

Deployed mode: set STANDARDS_RUNTIME_ARN and CATALOG_RUNTIME_ARN (and AWS credentials) and the
same assertions run against the AgentCore Runtime endpoints with SigV4.
"""
import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from archrev.mcp_client import AgentCoreMcpClient, SigV4HttpxAuth, runtime_url

ROOT = Path(__file__).resolve().parents[2]

CONTRACT = {
    "enterprise-standards": {
        "dir": "mcp_servers/standards", "arn_env": "STANDARDS_RUNTIME_ARN",
        "tools": {"search_standards": {"query"}, "get_standard": {"standard_id"},
                  "list_required_controls": {"risk_level", "data_classification"}},
        "calls": [("search_standards", {"query": "encryption keys kms"}, "results"),
                  ("get_standard", {"standard_id": "STD-DR-001"}, "standard"),
                  ("list_required_controls", {"risk_level": "high", "data_classification": "restricted"}, "controls")],
        "bad": ("list_required_controls", {"risk_level": "nope", "data_classification": "public"}),
    },
    "approved-service-catalog": {
        "dir": "mcp_servers/catalog", "arn_env": "CATALOG_RUNTIME_ARN",
        "tools": {"search_services": {"requirement"}, "get_service_profile": {"service_id"},
                  "compare_services": {"service_ids"}, "find_reference_pattern": {"use_case"}},
        "calls": [("search_services", {"requirement": "serverless api"}, "results"),
                  ("get_service_profile", {"service_id": "SVC-DYNAMODB"}, "profile"),
                  ("compare_services", {"service_ids": ["SVC-LAMBDA", "SVC-FARGATE"]}, "comparison"),
                  ("find_reference_pattern", {"use_case": "partner integration webhook"}, "results")],
        "bad": ("get_service_profile", {"service_id": "not an id"}),
    },
}


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module", params=list(CONTRACT))
def server(request):
    name = request.param
    spec = CONTRACT[name]
    arn = os.environ.get(spec["arn_env"])
    if arn:
        region = arn.split(":")[3]
        client = AgentCoreMcpClient(name, runtime_url(arn), "cr-contract-test-0001", auth=SigV4HttpxAuth(region))
        yield name, spec, client
        return
    port = _free_port()
    code = (f"import main; main.mcp.settings.port={port}; main.mcp.settings.host='127.0.0.1'; "
            f"main.mcp.run(transport='streamable-http')")
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT / spec["dir"], stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT)
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            break
        except OSError:
            time.sleep(0.2)
    client = AgentCoreMcpClient(name, f"http://127.0.0.1:{port}/mcp", "cr-contract-test-0001", max_attempts=2)
    yield name, spec, client
    proc.terminate()
    proc.wait(timeout=10)


def test_tool_inventory_and_schemas(server):
    name, spec, client = server
    tools = asyncio.run(client.list_tools())
    by_name = {t["name"]: t for t in tools}
    assert set(by_name) == set(spec["tools"])
    for tname, required in spec["tools"].items():
        schema = by_name[tname]["inputSchema"]
        assert schema["type"] == "object"
        assert required <= set(schema.get("required", []))
        assert by_name[tname]["description"]


def test_every_tool_returns_contract_envelope(server):
    name, spec, client = server
    for tool, args, key in spec["calls"]:
        payload, rec = asyncio.run(client.call_tool(tool, args))
        assert rec.ok, (tool, payload)
        assert payload["server"] == name
        assert "SYNTHETIC" in payload["dataset_notice"]
        assert payload["correlation_id"] == "cr-contract-test-0001"
        assert key in payload and payload[key]
        assert rec.record_ids, tool


def test_invalid_input_is_tool_error(server):
    name, spec, client = server
    tool, args = spec["bad"]
    payload, rec = asyncio.run(client.call_tool(tool, args))
    assert rec.ok is False and "error" in payload
