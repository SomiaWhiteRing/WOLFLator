import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ainiee import run_proofread
from models import AppSettings, ImportCategory, Stage, StageStatus, TranslationItem
from pipeline import Pipeline, create_project, load_manifest
from proofread import (
    build_worker_input,
    load_report,
    make_report,
    report_is_stale,
    run_project_proofread,
    save_report,
)
from wolf_tools import dump_items, load_items, sha256_file


def make_game(root: Path) -> Path:
    (root / "Data" / "BasicData").mkdir(parents=True)
    (root / "Game.exe").write_bytes(b"game")
    (root / "Data" / "BasicData" / "Game.dat").write_bytes(b"data")
    return root


def translated_project(root: Path) -> tuple[Path, Path]:
    manifest_path = create_project(root / "projects", make_game(root / "game"))
    items_path = dump_items(
        root / "projects" / "items-translated.json",
        [
            TranslationItem(
                key="display-one",
                code="COMMON-1-1-0",
                original=r"\C[1]原文",
                translation=r"\C[1]译文",
                context="Message",
                control_signature=[r"\C[1]"],
            ),
            TranslationItem(
                key="display-two",
                code="COMMON-1-2-0",
                original="第二行",
                translation="第二译文",
                context="Message",
            ),
            TranslationItem(
                key="external",
                code="FILE-1",
                original="外部",
                translation="外部译文",
                category=ImportCategory.EXTERNAL,
            ),
            TranslationItem(
                key="copy",
                code="COPY-1",
                original="COPY-FROM-COMMON-1-1-0",
                translation="不应校对",
                category=ImportCategory.COPY,
            ),
        ],
    )
    manifest = load_manifest(manifest_path)
    translate = manifest.version.stage(Stage.TRANSLATE)
    translate.status = StageStatus.COMPLETED
    translate.artifacts["items"] = str(items_path)
    manifest_path.write_text(json.dumps(manifest.to_dict(), ensure_ascii=False), encoding="utf-8")
    return manifest_path, items_path


class ProofreadTests(unittest.TestCase):
    def test_input_uses_translation_scope_stable_keys_context_and_control_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path, items_path = translated_project(Path(directory))
            manifest = load_manifest(manifest_path)
            payload = build_worker_input(items_path, manifest, context_lines=1)
            self.assertEqual(
                ["display-one", "display-two", "external"],
                [row["key"] for row in payload["rows"]],
            )
            self.assertEqual("COMMON-1-1-0", payload["rows"][0]["code"])
            self.assertEqual("\ue100原文", payload["rows"][0]["original"])
            self.assertEqual("\ue100译文", payload["rows"][0]["translation"])
            self.assertEqual("第二行", payload["rows"][0]["context"][0]["original"])

    def test_report_is_strict_and_rejects_invalid_control_suggestion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path, items_path = translated_project(root)
            manifest = load_manifest(manifest_path)
            payload = build_worker_input(items_path, manifest, context_lines=1)
            result = {
                "schema": 1,
                "entries": {
                    "display-one": {
                        "issues": [
                            {
                                "source": "ai",
                                "type": "omission",
                                "severity": "high",
                                "description": "漏译",
                                "suggestion": "补全",
                                "confidence": 0.91,
                            }
                        ],
                        "suggested_translation": "没有控制符",
                    }
                },
                "failed_batches": [{"batch": 2, "keys": ["display-two"], "error": "failed"}],
            }
            report = make_report(
                payload,
                result,
                mode="rules_ai",
                model="test-model",
                batch_size=20,
                context_lines=1,
                confidence_percent=70,
            )
            self.assertEqual("partial", report["status"])
            self.assertFalse(report["entries"][0]["applicable"])
            self.assertEqual(r"\C[1]译文", report["entries"][0]["suggested_translation"])
            path = save_report(root / "report.json", report)
            self.assertEqual(1, load_report(path)["schema"])
            invalid = json.loads(path.read_text(encoding="utf-8"))
            invalid["unexpected"] = True
            path.write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "字段不匹配"):
                load_report(path)

    def test_report_rejects_missing_or_unchanged_full_suggestion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path, items_path = translated_project(root)
            payload = build_worker_input(items_path, load_manifest(manifest_path), context_lines=0)
            issue = {
                "source": "rule", "type": "quote_pair", "severity": "medium",
                "description": "引号不配对", "suggestion": "修正引号", "confidence": 1.0,
            }
            result = {
                "schema": 1,
                "entries": {
                    "display-one": {"issues": [issue], "suggested_translation": ""},
                    "display-two": {"issues": [issue], "suggested_translation": "第二译文"},
                },
                "failed_batches": [],
            }
            report = make_report(
                payload, result, mode="rules_ai", model="model", batch_size=20,
                context_lines=0, confidence_percent=70,
            )
            missing, unchanged = report["entries"]
            self.assertFalse(missing["applicable"])
            self.assertEqual("", missing["suggested_translation"])
            self.assertIn("未生成", missing["apply_error"])
            self.assertFalse(unchanged["applicable"])
            self.assertEqual("第二译文", unchanged["suggested_translation"])
            self.assertIn("没有产生任何修改", unchanged["apply_error"])

    def test_apply_and_restore_preserve_ai_items_and_reset_only_downstream(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path, items_path = translated_project(root)
            manifest = load_manifest(manifest_path)
            for stage in (Stage.COPY, Stage.UNPACK, Stage.EXTRACT, Stage.GLOSSARY, Stage.VALIDATE, Stage.IMPORT, Stage.RELEASE):
                manifest.version.stage(stage).status = StageStatus.COMPLETED
            manifest_path.write_text(json.dumps(manifest.to_dict(), ensure_ascii=False), encoding="utf-8")
            payload = build_worker_input(items_path, manifest, context_lines=0)
            result = {
                "schema": 1,
                "entries": {
                    "display-one": {
                        "issues": [{
                            "source": "rule", "type": "quote_pair", "severity": "medium",
                            "description": "test", "suggestion": "", "confidence": 1.0,
                        }],
                        "suggested_translation": "\ue100修订",
                    }
                },
                "failed_batches": [],
            }
            report = make_report(
                payload, result, mode="rules", model="test-model", batch_size=20,
                context_lines=0, confidence_percent=70,
            )
            report["entries"][0]["decision"] = "accept"
            report["entries"][0]["edited_translation"] = r"\C[1]修订"
            report_path = save_report(root / "report.json", report)
            pipeline = Pipeline(manifest_path, AppSettings(), "", root / "cache", glossary_api_key="")
            output = pipeline.apply_proofread(report_path)
            applied = load_items(output)
            self.assertEqual(r"\C[1]修订", applied[0].translation)
            manifest = load_manifest(manifest_path)
            translate = manifest.version.stage(Stage.TRANSLATE)
            self.assertEqual(StageStatus.COMPLETED, translate.status)
            self.assertEqual(str(items_path), translate.artifacts["items_ai_translation"])
            self.assertEqual(str(output), translate.artifacts["items"])
            self.assertTrue(all(
                manifest.version.stage(stage).status is StageStatus.COMPLETED
                for stage in (Stage.COPY, Stage.UNPACK, Stage.EXTRACT, Stage.GLOSSARY, Stage.TRANSLATE)
            ))
            self.assertTrue(all(
                manifest.version.stage(stage).status is StageStatus.PENDING
                for stage in (Stage.VALIDATE, Stage.IMPORT, Stage.RELEASE)
            ))
            self.assertTrue(report_is_stale(report, output))
            pipeline.restore_ai_translation()
            restored = load_manifest(manifest_path).version.stage(Stage.TRANSLATE)
            self.assertEqual(str(items_path), restored.artifacts["items"])
            self.assertNotIn("items_ai_translation", restored.artifacts)

    def test_manual_translation_edits_validate_tokens_and_reset_downstream(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path, items_path = translated_project(root)
            manifest = load_manifest(manifest_path)
            for stage in (Stage.VALIDATE, Stage.IMPORT, Stage.RELEASE):
                manifest.version.stage(stage).status = StageStatus.COMPLETED
            manifest_path.write_text(
                json.dumps(manifest.to_dict(), ensure_ascii=False),
                encoding="utf-8",
            )
            pipeline = Pipeline(
                manifest_path,
                AppSettings(),
                "",
                root / "cache",
                glossary_api_key="",
            )
            source_hash = sha256_file(items_path)
            with self.assertRaisesRegex(ValueError, "控制符"):
                pipeline.apply_translation_edits(
                    {"display-one": r"\C[2]错误修订"},
                    source_sha256=source_hash,
                )

            output = pipeline.apply_translation_edits(
                {"display-one": r"\C[1]人工润色"},
                source_sha256=source_hash,
            )
            edited = load_items(output)
            self.assertEqual(r"\C[1]人工润色", edited[0].translation)
            manifest = load_manifest(manifest_path)
            translate = manifest.version.stage(Stage.TRANSLATE)
            self.assertEqual(str(output), translate.artifacts["items"])
            self.assertEqual(str(items_path), translate.artifacts["items_before_edit"])
            self.assertTrue(all(
                manifest.version.stage(stage).status is StageStatus.PENDING
                for stage in (Stage.VALIDATE, Stage.IMPORT, Stage.RELEASE)
            ))

    def test_worker_reports_progress_partial_failure_and_keeps_secret_out_of_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime"
            package = runtime / "ModuleFolders" / "Service" / "Proofreader"
            package.mkdir(parents=True)
            (package / "RuleBasedChecker.py").write_text(
                """
from types import SimpleNamespace
class RuleBasedChecker:
    def check(self, source, target):
        return [SimpleNamespace(rule_name='newline_consistency', severity='low', description='rule', fix_suggestion='')] if 'bad' in target else []
""",
                encoding="utf-8",
            )
            (package / "AIProofreader.py").write_text(
                """
from types import SimpleNamespace
class AIProofreader:
    def __init__(self, config): self.prompt_template = 'base prompt'
    def proofread_lines_block(self, items, **kwargs):
        assert 'corrected_translation' in self.prompt_template
        assert 'WOLFLator已核验规则问题' in kwargs['world_building']
        if items[0]['index'] >= 2:
            raise RuntimeError('batch failed')
        issue = SimpleNamespace(type='logic_error', severity='high', description='ai', suggestion='fix', confidence=.9)
        return {
            item['index']: SimpleNamespace(
                issues=[issue],
                corrected_translation='fixed' if item['index'] == 0 else '',
            )
            for item in items
        }
""",
                encoding="utf-8",
            )
            input_path = root / "input.json"
            output_path = root / "output.json"
            payload = {
                "schema": 1,
                "rows": [
                    {"index": index, "key": f"key-{index}", "original": "src", "translation": "bad", "context": []}
                    for index in range(3)
                ],
                "config": {"target_platform": "custom", "platforms": {"custom": {"api_key": ""}}},
                "glossary": [],
            }
            input_path.write_text(json.dumps(payload), encoding="utf-8")
            secret = "worker-secret-value"
            env = os.environ.copy()
            env["WOLFLATOR_PROOFREAD_API_KEY"] = secret
            result = subprocess.run(
                [
                    sys.executable, str(Path(__file__).parents[1] / "ainiee_proofread_worker.py"),
                    "--runtime", str(runtime), "--input", str(input_path), "--output", str(output_path),
                    "--mode", "rules_ai", "--batch-size", "2", "--confidence", "70", "--threads", "2",
                ],
                text=True,
                encoding="utf-8",
                capture_output=True,
                env=env,
                check=True,
            )
            output = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(2, len(output["failed_batches"]))
            self.assertIn(
                "未返回可直接替换",
                "\n".join(batch["error"] for batch in output["failed_batches"]),
            )
            self.assertIn("WOLFLATOR_PROOFREAD_EVENT", result.stdout)
            self.assertIn("logic_error", {issue["type"] for issue in output["entries"]["key-0"]["issues"]})
            self.assertNotIn(secret, input_path.read_text(encoding="utf-8"))
            self.assertNotIn(secret, output_path.read_text(encoding="utf-8"))
            self.assertNotIn(secret, result.stdout + result.stderr)

    def test_adapter_passes_secret_only_in_environment_and_forwards_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime"
            runtime.mkdir()
            input_path = root / "input.json"
            output_path = root / "output.json"
            input_path.write_text(json.dumps({"schema": 1, "source_sha256": "hash", "rows": []}), encoding="utf-8")
            events = []
            secret = "environment-only-secret"

            def fake_process(command, **kwargs):
                self.assertNotIn(secret, " ".join(command))
                self.assertEqual(secret, kwargs["env"]["WOLFLATOR_PROOFREAD_API_KEY"])
                kwargs["output_line"](
                    "stdout",
                    'WOLFLATOR_PROOFREAD_EVENT {"event":"progress","current":1,"total":1}',
                )
                output_path.write_text(
                    json.dumps({"schema": 1, "entries": {}, "failed_batches": []}),
                    encoding="utf-8",
                )

            settings = AppSettings(
                api_base_url="https://example.test/v1",
                api_model="model",
                proofread_mode="rules_ai",
            )
            with mock.patch("ainiee.validate_ainiee_source", return_value=runtime), mock.patch(
                "ainiee.locate_uv", return_value=root / "uv.exe"
            ), mock.patch("ainiee.run_process", side_effect=fake_process):
                result = run_proofread(
                    runtime,
                    input_path,
                    output_path,
                    {},
                    settings,
                    secret,
                    progress=events.append,
                )
            self.assertEqual(1, events[0]["current"])
            self.assertEqual({}, result["entries"])
            self.assertNotIn(secret, input_path.read_text(encoding="utf-8"))
            self.assertNotIn(secret, output_path.read_text(encoding="utf-8"))

    def test_cancelled_run_keeps_previous_complete_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path, items_path = translated_project(root)
            manifest = load_manifest(manifest_path)
            worker_input = build_worker_input(items_path, manifest, context_lines=0)
            report = make_report(
                worker_input,
                {"schema": 1, "entries": {}, "failed_batches": []},
                mode="rules",
                model="",
                batch_size=20,
                context_lines=0,
                confidence_percent=70,
            )
            from proofread import proofread_paths
            from wolf_tools import CancelledError

            report_path, _ = proofread_paths(manifest_path, manifest)
            save_report(report_path, report)
            before = report_path.read_bytes()
            settings = AppSettings(
                ainiee_source=str(root / "ainiee"),
                proofread_mode="rules",
            )
            with mock.patch("proofread.require_managed_runtime", return_value=root / "runtime"), mock.patch(
                "proofread.run_proofread", side_effect=CancelledError("cancelled")
            ):
                with self.assertRaises(CancelledError):
                    run_project_proofread(
                        manifest_path,
                        manifest,
                        settings,
                        "",
                        root / "cache",
                    )
            self.assertEqual(before, report_path.read_bytes())


if __name__ == "__main__":
    unittest.main()
