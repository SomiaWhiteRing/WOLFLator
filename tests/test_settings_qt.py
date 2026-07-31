import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QSizePolicy,
)

from formats import ARTIFACT_EPOCH
from app import (
    MainWindow,
    SettingsDialog,
    _completed_import_protection,
)
from models import (
    AppSettings,
    RunMode,
    Stage,
    StageStatus,
    TranslationItem,
)
from pipeline import PipelineStateEvent, create_project, load_manifest
from proofread import build_worker_input, load_report, make_report, proofread_paths, save_report
from settings import SettingsStore, protect_secret, unprotect_secret
from wolf_tools import dump_items


class SettingsQtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_dpapi_round_trip(self):
        encrypted = protect_secret("test-secret")
        self.assertNotIn("test-secret", encrypted)
        self.assertEqual("test-secret", unprotect_secret(encrypted))


    def test_dialog_loads_separate_api_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.ini")
            item = AppSettings(
                api_base_url="https://translate.example/v1",
                api_model="translate-model",
                api_threads=12,
                glossary_api_base_url="https://glossary.example/v1",
                glossary_api_model="glossary-model",
                glossary_api_threads=2,
                glossary_chunk_chars=456_789,
                glossary_api_max_tokens=65_535,
            )
            store.save(item)
            dialog = SettingsDialog(store)
            self.assertEqual(3, dialog.settings_tabs.count())
            self.assertEqual(
                ["工具与目录", "术语生成 API", "AiNiee 翻译 API"],
                [
                    dialog.settings_tabs.tabText(index)
                    for index in range(dialog.settings_tabs.count())
                ],
            )
            self.assertTrue(
                dialog.settings_tabs.widget(0).isAncestorOf(dialog.wolf_path)
            )
            self.assertTrue(
                dialog.settings_tabs.widget(1).isAncestorOf(dialog.glossary_api_url)
            )
            self.assertTrue(dialog.settings_tabs.widget(2).isAncestorOf(dialog.api_url))
            self.assertEqual("https://translate.example/v1", dialog.api_url.text())
            self.assertEqual("https://glossary.example/v1", dialog.glossary_api_url.text())
            self.assertEqual(12, dialog.api_threads.value())
            self.assertTrue(dialog.translation_token_mode.isChecked())
            self.assertEqual(256, dialog.translation_token_limit.value())
            self.assertEqual(8, dialog.translation_line_limit.value())
            self.assertEqual(1, dialog.translation_retry_min_lines.value())
            self.assertEqual(6, dialog.translation_rounds.value())
            self.assertEqual("", dialog.translation_token_limit.suffix())
            self.assertEqual("", dialog.translation_line_limit.suffix())
            self.assertEqual("", dialog.translation_retry_min_lines.suffix())
            self.assertEqual("Token", dialog.translation_token_unit.text())
            self.assertEqual("条", dialog.translation_line_unit.text())
            self.assertEqual("条", dialog.translation_retry_unit.text())
            self.assertEqual("轮", dialog.translation_rounds_unit.text())
            self.assertEqual(140, dialog.translation_token_limit.maximumWidth())
            self.assertEqual(90, dialog.translation_line_limit.maximumWidth())
            self.assertEqual(90, dialog.translation_retry_min_lines.maximumWidth())
            self.assertEqual(140, dialog.translation_rounds.maximumWidth())
            self.assertEqual(
                QSizePolicy.Fixed,
                dialog.translation_chunk_stack.sizePolicy().verticalPolicy(),
            )
            self.assertEqual(2, dialog.glossary_api_threads.value())
            self.assertEqual(456_789, dialog.glossary_chunk_chars.value())
            self.assertEqual(65_535, dialog.glossary_api_max_tokens.value())
            dialog.close()


    def test_completed_import_protection_is_reused_for_font_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = root / "game"
            (game / "Data" / "BasicData").mkdir(parents=True)
            (game / "Game.exe").write_bytes(b"game")
            (game / "Data" / "BasicData" / "Game.dat").write_bytes(b"data")
            manifest = load_manifest(create_project(root / "projects", game))
            items = root / "items.json"
            report = root / "import-protection.json"
            items.write_text("{}", encoding="utf-8")
            report.write_text(
                json.dumps(
                    {
                        "kind": "import-protection",
                        "epoch": ARTIFACT_EPOCH,
                        "protected_keys": ["key"],
                        "safe_to_translate": [],
                        "keep_original": [],
                        "translation_overrides": {},
                        "approvals": {},
                        "unresolved_scopes": [],
                        "translated_replay": {},
                        "structural_diff": {},
                        "entries": [],
                        "summary": {},
                        "middle_dot_normalized": [],
                    }
                ),
                encoding="utf-8",
            )
            record = manifest.version.stage(Stage.IMPORT)
            record.status = StageStatus.COMPLETED
            record.artifacts["import_protection"] = str(report)
            self.assertEqual(
                ["key"],
                _completed_import_protection(manifest, items)["protected_keys"],
            )


    def test_incompatible_project_manifest_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = root / "game"
            (game / "Data" / "BasicData").mkdir(parents=True)
            (game / "Game.exe").write_bytes(b"game")
            (game / "Data" / "BasicData" / "Game.dat").write_bytes(b"data")
            manifest_path = create_project(root / "projects", game)
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            data.pop("schema")
            manifest_path.write_text(json.dumps(data), encoding="utf-8")
            store = SettingsStore(root / "settings.ini")
            store.save(
                AppSettings(
                    projects_root=str(root / "projects"),
                    last_project=str(manifest_path),
                )
            )
            with patch("app.SettingsStore", return_value=store), patch.object(MainWindow, "_open_settings"):
                window = MainWindow()
                self.assertIn("已拒绝 1 个不兼容的项目清单", window.status_label.text())
                self.assertIn("项目格式不兼容", window.status_label.toolTip())
                window.close()

    def test_step_mode_progress_and_running_ui_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = root / "game"
            (game / "Data" / "BasicData").mkdir(parents=True)
            (game / "Game.exe").write_bytes(b"game")
            (game / "Data" / "BasicData" / "Game.dat").write_bytes(b"data")
            projects = root / "projects"
            manifest_path = create_project(projects, game)
            manifest = load_manifest(manifest_path)
            manifest.run_mode = RunMode.STEP
            manifest.version.stage(Stage.COPY).status = StageStatus.COMPLETED
            manifest.version.stage(Stage.EXTRACT).status = StageStatus.FAILED
            manifest.version.stage(Stage.EXTRACT).error = "test error"
            Path(manifest_path).write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
            store = SettingsStore(root / "settings.ini")
            store.save(AppSettings(projects_root=str(projects), last_project=str(manifest_path)))
            with patch("app.SettingsStore", return_value=store), patch.object(MainWindow, "_open_settings"):
                window = MainWindow()
                self.assertEqual(1, window.progress.maximum())
                self.assertTrue(window.step_buttons[Stage.COPY].isEnabled())
                self.assertTrue(window.retry_button.isEnabled())
                self.assertEqual(8, len(window.step_checks))
                self.assertFalse(window.run_range_button.isEnabled())
                window.step_checks[Stage.UNPACK].setChecked(True)
                window.step_checks[Stage.EXTRACT].setChecked(True)
                self.assertEqual(
                    (Stage.UNPACK, Stage.EXTRACT), window._selected_step_range()
                )
                self.assertTrue(window.run_range_button.isEnabled())
                self.assertEqual("已选择：解包 至 导出文本", window.step_range_summary.text())
                with patch.object(window, "_start") as start:
                    window.run_range_button.click()
                    start.assert_called_once_with(stages=(Stage.UNPACK, Stage.EXTRACT))
                window.step_checks[Stage.RELEASE].setChecked(True)
                self.assertIsNone(window._selected_step_range())
                self.assertFalse(window.run_range_button.isEnabled())
                window.step_checks[Stage.RELEASE].setChecked(False)
                window._set_pipeline_ui_locked(True)
                self.assertFalse(window.settings_button.isEnabled())
                self.assertFalse(window.project_combo.isEnabled())
                self.assertFalse(window.new_project_button.isEnabled())
                self.assertFalse(window.add_version_button.isEnabled())
                self.assertFalse(window.one_click.isEnabled())
                self.assertFalse(window.run_range_button.isEnabled())
                self.assertTrue(all(not check.isEnabled() for check in window.step_checks.values()))
                self.assertFalse(window.open_release_button.isEnabled())
                self.assertTrue(window.stop_button.isEnabled())
                self.assertTrue(all(not window.tabs.isTabEnabled(index) for index in range(1, 6)))

                with patch.object(window, "_load_project_view") as reload_view:
                    window._stage_progress(1, 8, Stage.COPY.value)
                    reload_view.assert_not_called()
                window._stage_state(
                    PipelineStateEvent(Stage.COPY, StageStatus.COMPLETED, 1, 8, "已完成")
                )
                self.assertEqual("已完成", window.easy_stage_status[Stage.COPY].text())
                window._set_pipeline_ui_locked(False)
                window.close()


    def test_proofread_review_saves_edits_and_individual_and_batch_decisions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = root / "game"
            (game / "Data" / "BasicData").mkdir(parents=True)
            (game / "Game.exe").write_bytes(b"game")
            (game / "Data" / "BasicData" / "Game.dat").write_bytes(b"data")
            projects = root / "projects"
            manifest_path = create_project(projects, game)
            items_path = dump_items(
                root / "items-translated.json",
                [
                    TranslationItem(key="one", code="C-1", original="原文一", translation="译文一"),
                    TranslationItem(key="two", code="C-2", original="原文二", translation="译文二"),
                ],
            )
            manifest = load_manifest(manifest_path)
            translate = manifest.version.stage(Stage.TRANSLATE)
            translate.status = StageStatus.COMPLETED
            translate.artifacts["items"] = str(items_path)
            manifest_path.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
            worker_input = build_worker_input(items_path, manifest, context_lines=0)
            issue = {
                "source": "ai", "type": "logic_error", "severity": "medium",
                "description": "测试问题", "suggestion": "", "confidence": 0.9,
            }
            report = make_report(
                worker_input,
                {
                    "kind": "proofread-worker-output",
                    "epoch": ARTIFACT_EPOCH,
                    "entries": {
                        "one": {"issues": [issue], "suggested_translation": "建议一"},
                        "two": {"issues": [issue], "suggested_translation": "建议二"},
                    },
                    "failed_batches": [],
                },
                mode="rules_ai",
                model="model",
                batch_size=20,
                context_lines=0,
                confidence_percent=70,
            )
            report_path, _ = proofread_paths(manifest_path, manifest)
            save_report(report_path, report)
            store = SettingsStore(root / "settings.ini")
            store.save(AppSettings(projects_root=str(projects), last_project=str(manifest_path)))
            with patch("app.SettingsStore", return_value=store), patch.object(MainWindow, "_open_settings"):
                window = MainWindow()
                self.assertEqual(2, window.proofread_table.rowCount())
                window.proofread_table.selectRow(0)
                window.proofread_suggestion.setPlainText("人工修订")
                self.app.processEvents()
                window._decide_current_proofread("accept")
                saved = load_report(report_path)
                self.assertEqual("人工修订", saved["entries"][0]["edited_translation"])
                self.assertEqual("accept", saved["entries"][0]["decision"])
                for row in range(window.proofread_table.rowCount()):
                    window.proofread_table.item(row, 0).setCheckState(Qt.Checked)
                window._decide_selected_proofread("keep")
                saved = load_report(report_path)
                self.assertEqual(["keep", "keep"], [entry["decision"] for entry in saved["entries"]])
                window.tabs.setCurrentIndex(window.proofread_tab_index)
                window._set_proofread_ui_locked(True)
                self.assertEqual(window.proofread_tab_index, window.tabs.currentIndex())
                self.assertTrue(window.tabs.isTabEnabled(window.proofread_tab_index))
                self.assertTrue(window.stop_proofread_button.isEnabled())
                self.assertFalse(window.proofread_mode.isEnabled())
                self.assertFalse(window.project_combo.isEnabled())
                self.assertFalse(window.start_button.isEnabled())
                window._set_proofread_ui_locked(False)
                window.close()


if __name__ == "__main__":
    unittest.main()
