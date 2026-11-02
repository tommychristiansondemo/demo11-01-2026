"""Post-deploy verification of the two AgentCore MCP runtimes.

1. Confirms the two runtimes are distinct AgentCore Runtime deployments (different IDs/ARNs, READY,
   protocol MCP, each with its own execution role).
2. Lists tools and invokes EVERY MCP tool directly over SigV4-signed streamable HTTP.
Writes build/mcp_verification.json.
"""
import asyncio
import json
import sys
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))
from archrev.mcp_client import AgentCoreMcpClient, SigV4HttpxAuth, runtime_url  # noqa: E402

CALLS = {
    "enterprise-standards": [
        ("search_standards", {"query": "multi-region disaster recovery for 99.99 availability"}),
        ("search_standards", {"query": "logs", "category": "privacy"}),
        ("get_standard", {"standard_id": "STD-AI-002"}),
        ("list_required_controls", {"risk_level": "high", "data_classification": "confidential"}),
    ],
    "approved-service-catalog": [
        ("search_services", {"requirement": "event-driven serverless processing with a queue"}),
        ("get_service_profile", {"service_id": "SVC-BEDROCK"}),
        ("compare_services", {"service_ids": ["SVC-AURORA", "SVC-DYNAMODB", "SVC-EKS"]}),
        ("find_reference_pattern", {"use_case": "generative AI assistant chatbot"}),
    ],
}


def summarize(payload):
    keys = ("found", "result_count", "record_id", "compared", "lowest_cost", "correlation_id", "server")
    return {k: payload[k] for k in keys if k in payload}


async def main():
    outputs = json.loads((ROOT / "build" / "outputs.json").read_text())["ArchRevApp"]
    arns = {"enterprise-standards": outputs["standardsRuntimeArn"], "approved-service-catalog": outputs["catalogRuntimeArn"]}
    region = arns["enterprise-standards"].split(":")[3]
    ctl = boto3.client("bedrock-agentcore-control", region_name=region)
    report = {"region": region, "runtimes": {}, "tool_calls": [], "all_ok": True}
    for srv, arn in arns.items():
        rid = arn.split("/")[-1]
        rt = ctl.get_agent_runtime(agentRuntimeId=rid)
        report["runtimes"][srv] = {
            "agentRuntimeName": rt["agentRuntimeName"], "agentRuntimeId": rt["agentRuntimeId"], "agentRuntimeArn": arn,
            "status": rt["status"], "protocol": rt.get("protocolConfiguration", {}).get("serverProtocol"),
            "roleArn": rt["roleArn"], "version": rt.get("agentRuntimeVersion"),
            "artifact": "codeConfiguration" if "codeConfiguration" in rt.get("agentRuntimeArtifact", {}) else "container",
        }
    r = report["runtimes"]
    report["distinct"] = (r["enterprise-standards"]["agentRuntimeId"] != r["approved-service-catalog"]["agentRuntimeId"]
                          and r["enterprise-standards"]["roleArn"] != r["approved-service-catalog"]["roleArn"])
    auth = SigV4HttpxAuth(region)
    for srv, calls in CALLS.items():
        client = AgentCoreMcpClient(srv, runtime_url(arns[srv]), "cr-verify-mcp-0001", auth=auth, timeout_s=60)
        tools = await client.list_tools()
        report["runtimes"][srv]["tools"] = sorted(t["name"] for t in tools)
        for tool, args in calls:
            payload, rec = await client.call_tool(tool, args)
            report["tool_calls"].append({"server": srv, "tool": tool, "args": args, "ok": rec.ok,
                                         "duration_ms": rec.duration_ms, "attempts": rec.attempts,
                                         "record_ids": rec.record_ids, "summary": summarize(payload)})
            report["all_ok"] &= rec.ok
    covered = {(c["server"], c["tool"]) for c in report["tool_calls"] if c["ok"]}
    report["every_tool_invoked"] = all((srv, t) in covered for srv in arns for t in report["runtimes"][srv]["tools"])
    out = ROOT / "build" / "mcp_verification.json"
    out.write_text(json.dumps(report, indent=2))
    for srv, info in report["runtimes"].items():
        print(f"{srv}: {info['agentRuntimeName']} ({info['agentRuntimeId']}) {info['status']} protocol={info['protocol']} "
              f"tools={info['tools']}")
    for c in report["tool_calls"]:
        print(f"  {'OK ' if c['ok'] else 'ERR'} {c['server']}.{c['tool']} {c['duration_ms']}ms ids={c['record_ids'][:6]}")
    print("distinct runtimes:", report["distinct"], "| every tool invoked:", report["every_tool_invoked"],
          "| all ok:", report["all_ok"])
    sys.exit(0 if report["all_ok"] and report["distinct"] and report["every_tool_invoked"] else 1)


if __name__ == "__main__":
    asyncio.run(main())
