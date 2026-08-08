from __future__ import annotations

import unittest

from ainiee_translation import (
    _rules_with_control_protection,
    _session_profile,
    fragment_translation_rows,
    merge_fragmented_rows,
)
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

    def test_ruby_anchor_stays_with_visible_text_during_fragment_retry(self) -> None:
        anchor = "天翔[[WOLFLATOR_RUBY_0]]ツバサです。"
        rows = [{"key": "ruby", "original": f"\uE100{anchor}\uE101", "context": "case"}]

        fragments, _ledger = fragment_translation_rows(rows)

        self.assertEqual([anchor], [row["original"] for row in fragments])

    def test_ruby_anchor_is_excluded_from_translation(self) -> None:
        rules = _rules_with_control_protection({})
        regexes = {item["regex"] for item in rules["exclusion_list_data"]}

        self.assertIn(r"\[\[WOLFLATOR_RUBY_[0-9]+\]\]", regexes)

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

    def test_session_profile_projects_ainiee_controls(self) -> None:
        settings = AppSettings(
            api_base_url="https://example.com/v1",
            api_model="model",
            api_top_p=0.75,
            api_temperature=0.65,
            api_presence_penalty=-0.25,
            api_frequency_penalty=0.4,
            api_think_switch=False,
            api_think_depth="xhigh",
            translation_retry_count=0,
            translation_pre_line_counts=0,
            translation_enable_smart_round_limit=True,
            translation_smart_round_limit_multiplier=4,
            translation_enable_retry_backoff=False,
        )
        profile = _session_profile(settings, "secret")
        platform = profile["platforms"][profile["target_platform"]]

        self.assertEqual(0.75, platform["top_p"])
        self.assertEqual(0.65, platform["temperature"])
        self.assertEqual(-0.25, platform["presence_penalty"])
        self.assertEqual(0.4, platform["frequency_penalty"])
        self.assertFalse(platform["think_switch"])
        self.assertEqual("xhigh", platform["think_depth"])
        self.assertFalse(profile["think_switch"])
        self.assertEqual("xhigh", profile["think_depth"])
        self.assertEqual(0, profile["retry_count"])
        self.assertEqual(0, profile["pre_line_counts"])
        self.assertTrue(profile["enable_smart_round_limit"])
        self.assertEqual(4, profile["smart_round_limit_multiplier"])
        self.assertFalse(profile["enable_retry_backoff"])

        numeric = _session_profile(
            AppSettings(
                api_base_url="https://example.com/v1",
                api_model="model",
                api_think_depth="256",
            ),
            "secret",
        )
        self.assertEqual(256, numeric["think_depth"])


if __name__ == "__main__":
    unittest.main()
