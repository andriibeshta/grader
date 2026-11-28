"""Fast, network-free tests for the grader core."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from grader import db
from grader import judges
from grader import pipeline
from grader.config import load_config
from grader.ingest import _safe_url, ingest_bytes, ingest_file, ingest_text, report_usable
from grader.judges import extract_json, validate
from grader.rubrics import resolve


class ValidationTests(unittest.TestCase):
    def test_valid_scores(self):
        out = validate({"accuracy": 8, "coverage": 7, "critical": 9, "sources": 6, "structure": 8,
                        "action": 7, "fact_errors": [], "notes": "ok"})
        self.assertEqual(out["accuracy"], 8.0)

    def test_null_optional_fields(self):
        out = validate({"accuracy": 8, "coverage": 7, "critical": 9, "sources": 6, "structure": 8,
                        "action": 7, "fact_errors": None, "notes": None})
        self.assertEqual(out["fact_errors"], [])
        self.assertIn("no notes", out["notes"])

    def test_missing_score_rejected(self):
        with self.assertRaises(ValueError):
            validate({"accuracy": 8})

    def test_fenced_json(self):
        self.assertEqual(extract_json("```json\n{\"a\":1}\n```"), {"a": 1})


class IngestTests(unittest.TestCase):
    def test_unusable(self):
        self.assertFalse(report_usable("Final synthesis produced no usable text."))
        self.assertFalse(report_usable("short report"))

    def test_ldr_failure_page_not_usable(self):
        text = (
            "# ⚠️ Research Failed\n\n**Error Type:** Unexpected Error\n\n"
            "**What happened:** The LLM request took too long to complete.\n\n"
            "## Research Metrics\n- Search Iterations: 1\n"
        )
        self.assertFalse(report_usable(text))

    def test_usable(self):
        text = "# Report\n" + ("word " * 120) + "\n## Sources\nx"
        self.assertTrue(report_usable(text))

    def test_generic(self):
        d = ingest_text("# A report\n" + ("word " * 120), title="t")
        self.assertEqual(d["rubric_id"], "generic_research_v1")
        self.assertTrue(d["report_usable"])

    def test_private_url_blocked(self):
        with self.assertRaises(ValueError):
            _safe_url("http://127.0.0.1:9131/health")

    def test_url_pdf_keeps_url_source_metadata(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
            proc = Mock(returncode=0, stdout="# Report\n" + ("word " * 120), stderr="")
            with patch("grader.ingest.subprocess.run", return_value=proc):
                d = ingest_file(f.name, source_type="url", source_ref="https://example.com/a.pdf")
        self.assertEqual(d["source_type"], "url")
        self.assertEqual(d["source_ref"], "https://example.com/a.pdf")

    def test_text_upload_without_host_mount(self):
        d = ingest_bytes(b"# Report\n" + (b"word " * 120), "report.md",
                         source_ref="pipeline:report-1", title="pipeline report")
        self.assertEqual(d["source_type"], "file")
        self.assertEqual(d["source_ref"], "pipeline:report-1")
        self.assertTrue(d["report_usable"])


class RubricTests(unittest.TestCase):
    def test_generic(self):
        r = resolve(query_text="A novel question")
        self.assertEqual(r["rubric_id"], "generic_research_v1")
        self.assertEqual(r["factual_confidence"], "low")
        self.assertEqual(r["calibration_status"], "shadow")
        self.assertIn("source-agnostic", r["rubric_text"])
        self.assertNotIn("ldr-benchmark", r["rubric_text"])

    def test_custom_query_mapping(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td, "my_rubric.md").write_text("# My rubric\n\nScore this report.\n")
            with patch("grader.rubrics.QUERIES", {"my_rubric": "Custom question"}), \
                 patch("grader.rubrics.RUBRIC_OVERLAY_DIR", Path(td)):
                r = resolve(query_id="my_rubric")
        self.assertEqual(r["rubric_id"], "my_rubric")
        self.assertEqual(r["query_text"], "Custom question")
        self.assertEqual(r["factual_confidence"], "low")

    def test_custom_rubric_overlay(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td, "my_rubric.md").write_text("# My rubric\n\nScore this report.\n")
            with patch("grader.rubrics.RUBRIC_OVERLAY_DIR", Path(td)):
                r = resolve(rubric_id="my_rubric", query_text="A custom report")
        self.assertEqual(r["rubric_id"], "my_rubric")
        self.assertEqual(r["factual_confidence"], "low")
        self.assertIn("source-agnostic", r["rubric_text"])


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.patcher = patch.object(db, "DB_PATH", Path(self.tmp.name))
        self.patcher.start()
        db.init_db()

    def tearDown(self):
        self.patcher.stop()
        Path(self.tmp.name).unlink(missing_ok=True)

    def test_schema_and_stats(self):
        s = db.stats()
        self.assertEqual(s["papers"], 0)
        cols = {r[1] for r in db.conn().execute("PRAGMA table_info(judge_runs)")}
        self.assertIn("prompt_hash", cols)
        self.assertIn("temperature", cols)

    def test_paper_idempotency(self):
        d = ingest_text("# R\n" + ("word " * 120), title="x")
        p1, c1 = db.create_paper(d)
        p2, c2 = db.create_paper(d)
        self.assertTrue(c1)
        self.assertFalse(c2)
        self.assertEqual(p1["paper_id"], p2["paper_id"])

    def test_judge_circuit_breaker(self):
        db.record_judge_failure("bad", "empty", threshold=2, cooldown_seconds=60)
        self.assertTrue(db.judge_available("bad"))
        db.record_judge_failure("bad", "empty", threshold=2, cooldown_seconds=60)
        self.assertFalse(db.judge_available("bad"))
        db.record_judge_success("bad")
        self.assertTrue(db.judge_available("bad"))

    def test_only_one_current_grade(self):
        paper, _ = db.create_paper(ingest_text("# R\n" + ("word " * 120), title="current"))
        base = {
            "paper_id": paper["paper_id"], "rubric_id": "generic_research_v1", "rubric_version": "v1",
            "judge_pool": "test", "accuracy": 8, "coverage": 8, "critical": 8,
            "sources": 8, "structure": 8, "action": 8, "overall": 8,
            "report_usable": True,
        }
        g1 = db.save_grade({**base, "cache_key": "k1"})
        g2 = db.save_grade({**base, "cache_key": "k2"})
        assert g1 and g2
        self.assertFalse(db.get_grade(g1)["is_current"])
        self.assertTrue(db.get_grade(g2)["is_current"])
        current = db.get_current_grade(paper["paper_id"])
        assert current
        self.assertEqual(current["grade_id"], g2)

    def test_forced_grade_replaces_cache_key_without_unique_conflict(self):
        paper, _ = db.create_paper(ingest_text("# R\n" + ("word " * 120), title="force"))
        base = {
            "paper_id": paper["paper_id"], "rubric_id": "generic_research_v1", "rubric_version": "v1",
            "judge_pool": "test", "accuracy": 8, "coverage": 8, "critical": 8,
            "sources": 8, "structure": 8, "action": 8, "overall": 8,
            "report_usable": True,
        }
        old = db.save_grade({**base, "cache_key": "same"})
        new = db.save_grade({**base, "cache_key": "same", "replace_cache_key": True})
        cached = db.get_grade_by_cache("same")
        old_row = db.get_grade(old)
        new_row = db.get_grade(new)
        assert cached and old_row and new_row
        self.assertEqual(cached["grade_id"], new)
        self.assertTrue(old_row["cache_key"].startswith("same:previous:"))
        self.assertTrue(new_row["is_current"])


class CacheKeyTests(unittest.TestCase):
    def test_rubric_version_changes_cache_key(self):
        paper = {"content_hash": "abc", "rubric_id": "generic_research_v1"}
        a = pipeline._cache_key(paper, {"rubric_version": "v1"})
        b = pipeline._cache_key(paper, {"rubric_version": "v2"})
        self.assertNotEqual(a, b)


class JudgeFallbackTests(unittest.TestCase):
    def test_fallback_when_one_primary_fails(self):
        cfg = {
            "min_judges": 2, "max_judge_workers": 2,
            "disagreement_overall": 1.0, "disagreement_criterion": 2.0,
            "judges": [
                {"id": "good", "model": "m1", "family": "a", "env_key": "TEST_GOOD"},
                {"id": "bad", "model": "m2", "family": "b", "env_key": "TEST_BAD"},
            ],
            "adjudicator": {"id": "fallback", "model": "m3", "family": "c", "env_key": "TEST_FB"},
        }
        scores = {"accuracy": 8, "coverage": 8, "critical": 8, "sources": 8, "structure": 8,
                  "action": 8, "fact_errors": [], "notes": "ok"}

        def fake_call(judge, prompt, key, *args, **kwargs):
            if judge["id"] == "bad":
                return {"ok": False, "error": "empty", "raw": "", "usage": {}, "latency_ms": 1}
            return {"ok": True, "scores": scores, "raw": "{}", "usage": {}, "latency_ms": 1, "overall": 8.0}

        with patch.dict("os.environ", {"TEST_GOOD": "x", "TEST_BAD": "x", "TEST_FB": "x"}), \
             patch.object(judges, "load_config", return_value=cfg), \
             patch.object(judges, "call_judge", side_effect=fake_call), \
             patch.object(judges.db, "judge_available", return_value=True), \
             patch.object(judges.db, "record_judge_success"), \
             patch.object(judges.db, "record_judge_failure"):
            out = judges.judge_paper("p", "x" * 200, resolve(query_text="test"))

        self.assertEqual(out["confidence"], "medium")
        self.assertIn("fallback", out["judge_pool"])
        self.assertFalse(out["adjudicated"])


class JudgeRetryTests(unittest.TestCase):
    def test_empty_content_retries_with_more_room(self):
        scores = {"accuracy": 8, "coverage": 8, "critical": 8, "sources": 8, "structure": 8,
                  "action": 8, "fact_errors": [], "notes": "ok"}
        empty = Mock(status_code=200)
        empty.json.return_value = {"choices": [{"message": {"content": ""}}], "usage": {}}
        good = Mock(status_code=200)
        good.json.return_value = {"choices": [{"message": {"content": json.dumps(scores)}}], "usage": {}}
        cfg = {"id": "test", "base": "https://openrouter.ai/api/v1", "model": "m", "max_tokens": 4000}
        sent = []
        responses = [empty, empty, good]

        def fake_post(url, **kwargs):
            sent.append(kwargs["json"].copy())
            return responses.pop(0)

        with patch("grader.judges.requests.post", side_effect=fake_post):
            out = judges.call_judge(cfg, "prompt", "key", attempts=3, retry_base=0, retry_max=0)
        self.assertTrue(out["ok"])
        self.assertEqual(len(sent), 3)
        self.assertIn("response_format", sent[0])
        self.assertNotIn("response_format", sent[2])
        self.assertGreater(sent[2]["max_tokens"], 4000)

    def test_provider_preference_is_sent(self):
        scores = {"accuracy": 8, "coverage": 8, "critical": 8, "sources": 8, "structure": 8,
                  "action": 8, "fact_errors": [], "notes": "ok"}
        response = Mock(status_code=200)
        response.json.return_value = {"choices": [{"message": {"content": json.dumps(scores)}}], "usage": {}}
        cfg = {
            "id": "test", "base": "https://openrouter.ai/api/v1", "model": "m",
            "max_tokens": 4000,
            "provider": {"only": ["deepseek"], "allow_fallbacks": False},
        }
        sent = []
        def fake_post(url, **kwargs):
            sent.append(kwargs["json"].copy())
            return response
        with patch("grader.judges.requests.post", side_effect=fake_post):
            out = judges.call_judge(cfg, "prompt", "key", attempts=1)
        self.assertTrue(out["ok"])
        self.assertEqual(sent[0]["provider"], {"only": ["deepseek"], "allow_fallbacks": False})


class ProviderConfigTests(unittest.TestCase):
    def test_primary_provider_override(self):
        with patch.dict("os.environ", {"GRADER_PRIMARY_PROVIDER": "deepseek"}):
            cfg = load_config()
        primary = next(j for j in cfg["judges"] if j["id"] == "primary")
        self.assertEqual(primary["provider"], {"only": ["deepseek"], "allow_fallbacks": False})


if __name__ == "__main__":
    unittest.main()
