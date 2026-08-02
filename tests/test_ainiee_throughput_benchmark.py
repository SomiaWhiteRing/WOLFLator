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

    def test_session_profile_always_uses_fixed_compatibility_fields(self) -> None:
        for base_url, model in (
            ("https://gateway.example/v1", "deepseek-v4-flash"),
            ("https://example.com/v1", "deepseek-v4-flash"),
            ("https://example.com/v1", "qwen3"),
        ):
            profile = _session_profile(
                AppSettings(api_base_url=base_url, api_model=model),
                "secret",
            )
            platform = profile["platforms"][profile["target_platform"]]

            self.assertEqual("deepseek", profile["target_platform"])
            self.assertEqual("online", platform["group"])
            self.assertEqual("DeepSeek", platform["name"])
            self.assertEqual("deepseek", platform["icon"])
            self.assertEqual(1.3, platform["temperature"])
            self.assertTrue(platform["think_switch"])
            self.assertEqual("low", platform["think_depth"])


if __name__ == "__main__":
    unittest.main()
