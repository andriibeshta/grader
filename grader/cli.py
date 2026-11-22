"""CLI client for the local grader service."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests

BASE = os.environ.get("GRADER_BASE_URL", "http://127.0.0.1:9131").rstrip("/")
_base = Path(os.environ.get("GRADER_DATA_DIR", str(Path(__file__).resolve().parent.parent)))
_TOKEN_ENV = os.environ.get("GRADER_TOKEN_FILE", "").strip()
TOKEN_PATHS = ([Path(_TOKEN_ENV)] if _TOKEN_ENV else []) + [
    _base / "state" / "api_token",
    _base / "api_token",
]


def headers():
    tok = ""
    for p in TOKEN_PATHS:
        try:
            tok = p.read_text().strip()
            if tok:
                break
        except OSError:
            continue
    return {"X-API-Token": tok} if tok else {}


def post(path, payload, timeout=330):
    r = requests.post(BASE + path, headers={**headers(), "Content-Type": "application/json"}, json=payload, timeout=timeout)
    try:
        b = r.json()
    except Exception:
        b = {"error": r.text}
    if r.status_code >= 400:
        print(json.dumps(b, indent=1)); sys.exit(1)
    return b


def main():
    ap = argparse.ArgumentParser(prog="grader-cli")
    sub = ap.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("grade", help="grade an LDR job, file, URL, or text")
    src = g.add_mutually_exclusive_group(required=True)
    src.add_argument("--ldr", metavar="JOB_ID")
    src.add_argument("--file", metavar="PATH")
    src.add_argument("--url", metavar="URL")
    src.add_argument("--text", metavar="TEXT")
    g.add_argument("--query-id")
    g.add_argument("--query-text")
    g.add_argument("--rubric-id")
    g.add_argument("--title", default="")
    g.add_argument("--model-id")
    g.add_argument("--no-wait", action="store_true")

    s = sub.add_parser("status", help="get job status and grade")
    s.add_argument("job_id")
    sc = sub.add_parser("scores", help="list recent grades")
    sc.add_argument("--csv", action="store_true")
    sub.add_parser("chart", help="print chart path")
    sub.add_parser("health", help="service health")
    sub.add_parser("serve", help="run the service in the foreground")

    a = ap.parse_args()
    if a.cmd == "serve":
        from .server import serve
        serve(); return
    if a.cmd == "health":
        print(requests.get(BASE + "/health", headers=headers(), timeout=10).text); return
    if a.cmd == "status":
        print(requests.get(BASE + f"/v1/jobs/{a.job_id}", headers=headers(), timeout=30).text); return
    if a.cmd == "scores":
        r = requests.get(BASE + "/v1/scores", params={"format": "csv" if a.csv else "json"}, headers=headers(), timeout=60)
        print(r.text); return
    if a.cmd == "chart":
        print(BASE + "/v1/chart.png"); return

    wait = not a.no_wait
    common = {"wait": wait, "query_id": a.query_id, "query_text": a.query_text,
              "rubric_id": a.rubric_id, "title": a.title, "model_id": a.model_id}
    if a.ldr:
        out = post(f"/v1/papers/from-ldr/{a.ldr}", {"wait": wait})
    elif a.file:
        out = post("/v1/papers", {"source_type": "file", "path": a.file, "grade": True, **common})
    elif a.url:
        out = post("/v1/papers", {"source_type": "url", "url": a.url, "grade": True, **common})
    else:
        out = post("/v1/papers", {"source_type": "text", "content": a.text, "grade": True, **common})
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
