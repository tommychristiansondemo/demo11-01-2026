"""Deterministic risk-level derivation (transparent and unit-tested; not model-generated)."""
from __future__ import annotations

from .models import ReviewRequest

LEVELS = ["low", "medium", "high", "critical"]
_SCORE_TO_LEVEL = ["low", "low", "medium", "high", "critical"]  # score 0-1 low, 2 medium, 3 high, 4+ critical
_CLASS_BASE = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}


def derive_risk_level(req: ReviewRequest) -> tuple[str, list[str]]:
    score = _CLASS_BASE[req.data_classification]
    reasons = [f"data classification '{req.data_classification}' (+{score})"]
    if float(req.availability_target) >= 99.95:
        score += 1
        reasons.append(f"availability target {req.availability_target}% is Tier 1 (+1)")
    if req.external_integrations and _CLASS_BASE[req.data_classification] >= 2:
        score += 1
        reasons.append("external integrations with confidential/restricted data (+1)")
    if req.expected_users > 100_000:
        score += 1
        reasons.append("more than 100,000 expected users (+1)")
    level = _SCORE_TO_LEVEL[min(score, 4)]
    return level, reasons


def availability_tier(target: str) -> str:
    t = float(target)
    if t >= 99.95:
        return "Tier 1"
    if t >= 99.9:
        return "Tier 2"
    return "Tier 3"
