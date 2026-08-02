from __future__ import annotations

import unittest

from ainiee_translation import _session_profile, fragment_translation_rows, merge_fragmented_rows
from models import AppSettings


class FragmentInputTests(unittest.TestCase):
    def test_structure_is_kept_out_of_model_input_and_restored_exactly(self) -> None:
        rows = [
            {
                "key": "parent",
                "original": "alpha \uE100beta\r\ngamma",
                "translation": "",
                "context": "case",
                "stage": 0,
            }
        ]

        fragments, ledger = fragment_translation_rows(rows)
        self.assertEqual(["alpha", "beta", "gamma"], [row["original"] for row in fragments])
        translated = [dict(row, translation=f"T{index}") for index, row in enumerate(fragments, 1)]

        merged, missing = merge_fragmented_rows(rows, translated, ledger)

        self.assertEqual({}, missing)
        self.assertEqual("T1 \uE100T2\r\nT3", merged[0]["translation"])

    def test_unstructured_text_stays_in_one_contextual_fragment(self) -> None:
        rows = [{"key": "plain", "original": "alpha beta", "context": "case"}]

        fragments, _ledger = fragment_translation_rows(rows)

        self.assertEqual(["alpha beta"], [row["original"] for row in fragments])

    def test_missing_fragment_is_reported_by_parent(self) -> None:
        rows = [{"key": "parent", "original": "alpha\uE100beta", "context": "case"}]
        fragments, ledger = fragment_translation_rows(rows)

        merged, missing = merge_fragmented_rows(rows, [dict(fragments[0], translation="T1")], ledger)

        self.assertEqual({"parent": ["parent::fragment:2"]}, missing)
        self.assertEqual("T1\uE100beta", merged[0]["translation"])

    def test_deepseek_v4_uses_explicit_low_reasoning(self) -> None:
        profile = _session_profile(
            AppSettings(api_base_url="https://tokenflux.dev/v1", api_model="deepseek-v4-flash"),
            "secret",
        )
        platform = profile["platforms"][profile["target_platform"]]

        self.assertTrue(platform["think_switch"])
        self.assertEqual("low", platform["think_depth"])

        other = _session_profile(
            AppSettings(api_base_url="https://example.com/v1", api_model="deepseek-v4-flash"),
            "secret",
        )
        other_platform = other["platforms"][other["target_platform"]]
        self.assertFalse(other_platform["think_switch"])


if __name__ == "__main__":
    unittest.main()
