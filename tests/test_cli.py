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
from models import AppSettings
from pipeline import create_project
from safe_io import project_lock
from wolf_tools import CancelledError


def make_game(root: Path) -> Path:
    root.mkdir()
    (root / "Game.exe").write_bytes(b"game")
    (root / "Data.wolf").write_bytes(b"data")
    return root


class CliTests(unittest.TestCase):
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
