import tempfile
import unittest
import json
from pathlib import Path
from unittest import mock

from openpyxl import Workbook

from fonts import BUNDLED_FONT_FAMILY, BUNDLED_FONT_ID, default_font_scheme, load_font_scheme
from models import STAGE_ORDER, AppSettings, ImportScope, RunMode, Stage, StageStatus, TranslationItem
from pipeline import Pipeline, create_project, load_manifest
from wolf_tools import dump_items, full_export_scope, load_items


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

    def test_new_project_has_four_slot_default_font_scheme(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = create_project(root / "projects", make_game(root / "game"))
            scheme = load_font_scheme(manifest_path.parent)
            self.assertEqual("default", scheme["origin"])
            self.assertEqual(
                [BUNDLED_FONT_FAMILY] * 4,
                [slot["family"] for slot in scheme["slots"]],
            )

    def test_font_scheme_change_invalidates_only_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = create_project(root / "projects", make_game(root / "game"))
            pipeline = Pipeline(manifest_path, AppSettings(), "", root / "cache", glossary_api_key="")
            for stage in STAGE_ORDER:
                pipeline.manifest.version.stage(stage).status = StageStatus.COMPLETED
            pipeline.save()
            scheme = default_font_scheme()
            scheme["slots"][0] = {"mode": "keep"}
            pipeline.set_font_scheme(scheme)
            manifest = load_manifest(manifest_path)
            self.assertTrue(
                all(
                    manifest.version.stage(stage).status is StageStatus.COMPLETED
                    for stage in STAGE_ORDER[:-1]
                )
            )
            self.assertIs(StageStatus.PENDING, manifest.version.stage(Stage.RELEASE).status)

    def test_font_release_uses_official_workbook_and_verifies_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = create_project(root / "projects", make_game(root / "game"))
            pipeline = Pipeline(manifest_path, AppSettings(), "", root / "cache", glossary_api_key="")
            items = [
                TranslationItem(key=f"font-{index}", original=f"原字体{index}", code=f"BASICDATA-{index + 3}")
                for index in range(4)
            ]
            items.append(TranslationItem(key="text", original="原文", translation="中文", code="COMMON-1"))
            items_path = dump_items(pipeline.artifacts_dir / "items-translated.json", items)
            pipeline.manifest.version.stage(Stage.VALIDATE).artifacts["items"] = str(items_path)
            workbook_path = pipeline.artifacts_dir / "source.xlsx"
            workbook_path.parent.mkdir(parents=True, exist_ok=True)
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(
                [
                    "Code (No Change)",
                    "Flag (No Change)",
                    "Type",
                    "Info",
                    "Your notes",
                    "Original text (No Change)",
                    "Translated text 1 / Chinese (Simplified)",
                ]
            )
            for index in range(4):
                sheet.append(
                    [
                        f"BASICDATA-{index + 3}",
                        "",
                        "Basic Game Settings",
                        f"Font {index}",
                        "",
                        f"原字体{index}",
                        "",
                    ]
                )
            workbook.save(workbook_path)
            pipeline.manifest.version.stage(Stage.EXTRACT).artifacts["workbook"] = str(workbook_path)
            pipeline.manifest.version.stage(Stage.EXTRACT).artifacts["items"] = str(items_path)

            verification = root / "verification.xlsx"
            verify_book = Workbook()
            verify_sheet = verify_book.active
            verify_sheet.append(list(sheet.iter_rows(min_row=1, max_row=1, values_only=True))[0])
            for index in range(4):
                verify_sheet.append(
                    [
                        f"BASICDATA-{index + 3}",
                        "",
                        "Basic Game Settings",
                        f"Font {index}",
                        "",
                        BUNDLED_FONT_FAMILY,
                        "",
                    ]
                )
            verify_book.save(verification)

            translated = make_game(root / "translated")
            generated = root / "generated"
            runner = mock.Mock()

            def translate(*_args, **_kwargs):
                make_game(generated)
                return generated

            runner.translate.side_effect = translate
            runner.extract.return_value = verification
            runner.console_outputs = []
            temporary = pipeline.version_dir / ".release-ready"
            with mock.patch.object(pipeline, "_official_runner", return_value=runner):
                artifacts = pipeline._build_font_release(
                    translated, temporary, load_font_scheme(manifest_path.parent)
                )
            self.assertTrue((temporary / BUNDLED_FONT_ID).is_file())
            self.assertEqual("0", artifacts["font_warning_count"])
            result = json.loads(Path(artifacts["font_result"]).read_text(encoding="utf-8"))
            self.assertEqual([BUNDLED_FONT_FAMILY] * 4, result["applied_slots"])
            runner.translate.assert_called_once()
            self.assertEqual(2, runner.extract.call_count)

    def test_font_release_rejects_non_font_text_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = create_project(root / "projects", make_game(root / "game"))
            pipeline = Pipeline(manifest_path, AppSettings(), "", root / "cache", glossary_api_key="")
            items = [
                TranslationItem(key=f"font-{index}", original=f"原字体{index}", code=f"BASICDATA-{index + 3}")
                for index in range(4)
            ]
            items_path = dump_items(pipeline.artifacts_dir / "items.json", items)
            pipeline.manifest.version.stage(Stage.VALIDATE).artifacts["items"] = str(items_path)

            def workbook(path, text):
                book = Workbook()
                sheet = book.active
                sheet.append([
                    "Code (No Change)", "Flag (No Change)", "Type", "Info",
                    "Your notes", "Original text (No Change)",
                    "Translated text 1 / Chinese (Simplified)",
                ])
                for index in range(4):
                    sheet.append([
                        f"BASICDATA-{index + 3}", "", "Basic Game Settings", f"Font {index}",
                        "", BUNDLED_FONT_FAMILY, "",
                    ])
                sheet.append(["COMMON-1", "", "Event", "Message", "", text, ""])
                book.save(path)
                return path

            baseline = workbook(root / "baseline.xlsx", "未变化")
            changed = workbook(root / "changed.xlsx", "被改动")
            pipeline.manifest.version.stage(Stage.EXTRACT).artifacts = {
                "workbook": str(baseline),
                "items": str(items_path),
            }
            translated = make_game(root / "translated")
            generated = root / "generated"
            runner = mock.Mock()
            runner.extract.side_effect = [baseline, changed]
            runner.translate.side_effect = lambda *_args, **_kwargs: make_game(generated)
            runner.console_outputs = []
            with mock.patch.object(pipeline, "_official_runner", return_value=runner):
                with self.assertRaisesRegex(RuntimeError, "字体字段以外"):
                    pipeline._build_font_release(
                        translated,
                        pipeline.version_dir / ".release-ready",
                        load_font_scheme(manifest_path.parent),
                    )

    def test_font_release_failure_keeps_previous_release(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = create_project(root / "projects", make_game(root / "game"))
            pipeline = Pipeline(manifest_path, AppSettings(), "", root / "cache", glossary_api_key="")
            translated = make_game(root / "translated")
            pipeline.manifest.version.stage(Stage.IMPORT).artifacts["translated_game"] = str(translated)
            pipeline.release_dir.mkdir(parents=True)
            (pipeline.release_dir / "old.txt").write_text("keep", encoding="utf-8")
            with mock.patch.object(pipeline, "_build_font_release", side_effect=RuntimeError("font failed")):
                with self.assertRaisesRegex(RuntimeError, "font failed"):
                    pipeline._release()
            self.assertEqual("keep", (pipeline.release_dir / "old.txt").read_text(encoding="utf-8"))

    def test_import_uses_the_same_full_structure_as_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pipeline = self._translation_pipeline(root)
            items = [TranslationItem(key="plain", original="甲", translation="译文", code="COMMON-1")]
            items_path = dump_items(pipeline.artifacts_dir / "items-translated.json", items)
            pipeline.manifest.version.stage(Stage.VALIDATE).artifacts = {
                "full_workbook": str(pipeline.artifacts_dir / "translated-full.xlsx"),
                "items": str(items_path),
            }
            scoped = root / "import-scoped.xlsx"
            scoped.write_bytes(b"xlsx")
            translated = root / "translated-game"
            runner = mock.Mock()
            runner.translate.return_value = translated
            runner.diagnostics = []
            runner.console_outputs = []
            stale_diagnostics = pipeline.artifacts_dir / "official-diagnostics.json"
            stale_diagnostics.parent.mkdir(parents=True, exist_ok=True)
            stale_diagnostics.write_text("stale", encoding="utf-8")

            with mock.patch.object(pipeline, "_official_runner", return_value=runner) as factory, mock.patch(
                "pipeline.write_scoped_workbook", return_value=scoped
            ):
                artifacts = pipeline._import()

            factory.assert_called_once_with(full_export_scope())
            runner.translate.assert_called_once()
            self.assertEqual(str(translated), artifacts["translated_game"])
            self.assertFalse(stale_diagnostics.exists())

    def test_import_persists_official_warnings_and_console(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pipeline = self._translation_pipeline(root)
            items = [TranslationItem(key="plain", original="甲", translation="译文", code="COMMON-1")]
            items_path = dump_items(pipeline.artifacts_dir / "items-translated.json", items)
            pipeline.manifest.version.stage(Stage.VALIDATE).artifacts = {
                "full_workbook": str(pipeline.artifacts_dir / "translated-full.xlsx"),
                "items": str(items_path),
            }
            scoped = root / "import-scoped.xlsx"
            scoped.write_bytes(b"xlsx")
            runner = mock.Mock()
            runner.translate.return_value = root / "translated-game"
            runner.diagnostics = [
                {
                    "mode": "TRANSLATE",
                    "code": "COMMON-1",
                    "source": "normalized-source",
                    "message": "warning",
                }
            ]
            runner.console_outputs = [
                {"mode": "TRANSLATE", "timeline": "earlier screen", "final": "raw screen"}
            ]

            with mock.patch.object(pipeline, "_official_runner", return_value=runner), mock.patch(
                "pipeline.write_scoped_workbook", return_value=scoped
            ):
                artifacts = pipeline._import()

            self.assertEqual("1", artifacts["official_warning_count"])
            warnings = json.loads(Path(artifacts["official_warnings"]).read_text(encoding="utf-8"))
            self.assertEqual(runner.diagnostics, warnings)
            self.assertEqual("甲", warnings[0]["source"])
            console = Path(artifacts["official_console"]).read_text(encoding="utf-8")
            self.assertIn("earlier screen", console)
            self.assertIn("raw screen", console)

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
