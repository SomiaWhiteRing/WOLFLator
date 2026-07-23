import io
import json
import signal
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import cli
from PySide6.QtCore import QCoreApplication
from models import AppSettings
from pipeline import create_project
from safe_io import project_lock
from wolf_tools import CancelledError
from wolf_editor import EditorInfo


def make_game(root: Path) -> Path:
    root.mkdir()
    (root / "Game.exe").write_bytes(b"game")
    (root / "Data.wolf").write_bytes(b"data")
    return root


class CliTests(unittest.TestCase):
    def test_closed_console_does_not_fail_pipeline_logging(self):
        with patch("builtins.print", side_effect=OSError(22, "invalid handle")):
            cli._print_log("still written to the detailed log")
            cli._print_progress(1, 8, cli.Stage.COPY)

    def test_cli_uses_the_same_qt_identity_as_the_gui(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.ini"
            errors = io.StringIO()
            with redirect_stderr(errors):
                cli.main(["--settings", str(settings_path), "settings-check"])
            self.assertEqual("WOLFLator", QCoreApplication.applicationName())
            self.assertEqual("WOLFLator", QCoreApplication.organizationName())

    def test_settings_check_json_reports_editor_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings_path = root / "settings.ini"
            editor = root / "Editor.exe"
            editor.write_bytes(b"editor")
            cli.SettingsStore(settings_path).save(AppSettings(wolf_editor_path=str(editor)))
            info = EditorInfo(editor, "3.713", (3, 713, 0, 0), "a" * 64)
            output = io.StringIO()
            with patch.object(cli, "validate_settings", return_value=[]), patch.object(
                cli, "inspect_wolf_editor", return_value=info
            ), redirect_stdout(output):
                self.assertEqual(
                    0,
                    cli.main(
                        [
                            "--settings",
                            str(settings_path),
                            "settings-check",
                            "--json",
                        ]
                    ),
                )
            result = json.loads(output.getvalue())
            self.assertEqual("ready", result["tools"]["wolf_editor"]["status"])
            self.assertEqual("3.713", result["tools"]["wolf_editor"]["version"])

    def test_status_json_and_busy_project_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = create_project(root / "projects", make_game(root / "game"))
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(0, cli.main(["status", str(manifest), "--json"]))
            result = json.loads(output.getvalue())
            self.assertEqual("game", result["project"])
            self.assertEqual("pending", result["stages"]["copy"]["status"])

            entered = threading.Event()
            release = threading.Event()

            def holder():
                with project_lock(manifest, "test-holder"):
                    entered.set()
                    release.wait(5)

            thread = threading.Thread(target=holder)
            thread.start()
            self.assertTrue(entered.wait(2))
            try:
                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(0, cli.main(["status", str(manifest), "--json"]))
                status = json.loads(output.getvalue())
                self.assertTrue(status["busy"])
                self.assertEqual("test-holder", status["lock"]["operation"])

                errors = io.StringIO()
                with redirect_stderr(errors):
                    self.assertEqual(
                        3,
                        cli.main(["run", str(manifest), "--stage", "copy"]),
                    )
                self.assertIn("test-holder", errors.getvalue())
            finally:
                release.set()
                thread.join()

    def test_project_create_uses_persisted_projects_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings_path = root / "settings.ini"
            store = cli.SettingsStore(settings_path)
            store.save(AppSettings(projects_root=str(root / "projects")))
            output = io.StringIO()
            with patch.object(cli, "validate_settings", return_value=[]), redirect_stdout(output):
                self.assertEqual(
                    0,
                    cli.main(["--settings", str(settings_path), "project-create", str(make_game(root / "game"))]),
                )
            manifest = Path(output.getvalue().strip())
            self.assertTrue(manifest.is_file())

    def test_editor_install_persists_managed_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings_path = root / "settings.ini"
            executable = root / "packages" / "editor" / "3.713" / "Editor.exe"
            output = io.StringIO()
            with patch.object(cli, "install_supported_editor", return_value=executable), patch.object(
                cli, "local_data_dir", return_value=root
            ), redirect_stdout(output):
                self.assertEqual(
                    0,
                    cli.main(["--settings", str(settings_path), "editor-install"]),
                )
            self.assertEqual(
                str(executable),
                cli.SettingsStore(settings_path).load().wolf_editor_path,
            )

    def test_api_test_can_target_glossary_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "settings.ini"
            store = cli.SettingsStore(settings_path)
            item = AppSettings(
                api_base_url="https://translate.example/v1",
                api_model="translate-model",
                glossary_api_base_url="https://glossary.example/v1",
                glossary_api_model="glossary-model",
            )
            store.set_api_key(item, "translate-secret")
            store.set_glossary_api_key(item, "glossary-secret")
            store.save(item)
            output = io.StringIO()
            with patch.object(cli, "test_api", return_value="glossary-ok") as test, redirect_stdout(output):
                self.assertEqual(
                    0,
                    cli.main(
                        [
                            "--settings",
                            str(settings_path),
                            "api-test",
                            "--target",
                            "glossary",
                        ]
                    ),
                )
            self.assertEqual("glossary-ok", output.getvalue().strip())
            test.assert_called_once()
            self.assertEqual("glossary-secret", test.call_args.args[1])
            self.assertTrue(test.call_args.kwargs["glossary"])

    def test_scope_updates_the_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = create_project(root / "projects", make_game(root / "game"))
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(0, cli.main(["scope", str(manifest), "--external"]))
            loaded = cli.load_manifest(manifest)
            self.assertTrue(loaded.import_scope.external)
            self.assertFalse(loaded.translation_scope.external)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    0,
                    cli.main(["scope", str(manifest), "--target", "export", "--external"]),
                )
            loaded = cli.load_manifest(manifest)
            self.assertTrue(loaded.export_scope.external)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    0,
                    cli.main(
                        [
                            "scope",
                            str(manifest),
                            "--target",
                            "export",
                            "--no-exclude-large-external",
                            "--external-size-limit-kb",
                            "256",
                        ]
                    ),
                )
            loaded = cli.load_manifest(manifest)
            self.assertFalse(loaded.exclude_large_external_files)
            self.assertEqual(256, loaded.external_file_limit_kb)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    0,
                    cli.main(["scope", str(manifest), "--target", "translation", "--optional-name"]),
                )
            loaded = cli.load_manifest(manifest)
            self.assertTrue(loaded.translation_scope.optional_name)
            self.assertFalse(loaded.import_scope.optional_name)
            with redirect_stdout(io.StringIO()):
                self.assertEqual(
                    0,
                    cli.main(
                        [
                            "scope",
                            str(manifest),
                            "--target",
                            "import",
                            "--no-protect-paths-and-commands",
                            "--logic-unknown-policy",
                            "warn",
                            "--suspicious-identifiers",
                            "protect",
                        ]
                    ),
                )
            loaded = cli.load_manifest(manifest)
            self.assertFalse(loaded.import_protection.protect_paths_and_commands)
            self.assertEqual("warn", loaded.import_protection.logic_unknown_policy)
            self.assertEqual("protect", loaded.import_protection.suspicious_identifiers)

    def test_ctrl_c_cancels_pipeline_and_returns_130(self):
        class FakePipeline:
            cancelled = False

            def cancel(self):
                self.cancelled = True

            def run(self):
                signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
                raise CancelledError("cancelled")

        pipeline = FakePipeline()
        errors = io.StringIO()
        with patch.object(cli, "_pipeline", return_value=pipeline), redirect_stderr(errors):
            self.assertEqual(130, cli.main(["run", "project.json"]))
        self.assertTrue(pipeline.cancelled)
        self.assertIn("已取消", errors.getvalue())

    def test_run_reports_missing_runtime_without_starting_pipeline(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "project.json"
            output = io.StringIO()
            errors = io.StringIO()
            with patch.object(cli, "_pipeline", side_effect=RuntimeError("runtime missing")), redirect_stdout(
                output
            ), redirect_stderr(errors):
                self.assertEqual(1, cli.main(["run", str(manifest)]))
            self.assertIn("runtime missing", errors.getvalue())

if __name__ == "__main__":
    unittest.main()
