from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from formats import QA_SCHEMA
from scripts.wolf_corpus_qa import (
    _official_out_of_scope,
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
