"""Optional LDR adapter.

The core grader does not depend on this module. Enable it only when the
deployment wants to poll a Local Deep Research instance and auto-grade new
completed jobs. Other pipelines should use the generic HTTP API instead.
"""
from __future__ import annotations

import json
import re
import threading

import requests

from .. import db, pipeline
from ..config import LDR_COOKIE_PATH, REPORTS_DIR, STATE_PATH, load_config, ldr_credentials
from ..ingest import make_paper
from ..rubrics import resolve


class LDRClient:
    def __init__(self):
        cfg = load_config()["ldr"]
        self.base = cfg["base"].rstrip("/")
        self.session = requests.Session()
        self._load_cookie()
        self._csrf = ""

    def _load_cookie(self) -> None:
        p = LDR_COOKIE_PATH
        if p.exists():
            try:
                self.session.cookies.update(json.loads(p.read_text()))
            except Exception:
                pass

    def _login(self) -> None:
        user, password = ldr_credentials()
        r = self.session.get(f"{self.base}/auth/login", timeout=20)
        m = re.search(r'name=["\']csrf_token["\']\s+value=["\']([^"\']+)', r.text)
        if not m:
            raise RuntimeError("LDR login page: csrf token not found")
        r = self.session.post(f"{self.base}/auth/login", data={"username": user, "password": password, "csrf_token": m.group(1)},
                              allow_redirects=True, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"LDR login failed: HTTP {r.status_code}")
        c = self.session.get(f"{self.base}/auth/csrf-token", timeout=20)
        if c.status_code == 200:
            self._csrf = c.json().get("csrf_token", "")
            self.session.headers["X-CSRF-Token"] = self._csrf
        p = LDR_COOKIE_PATH
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.session.cookies.get_dict()))
        try:
            p.chmod(0o600)
        except OSError:
            pass

    def _get(self, path: str, **kwargs):
        r = self.session.get(self.base + path, timeout=kwargs.pop("timeout", 30), **kwargs)
        if r.status_code == 401:
            self._login()
            r = self.session.get(self.base + path, timeout=kwargs.pop("timeout", 30), **kwargs)
        return r

    def status(self, job_id: str) -> dict:
        r = self._get(f"/api/research/{job_id}/status")
        r.raise_for_status()
        return r.json()

    def report(self, job_id: str) -> dict:
        r = self._get(f"/api/report/{job_id}")
        r.raise_for_status()
        return r.json()

    def history(self) -> list[dict]:
        r = self._get("/api/history")
        r.raise_for_status()
        return r.json().get("items", [])

    def ingest_job(self, job_id: str) -> dict:
        st = self.status(job_id)
        rep = self.report(job_id)
        submission = (st.get("metadata") or {}).get("submission") or {}
        text = rep.get("summary") or rep.get("content") or ""
        query = rep.get("metadata", {}).get("query") or submission.get("query") or ""
        hist = {h.get("id"): h for h in self.history()}
        h = hist.get(job_id, {})
        model = submission.get("model") or h.get("model")
        provider = submission.get("model_provider") or ""
        r = resolve(query_text=query)
        out = make_paper(
            text, source_type="ldr", source_ref=job_id, ldr_job_id=job_id,
            title=f"LDR {r['query_id']} / {model or 'unknown'}",
            query_id=r["query_id"], query_text=query, rubric_id=r["rubric_id"],
            model_id=model, model_key=model,
            metadata={"provider": provider, "strategy": submission.get("strategy"),
                      "iterations": submission.get("iterations"), "status": st.get("status"),
                      "duration_seconds": h.get("duration_seconds"), "created_at": h.get("created_at")},
        )
        # Persist the report for audit even if unusable.
        outdir = REPORTS_DIR / "ldr"
        outdir.mkdir(parents=True, exist_ok=True)
        rp = outdir / f"{job_id}.md"
        rp.write_text(text)
        out["report_path"] = str(rp)
        return out


class LdrWatcher(threading.Thread):
    """Poll an LDR instance and auto-grade newly completed research jobs."""

    def __init__(self):
        super().__init__(daemon=True)
        self.cfg = load_config()["ldr"]
        self.stop = threading.Event()
        self.state = {"seen": [], "initialized": False}
        if STATE_PATH.exists():
            try:
                self.state = json.loads(STATE_PATH.read_text())
            except Exception:
                pass

    def _save(self):
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
        tmp.write_text(json.dumps(self.state))
        tmp.replace(STATE_PATH)

    def _baseline(self, client: LDRClient):
        items = client.history()
        self.state["seen"] = sorted({str(i.get("id")) for i in items if i.get("id")})
        self.state["initialized"] = True
        self._save()
        print(f"[watcher] baseline: {len(self.state['seen'])} existing jobs marked seen; new jobs will auto-grade", flush=True)

    def run(self):
        client = LDRClient()
        if not self.state.get("initialized") and self.cfg.get("baseline_existing", True):
            # Never enter the polling loop with an empty seen-set. A transient
            # LDR failure or a missing state file must not re-grade history.
            while not self.stop.is_set():
                try:
                    self._baseline(client)
                    break
                except Exception as e:
                    print(f"[watcher] baseline failed: {e}; retrying", flush=True)
                    self.stop.wait(max(10, int(self.cfg.get("poll_seconds", 60))))
            if self.stop.is_set():
                return
        seen = set(self.state.get("seen") or [])
        while not self.stop.is_set():
            try:
                items = client.history()
                for h in items:
                    jid = h.get("id")
                    if not jid or jid in seen:
                        continue
                    if h.get("status") not in ("completed", "complete", "done"):
                        continue
                    existing = db.get_paper_by_ldr(jid)
                    if existing:
                        try:
                            if existing.get("report_usable") and not db.get_current_grade(existing["paper_id"]):
                                from ..server import enqueue_grade
                                j, _ = enqueue_grade(existing["paper_id"])
                                print(f"[watcher] recovered grade for {jid} -> job {j}", flush=True)
                            seen.add(jid)
                            self.state["seen"] = sorted(seen)
                            self._save()
                        except Exception as e:
                            print(f"[watcher] existing paper enqueue failed for {jid}: {type(e).__name__}: {e}", flush=True)
                        continue
                    try:
                        data = client.ingest_job(jid)
                        paper, created = pipeline.register(data)
                        if paper["report_usable"]:
                            from ..server import enqueue_grade
                            j, _ = enqueue_grade(paper["paper_id"])
                            print(f"[watcher] auto-graded {jid} -> paper {paper['paper_id']} job {j}", flush=True)
                        else:
                            print(f"[watcher] stored unusable report {jid} (no judge calls)", flush=True)
                        seen.add(jid)
                        self.state["seen"] = sorted(seen)
                        self._save()
                    except Exception as e:
                        # Leave unseen so a transient LDR/report failure retries next poll.
                        print(f"[watcher] failed to ingest {jid}: {type(e).__name__}: {e}", flush=True)
            except Exception as e:
                print(f"[watcher] poll error: {type(e).__name__}: {e}", flush=True)
            self.stop.wait(max(10, int(self.cfg.get("poll_seconds", 60))))


Watcher = LdrWatcher
