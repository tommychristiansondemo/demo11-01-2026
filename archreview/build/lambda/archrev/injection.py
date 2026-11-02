"""Heuristic prompt-injection screening for user input and retrieved MCP records.

Defense in depth: this screen flags and neutralizes obvious instruction-like content; the
system prompt also tells the model to treat all user and retrieved content as data, and the
output validator independently checks every claim against retrieved records.
"""
from __future__ import annotations

import re
from typing import Any

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("ignore_instructions", re.compile(r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b(instruction|prompt|rule|guidance|polic)", re.I)),
    ("system_override", re.compile(r"\b(system|developer)\s*(override|prompt|message|mode)\b", re.I)),
    ("role_change", re.compile(r"\byou are now\b|\bact as (an? )?(unrestricted|different|new)\b|\bpretend (to be|you are)\b", re.I)),
    ("forced_verdict", re.compile(r"\b(mark|declare|rate|label)\b[^.\n]{0,40}\b(approved|no risks?|compliant)\b", re.I)),
    ("tag_injection", re.compile(r"</?\s*(system|assistant|instructions?|tool_result|untrusted)\s*>", re.I)),
    ("exfiltration", re.compile(r"\b(reveal|print|show|output)\b[^.\n]{0,30}\b(system prompt|instructions|credentials|secrets?|api key)\b", re.I)),
    ("jailbreak", re.compile(r"\bjailbreak\b|\bDAN mode\b|\bdo anything now\b", re.I)),
]

REDACTION = "[REDACTED: instruction-like content removed by injection screen]"


def scan_text(text: str) -> list[str]:
    if not text:
        return []
    return [name for name, rx in PATTERNS if rx.search(text)]


def neutralize_tags(text: str) -> str:
    """Prevent user text from closing or opening the delimiters used in the prompt."""
    return re.sub(r"<\s*/?\s*(proposal|system|assistant|instructions?|tool_result|untrusted[a-z_]*)\b[^>]*>", "[tag removed]", text, flags=re.I)


def scrub_record(obj: Any, path: str = "") -> tuple[Any, list[dict[str, str]]]:
    """Walk a retrieved JSON object; redact string fields that look like injected instructions.

    Returns the cleaned object and a list of findings ({path, patterns}).
    """
    findings: list[dict[str, str]] = []
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            out[k], f = scrub_record(v, f"{path}.{k}" if path else k)
            findings.extend(f)
        return out, findings
    if isinstance(obj, list):
        out_l = []
        for i, v in enumerate(obj):
            nv, f = scrub_record(v, f"{path}[{i}]")
            out_l.append(nv)
            findings.extend(f)
        return out_l, findings
    if isinstance(obj, str):
        hits = scan_text(obj)
        if hits:
            return REDACTION, [{"path": path, "patterns": ",".join(hits)}]
    return obj, findings
