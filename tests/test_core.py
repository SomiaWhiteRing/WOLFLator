import http.server
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
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
    OfficialToolRunner,
    SUPPORT_DIR,
    _console_delta,
    _official_config_text,
    _pe_import_name_offset,
    _process_startupinfo,
    _silent_official_executable,
    _write_console_snapshot,
    apply_managed_translations,
    classify_optional_name_delta,
    dump_items,
    full_export_scope,
    load_items,
    locate_workbook,
    merge_ainiee_output,
    name_baseline_scope,
    protect_control_tokens,
    read_translation_items,
    reconcile_incremental,
    restore_control_tokens,
    retryable_translation_errors,
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
            payload = to_paratranz(items, full_export_scope())
            self.assertEqual(7, len(payload))
            protected = payload[0]["original"]
            self.assertIn(chr(0xE100), protected)
            self.assertNotIn(r"\C[1]", protected)

    def test_safe_scope_excludes_optional_rows_from_ai(self):
        with tempfile.TemporaryDirectory() as directory:
            items = read_translation_items(make_workbook(Path(directory) / "source.xlsx"))
            payload = to_paratranz(items, ImportScope())
            self.assertEqual(3, len(payload))
            self.assertNotIn("主人公", {row["original"] for row in payload})
            self.assertNotIn("Picture/顔.png", {row["original"] for row in payload})
            translated = [
                {
                    **row,
                    "translation": "译文" + "".join(
                        char for char in row["original"] if 0xE100 <= ord(char) <= 0xF7FF
                    ),
                    "stage": 1,
                }
                for row in payload
            ]
            merged = merge_ainiee_output(items, translated, ImportScope())
            self.assertEqual("", merged[1].translation)
            self.assertEqual("", merged[2].translation)

    def test_explicit_ainiee_exclusion_is_restored_without_becoming_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            items = read_translation_items(make_workbook(Path(directory) / "source.xlsx"))
            payload = to_paratranz(items, ImportScope())
            translated = [
                {
                    **row,
                    "translation": row["original"] if index == 0 else "译文",
                    "stage": 1,
                    **({"wolflator_excluded": True} if index == 0 else {}),
                }
                for index, row in enumerate(payload)
            ]
            merged = merge_ainiee_output(items, translated, ImportScope())
            self.assertEqual(items[0].original, merged[0].translation)

            translated[0]["translation"] = "被篡改"
            with self.assertRaisesRegex(ValueError, "不能安全原样回填"):
                merge_ainiee_output(items, translated, ImportScope())

    def test_control_failure_identifies_the_wolf_row(self):
        with tempfile.TemporaryDirectory() as directory:
            items = read_translation_items(make_workbook(Path(directory) / "source.xlsx"))
            payload = to_paratranz(items, ImportScope())
            translated = [{**row, "translation": "译文", "stage": 1} for row in payload]
            with self.assertRaisesRegex(ValueError, "COMMON-1.*占位序列"):
                merge_ainiee_output(items, translated, ImportScope())

    def test_middle_dot_normalization_skips_filename_and_halfwidth_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(HEADERS)
            sheet.append(["DISPLAY-1", "", "Event", "Message", "", r"表示・文\C[1]", ""])
            sheet.append(["DISPLAY-2", "", "Event", "Message", "", "画像・名", ""])
            sheet.append(
                ["FILE-COPY", "<FILENAME>\nCOPY-FROM-DISPLAY-2", "Image", "File", "", "画像・名", ""]
            )
            sheet.append(["FILE-1", "<FILENAME>", "Image", "File", "", "画像・名.png", ""])
            sheet.append(["HALF-1", "<HALF-WIDTH CHARACTERS ONLY>", "Event", "Code", "", "A・B", ""])
            workbook.save(path)

            items = read_translation_items(path)
            payload = to_paratranz(items, full_export_scope())
            translated = []
            for row in payload:
                controls = "".join(char for char in row["original"] if 0xE100 <= ord(char) <= 0xF7FF)
                translated.append({**row, "translation": "中・文" + controls, "stage": 1})

            merged = merge_ainiee_output(items, translated, full_export_scope())
            by_code = {item.code: item.translation for item in merged}
            self.assertEqual(r"中·文\C[1]", by_code["DISPLAY-1"])
            self.assertEqual("中・文", by_code["DISPLAY-2"])
            self.assertEqual("", by_code["FILE-COPY"])
            self.assertEqual("中・文", by_code["FILE-1"])
            self.assertEqual("中・文", by_code["HALF-1"])

    def test_retryable_errors_include_only_missing_empty_and_invalid_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            items = read_translation_items(make_workbook(Path(directory) / "source.xlsx"))
            payload = to_paratranz(items, ImportScope())
            translated = [
                {**payload[0], "translation": "缺少控制符", "stage": 1},
                {**payload[1], "translation": "", "stage": 0},
            ]
            errors = retryable_translation_errors(items, translated, ImportScope())
            self.assertEqual({row["key"] for row in payload}, set(errors))
            self.assertIn("控制符", errors[payload[0]["key"]])
            self.assertIn("没有生成译文", errors[payload[1]["key"]])
            self.assertIn("缺少输出", errors[payload[2]["key"]])

    def test_official_config_exports_all_and_fonts_use_managed_defaults(self):
        config = _official_config_text(full_export_scope())
        self.assertIn("Tool_A_Get_CommonEvent_Name=1\r\n", config)
        self.assertIn("Tool_A_Get_DB_DataName=1\r\n", config)
        self.assertIn("Tool_A_Get_TXT=1\r\n", config)
        baseline = _official_config_text(name_baseline_scope())
        self.assertIn("Tool_A_Get_CommonEvent_Name=0\r\n", baseline)
        self.assertIn("Tool_A_Get_DB_DataName=0\r\n", baseline)
        self.assertIn("Tool_A_Get_TXT=1\r\n", baseline)

        with tempfile.TemporaryDirectory() as directory:
            items = read_translation_items(make_workbook(Path(directory) / "source.xlsx"))
            items[0].code = "BASICDATA-3"
            apply_managed_translations(items)
            self.assertEqual("KaiTi", items[0].translation)

    def test_full_baseline_delta_and_cross_category_copy_are_scope_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            full_path = root / "full.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(HEADERS)
            sheet.append(["NAME-D-UDB-1-0", "", "UDB info", "Data name", "", "攻撃力", ""])
            sheet.append(
                [
                    "UDB-1-0-0",
                    "COPY-FROM-NAME-D-UDB-1-0",
                    "Status",
                    "Label",
                    "",
                    "攻撃力",
                    "",
                ]
            )
            sheet.append(["COMMON-1-0-0", "", "Event", "(Common Event)", "", "内部名", ""])
            sheet.append(["DISPLAY-1", "", "Event", "Message", "", "顔", ""])
            sheet.append(["FILE-1", "<FILENAME>\nCOPY-FROM-DISPLAY-1", "Image", "File", "", "顔", ""])
            sheet.append(["NAME-D-SDB-0-9", "<FILENAME>", "SDB info", "Data name", "", "トイレ", ""])
            sheet.append(["DISPLAY-2", "COPY-FROM-NAME-D-SDB-0-9", "Event", "Message", "", "トイレ", ""])
            workbook.save(full_path)

            baseline_path = root / "baseline.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(HEADERS)
            sheet.append(["UDB-1-0-0", "", "Status", "Label", "", "攻撃力", ""])
            sheet.append(["DISPLAY-1", "", "Event", "Message", "", "顔", ""])
            sheet.append(["FILE-1", "<FILENAME>\nCOPY-FROM-DISPLAY-1", "Image", "File", "", "顔", ""])
            sheet.append(["DISPLAY-2", "COPY-FROM-NAME-D-SDB-0-9", "Event", "Message", "", "トイレ", ""])
            workbook.save(baseline_path)

            items = read_translation_items(full_path)
            baseline_items = read_translation_items(baseline_path)
            self.assertEqual(3, classify_optional_name_delta(items, baseline_items))
            self.assertEqual(ImportCategory.OPTIONAL_NAME, items[0].category)
            self.assertEqual(ImportCategory.DISPLAY, items[1].copy_category)
            self.assertEqual(ImportCategory.OPTIONAL_NAME, items[2].category)

            payload = to_paratranz(items, ImportScope())
            self.assertEqual(["攻撃力"], [row["original"] for row in payload])
            merge_ainiee_output(items, [{**payload[0], "translation": "攻击力", "stage": 1}], ImportScope())
            translated_full = write_full_workbook(full_path, root / "translated.xlsx", items)
            scoped = write_scoped_workbook(
                translated_full,
                root / "scoped.xlsx",
                ImportScope(),
                root / "game",
                items,
            )
            output = load_workbook(scoped)
            self.assertEqual("攻击力", output.active["G2"].value)
            self.assertIsNone(output.active["G3"].value)
            self.assertIsNone(output.active["G4"].value)
            self.assertIsNone(output.active["G5"].value)
            self.assertIsNone(output.active["G6"].value)
            self.assertIsNone(output.active["G7"].value)
            self.assertIsNone(output.active["G8"].value)

    def test_merge_and_scoped_workbook_preserve_table(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = make_workbook(root / "source.xlsx")
            items = read_translation_items(source)
            payload = to_paratranz(items, full_export_scope())
            output = []
            for row in payload:
                translation = "译文"
                if chr(0xE100) in row["original"]:
                    translation += chr(0xE100)
                output.append({**row, "translation": translation, "stage": 1})
            merge_ainiee_output(items, output, full_export_scope())
            full = write_full_workbook(source, root / "full.xlsx", items)
            game = root / "game"
            (game / "Data").mkdir(parents=True)
            scoped = write_scoped_workbook(full, root / "scoped.xlsx", ImportScope(), game, items)
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
            (game / "Data" / "Other").mkdir(parents=True)
            (game / "Data" / "Other" / "face.png").write_bytes(b"wrong path")
            scope = ImportScope(filename=True)
            with self.assertRaisesRegex(ValueError, "没有对应真实文件"):
                write_scoped_workbook(full, root / "bad.xlsx", scope, game, items)
            (game / "Data" / "Picture" / "face.png").write_bytes(b"png")
            write_scoped_workbook(full, root / "good.xlsx", scope, game, items)

    def test_workbook_locator_requires_official_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            support = root / SUPPORT_DIR
            support.mkdir()
            make_workbook(support / "plausible-but-wrong.xlsx")
            with self.assertRaisesRegex(FileNotFoundError, "WOLF_Translation_Text.xlsx"):
                locate_workbook(root)

    def test_copy_source_requires_matching_original(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(HEADERS)
            sheet.append(["COMMON-1", "", "Event", "Message", "", "原文甲", ""])
            sheet.append(["COMMON-2", "COPY-FROM-COMMON-1", "Event", "Copy", "", "原文乙", ""])
            workbook.save(path)
            with self.assertRaisesRegex(ValueError, "找不到唯一来源"):
                to_paratranz(read_translation_items(path), full_export_scope())

    def test_item_file_requires_versioned_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            items = read_translation_items(make_workbook(root / "source.xlsx"))
            versioned = dump_items(root / "items.json", items)
            self.assertEqual(len(items), len(load_items(versioned)))
            old = root / "old-items.json"
            old.write_text(
                json.dumps([item.to_dict() for item in items], ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "结构不匹配"):
                load_items(old)
            malformed = json.loads(versioned.read_text(encoding="utf-8"))
            malformed["items"][0]["stage"] = "1"
            versioned.write_text(json.dumps(malformed), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "stage 不是整数"):
                load_items(versioned)

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
    def test_pe_import_name_offset_finds_named_import(self):
        offset = _pe_import_name_offset(
            sys.executable, "KERNEL32.dll", "GetProcAddress"
        )
        self.assertEqual(b"GetProcAddress\0", Path(sys.executable).read_bytes()[offset : offset + 15])
        with self.assertRaisesRegex(ValueError, "未导入"):
            _pe_import_name_offset(sys.executable, "KERNEL32.dll", "DefinitelyMissing")

    def test_silent_official_executable_does_not_modify_source(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "official.exe"
            original = b"prefix-MessageBeep\0-suffix"
            source.write_bytes(original)
            with mock.patch("wolf_tools._pe_import_name_offset", return_value=7):
                silent = _silent_official_executable(source)
            self.assertEqual(original, source.read_bytes())
            self.assertEqual(b"prefix-IsWindow\0\0\0\0-suffix", silent)

    def test_console_delta_keeps_appends_and_rewritten_progress(self):
        self.assertEqual(["third"], _console_delta("first\nsecond", "first\nsecond\nthird"))
        self.assertEqual(
            ["Process 2 / 10", "done"],
            _console_delta("header\nProcess 1 / 10", "header\nProcess 2 / 10\ndone"),
        )

    def test_console_snapshot_retries_windows_replace_sharing_violation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            real_replace = os.replace
            attempts = 0

            def flaky_replace(source, target):
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    raise PermissionError(5, "sharing violation")
                return real_replace(source, target)

            with mock.patch("wolf_tools.os.replace", side_effect=flaky_replace):
                _write_console_snapshot(path, text="captured")
            self.assertEqual(3, attempts)
            self.assertEqual("captured", json.loads(path.read_text(encoding="utf-8"))["text"])

    def test_hidden_process_startupinfo_uses_windows_hide_flag(self):
        startupinfo = _process_startupinfo(True)
        if os.name == "nt":
            self.assertIsNotNone(startupinfo)
            self.assertTrue(startupinfo.dwFlags & subprocess.STARTF_USESHOWWINDOW)
            self.assertEqual(subprocess.SW_HIDE, startupinfo.wShowWindow)
        else:
            self.assertIsNone(startupinfo)
        self.assertIsNone(_process_startupinfo(False))

    def test_official_runner_waits_and_captures_hidden_console(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "official.exe"
            executable.touch()
            details = []
            with mock.patch(
                "wolf_tools.run_process",
                return_value=ToolResult([], 0, "", "", 0.1),
            ) as run:
                OfficialToolRunner(executable, ImportScope()).run(
                    "EXTRACT", root, diagnostic_log=details.append
                )
            command = run.call_args.args[0]
            self.assertEqual("-wait", command[-1])
            self.assertTrue(run.call_args.kwargs["hide_window"])
            self.assertTrue(run.call_args.kwargs["capture_console"])
            self.assertTrue(any("MessageBeep" in line and "IsWindow" in line for line in details))

    def test_nonzero_and_cancel(self):
        with self.assertRaisesRegex(RuntimeError, "退出码 3"):
            run_process([sys.executable, "-c", "raise SystemExit(3)"], timeout=10)
        event = threading.Event()
        event.set()
        with self.assertRaises(CancelledError):
            run_process([sys.executable, "-c", "import time; time.sleep(5)"], cancel_event=event)

    def test_diagnostic_log_streams_process_output_without_flooding_app_log(self):
        app_log = []
        diagnostic_log = []
        first_line = threading.Event()
        finished = threading.Event()

        def detail(message):
            diagnostic_log.append(message)
            if "process.stdout" in message and "first-line" in message:
                first_line.set()

        def run():
            try:
                run_process(
                    [
                        sys.executable,
                        "-u",
                        "-c",
                        "import sys,time; print('first-line', flush=True); "
                        "print('stderr-line', file=sys.stderr, flush=True); time.sleep(3)",
                    ],
                    timeout=10,
                    log=app_log.append,
                    diagnostic_log=detail,
                )
            finally:
                finished.set()

        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(first_line.wait(2.5))
        self.assertFalse(finished.is_set())
        worker.join(5)
        self.assertTrue(any("process.start" in line for line in diagnostic_log))
        self.assertTrue(any("process.stderr" in line and "stderr-line" in line for line in diagnostic_log))
        self.assertTrue(any("process.exit" in line for line in diagnostic_log))
        self.assertNotIn("first-line", app_log)
        self.assertNotIn("stderr-line", app_log)

    def test_narrow_console_cannot_abort_process_logging(self):
        messages = []

        def gbk_sink(message: str) -> None:
            message.encode("gbk")
            messages.append(message)

        result = run_process(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\xff\\n')"],
            timeout=10,
            diagnostic_log=gbk_sink,
        )
        self.assertEqual(0, result.return_code)
        self.assertTrue(any(r"\ufffd" in message for message in messages))

    def test_detached_console_cannot_invalidate_completed_process(self):
        def detached_console(_message: str) -> None:
            raise OSError(22, "invalid console handle")

        result = run_process(
            [sys.executable, "-c", "print('completed')"],
            timeout=10,
            log=detached_console,
        )
        self.assertEqual(0, result.return_code)


class AiNieeTests(unittest.TestCase):
    def make_runtime(self, root: Path) -> Path:
        (root / "Resource" / "profiles").mkdir(parents=True)
        (root / "Resource" / "rules_profiles").mkdir(parents=True)
        (root / "Resource" / "Version").mkdir(parents=True)
        (root / "Resource" / "Version" / "version.json").write_text(
            '{"version":"test"}', encoding="utf-8"
        )
        assets = root / "Tools" / "WebServer" / "dist" / "assets"
        assets.mkdir(parents=True)
        (assets.parent / "index.html").write_text("<html></html>", encoding="utf-8")
        (assets / "index.js").write_text("", encoding="utf-8")
        (root / "ainiee_cli.py").write_text("# test runtime\n", encoding="utf-8")
        (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
        (root / "uv.lock").write_text("lock", encoding="utf-8")
        patcher = mock.patch.object(ainiee, "AINIEE_SOURCE_SHA256", ainiee._source_code_hash(root))
        patcher.start()
        self.addCleanup(patcher.stop)
        return root

    def test_source_validation_rejects_changed_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_runtime(Path(directory) / "runtime")
            (root / "ainiee_cli.py").write_text("# changed\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "源码版本不兼容"):
                ainiee.validate_ainiee_source(root)

    def test_safe_extract_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("../outside.txt", "bad")
            with self.assertRaisesRegex(ValueError, "越界路径"):
                ainiee._safe_extract(archive, root / "out")

    def test_web_dist_extract_requires_official_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "web-dist.zip"
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("dist/index.html", "<html></html>")
                package.writestr("dist/assets/index.js", "")
            dist = ainiee._safe_extract_web_dist(archive, root / "out")
            self.assertTrue((dist / "index.html").is_file())
            self.assertTrue((dist / "assets" / "index.js").is_file())

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

                (runtime / "stale-runtime-file").write_text("stale", encoding="utf-8")
                refreshed = ainiee.prepare_managed_runtime(
                    source,
                    root / "runtimes",
                    force_sync=True,
                )
                self.assertEqual(runtime, refreshed)
                self.assertFalse((refreshed / "stale-runtime-file").exists())
                self.assertEqual(2, run.call_count)

            (runtime / ".uv-sync").unlink()
            with self.assertRaisesRegex(RuntimeError, "请打开设置"):
                ainiee.require_managed_runtime(source, root / "runtimes")

    def test_source_locator_rejects_multiple_compatible_roots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_runtime(root / "first")
            self.make_runtime(root / "second")
            with self.assertRaisesRegex(FileNotFoundError, "数量为 2"):
                ainiee.locate_ainiee_source(root)

    def test_managed_package_requires_install_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.make_runtime(Path(directory) / "source")
            with self.assertRaisesRegex(ValueError, "缺少安装元数据"):
                ainiee._validate_managed_package(source)

    def test_remove_managed_package_also_removes_its_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            packages = root / "packages"
            source = self.make_runtime(packages / "V2.7.5")
            runtimes = root / "runtimes"
            owned = runtimes / "owned"
            unrelated = runtimes / "unrelated"
            owned.mkdir(parents=True)
            unrelated.mkdir()
            (owned / ".wolflator-runtime.json").write_text(
                json.dumps({"source": str(source.resolve())}),
                encoding="utf-8",
            )
            (unrelated / ".wolflator-runtime.json").write_text(
                json.dumps({"source": str((root / "other").resolve())}),
                encoding="utf-8",
            )
            ainiee.remove_managed_ainiee(source, packages, runtimes)
            self.assertFalse(source.exists())
            self.assertFalse(owned.exists())
            self.assertTrue(unrelated.exists())

    def test_glossary_json_requires_an_array_of_objects(self):
        self.assertEqual([{"original": "猫"}], ainiee._json_list('[{"original":"猫"}]'))
        with self.assertRaisesRegex(ValueError, "JSON 数组"):
            ainiee._json_list('{"data":[]}')
        with self.assertRaisesRegex(ValueError, "非对象项"):
            ainiee._json_list('[{}, "bad"]')

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

    def test_translation_profile_uses_verified_deepseek_settings(self):
        settings = AppSettings(
            api_base_url="https://api.deepseek.com/v1",
            api_model="deepseek-v4-flash",
            api_threads=4,
        )
        profile = ainiee._session_profile(settings, "secret")
        platform = profile["platforms"]["deepseek"]
        self.assertEqual("deepseek", profile["target_platform"])
        self.assertEqual({"last_selected_id": 100}, profile["translation_prompt_selection"])
        self.assertEqual("openai", profile["sdk_request_mode"])
        self.assertTrue(profile["use_openai_sdk"])
        self.assertFalse(profile["auto_set_output_path"])
        self.assertFalse(profile["response_conversion_toggle"])
        self.assertTrue(profile["auto_process_text_code_segment"])
        self.assertTrue(profile["tokens_limit_switch"])
        self.assertEqual(256, profile["tokens_limit"])
        self.assertEqual(8, profile["lines_limit"])
        self.assertEqual(1, profile["retry_split_min_lines"])
        self.assertEqual(6, profile["round_limit"])
        self.assertFalse(profile["enable_smart_round_limit"])
        self.assertFalse(platform["think_switch"])
        self.assertEqual("deepseek-v4-flash", platform["model"])

    def test_only_verified_ainiee_exclusions_are_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            cache = output / "cache"
            cache.mkdir()
            input_rows = [{"key": "k", "original": "x", "translation": "", "stage": 0}]
            cache_data = {
                "files": {
                    "input.json": {
                        "items": [
                            {
                                "source_text": "x",
                                "translation_status": 7,
                                "extra": {"key": "k"},
                            }
                        ]
                    }
                }
            }
            (cache / "AinieeCacheData.json").write_text(
                json.dumps(cache_data, ensure_ascii=False), encoding="utf-8"
            )
            diagnostics = []
            restored = ainiee._restore_excluded_rows(input_rows, [], output, diagnostics.append)
            self.assertEqual("x", restored[0]["translation"])
            self.assertTrue(restored[0]["wolflator_excluded"])
            self.assertIn("restored=1 unresolved=0", diagnostics[0])

            cache_data["files"]["input.json"]["items"][0]["source_text"] = "changed"
            (cache / "AinieeCacheData.json").write_text(
                json.dumps(cache_data, ensure_ascii=False), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "原文不一致"):
                ainiee._restore_excluded_rows(input_rows, [], output, None)

            with self.assertRaisesRegex(ValueError, "重复键"):
                ainiee._restore_excluded_rows(input_rows * 2, [], output, None)

    def test_empty_dictionary_still_enables_control_protection(self):
        rules = ainiee._rules_with_control_protection(
            {"prompt_dictionary_switch": False, "prompt_dictionary_data": []}
        )
        self.assertTrue(rules["prompt_dictionary_switch"])
        self.assertEqual([], rules["prompt_dictionary_data"])
        self.assertTrue(rules["exclusion_list_switch"])
        self.assertIn(
            ainiee.CONTROL_PLACEHOLDER_REGEX,
            [item["regex"] for item in rules["exclusion_list_data"]],
        )

    def test_api_test_uses_dedicated_glossary_settings(self):
        settings = AppSettings(
            api_base_url="https://translate.example/v1",
            api_model="translate-model",
            glossary_api_base_url="https://glossary.example/v1",
            glossary_api_model="glossary-model",
            glossary_api_timeout=77,
        )
        with mock.patch.object(ainiee, "OpenAICompatibleClient") as client:
            client.return_value.chat.return_value = "ok"
            self.assertEqual("ok", ainiee.test_api(settings, "glossary-secret", glossary=True))
        client.assert_called_once_with(
            "https://glossary.example/v1",
            "glossary-secret",
            "glossary-model",
            77,
        )

    def test_nonempty_length_response_is_rejected(self):
        response = mock.MagicMock()
        response.read.return_value = json.dumps(
            {"choices": [{"message": {"content": '[{"src":"truncated"}'}, "finish_reason": "length"}]}
        ).encode("utf-8")
        response.status = 200
        response.__enter__.return_value = response
        client = ainiee.OpenAICompatibleClient("https://example.com/v1", "secret", "model")
        with mock.patch("urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(ainiee.ApiError, "输出达到上限"):
                client.chat("hello", max_tokens=4096)

    def test_glossary_output_limit_splits_chunk_immediately(self):
        client = mock.Mock()
        client.chat.side_effect = [
            ainiee.ApiError("模型输出达到上限，响应被截断。"),
            '[{"src":"left"}]',
            '[{"src":"right"}]',
        ]
        diagnostics = []
        rows = ainiee._request_chunk(
            client,
            "prompt",
            "left line\nright line",
            cancel_event=None,
            max_tokens=65_535,
            diagnostic_log=diagnostics.append,
            request_label="角色分析:1/1",
        )
        self.assertEqual([{"src": "left"}, {"src": "right"}], rows)
        self.assertEqual(3, client.chat.call_count)
        self.assertIn("glossary.split label=角色分析:1/1", "\n".join(diagnostics))
        self.assertNotIn("glossary.retry label=角色分析:1/1", "\n".join(diagnostics))
        self.assertTrue(all(call.kwargs["max_tokens"] == 65_535 for call in client.chat.call_args_list))

    def test_glossary_chunks_use_configured_character_limit(self):
        self.assertEqual(
            ["12345\n12345", "12345"],
            ainiee._chunks(["12345", "12345", "12345"], max_chars=11, overlap=0),
        )
        items = [
            ainiee.TranslationItem(key=str(index), original="文" * 30, type="Event", info="Message")
            for index in range(3)
        ]
        settings = AppSettings(
            glossary_api_base_url="https://example.com/v1",
            glossary_api_model="model",
            glossary_chunk_chars=50,
        )
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            ainiee, "_parallel_stage", return_value=[]
        ), mock.patch.object(ainiee, "_chunks", return_value=["chunk"]) as chunks:
            ainiee.generate_glossary(
                items,
                Path(directory) / "glossary.json",
                settings,
                "secret",
            )
        self.assertEqual(50, chunks.call_args.kwargs["max_chars"])

    def test_deepseek_request_matches_managed_ainiee_profile(self):
        response = mock.MagicMock()
        response.read.return_value = json.dumps(
            {
                "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            }
        ).encode("utf-8")
        response.status = 200
        response.__enter__.return_value = response
        diagnostics = []
        client = ainiee.OpenAICompatibleClient(
            "https://api.deepseek.com/v1/chat/completions",
            "secret",
            "deepseek-chat",
            diagnostic_log=diagnostics.append,
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
        joined = "\n".join(diagnostics)
        self.assertIn("api.request id=1", joined)
        self.assertIn("api.response id=1 status=200", joined)
        self.assertIn("finish_reason=stop", joined)
        self.assertIn('"prompt_tokens": 10', joined)
        self.assertNotIn("secret", joined)

    def test_http_api_error_is_written_to_diagnostic_log(self):
        diagnostics = []
        client = ainiee.OpenAICompatibleClient(
            "https://example.com/v1", "secret", "model", diagnostic_log=diagnostics.append
        )
        error = urllib.error.HTTPError(
            client.url,
            429,
            "Too Many Requests",
            {},
            io.BytesIO(b'{"error":"rate limited"}'),
        )
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(ainiee.ApiError, "429"):
                client.chat("hello")
        joined = "\n".join(diagnostics)
        self.assertIn("api.error id=1 kind=http status=429", joined)
        self.assertIn("rate limited", joined)
        self.assertNotIn("secret", joined)

    def test_api_timeout_covers_the_complete_response(self):
        body = json.dumps(
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        ).encode("utf-8")

        class SlowHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    for part in (body[:1], body[1:2], body[2:]):
                        self.wfile.write(part)
                        self.wfile.flush()
                        time.sleep(0.15)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def log_message(self, _format, *_args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        diagnostics = []
        client = ainiee.OpenAICompatibleClient(
            f"http://127.0.0.1:{server.server_port}/v1",
            "secret",
            "model",
            diagnostic_log=diagnostics.append,
        )
        client.timeout = 0.2
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(ainiee.ApiError, "总时限"):
                client.chat("hello")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=1)
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIn("kind=timeout", "\n".join(diagnostics))

    def test_glossary_json_failure_records_each_retry_and_chunk_identity(self):
        client = mock.Mock()
        client.chat.return_value = '[{"src":"broken"}'
        diagnostics = []
        with mock.patch.object(ainiee.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "Expecting"):
                ainiee._request_chunk(
                    client,
                    "prompt",
                    "line one\nline two",
                    cancel_event=None,
                    diagnostic_log=diagnostics.append,
                    request_label="角色分析:1/2",
                )
        joined = "\n".join(diagnostics)
        self.assertEqual(3, joined.count("glossary.request label=角色分析:1/2"))
        self.assertEqual(3, joined.count("glossary.error label=角色分析:1/2"))
        self.assertIn("chunk_sha256=", joined)
        self.assertIn("glossary.invalid_json label=角色分析:1/2", joined)
        self.assertIn('response_tail=[{"src":"broken"}', joined)
        self.assertIn("glossary.retry label=角色分析:1/2 delay=1s", joined)
        self.assertIn("glossary.retry label=角色分析:1/2 delay=2s", joined)

    def test_glossary_repairs_only_invalid_json_escapes(self):
        client = mock.Mock()
        client.chat.return_value = (
            r'[{"speech_quirks":"red \c[2], icon \i[3]",'
            r'"path":"C:\\Games","line":"a\nb","unicode":"\u65e5"}]'
        )
        diagnostics = []
        rows = ainiee._request_chunk(
            client,
            "prompt",
            "chunk",
            cancel_event=None,
            diagnostic_log=diagnostics.append,
            request_label="角色分析:4/20",
        )
        self.assertEqual(r"red \c[2], icon \i[3]", rows[0]["speech_quirks"])
        self.assertEqual(r"C:\Games", rows[0]["path"])
        self.assertEqual("a\nb", rows[0]["line"])
        self.assertEqual("日", rows[0]["unicode"])
        self.assertEqual(1, client.chat.call_count)
        self.assertIn("glossary.json_escape_repaired label=角色分析:4/20 repairs=2", "\n".join(diagnostics))

        broken, repairs = ainiee._repair_invalid_json_escapes(r'[{"src":"\c[2]" "dst":"红"}]')
        self.assertEqual(1, repairs)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(broken)

    def test_glossary_first_failure_cancels_queued_chunks(self):
        client = mock.Mock()
        client.chat.return_value = '[{"src":"broken"}'
        diagnostics = []
        with mock.patch.object(ainiee.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "Expecting"):
                ainiee._parallel_stage(
                    client,
                    "prompt",
                    ["bad", "must not start", "also must not start"],
                    workers=1,
                    cancel_event=None,
                    log=None,
                    diagnostic_log=diagnostics.append,
                    label="角色分析",
                    max_tokens=None,
                )
        self.assertEqual(3, client.chat.call_count)
        self.assertFalse(any("角色分析:2/3" in line for line in diagnostics))

        aborted = threading.Event()
        aborted.set()
        client.reset_mock()
        with self.assertRaises(CancelledError):
            ainiee._request_chunk(
                client,
                "prompt",
                "chunk",
                cancel_event=None,
                abort_event=aborted,
            )
        client.chat.assert_not_called()

    def test_zero_exit_without_artifact_is_failure_and_profile_is_cleaned(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = self.make_runtime(root / "runtime")
            input_path = root / "input.json"
            input_path.write_text("[]", encoding="utf-8")
            settings = AppSettings(api_base_url="https://example.com/v1", api_model="model")
            fake_result = ToolResult([], 0)
            output = root / "output"
            output.mkdir()
            (output / "input_translated.json").write_text("[]", encoding="utf-8")
            with mock.patch.object(ainiee, "run_process", return_value=fake_result) as run, mock.patch.object(
                ainiee, "locate_uv", return_value=Path(sys.executable)
            ):
                with self.assertRaisesRegex(RuntimeError, "没有生成"):
                    ainiee.run_translation(
                        runtime, input_path, output, dict(ainiee.RULE_DEFAULTS), "project", settings, "secret"
                    )
            self.assertEqual(
                ["run", "--frozen", "--no-sync", "ainiee_cli.py"],
                run.call_args.args[0][1:5],
            )
            self.assertNotIn("-p", run.call_args.args[0])
            self.assertNotIn("--rules-profile", run.call_args.args[0])
            self.assertFalse((runtime / "Resource" / "profiles" / "WOLFLator_session.json").exists())

    def test_translation_activates_profiles_restores_config_and_reads_v275_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = self.make_runtime(root / "runtime")
            config_path = runtime / "Resource" / "config.json"
            original_config = b'{"active_profile":"default","active_rules_profile":"default","keep":1}\n'
            config_path.write_bytes(original_config)
            input_path = root / "input.json"
            input_path.write_text('[{"key":"k","original":"x","translation":"","stage":0}]', encoding="utf-8")
            output = root / "output"
            settings = AppSettings(
                api_base_url="https://api.deepseek.com/v1",
                api_model="deepseek-v4-flash",
            )

            def fake_process(command, *, cwd, **_kwargs):
                self.assertNotIn("-p", command)
                self.assertNotIn("--rules-profile", command)
                self.assertEqual("6", command[command.index("--rounds") + 1])
                self.assertEqual("256", command[command.index("--tokens") + 1])
                self.assertNotIn("--lines", command)
                self.assertEqual("1", _kwargs["env"]["PYTHONUTF8"])
                self.assertEqual("utf-8", _kwargs["env"]["PYTHONIOENCODING"])
                active = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertEqual("WOLFLator_session", active["active_profile"])
                self.assertEqual("WOLFLator_project", active["active_rules_profile"])
                profile = json.loads(
                    (runtime / "Resource" / "profiles" / "WOLFLator_session.json").read_text(encoding="utf-8")
                )
                rules = json.loads(
                    (runtime / "Resource" / "rules_profiles" / "WOLFLator_project.json").read_text(encoding="utf-8")
                )
                self.assertEqual("secret", profile["platforms"]["deepseek"]["api_key"])
                self.assertTrue(profile["tokens_limit_switch"])
                self.assertEqual(256, profile["tokens_limit"])
                self.assertEqual(6, profile["round_limit"])
                self.assertFalse(profile["enable_smart_round_limit"])
                self.assertEqual([], rules["prompt_dictionary_data"])
                self.assertTrue(rules["prompt_dictionary_switch"])
                self.assertTrue(rules["exclusion_list_switch"])
                output.mkdir(parents=True, exist_ok=True)
                (output / input_path.name).write_text(
                    '[{"key":"k","original":"x","translation":"译文"}]', encoding="utf-8"
                )
                return ToolResult(command, 0)

            with mock.patch.object(ainiee, "run_process", side_effect=fake_process), mock.patch.object(
                ainiee, "locate_uv", return_value=Path(sys.executable)
            ):
                translated = ainiee.run_translation(
                    runtime,
                    input_path,
                    output,
                    {"prompt_dictionary_data": []},
                    "project",
                    settings,
                    "secret",
                )
            self.assertEqual("译文", translated[0]["translation"])
            self.assertEqual(original_config, config_path.read_bytes())
            self.assertFalse((runtime / "Resource" / "profiles" / "WOLFLator_session.json").exists())

    def test_translation_failure_includes_ainiee_session_log_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = self.make_runtime(root / "runtime")
            input_path = root / "input.json"
            input_path.write_text("[]", encoding="utf-8")
            output = root / "output"
            diagnostics = []

            def fail_process(*_args, **_kwargs):
                logs = output / "logs"
                logs.mkdir(parents=True)
                (logs / "session.log").write_text("API 429 rate limited\nretry exhausted", encoding="utf-8")
                raise RuntimeError("translation failed")

            settings = AppSettings(api_base_url="https://example.com/v1", api_model="model")
            with mock.patch.object(ainiee, "run_process", side_effect=fail_process), mock.patch.object(
                ainiee, "locate_uv", return_value=Path(sys.executable)
            ):
                with self.assertRaisesRegex(RuntimeError, "translation failed"):
                    ainiee.run_translation(
                        runtime,
                        input_path,
                        output,
                        dict(ainiee.RULE_DEFAULTS),
                        "project",
                        settings,
                        "secret",
                        diagnostic_log=diagnostics.append,
                    )
            joined = "\n".join(diagnostics)
            self.assertIn("ainiee.session_log.tail", joined)
            self.assertIn("API 429 rate limited", joined)


if __name__ == "__main__":
    unittest.main()
