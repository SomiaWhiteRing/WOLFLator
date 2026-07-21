import os
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from app import InstallThread, MainWindow, SettingsDialog
from models import AppSettings, RunMode, Stage, StageStatus
from pipeline import create_project, load_manifest
from settings import SettingsStore, protect_secret, unprotect_secret


class SettingsQtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_dpapi_round_trip(self):
        encrypted = protect_secret("test-secret")
        self.assertNotIn("test-secret", encrypted)
        self.assertEqual("test-secret", unprotect_secret(encrypted))

    def test_dialog_loads_persisted_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.ini")
            item = AppSettings(wolf_tool_path=r"C:\Tools\Wolf.exe", ainiee_source=r"C:\Tools\AiNiee")
            store.save(item)
            dialog = SettingsDialog(store)
            self.assertEqual(item.wolf_tool_path, dialog.wolf_path.text())
            self.assertEqual(item.ainiee_source, dialog.ainiee_path.text())
            dialog.close()

    def test_legacy_api_settings_seed_dedicated_glossary_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.ini")
            encrypted = protect_secret("legacy-secret")
            store._settings.setValue("api_base_url", "https://legacy.example/v1")
            store._settings.setValue("api_model", "legacy-model")
            store._settings.setValue("api_key_blob", encrypted)
            store._settings.setValue("api_timeout", 75)
            store._settings.sync()
            item = store.load()
            self.assertEqual("https://legacy.example/v1", item.glossary_api_base_url)
            self.assertEqual("legacy-model", item.glossary_api_model)
            self.assertEqual("legacy-secret", store.glossary_api_key(item))
            self.assertEqual(75, item.glossary_api_timeout)
            self.assertEqual(3, item.glossary_api_threads)
            self.assertEqual(0, item.glossary_api_max_tokens)

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
                glossary_api_max_tokens=65_535,
            )
            store.save(item)
            dialog = SettingsDialog(store)
            self.assertEqual(2, dialog.api_tabs.count())
            self.assertEqual("https://translate.example/v1", dialog.api_url.text())
            self.assertEqual("https://glossary.example/v1", dialog.glossary_api_url.text())
            self.assertEqual(12, dialog.api_threads.value())
            self.assertEqual(2, dialog.glossary_api_threads.value())
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
                self.assertEqual(3, window.tabs.count())
                self.assertEqual("范围", window.tabs.tabText(2))
                self.assertEqual(2, window.scope_stack.count())
                self.assertTrue(window.tabs.widget(2).isAncestorOf(window.translation_scope_button))
                self.assertTrue(window.tabs.widget(2).isAncestorOf(window.import_scope_button))
                self.assertEqual(8, len(window.step_buttons))
                self.assertEqual(8, len(window.step_skip_buttons))
                self.assertTrue(all(button.text() == "执行" for button in window.step_buttons.values()))
                self.assertTrue(all(button.text() == "跳过" for button in window.step_skip_buttons.values()))
                window.step_mode.click()
                self.assertEqual(1, window.workflow_stack.currentIndex())
                window.close()

    def test_step_mode_uses_per_stage_progress_and_keeps_completed_stage_runnable(self):
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
                window.close()


if __name__ == "__main__":
    unittest.main()
