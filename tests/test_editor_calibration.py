from __future__ import annotations

import unittest
from pathlib import Path

from scripts.editor_calibration import (
    CalibrationError,
    _case_records,
    _render_promoted_catalog,
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
                self.assertIsInstance(semantics["transfer"], str)
                self.assertNotEqual("opaque", semantics["transfer"])
                self.assertIn("data_effects", semantics)
            self.assertIsNone(command_effect(opcode, 999, 999))
            self.assertIsNone(command_semantics(opcode, 999, 999))


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
