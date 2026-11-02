"""Structured logging and CloudWatch Embedded Metric Format (EMF) helpers.

Logs are sanitized: callers pass only metadata (ids, sizes, counts, durations). Proposal
text is never logged unless LOG_PROPOSAL_TEXT=true (off by default).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from typing import Any

NAMESPACE = os.environ.get("METRICS_NAMESPACE", "ArchReview")
SERVICE = os.environ.get("SERVICE_NAME", "archrev")
_CID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
_SENSITIVE_KEYS = {"description", "business_objective", "proposal", "title", "password", "text", "content", "prompt"}


def new_correlation_id(candidate: str | None = None) -> str:
    """Accept a well-formed client correlation id, otherwise mint one."""
    if candidate and _CID_RE.match(candidate):
        return candidate
    return f"cr-{uuid.uuid4()}"


def _sanitize(value: Any) -> Any:
    if os.environ.get("LOG_PROPOSAL_TEXT", "false").lower() == "true":
        return value
    if isinstance(value, dict):
        return {k: ("[redacted len=%d]" % len(str(v)) if k in _SENSITIVE_KEYS else _sanitize(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    return value


def log(event: str, level: str = "INFO", **fields: Any) -> None:
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "level": level, "service": SERVICE, "event": event}
    rec.update(_sanitize(fields))
    sys.stdout.write(json.dumps(rec, default=str) + "\n")
    sys.stdout.flush()


def metric(name: str, value: float = 1, unit: str = "Count", **dims: str) -> None:
    """Emit one EMF metric. Dimensions limited to low-cardinality values."""
    dim_names = list(dims.keys())
    doc = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{
                "Namespace": NAMESPACE,
                "Dimensions": [dim_names] if dim_names else [[]],
                "Metrics": [{"Name": name, "Unit": unit}],
            }],
        },
        name: value,
        **dims,
    }
    sys.stdout.write(json.dumps(doc) + "\n")
    sys.stdout.flush()
