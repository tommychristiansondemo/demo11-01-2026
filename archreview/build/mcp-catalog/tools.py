"""Approved Service Catalog tool logic (pure functions, no MCP dependency).

All data is SYNTHETIC demonstration data for a fictional organization.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

SERVER_NAME = "approved-service-catalog"
_DATA = json.loads((Path(__file__).parent / "data" / "catalog.json").read_text())
NOTICE = _DATA["dataset"]
_SERVICES = {s["service_id"]: s for s in _DATA["services"]}
_SVC_RE = re.compile(r"^SVC-[A-Z0-9-]{2,30}$")
_MAX_TEXT = 500


class ToolInputError(ValueError):
    """Raised for invalid tool arguments."""


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9.\-]+", text.lower()) if len(t) > 1}


def _check_text(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolInputError(f"{name} must be a non-empty string")
    if len(value) > _MAX_TEXT:
        raise ToolInputError(f"{name} must be at most {_MAX_TEXT} characters")
    return value


def _score(rec: dict[str, Any], text: str) -> int:
    q = text.lower()
    toks = _tokens(text)
    score = sum(3 for kw in rec.get("keywords", []) if kw in q)
    score += sum(2 for uc in rec.get("use_cases", []) if uc.lower() in q)
    score += 2 * len(toks & _tokens(rec["name"]))
    score += len(toks & _tokens(" ".join(rec.get("use_cases", []))))
    return score


def _envelope(**payload: Any) -> dict[str, Any]:
    return {"server": SERVER_NAME, "dataset_notice": NOTICE, "dataset_version": _DATA["version"], **payload}


def _svc_summary(s: dict[str, Any]) -> dict[str, Any]:
    return {"record_id": s["service_id"], "service_id": s["service_id"], "name": s["name"], "category": s["category"],
            "status": s["status"], "cost_band": s["cost_band"], "max_classification": s["max_classification"]}


def search_services(requirement: str, max_results: int = 6) -> dict[str, Any]:
    _check_text("requirement", requirement)
    max_results = max(1, min(int(max_results), 10))
    scored = sorted(((_score(s, requirement), s) for s in _DATA["services"]), key=lambda x: (-x[0], x[1]["service_id"]))
    results = [{**_svc_summary(s), "relevance": sc} for sc, s in scored if sc > 0][:max_results]
    return _envelope(tool="search_services", result_count=len(results), results=results)


def get_service_profile(service_id: str) -> dict[str, Any]:
    sid = (service_id or "").strip().upper()
    if not _SVC_RE.match(sid):
        raise ToolInputError("service_id must look like SVC-NAME")
    s = _SERVICES.get(sid)
    if not s:
        return _envelope(tool="get_service_profile", found=False, record_id=sid, message=f"No catalog entry {sid}")
    profile = {k: v for k, v in s.items() if k != "keywords"}
    return _envelope(tool="get_service_profile", found=True, record_id=sid, profile=profile)


def compare_services(service_ids: list[str]) -> dict[str, Any]:
    if not isinstance(service_ids, list) or not 2 <= len(service_ids) <= 5:
        raise ToolInputError("service_ids must be a list of 2 to 5 IDs")
    ids = []
    for raw in service_ids:
        sid = str(raw).strip().upper()
        if not _SVC_RE.match(sid):
            raise ToolInputError(f"invalid service_id: {raw!r}")
        if sid not in ids:
            ids.append(sid)
    found = [_SERVICES[i] for i in ids if i in _SERVICES]
    missing = [i for i in ids if i not in _SERVICES]
    rows = [{
        "record_id": s["service_id"], "name": s["name"], "status": s["status"], "cost_band": s["cost_band"],
        "max_classification": s["max_classification"], "owner": s["owner"],
        "constraint_count": len(s["constraints"]), "constraints": s["constraints"], "use_cases": s["use_cases"],
    } for s in found]
    cost_rank = {"$": 1, "$$": 2, "$$$": 3}
    return _envelope(tool="compare_services", compared=[r["record_id"] for r in rows], not_found=missing,
                     comparison=rows,
                     lowest_cost=sorted(rows, key=lambda r: cost_rank.get(r["cost_band"], 9))[0]["record_id"] if rows else None)


def find_reference_pattern(use_case: str, max_results: int = 3) -> dict[str, Any]:
    _check_text("use_case", use_case)
    max_results = max(1, min(int(max_results), 6))
    scored = sorted(((_score(p, use_case), p) for p in _DATA["patterns"]), key=lambda x: (-x[0], x[1]["pattern_id"]))
    results = [{"record_id": p["pattern_id"], **{k: v for k, v in p.items() if k != "keywords"}, "relevance": sc}
               for sc, p in scored if sc > 0][:max_results]
    return _envelope(tool="find_reference_pattern", result_count=len(results), results=results)
