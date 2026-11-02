"""Discover which Bedrock models are actually invocable in the target account/region.

Probes a preference-ordered candidate list (filtered to inference profiles / models that exist in
the region) with a tiny Converse call using tool use, and prints a JSON list of working IDs.
The deploy script stores the list in SSM (/archrev/model-ids); the agent falls back through it.
"""
import json
import sys

import boto3
from botocore.config import Config

PREFERENCE = [
    "us.anthropic.claude-sonnet-5-5", "us.anthropic.claude-sonnet-5", "us.anthropic.claude-sonnet-4-6",
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0", "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "us.amazon.nova-pro-v1:0",
]


def main(region: str) -> None:
    bedrock = boto3.client("bedrock", region_name=region)
    rt = boto3.client("bedrock-runtime", region_name=region, config=Config(read_timeout=30, retries={"max_attempts": 2}))
    available = {p["inferenceProfileId"] for page in bedrock.get_paginator("list_inference_profiles").paginate()
                 for p in page["inferenceProfileSummaries"]}
    working, report = [], []
    for mid in PREFERENCE:
        if mid not in available:
            report.append((mid, "not offered in region"))
            continue
        try:
            rt.converse(modelId=mid, messages=[{"role": "user", "content": [{"text": "Call the ping tool."}]}],
                        inferenceConfig={"maxTokens": 50},
                        toolConfig={"tools": [{"toolSpec": {"name": "ping", "description": "ping",
                                                            "inputSchema": {"json": {"type": "object", "properties": {}}}}}]})
            working.append(mid)
            report.append((mid, "OK"))
        except Exception as exc:  # AccessDenied, ValidationException, etc.
            report.append((mid, type(exc).__name__ + ": " + str(exc)[:90]))
        if len(working) >= 3:
            break
    for mid, status in report:
        print(f"  {mid}: {status}", file=sys.stderr)
    if not working:
        sys.exit("No invocable Bedrock model found; enable model access in the Bedrock console.")
    print(json.dumps(working))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "us-west-2")
