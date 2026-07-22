import tempfile
import unittest
import json
from pathlib import Path
from unittest import mock

from models import STAGE_ORDER, AppSettings, ImportScope, RunMode, Stage, StageStatus, TranslationItem
from pipeline import Pipeline, create_project, load_manifest
from wolf_tools import dump_items, load_items


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
    def _translation_pipeline(self, root: Path) -> Pipeline:
        manifest_path = create_project(root / "projects", make_game(root / "game"))
        pipeline = Pipeline(
            manifest_path,
            AppSettings(translation_rounds=6),
            "secret",
            root / "cache",
            glossary_api_key="",
        )
        items = [
            TranslationItem(key="plain", original="甲", code="COMMON-1"),
            TranslationItem(
                key="control",
                original=r"\C[1]乙",
                code="COMMON-2",
                control_signature=[r"\C[1]"],
            ),
        ]
        items_path = dump_items(pipeline.artifacts_dir / "items-extracted.json", items)
        pipeline.manifest.version.stage(Stage.EXTRACT).artifacts["items"] = str(items_path)
        (pipeline.project_dir / "glossary.json").write_text("{}", encoding="utf-8")
        return pipeline

    def test_translation_retries_only_failed_rows_in_one_fresh_session(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pipeline = self._translation_pipeline(root)
            calls = []

            def fake_translation(_runtime, input_json, output_dir, *_args, **_kwargs):
                rows = json.loads(Path(input_json).read_text(encoding="utf-8"))
                calls.append((rows, Path(output_dir)))
                if len(calls) == 1:
                    return [{**rows[0], "translation": "译文甲", "stage": 1}]
                self.assertEqual(["control"], [row["key"] for row in rows])
                return [
                    {
                        **rows[0],
                        "translation": chr(0xE100) + "译文乙",
                        "stage": 1,
                    }
                ]

            with mock.patch("pipeline.require_managed_runtime", return_value=root / "runtime"), mock.patch(
                "pipeline.run_translation", side_effect=fake_translation
            ):
                artifacts = pipeline._translate()

            self.assertEqual(2, len(calls))
            self.assertEqual("ainiee-output", calls[0][1].name)
            self.assertEqual("ainiee-retry-output", calls[1][1].name)
            merged = load_items(artifacts["items"])
            self.assertEqual(["译文甲", r"\C[1]译文乙"], [item.translation for item in merged])
            retry_input = json.loads(Path(artifacts["ainiee_retry_input"]).read_text(encoding="utf-8"))
            self.assertEqual(["control"], [row["key"] for row in retry_input])
            report = json.loads(Path(artifacts["ainiee_retry_result"]).read_text(encoding="utf-8"))
            self.assertEqual(1, report["first_pass_failed"])
            self.assertEqual(0, report["remaining_failed"])

    def test_translation_stops_after_one_failed_only_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pipeline = self._translation_pipeline(root)
            with mock.patch("pipeline.require_managed_runtime", return_value=root / "runtime"), mock.patch(
                "pipeline.run_translation", return_value=[]
            ) as run:
                with self.assertRaisesRegex(ValueError, "missing=2"):
                    pipeline._translate()
            self.assertEqual(2, run.call_count)

    def test_manifest_rejects_missing_translation_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = create_project(root / "projects", make_game(root / "game"))
            data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
            data.pop("translation_scope")
            Path(manifest_path).write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "translation_scope"):
                load_manifest(manifest_path)

    def test_manifest_rejects_non_boolean_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = create_project(root / "projects", make_game(root / "game"))
            data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
            data["translation_scope"]["display"] = "true"
            Path(manifest_path).write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "必须是布尔值"):
                load_manifest(manifest_path)

    def test_run_stage_executes_only_selected_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = create_project(root / "projects", make_game(root / "game"))
            executed = []
            pipeline = FakePipeline(
                manifest_path, AppSettings(), "", root / "cache", glossary_api_key=""
            )
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
            self.assertEqual(
                "completed",
                FakePipeline(
                    manifest_path, AppSettings(), "", root / "cache", glossary_api_key=""
                ).run(),
            )
            executed = []
            pipeline = FakePipeline(
                manifest_path, AppSettings(), "", root / "cache", glossary_api_key=""
            )
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
            pipeline = FakePipeline(
                manifest_path, AppSettings(), "", root / "cache", glossary_api_key=""
            )
            pipeline.skip_stage(Stage.COPY)
            skipped = load_manifest(manifest_path).version.stage(Stage.COPY)
            self.assertEqual(StageStatus.COMPLETED, skipped.status)
            self.assertEqual("true", skipped.artifacts["skipped"])
            executed = []
            pipeline = FakePipeline(
                manifest_path, AppSettings(), "", root / "cache", glossary_api_key=""
            )
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
                glossary_api_base_url="https://glossary-user:glossary-password@example.net/v1?key=glossary-hidden",
                glossary_api_model="glossary-model",
            )
            app_log = []
            pipeline = FailingPipeline(
                manifest_path,
                settings,
                "secret-token",
                root / "cache",
                glossary_api_key="glossary-secret",
                log=app_log.append,
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
            pipeline.detail(
                "glossary echoed https://glossary-user:glossary-password@example.net/v1?key=glossary-hidden glossary-secret"
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
            self.assertIn("glossary_api_url=https://example.net/v1", log_text)
            self.assertNotIn("secret-token", log_text)
            self.assertNotIn("glossary-secret", log_text)
            self.assertNotIn("glossary-password", log_text)
            self.assertNotIn("glossary-hidden", log_text)
            self.assertNotIn("password", log_text)
            self.assertNotIn("token=hidden", log_text)
            pipeline.retry_failed()
            self.assertEqual(StageStatus.PENDING, load_manifest(manifest_path).version.stage(Stage.GLOSSARY).status)

    def test_source_change_is_blocked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = make_game(root / "game")
            manifest_path = create_project(root / "projects", game)
            pipeline = FakePipeline(
                manifest_path, AppSettings(), "", root / "cache", glossary_api_key=""
            )
            pipeline.set_run_mode(RunMode.STEP)
            pipeline.run()
            (game / "changed.txt").write_text("changed", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "新的源版本"):
                FakePipeline(
                    manifest_path, AppSettings(), "", root / "cache", glossary_api_key=""
                ).run()

    def test_import_scope_change_only_rebuilds_import_and_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = make_game(root / "game")
            manifest_path = create_project(root / "projects", game)
            first = FakePipeline(
                manifest_path, AppSettings(), "", root / "cache", glossary_api_key=""
            )
            self.assertEqual("completed", first.run())
            first.set_import_scope(ImportScope(external=True))
            changed = load_manifest(manifest_path)
            self.assertEqual(StageStatus.COMPLETED, changed.version.stage(Stage.VALIDATE).status)
            for stage in STAGE_ORDER[STAGE_ORDER.index(Stage.IMPORT):]:
                self.assertEqual(StageStatus.PENDING, changed.version.stage(stage).status)
            executed = []
            second = FakePipeline(
                manifest_path, AppSettings(), "", root / "cache", glossary_api_key=""
            )
            second.executed = executed
            self.assertEqual("completed", second.run())
            self.assertEqual(list(STAGE_ORDER[STAGE_ORDER.index(Stage.IMPORT):]), executed)

    def test_translation_scope_change_keeps_full_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = create_project(root / "projects", make_game(root / "game"))
            first = FakePipeline(
                manifest_path, AppSettings(), "", root / "cache", glossary_api_key=""
            )
            self.assertEqual("completed", first.run())
            first.set_translation_scope(ImportScope(optional_name=True))
            changed = load_manifest(manifest_path)
            self.assertEqual(StageStatus.COMPLETED, changed.version.stage(Stage.EXTRACT).status)
            for stage in STAGE_ORDER[STAGE_ORDER.index(Stage.GLOSSARY):]:
                self.assertEqual(StageStatus.PENDING, changed.version.stage(stage).status)
            executed = []
            second = FakePipeline(
                manifest_path, AppSettings(), "", root / "cache", glossary_api_key=""
            )
            second.executed = executed
            self.assertEqual("completed", second.run())
            self.assertEqual(list(STAGE_ORDER[STAGE_ORDER.index(Stage.GLOSSARY):]), executed)


if __name__ == "__main__":
    unittest.main()
