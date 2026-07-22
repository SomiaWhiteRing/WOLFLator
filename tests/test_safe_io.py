import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from models import AppSettings, RunMode
from pipeline import Pipeline, create_project, load_manifest
from safe_io import (
    ProjectBusyError,
    RuntimeBusyError,
    atomic_write_json,
    project_lock,
    project_lock_status,
    read_text_with_retry,
    replace_with_retry,
    runtime_lock,
)


def make_game(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "Game.exe").write_bytes(b"game")
    (root / "Data.wolf").write_bytes(b"data")
    return root


class SafeIoTests(unittest.TestCase):
    def test_atomic_io_is_unique_retrying_and_lossless(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "state.json"
            barrier = threading.Barrier(3)
            errors = []

            def writer(value):
                try:
                    barrier.wait()
                    for index in range(5):
                        atomic_write_json(path, {"writer": value, "index": index})
                except Exception as error:
                    errors.append(error)

            threads = [threading.Thread(target=writer, args=(value,)) for value in (1, 2)]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join()
            self.assertEqual([], errors)
            self.assertIn(json.loads(path.read_text(encoding="utf-8"))["writer"], {1, 2})
            self.assertEqual([], list(root.glob(f".{path.name}.*.tmp")))

            path.write_text('{"value":"old"}', encoding="utf-8")
            with mock.patch("safe_io.replace_with_retry", side_effect=PermissionError("busy")):
                with self.assertRaises(PermissionError):
                    atomic_write_json(path, {"value": "new"})
            self.assertEqual("old", json.loads(path.read_text(encoding="utf-8"))["value"])
            self.assertEqual([], list(root.glob(f".{path.name}.*.tmp")))

            source = root / "source"
            target = root / "target"
            source.write_text("new", encoding="utf-8")
            target.write_text("old", encoding="utf-8")
            actual_replace = os.replace
            attempts = 0

            def flaky_replace(src, dst):
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    raise PermissionError(13, "sharing violation")
                actual_replace(src, dst)

            if os.name == "nt":
                with mock.patch("safe_io.os.replace", side_effect=flaky_replace):
                    replace_with_retry(source, target)
                self.assertEqual(3, attempts)
                self.assertEqual("new", target.read_text(encoding="utf-8"))
                with mock.patch(
                    "safe_io.Path.read_bytes",
                    side_effect=[PermissionError(13, "sharing violation"), b"recovered"],
                ) as read:
                    self.assertEqual("recovered", read_text_with_retry(path))
                self.assertEqual(2, read.call_count)

            source.write_text("again", encoding="utf-8")
            with mock.patch("safe_io.os.replace", side_effect=FileNotFoundError("bad")) as replace:
                with self.assertRaises(FileNotFoundError):
                    replace_with_retry(source, target)
            replace.assert_called_once()

    def test_project_lock_is_fail_fast_and_recovers_after_process_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = (
                "from safe_io import project_lock\n"
                f"with project_lock({str(root)!r}, 'child-holder'):\n"
                " print('ready', flush=True)\n"
                " input()\n"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", script],
                cwd=Path(__file__).resolve().parents[1],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
            )
            self.assertEqual("ready", process.stdout.readline().strip())
            try:
                busy, owner = project_lock_status(root)
                self.assertTrue(busy)
                self.assertEqual("child-holder", owner["operation"])
                with self.assertRaises(ProjectBusyError):
                    with project_lock(root, "parent-contender"):
                        pass
            finally:
                process.kill()
                process.wait(5)
                process.stdin.close()
                process.stdout.close()
            with project_lock(root, "after-exit"):
                self.assertTrue(project_lock_status(root)[0])

    def test_pipeline_save_ignores_and_cleans_legacy_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = create_project(root / "projects", make_game(root / "game"))
            legacy = manifest_path.with_name("project.json.tmp")
            legacy.write_text('{"run_mode":"corrupt"}', encoding="utf-8")
            pipeline = Pipeline(manifest_path, AppSettings(), "", root / "cache", glossary_api_key="")
            pipeline.set_run_mode(RunMode.STEP)
            self.assertFalse(legacy.exists())
            self.assertIs(RunMode.STEP, load_manifest(manifest_path).run_mode)

    def test_runtime_lock_blocks_before_translation_state_is_touched(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime" / "fingerprint"
            output = root / "output"
            entered = threading.Event()
            release = threading.Event()

            def holder():
                with runtime_lock(runtime.parent, "repair"):
                    entered.set()
                    release.wait(5)

            thread = threading.Thread(target=holder)
            thread.start()
            self.assertTrue(entered.wait(2))
            try:
                from ainiee import run_translation

                with self.assertRaises(RuntimeBusyError):
                    run_translation(runtime, root / "input.json", output, {}, "p", AppSettings(), "key")
                self.assertFalse(output.exists())
            finally:
                release.set()
                thread.join()


if __name__ == "__main__":
    unittest.main()
