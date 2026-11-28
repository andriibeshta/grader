"""HTTP API and queue worker for the grader service."""
from __future__ import annotations

import base64
import csv
import io
import json
import os
import queue
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import db, pipeline
from .charts import CHART_PATH, render_chart
from .config import TOKEN_PATH, api_token, load_config
from .ingest import ingest_bytes, ingest_file, ingest_text, ingest_url

CFG = load_config()
TOKEN = api_token()


class Worker:
    def __init__(self):
        self.q: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()
        self.threads: list[threading.Thread] = []
        self.cfg = load_config()

    def submit(self, job_id: str):
        self.q.put(job_id)

    def request_stop(self):
        self._stop.set()

    def stop(self, timeout: float = 25.0):
        self.request_stop()
        deadline = time.time() + timeout
        for t in self.threads:
            t.join(timeout=max(0.1, deadline - time.time()))

    def alive(self) -> bool:
        return bool(self.threads) and all(t.is_alive() for t in self.threads)

    def _finish_batches(self, job_id: str):
        try:
            for bid in db.batch_ids_for_job(job_id):
                db.finish_batch_if_done(bid)
        except Exception as e:
            print(f"[worker] batch completion skipped: {type(e).__name__}: {e}", flush=True)

    def start(self):
        count = max(1, int(self.cfg.get("worker_threads", 2)))
        for i in range(count):
            thread = threading.Thread(target=self.run, name=f"grader-worker-{i}", daemon=True)
            self.threads.append(thread)
            thread.start()

    def run(self):
        print(f"[worker] started ({threading.current_thread().name})", flush=True)
        try:
            for job in db.queued_jobs():
                self.q.put(job["job_id"])
        except Exception as e:
            print(f"[worker] initial queue sweep failed: {type(e).__name__}: {e}", flush=True)
        last_sweep = 0.0
        while not self._stop.is_set():
            try:
                job_id = self.q.get(timeout=1)
            except queue.Empty:
                if time.time() - last_sweep > 30:
                    last_sweep = time.time()
                    try:
                        for job in db.queued_jobs():
                            self.q.put(job["job_id"])
                    except Exception as e:
                        print(f"[worker] queue sweep failed: {type(e).__name__}: {e}", flush=True)
                continue
            job = None
            try:
                job = db.claim_job(job_id)
                if not job:
                    print(f"[worker] skip {job_id} (not queued)", flush=True)
                    continue
                print(f"[worker] picked {job_id} attempt={job['attempts']}", flush=True)
                paper = db.get_paper(job["paper_id"])
                if not paper:
                    raise RuntimeError(f"paper not found: {job['paper_id']}")
                grade = pipeline.grade(paper["paper_id"], force=(job.get("type") == "regrade"))
                db.update_job(job_id, status="completed", result_grade_id=grade["grade_id"], finished_at=db.now(), error=None)
                print(f"[worker] completed {job_id} grade={grade['grade_id']} overall={grade['overall']}", flush=True)
                self._finish_batches(job_id)
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                attempts = int(job["attempts"] or 0) if job else 999
                if attempts < int(self.cfg.get("max_job_attempts", 3)):
                    print(f"[worker] retry {job_id} attempt {attempts}: {error}", flush=True)
                    db.update_job(job_id, status="queued", error=error, finished_at=None)
                    time.sleep(1)
                    self.q.put(job_id)
                else:
                    print(f"[worker] failed {job_id} after {attempts} attempts: {error}", flush=True)
                    db.update_job(job_id, status="failed", error=error, finished_at=db.now())
                    self._finish_batches(job_id)
            finally:
                self.q.task_done()


WORKER = Worker()


def public_paper(p: dict) -> dict:
    out = dict(p)
    out.pop("report_text", None)
    try:
        out["metadata"] = json.loads(out.pop("metadata_json") or "{}")
    except Exception:
        out["metadata"] = {}
    return out


def enqueue_grade(paper_id: str, force: bool = False) -> tuple[str, dict | None]:
    paper = db.get_paper(paper_id)
    if not paper:
        raise KeyError("paper not found")
    active = db.active_job(paper_id)
    if active:
        return active["job_id"], None
    # Cache hit: create a completed job for idempotency.
    if not paper["report_usable"]:
        g = pipeline.grade(paper_id, force=force)
        jid = db.create_job(paper_id, "regrade" if force else "grade")
        db.update_job(jid, status="completed", result_grade_id=g["grade_id"], started_at=db.now(), finished_at=db.now())
        return jid, g
    jid = db.create_job(paper_id, "regrade" if force else "grade")
    WORKER.submit(jid)
    return jid, None


class Handler(BaseHTTPRequestHandler):
    try:
        server_version = "Grader/" + (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
    except Exception:
        server_version = "Grader/dev"

    def log_message(self, fmt, *args):
        print(f"[http] {self.address_string()} {fmt % args}", flush=True)

    def _auth(self) -> bool:
        route = self.path.split("?", 1)[0]
        if route in ("/health/live", "/health/ready"):
            return True
        supplied = self.headers.get("X-API-Token", "")
        if not supplied:
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                supplied = auth[7:]
        return supplied == TOKEN

    def _send(self, code: int, obj, ctype="application/json") -> None:
        if isinstance(obj, (dict, list)):
            data = json.dumps(obj, indent=1).encode()
        elif isinstance(obj, bytes):
            data = obj
        else:
            data = str(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length", "0"))
        if n > 25_000_000:
            raise ValueError("request body too large")
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw.decode() or "{}")

    def _route(self):
        return urlparse(self.path)

    def do_GET(self):
        if not self._auth():
            return self._send(401, {"error": "unauthorized"})
        u = self._route()
        p = u.path
        if p == "/health/live":
            return self._send(200, {"ok": True, "service": "grader"})
        if p == "/health/ready":
            try:
                with db.conn() as c:
                    c.execute("SELECT 1").fetchone()
            except Exception as e:
                return self._send(503, {"ok": False, "error": f"database unavailable: {type(e).__name__}"})
            if not WORKER.alive():
                return self._send(503, {"ok": False, "error": "worker unavailable"})
            return self._send(200, {"ok": True, "service": "grader", "database": "ok", "worker": "ok"})
        if p == "/health":
            judges = []
            for j in CFG.get("judges", []):
                judges.append({"id": j["id"], "model": j["model"],
                               "provider_chain": j.get("provider_chain") or [],
                               "key_present": bool(os.environ.get(j["env_key"], "")),
                               "available": db.judge_available(j["id"])})
            adj = CFG.get("adjudicator", {})
            return self._send(200, {"ok": True, "service": "grader", "db": str(db.DB_PATH),
                                    "queued": len(db.queued_jobs()), "judges": judges,
                                    "adjudicator": {"id": adj.get("id"), "model": adj.get("model"),
                                                    "provider_chain": adj.get("provider_chain") or []},
                                    "adapters": {"ldr": {"enabled": bool(CFG.get("ldr", {}).get("enabled")),
                                                         "base": CFG.get("ldr", {}).get("base") if CFG.get("ldr", {}).get("enabled") else None}},
                                    "judge_health": [db.get_judge_health(j["id"]) for j in CFG.get("judges", [])]})
        if p == "/v1/stats":
            return self._send(200, db.stats())
        if p.startswith("/v1/batches/"):
            rest = p[len("/v1/batches/"):]
            if rest.endswith("/export"):
                bid = rest[:-len("/export")].strip("/")
                b = db.get_batch(bid)
                if not b:
                    return self._send(404, {"error": "batch not found"})
                fmt = (parse_qs(u.query).get("format", ["json"])[0] or "json").lower()
                if fmt == "csv":
                    buf = io.StringIO()
                    rows = b["items"]
                    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()) if rows else ["paper_id"])
                    w.writeheader(); w.writerows(rows)
                    return self._send(200, buf.getvalue().encode(), "text/csv")
                return self._send(200, b)
            b = db.get_batch(rest)
            if not b:
                return self._send(404, {"error": "batch not found"})
            b["budget_exceeded"] = b["cost_usd"] > float(CFG.get("max_cost_per_batch_usd", 1.0))
            return self._send(200, b)
        if p == "/v1/jobs":
            qs = parse_qs(u.query)
            status = qs.get("status", [None])[0]
            limit = int(qs.get("limit", ["100"])[0] or 100)
            return self._send(200, {"jobs": db.jobs(status=status, limit=min(limit, 500))})
        if p.startswith("/v1/jobs/"):
            j = db.get_job(p.rsplit("/", 1)[-1])
            if not j:
                return self._send(404, {"error": "job not found"})
            out = {"job": j}
            if j.get("result_grade_id"):
                out["grade"] = db.get_grade(j["result_grade_id"])
            return self._send(200, out)
        if p.startswith("/v1/papers/") and p.endswith("/current-grade"):
            paper_id = p.split("/")[-2]
            g = db.get_current_grade(paper_id)
            if not g:
                return self._send(404, {"error": "no current grade"})
            return self._send(200, {"grade": g})
        if p.startswith("/v1/papers/"):
            paper = db.get_paper(p.rsplit("/", 1)[-1])
            if not paper:
                return self._send(404, {"error": "paper not found"})
            return self._send(200, {"paper": public_paper(paper)})
        if p.startswith("/v1/grades/"):
            g = db.get_grade(p.rsplit("/", 1)[-1])
            if not g:
                return self._send(404, {"error": "grade not found"})
            return self._send(200, {"grade": g, "judge_runs": db.judge_runs_for_paper(g["paper_id"])})
        if p == "/v1/scores":
            qs = parse_qs(u.query)
            fmt = (qs.get("format", ["json"])[0] or "json").lower()
            current_only = (qs.get("include_history", ["false"])[0] or "false").lower() != "true"
            rows = db.scores(5000, current_only=current_only)
            if fmt == "csv":
                buf = io.StringIO()
                flat = [{**r, "fact_errors_json": r.get("fact_errors_json", "")} for r in rows]
                w = csv.DictWriter(buf, fieldnames=list(flat[0].keys()) if flat else ["grade_id"])
                w.writeheader(); w.writerows(flat)
                return self._send(200, buf.getvalue().encode(), "text/csv")
            return self._send(200, {"scores": rows})
        if p == "/v1/chart.png":
            render_chart()
            return self._send(200, CHART_PATH.read_bytes(), "image/png")
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._auth():
            return self._send(401, {"error": "unauthorized"})
        u = self._route()
        p = u.path
        try:
            body = self._body()
            if p == "/v1/papers":
                st = body.get("source_type", "text")
                common = dict(
                    title=body.get("title", ""), query_id=body.get("query_id"), query_text=body.get("query_text"),
                    rubric_id=body.get("rubric_id"), model_id=body.get("model_id"), model_key=body.get("model_key"),
                    metadata=body.get("metadata") or {},
                )
                if st == "ldr":
                    if not CFG.get("ldr", {}).get("enabled"):
                        return self._send(400, {"error": "LDR adapter is disabled"})
                    from .adapters.ldr import LDRClient
                    job_id = body.get("job_id") or body.get("source_ref")
                    if not job_id:
                        return self._send(400, {"error": "job_id required for source_type=ldr"})
                    data = LDRClient().ingest_job(str(job_id))
                elif st == "file":
                    if body.get("content_base64"):
                        try:
                            blob = base64.b64decode(body["content_base64"], validate=True)
                        except Exception:
                            return self._send(400, {"error": "invalid content_base64"})
                        data = ingest_bytes(blob, body.get("filename") or body.get("source_ref") or "upload.txt",
                                            source_type="file",
                                            source_ref=body.get("source_ref") or body.get("filename") or "upload",
                                            **common)
                    else:
                        path = body.get("path") or body.get("source_ref")
                        if not path:
                            return self._send(400, {"error": "path required for source_type=file"})
                        data = ingest_file(str(path), **common)
                elif st == "url":
                    data = ingest_url(body.get("url") or body.get("source_ref"), **common)
                elif st == "text":
                    data = ingest_text(body.get("content") or body.get("text") or "", **common)
                else:
                    return self._send(400, {"error": f"unsupported source_type {st}"})
                paper, created = pipeline.register(data)
                job = None
                if body.get("grade", True):
                    jid, cached = enqueue_grade(paper["paper_id"])
                    if body.get("wait", False):
                        cached = self._wait(jid)
                    job = {"job_id": jid, "grade": cached}
                return self._send(201, {"paper": public_paper(paper), "created": created, "job": job})
            if p.startswith("/v1/papers/from-ldr/"):
                if not CFG.get("ldr", {}).get("enabled"):
                    return self._send(400, {"error": "LDR adapter is disabled"})
                from .adapters.ldr import LDRClient
                job_id = p.rsplit("/", 1)[-1]
                data = LDRClient().ingest_job(job_id)
                paper, created = pipeline.register(data)
                jid, cached = enqueue_grade(paper["paper_id"])
                if body.get("wait", False):
                    cached = self._wait(jid)
                return self._send(201, {"paper": public_paper(paper), "created": created, "job": {"job_id": jid, "grade": cached}})
            if p == "/v1/grades":
                paper_id = body.get("paper_id")
                if not paper_id:
                    return self._send(400, {"error": "paper_id required"})
                force = bool(body.get("force", False))
                jid, cached = enqueue_grade(paper_id, force=force)
                if body.get("wait", False):
                    cached = self._wait(jid)
                return self._send(202, {"job_id": jid, "force": force, "grade": cached})
            if p == "/v1/batches":
                name = str(body.get("name") or "batch")
                paper_ids = body.get("paper_ids") or []
                if not isinstance(paper_ids, list) or not paper_ids:
                    return self._send(400, {"error": "paper_ids must be a non-empty list"})
                paper_ids = list(dict.fromkeys(str(x) for x in paper_ids))
                missing = [pid for pid in paper_ids if not db.get_paper(pid)]
                if missing:
                    return self._send(400, {"error": "papers not found", "paper_ids": missing})
                avg_cost = db.average_grade_cost()
                estimated = round(avg_cost * len(paper_ids), 4)
                max_batch = float(CFG.get("max_cost_per_batch_usd", 1.0))
                if estimated > max_batch and not body.get("allow_over_budget", False):
                    return self._send(400, {"error": "estimated batch cost exceeds cap",
                                            "estimated_cost_usd": estimated, "cap_usd": max_batch,
                                            "allow_over_budget": "pass allow_over_budget=true to override"})
                bid = db.create_batch(name, paper_ids)
                jobs = []
                for pid in paper_ids:
                    jid, cached = enqueue_grade(pid)
                    db.set_batch_job(bid, pid, jid)
                    jobs.append({"paper_id": pid, "job_id": jid, "cached": bool(cached)})
                return self._send(202, {"batch_id": bid, "name": name, "total": len(paper_ids), "jobs": jobs})
            if p.startswith("/v1/jobs/") and p.endswith("/retry"):
                jid = p.split("/")[-2]
                j = db.get_job(jid)
                if not j:
                    return self._send(404, {"error": "job not found"})
                if j["status"] != "failed":
                    return self._send(400, {"error": f"job is {j['status']}, not failed"})
                db.update_job(jid, status="queued", error=None, finished_at=None, attempts=0)
                WORKER.submit(jid)
                return self._send(202, {"job_id": jid, "status": "queued"})
            return self._send(404, {"error": "not found"})
        except KeyError as e:
            return self._send(404, {"error": str(e)})
        except Exception as e:
            return self._send(400, {"error": f"{type(e).__name__}: {e}"})

    def _wait(self, job_id: str, timeout=300):
        t0 = time.time()
        while time.time() - t0 < timeout:
            j = db.get_job(job_id)
            if j and j["status"] in ("completed", "failed"):
                return db.get_grade(j["result_grade_id"]) if j.get("result_grade_id") else None
            time.sleep(0.5)
        return None


class GraderHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve():
    db.init_db()
    n = db.requeue_running()
    if n:
        print(f"[service] requeued {n} interrupted job(s)", flush=True)
    render_chart()
    WORKER.start()
    cfg = load_config()
    if cfg.get("ldr", {}).get("enabled") and cfg.get("ldr", {}).get("mode", "on") != "off":
        try:
            from .adapters.ldr import LdrWatcher
            LdrWatcher().start()
        except Exception as e:
            print(f"[watcher] disabled: {type(e).__name__}: {e}", flush=True)
    addr = (cfg.get("host", "127.0.0.1"), int(cfg.get("port", 9131)))
    server = GraderHTTPServer(addr, Handler)

    def request_shutdown(signum, _frame):
        print(f"[service] received signal {signum}; draining", flush=True)
        WORKER.request_stop()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    print(f"[service] listening on http://{addr[0]}:{addr[1]}  token_file={TOKEN_PATH}", flush=True)
    try:
        server.serve_forever(poll_interval=1.0)
    finally:
        WORKER.stop()
        server.server_close()
        try:
            db.checkpoint()
        except Exception:
            pass
        print("[service] stopped", flush=True)


if __name__ == "__main__":
    serve()
