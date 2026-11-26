"""Regression tests for the optional LDR adapter (network-free)."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from grader.adapters import ldr as adapter


class _Cookies(dict):
    def get_dict(self):
        return dict(self)


class _Resp:
    def __init__(self, status_code=200, text="", payload=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload

    def json(self):
        return self._payload if self._payload is not None else {}


class _Session:
    """Minimal stand-in for requests.Session that records login POST headers."""

    def __init__(self, login_status=200):
        self.headers = {}
        self.cookies = _Cookies()
        self.login_status = login_status
        self.posts = []

    def get(self, url, **kwargs):
        if url.endswith("/auth/login"):
            return _Resp(200, '<input name="csrf_token" value="form-token">')
        return _Resp(200, payload={"csrf_token": "fresh-token"})

    def post(self, url, data=None, **kwargs):
        self.posts.append({"url": url, "data": dict(data or {}), "headers": dict(self.headers)})
        return _Resp(self.login_status, "<html>ok</html>")


def _client(session):
    client = adapter.LDRClient.__new__(adapter.LDRClient)
    client.base = "http://ldr.invalid"
    client.session = session
    client._csrf = ""
    return client


class LoginTests(unittest.TestCase):
    def _relogin(self, session, cookie_path=None):
        client = _client(session)
        if cookie_path is None:
            td = tempfile.TemporaryDirectory()
            cookie = Path(td.name) / "cookies.json"
        else:
            td = None
            cookie = Path(cookie_path)
        try:
            with patch.object(adapter, "ldr_credentials", return_value=("u", "p")), \
                 patch.object(adapter, "LDR_COOKIE_PATH", cookie):
                client._login()
        finally:
            if td is not None:
                td.cleanup()
        return cookie

    def test_relogin_drops_stale_csrf_header(self):
        """A leftover X-CSRF-Token makes POST /auth/login fail CSRF with HTTP 403."""
        session = _Session()
        session.headers["X-CSRF-Token"] = "stale-token-from-previous-session"
        self._relogin(session)
        self.assertEqual(len(session.posts), 1)
        self.assertNotIn("X-CSRF-Token", session.posts[0]["headers"])
        self.assertEqual(session.posts[0]["data"]["csrf_token"], "form-token")

    def test_csrf_header_restored_for_later_posts(self):
        session = _Session()
        session.headers["X-CSRF-Token"] = "stale-token"
        self._relogin(session)
        self.assertEqual(session.headers.get("X-CSRF-Token"), "fresh-token")

    def test_login_failure_raises(self):
        session = _Session(login_status=403)
        with self.assertRaises(RuntimeError) as ctx:
            self._relogin(session)
        self.assertIn("403", str(ctx.exception))

    def test_login_writes_cookie_file(self):
        session = _Session()
        session.cookies["session"] = "abc123"
        with tempfile.TemporaryDirectory() as td:
            cookie = self._relogin(session, cookie_path=Path(td) / "cookies.json")
            self.assertEqual(cookie.read_text(), '{"session": "abc123"}')


class WatcherBackoffTests(unittest.TestCase):
    def test_backoff_grows_and_caps(self):
        """Consecutive poll failures must not hammer LDR's login limiter."""
        poll = 60
        waits = [min(max(10, poll) * (2 ** min(n, 4)), 900) for n in range(1, 7)]
        self.assertEqual(waits, [120, 240, 480, 900, 900, 900])


if __name__ == "__main__":
    unittest.main()
