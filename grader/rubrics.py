"""Rubric and query resolution.

The grading core is source-agnostic. Rubrics are markdown files resolved from
an optional overlay first, then the configured rubric directory, then the
rubrics shipped in the image. A deployment can therefore add its own rubric
without rebuilding the service.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .config import CODE_RUBRIC_DIR, GENERIC_GLOBAL_PATH, RUBRIC_DIR, RUBRIC_GLOBAL_PATH, RUBRIC_OVERLAY_DIR, query_map, rubric_version

QUERIES = query_map()


def _dirs() -> list[Path]:
    out: list[Path] = []
    for d in (RUBRIC_OVERLAY_DIR, RUBRIC_DIR, CODE_RUBRIC_DIR):
        if d and d not in out:
            out.append(d)
    return out


def _find(name: str) -> Path | None:
    for d in _dirs():
        p = d / name
        if p.exists():
            return p
    return None


def _read_registry() -> dict:
    merged: dict = {}
    for d in reversed(_dirs()):
        p = d / "registry.json"
        if p.exists():
            try:
                merged.update(json.loads(p.read_text()))
            except Exception:
                pass
    return merged


REGISTRY = _read_registry()


def _global_text(rubric_id: str) -> str:
    # Benchmark rubrics (Q1–Q5) use the benchmark-specific global rubric.
    # Generic and custom rubrics use the neutral, source-agnostic one.
    generic = rubric_id == "generic_research_v1" or rubric_id not in ("Q1", "Q2", "Q3", "Q4", "Q5")
    name = "rubric_global_generic.md" if generic else "rubric_global.md"
    for d in (RUBRIC_OVERLAY_DIR, RUBRIC_DIR, CODE_RUBRIC_DIR):
        if d and (d / name).exists():
            return (d / name).read_text()
    path = GENERIC_GLOBAL_PATH if generic else RUBRIC_GLOBAL_PATH
    if path.exists():
        return path.read_text()
    fallback = GENERIC_GLOBAL_PATH if GENERIC_GLOBAL_PATH.exists() else RUBRIC_GLOBAL_PATH
    return fallback.read_text() if fallback.exists() else ""


def _normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def resolve(query_text: str | None = None, query_id: str | None = None, rubric_id: str | None = None) -> dict:
    """Return rubric_id, query_id, query_text, text, version, factual_confidence."""
    qtext = query_text or ""
    qid = query_id

    if rubric_id == "generic_research_v1":
        rid = "generic_research_v1"
    elif rubric_id:
        rid = rubric_id
        qid = qid or rid
        qtext = qtext or QUERIES.get(rid, "")
    elif qid and qid in QUERIES:
        rid = qid
        qtext = qtext or QUERIES[qid]
    else:
        # Exact query text match gives us a specific checklist.
        n = _normalize(qtext)
        match = next(((k, v) for k, v in QUERIES.items() if _normalize(v) == n), None)
        if match:
            qid, rid, qtext = match[0], match[0], match[1]
        else:
            rid = "generic_research_v1"

    if rid == "generic_research_v1":
        p = _find("generic.md")
        qtext = qtext or "Evaluate this research report's quality and evidence."
        factual = "low"
    else:
        p = _find(f"{rid}.md")
        if not p:
            raise RuntimeError(f"unknown rubric {rid}")
        qtext = qtext or QUERIES.get(rid, "")
        factual = "high" if rid in ("Q1", "Q2", "Q3", "Q4", "Q5") else "low"

    if not p:
        raise RuntimeError("generic rubric not found")

    meta = REGISTRY.get(rid, {})
    return {
        "rubric_id": rid,
        "query_id": qid or rid,
        "query_text": qtext,
        "rubric_text": _global_text(rid) + "\n\n" + p.read_text(),
        "rubric_version": rubric_version(rid),
        "factual_confidence": factual,
        "calibration_status": meta.get("calibration_status", "shadow" if rid == "generic_research_v1" else "provisional"),
        "review_after": meta.get("review_after"),
    }


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode()).hexdigest()
