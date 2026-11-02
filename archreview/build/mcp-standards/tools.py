"""Enterprise Standards tool logic (pure functions, no MCP dependency).

All data is SYNTHETIC demonstration data for a fictional organization.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

SERVER_NAME = "enterprise-standards"
_DATA = json.loads((Path(__file__).parent / "data" / "standards.json").read_text())

CATEGORIES = sorted({s["category"] for s in _DATA["standards"]})
CLASSIFICATIONS: list[str] = _DATA["classifications"]
RISK_LEVELS: list[str] = _DATA["risk_levels"]
NOTICE = _DATA["dataset"]

_ID_RE = re.compile(r"^[A-Z]{3}-[A-Z]{2,4}-\d{3}$")
_MAX_QUERY = 500


class ToolInputError(ValueError):
    """Raised for invalid tool arguments (returned to the client as a tool error)."""


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9.\-]+", text.lower()) if len(t) > 1]


def _score(std: dict[str, Any], query: str) -> int:
    q = query.lower()
    toks = set(_tokens(query))
    score = 0
    for kw in std.get("keywords", []):
        if kw in q:
            score += 3
    title_toks = set(_tokens(std["title"]))
    score += 2 * len(toks & title_toks)
    body = " ".join([std["statement"], *std["requirements"]]).lower()
    body_toks = set(_tokens(body))
    score += len(toks & body_toks)
    return score


def _envelope(**payload: Any) -> dict[str, Any]:
    return {"server": SERVER_NAME, "dataset_notice": NOTICE, "dataset_version": _DATA["version"], **payload}


def _summary(std: dict[str, Any]) -> dict[str, Any]:
    return {
        "record_id": std["standard_id"],
        "standard_id": std["standard_id"],
        "title": std["title"],
        "category": std["category"],
        "status": std["status"],
        "statement": std["statement"],
    }


def search_standards(query: str, category: str | None = None, max_results: int = 5) -> dict[str, Any]:
    if not isinstance(query, str) or not query.strip():
        raise ToolInputError("query must be a non-empty string")
    if len(query) > _MAX_QUERY:
        raise ToolInputError(f"query must be at most {_MAX_QUERY} characters")
    if category in ("", "any", "all"):
        category = None
    if category is not None and category not in CATEGORIES:
        raise ToolInputError(f"category must be one of {CATEGORIES}")
    max_results = max(1, min(int(max_results), 10))
    scored = []
    for std in _DATA["standards"]:
        if category and std["category"] != category:
            continue
        s = _score(std, query)
        if s > 0 or category:
            scored.append((s, std))
    scored.sort(key=lambda x: (-x[0], x[1]["standard_id"]))
    results = [{**_summary(std), "relevance": s} for s, std in scored[:max_results]]
    return _envelope(tool="search_standards", query_category=category, result_count=len(results), results=results)


def get_standard(standard_id: str) -> dict[str, Any]:
    sid = (standard_id or "").strip().upper()
    if not _ID_RE.match(sid):
        raise ToolInputError("standard_id must look like STD-ABC-001")
    for std in _DATA["standards"]:
        if std["standard_id"] == sid:
            controls = [c for c in _DATA["controls"] if c["control_id"] in std["control_ids"]]
            record = {k: v for k, v in std.items() if k != "keywords"}
            return _envelope(tool="get_standard", found=True, record_id=sid, standard={**record, "controls": controls})
    return _envelope(tool="get_standard", found=False, record_id=sid, message=f"No standard with id {sid}")


def list_required_controls(risk_level: str, data_classification: str) -> dict[str, Any]:
    rl = (risk_level or "").strip().lower()
    dc = (data_classification or "").strip().lower()
    if rl not in RISK_LEVELS:
        raise ToolInputError(f"risk_level must be one of {RISK_LEVELS}")
    if dc not in CLASSIFICATIONS:
        raise ToolInputError(f"data_classification must be one of {CLASSIFICATIONS}")
    rank = RISK_LEVELS.index(rl)
    controls = [
        {"record_id": c["control_id"], **c}
        for c in _DATA["controls"]
        if dc in c["classifications"] and RISK_LEVELS.index(c["min_risk_level"]) <= rank
    ]
    return _envelope(
        tool="list_required_controls",
        risk_level=rl,
        data_classification=dc,
        result_count=len(controls),
        controls=controls,
        source_standards=sorted({c["standard_id"] for c in controls}),
    )
