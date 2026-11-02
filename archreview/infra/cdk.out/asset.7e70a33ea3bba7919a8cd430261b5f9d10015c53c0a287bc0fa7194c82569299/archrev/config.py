"""Runtime configuration from environment + SSM Parameter Store (cached per container)."""
from __future__ import annotations

import json
import os
from functools import lru_cache

DEFAULT_MODEL_PREFERENCE = [
    "us.anthropic.claude-sonnet-4-6",
    "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "us.amazon.nova-pro-v1:0",
]


@lru_cache(maxsize=1)
def ssm_params() -> dict[str, str]:
    prefix = os.environ.get("PARAM_PREFIX", "/archrev")
    import boto3
    ssm = boto3.client("ssm")
    out: dict[str, str] = {}
    paginator = ssm.get_paginator("get_parameters_by_path")
    for page in paginator.paginate(Path=prefix, Recursive=True):
        for p in page["Parameters"]:
            out[p["Name"][len(prefix):].lstrip("/")] = p["Value"]
    return out


def model_ids() -> list[str]:
    raw = ssm_params().get("model-ids")
    try:
        ids = json.loads(raw) if raw else []
    except ValueError:
        ids = []
    return [m for m in ids if isinstance(m, str)] or DEFAULT_MODEL_PREFERENCE


def runtime_arn(server: str) -> str:
    key = {"enterprise-standards": "STANDARDS_RUNTIME_ARN", "approved-service-catalog": "CATALOG_RUNTIME_ARN"}[server]
    return os.environ[key]
