"""Paper registration and grading pipeline."""
from __future__ import annotations

import hashlib
import json
import threading

from . import db
from .config import load_config
from .judges import JudgeFailure, judge_paper
from .rubrics import resolve

PROMPT_VERSION = "judge-v1"
_CHART_LOCK = threading.Lock()


def register(data: dict) -> tuple[dict, bool]:
    row, created = db.create_paper(data)
    if created:
        db.audit("service", "paper.created", {"paper_id": row["paper_id"], "source_type": row["source_type"]})
    return row, created


def _cache_key(paper: dict, resolved: dict) -> str:
    cfg = load_config()
    pool = "|".join(f"{j['id']}:{j['model']}" for j in cfg["judges"]) + "|adj:" + cfg["adjudicator"]["id"] + ":" + cfg["adjudicator"]["model"]
    raw = "\n".join([paper["content_hash"], paper["rubric_id"], resolved["rubric_version"], pool, PROMPT_VERSION])
    return hashlib.sha256(raw.encode()).hexdigest()


def failure_grade(paper: dict, cache_key: str, resolved: dict, replace_cache_key: bool = False) -> dict:
    gid = db.save_grade({
        "paper_id": paper["paper_id"], "rubric_id": paper["rubric_id"], "rubric_version": resolved["rubric_version"],
        "judge_pool": "none", "cache_key": cache_key,
        "accuracy": 0, "coverage": 0, "critical": 0, "sources": 0, "structure": 0, "action": 0,
        "overall": 0, "disagreement": 0, "confidence": "none", "factual_confidence": "none",
        "fact_errors": ["Final synthesis produced no usable text"], "notes": "Unusable synthesis; not sent to judges.",
        "report_usable": False, "cost_usd": 0, "calibration_status": "unusable", "budget_exceeded": False,
        "replace_cache_key": replace_cache_key,
    })
    return db.get_grade(gid)


def _persist_runs(paper_id: str, grade_id: str | None, results: list, prompt_hash_value: str) -> None:
    for judge, result in results:
        usage = result.get("usage") or {}
        db.save_judge_run({
            "paper_id": paper_id, "grade_id": grade_id, "judge_id": judge["id"], "judge_model": judge["model"],
            "family": judge.get("family"), "raw_response": result.get("raw", ""),
            "scores": result.get("scores"), "overall": result.get("overall"),
            "parse_ok": bool(result.get("ok")), "error": result.get("error"),
            "latency_ms": result.get("latency_ms"),
            "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
            "cost_usd": usage.get("cost"), "prompt_hash": prompt_hash_value, "temperature": 0.1,
        })


def grade(paper_id: str, force: bool = False) -> dict:
    paper = db.get_paper(paper_id)
    if not paper:
        raise RuntimeError(f"paper not found: {paper_id}")
    resolved = resolve(query_text=paper["query_text"], query_id=paper["query_id"], rubric_id=paper["rubric_id"])
    cache_key = _cache_key(paper, resolved)
    db.update_paper_rubric(paper_id, resolved["rubric_id"], resolved["rubric_version"])
    if not force:
        cached = db.get_grade_by_cache(cache_key)
        if cached:
            db.audit("service", "grade.cache_hit", {"paper_id": paper_id, "grade_id": cached["grade_id"]})
            return cached
    if not paper["report_usable"]:
        g = failure_grade(paper, cache_key, resolved, replace_cache_key=force)
        db.audit("service", "grade.unusable", {"paper_id": paper_id, "grade_id": g["grade_id"]})
        return g

    try:
        out = judge_paper(paper_id, paper["report_text"], resolved)
    except JudgeFailure as e:
        _persist_runs(paper_id, None, e.results, e.prompt_hash)
        raise
    cost = round(sum((r.get("usage") or {}).get("cost") or 0 for _, r in out["judge_results"]), 6)
    out["cost_usd"] = cost
    out["calibration_status"] = resolved["calibration_status"]
    out["budget_exceeded"] = bool(out.get("budget_exceeded") or cost > float(load_config().get("max_cost_per_grade_usd", 0.05)))
    gid = db.save_grade({
        **out,
        "paper_id": paper_id,
        "cache_key": cache_key,
        "report_usable": True,
        "replace_cache_key": force,
    })
    _persist_runs(paper_id, gid, out["judge_results"], out["prompt_hash"])
    db.audit("service", "grade.completed", {"paper_id": paper_id, "grade_id": gid, "overall": out["overall"]})
    try:
        from .charts import render_chart
        with _CHART_LOCK:
            render_chart()
    except Exception:
        pass
    return db.get_grade(gid)
