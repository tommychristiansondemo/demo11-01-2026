"""Asynchronous review worker Lambda: runs the orchestrating agent for one queued review."""
from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

import boto3
from botocore.config import Config

from . import config, obs
from .mcp_client import AgentCoreMcpClient, SigV4HttpxAuth, runtime_url
from .models import ReviewRequest
from .orchestrator import CATALOG, STANDARDS, ReviewAgent

REGION = os.environ.get("AWS_REGION", "us-west-2")
_bedrock = boto3.client("bedrock-runtime", config=Config(connect_timeout=5, read_timeout=120,
                                                          retries={"max_attempts": 4, "mode": "adaptive"}))
_table = boto3.resource("dynamodb").Table(os.environ.get("TABLE_NAME", "unset"))


async def bedrock_converse(**kwargs: Any) -> dict[str, Any]:
    return await asyncio.to_thread(_bedrock.converse, **kwargs)


def build_agent(req: ReviewRequest, cid: str) -> ReviewAgent:
    auth = SigV4HttpxAuth(REGION)
    clients = {
        srv: AgentCoreMcpClient(srv, runtime_url(config.runtime_arn(srv)), cid, auth=auth)
        for srv in (STANDARDS, CATALOG)
    }
    remaining = 270.0
    return ReviewAgent(req, cid, clients, bedrock_converse, config.model_ids(), deadline_s=remaining - 40)


def _update(review_id: str, **fields: Any) -> None:
    names = {f"#{k}": k for k in fields}
    values = {f":{k}": v for k, v in fields.items()}
    _table.update_item(Key={"review_id": review_id},
                       UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in fields),
                       ExpressionAttributeNames=names, ExpressionAttributeValues=values)


def handler(event, context):
    review_id = event.get("review_id", "")
    cid = obs.new_correlation_id(event.get("correlation_id"))
    item = _table.get_item(Key={"review_id": review_id}).get("Item")
    if not item:
        obs.log("review_missing", level="ERROR", correlation_id=cid, review_id=review_id)
        return {"ok": False}
    if item.get("status") not in ("queued",):
        obs.log("review_already_processed", level="WARN", correlation_id=cid, review_id=review_id)
        return {"ok": True}
    _update(review_id, status="running", started_at=int(time.time()))
    start = time.time()
    try:
        req = ReviewRequest.model_validate(json.loads(item["request"]))
        result = asyncio.run(build_agent(req, cid).run())
        _update(review_id, status=result["status"], result=json.dumps(result), completed_at=int(time.time()))
        obs.metric("ReviewsCompleted", 1, Status=result["status"])
        obs.metric("ReviewDuration", round((time.time() - start) * 1000), "Milliseconds")
        if len(result["servers_used"]) == 2:
            obs.metric("ReviewsUsingBothServers", 1)
        return {"ok": True, "status": result["status"]}
    except Exception as exc:
        obs.log("review_failed", level="ERROR", correlation_id=cid, review_id=review_id, error=type(exc).__name__,
                detail=str(exc)[:300])
        obs.metric("ReviewsFailed", 1)
        _update(review_id, status="failed", error="The review could not be completed. Please try again.",
                completed_at=int(time.time()))
        return {"ok": False}


