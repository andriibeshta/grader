"""SQLite storage for papers, jobs, grades, and judge runs."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import DB_PATH


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.execute("PRAGMA busy_timeout=5000")
    c.execute("PRAGMA foreign_keys=ON")
    return c


def checkpoint() -> None:
    with conn() as c:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS papers (
          paper_id TEXT PRIMARY KEY,
          paper_key TEXT UNIQUE NOT NULL,
          ldr_job_id TEXT UNIQUE,
          source_type TEXT NOT NULL,
          source_ref TEXT,
          title TEXT,
          query_id TEXT,
          query_text TEXT,
          rubric_id TEXT NOT NULL,
          rubric_version TEXT NOT NULL,
          content_hash TEXT NOT NULL,
          report_text TEXT NOT NULL,
          report_path TEXT,
          report_usable INTEGER NOT NULL DEFAULT 1,
          word_count INTEGER,
          model_id TEXT,
          model_key TEXT,
          metadata_json TEXT,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS jobs (
          job_id TEXT PRIMARY KEY,
          type TEXT NOT NULL,
          paper_id TEXT,
          status TEXT NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0,
          error TEXT,
          result_grade_id TEXT,
          created_at TEXT NOT NULL,
          started_at TEXT,
          finished_at TEXT
        );
        CREATE TABLE IF NOT EXISTS grades (
          grade_id TEXT PRIMARY KEY,
          paper_id TEXT NOT NULL REFERENCES papers(paper_id),
          rubric_id TEXT NOT NULL,
          rubric_version TEXT NOT NULL,
          judge_pool TEXT NOT NULL,
          cache_key TEXT UNIQUE NOT NULL,
          accuracy REAL NOT NULL,
          coverage REAL NOT NULL,
          critical REAL NOT NULL,
          sources REAL NOT NULL,
          structure REAL NOT NULL,
          action REAL NOT NULL,
          overall REAL NOT NULL,
          disagreement REAL,
          confidence TEXT,
          factual_confidence TEXT,
          fact_errors_json TEXT,
          notes TEXT,
          report_usable INTEGER NOT NULL,
          is_current INTEGER NOT NULL DEFAULT 1,
          cost_usd REAL,
          calibration_status TEXT NOT NULL DEFAULT 'provisional',
          budget_exceeded INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS judge_runs (
          run_id TEXT PRIMARY KEY,
          paper_id TEXT NOT NULL REFERENCES papers(paper_id),
          grade_id TEXT,
          judge_id TEXT NOT NULL,
          judge_model TEXT NOT NULL,
          family TEXT,
          raw_response TEXT,
          scores_json TEXT,
          overall REAL,
          parse_ok INTEGER NOT NULL DEFAULT 0,
          error TEXT,
          latency_ms INTEGER,
          prompt_tokens INTEGER,
          completion_tokens INTEGER,
          cost_usd REAL,
          prompt_hash TEXT,
          temperature REAL,
          served_provider TEXT,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS audit (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          actor TEXT,
          action TEXT NOT NULL,
          payload_json TEXT,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_papers_job ON papers(ldr_job_id);
        CREATE INDEX IF NOT EXISTS idx_grades_paper ON grades(paper_id);
        CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
        CREATE INDEX IF NOT EXISTS idx_judge_paper ON judge_runs(paper_id);
        CREATE TABLE IF NOT EXISTS judge_health (
          judge_id TEXT PRIMARY KEY,
          consecutive_failures INTEGER NOT NULL DEFAULT 0,
          last_error TEXT,
          last_failure_at TEXT,
          cooldown_until TEXT,
          last_success_at TEXT,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS batches (
          batch_id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          created_at TEXT NOT NULL,
          finished_at TEXT
        );
        CREATE TABLE IF NOT EXISTS batch_items (
          batch_id TEXT NOT NULL,
          paper_id TEXT NOT NULL,
          job_id TEXT,
          PRIMARY KEY(batch_id,paper_id)
        );
        """)
        # Additive migrations for existing databases.
        grade_cols = {row[1] for row in c.execute("PRAGMA table_info(grades)")}
        if "is_current" not in grade_cols:
            c.execute("ALTER TABLE grades ADD COLUMN is_current INTEGER NOT NULL DEFAULT 1")
        if "cost_usd" not in grade_cols:
            c.execute("ALTER TABLE grades ADD COLUMN cost_usd REAL")
        if "calibration_status" not in grade_cols:
            c.execute("ALTER TABLE grades ADD COLUMN calibration_status TEXT NOT NULL DEFAULT 'provisional'")
        if "budget_exceeded" not in grade_cols:
            c.execute("ALTER TABLE grades ADD COLUMN budget_exceeded INTEGER NOT NULL DEFAULT 0")
        # Existing databases have no current marker. Keep the newest grade per
        # paper/rubric as current before creating the partial unique index.
        if "is_current" not in grade_cols:
            papers = c.execute("SELECT DISTINCT paper_id,rubric_id FROM grades").fetchall()
            for paper_id, rubric_id in papers:
                latest = c.execute(
                    "SELECT grade_id FROM grades WHERE paper_id=? AND rubric_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                    (paper_id, rubric_id)).fetchone()
                c.execute("UPDATE grades SET is_current=0 WHERE paper_id=? AND rubric_id=?", (paper_id, rubric_id))
                if latest:
                    c.execute("UPDATE grades SET is_current=1 WHERE grade_id=?", (latest[0],))
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_grades_current ON grades(paper_id,rubric_id) WHERE is_current=1")
        c.execute("CREATE INDEX IF NOT EXISTS idx_grades_current_paper ON grades(paper_id,is_current)")
        c.execute("""UPDATE grades SET cost_usd=coalesce((
            SELECT sum(cost_usd) FROM judge_runs WHERE judge_runs.grade_id=grades.grade_id),0)
            WHERE cost_usd IS NULL""")
        judge_cols = {row[1] for row in c.execute("PRAGMA table_info(judge_runs)")}
        if "prompt_hash" not in judge_cols:
            c.execute("ALTER TABLE judge_runs ADD COLUMN prompt_hash TEXT")
        if "temperature" not in judge_cols:
            c.execute("ALTER TABLE judge_runs ADD COLUMN temperature REAL")
        if "served_provider" not in judge_cols:
            c.execute("ALTER TABLE judge_runs ADD COLUMN served_provider TEXT")


def audit(actor: str, action: str, payload: dict | None = None) -> None:
    with conn() as c:
        c.execute("INSERT INTO audit(actor,action,payload_json,created_at) VALUES(?,?,?,?)",
                  (actor, action, json.dumps(payload or {}), now()))


def create_paper(data: dict) -> tuple[dict, bool]:
    """Insert or return an existing paper. Returns (row, created)."""
    with conn() as c:
        existing = c.execute(
            "SELECT * FROM papers WHERE paper_key=? OR (ldr_job_id IS NOT NULL AND ldr_job_id=?) LIMIT 1",
            (data["paper_key"], data.get("ldr_job_id")),
        ).fetchone()
        if existing:
            return dict(existing), False
        pid = data.get("paper_id") or uuid.uuid4().hex
        c.execute("""INSERT INTO papers
            (paper_id,paper_key,ldr_job_id,source_type,source_ref,title,query_id,query_text,rubric_id,rubric_version,
             content_hash,report_text,report_path,report_usable,word_count,model_id,model_key,metadata_json,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            pid, data["paper_key"], data.get("ldr_job_id"), data["source_type"], data.get("source_ref"),
            data.get("title"), data.get("query_id"), data.get("query_text"), data["rubric_id"], data["rubric_version"],
            data["content_hash"], data["report_text"], data.get("report_path"), 1 if data.get("report_usable", True) else 0,
            data.get("word_count"), data.get("model_id"), data.get("model_key"),
            json.dumps(data.get("metadata") or {}), now(),
        ))
        row = c.execute("SELECT * FROM papers WHERE paper_id=?", (pid,)).fetchone()
        return dict(row), True


def get_paper(paper_id: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM papers WHERE paper_id=?", (paper_id,)).fetchone()
        return dict(r) if r else None


def update_paper_rubric(paper_id: str, rubric_id: str, rubric_version_value: str) -> None:
    with conn() as c:
        c.execute("UPDATE papers SET rubric_id=?, rubric_version=? WHERE paper_id=?",
                  (rubric_id, rubric_version_value, paper_id))


def get_paper_by_ldr(job_id: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM papers WHERE ldr_job_id=?", (job_id,)).fetchone()
        return dict(r) if r else None


def create_job(paper_id: str, kind: str = "grade") -> str:
    jid = uuid.uuid4().hex
    with conn() as c:
        c.execute("INSERT INTO jobs(job_id,type,paper_id,status,created_at) VALUES(?,?,?,?,?)",
                  (jid, kind, paper_id, "queued", now()))
    return jid


def update_job(job_id: str, **fields: Any) -> None:
    allowed = {"status", "attempts", "error", "result_grade_id", "started_at", "finished_at"}
    sets = [f"{k}=?" for k in fields if k in allowed]
    vals = [v for k, v in fields.items() if k in allowed]
    if not sets:
        return
    with conn() as c:
        c.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE job_id=?", (*vals, job_id))


def get_job(job_id: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return dict(r) if r else None


def jobs(status: str | None = None, limit: int = 100) -> list[dict]:
    with conn() as c:
        if status:
            rows = c.execute("SELECT * FROM jobs WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit))
        else:
            rows = c.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]


def get_judge_health(judge_id: str) -> dict:
    with conn() as c:
        r = c.execute("SELECT * FROM judge_health WHERE judge_id=?", (judge_id,)).fetchone()
        if not r:
            return {"judge_id": judge_id, "consecutive_failures": 0, "last_error": None,
                    "last_failure_at": None, "cooldown_until": None, "last_success_at": None}
        return dict(r)


def judge_available(judge_id: str) -> bool:
    h = get_judge_health(judge_id)
    until = h.get("cooldown_until")
    if not until:
        return True
    try:
        return datetime.fromisoformat(until) <= datetime.now(timezone.utc)
    except Exception:
        return True


def record_judge_success(judge_id: str) -> None:
    with conn() as c:
        c.execute("""INSERT INTO judge_health(judge_id,consecutive_failures,last_success_at,updated_at)
            VALUES(?,0,?,?)
            ON CONFLICT(judge_id) DO UPDATE SET consecutive_failures=0,last_success_at=excluded.last_success_at,
            cooldown_until=NULL,updated_at=excluded.updated_at""", (judge_id, now(), now()))


def record_judge_failure(judge_id: str, error: str, threshold: int, cooldown_seconds: int) -> None:
    current = get_judge_health(judge_id)
    failures = int(current.get("consecutive_failures") or 0) + 1
    now_dt = datetime.now(timezone.utc)
    cooldown = (now_dt + timedelta(seconds=cooldown_seconds)).isoformat() if failures >= threshold else current.get("cooldown_until")
    with conn() as c:
        c.execute("""INSERT INTO judge_health(judge_id,consecutive_failures,last_error,last_failure_at,cooldown_until,updated_at)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(judge_id) DO UPDATE SET consecutive_failures=excluded.consecutive_failures,
            last_error=excluded.last_error,last_failure_at=excluded.last_failure_at,
            cooldown_until=excluded.cooldown_until,updated_at=excluded.updated_at""",
                  (judge_id, failures, error[:1000], now_dt.isoformat(), cooldown, now_dt.isoformat()))


def active_job(paper_id: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM jobs WHERE paper_id=? AND status IN ('queued','running') ORDER BY created_at LIMIT 1",
                      (paper_id,)).fetchone()
        return dict(r) if r else None


def claim_job(job_id: str) -> dict | None:
    """Atomically claim a queued job for one worker."""
    with conn() as c:
        cur = c.execute("""UPDATE jobs SET status='running', started_at=?, finished_at=NULL,
            attempts=attempts+1, error=NULL WHERE job_id=? AND status='queued'""", (now(), job_id))
        if cur.rowcount != 1:
            return None
        r = c.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        return dict(r) if r else None


def create_batch(name: str, paper_ids: list[str]) -> str:
    bid = uuid.uuid4().hex
    with conn() as c:
        c.execute("INSERT INTO batches(batch_id,name,created_at) VALUES(?,?,?)", (bid, name, now()))
        for pid in paper_ids:
            c.execute("INSERT OR IGNORE INTO batch_items(batch_id,paper_id) VALUES(?,?)", (bid, pid))
    return bid


def set_batch_job(batch_id: str, paper_id: str, job_id: str) -> None:
    with conn() as c:
        c.execute("UPDATE batch_items SET job_id=? WHERE batch_id=? AND paper_id=?", (job_id, batch_id, paper_id))


def batch_ids_for_job(job_id: str) -> list[str]:
    with conn() as c:
        return [r[0] for r in c.execute("SELECT DISTINCT batch_id FROM batch_items WHERE job_id=?", (job_id,))]


def finish_batch_if_done(batch_id: str) -> dict | None:
    """Atomically mark a terminal batch finished; return it once for notification."""
    b = get_batch(batch_id)
    if not b or b["status"] not in ("completed", "partial", "failed"):
        return None
    with conn() as c:
        cur = c.execute("UPDATE batches SET finished_at=? WHERE batch_id=? AND finished_at IS NULL",
                        (now(), batch_id))
        if cur.rowcount != 1:
            return None
    b["finished_at"] = now()
    return b


def get_batch(batch_id: str) -> dict | None:
    with conn() as c:
        b = c.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        if not b:
            return None
        items = []
        for r in c.execute("""SELECT bi.paper_id,bi.job_id,j.status job_status,j.error,
            j.result_grade_id,g.overall,g.cost_usd,p.title,p.model_id,p.query_id
            FROM batch_items bi LEFT JOIN jobs j ON j.job_id=bi.job_id
            LEFT JOIN papers p ON p.paper_id=bi.paper_id
            LEFT JOIN grades g ON g.grade_id=j.result_grade_id
            WHERE bi.batch_id=? ORDER BY bi.rowid""", (batch_id,)):
            items.append(dict(r))
        statuses = [i["job_status"] for i in items]
        if any(s in (None, "queued", "running") for s in statuses):
            status = "running"
        elif not items:
            status = "empty"
        elif all(s == "completed" for s in statuses):
            status = "completed"
        elif all(s == "failed" for s in statuses):
            status = "failed"
        else:
            status = "partial"
        finished = b["finished_at"]
        if status in ("completed", "partial", "failed") and not finished:
            finished = now()
            c.execute("UPDATE batches SET finished_at=? WHERE batch_id=? AND finished_at IS NULL",
                      (finished, batch_id))
        return {"batch_id": b["batch_id"], "name": b["name"], "created_at": b["created_at"],
                "finished_at": finished, "status": status, "total": len(items),
                "completed": sum(1 for s in statuses if s == "completed"),
                "failed": sum(1 for s in statuses if s == "failed"),
                "cost_usd": round(sum(float(i.get("cost_usd") or 0) for i in items), 6),
                "items": items}


def queued_jobs() -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at")]


def requeue_running() -> int:
    """Jobs left running by a crash are safe to retry; grading is idempotent."""
    with conn() as c:
        cur = c.execute("UPDATE jobs SET status='queued', error='requeued after service restart' WHERE status='running'")
        return cur.rowcount


def get_grade(grade_id: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM grades WHERE grade_id=?", (grade_id,)).fetchone()
        return dict(r) if r else None


def get_grade_by_cache(cache_key: str) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM grades WHERE cache_key=? AND is_current=1", (cache_key,)).fetchone()
        return dict(r) if r else None


def get_current_grade(paper_id: str, rubric_id: str | None = None) -> dict | None:
    with conn() as c:
        if rubric_id:
            r = c.execute("SELECT * FROM grades WHERE paper_id=? AND rubric_id=? AND is_current=1 ORDER BY created_at DESC LIMIT 1",
                          (paper_id, rubric_id)).fetchone()
        else:
            r = c.execute("SELECT * FROM grades WHERE paper_id=? AND is_current=1 ORDER BY created_at DESC LIMIT 1",
                          (paper_id,)).fetchone()
        return dict(r) if r else None


def save_grade(data: dict) -> str:
    gid = data.get("grade_id") or uuid.uuid4().hex
    with conn() as c:
        c.execute("UPDATE grades SET is_current=0 WHERE paper_id=? AND rubric_id=? AND is_current=1",
                  (data["paper_id"], data["rubric_id"]))
        if data.get("replace_cache_key"):
            # A forced regrade becomes the canonical grade for this cache key.
            # Preserve the previous row as history without violating UNIQUE(cache_key).
            c.execute("""UPDATE grades
                         SET cache_key=cache_key || ':previous:' || grade_id, is_current=0
                         WHERE cache_key=? AND paper_id=?""",
                      (data["cache_key"], data["paper_id"]))
        c.execute("""INSERT INTO grades
            (grade_id,paper_id,rubric_id,rubric_version,judge_pool,cache_key,accuracy,coverage,critical,sources,
             structure,action,overall,disagreement,confidence,factual_confidence,fact_errors_json,notes,report_usable,
             is_current,cost_usd,calibration_status,budget_exceeded,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            gid, data["paper_id"], data["rubric_id"], data["rubric_version"], data["judge_pool"], data["cache_key"],
            data["accuracy"], data["coverage"], data["critical"], data["sources"], data["structure"], data["action"],
            data["overall"], data.get("disagreement"), data.get("confidence"), data.get("factual_confidence"),
            json.dumps(data.get("fact_errors") or []), data.get("notes"), 1 if data.get("report_usable", True) else 0,
            1 if data.get("is_current", True) else 0, data.get("cost_usd"), data.get("calibration_status", "provisional"),
            1 if data.get("budget_exceeded") else 0, now(),
        ))
    return gid


def save_judge_run(data: dict) -> None:
    with conn() as c:
        c.execute("""INSERT INTO judge_runs
            (run_id,paper_id,grade_id,judge_id,judge_model,family,raw_response,scores_json,overall,parse_ok,error,
             latency_ms,prompt_tokens,completion_tokens,cost_usd,prompt_hash,temperature,served_provider,created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
            data.get("run_id") or uuid.uuid4().hex, data["paper_id"], data.get("grade_id"), data["judge_id"],
            data["judge_model"], data.get("family"), data.get("raw_response"), json.dumps(data.get("scores") or {}),
            data.get("overall"), 1 if data.get("parse_ok") else 0, data.get("error"), data.get("latency_ms"),
            data.get("prompt_tokens"), data.get("completion_tokens"), data.get("cost_usd"),
            data.get("prompt_hash"), data.get("temperature"), data.get("served_provider"), now(),
        ))


def scores(limit: int = 500, current_only: bool = True) -> list[dict]:
    with conn() as c:
        where = "WHERE g.is_current=1" if current_only else ""
        rows = c.execute(f"""SELECT g.*, p.source_type, p.source_ref, p.title, p.query_id, p.query_text,
            p.rubric_id, p.report_usable, p.word_count, p.model_id, p.model_key, p.ldr_job_id, p.metadata_json,
            p.created_at AS paper_created_at
            FROM grades g JOIN papers p ON p.paper_id=g.paper_id
            {where}
            ORDER BY g.created_at DESC LIMIT ?""", (limit,)).fetchall()
        return [dict(r) for r in rows]


def judge_runs_for_paper(paper_id: str) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM judge_runs WHERE paper_id=? ORDER BY created_at", (paper_id,))]


def average_grade_cost() -> float:
    with conn() as c:
        v = c.execute("SELECT avg(cost_usd) FROM grades WHERE cost_usd IS NOT NULL AND cost_usd > 0").fetchone()[0]
        return float(v or 0)


def stats() -> dict:
    with conn() as c:
        jobs = {r["status"]: r["n"] for r in c.execute("SELECT status,count(*) n FROM jobs GROUP BY status")}
        judges = c.execute("""SELECT judge_id,count(*) n,
            sum(CASE WHEN parse_ok=0 THEN 1 ELSE 0 END) errors,
            round(avg(latency_ms),0) avg_latency_ms
            FROM judge_runs GROUP BY judge_id ORDER BY judge_id""").fetchall()
        return {
            "papers": c.execute("SELECT count(*) FROM papers").fetchone()[0],
            "grades": c.execute("SELECT count(*) FROM grades").fetchone()[0],
            "current_grades": c.execute("SELECT count(*) FROM grades WHERE is_current=1").fetchone()[0],
            "usable_grades": c.execute("SELECT count(*) FROM grades WHERE report_usable=1").fetchone()[0],
            "jobs": jobs,
            "judges": [dict(r) for r in judges],
            "judge_health": [dict(r) for r in c.execute("SELECT * FROM judge_health ORDER BY judge_id")],
            "total_cost_usd": c.execute("SELECT round(coalesce(sum(cost_usd),0),6) FROM grades").fetchone()[0],
            "db_bytes": __import__("os").path.getsize(DB_PATH),
        }
