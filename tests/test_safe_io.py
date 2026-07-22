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
    replace_with_retry,
    runtime_lock,
)


def make_game(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "Game.exe").write_bytes(b"game")
    (root / "Data.wolf").write_bytes(b"data")
    return root


class SafeIoTests(unittest.TestCase):
    def test_concurrent_atomic_json_writes_remain_parseable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            barrier = threading.Barrier(3)
            errors = []

            def writer(value):
                try:
                    barrier.wait()
                    for index in range(30):
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
            self.assertEqual([], list(path.parent.glob(f".{path.name}.*.tmp")))

    def test_failed_atomic_replace_keeps_target_and_cleans_own_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text('{"value":"old"}', encoding="utf-8")
            with mock.patch("safe_io.replace_with_retry", side_effect=PermissionError("busy")):
                with self.assertRaises(PermissionError):
                    atomic_write_json(path, {"value": "new"})
            self.assertEqual("old", json.loads(path.read_text(encoding="utf-8"))["value"])
            self.assertEqual([], list(path.parent.glob(f".{path.name}.*.tmp")))

    @unittest.skipUnless(os.name == "nt", "Windows replace retry policy")
    def test_replace_retries_only_windows_sharing_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            target = root / "target"
            source.write_text("new", encoding="utf-8")
            target.write_text("old", encoding="utf-8")
            actual = os.replace
            attempts = 0

            def flaky(src, dst):
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    raise PermissionError(13, "sharing violation")
                actual(src, dst)

            with mock.patch("safe_io.os.replace", side_effect=flaky):
                replace_with_retry(source, target)
            self.assertEqual(3, attempts)
            self.assertEqual("new", target.read_text(encoding="utf-8"))

            source.write_text("again", encoding="utf-8")
            with mock.patch("safe_io.os.replace", side_effect=FileNotFoundError("bad")) as replace:
                with self.assertRaises(FileNotFoundError):
                    replace_with_retry(source, target)
            replace.assert_called_once()

    def test_project_lock_is_fail_fast_between_threads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entered = threading.Event()
            release = threading.Event()

            def holder():
                with project_lock(root, "holder"):
                    entered.set()
                    release.wait(5)

            thread = threading.Thread(target=holder)
            thread.start()
            self.assertTrue(entered.wait(2))
            try:
                busy, owner = project_lock_status(root)
                self.assertTrue(busy)
                self.assertEqual("holder", owner["operation"])
                with self.assertRaises(ProjectBusyError):
                    with project_lock(root, "contender"):
                        pass
            finally:
                release.set()
                thread.join()
            self.assertFalse(project_lock_status(root)[0])

    def test_same_lock_object_can_be_reentered_without_leaking(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = project_lock(Path(directory), "nested")
            with lock:
                with lock:
                    self.assertTrue(project_lock_status(directory)[0])
            self.assertFalse(project_lock_status(directory)[0])

    def test_project_lock_is_released_when_process_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = (
                "import os\n"
                "from safe_io import project_lock\n"
                f"with project_lock({str(root)!r}, 'child'):\n"
                " print('ready', flush=True)\n"
                " os._exit(0)\n"
            )
            process = subprocess.Popen(
                [sys.executable, "-c", script],
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE,
                text=True,
            )
            self.assertEqual("ready", process.stdout.readline().strip())
            self.assertEqual(0, process.wait(5))
            process.stdout.close()
            with project_lock(root, "after-crash"):
                self.assertTrue(project_lock_status(root)[0])

    def test_project_lock_is_fail_fast_between_processes(self):
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
                process.stdin.write("\n")
                process.stdin.flush()
                self.assertEqual(0, process.wait(5))
                process.stdin.close()
                process.stdout.close()

    def test_manifest_survives_one_hundred_saves_with_lockless_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = create_project(root / "projects", make_game(root / "game"))
            pipeline = Pipeline(manifest_path, AppSettings(), "", root / "cache", glossary_api_key="")
            stop = threading.Event()
            errors = []

            def reader():
                while not stop.is_set():
                    try:
                        load_manifest(manifest_path)
                    except Exception as error:
                        errors.append(error)
                        return

            thread = threading.Thread(target=reader)
            thread.start()
            try:
                with pipeline._mutation("stress-save"):
                    for index in range(100):
                        pipeline.manifest.name = f"project-{index}"
                        pipeline.save()
            finally:
                stop.set()
                thread.join()
            self.assertEqual([], errors)
            self.assertEqual("project-99", load_manifest(manifest_path).name)

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

    def test_same_pipeline_instance_cannot_mutate_from_another_thread(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = create_project(root / "projects", make_game(root / "game"))
            pipeline = Pipeline(manifest_path, AppSettings(), "", root / "cache", glossary_api_key="")
            errors = []

            def contender():
                try:
                    pipeline.set_run_mode(RunMode.STEP)
                except Exception as error:
                    errors.append(error)

            with pipeline._mutation("holder"):
                thread = threading.Thread(target=contender)
                thread.start()
                thread.join()
            self.assertEqual(1, len(errors))
            self.assertIsInstance(errors[0], ProjectBusyError)
            self.assertIs(RunMode.ONE_CLICK, load_manifest(manifest_path).run_mode)

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
