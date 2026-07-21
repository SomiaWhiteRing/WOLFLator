import tempfile
import unittest
from pathlib import Path

from models import AppSettings, ImportScope, RunMode, Stage, StageStatus
from pipeline import Pipeline, create_project, load_manifest


def make_game(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "Game.exe").write_bytes(b"game")
    (root / "Data" / "BasicData").mkdir(parents=True)
    (root / "Data" / "BasicData" / "Game.dat").write_bytes(b"data")
    return root


class FakePipeline(Pipeline):
    executed = None

    def _execute(self, stage: Stage) -> dict[str, str]:
        if self.executed is not None:
            self.executed.append(stage)
        if stage is Stage.COPY:
            return self._copy()
        return {"artifact": str(self.artifacts_dir / f"{stage.value}.ok")}


class FailingPipeline(FakePipeline):
    def _execute(self, stage: Stage) -> dict[str, str]:
        if stage is Stage.GLOSSARY:
            raise RuntimeError("simulated failure")
        return super()._execute(stage)


class PipelineTests(unittest.TestCase):
    def test_run_stage_executes_only_selected_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = create_project(root / "projects", make_game(root / "game"))
            executed = []
            pipeline = FakePipeline(manifest_path, AppSettings(), "", root / "cache")
            pipeline.executed = executed
            self.assertEqual("completed", pipeline.run_stage(Stage.GLOSSARY))
            self.assertEqual([Stage.GLOSSARY], executed)
            current = load_manifest(manifest_path)
            self.assertEqual(StageStatus.COMPLETED, current.version.stage(Stage.GLOSSARY).status)
            self.assertEqual(StageStatus.PENDING, current.version.stage(Stage.COPY).status)
            self.assertEqual(StageStatus.PENDING, current.version.stage(Stage.TRANSLATE).status)

    def test_rerun_stage_invalidates_only_downstream(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = create_project(root / "projects", make_game(root / "game"))
            self.assertEqual("completed", FakePipeline(manifest_path, AppSettings(), "", root / "cache").run())
            executed = []
            pipeline = FakePipeline(manifest_path, AppSettings(), "", root / "cache")
            pipeline.executed = executed
            self.assertEqual("completed", pipeline.run_stage(Stage.EXTRACT))
            self.assertEqual([Stage.EXTRACT], executed)
            current = load_manifest(manifest_path)
            self.assertEqual(StageStatus.COMPLETED, current.version.stage(Stage.UNPACK).status)
            self.assertEqual(StageStatus.COMPLETED, current.version.stage(Stage.EXTRACT).status)
            self.assertEqual(StageStatus.PENDING, current.version.stage(Stage.GLOSSARY).status)

    def test_one_click_executes_manually_skipped_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = create_project(root / "projects", make_game(root / "game"))
            pipeline = FakePipeline(manifest_path, AppSettings(), "", root / "cache")
            pipeline.skip_stage(Stage.COPY)
            skipped = load_manifest(manifest_path).version.stage(Stage.COPY)
            self.assertEqual(StageStatus.COMPLETED, skipped.status)
            self.assertEqual("true", skipped.artifacts["skipped"])
            executed = []
            pipeline = FakePipeline(manifest_path, AppSettings(), "", root / "cache")
            pipeline.executed = executed
            self.assertEqual("completed", pipeline.run())
            self.assertEqual(list(Stage), executed)

    def test_failure_is_persisted_and_retryable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = make_game(root / "game")
            manifest_path = create_project(root / "projects", game)
            settings = AppSettings(
                api_base_url="https://user:password@example.com/v1/secret-token?token=hidden",
                api_model="test-model",
            )
            app_log = []
            pipeline = FailingPipeline(
                manifest_path, settings, "secret-token", root / "cache", log=app_log.append
            )
            with self.assertRaisesRegex(RuntimeError, "simulated"):
                pipeline.run()
            current = load_manifest(manifest_path)
            self.assertEqual(StageStatus.FAILED, current.version.stage(Stage.GLOSSARY).status)
            self.assertEqual(StageStatus.PENDING, current.version.stage(Stage.TRANSLATE).status)
            logs = list((Path(manifest_path).parent / "versions" / current.active_version / "artifacts" / "logs").glob("*.log"))
            self.assertEqual(1, len(logs))
            pipeline.log("credential=secret-token")
            pipeline.detail(
                "tool echoed https://user:password@example.com/v1/secret-token?token=hidden"
            )
            log_text = logs[0].read_text(encoding="utf-8-sig")
            self.assertIn("simulated failure", log_text)
            self.assertIn("credential=[REDACTED]", log_text)
            self.assertIn("[DETAIL] stage.exception stage=glossary", log_text)
            self.assertIn("Traceback", log_text)
            self.assertIn("manifest.save.complete", log_text)
            self.assertIn("tool echoed https://example.com/v1/[REDACTED]", log_text)
            self.assertFalse(any("Traceback" in line for line in app_log))
            self.assertIn("api_url=https://example.com/v1/[REDACTED]", log_text)
            self.assertNotIn("secret-token", log_text)
            self.assertNotIn("password", log_text)
            self.assertNotIn("token=hidden", log_text)
            pipeline.retry_failed()
            self.assertEqual(StageStatus.PENDING, load_manifest(manifest_path).version.stage(Stage.GLOSSARY).status)

    def test_source_change_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = make_game(root / "game")
            manifest_path = create_project(root / "projects", game)
            pipeline = FakePipeline(manifest_path, AppSettings(), "", root / "cache")
            pipeline.set_run_mode(RunMode.STEP)
            pipeline.run()
            (game / "changed.txt").write_text("changed", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "新的源版本"):
                FakePipeline(manifest_path, AppSettings(), "", root / "cache").run()

    def test_scope_change_invalidates_only_import_and_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = make_game(root / "game")
            manifest_path = create_project(root / "projects", game)
            first = FakePipeline(manifest_path, AppSettings(), "", root / "cache")
            self.assertEqual("completed", first.run())
            first.set_import_scope(ImportScope(external=True))
            changed = load_manifest(manifest_path)
            self.assertEqual(StageStatus.COMPLETED, changed.version.stage(Stage.VALIDATE).status)
            self.assertEqual(StageStatus.PENDING, changed.version.stage(Stage.IMPORT).status)
            self.assertEqual(StageStatus.PENDING, changed.version.stage(Stage.RELEASE).status)
            executed = []
            second = FakePipeline(manifest_path, AppSettings(), "", root / "cache")
            second.executed = executed
            self.assertEqual("completed", second.run())
            self.assertEqual([Stage.IMPORT, Stage.RELEASE], executed)


if __name__ == "__main__":
    unittest.main()
