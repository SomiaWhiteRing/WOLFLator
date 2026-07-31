import os
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import (
    QApplication,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QToolButton,
)

from formats import ARTIFACT_EPOCH
from app import (
    EditorInstallThread,
    STAGE_RESULT_LABELS,
    InstallThread,
    MainWindow,
    SettingsDialog,
    _completed_import_protection,
    _font_required_characters,
    _qt_preview_font,
    _qt_preview_family,
)
from fonts import FontCandidate
from models import (
    AppSettings,
    ImportProtectionRules,
    ImportScope,
    RunMode,
    Stage,
    StageStatus,
    TranslationItem,
)
from pipeline import PipelineStateEvent, create_project, load_manifest
from proofread import build_worker_input, load_report, make_report, proofread_paths, save_report
from settings import SettingsStore, protect_secret, unprotect_secret
from wolf_tools import dump_items, load_items


class SettingsQtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_dpapi_round_trip(self):
        encrypted = protect_secret("test-secret")
        self.assertNotIn("test-secret", encrypted)
        self.assertEqual("test-secret", unprotect_secret(encrypted))

    def test_completed_stage_can_surface_official_warnings(self):
        label = QLabel()
        MainWindow._update_stage_status(label, StageStatus.COMPLETED, "details", 16)
        self.assertEqual("已完成（16 个警告）", label.text())
        self.assertEqual("warning", label.property("state"))
        self.assertEqual("details", label.toolTip())

    def test_preview_uses_qt_registered_alias_instead_of_localized_fallback(self):
        candidate = FontCandidate(
            source="system",
            family="GenSenRounded JP R",
            aliases=("GenSenRounded JP R", "源泉圓體 R"),
            files=(),
            preview_family="GenSenRounded JP",
            style="R",
            weight=400,
        )
        with patch(
            "app.QFontDatabase.families",
            return_value=["GenSenRounded JP", "SimSun"],
        ):
            self.assertEqual(
                "GenSenRounded JP",
                _qt_preview_family(candidate, candidate.family),
            )
            self.assertEqual("R", _qt_preview_font(candidate, candidate.family).styleName())

    def test_dialog_loads_persisted_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.ini")
            item = AppSettings(
                wolf_tool_path=r"C:\Tools\Wolf.exe",
                wolf_editor_path=r"C:\Tools\Editor.exe",
                ainiee_source=r"C:\Tools\AiNiee",
            )
            store.save(item)
            dialog = SettingsDialog(store)
            self.assertEqual(item.wolf_tool_path, dialog.wolf_path.text())
            self.assertEqual(item.wolf_editor_path, dialog.editor_path.text())
            self.assertTrue(dialog.auto_convert_legacy_games.isChecked())
            dialog.auto_convert_legacy_games.setChecked(False)
            self.assertFalse(dialog._current_settings().auto_convert_legacy_games)
            self.assertEqual(item.ainiee_source, dialog.ainiee_path.text())
            self.assertIn(
                "官方下载页",
                [button.text() for button in dialog.findChildren(QPushButton)],
            )
            self.assertIn(
                "安装最新版",
                [button.text() for button in dialog.findChildren(QPushButton)],
            )
            dialog.close()

    def test_missing_glossary_settings_do_not_inherit_translation_api(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.ini")
            encrypted = protect_secret("translation-secret")
            store._settings.setValue("api_base_url", "https://translation.example/v1")
            store._settings.setValue("api_model", "translation-model")
            store._settings.setValue("api_key_blob", encrypted)
            store._settings.setValue("api_timeout", 75)
            store._settings.sync()
            item = store.load()
            self.assertEqual("", item.glossary_api_base_url)
            self.assertEqual("", item.glossary_api_model)
            self.assertEqual("", store.glossary_api_key(item))
            self.assertEqual(600, item.glossary_api_timeout)
            self.assertEqual(3, item.glossary_api_threads)
            self.assertEqual(500_000, item.glossary_chunk_chars)
            self.assertEqual(393_216, item.glossary_api_max_tokens)
            self.assertEqual("token", item.translation_chunk_mode)
            self.assertEqual(256, item.translation_token_limit)
            self.assertEqual(8, item.translation_line_limit)
            self.assertEqual(1, item.translation_retry_min_lines)
            self.assertEqual(6, item.translation_rounds)
            self.assertEqual("rules_ai", item.proofread_mode)
            self.assertEqual(20, item.proofread_batch_size)
            self.assertEqual(5, item.proofread_context_lines)
            self.assertEqual(70, item.proofread_confidence_percent)

    def test_dialog_loads_separate_api_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.ini")
            item = AppSettings(
                api_base_url="https://translate.example/v1",
                api_model="translate-model",
                api_threads=12,
                glossary_api_base_url="https://glossary.example/v1",
                glossary_api_model="glossary-model",
                glossary_api_threads=2,
                glossary_chunk_chars=456_789,
                glossary_api_max_tokens=65_535,
            )
            store.save(item)
            dialog = SettingsDialog(store)
            self.assertEqual(3, dialog.settings_tabs.count())
            self.assertEqual(
                ["工具与目录", "术语生成 API", "AiNiee 翻译 API"],
                [
                    dialog.settings_tabs.tabText(index)
                    for index in range(dialog.settings_tabs.count())
                ],
            )
            self.assertTrue(
                dialog.settings_tabs.widget(0).isAncestorOf(dialog.wolf_path)
            )
            self.assertTrue(
                dialog.settings_tabs.widget(1).isAncestorOf(dialog.glossary_api_url)
            )
            self.assertTrue(dialog.settings_tabs.widget(2).isAncestorOf(dialog.api_url))
            self.assertEqual("https://translate.example/v1", dialog.api_url.text())
            self.assertEqual("https://glossary.example/v1", dialog.glossary_api_url.text())
            self.assertEqual(12, dialog.api_threads.value())
            self.assertTrue(dialog.translation_token_mode.isChecked())
            self.assertEqual(256, dialog.translation_token_limit.value())
            self.assertEqual(8, dialog.translation_line_limit.value())
            self.assertEqual(1, dialog.translation_retry_min_lines.value())
            self.assertEqual(6, dialog.translation_rounds.value())
            self.assertEqual("", dialog.translation_token_limit.suffix())
            self.assertEqual("", dialog.translation_line_limit.suffix())
            self.assertEqual("", dialog.translation_retry_min_lines.suffix())
            self.assertEqual("Token", dialog.translation_token_unit.text())
            self.assertEqual("条", dialog.translation_line_unit.text())
            self.assertEqual("条", dialog.translation_retry_unit.text())
            self.assertEqual("轮", dialog.translation_rounds_unit.text())
            self.assertEqual(140, dialog.translation_token_limit.maximumWidth())
            self.assertEqual(90, dialog.translation_line_limit.maximumWidth())
            self.assertEqual(90, dialog.translation_retry_min_lines.maximumWidth())
            self.assertEqual(140, dialog.translation_rounds.maximumWidth())
            self.assertEqual(
                QSizePolicy.Fixed,
                dialog.translation_chunk_stack.sizePolicy().verticalPolicy(),
            )
            self.assertEqual(2, dialog.glossary_api_threads.value())
            self.assertEqual(456_789, dialog.glossary_chunk_chars.value())
            self.assertEqual(65_535, dialog.glossary_api_max_tokens.value())
            dialog.close()

    def test_install_thread_prepares_dependencies_before_reporting_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            with patch("app.install_supported_ainiee", return_value=source) as install, patch(
                "app.prepare_managed_runtime"
            ) as prepare:
                thread = InstallThread(root / "packages", root / "runtime", False)
                thread.run()
            install.assert_called_once()
            prepare.assert_called_once_with(
                source,
                root / "runtime",
                force_sync=False,
                log=thread.log_line.emit,
            )

    def test_editor_install_thread_uses_managed_package(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            editor = root / "packages" / "3.713" / "Editor.exe"
            with patch("app.install_supported_editor", return_value=editor) as install:
                thread = EditorInstallThread(root / "packages")
                thread.run()
            install.assert_called_once_with(
                root / "packages",
                progress=thread.progress_changed.emit,
                log=thread.log_line.emit,
            )

    def test_first_run_dialog_waits_until_window_can_be_shown(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.ini")
            with patch("app.SettingsStore", return_value=store), patch.object(
                MainWindow, "_open_settings"
            ) as open_settings:
                window = MainWindow()
                open_settings.assert_not_called()
                window.show()
                self.app.processEvents()
                open_settings.assert_called_once_with(first_run=True)
                window.close()

    def test_workflow_modes_are_inside_workflow_page(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.ini")
            with patch("app.SettingsStore", return_value=store), patch.object(MainWindow, "_open_settings"):
                window = MainWindow()
                self.assertTrue(window.workflow_page.isAncestorOf(window.one_click))
                self.assertTrue(window.workflow_page.isAncestorOf(window.step_mode))
                self.assertTrue(window.workflow_page.isAncestorOf(window.log_view))
                self.assertEqual(6, window.tabs.count())
                self.assertEqual("校对", window.tabs.tabText(2))
                self.assertEqual("编辑", window.tabs.tabText(3))
                self.assertEqual("范围", window.tabs.tabText(4))
                self.assertEqual("修改字体", window.tabs.tabText(5))
                self.assertTrue(
                    any(
                        label.text() == "原字体"
                        for label in window.tabs.widget(5).findChildren(QLabel)
                    )
                )
                self.assertEqual(3, window.scope_stack.count())
                self.assertTrue(window.tabs.widget(4).isAncestorOf(window.translation_scope_button))
                self.assertTrue(window.tabs.widget(4).isAncestorOf(window.import_scope_button))
                self.assertTrue(window.tabs.widget(4).isAncestorOf(window.export_scope_button))
                self.assertTrue(window.translation_scope_checks["external"].isChecked())
                self.assertTrue(window.import_scope_checks["external"].isChecked())
                self.assertTrue(
                    window.import_scope_column.isAncestorOf(
                        window.import_scope_checks["filename"]
                    )
                )
                self.assertTrue(
                    window.import_protection_column.isAncestorOf(
                        window.protect_external_references
                    )
                )
                self.assertFalse(window.external_filter_options.isHidden())
                self.assertTrue(window.exclude_large_external_files.isChecked())
                self.assertEqual(128, window.external_file_limit_kb.value())
                self.assertEqual("", window.external_file_limit_kb.suffix())
                self.assertTrue(
                    any(
                        label.text() == "KB 的文件"
                        for label in window.external_filter_options.findChildren(QLabel)
                    )
                )
                window.exclude_large_external_files.setChecked(False)
                self.assertFalse(window.external_file_limit_kb.isEnabled())
                window.export_scope_checks["external"].setChecked(False)
                self.assertTrue(window.external_filter_options.isHidden())
                self.assertTrue(window.protect_logic_references.isChecked())
                self.assertTrue(window.protect_external_references.isChecked())
                self.assertTrue(window.protect_paths_and_commands.isChecked())
                self.assertTrue(window.allow_copy_condition_groups.isChecked())
                self.assertEqual("block", window.logic_unknown_policy.currentData())
                window.logic_unknown_policy.setCurrentIndex(
                    window.logic_unknown_policy.findData("warn")
                )
                self.assertEqual("warn", window.logic_unknown_policy.currentData())
                window.protect_logic_references.setChecked(False)
                self.assertFalse(window.logic_unknown_policy.isEnabled())
                window.protect_logic_references.setChecked(True)
                self.assertEqual("warn", window.suspicious_identifier_action.currentData())
                self.assertEqual(4, window.import_protection_table.columnCount())
                self.assertEqual(8, len(window.step_buttons))
                self.assertEqual(8, len(window.step_result_buttons))
                self.assertTrue(all(button.text() == "执行" for button in window.step_buttons.values()))
                self.assertEqual(
                    [STAGE_RESULT_LABELS[stage] for stage in Stage],
                    [window.step_result_buttons[stage].text() for stage in Stage],
                )
                self.assertTrue(
                    all(
                        window.step_buttons[stage].width()
                        == window.step_result_buttons[stage].width()
                        for stage in Stage
                    )
                )
                window._append_log("[WARNING] 字体缺字：主字体")
                warning_block = window.log_view.document().lastBlock().previous()
                self.assertEqual("警告  字体缺字：主字体", warning_block.text())
                self.assertEqual(
                    "#a24625",
                    warning_block.begin().fragment().charFormat().foreground().color().name(),
                )
                window._append_log("[ERROR] 发布失败")
                error_block = window.log_view.document().lastBlock().previous()
                self.assertEqual("错误  发布失败", error_block.text())
                self.assertEqual(
                    "#b42318",
                    error_block.begin().fragment().charFormat().foreground().color().name(),
                )
                candidate = FontCandidate(
                    source="bundled",
                    family="测试字体",
                    aliases=("测试字体",),
                    files=(),
                    missing=frozenset({"∟"}),
                )
                window.font_context = {
                    "required": {"∟"},
                    "candidates": [candidate],
                    "original_slots": ["测试字体"] * 4,
                }
                for combo in window.font_combos:
                    combo.addItem(candidate.label, candidate)
                window._update_font_rows()
                self.assertEqual('缺少 1 字："∟"', window.font_coverage_labels[0].text())
                self.assertEqual('缺少字符：\n"∟"', window.font_coverage_labels[0].toolTip())
                self.assertNotIn("U+", window.font_coverage_labels[0].text())
                window.step_mode.click()
                self.assertEqual(1, window.workflow_stack.currentIndex())
                window.close()

    def test_font_scan_is_lazy_until_font_tab_is_opened(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.ini")
            with patch("app.SettingsStore", return_value=store), patch.object(
                MainWindow, "_open_settings"
            ), patch.object(MainWindow, "_refresh_font_tab") as refresh:
                window = MainWindow()
                refresh.assert_not_called()
                window.tabs.setCurrentIndex(window.font_tab_index)
                self.app.processEvents()
                refresh.assert_called_once_with()
                window.close()

    def test_completed_import_protection_is_reused_for_font_coverage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = root / "game"
            (game / "Data" / "BasicData").mkdir(parents=True)
            (game / "Game.exe").write_bytes(b"game")
            (game / "Data" / "BasicData" / "Game.dat").write_bytes(b"data")
            manifest = load_manifest(create_project(root / "projects", game))
            items = root / "items.json"
            report = root / "import-protection.json"
            items.write_text("{}", encoding="utf-8")
            report.write_text(
                json.dumps(
                    {
                        "kind": "import-protection",
                        "epoch": ARTIFACT_EPOCH,
                        "protected_keys": ["key"],
                        "safe_to_translate": [],
                        "keep_original": [],
                        "translation_overrides": {},
                        "approvals": {},
                        "unresolved_scopes": [],
                        "translated_replay": {},
                        "structural_diff": {},
                        "entries": [],
                        "summary": {},
                        "middle_dot_normalized": [],
                    }
                ),
                encoding="utf-8",
            )
            record = manifest.version.stage(Stage.IMPORT)
            record.status = StageStatus.COMPLETED
            record.artifacts["import_protection"] = str(report)
            self.assertEqual(
                ["key"],
                _completed_import_protection(manifest, items)["protected_keys"],
            )

    def test_pending_import_font_coverage_contains_translations_only(self):
        manifest = SimpleNamespace(
            import_scope=ImportScope(),
            import_protection=ImportProtectionRules(),
        )
        items = [
            TranslationItem(
                key="line",
                code="COMMON-1-1-0",
                original="原文甲",
                translation="译文乙",
            )
        ]
        required, exact = _font_required_characters(manifest, items, None)
        self.assertFalse(exact)
        self.assertTrue(set("译文乙") <= required)
        self.assertFalse(set("原文甲") <= required)

    def test_incompatible_project_manifest_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = root / "game"
            (game / "Data" / "BasicData").mkdir(parents=True)
            (game / "Game.exe").write_bytes(b"game")
            (game / "Data" / "BasicData" / "Game.dat").write_bytes(b"data")
            manifest_path = create_project(root / "projects", game)
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            data.pop("schema")
            manifest_path.write_text(json.dumps(data), encoding="utf-8")
            store = SettingsStore(root / "settings.ini")
            store.save(
                AppSettings(
                    projects_root=str(root / "projects"),
                    last_project=str(manifest_path),
                )
            )
            with patch("app.SettingsStore", return_value=store), patch.object(MainWindow, "_open_settings"):
                window = MainWindow()
                self.assertIn("已拒绝 1 个不兼容的项目清单", window.status_label.text())
                self.assertIn("项目格式不兼容", window.status_label.toolTip())
                window.close()

    def test_step_mode_progress_and_running_ui_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = root / "game"
            (game / "Data" / "BasicData").mkdir(parents=True)
            (game / "Game.exe").write_bytes(b"game")
            (game / "Data" / "BasicData" / "Game.dat").write_bytes(b"data")
            projects = root / "projects"
            manifest_path = create_project(projects, game)
            manifest = load_manifest(manifest_path)
            manifest.run_mode = RunMode.STEP
            manifest.version.stage(Stage.COPY).status = StageStatus.COMPLETED
            manifest.version.stage(Stage.EXTRACT).status = StageStatus.FAILED
            manifest.version.stage(Stage.EXTRACT).error = "test error"
            Path(manifest_path).write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
            store = SettingsStore(root / "settings.ini")
            store.save(AppSettings(projects_root=str(projects), last_project=str(manifest_path)))
            with patch("app.SettingsStore", return_value=store), patch.object(MainWindow, "_open_settings"):
                window = MainWindow()
                self.assertEqual(1, window.progress.maximum())
                self.assertTrue(window.step_buttons[Stage.COPY].isEnabled())
                self.assertTrue(window.retry_button.isEnabled())
                self.assertEqual(8, len(window.step_checks))
                self.assertFalse(window.run_range_button.isEnabled())
                window.step_checks[Stage.UNPACK].setChecked(True)
                window.step_checks[Stage.EXTRACT].setChecked(True)
                self.assertEqual(
                    (Stage.UNPACK, Stage.EXTRACT), window._selected_step_range()
                )
                self.assertTrue(window.run_range_button.isEnabled())
                self.assertEqual("已选择：解包 至 导出文本", window.step_range_summary.text())
                with patch.object(window, "_start") as start:
                    window.run_range_button.click()
                    start.assert_called_once_with(stages=(Stage.UNPACK, Stage.EXTRACT))
                window.step_checks[Stage.RELEASE].setChecked(True)
                self.assertIsNone(window._selected_step_range())
                self.assertFalse(window.run_range_button.isEnabled())
                window.step_checks[Stage.RELEASE].setChecked(False)
                window._set_pipeline_ui_locked(True)
                self.assertFalse(window.settings_button.isEnabled())
                self.assertFalse(window.project_combo.isEnabled())
                self.assertFalse(window.new_project_button.isEnabled())
                self.assertFalse(window.add_version_button.isEnabled())
                self.assertFalse(window.one_click.isEnabled())
                self.assertFalse(window.run_range_button.isEnabled())
                self.assertTrue(all(not check.isEnabled() for check in window.step_checks.values()))
                self.assertFalse(window.open_release_button.isEnabled())
                self.assertTrue(window.stop_button.isEnabled())
                self.assertTrue(all(not window.tabs.isTabEnabled(index) for index in range(1, 6)))

                with patch.object(window, "_load_project_view") as reload_view:
                    window._stage_progress(1, 8, Stage.COPY.value)
                    reload_view.assert_not_called()
                window._stage_state(
                    PipelineStateEvent(Stage.COPY, StageStatus.COMPLETED, 1, 8, "已完成")
                )
                self.assertEqual("已完成", window.easy_stage_status[Stage.COPY].text())
                window._set_pipeline_ui_locked(False)
                window.close()

    def test_proofread_tab_gate_tracks_translation_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = root / "game"
            (game / "Data" / "BasicData").mkdir(parents=True)
            (game / "Game.exe").write_bytes(b"game")
            (game / "Data" / "BasicData" / "Game.dat").write_bytes(b"data")
            projects = root / "projects"
            manifest_path = create_project(projects, game)
            items_path = dump_items(
                root / "items-translated.json",
                [TranslationItem(key="one", original="原文", translation="译文")],
            )
            manifest = load_manifest(manifest_path)
            manifest.version.stage(Stage.TRANSLATE).artifacts["items"] = str(items_path)
            manifest_path.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
            store = SettingsStore(root / "settings.ini")
            store.save(AppSettings(projects_root=str(projects), last_project=str(manifest_path)))
            with patch("app.SettingsStore", return_value=store), patch.object(MainWindow, "_open_settings"):
                window = MainWindow()
                self.assertEqual("校对", window.tabs.tabText(window.proofread_tab_index))
                for status in (
                    StageStatus.PENDING,
                    StageStatus.RUNNING,
                    StageStatus.FAILED,
                    StageStatus.CANCELLED,
                    StageStatus.COMPLETED,
                ):
                    manifest = load_manifest(manifest_path)
                    manifest.version.stage(Stage.TRANSLATE).status = status
                    manifest_path.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
                    window._load_project_view()
                    self.assertTrue(window.tabs.isTabEnabled(window.proofread_tab_index))
                    self.assertEqual(status is StageStatus.COMPLETED, window.proofread_content.isEnabled())
                    self.assertEqual(status is StageStatus.COMPLETED, window.proofread_gate_label.isHidden())
                window.close()

    def test_edit_tab_edits_normal_translation_and_validate_result_redirects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = root / "game"
            (game / "Data" / "BasicData").mkdir(parents=True)
            (game / "Game.exe").write_bytes(b"game")
            (game / "Data" / "BasicData" / "Game.dat").write_bytes(b"data")
            projects = root / "projects"
            manifest_path = create_project(projects, game)
            items_path = dump_items(
                root / "items-translated.json",
                [
                    TranslationItem(
                        key="one", code="C-1", original="原文一", translation="ABC译文一"
                    ),
                    TranslationItem(
                        key="two",
                        code="C-2",
                        original=r"\C[1]原文二",
                        translation=r"\C[1]译文二",
                        control_signature=[r"\C[1]"],
                    ),
                ],
            )
            manifest = load_manifest(manifest_path)
            translate = manifest.version.stage(Stage.TRANSLATE)
            translate.status = StageStatus.COMPLETED
            translate.artifacts["items"] = str(items_path)
            manifest_path.write_text(
                json.dumps(manifest.to_dict(), ensure_ascii=False),
                encoding="utf-8",
            )
            store = SettingsStore(root / "settings.ini")
            store.save(AppSettings(projects_root=str(projects), last_project=str(manifest_path)))
            with patch("app.SettingsStore", return_value=store), patch.object(MainWindow, "_open_settings"):
                window = MainWindow()
                window.show()
                self.app.processEvents()
                self.assertEqual("编辑译文", window.step_result_buttons[Stage.VALIDATE].text())
                self.assertTrue(window.step_result_buttons[Stage.VALIDATE].isEnabled())
                window._open_stage_result(Stage.VALIDATE)
                self.assertEqual(window.edit_tab_index, window.tabs.currentIndex())
                self.assertEqual(2, window.edit_model.rowCount())
                header = window.edit_table.horizontalHeader()
                self.assertTrue(
                    all(
                        header.sectionResizeMode(column) == QHeaderView.Stretch
                        for column in range(4)
                    )
                )
                self.assertEqual("▼", window.edit_replace_toggle.text())
                self.assertEqual(window.edit_search.height(), window.edit_replace_toggle.height())
                window.edit_replace_toggle.setChecked(True)
                self.app.processEvents()
                self.assertEqual("▲", window.edit_replace_toggle.text())
                self.assertFalse(window.edit_replace_popup.isHidden())
                self.assertEqual(window.edit_replace.height(), window.edit_replace_one.height())
                self.assertEqual(window.edit_replace.height(), window.edit_replace_all.height())
                self.assertEqual(
                    window.edit_search.mapTo(window.edit_content, QPoint()).x(),
                    window.edit_replace.mapTo(window.edit_content, QPoint()).x(),
                )
                for line_edit in (window.edit_search, window.edit_replace):
                    clear_button = line_edit.findChild(QToolButton)
                    self.assertIsNotNone(clear_button)
                    self.assertLessEqual(
                        abs(
                            clear_button.geometry().center().y()
                            - line_edit.rect().center().y()
                        ),
                        1,
                    )
                window.edit_table.setFocus()
                self.app.processEvents()
                self.assertTrue(window.edit_replace_toggle.isChecked())
                self.assertFalse(window.edit_replace_popup.isHidden())
                window.edit_replace_toggle.setChecked(False)
                self.assertEqual("▼", window.edit_replace_toggle.text())

                window.edit_search.setText("原文二")
                self.app.processEvents()
                self.assertEqual(1, window.edit_proxy.rowCount())
                window.edit_table.selectRow(0)
                self.app.processEvents()
                self.assertEqual(r"\C[1]原文二", window.edit_original.toPlainText())
                self.assertFalse(window.edit_replace_one.isEnabled())
                self.assertFalse(window.edit_replace_all.isEnabled())

                window.edit_search.setText("C")
                window.edit_replace.setText("X")
                self.app.processEvents()
                with patch("app.QMessageBox.warning") as warning:
                    window._replace_all_translations()
                warning.assert_called_once()
                self.assertFalse(window.edit_model.edits)

                window.edit_search.setText("译文")
                window.edit_replace.setText("润色")
                self.app.processEvents()
                window.edit_table.selectRow(0)
                self.app.processEvents()
                window._replace_current_translation()
                with patch("app.QMessageBox.question", return_value=QMessageBox.No) as question:
                    window._replace_all_translations()
                question.assert_called_once()
                self.assertNotIn("two", window.edit_model.edits)
                with patch("app.QMessageBox.question", return_value=QMessageBox.Yes):
                    window._replace_all_translations()
                self.assertEqual("ABC润色一", window.edit_model.edits["one"])
                self.assertEqual(r"\C[1]润色二", window.edit_model.edits["two"])
                self.assertEqual("原文一", window.edit_model.items[0].original)
                self.assertEqual(r"\C[1]原文二", window.edit_model.items[1].original)
                self.assertTrue(window.edit_save.isEnabled())
                window._save_translation_edits()

                manifest = load_manifest(manifest_path)
                output = Path(manifest.version.stage(Stage.TRANSLATE).artifacts["items"])
                self.assertEqual("items-edited.json", output.name)
                saved = load_items(output)
                self.assertEqual("ABC润色一", saved[0].translation)
                self.assertEqual(r"\C[1]润色二", saved[1].translation)
                self.assertFalse(window.edit_model.edits)
                window.close()

    def test_proofread_review_saves_edits_and_individual_and_batch_decisions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            game = root / "game"
            (game / "Data" / "BasicData").mkdir(parents=True)
            (game / "Game.exe").write_bytes(b"game")
            (game / "Data" / "BasicData" / "Game.dat").write_bytes(b"data")
            projects = root / "projects"
            manifest_path = create_project(projects, game)
            items_path = dump_items(
                root / "items-translated.json",
                [
                    TranslationItem(key="one", code="C-1", original="原文一", translation="译文一"),
                    TranslationItem(key="two", code="C-2", original="原文二", translation="译文二"),
                ],
            )
            manifest = load_manifest(manifest_path)
            translate = manifest.version.stage(Stage.TRANSLATE)
            translate.status = StageStatus.COMPLETED
            translate.artifacts["items"] = str(items_path)
            manifest_path.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
            worker_input = build_worker_input(items_path, manifest, context_lines=0)
            issue = {
                "source": "ai", "type": "logic_error", "severity": "medium",
                "description": "测试问题", "suggestion": "", "confidence": 0.9,
            }
            report = make_report(
                worker_input,
                {
                    "kind": "proofread-worker-output",
                    "epoch": ARTIFACT_EPOCH,
                    "entries": {
                        "one": {"issues": [issue], "suggested_translation": "建议一"},
                        "two": {"issues": [issue], "suggested_translation": "建议二"},
                    },
                    "failed_batches": [],
                },
                mode="rules_ai",
                model="model",
                batch_size=20,
                context_lines=0,
                confidence_percent=70,
            )
            report_path, _ = proofread_paths(manifest_path, manifest)
            save_report(report_path, report)
            store = SettingsStore(root / "settings.ini")
            store.save(AppSettings(projects_root=str(projects), last_project=str(manifest_path)))
            with patch("app.SettingsStore", return_value=store), patch.object(MainWindow, "_open_settings"):
                window = MainWindow()
                self.assertEqual(2, window.proofread_table.rowCount())
                window.proofread_table.selectRow(0)
                window.proofread_suggestion.setPlainText("人工修订")
                self.app.processEvents()
                window._decide_current_proofread("accept")
                saved = load_report(report_path)
                self.assertEqual("人工修订", saved["entries"][0]["edited_translation"])
                self.assertEqual("accept", saved["entries"][0]["decision"])
                for row in range(window.proofread_table.rowCount()):
                    window.proofread_table.item(row, 0).setCheckState(Qt.Checked)
                window._decide_selected_proofread("keep")
                saved = load_report(report_path)
                self.assertEqual(["keep", "keep"], [entry["decision"] for entry in saved["entries"]])
                window.tabs.setCurrentIndex(window.proofread_tab_index)
                window._set_proofread_ui_locked(True)
                self.assertEqual(window.proofread_tab_index, window.tabs.currentIndex())
                self.assertTrue(window.tabs.isTabEnabled(window.proofread_tab_index))
                self.assertTrue(window.stop_proofread_button.isEnabled())
                self.assertFalse(window.proofread_mode.isEnabled())
                self.assertFalse(window.project_combo.isEnabled())
                self.assertFalse(window.start_button.isEnabled())
                window._set_proofread_ui_locked(False)
                window.close()


if __name__ == "__main__":
    unittest.main()
