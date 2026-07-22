import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel

from app import STAGE_RESULT_LABELS, InstallThread, MainWindow, SettingsDialog
from fonts import FontCandidate
from models import AppSettings, RunMode, Stage, StageStatus
from pipeline import PipelineStateEvent, create_project, load_manifest
from settings import SettingsStore, protect_secret, unprotect_secret


class SettingsQtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_dpapi_round_trip(self):
        encrypted = protect_secret("test-secret")
        self.assertNotIn("test-secret", encrypted)
        self.assertEqual("test-secret", unprotect_secret(encrypted))

    def test_completed_stage_can_surface_official_warnings(self):
        label = QLabel()
        MainWindow._update_stage_status(label, StageStatus.COMPLETED, "details", 16)
        self.assertEqual("已完成（16 个警告）", label.text())
        self.assertEqual("warning", label.property("state"))
        self.assertEqual("details", label.toolTip())

    def test_dialog_loads_persisted_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.ini")
            item = AppSettings(wolf_tool_path=r"C:\Tools\Wolf.exe", ainiee_source=r"C:\Tools\AiNiee")
            store.save(item)
            dialog = SettingsDialog(store)
            self.assertEqual(item.wolf_tool_path, dialog.wolf_path.text())
            self.assertEqual(item.ainiee_source, dialog.ainiee_path.text())
            dialog.close()

    def test_missing_glossary_settings_do_not_inherit_translation_api(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.ini")
            encrypted = protect_secret("translation-secret")
            store._settings.setValue("api_base_url", "https://translation.example/v1")
            store._settings.setValue("api_model", "translation-model")
            store._settings.setValue("api_key_blob", encrypted)
            store._settings.setValue("api_timeout", 75)
            store._settings.sync()
            item = store.load()
            self.assertEqual("", item.glossary_api_base_url)
            self.assertEqual("", item.glossary_api_model)
            self.assertEqual("", store.glossary_api_key(item))
            self.assertEqual(600, item.glossary_api_timeout)
            self.assertEqual(3, item.glossary_api_threads)
            self.assertEqual(500_000, item.glossary_chunk_chars)
            self.assertEqual(393_216, item.glossary_api_max_tokens)
            self.assertEqual("token", item.translation_chunk_mode)
            self.assertEqual(256, item.translation_token_limit)
            self.assertEqual(8, item.translation_line_limit)
            self.assertEqual(1, item.translation_retry_min_lines)
            self.assertEqual(6, item.translation_rounds)

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
            self.assertEqual(2, dialog.api_tabs.count())
            self.assertEqual("https://translate.example/v1", dialog.api_url.text())
            self.assertEqual("https://glossary.example/v1", dialog.glossary_api_url.text())
            self.assertEqual(12, dialog.api_threads.value())
            self.assertTrue(dialog.translation_token_mode.isChecked())
            self.assertEqual(256, dialog.translation_token_limit.value())
            self.assertEqual(8, dialog.translation_line_limit.value())
            self.assertEqual(1, dialog.translation_retry_min_lines.value())
            self.assertEqual(6, dialog.translation_rounds.value())
            self.assertEqual(2, dialog.glossary_api_threads.value())
            self.assertEqual(456_789, dialog.glossary_chunk_chars.value())
            self.assertEqual(65_535, dialog.glossary_api_max_tokens.value())
            dialog.close()

    def test_install_thread_prepares_dependencies_before_reporting_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            with patch("app.install_supported_ainiee", return_value=source) as install, patch(
                "app.prepare_managed_runtime"
            ) as prepare:
                thread = InstallThread(root / "packages", root / "runtime", False)
                thread.run()
            install.assert_called_once()
            prepare.assert_called_once_with(
                source,
                root / "runtime",
                force_sync=False,
                log=thread.log_line.emit,
            )

    def test_first_run_dialog_waits_until_window_can_be_shown(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.ini")
            with patch("app.SettingsStore", return_value=store), patch.object(
                MainWindow, "_open_settings"
            ) as open_settings:
                window = MainWindow()
                open_settings.assert_not_called()
                window.show()
                self.app.processEvents()
                open_settings.assert_called_once_with(first_run=True)
                window.close()

    def test_workflow_modes_are_inside_workflow_page(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.ini")
            with patch("app.SettingsStore", return_value=store), patch.object(MainWindow, "_open_settings"):
                window = MainWindow()
                self.assertTrue(window.workflow_page.isAncestorOf(window.one_click))
                self.assertTrue(window.workflow_page.isAncestorOf(window.step_mode))
                self.assertTrue(window.workflow_page.isAncestorOf(window.log_view))
                self.assertEqual(4, window.tabs.count())
                self.assertEqual("范围", window.tabs.tabText(2))
                self.assertEqual("修改字体", window.tabs.tabText(3))
                self.assertTrue(
                    any(
                        label.text() == "原字体"
                        for label in window.tabs.widget(3).findChildren(QLabel)
                    )
                )
                self.assertEqual(3, window.scope_stack.count())
                self.assertTrue(window.tabs.widget(2).isAncestorOf(window.translation_scope_button))
                self.assertTrue(window.tabs.widget(2).isAncestorOf(window.import_scope_button))
                self.assertTrue(window.tabs.widget(2).isAncestorOf(window.export_scope_button))
                self.assertFalse(window.external_filter_options.isHidden())
                self.assertTrue(window.exclude_large_external_files.isChecked())
                self.assertEqual(128, window.external_file_limit_kb.value())
                window.exclude_large_external_files.setChecked(False)
                self.assertFalse(window.external_file_limit_kb.isEnabled())
                window.export_scope_checks["external"].setChecked(False)
                self.assertTrue(window.external_filter_options.isHidden())
                self.assertEqual(8, len(window.step_buttons))
                self.assertEqual(8, len(window.step_result_buttons))
                self.assertTrue(all(button.text() == "执行" for button in window.step_buttons.values()))
                self.assertEqual(
                    [STAGE_RESULT_LABELS[stage] for stage in Stage],
                    [window.step_result_buttons[stage].text() for stage in Stage],
                )
                self.assertTrue(
                    all(
                        window.step_buttons[stage].width()
                        == window.step_result_buttons[stage].width()
                        for stage in Stage
                    )
                )
                window._append_log("[WARNING] 字体缺字：主字体")
                warning_block = window.log_view.document().lastBlock().previous()
                self.assertEqual("警告  字体缺字：主字体", warning_block.text())
                self.assertEqual(
                    "#a24625",
                    warning_block.begin().fragment().charFormat().foreground().color().name(),
                )
                window._append_log("[ERROR] 发布失败")
                error_block = window.log_view.document().lastBlock().previous()
                self.assertEqual("错误  发布失败", error_block.text())
                self.assertEqual(
                    "#b42318",
                    error_block.begin().fragment().charFormat().foreground().color().name(),
                )
                candidate = FontCandidate(
                    source="bundled",
                    family="测试字体",
                    aliases=("测试字体",),
                    files=(),
                    missing=frozenset({"∟"}),
                )
                window.font_context = {
                    "required": {"∟"},
                    "candidates": [candidate],
                    "original_slots": ["测试字体"] * 4,
                }
                for combo in window.font_combos:
                    combo.addItem(candidate.label, candidate)
                window._update_font_rows()
                self.assertEqual('缺少 1 字："∟"', window.font_coverage_labels[0].text())
                self.assertEqual('缺少字符：\n"∟"', window.font_coverage_labels[0].toolTip())
                self.assertNotIn("U+", window.font_coverage_labels[0].text())
                window.step_mode.click()
                self.assertEqual(1, window.workflow_stack.currentIndex())
                window.close()

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
                self.assertIn("schema", window.status_label.toolTip())
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
                window._set_pipeline_ui_locked(True)
                self.assertFalse(window.settings_button.isEnabled())
                self.assertFalse(window.project_combo.isEnabled())
                self.assertFalse(window.new_project_button.isEnabled())
                self.assertFalse(window.add_version_button.isEnabled())
                self.assertFalse(window.one_click.isEnabled())
                self.assertFalse(window.open_release_button.isEnabled())
                self.assertTrue(window.stop_button.isEnabled())
                self.assertFalse(window.tabs.isTabEnabled(1))
                self.assertFalse(window.tabs.isTabEnabled(2))
                self.assertFalse(window.tabs.isTabEnabled(3))

                with patch.object(window, "_load_project_view") as reload_view:
                    window._stage_progress(1, 8, Stage.COPY.value)
                    reload_view.assert_not_called()
                window._stage_state(
                    PipelineStateEvent(Stage.COPY, StageStatus.COMPLETED, 1, 8, "已完成")
                )
                self.assertEqual("已完成", window.easy_stage_status[Stage.COPY].text())
                window._set_pipeline_ui_locked(False)
                window.close()


if __name__ == "__main__":
    unittest.main()
