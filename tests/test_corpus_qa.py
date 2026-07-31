from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from formats import QA_SCHEMA
from scripts.wolf_corpus_qa import (
    _aggregate,
    _official_out_of_scope,
    _out_of_scope_evidence,
    _sandbox_root,
    build_parser,
    discover,
    pseudo_translation,
    select_manifest,
    verify,
)


def _coverage() -> dict[str, object]:
    return {
        "shape_coverage": {"ratio": 1.0, "missing": 0},
        "semantic_coverage": {"ratio": 1.0, "missing": 0},
        "cfg_coverage": {"ratio": 1.0, "missing": 0},
        "call_target_coverage": {"ratio": 1.0, "missing": 0},
        "data_effect_coverage": {"ratio": 1.0, "missing": 0},
        "opaque_effects": 0,
        "unexplained_data_side_effects": 0,
    }


class CorpusQaTests(unittest.TestCase):
    def test_run_parser_accepts_targeted_candidate_ids(self):
        args = build_parser().parse_args(
            [
                "run",
                "--manifest",
                "corpus.json",
                "--editor",
                "Editor.exe",
                "--run-dir",
                "targeted",
                "--candidate-id",
                "one",
                "--candidate-id",
                "two",
            ]
        )
        self.assertEqual(["one", "two"], args.candidate_id)
        self.assertEqual(Path("targeted"), args.run_dir)

    def test_defect_aggregate_keeps_analysis_evidence(self):
        coverage = _coverage()
        aggregate = _aggregate(
            {"scan_complete": True, "access_errors": [], "candidates": [{}]},
            [
                {
                    "candidate_id": "abc",
                    "path": "game",
                    "status": "DEFECT",
                    "failure_stage": "acceptance",
                    "failure_class": "coverage_gate",
                    "coverage": coverage,
                    "blocking_issue_count": 2,
                    "error": "failed after analysis",
                }
            ],
        )
        result = aggregate["reports"][0]
        self.assertEqual(coverage, result["coverage"])
        self.assertEqual("acceptance", result["failure_stage"])
        self.assertEqual("coverage_gate", result["failure_class"])
        self.assertEqual(2, result["blocking_issue_count"])

    def test_official_scope_dialogs_require_exact_positive_evidence(self):
        legacy = _official_out_of_scope(
            [
                "Warning! | The process completed, but the Editor.exe version used "
                "to create the game data seems to be old! Runtime Error!"
            ]
        )
        self.assertEqual("legacy_editor_data", legacy[0]["kind"])
        damaged = _official_out_of_scope(["Error | Map Size Error! [Size 0 Error]"])
        self.assertEqual("damaged_map_data", damaged[0]["kind"])
        self.assertEqual([], _official_out_of_scope(["Warning! | Unknown warning"]))

    def test_qa_sandbox_uses_short_public_path(self):
        with mock.patch.dict("os.environ", {"PUBLIC": r"C:\Users\Public"}):
            root = _sandbox_root("a" * 64)
        self.assertEqual(
            Path(r"C:\Users\Public\WOLFLator\corpus-qa") / ("a" * 16),
            root,
        )

    def test_discovery_deduplicates_and_pseudo_text_preserves_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            games = root / "games"
            output = root / "qa"
            for name in ("one", "two"):
                game = games / name
                game.mkdir(parents=True)
                (game / "Game.exe").write_bytes(b"game")
                (game / "Data.wolf").write_bytes(b"data")

            manifest = discover([games], output)
            self.assertTrue(manifest["scan_complete"])
            self.assertEqual(2, manifest["path_count"])
            self.assertEqual(1, manifest["unique_count"])
            self.assertEqual(1, len(manifest["candidates"][0]["duplicates"]))

            scoped = select_manifest(output / "corpus-manifest.json", root / "packed", "packed")
            self.assertTrue(scoped["scan_complete"])
            self.assertEqual(1, scoped["unique_count"])
            self.assertEqual("packed", scoped["scope"]["kind"])
            self.assertEqual(0, len(scoped["access_errors"]))
            self.assertEqual(
                manifest["scan_complete"], scoped["scope"]["source_scan_complete"]
            )

            original = "\\c[1]AB\nCD"
            translated = pseudo_translation(original, "stable-key")
            self.assertNotEqual(original, translated)
            self.assertTrue(translated.startswith("\\c[1]"))
            self.assertEqual(original.count("\n"), translated.count("\n"))

            evidence = _out_of_scope_evidence(
                {
                    "unknown_commands": [
                        {"opcode": 1000, "shape": "ints=1,strings=0", "count": 2},
                        {"opcode": 112, "shape": "unsupported-flag", "count": 1},
                    ]
                }
            )
            self.assertEqual("pro_opcode", evidence[0]["kind"])
            self.assertEqual(1, len(evidence))

    def test_verify_rejects_no_pass_evidence_and_accepts_complete_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory)
            candidate_id = "abc"
            report_dir = run / "games" / candidate_id
            report_dir.mkdir(parents=True)
            report = {
                "kind": "qa-game-report",
                "schema": QA_SCHEMA,
                "candidate_id": candidate_id,
                "status": "PASS",
                "coverage": _coverage(),
                "source_fingerprint_before": "same",
                "source_fingerprint_after": "same",
                "analysis_hash": "same",
                "repeat_analysis_hash": "same",
                "structural_diff": {"status": "passed", "differences": []},
                "translated_replay": {
                    "control_flow_equivalent": True,
                    "data_effects_equivalent": True,
                    "condition_results_equivalent": True,
                    "resource_targets_equivalent": True,
                    "differences": [],
                },
            }
            (report_dir / "report.json").write_text(
                json.dumps(report), encoding="utf-8"
            )
            aggregate = {
                "kind": "qa-run",
                "schema": QA_SCHEMA,
                "scan_complete": True,
                "access_error_count": 0,
                "candidate_total": 1,
                "eligible_total": 1,
                "pass_total": 1,
                "out_of_scope_total": 0,
                "defect_total": 0,
                "incomplete_total": 0,
                "reports": [{"candidate_id": candidate_id, "status": "PASS"}],
            }
            (run / "run.json").write_text(json.dumps(aggregate), encoding="utf-8")
            (run / "environment.json").write_text(
                json.dumps(
                    {
                        "kind": "qa-environment",
                        "schema": QA_SCHEMA,
                        "git": {
                            "available": True,
                            "commit": "a" * 40,
                            "worktree_clean": True,
                            "error": "",
                        },
                        "editor": {
                            "version": "3.713.2026.718",
                            "sha256": "2ce5639f669643ded07a9390ef05054b8f95acbfa1b4dc1f4936246df5eae0c3",
                        },
                    }
                ),
                encoding="utf-8",
            )

            passed, errors, _result = verify(run)
            self.assertTrue(passed, errors)
            self.assertTrue((run / "验收报告.md").is_file())

            aggregate["scan_complete"] = False
            aggregate["access_error_count"] = 1
            (run / "run.json").write_text(json.dumps(aggregate), encoding="utf-8")
            passed, errors, _result = verify(run)
            self.assertFalse(passed)
            self.assertTrue(any("发现不完整" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
