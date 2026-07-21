import json
import os
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font
from openpyxl.worksheet.table import Table, TableStyleInfo

import ainiee
from models import AppSettings, ImportCategory, ImportScope, ToolResult
from wolf_tools import (
    CancelledError,
    merge_ainiee_output,
    protect_control_tokens,
    read_translation_items,
    reconcile_incremental,
    restore_control_tokens,
    run_process,
    to_paratranz,
    write_full_workbook,
    write_scoped_workbook,
)


HEADERS = [
    "Code (No Change)",
    "Flag (No Change)",
    "Type",
    "Info",
    "Your notes",
    "Original text (No Change)",
    "Translated text 1 / Chinese (Simplified)",
]


def make_workbook(path: Path) -> Path:
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(HEADERS)
    sheet.append(["COMMON-1", "", "Event", "Message", "", r"こんにちは\C[1]", ""])
    sheet.append(["NAME-D-SDB-1-0", "", "SDB info", "Data name", "", "主人公", ""])
    sheet.append(["SDB-1-0", "<FILENAME>", "Image", "File", "", "Picture/顔.png", ""])
    sheet.append(["COMMON-2", "<Half-Width Characters Only>", "Event", "Code", "", "ABC", ""])
    sheet.append(["COMMON-3", "COPY-FROM-COMMON-1", "Event", "Copy", "", r"こんにちは\C[1]", ""])
    sheet.append(["TXT-1", "", "TXT File", "Line", "", "外部テキスト", ""])
    sheet.append(["DUP", "", "Event", "A", "", "重複", ""])
    sheet.append(["DUP", "", "Event", "B", "", "重複", ""])
    sheet["A1"].font = Font(bold=True, color="FF112233")
    table = Table(displayName="WolfTranslation", ref=f"A1:G{sheet.max_row}")
    table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
    sheet.add_table(table)
    workbook.save(path)
    return path


class WorkbookTests(unittest.TestCase):
    def test_classification_stable_keys_and_controls(self):
        with tempfile.TemporaryDirectory() as directory:
            path = make_workbook(Path(directory) / "source.xlsx")
            items = read_translation_items(path)
            self.assertEqual(8, len(items))
            self.assertEqual(
                [
                    ImportCategory.DISPLAY,
                    ImportCategory.OPTIONAL_NAME,
                    ImportCategory.FILENAME,
                    ImportCategory.HALFWIDTH,
                    ImportCategory.COPY,
                    ImportCategory.EXTERNAL,
                    ImportCategory.DISPLAY,
                    ImportCategory.DISPLAY,
                ],
                [item.category for item in items],
            )
            self.assertNotEqual(items[-1].key, items[-2].key)
            payload = to_paratranz(items)
            self.assertEqual(7, len(payload))
            protected = payload[0]["original"]
            self.assertIn(chr(0xE100), protected)
            self.assertNotIn(r"\C[1]", protected)

    def test_merge_and_scoped_workbook_preserve_table(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = make_workbook(root / "source.xlsx")
            items = read_translation_items(source)
            payload = to_paratranz(items)
            output = []
            for row in payload:
                translation = "译文"
                if chr(0xE100) in row["original"]:
                    translation += chr(0xE100)
                output.append({**row, "translation": translation, "stage": 1})
            merge_ainiee_output(items, output)
            full = write_full_workbook(source, root / "full.xlsx", items)
            game = root / "game"
            (game / "Data").mkdir(parents=True)
            scoped = write_scoped_workbook(full, root / "scoped.xlsx", ImportScope(), game)
            workbook = load_workbook(scoped)
            sheet = workbook.active
            values = [sheet.cell(row, 7).value for row in range(2, 10)]
            self.assertTrue(values[0])
            self.assertIsNone(values[1])
            self.assertIsNone(values[2])
            self.assertIsNone(values[3])
            self.assertIsNone(values[4])
            self.assertIsNone(values[5])
            self.assertTrue(values[6])
            self.assertTrue(values[7])
            self.assertIn("WolfTranslation", sheet.tables)
            self.assertTrue(sheet["A1"].font.bold)

    def test_filename_scope_requires_real_target(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = make_workbook(root / "source.xlsx")
            items = read_translation_items(source)
            for item in items:
                if item.category is ImportCategory.FILENAME:
                    item.translation = "Picture/face.png"
                elif item.category is not ImportCategory.COPY:
                    item.translation = "译文" + (r"\C[1]" if item.control_signature else "")
            full = write_full_workbook(source, root / "full.xlsx", items)
            game = root / "game"
            (game / "Data" / "Picture").mkdir(parents=True)
            scope = ImportScope(filename=True)
            with self.assertRaisesRegex(ValueError, "没有对应真实文件"):
                write_scoped_workbook(full, root / "bad.xlsx", scope, game)
            (game / "Data" / "Picture" / "face.png").write_bytes(b"png")
            write_scoped_workbook(full, root / "good.xlsx", scope, game)

    def test_incremental_ambiguity_is_not_guessed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = make_workbook(Path(directory) / "source.xlsx")
            previous = read_translation_items(path)
            duplicates = [item for item in previous if item.original == "重複"]
            duplicates[0].translation = "译法一"
            duplicates[1].translation = "译法二"
            current = read_translation_items(path)
            moved = [item for item in current if item.original == "重複"][0]
            moved.key = "new-location"
            current = [moved]
            reconciled, conflicts = reconcile_incremental(previous, current)
            self.assertEqual("", reconciled[0].translation)
            self.assertEqual(["译法一", "译法二"], conflicts[0]["candidates"])


class ControlTests(unittest.TestCase):
    def test_round_trip_and_reorder_rejection(self):
        protected, tokens = protect_control_tokens(r"\C[1]名前\V[2]")
        self.assertEqual([r"\C[1]", r"\V[2]"], tokens)
        self.assertEqual(r"\C[1]姓名\V[2]", restore_control_tokens(protected.replace("名前", "姓名"), tokens))
        swapped = protected.replace(chr(0xE100), "X").replace(chr(0xE101), chr(0xE100)).replace("X", chr(0xE101))
        with self.assertRaisesRegex(ValueError, "占位序列"):
            restore_control_tokens(swapped, tokens)


class ProcessTests(unittest.TestCase):
    def test_nonzero_and_cancel(self):
        with self.assertRaisesRegex(RuntimeError, "退出码 3"):
            run_process([sys.executable, "-c", "raise SystemExit(3)"], timeout=10)
        event = threading.Event()
        event.set()
        with self.assertRaises(CancelledError):
            run_process([sys.executable, "-c", "import time; time.sleep(5)"], cancel_event=event)


class AiNieeTests(unittest.TestCase):
    def make_runtime(self, root: Path) -> Path:
        (root / "Resource" / "profiles").mkdir(parents=True)
        (root / "Resource" / "rules_profiles").mkdir(parents=True)
        (root / "ainiee_cli.py").write_text(
            "# translate --rules-profile --type --api-key\n", encoding="utf-8"
        )
        (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        (root / "uv.lock").write_text("lock", encoding="utf-8")
        return root

    def test_safe_extract_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("../outside.txt", "bad")
            with self.assertRaisesRegex(ValueError, "越界路径"):
                ainiee._safe_extract(archive, root / "out")

    def test_dependencies_are_prepared_before_runtime_is_required(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.make_runtime(root / "source")

            def fake_sync(command, *, cwd, **_kwargs):
                (Path(cwd) / ".venv").mkdir()
                return ToolResult(command, 0)

            with mock.patch.object(ainiee, "run_process", side_effect=fake_sync) as run:
                runtime = ainiee.prepare_managed_runtime(source, root / "runtimes")
                self.assertEqual(runtime, ainiee.require_managed_runtime(source, root / "runtimes"))
                self.assertEqual(1, run.call_count)

            (runtime / ".uv-sync").unlink()
            with self.assertRaisesRegex(RuntimeError, "请打开设置"):
                ainiee.require_managed_runtime(source, root / "runtimes")

    def test_api_test_leaves_room_for_reasoning_tokens(self):
        settings = AppSettings(api_base_url="https://example.com/v1", api_model="reasoning-model")
        with mock.patch.object(ainiee.OpenAICompatibleClient, "chat", return_value="ok") as chat:
            self.assertEqual("ok", ainiee.test_api(settings, "secret"))
        self.assertIsNone(chat.call_args.kwargs["max_tokens"])
        self.assertEqual("小可爱，你在干嘛", chat.call_args.args[0])
        self.assertEqual(
            "你接下来要扮演我的女朋友，名字叫欣雨，请你以女朋友的方式回复我。",
            chat.call_args.kwargs["system_prompt"],
        )

    def test_deepseek_request_matches_managed_ainiee_profile(self):
        response = mock.MagicMock()
        response.read.return_value = json.dumps(
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        ).encode("utf-8")
        response.__enter__.return_value = response
        client = ainiee.OpenAICompatibleClient(
            "https://api.deepseek.com/v1/chat/completions", "secret", "deepseek-chat"
        )
        with mock.patch("urllib.request.urlopen", return_value=response) as urlopen:
            self.assertEqual(
                "ok",
                client.chat("hello", max_tokens=None, system_prompt="system"),
            )
        request = urlopen.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual("https://api.deepseek.com/v1/chat/completions", request.full_url)
        self.assertNotIn("max_tokens", body)
        self.assertEqual({"type": "disabled"}, body["thinking"])
        self.assertEqual(["system", "user"], [message["role"] for message in body["messages"]])

    def test_zero_exit_without_artifact_is_failure_and_profile_is_cleaned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = self.make_runtime(root / "runtime")
            input_path = root / "input.json"
            input_path.write_text("[]", encoding="utf-8")
            settings = AppSettings(api_base_url="https://example.com/v1", api_model="model")
            fake_result = ToolResult([], 0)
            with mock.patch.object(ainiee, "run_process", return_value=fake_result), mock.patch.object(
                ainiee, "locate_uv", return_value=Path(sys.executable)
            ):
                with self.assertRaisesRegex(RuntimeError, "没有生成"):
                    ainiee.run_translation(
                        runtime, input_path, root / "output", dict(ainiee.RULE_DEFAULTS), "project", settings, "secret"
                    )
            self.assertFalse((runtime / "Resource" / "profiles" / "WOLFLator_session.json").exists())


if __name__ == "__main__":
    unittest.main()
