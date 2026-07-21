import http.server
import io
import json
import os
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
    _official_config_text,
    apply_managed_translations,
    classify_optional_name_delta,
    merge_ainiee_output,
    name_baseline_scope,
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

    def test_official_config_exports_all_and_fonts_use_managed_defaults(self):
        config = _official_config_text()
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
            workbook.save(full_path)

            baseline_path = root / "baseline.xlsx"
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(HEADERS)
            sheet.append(["UDB-1-0-0", "", "Status", "Label", "", "攻撃力", ""])
            sheet.append(["DISPLAY-1", "", "Event", "Message", "", "顔", ""])
            sheet.append(["FILE-1", "<FILENAME>\nCOPY-FROM-DISPLAY-1", "Image", "File", "", "顔", ""])
            workbook.save(baseline_path)

            items = read_translation_items(full_path)
            baseline_items = read_translation_items(baseline_path)
            self.assertEqual(2, classify_optional_name_delta(items, baseline_items))
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
            with mock.patch.object(ainiee, "run_process", return_value=fake_result) as run, mock.patch.object(
                ainiee, "locate_uv", return_value=Path(sys.executable)
            ):
                with self.assertRaisesRegex(RuntimeError, "没有生成"):
                    ainiee.run_translation(
                        runtime, input_path, root / "output", dict(ainiee.RULE_DEFAULTS), "project", settings, "secret"
                    )
            self.assertEqual(
                ["run", "--frozen", "--no-sync", "ainiee_cli.py"],
                run.call_args.args[0][1:5],
            )
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
