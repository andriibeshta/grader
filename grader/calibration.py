"""Provisional calibration harness for the local grader service.

Anchors are **provisional**, not human truth. They exist so that current
service grades can be checked for drift against previous benchmark scores or
early manual reviews. A later human-scored set should replace them.

The harness is deliberately read-only with respect to ``grades.db``:

* it connects with SQLite ``mode=ro`` plus ``PRAGMA query_only=ON``;
* it never imports the service pipeline, judges, or any network client;
* it only needs ``papers``, ``grades``, and (optionally) ``judge_runs``.

Usage::

    python3 -m grader.calibration \\
        --anchors calibration/human_scores.example.yaml \\
        --db grades.db --out calibration/report.json

    # Convert the old LDR benchmark scores into provisional anchors
    python3 -m grader.calibration \\
        --import-benchmark ../ldr-benchmark/data/quality_scores.json \\
        --write-anchors calibration/benchmark.anchors.yaml

Anchor file format (YAML subset or JSON)::

    - paper_id: "6c6e08..."        # exact papers.paper_id, OR
      query_id: "Q4"               # query_id + model_id (+ optional title)
      model_id: "qwen3.8-27b-q4_k_s"
      title: "Benchmark Q4 / qwen3.8-27b-q4_k_s"
      label: "Q4 qwen3.8 q4_k_s"
      source: "calibration.json manual overall"
      expected:
        overall: 9.1
        accuracy: 7.5
        coverage: 8.5
        critical: 8.5
        sources: 6.5
        structure: 8.5
        action: 6.5

``overall`` is required; criterion scores are optional but recommended.
The YAML reader intentionally supports only a small, strict subset: comments,
a top-level sequence of mappings, nested mappings, and quoted/bare scalars.
JSON is the escape hatch for anything richer. PyYAML is not required.

Bias sign: ``actual - expected``, so a positive bias means the service scores
higher than the anchor.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

CRITERIA = ("accuracy", "coverage", "critical", "sources", "structure", "action")
SCORE_FIELDS = ("overall",) + CRITERIA
DEFAULT_DB = Path(__file__).resolve().parent.parent / "grades.db"
MAX_SCORE = 10.0

_ANCHOR_KEYS = {"paper_id", "query_id", "model_id", "title", "label", "id", "expected", "source", "notes"}
_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_INT_RE = re.compile(r"^[-+]?\d+$")
_FLOAT_RE = re.compile(r"^[-+]?(?:\d+\.\d*|\.\d+)(?:[eE][-+]?\d+)?$")


class CalibrationError(ValueError):
    """Raised for invalid anchors, databases, or unsupported YAML."""


class YamlSubsetError(CalibrationError):
    """Raised when a YAML file steps outside the supported subset."""


# ---------------------------------------------------------------------------
# Minimal YAML reader (no PyYAML dependency)
# ---------------------------------------------------------------------------

def _strip_comment(line: str) -> str:
    out: list[str] = []
    quote_char: str | None = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote_char:
            out.append(ch)
            if quote_char == '"' and ch == "\\" and i + 1 < len(line):
                out.append(line[i + 1])
                i += 2
                continue
            if ch == quote_char:
                quote_char = None
            i += 1
            continue
        if ch in "\"'":
            quote_char = ch
            out.append(ch)
            i += 1
            continue
        if ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        out.append(ch)
        i += 1
    return "".join(out).rstrip()


def _split_key(text: str, lineno: int) -> tuple[str, str]:
    quote_char: str | None = None
    for i, ch in enumerate(text):
        if quote_char:
            if ch == quote_char:
                quote_char = None
            continue
        if ch in "\"'":
            quote_char = ch
        elif ch == ":":
            key = text[:i].strip()
            value = text[i + 1:].strip()
            if not _KEY_RE.match(key):
                raise YamlSubsetError(f"line {lineno}: invalid key {key!r}")
            return key, value
    raise YamlSubsetError(f"line {lineno}: expected 'key: value', got {text!r}")


def _scalar(text: str, lineno: int):
    t = text.strip()
    if t == "":
        return None
    if t[0] == '"' or t[0] == "'":
        if len(t) < 2 or t[-1] != t[0]:
            raise YamlSubsetError(f"line {lineno}: unterminated quoted string")
        if t[0] == '"':
            try:
                return json.loads(t)
            except json.JSONDecodeError:
                return t[1:-1].replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
        return t[1:-1].replace("''", "'")
    if t[0] in "[{":
        raise YamlSubsetError(f"line {lineno}: flow collections are not supported; use a JSON anchor file")
    if t[0] in "|>":
        raise YamlSubsetError(f"line {lineno}: block scalars are not supported")
    if t[0] in "&*":
        raise YamlSubsetError(f"line {lineno}: YAML anchors/aliases are not supported")
    low = t.lower()
    if low in ("null", "~"):
        return None
    if low == "true":
        return True
    if low == "false":
        return False
    if _INT_RE.match(t):
        return int(t)
    if _FLOAT_RE.match(t):
        return float(t)
    return t


def _assign(mapping: dict, key: str, value, lineno: int) -> None:
    if key in mapping:
        raise YamlSubsetError(f"line {lineno}: duplicate key {key!r}")
    mapping[key] = value


def parse_simple_yaml(text: str, source: str = "<yaml>") -> list[dict]:
    """Parse the strict YAML subset used by anchor files.

    Supported: comments, ``---`` markers, a top-level ``- `` sequence of
    mappings, one or more nested mappings, and quoted or bare scalars.
    Unsupported constructs fail loudly instead of being silently misread.
    """
    root: list[dict] = []
    current: dict | None = None
    stack: list[tuple[int, dict]] = []
    pending: tuple[int, dict] | None = None

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw)
        if not line.strip() or line.strip() in ("---", "..."):
            continue
        leading = line[: len(line) - len(line.lstrip(" \t"))]
        if "\t" in leading:
            raise YamlSubsetError(f"{source}: line {lineno}: tabs are not allowed for indentation")
        indent = len(leading)
        stripped = line.strip()

        if stripped == "-" or stripped.startswith("- "):
            if indent != 0:
                raise YamlSubsetError(f"{source}: line {lineno}: anchors must be a top-level '- ' list")
            current = {}
            root.append(current)
            stack = [(2, current)]
            pending = None
            rest = stripped[1:].strip()
            if not rest:
                continue
            key, value = _split_key(rest, lineno)
            if value == "":
                nested: dict = {}
                _assign(current, key, nested, lineno)
                pending = (2, nested)
            else:
                _assign(current, key, _scalar(value, lineno), lineno)
            continue

        if current is None:
            raise YamlSubsetError(f"{source}: line {lineno}: expected a '- ' anchor entry first")
        key, value = _split_key(stripped, lineno)

        if pending is not None:
            pending_indent, pending_map = pending
            if indent <= pending_indent:
                raise YamlSubsetError(f"{source}: line {lineno}: expected an indented mapping")
            stack.append((indent, pending_map))
            pending = None
        else:
            while stack and stack[-1][0] > indent:
                stack.pop()
            if not stack or stack[-1][0] != indent:
                raise YamlSubsetError(f"{source}: line {lineno}: unexpected indentation ({indent})")

        target = stack[-1][1]
        if value == "":
            nested = {}
            _assign(target, key, nested, lineno)
            pending = (indent, nested)
        else:
            _assign(target, key, _scalar(value, lineno), lineno)

    if pending is not None:
        raise YamlSubsetError(f"{source}: file ended while a nested mapping was expected")
    return root


# ---------------------------------------------------------------------------
# Anchor loading and validation
# ---------------------------------------------------------------------------

def _num(value, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CalibrationError(f"{where}: expected a number, got {value!r}")
    number = float(value)
    if not 0.0 <= number <= MAX_SCORE:
        raise CalibrationError(f"{where}: score {number} is outside 0..{MAX_SCORE:g}")
    return number


def normalize_anchor(raw: dict, index: int = 1) -> dict:
    """Validate one anchor and return a normalized copy."""
    if not isinstance(raw, dict):
        raise CalibrationError(f"anchor {index}: expected a mapping, got {type(raw).__name__}")
    unknown = set(raw) - _ANCHOR_KEYS
    if unknown:
        raise CalibrationError(f"anchor {index}: unknown field(s): {', '.join(sorted(unknown))}")

    expected_raw = raw.get("expected")
    if not isinstance(expected_raw, dict):
        raise CalibrationError(f"anchor {index}: 'expected' must be a mapping")
    unknown_expected = set(expected_raw) - set(SCORE_FIELDS)
    if unknown_expected:
        raise CalibrationError(
            f"anchor {index}: unknown expected field(s): {', '.join(sorted(unknown_expected))}"
        )
    expected = {k: _num(v, f"anchor {index} expected.{k}") for k, v in expected_raw.items()}
    if "overall" not in expected:
        raise CalibrationError(f"anchor {index}: expected.overall is required")

    def text_field(name: str) -> str | None:
        value = raw.get(name)
        if value is None:
            return None
        if not isinstance(value, str):
            raise CalibrationError(f"anchor {index}: {name} must be a string")
        value = value.strip()
        return value or None

    paper_id = text_field("paper_id")
    query_id = text_field("query_id")
    model_id = text_field("model_id")
    title = text_field("title")
    if not paper_id and not (query_id and model_id):
        raise CalibrationError(
            f"anchor {index}: provide paper_id, or both query_id and model_id"
        )

    label = text_field("label") or text_field("id") or paper_id or f"{query_id}/{model_id}"
    return {
        "label": label,
        "paper_id": paper_id,
        "query_id": query_id,
        "model_id": model_id,
        "title": title,
        "expected": expected,
        "source": text_field("source"),
        "notes": text_field("notes"),
    }


def normalize_anchors(raw_anchors) -> list[dict]:
    if isinstance(raw_anchors, dict):
        if "anchors" in raw_anchors:
            raw_anchors = raw_anchors["anchors"]
        else:
            raise CalibrationError("JSON anchor object must contain an 'anchors' list")
    if not isinstance(raw_anchors, list):
        raise CalibrationError("anchor file must contain a list of anchor mappings")
    if not raw_anchors:
        raise CalibrationError("anchor file contains no anchors")
    return [normalize_anchor(item, i) for i, item in enumerate(raw_anchors, 1)]


def load_anchors(path: str | Path) -> list[dict]:
    """Load anchors from a JSON or supported-YAML file."""
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"anchor file not found: {p}")
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() == ".json":
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CalibrationError(f"invalid JSON in {p}: {exc}") from exc
    else:
        raw = parse_simple_yaml(text, source=str(p))
    return normalize_anchors(raw)


# ---------------------------------------------------------------------------
# Read-only SQLite access
# ---------------------------------------------------------------------------

def _connect_read_only(db_path: str | Path) -> sqlite3.Connection:
    p = Path(db_path).expanduser().resolve()
    if not p.exists():
        raise FileNotFoundError(f"database not found: {p}")
    uri = f"file:{quote(p.as_posix(), safe='/')}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise CalibrationError(f"cannot open database read-only ({p}): {exc}") from exc
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _load_rows(connection: sqlite3.Connection, table: str) -> list[dict]:
    if not _table_exists(connection, table):
        return []
    return [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]


def _sort_key(row: dict) -> tuple[str, str]:
    return (str(row.get("created_at") or ""), str(row.get("paper_id") or row.get("grade_id") or ""))


def _load_grades(connection: sqlite3.Connection) -> dict[str, list[dict]]:
    by_paper: dict[str, list[dict]] = {}
    for row in _load_rows(connection, "grades"):
        by_paper.setdefault(str(row.get("paper_id")), []).append(row)
    for rows in by_paper.values():
        rows.sort(key=_sort_key)
    return by_paper


def _load_judge_runs(connection: sqlite3.Connection) -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    by_grade: dict[str, list[dict]] = {}
    by_paper: dict[str, list[dict]] = {}
    for row in _load_rows(connection, "judge_runs"):
        grade_id = row.get("grade_id")
        if grade_id:
            by_grade.setdefault(str(grade_id), []).append(row)
        paper_id = row.get("paper_id")
        if paper_id:
            by_paper.setdefault(str(paper_id), []).append(row)
    return by_grade, by_paper


def _paper_index(papers: list[dict]) -> tuple[dict[str, dict], dict[tuple[str, str], list[dict]]]:
    ordered = sorted(papers, key=lambda p: (str(p.get("created_at") or ""), str(p.get("paper_id") or "")))
    by_id: dict[str, dict] = {}
    by_query_model: dict[tuple[str, str], list[dict]] = {}
    for paper in ordered:
        paper_id = paper.get("paper_id")
        if paper_id:
            by_id[str(paper_id)] = paper
        query_id = paper.get("query_id")
        model_id = paper.get("model_id")
        if query_id and model_id:
            by_query_model.setdefault((str(query_id), str(model_id)), []).append(paper)
    return by_id, by_query_model


def _normal_title(value) -> str:
    return " ".join(str(value or "").split()).casefold()


def _match_anchor(
    anchor: dict,
    by_id: dict[str, dict],
    by_query_model: dict[tuple[str, str], list[dict]],
) -> tuple[dict | None, str | None, str | None, int]:
    """Return (paper, missing_reason, matched_by, extra_ambiguous_candidates)."""
    if anchor["paper_id"]:
        paper = by_id.get(anchor["paper_id"])
        if paper is None:
            return None, "paper_id not found", None, 0
        return paper, None, "paper_id", 0

    candidates = list(by_query_model.get((anchor["query_id"], anchor["model_id"]), []))
    if anchor["title"]:
        wanted = _normal_title(anchor["title"])
        candidates = [p for p in candidates if _normal_title(p.get("title")) == wanted]
        reason = "no paper matches query_id+model_id+title"
        matched_by = "query_model_title"
    else:
        reason = "no paper matches query_id+model_id"
        matched_by = "query_model"
    if not candidates:
        return None, reason, None, 0
    return candidates[-1], None, matched_by, len(candidates) - 1


# ---------------------------------------------------------------------------
# Judge spread
# ---------------------------------------------------------------------------

def _scores_from_run(run: dict) -> dict[str, float]:
    try:
        raw = json.loads(run.get("scores_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    result: dict[str, float] = {}
    for criterion in CRITERIA:
        value = raw.get(criterion)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            result[criterion] = float(value)
    return result


def _grade_spread(
    grade: dict,
    runs_by_grade: dict[str, list[dict]],
    runs_by_paper: dict[str, list[dict]],
) -> dict:
    grade_id = grade.get("grade_id")
    runs = list(runs_by_grade.get(str(grade_id), [])) if grade_id else []
    if not runs:
        # Older rows may have judge_runs without a grade_id; paper-level is a
        # best-effort fallback only when the grade itself has no linked runs.
        runs = list(runs_by_paper.get(str(grade.get("paper_id")), []))
    parsed = [r for r in runs if int(r.get("parse_ok") or 0) == 1]

    overalls = [
        float(r["overall"])
        for r in parsed
        if isinstance(r.get("overall"), (int, float)) and not isinstance(r.get("overall"), bool)
    ]
    criteria_spread: dict[str, float] = {}
    for criterion in CRITERIA:
        values = [s[criterion] for s in (_scores_from_run(r) for r in parsed) if criterion in s]
        if len(values) >= 2:
            criteria_spread[criterion] = _round(max(values) - min(values))

    return {
        "n_judges": len(parsed),
        "judge_ids": [str(r.get("judge_id")) for r in parsed if r.get("judge_id")],
        "overall": _round(max(overalls) - min(overalls)) if len(overalls) >= 2 else None,
        "criteria": criteria_spread,
        "stored_disagreement": _round(grade["disagreement"])
        if isinstance(grade.get("disagreement"), (int, float))
        else None,
    }


# ---------------------------------------------------------------------------
# Comparison and metrics
# ---------------------------------------------------------------------------

def _round(value: float) -> float:
    rounded = round(float(value), 4)
    return 0.0 if rounded == 0 else rounded


def _mean(values: list[float]) -> float | None:
    return _round(statistics.fmean(values)) if values else None


def _build_row(anchor: dict, paper: dict, grade: dict, matched_by: str | None, spread: dict) -> dict:
    actual = {field: float(grade[field]) for field in SCORE_FIELDS if isinstance(grade.get(field), (int, float))}
    expected = dict(anchor["expected"])
    common = [field for field in SCORE_FIELDS if field in expected and field in actual]
    diff = {field: _round(actual[field] - expected[field]) for field in common}
    abs_error = {field: _round(abs(diff[field])) for field in common}
    return {
        "label": anchor["label"],
        "status": "matched",
        "matched_by": matched_by,
        "paper_id": paper.get("paper_id"),
        "title": paper.get("title"),
        "query_id": paper.get("query_id"),
        "model_id": paper.get("model_id"),
        "grade_id": grade.get("grade_id"),
        "grade_created_at": grade.get("created_at"),
        "expected": expected,
        "actual": actual,
        "diff": diff,
        "abs_error": abs_error,
        "judge_spread": spread,
        "source": anchor.get("source"),
        "notes": anchor.get("notes"),
    }


def _missing_row(anchor: dict, reason: str) -> dict:
    return {
        "label": anchor["label"],
        "status": "missing",
        "reason": reason,
        "paper_id": anchor["paper_id"],
        "query_id": anchor["query_id"],
        "model_id": anchor["model_id"],
        "title": anchor["title"],
        "expected": anchor["expected"],
        "source": anchor.get("source"),
    }


def _summarize(rows: list[dict]) -> dict:
    mae: dict[str, float | None] = {}
    bias: dict[str, float | None] = {}
    counts: dict[str, int] = {}
    for field in SCORE_FIELDS:
        errors = [row["diff"][field] for row in rows if field in row["diff"]]
        mae[field] = _mean([abs(e) for e in errors])
        bias[field] = _mean(errors)
        counts[field] = len(errors)

    # Inter-judge spread is aggregated over unique matched grades so that an
    # anchor repeated with a different label does not double-count a run.
    seen: set[str] = set()
    overall_spreads: list[float] = []
    criterion_spreads: dict[str, list[float]] = {c: [] for c in CRITERIA}
    disagreements: list[float] = []
    grades_with_spread = 0
    for row in rows:
        gid = row.get("grade_id") or f"{row.get('paper_id')}@{row.get('grade_created_at')}"
        if gid in seen:
            continue
        seen.add(str(gid))
        spread = row.get("judge_spread") or {}
        if isinstance(spread.get("overall"), (int, float)):
            grades_with_spread += 1
            overall_spreads.append(float(spread["overall"]))
        for criterion, value in (spread.get("criteria") or {}).items():
            if criterion in criterion_spreads and isinstance(value, (int, float)):
                criterion_spreads[criterion].append(float(value))
        if isinstance(spread.get("stored_disagreement"), (int, float)):
            disagreements.append(float(spread["stored_disagreement"]))

    return {
        "mae": mae,
        "bias": bias,
        "n": counts,
        "inter_judge_spread": {
            "grades_with_spread": grades_with_spread,
            "unique_grades": len(seen),
            "overall_mean": _mean(overall_spreads),
            "overall_max": _round(max(overall_spreads)) if overall_spreads else None,
            "criteria_mean": {c: _mean(v) for c, v in criterion_spreads.items()},
            "criteria_max": {c: (_round(max(v)) if v else None) for c, v in criterion_spreads.items()},
            "stored_disagreement_mean": _mean(disagreements),
        },
    }


def compare(
    anchors,
    db_path: str | Path = DEFAULT_DB,
    *,
    all_grades: bool = False,
    anchors_file: str | Path | None = None,
) -> dict:
    """Compare normalized anchors against grades stored in SQLite (read-only).

    By default only the newest grade per paper is used. ``all_grades=True``
    reports every stored grade for each matched paper.
    """
    normalized = normalize_anchors(anchors)
    resolved_db = Path(db_path).expanduser().resolve()
    connection = _connect_read_only(resolved_db)
    try:
        if not _table_exists(connection, "papers"):
            raise CalibrationError(f"database has no 'papers' table: {resolved_db}")
        if not _table_exists(connection, "grades"):
            raise CalibrationError(f"database has no 'grades' table: {resolved_db}")
        papers = _load_rows(connection, "papers")
        grades_by_paper = _load_grades(connection)
        runs_by_grade, runs_by_paper = _load_judge_runs(connection)
    finally:
        connection.close()

    by_id, by_query_model = _paper_index(papers)
    rows: list[dict] = []
    missing: list[dict] = []
    warnings: list[str] = []
    ambiguous = 0
    ungraded = 0
    matched_anchors = 0

    seen_keys: dict[tuple, int] = {}
    for index, anchor in enumerate(normalized, 1):
        key = (
            ("paper_id", anchor["paper_id"])
            if anchor["paper_id"]
            else ("qm", anchor["query_id"], anchor["model_id"], _normal_title(anchor["title"]))
        )
        if key in seen_keys:
            warnings.append(f"anchor {index} duplicates anchor {seen_keys[key]} ({anchor['label']})")
        else:
            seen_keys[key] = index
        absent = [c for c in CRITERIA if c not in anchor["expected"]]
        if absent:
            warnings.append(f"anchor {index} ({anchor['label']}): no expected value for {', '.join(absent)}")

        paper, reason, matched_by, extra = _match_anchor(anchor, by_id, by_query_model)
        if paper is None:
            missing.append(_missing_row(anchor, reason or "no matching paper"))
            continue

        paper_grades = grades_by_paper.get(str(paper.get("paper_id")), [])
        if not paper_grades:
            ungraded += 1
            missing.append(_missing_row(anchor, "paper matched but has no grade"))
            continue

        selected = paper_grades if all_grades else paper_grades[-1:]
        matched_anchors += 1
        ambiguous += extra
        for grade in selected:
            spread = _grade_spread(grade, runs_by_grade, runs_by_paper)
            rows.append(_build_row(anchor, paper, grade, matched_by, spread))

    graded_rows = [row for row in rows if row["status"] == "matched"]
    summary = _summarize(graded_rows)
    matched_row_counts = {
        field: summary["n"][field] for field in SCORE_FIELDS
    }

    return {
        "harness": "grader.calibration",
        "report_version": 1,
        "provisional": True,
        "interpretation": (
            "Anchors are provisional benchmark or manual scores, not human truth. "
            "bias = mean(actual - expected); positive means the service over-scores."
        ),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "anchors_file": str(Path(anchors_file).expanduser().resolve()) if anchors_file else None,
        "db": str(resolved_db),
        "db_access": "read-only",
        "latest_grade_only": not all_grades,
        "counts": {
            "anchors": len(normalized),
            "matched": matched_anchors,
            "missing": len(missing),
            "ambiguous_matches": ambiguous,
            "papers_matched_but_ungraded": ungraded,
            "rows": len(graded_rows),
            "scored_per_field": matched_row_counts,
        },
        "score_fields": list(SCORE_FIELDS),
        "mae": summary["mae"],
        "bias": summary["bias"],
        "n": summary["n"],
        "inter_judge_spread": summary["inter_judge_spread"],
        "rows": rows,
        "missing": missing,
        "warnings": warnings,
    }


def format_report(report: dict) -> str:
    """Render a compact, terminal-readable calibration summary."""
    counts = report["counts"]
    lines: list[str] = [
        "Calibration report — provisional anchors, not human truth",
        f"DB      : {report['db']} ({report['db_access']})",
        f"Anchors : {report.get('anchors_file') or '<in-memory>'}",
        (
            f"Counts  : {counts['anchors']} anchors | {counts['matched']} matched | "
            f"{counts['missing']} missing | {counts['ambiguous_matches']} ambiguous | "
            f"{counts['rows']} rows"
        ),
        "",
        f"{'score':<12}{'MAE':>9}{'bias':>9}{'n':>5}",
    ]
    for field in report["score_fields"]:
        mae = report["mae"].get(field)
        bias = report["bias"].get(field)
        lines.append(
            f"{field:<12}{(f'{mae:.4f}' if mae is not None else '-'):>9}"
            f"{(f'{bias:+.4f}' if bias is not None else '-'):>9}{report['n'].get(field, 0):>5}"
        )

    spread = report["inter_judge_spread"]
    lines.append("")
    if spread["grades_with_spread"]:
        lines.append(
            "Inter-judge spread (unique grades with >=2 parsed judges: "
            f"{spread['grades_with_spread']}/{spread['unique_grades']}): "
            f"overall mean {spread['overall_mean']:.2f}, max {spread['overall_max']:.2f}"
        )
        crit = [
            f"{field} {value:.2f}"
            for field, value in spread["criteria_mean"].items()
            if value is not None
        ]
        if crit:
            lines.append("  criterion mean spread: " + ", ".join(crit))
    else:
        lines.append("Inter-judge spread: unavailable (no matched grade has >=2 parsed judge runs)")

    lines.append("")
    lines.append(f"{'anchor':<34}{'status':<9}{'exp':>7}{'act':>7}{'diff':>8}  match")
    for row in report["rows"]:
        expected = row["expected"].get("overall")
        actual = row.get("actual", {}).get("overall")
        diff = row.get("diff", {}).get("overall")
        lines.append(
            f"{row['label'][:33]:<34}{row['status']:<9}"
            f"{(f'{expected:.2f}' if expected is not None else '-'):>7}"
            f"{(f'{actual:.2f}' if actual is not None else '-'):>7}"
            f"{(f'{diff:+.2f}' if diff is not None else '-'):>8}  {row.get('matched_by') or '-'}"
        )
    if report["missing"]:
        lines.append("")
        lines.append("Missing anchors:")
        for row in report["missing"]:
            lines.append(f"  - {row['label']}: {row['reason']}")
    if report["warnings"]:
        lines.append("")
        lines.append("Warnings:")
        for warning in report["warnings"]:
            lines.append(f"  - {warning}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Legacy benchmark importer
# ---------------------------------------------------------------------------

def import_benchmark(path: str | Path) -> list[dict]:
    """Convert old benchmark scores into provisional anchors.

    Handles two shapes:

    * ``data/quality_scores.json`` — ``{query: {model: {"scores": {...}}}}``
    * ``benchmark_grades.json`` — a list of rows with ``query``/``model`` and
      score fields (and optionally ``paper_id``)

    Rows explicitly marked unusable or not completed are skipped. These are
    anchors from benchmark judges, **not** human truth.
    """
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"benchmark file not found: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CalibrationError(f"invalid JSON in {p}: {exc}") from exc

    anchors: list[dict] = []
    skipped: list[str] = []

    def add(query_id: str, model_id: str, scores: dict, entry: dict, paper_id: str | None = None) -> None:
        expected = {}
        for field in SCORE_FIELDS:
            value = scores.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                expected[field] = float(value)
        if "overall" not in expected:
            skipped.append(f"{query_id}/{model_id}: no overall score")
            return
        anchor = {
            "label": f"{query_id}/{model_id}",
            "query_id": query_id,
            "model_id": model_id,
            "expected": expected,
            "source": f"imported benchmark: {p.name} (provisional, not human truth)",
        }
        if paper_id:
            anchor["paper_id"] = str(paper_id)
        title = entry.get("title") if isinstance(entry, dict) else None
        if title:
            anchor["title"] = str(title)
        notes = entry.get("notes") if isinstance(entry, dict) else None
        if isinstance(notes, str) and notes.strip():
            anchor["notes"] = notes.strip()
        anchors.append(normalize_anchor(anchor, len(anchors) + 1))

    if isinstance(data, dict):
        for query_id, models in data.items():
            if not isinstance(models, dict):
                raise CalibrationError(f"expected a model mapping under query {query_id!r}")
            for model_id, entry in models.items():
                if not isinstance(entry, dict):
                    raise CalibrationError(f"expected a mapping for {query_id}/{model_id}")
                scores = entry.get("scores")
                if not isinstance(scores, dict):
                    raise CalibrationError(f"missing 'scores' mapping for {query_id}/{model_id}")
                if entry.get("report_usable") is False:
                    skipped.append(f"{query_id}/{model_id}: report_usable=false")
                    continue
                add(str(query_id), str(model_id), scores, entry)
    elif isinstance(data, list):
        for index, entry in enumerate(data, 1):
            if not isinstance(entry, dict):
                raise CalibrationError(f"row {index}: expected a mapping")
            status = entry.get("status")
            if status is not None and status != "completed":
                skipped.append(f"row {index}: status={status}")
                continue
            if entry.get("report_usable") in (0, False):
                skipped.append(f"row {index}: report_usable={entry.get('report_usable')}")
                continue
            query_id = entry.get("query") or entry.get("query_id")
            model_id = entry.get("model") or entry.get("model_id")
            if not query_id or not model_id:
                raise CalibrationError(f"row {index}: missing query/model")
            scores = entry.get("scores") if isinstance(entry.get("scores"), dict) else entry
            if not isinstance(scores, dict):
                raise CalibrationError(f"row {index}: expected a scores mapping")
            add(str(query_id), str(model_id), scores, entry, paper_id=entry.get("paper_id"))
    else:
        raise CalibrationError("benchmark file must contain an object or a list")

    if skipped:
        print(
            "skipped " + str(len(skipped)) + " benchmark row(s): " + "; ".join(skipped),
            file=sys.stderr,
        )
    if not anchors:
        raise CalibrationError("no usable benchmark rows found")
    return anchors


def _format_number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


def anchors_to_yaml(anchors: list[dict]) -> str:
    """Serialize anchors into the YAML subset accepted by load_anchors."""
    lines = [
        "# Provisional anchors generated by grader.calibration.",
        "# Benchmark scores are not human truth; replace them before making claims.",
        "",
    ]
    for anchor in anchors:
        first = True
        for key in ("label", "paper_id", "query_id", "model_id", "title", "source", "notes"):
            value = anchor.get(key)
            if value is None:
                continue
            prefix = "- " if first else "  "
            lines.append(f"{prefix}{key}: {json.dumps(str(value), ensure_ascii=False)}")
            first = False
        lines.append(f"{'  ' if not first else '- '}expected:")
        for field in SCORE_FIELDS:
            value = anchor["expected"].get(field)
            if value is not None:
                lines.append(f"    {field}: {_format_number(value)}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m grader.calibration",
        description=(
            "Compare provisional anchor scores against grades stored in the local "
            "grader SQLite database (read-only)."
        ),
        epilog=(
            "Example: python3 -m grader.calibration "
            "--anchors calibration/human_scores.example.yaml --db grades.db "
            "--out calibration/report.json"
        ),
    )
    parser.add_argument("--anchors", metavar="PATH", help="YAML or JSON anchor file")
    parser.add_argument("--db", metavar="PATH", default=str(DEFAULT_DB), help="SQLite database (read-only)")
    parser.add_argument("--out", metavar="PATH", help="write the JSON report to this path")
    parser.add_argument(
        "--format", choices=("text", "json"), default="text", help="stdout format (default: text)"
    )
    parser.add_argument(
        "--all-grades",
        action="store_true",
        help="compare against every stored grade instead of the newest grade per paper",
    )
    parser.add_argument(
        "--strict", action="store_true", help="exit 1 when any anchor is missing or ambiguous"
    )
    parser.add_argument("--quiet", action="store_true", help="suppress human-readable stdout")
    parser.add_argument(
        "--import-benchmark",
        metavar="PATH",
        help="convert old benchmark quality_scores.json/benchmark_grades.json into anchors",
    )
    parser.add_argument(
        "--write-anchors",
        metavar="PATH",
        help="with --import-benchmark, write generated YAML anchors here instead of stdout",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    try:
        if args.import_benchmark:
            anchors = import_benchmark(args.import_benchmark)
            yaml_text = anchors_to_yaml(anchors)
            if args.write_anchors:
                out = Path(args.write_anchors).expanduser()
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(yaml_text, encoding="utf-8")
                if not args.quiet:
                    print(f"wrote {len(anchors)} provisional anchors to {out}")
            elif not args.quiet:
                print(yaml_text, end="")
            return 0

        if not args.anchors:
            _parser().error("--anchors is required unless --import-benchmark is used")

        anchors = load_anchors(args.anchors)
        report = compare(anchors, args.db, all_grades=args.all_grades, anchors_file=args.anchors)

        if args.out:
            out = Path(args.out).expanduser()
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

        if not args.quiet:
            if args.format == "json":
                print(json.dumps(report, indent=2, ensure_ascii=False))
            else:
                print(format_report(report))
                if args.out:
                    print(f"\nJSON report written to {Path(args.out).expanduser()}")

        if args.strict and (report["counts"]["missing"] or report["counts"]["ambiguous_matches"]):
            return 1
        return 0
    except (CalibrationError, FileNotFoundError, OSError, sqlite3.Error) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
