#!/usr/bin/env python3
"""Create a consistent SQLite backup of the grader database and verify it."""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(os.environ.get("GRADER_DATA_DIR", "") or Path(__file__).resolve().parent)
DB = Path(os.environ.get("GRADER_DB", str(ROOT / "grades.db")))
OUTDIR = Path(os.environ.get("GRADER_BACKUP_DIR", str(ROOT / "backups")))
KEEP = 30


def main() -> int:
    OUTDIR.mkdir(parents=True, exist_ok=True)
    out = OUTDIR / f"grades-{datetime.now():%Y%m%d-%H%M%S}.db"
    src = sqlite3.connect(DB)
    dst = sqlite3.connect(out)
    try:
        src.backup(dst)
    finally:
        dst.close(); src.close()
    # Verify the backup is readable, intact, and has the expected core tables.
    check = sqlite3.connect(out)
    try:
        integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            print(f"backup integrity failed: {out}: {integrity}", file=sys.stderr)
            return 1
        tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"papers", "grades", "jobs"} <= tables:
            print(f"backup verification failed: {out}", file=sys.stderr)
            return 1
        papers = check.execute("SELECT count(*) FROM papers").fetchone()[0]
        grades = check.execute("SELECT count(*) FROM grades").fetchone()[0]
    finally:
        check.close()
    try:
        out.chmod(0o600)
    except OSError:
        pass
    backups = sorted(OUTDIR.glob("grades-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[KEEP:]:
        old.unlink(missing_ok=True)
    print(f"{out} papers={papers} grades={grades}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
