"""Network-free tests for the provisional calibration harness."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from grader import calibration as cal

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SCHEMA = """
CREATE TABLE papers (
  paper_id TEXT PRIMARY KEY,
  title TEXT,
  query_id TEXT,
  model_id TEXT,
  created_at TEXT
);
CREATE TABLE grades (
  grade_id TEXT PRIMARY KEY,
  paper_id TEXT NOT NULL,
  accuracy REAL,
  coverage REAL,
  critical REAL,
  sources REAL,
  structure REAL,
  action REAL,
  overall REAL,
  disagreement REAL,
  created_at TEXT
);
CREATE TABLE judge_runs (
  run_id TEXT PRIMARY KEY,
  paper_id TEXT,
  grade_id TEXT,
  judge_id TEXT,
  overall REAL,
  scores_json TEXT,
  parse_ok INTEGER,
  created_at TEXT
);
"""

ANCHORS_YAML = """
# six anchors: five for the metric fixture, one ambiguity probe
- label: "a1 paper_id"
  paper_id: "p1"
  expected:
    overall: 8.5
    accuracy: 7.5
    coverage: 7.5
    critical: 8.0
    sources: 6.5
    structure: 7.0
    action: 7.5

- label: "a2 latest grade"
  query_id: "Q2"
  model_id: "model-b"
  source: "test"
  expected:
    overall: 5.5
    accuracy: 6.5
    coverage: 6.5
    critical: 5.5
    sources: 4.5
    structure: 6.5
    action: 6.5

- label: "a3 ungraded paper"
  query_id: "Q3"
  model_id: "model-c"
  expected:
    overall: 7.0
    accuracy: 7.0
    coverage: 7.0
    critical: 7.0
    sources: 7.0
    structure: 7.0
    action: 7.0

- label: "a4 unknown paper"
  query_id: "Q9"
  model_id: "model-z"
  expected:
    overall: 7.0

- label: "a5 title filter"
  query_id: "Q1"
  model_id: "model-a"
  title: "Benchmark Q1 / model-a"
  expected:
    overall: 8.0
    accuracy: 7.5
    coverage: 7.5
    critical: 8.5
    sources: 6.0
    structure: 8.0
    action: 7.0

- label: "a6 ambiguity probe"
  query_id: "Q1"
  model_id: "model-a"
  expected:
    overall: 9.0
"""

# a6 makes the fixture file have six anchors; the compare fixture uses the
# first five only via `ANCHOR_LIMIT`, so the ambiguity probe stays separate.
ANCHOR_LIMIT = 5


def _scores(accuracy, coverage, critical, sources, structure, action) -> str:
    return json.dumps(
        {
            "accuracy": accuracy,
            "coverage": coverage,
            "critical": critical,
            "sources": sources,
            "structure": structure,
            "action": action,
        }
    )


class CalibrationFixture(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmpdir.name)
        self.db = self.root / "grades.db"
        self._create_db()
        self.anchors_path = self.root / "anchors.yaml"
        self.anchors_path.write_text(ANCHORS_YAML, encoding="utf-8")

    def tearDown(self):
        self.tmpdir.cleanup()

    def _create_db(self):
        connection = sqlite3.connect(self.db)
        connection.executescript(SCHEMA)
        papers = [
            ("p1", "Benchmark Q1 / model-a", "Q1", "model-a", "2026-01-01T00:00:00+00:00"),
            ("p1b", "LDR Q1 / model-a", "Q1", "model-a", "2026-01-02T00:00:00+00:00"),
            ("p2", "Benchmark Q2 / model-b", "Q2", "model-b", "2026-01-01T00:00:00+00:00"),
            ("p3", "Benchmark Q3 / model-c", "Q3", "model-c", "2026-01-01T00:00:00+00:00"),
        ]
        connection.executemany(
            "INSERT INTO papers(paper_id,title,query_id,model_id,created_at) VALUES(?,?,?,?,?)", papers
        )
        grades = [
            ("g1", "p1", 8.0, 7.0, 9.0, 6.0, 8.0, 7.0, 8.0, 0.2, "2026-01-05T00:00:00+00:00"),
            ("g1b", "p1b", 9.0, 9.0, 9.0, 9.0, 9.0, 9.0, 9.0, 0.1, "2026-01-06T00:00:00+00:00"),
            ("g2a", "p2", 5.0, 5.0, 4.0, 4.0, 6.0, 5.0, 5.0, 2.5, "2026-01-03T00:00:00+00:00"),
            ("g2b", "p2", 6.0, 6.0, 5.0, 4.0, 7.0, 6.0, 6.0, 0.3, "2026-01-04T00:00:00+00:00"),
        ]
        connection.executemany(
            "INSERT INTO grades(grade_id,paper_id,accuracy,coverage,critical,sources,structure,action,"
            "overall,disagreement,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            grades,
        )
        runs = [
            ("r1", "p1", "g1", "judge-1", 8.0, _scores(8.0, 7.0, 9.0, 6.0, 8.0, 7.0), 1),
            ("r2", "p1", "g1", "judge-2", 8.4, _scores(8.5, 7.5, 9.5, 6.5, 8.5, 7.5), 1),
            ("r3", "p2", "g2a", "judge-1", 4.0, _scores(4.0, 4.0, 3.0, 3.0, 5.0, 4.0), 1),
            ("r4", "p2", "g2a", "judge-2", 7.0, _scores(7.0, 7.0, 6.0, 6.0, 8.0, 7.0), 1),
            ("r5", "p2", "g2b", "judge-1", 6.0, _scores(6.0, 6.0, 5.0, 4.0, 7.0, 6.0), 1),
            ("r6", "p1b", "g1b", "judge-1", 9.0, _scores(9.0, 9.0, 9.0, 9.0, 9.0, 9.0), 1),
            ("r7", "p1b", "g1b", "judge-2", 9.0, _scores(9.0, 9.0, 9.0, 9.0, 9.0, 9.0), 1),
        ]
        connection.executemany(
            "INSERT INTO judge_runs(run_id,paper_id,grade_id,judge_id,overall,scores_json,parse_ok,created_at) "
            "VALUES(?,?,?,?,?,?,?,'2026-01-07T00:00:00+00:00')",
            runs,
        )
        connection.commit()
        connection.close()

    def anchors(self) -> list[dict]:
        all_anchors = cal.load_anchors(self.anchors_path)
        return all_anchors[:ANCHOR_LIMIT]


class YamlSubsetTests(unittest.TestCase):
    def test_parses_example_file(self):
        anchors = cal.load_anchors(PROJECT_ROOT / "calibration" / "human_scores.example.yaml")
        self.assertEqual(len(anchors), 3)
        self.assertEqual([a["expected"]["overall"] for a in anchors], [9.1, 8.63, 6.58])
        for anchor in anchors:
            self.assertEqual(set(anchor["expected"]), set(cal.SCORE_FIELDS))

    def test_quotes_comments_and_types(self):
        text = textwrap.dedent(
            """
            # leading comment
            - label: "hash # inside string"   # trailing comment
              query_id: 'Q1'
              model_id: model-x
              expected:
                overall: 8      # int becomes float in normalize
                accuracy: 7.5
            """
        )
        raw = cal.parse_simple_yaml(text)
        self.assertEqual(raw[0]["label"], "hash # inside string")
        self.assertEqual(raw[0]["query_id"], "Q1")
        self.assertEqual(raw[0]["expected"]["overall"], 8)
        normalized = cal.normalize_anchors(raw)
        self.assertEqual(normalized[0]["expected"]["overall"], 8.0)

    def test_rejects_tabs(self):
        with self.assertRaises(cal.YamlSubsetError):
            cal.parse_simple_yaml("\t- label: x\n")

    def test_rejects_flow_collections(self):
        with self.assertRaises(cal.YamlSubsetError):
            cal.parse_simple_yaml("- label: x\n  expected: {overall: 8}\n")

    def test_rejects_block_scalars(self):
        with self.assertRaises(cal.YamlSubsetError):
            cal.parse_simple_yaml("- label: x\n  notes: |\n    hello\n")

    def test_rejects_duplicate_keys(self):
        with self.assertRaises(cal.YamlSubsetError):
            cal.parse_simple_yaml('- label: "x"\n  label: "y"\n')

    def test_yaml_roundtrip(self):
        anchors = cal.load_anchors(PROJECT_ROOT / "calibration" / "human_scores.example.yaml")
        reparsed = cal.parse_simple_yaml(cal.anchors_to_yaml(anchors))
        again = cal.normalize_anchors(reparsed)
        self.assertEqual([a["label"] for a in again], [a["label"] for a in anchors])
        self.assertEqual([a["expected"] for a in again], [a["expected"] for a in anchors])
        self.assertEqual([a["paper_id"] for a in again], [a["paper_id"] for a in anchors])


class AnchorValidationTests(CalibrationFixture):
    def test_json_list_and_wrapper(self):
        raw = [{"query_id": "Q1", "model_id": "model-a", "expected": {"overall": 8}}]
        list_path = self.root / "list.json"
        list_path.write_text(json.dumps(raw))
        wrapped_path = self.root / "wrapped.json"
        wrapped_path.write_text(json.dumps({"anchors": raw}))
        self.assertEqual(cal.load_anchors(list_path)[0]["expected"]["overall"], 8.0)
        self.assertEqual(cal.load_anchors(wrapped_path)[0]["expected"]["overall"], 8.0)

    def test_requires_matching_key(self):
        with self.assertRaises(cal.CalibrationError):
            cal.normalize_anchor({"label": "x", "expected": {"overall": 8}})

    def test_requires_overall(self):
        with self.assertRaises(cal.CalibrationError):
            cal.normalize_anchor({"query_id": "Q1", "model_id": "m", "expected": {"accuracy": 8}})

    def test_rejects_out_of_range(self):
        with self.assertRaises(cal.CalibrationError):
            cal.normalize_anchor(
                {"query_id": "Q1", "model_id": "m", "expected": {"overall": 11}}
            )

    def test_rejects_unknown_fields(self):
        with self.assertRaises(cal.CalibrationError):
            cal.normalize_anchor(
                {"query_id": "Q1", "model_id": "m", "expected": {"overall": 8}, "typo": 1}
            )

    def test_rejects_empty_anchor_list(self):
        with self.assertRaises(cal.CalibrationError):
            cal.normalize_anchors([])


class CompareTests(CalibrationFixture):
    def test_counts_and_missing_reasons(self):
        report = cal.compare(self.anchors(), self.db)
        counts = report["counts"]
        self.assertEqual(counts["anchors"], 5)
        self.assertEqual(counts["matched"], 3)
        self.assertEqual(counts["missing"], 2)
        self.assertEqual(counts["rows"], 3)
        self.assertEqual(counts["ambiguous_matches"], 0)
        self.assertEqual(counts["papers_matched_but_ungraded"], 1)
        reasons = {row["label"]: row["reason"] for row in report["missing"]}
        self.assertEqual(reasons["a3 ungraded paper"], "paper matched but has no grade")
        self.assertEqual(reasons["a4 unknown paper"], "no paper matches query_id+model_id")

    def test_overall_mae_and_signed_bias(self):
        report = cal.compare(self.anchors(), self.db)
        self.assertAlmostEqual(report["mae"]["overall"], 0.3333, places=4)
        self.assertAlmostEqual(report["bias"]["overall"], 0.0, places=4)
        self.assertEqual(report["n"]["overall"], 3)

    def test_per_criterion_mae(self):
        report = cal.compare(self.anchors(), self.db)
        expected = {
            "accuracy": 0.5,
            "coverage": 0.5,
            "critical": 0.6667,
            "sources": 0.3333,
            "structure": 0.5,
            "action": 0.3333,
        }
        for field, value in expected.items():
            self.assertAlmostEqual(report["mae"][field], value, places=4, msg=field)

    def test_per_criterion_bias(self):
        report = cal.compare(self.anchors(), self.db)
        expected = {
            "accuracy": 0.1667,
            "coverage": -0.5,
            "critical": 0.3333,
            "sources": -0.3333,
            "structure": 0.5,
            "action": -0.3333,
        }
        for field, value in expected.items():
            self.assertAlmostEqual(report["bias"][field], value, places=4, msg=field)

    def test_latest_grade_is_used_by_default(self):
        report = cal.compare(self.anchors(), self.db)
        row = next(r for r in report["rows"] if r["label"] == "a2 latest grade")
        self.assertEqual(row["grade_id"], "g2b")
        self.assertEqual(row["actual"]["overall"], 6.0)
        self.assertEqual(row["diff"]["overall"], 0.5)

    def test_title_filter_disambiguates(self):
        report = cal.compare(self.anchors(), self.db)
        row = next(r for r in report["rows"] if r["label"] == "a5 title filter")
        self.assertEqual(row["paper_id"], "p1")
        self.assertEqual(row["matched_by"], "query_model_title")
        self.assertEqual(row["actual"]["overall"], 8.0)

    def test_missing_title_reports_ambiguity(self):
        anchors = cal.load_anchors(self.anchors_path)[ANCHOR_LIMIT:]
        report = cal.compare(anchors, self.db)
        self.assertEqual(report["counts"]["matched"], 1)
        self.assertEqual(report["counts"]["ambiguous_matches"], 1)
        row = report["rows"][0]
        self.assertEqual(row["paper_id"], "p1b")
        self.assertEqual(row["actual"]["overall"], 9.0)

    def test_all_grades_reports_every_stored_grade(self):
        report = cal.compare(self.anchors(), self.db, all_grades=True)
        self.assertEqual(report["counts"]["matched"], 3)
        self.assertEqual(report["counts"]["rows"], 4)
        self.assertFalse(report["latest_grade_only"])
        a2 = [r for r in report["rows"] if r["label"] == "a2 latest grade"]
        self.assertEqual({r["grade_id"] for r in a2}, {"g2a", "g2b"})

    def test_read_only_connection_does_not_change_db(self):
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        cal.compare(self.anchors(), self.db)
        after = hashlib.sha256(self.db.read_bytes()).hexdigest()
        self.assertEqual(before, after)


class SpreadTests(CalibrationFixture):
    def test_spread_from_matching_grade_only(self):
        report = cal.compare(self.anchors(), self.db)
        spread = report["inter_judge_spread"]
        self.assertEqual(spread["grades_with_spread"], 1)
        self.assertEqual(spread["unique_grades"], 2)
        self.assertAlmostEqual(spread["overall_mean"], 0.4, places=4)
        self.assertAlmostEqual(spread["overall_max"], 0.4, places=4)
        self.assertAlmostEqual(spread["criteria_mean"]["accuracy"], 0.5, places=4)
        self.assertAlmostEqual(spread["criteria_mean"]["critical"], 0.5, places=4)
        # g2a's 3.0 spread belongs to an older grade and must not leak in.
        self.assertLess(spread["overall_max"], 1.0)

    def test_stored_disagreement_uses_unique_grades(self):
        report = cal.compare(self.anchors(), self.db)
        self.assertAlmostEqual(
            report["inter_judge_spread"]["stored_disagreement_mean"], 0.25, places=4
        )

    def test_spread_unavailable_without_judge_runs(self):
        connection = sqlite3.connect(self.db)
        connection.execute("DELETE FROM judge_runs")
        connection.commit()
        connection.close()
        report = cal.compare(self.anchors(), self.db)
        spread = report["inter_judge_spread"]
        self.assertEqual(spread["grades_with_spread"], 0)
        self.assertIsNone(spread["overall_mean"])
        self.assertIsNone(spread["overall_max"])


class ImporterTests(CalibrationFixture):
    def test_quality_scores_dict_shape(self):
        path = self.root / "quality_scores.json"
        path.write_text(
            json.dumps(
                {
                    "Q1": {
                        "model-x": {
                            "scores": {
                                "accuracy": 8,
                                "coverage": 7,
                                "critical": 9,
                                "sources": 6,
                                "structure": 8,
                                "action": 7,
                                "overall": 8.1,
                            },
                            "notes": "example",
                            "report_usable": True,
                        }
                    },
                    "Q2": {
                        "model-y": {
                            "scores": {"overall": 5.0},
                            "report_usable": False,
                        }
                    },
                }
            )
        )
        with contextlib.redirect_stderr(io.StringIO()):
            anchors = cal.import_benchmark(path)
        self.assertEqual(len(anchors), 1)
        self.assertEqual(anchors[0]["query_id"], "Q1")
        self.assertEqual(anchors[0]["model_id"], "model-x")
        self.assertEqual(anchors[0]["expected"]["overall"], 8.1)
        self.assertIn("provisional", anchors[0]["source"])

    def test_benchmark_list_shape_skips_noncompleted(self):
        path = self.root / "benchmark_grades.json"
        path.write_text(
            json.dumps(
                [
                    {
                        "query": "Q4",
                        "model": "model-z",
                        "paper_id": "p4",
                        "status": "completed",
                        "overall": 7.5,
                        "accuracy": 7.0,
                        "coverage": 8.0,
                        "critical": 7.0,
                        "sources": 6.0,
                        "structure": 8.0,
                        "action": 7.0,
                    },
                    {"query": "Q4", "model": "model-w", "status": "failed", "overall": 1.0},
                ]
            )
        )
        with contextlib.redirect_stderr(io.StringIO()):
            anchors = cal.import_benchmark(path)
        self.assertEqual(len(anchors), 1)
        self.assertEqual(anchors[0]["paper_id"], "p4")
        self.assertEqual(anchors[0]["expected"]["overall"], 7.5)
        self.assertEqual(set(anchors[0]["expected"]), set(cal.SCORE_FIELDS))

    def test_generated_yaml_is_reloadable(self):
        path = self.root / "quality_scores.json"
        path.write_text(
            json.dumps(
                {
                    "Q1": {
                        "model-x": {
                            "scores": {field: 8 for field in cal.SCORE_FIELDS},
                            "notes": "line one\nline two: with colon",
                        }
                    }
                }
            )
        )
        anchors = cal.import_benchmark(path)
        yaml_text = cal.anchors_to_yaml(anchors)
        reloaded = cal.normalize_anchors(cal.parse_simple_yaml(yaml_text))
        self.assertEqual(reloaded[0]["expected"], anchors[0]["expected"])
        self.assertEqual(reloaded[0]["notes"], anchors[0]["notes"])


class CliTests(CalibrationFixture):
    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "grader.calibration", *args],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_cli_writes_json_report_and_leaves_db_untouched(self):
        out = self.root / "report.json"
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        proc = self._run(
            "--anchors", str(self.anchors_path), "--db", str(self.db),
            "--out", str(out), "--format", "json",
        )
        after = hashlib.sha256(self.db.read_bytes()).hexdigest()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(before, after)
        stdout_report = json.loads(proc.stdout)
        file_report = json.loads(out.read_text())
        self.assertEqual(stdout_report["counts"], file_report["counts"])
        self.assertEqual(file_report["counts"]["matched"], 4)  # includes a6 probe
        self.assertEqual(file_report["counts"]["missing"], 2)

    def test_cli_strict_exits_one_on_missing(self):
        proc = self._run(
            "--anchors", str(self.anchors_path), "--db", str(self.db), "--strict", "--quiet"
        )
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(proc.stdout, "")

    def test_cli_missing_database_exits_two(self):
        proc = self._run(
            "--anchors", str(self.anchors_path), "--db", str(self.root / "nope.db"), "--quiet"
        )
        self.assertEqual(proc.returncode, 2)
        self.assertIn("database not found", proc.stderr)

    def test_cli_import_benchmark_writes_anchors(self):
        source = self.root / "quality_scores.json"
        source.write_text(
            json.dumps(
                {
                    "Q1": {
                        "model-x": {
                            "scores": {
                                "overall": 8.0,
                                "accuracy": 8,
                                "coverage": 8,
                                "critical": 8,
                                "sources": 8,
                                "structure": 8,
                                "action": 8,
                            }
                        }
                    }
                }
            )
        )
        out = self.root / "generated.yaml"
        proc = self._run("--import-benchmark", str(source), "--write-anchors", str(out))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        anchors = cal.load_anchors(out)
        self.assertEqual(len(anchors), 1)
        self.assertEqual(anchors[0]["expected"]["overall"], 8.0)


if __name__ == "__main__":
    unittest.main()
