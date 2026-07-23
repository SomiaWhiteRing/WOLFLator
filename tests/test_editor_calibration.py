from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.editor_calibration import (
    CalibrationError,
    _catalog_report,
    _case_records,
    _render_promoted_catalog,
    _safe_extract,
    _scan_auto,
    _validate_manual_cases,
)
from wolf_command_catalog import (
    CALIBRATED_SHAPES,
    COMMAND_CATALOG,
    EXCLUDED_COMMANDS,
    MANUAL_CALIBRATION_CASES,
    PRO_OPCODE,
    command_effect,
    command_semantics,
)


class EditorCalibrationTests(unittest.TestCase):
    def test_free_catalog_has_one_effect_and_never_accepts_unknown_shapes(self):
        effects = {
            "no_write",
            "numeric_write",
            "string_read",
            "string_write",
            "condition",
            "control_flow",
            "database",
            "event_call",
            "opaque",
        }
        self.assertNotIn(PRO_OPCODE, COMMAND_CATALOG)
        self.assertEqual("excluded_pro", EXCLUDED_COMMANDS[PRO_OPCODE]["status"])
        for opcode, (_name, effect, _evidence) in COMMAND_CATALOG.items():
            self.assertIn(effect, effects)
            for int_count, string_count in CALIBRATED_SHAPES.get(opcode, ()):
                self.assertEqual(effect, command_effect(opcode, int_count, string_count))
                semantics = command_semantics(opcode, int_count, string_count)
                self.assertIsNotNone(semantics)
                self.assertTrue(semantics["semantic_complete"])
                self.assertNotIn("encoded_parameter", semantics["integer_roles"])
                self.assertIn(semantics["transfer"], {
                    "preserve", "numeric_write", "string_read", "string_write",
                    "condition", "control_flow", "database", "event_call", "opaque",
                })
            self.assertIsNone(command_effect(opcode, 999, 999))
            self.assertIsNone(command_semantics(opcode, 999, 999))

    def test_auto_inventory_keeps_shapes_and_evidence_locations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            auto = root / "CommonEvent.dat.Auto.txt"
            auto.write_text(
                "[COMMON_EVENT_TEXT_OUTPUT]\n"
                "[150][2,0]<0>(1,1600001)()\n"
                "[150][2,0]<0>(2,1600002)()\n"
                "[1000][1,0]<0>(1)()\n",
                encoding="utf-8",
            )
            report = _scan_auto([root])
            self.assertEqual(1, report["file_count"])
            self.assertEqual(3, report["command_count"])
            shapes = {
                (item["opcode"], item["int_count"], item["string_count"]): item
                for item in report["shapes"]
            }
            self.assertEqual(2, shapes[(150, 2, 0)]["count"])
            self.assertEqual("no_write", shapes[(150, 2, 0)]["catalog_effect"])
            self.assertIsNone(shapes[(1000, 1, 0)]["catalog_effect"])
            self.assertEqual(2, shapes[(150, 2, 0)]["examples"][0]["line"])
            report["shapes"].append({
                "opcode": 150,
                "int_count": 999,
                "string_count": 999,
                "count": 1,
                "examples": [],
                "catalog_effect": None,
            })
            _commands, unresolved = _catalog_report(report)
            self.assertTrue(any(
                item["opcode"] == 150
                and item["reason"] == "语料出现未经校准的参数形状"
                for item in unresolved
            ))

    def test_safe_extract_rejects_parent_escape(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "bad.zip"
            output = root / "output"
            output.mkdir()
            (output / "old.txt").write_text("keep", encoding="utf-8")
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("../escape.txt", "bad")
            with self.assertRaises(CalibrationError):
                _safe_extract(archive, output)
            self.assertFalse((root / "escape.txt").exists())
            self.assertEqual("keep", (output / "old.txt").read_text(encoding="utf-8"))

    def test_manual_cases_require_marker_shape_and_differential_evidence(self):
        lines = ["WoditorEvCOMMAND_START"]
        for case in MANUAL_CALIBRATION_CASES:
            lines.extend((f'[103][0,1]<0>()("{case["id"]}")', str(case["record"])))
        lines.append("WoditorEvCOMMAND_END")
        event_code = "\n".join(lines)
        records = _case_records(event_code)
        self.assertEqual(len(MANUAL_CALIBRATION_CASES), len(records))
        evidence = _validate_manual_cases(event_code)
        self.assertEqual(len(MANUAL_CALIBRATION_CASES), len(evidence))
        self.assertEqual(
            "differential",
            next(item["level"] for item in evidence if item["opcode"] == 251),
        )
        with self.assertRaises(CalibrationError):
            _validate_manual_cases(event_code.replace("CAL-251-B.csv", "CAL-251-C.csv"))

        catalog = Path("wolf_command_catalog.py").read_text(encoding="utf-8")
        promoted = _render_promoted_catalog(catalog, evidence)
        self.assertIn("105: ((0, 0),)", promoted)
        self.assertIn("251: 'differential'", promoted)
        self.assertEqual(1, promoted.count("# BEGIN WOLFLATOR EDITOR CALIBRATION"))


if __name__ == "__main__":
    unittest.main()
