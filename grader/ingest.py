"""Paper ingestion: text, files, and URLs. LDR is an optional adapter."""
from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import socket
import subprocess
import tempfile
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

from .rubrics import resolve


def normalize_text(text: str) -> str:
    return re.sub(r"\r\n?", "\n", text or "").strip()


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode()).hexdigest()


def paper_key(text: str, rubric_id: str, query_text: str) -> str:
    return hashlib.sha256((normalize_text(text) + "\n" + rubric_id + "\n" + query_text).encode()).hexdigest()


def report_usable(text: str) -> bool:
    t = normalize_text(text)
    head = t[:800].lower()
    # LDR can return a failure page with a completed history status.
    if any(marker in head for marker in (
        "research failed",
        "final synthesis failed",
        "final synthesis produced no usable text",
        "the llm request took too long",
    )):
        return False
    if len(t.split()) < 80:
        return False
    # A bare source dump is not a report.
    if "## Sources" in t and len(t.split("## Sources")[0].split()) < 40:
        return False
    return True


def make_paper(text: str, *, source_type: str, source_ref: str, title: str = "", query_id: str | None = None,
               query_text: str | None = None, rubric_id: str | None = None, model_id: str | None = None,
               model_key: str | None = None, metadata: dict | None = None, ldr_job_id: str | None = None) -> dict:
    text = normalize_text(text)
    r = resolve(query_text=query_text, query_id=query_id, rubric_id=rubric_id)
    usable = report_usable(text)
    return {
        "paper_key": paper_key(text, r["rubric_id"], r["query_text"]),
        "ldr_job_id": ldr_job_id,
        "source_type": source_type,
        "source_ref": source_ref,
        "title": title or (source_ref[:120] if source_ref else "Untitled paper"),
        "query_id": r["query_id"],
        "query_text": r["query_text"],
        "rubric_id": r["rubric_id"],
        "rubric_version": r["rubric_version"],
        "content_hash": content_hash(text),
        "report_text": text,
        "report_usable": usable,
        "word_count": len(text.split()),
        "model_id": model_id,
        "model_key": model_key,
        "metadata": metadata or {},
    }


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self._skip += 1

    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def ingest_text(text: str, **kwargs) -> dict:
    return make_paper(text, source_type=kwargs.pop("source_type", "text"), source_ref=kwargs.pop("source_ref", "text"), **kwargs)


def ingest_file(path: str, **kwargs) -> dict:
    p = Path(os.path.expanduser(path))
    if not p.exists():
        raise FileNotFoundError(path)
    source_type = kwargs.pop("source_type", "file")
    source_ref = kwargs.pop("source_ref", str(p))
    suffix = p.suffix.lower()
    if suffix == ".pdf":
        r = subprocess.run(["pdftotext", "-layout", str(p), "-"], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(f"pdftotext failed: {r.stderr[:300]}")
        text = r.stdout
    elif suffix in (".md", ".txt", ".markdown", ".rst", ".html", ".htm"):
        text = p.read_text(errors="replace")
        if suffix in (".html", ".htm"):
            ex = _TextExtractor(); ex.feed(text); text = " ".join(ex.parts)
    else:
        raise RuntimeError(f"unsupported file type: {suffix}")
    return make_paper(text, source_type=source_type, source_ref=source_ref, **kwargs)


def ingest_bytes(data: bytes, filename: str, **kwargs) -> dict:
    """Ingest an uploaded file without requiring a host bind mount."""
    suffix = Path(filename).suffix.lower() or ".txt"
    if suffix not in (".pdf", ".md", ".txt", ".markdown", ".rst", ".html", ".htm"):
        raise RuntimeError(f"unsupported file type: {suffix}")
    tmp = tempfile.NamedTemporaryFile(prefix="grader-upload-", suffix=suffix, delete=False)
    tmp.write(data)
    tmp.close()
    try:
        return ingest_file(tmp.name, source_type=kwargs.pop("source_type", "file"),
                           source_ref=kwargs.pop("source_ref", filename), **kwargs)
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def _safe_url(url: str) -> None:
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("only http/https URLs are supported")
    for info in socket.getaddrinfo(u.hostname, None):
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError(f"blocked private/link-local URL host: {u.hostname}")


def ingest_url(url: str, _redirects: int = 0, **kwargs) -> dict:
    if _redirects > 3:
        raise ValueError("too many redirects")
    _safe_url(url)
    r = requests.get(url, timeout=45, headers={"User-Agent": "HermesGrader/0.1"}, stream=True, allow_redirects=False)
    if 300 <= r.status_code < 400 and r.headers.get("location"):
        return ingest_url(urljoin(url, r.headers["location"]), _redirects + 1, **kwargs)
    r.raise_for_status()
    ctype = r.headers.get("content-type", "")
    data = r.content[:10_000_000]
    if "pdf" in ctype or urlparse(url).path.lower().endswith(".pdf"):
        tmp = tempfile.NamedTemporaryFile(prefix="grader-url-", suffix=".pdf", delete=False)
        tmp.write(data); tmp.close()
        try:
            return ingest_file(tmp.name, source_type="url", source_ref=url, **kwargs)
        finally:
            Path(tmp.name).unlink(missing_ok=True)
    text = data.decode(r.encoding or "utf-8", errors="replace")
    if "html" in ctype:
        ex = _TextExtractor(); ex.feed(text); text = " ".join(ex.parts)
    return make_paper(text, source_type="url", source_ref=url, **kwargs)
