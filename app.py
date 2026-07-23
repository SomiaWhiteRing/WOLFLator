from __future__ import annotations

import json
import os
import subprocess
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import (
    QColor,
    QCloseEvent,
    QDesktopServices,
    QFont,
    QFontDatabase,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QSpinBox,
    QStackedWidget,
    QStyle,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ainiee import (
    AINIEE_VERSION,
    install_supported_ainiee,
    locate_ainiee_source,
    prepare_managed_runtime,
    remove_managed_ainiee,
    test_api,
)
from fonts import (
    FONT_SLOT_NAMES,
    FontCandidate,
    candidate_for_family,
    coverage_fingerprint,
    discover_font_candidates,
    font_file_info,
    load_font_scheme,
    load_original_fonts,
    materialize_candidate,
    record_original_fonts,
    required_characters,
    resolve_scheme_files,
)
from models import (
    DEFAULT_EXTERNAL_FILE_LIMIT_KB,
    MAX_EXTERNAL_FILE_LIMIT_KB,
    ImportProtectionRules,
    ImportScope,
    RunMode,
    STAGE_ORDER,
    Stage,
    StageStatus,
    default_export_scope,
)
from pipeline import Pipeline, PipelineStateEvent, add_version, create_project, load_manifest
from safe_io import project_lock
from settings import SettingsStore, local_data_dir, validate_settings
from wolf_editor import (
    EDITOR_DOWNLOAD_URL,
    inspect_wolf_editor,
    install_supported_editor,
)
from wolf_tools import (
    analyze_import_protection,
    final_display_texts,
    load_items,
    read_font_slots,
)


STAGE_LABELS = {
    Stage.COPY: "复制游戏",
    Stage.UNPACK: "解包",
    Stage.EXTRACT: "导出文本",
    Stage.GLOSSARY: "生成术语",
    Stage.TRANSLATE: "AI 翻译",
    Stage.VALIDATE: "校验译文",
    Stage.IMPORT: "导入游戏",
    Stage.RELEASE: "发布",
}
STATUS_LABELS = {
    StageStatus.PENDING: "待完成",
    StageStatus.RUNNING: "待完成",
    StageStatus.COMPLETED: "已完成",
    StageStatus.FAILED: "出现错误",
    StageStatus.CANCELLED: "出现错误",
}
STAGE_DESCRIPTIONS = {
    Stage.COPY: "建立源副本与工作副本",
    Stage.UNPACK: "使用 UberWolf 准备松散 Data",
    Stage.EXTRACT: "导出 XLSX 并分析全部事件",
    Stage.GLOSSARY: "从完整语料生成角色与术语",
    Stage.TRANSLATE: "调用 AiNiee 翻译文本",
    Stage.VALIDATE: "校验键、译文与控制符",
    Stage.IMPORT: "按选定范围回填游戏",
    Stage.RELEASE: "生成可直接运行的发布目录",
}
STAGE_RESULT_LABELS = {
    Stage.COPY: "工作副本",
    Stage.UNPACK: "Data目录",
    Stage.EXTRACT: "导出表格",
    Stage.GLOSSARY: "查看术语",
    Stage.TRANSLATE: "翻译结果",
    Stage.VALIDATE: "译文表格",
    Stage.IMPORT: "导入结果",
    Stage.RELEASE: "启动游戏",
}
STAGE_RESULT_ARTIFACTS = {
    Stage.COPY: "work",
    Stage.UNPACK: "data",
    Stage.EXTRACT: "workbook",
    Stage.GLOSSARY: "glossary",
    Stage.TRANSLATE: "ainiee_output",
    Stage.VALIDATE: "full_workbook",
    Stage.IMPORT: "translated_game",
    Stage.RELEASE: "release",
}
IMPORT_PROTECTION_ACTION_LABELS = {
    "keep_original": "保留原文",
    "warn": "仅警告",
    "atomic_translate": "整体翻译",
}
IMPORT_PROTECTION_REASON_LABELS = {
    "external_reference": "外部脚本引用",
    "path_or_command": "路径或脚本命令",
    "logic_condition": "WOLF 条件字面量",
    "logic_value_change": "WOLF 条件真值变化",
    "logic_untracked": "WOLF 条件来源未追踪",
    "logic_blocking": "WOLF 条件来源阻断",
    "suspicious_identifier": "可疑标识符",
    "copy_mixed_scope_group": "COPY-FROM 条件/混合范围组",
}


def _load_editor_analysis(manifest) -> dict[str, object] | None:
    path = manifest.version.stage(Stage.EXTRACT).artifacts.get("editor_analysis", "")
    if not path or not Path(path).is_file():
        return None
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Editor 分析报告根节点不是对象。")
    return value


class PipelineThread(QThread):
    log_line = Signal(str)
    stage_progress = Signal(int, int, str)
    stage_state = Signal(object)
    result_ready = Signal(str)
    failed = Signal(str)

    def __init__(self, pipeline: Pipeline, stage: Stage | None = None):
        super().__init__()
        self.pipeline = pipeline
        self.stage = stage
        self.pipeline.set_log_sink(self.log_line.emit)
        self.pipeline.progress = lambda current, total, stage: self.stage_progress.emit(current, total, stage.value)
        self.pipeline.state = self.stage_state.emit

    def run(self) -> None:
        try:
            result = self.pipeline.run_stage(self.stage) if self.stage is not None else self.pipeline.run()
            self.result_ready.emit(result)
        except Exception:
            detail = traceback.format_exc()
            self.pipeline.detail("pipeline.thread.exception\n" + detail)
            self.failed.emit(detail)


class InstallThread(QThread):
    progress_changed = Signal(int, int)
    log_line = Signal(str)
    installed = Signal(str)
    failed = Signal(str)

    def __init__(self, packages_root: Path, runtime_root: Path, repair: bool, source: str = ""):
        super().__init__()
        self.packages_root = packages_root
        self.runtime_root = runtime_root
        self.repair = repair
        self.source = source

    def run(self) -> None:
        try:
            if self.source:
                path = locate_ainiee_source(self.source)
            else:
                path = install_supported_ainiee(
                    self.packages_root,
                    repair=self.repair,
                    progress=self.progress_changed.emit,
                    log=self.log_line.emit,
                )
            self.log_line.emit("正在安装 AiNiee 依赖，首次准备可能需要较长时间...")
            prepare_managed_runtime(
                path,
                self.runtime_root,
                force_sync=self.repair,
                log=self.log_line.emit,
            )
            self.installed.emit(str(path))
        except Exception:
            self.failed.emit(traceback.format_exc())


class EditorInstallThread(QThread):
    progress_changed = Signal(int, int)
    log_line = Signal(str)
    installed = Signal(str)
    failed = Signal(str)

    def __init__(self, packages_root: Path):
        super().__init__()
        self.packages_root = packages_root

    def run(self) -> None:
        try:
            path = install_supported_editor(
                self.packages_root,
                progress=self.progress_changed.emit,
                log=self.log_line.emit,
            )
            self.installed.emit(str(path))
        except Exception:
            self.failed.emit(traceback.format_exc())


class ApiTestThread(QThread):
    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(self, settings, api_key: str, *, glossary: bool = False):
        super().__init__()
        self.settings = settings
        self.api_key = api_key
        self.glossary = glossary

    def run(self) -> None:
        try:
            self.succeeded.emit(test_api(self.settings, self.api_key, glossary=self.glossary))
        except Exception as exc:
            self.failed.emit(str(exc))


class FontScanThread(QThread):
    succeeded = Signal(object)
    failed = Signal(str, str)

    def __init__(self, manifest_path: Path):
        super().__init__()
        self.manifest_path = manifest_path

    def run(self) -> None:
        try:
            manifest = load_manifest(self.manifest_path)
            if self.isInterruptionRequested():
                return
            record = manifest.version.stage(Stage.VALIDATE)
            items_path = record.artifacts.get("items", "")
            if record.status is not StageStatus.COMPLETED or not items_path or not Path(items_path).is_file():
                raise RuntimeError("完成“校验译文”后才能检查和修改字体。")
            items = load_items(items_path)
            if self.isInterruptionRequested():
                return
            extract = manifest.version.stage(Stage.EXTRACT).artifacts
            original_record = load_original_fonts(
                self.manifest_path.parent, manifest.active_version
            )
            if original_record is None:
                source_items = load_items(extract["items"])
                record_original_fonts(
                    self.manifest_path.parent,
                    manifest.active_version,
                    read_font_slots(source_items),
                    manifest.version.source_hash,
                    extract["workbook"],
                )
                original_record = load_original_fonts(
                    self.manifest_path.parent, manifest.active_version
                )
            if self.isInterruptionRequested():
                return
            if original_record is None:
                raise RuntimeError("无法建立当前版本的原字体记录。")
            original_slots = list(original_record["slots"])
            version_dir = self.manifest_path.parent / "versions" / manifest.active_version
            game_root = version_dir / "work"
            if not game_root.is_dir():
                game_root = version_dir / "source"
            if not game_root.is_dir():
                game_root = Path(manifest.version.original_path)
            protection = analyze_import_protection(
                items,
                manifest.import_scope,
                game_root,
                manifest.import_protection,
                _load_editor_analysis(manifest),
            )
            required = required_characters(
                final_display_texts(
                    items,
                    manifest.import_scope,
                    allow_copy_condition_groups=(
                        manifest.import_protection.allow_copy_condition_groups
                    ),
                    protected_keys=set(protection["protected_keys"]),
                )
            )
            if self.isInterruptionRequested():
                return
            candidates = discover_font_candidates(
                game_root,
                required,
                cancelled=self.isInterruptionRequested,
            )
            if self.isInterruptionRequested():
                return
            scheme = load_font_scheme(self.manifest_path.parent)
            if scheme is not None:
                resolved = resolve_scheme_files(self.manifest_path.parent, scheme)
                for slot, files in zip(scheme["slots"], resolved, strict=True):
                    if self.isInterruptionRequested():
                        return
                    if slot["mode"] != "font":
                        continue
                    if any(
                        candidate.source == slot["provenance"]
                        and candidate.family.casefold() == str(slot["family"]).casefold()
                        for candidate in candidates
                    ):
                        continue
                    coverage: set[int] = set()
                    aliases: set[str] = {str(slot["family"])}
                    for path in files:
                        if self.isInterruptionRequested():
                            return
                        families, codepoints = font_file_info(path)
                        aliases.update(families)
                        coverage.update(codepoints)
                    candidates.append(
                        FontCandidate(
                            source=str(slot["provenance"]),
                            family=str(slot["family"]),
                            aliases=tuple(sorted(aliases, key=str.casefold)),
                            files=tuple(files),
                            missing=frozenset(
                                character for character in required if ord(character) not in coverage
                            ),
                        )
                    )
            release_record = manifest.version.stage(Stage.RELEASE)
            self.succeeded.emit(
                {
                    "manifest": str(self.manifest_path),
                    "scheme": scheme,
                    "original_slots": original_slots,
                    "required": required,
                    "candidates": candidates,
                    "release_status": release_record.status.value,
                    "font_warning_count": release_record.artifacts.get("font_warning_count", "0"),
                    "font_warnings": release_record.artifacts.get("font_warnings", ""),
                }
            )
        except InterruptedError:
            return
        except Exception as exc:
            self.failed.emit(str(self.manifest_path), str(exc))


def _path_row(line_edit: QLineEdit, button_text: str, callback) -> QWidget:
    widget = QWidget()
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(8)
    layout.addWidget(line_edit, 1)
    button = QPushButton(button_text)
    button.clicked.connect(callback)
    layout.addWidget(button)
    return widget


class SettingsDialog(QDialog):
    def __init__(self, store: SettingsStore, parent=None):
        super().__init__(parent)
        self.store = store
        self.settings = store.load()
        self.install_thread: InstallThread | None = None
        self.editor_install_thread: EditorInstallThread | None = None
        self.api_thread: ApiTestThread | None = None
        self.api_test_target = "translation"
        self.setWindowTitle("WOLFLator 设置")
        self.setMinimumWidth(720)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 20)
        layout.setSpacing(16)

        title = QLabel("设置")
        title.setObjectName("dialogTitle")
        layout.addWidget(title)

        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(12)
        self.wolf_path = QLineEdit(self.settings.wolf_tool_path)
        form.addRow("官方翻译工具", _path_row(self.wolf_path, "选择 EXE", self._choose_wolf))

        self.editor_path = QLineEdit(self.settings.wolf_editor_path)
        editor_widget = QWidget()
        editor_layout = QHBoxLayout(editor_widget)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(8)
        editor_layout.addWidget(self.editor_path, 1)
        self.select_editor_button = QPushButton("选择 Editor.exe")
        self.select_editor_button.clicked.connect(self._choose_editor)
        self.editor_install_button = QPushButton("安装最新版")
        self.editor_install_button.clicked.connect(self._install_editor)
        download_editor = QPushButton("官方下载页")
        download_editor.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(EDITOR_DOWNLOAD_URL))
        )
        editor_layout.addWidget(self.select_editor_button)
        editor_layout.addWidget(self.editor_install_button)
        editor_layout.addWidget(download_editor)
        form.addRow("WOLF RPG Editor", editor_widget)
        self.editor_status = QLabel("")
        self.editor_status.setObjectName("secondaryText")
        form.addRow("", self.editor_status)
        self.editor_path.editingFinished.connect(self._probe_editor)
        self._probe_editor()

        self.ainiee_path = QLineEdit(self.settings.ainiee_source)
        ainiee_widget = QWidget()
        ainiee_layout = QHBoxLayout(ainiee_widget)
        ainiee_layout.setContentsMargins(0, 0, 0, 0)
        ainiee_layout.setSpacing(8)
        ainiee_layout.addWidget(self.ainiee_path, 1)
        self.select_ainiee_button = QPushButton("选择目录")
        self.select_ainiee_button.clicked.connect(self._choose_ainiee)
        self.install_button = QPushButton(f"安装 {AINIEE_VERSION}")
        self.install_button.clicked.connect(lambda: self._install_ainiee(False))
        ainiee_layout.addWidget(self.select_ainiee_button)
        ainiee_layout.addWidget(self.install_button)
        form.addRow("AiNiee-Next", ainiee_widget)

        management = QWidget()
        manage_layout = QHBoxLayout(management)
        manage_layout.setContentsMargins(0, 0, 0, 0)
        manage_layout.setSpacing(8)
        self.repair_button = QPushButton("修复")
        self.repair_button.clicked.connect(lambda: self._install_ainiee(True))
        open_button = QPushButton("打开目录")
        open_button.clicked.connect(self._open_ainiee)
        remove = QPushButton("移除托管版本")
        remove.clicked.connect(self._remove_ainiee)
        manage_layout.addWidget(self.repair_button)
        manage_layout.addWidget(open_button)
        manage_layout.addWidget(remove)
        manage_layout.addStretch(1)
        form.addRow("版本管理", management)

        self.projects_root = QLineEdit(self.settings.projects_root)
        form.addRow("项目目录", _path_row(self.projects_root, "选择", self._choose_projects_root))
        self.ascii_dir = QLineEdit(self.settings.ascii_runner_dir)
        form.addRow("ASCII 执行目录", _path_row(self.ascii_dir, "选择", self._choose_ascii_dir))
        layout.addLayout(form)

        self.api_tabs = QTabWidget()
        glossary_page = QWidget()
        glossary_form = QFormLayout(glossary_page)
        glossary_form.setHorizontalSpacing(18)
        glossary_form.setVerticalSpacing(10)
        self.glossary_api_url = QLineEdit(self.settings.glossary_api_base_url)
        self.glossary_api_url.setPlaceholderText("https://example.com/v1")
        self.glossary_api_model = QLineEdit(self.settings.glossary_api_model)
        self.glossary_api_key = QLineEdit()
        self.glossary_api_key.setEchoMode(QLineEdit.Password)
        try:
            self.glossary_api_key.setText(self.store.glossary_api_key(self.settings))
        except Exception:
            pass
        glossary_form.addRow("API 基础地址", self.glossary_api_url)
        glossary_form.addRow("模型", self.glossary_api_model)
        glossary_form.addRow("API 密钥", self.glossary_api_key)

        glossary_limits = QWidget()
        glossary_limits_layout = QHBoxLayout(glossary_limits)
        glossary_limits_layout.setContentsMargins(0, 0, 0, 0)
        self.glossary_api_threads = QSpinBox()
        self.glossary_api_threads.setRange(1, 100)
        self.glossary_api_threads.setValue(self.settings.glossary_api_threads)
        self.glossary_api_timeout = QSpinBox()
        self.glossary_api_timeout.setRange(10, 3600)
        self.glossary_api_timeout.setSuffix(" 秒")
        self.glossary_api_timeout.setValue(self.settings.glossary_api_timeout)
        glossary_limits_layout.addWidget(QLabel("并发"))
        glossary_limits_layout.addWidget(self.glossary_api_threads)
        glossary_limits_layout.addWidget(QLabel("超时"))
        glossary_limits_layout.addWidget(self.glossary_api_timeout)
        glossary_limits_layout.addStretch(1)
        glossary_form.addRow("请求", glossary_limits)

        self.glossary_chunk_chars = QSpinBox()
        self.glossary_chunk_chars.setRange(1, 10_000_000)
        self.glossary_chunk_chars.setSuffix(" 字符")
        self.glossary_chunk_chars.setGroupSeparatorShown(True)
        self.glossary_chunk_chars.setValue(self.settings.glossary_chunk_chars)
        glossary_form.addRow("每块最大输入", self.glossary_chunk_chars)

        glossary_output = QWidget()
        glossary_output_layout = QHBoxLayout(glossary_output)
        glossary_output_layout.setContentsMargins(0, 0, 0, 0)
        self.glossary_api_max_tokens = QSpinBox()
        self.glossary_api_max_tokens.setRange(0, 1_000_000)
        self.glossary_api_max_tokens.setSpecialValueText("服务端默认")
        self.glossary_api_max_tokens.setGroupSeparatorShown(True)
        self.glossary_api_max_tokens.setValue(self.settings.glossary_api_max_tokens)
        self.glossary_test_button = QPushButton("测试术语 API")
        self.glossary_test_button.clicked.connect(lambda: self._test_api(True))
        glossary_output_layout.addWidget(self.glossary_api_max_tokens)
        glossary_output_layout.addWidget(self.glossary_test_button)
        glossary_output_layout.addStretch(1)
        glossary_form.addRow("最大输出 Token", glossary_output)
        self.api_tabs.addTab(glossary_page, "术语生成 API")

        translation_page = QWidget()
        translation_form = QFormLayout(translation_page)
        translation_form.setHorizontalSpacing(18)
        translation_form.setVerticalSpacing(10)
        self.api_url = QLineEdit(self.settings.api_base_url)
        self.api_url.setPlaceholderText("https://example.com/v1")
        self.api_model = QLineEdit(self.settings.api_model)
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.Password)
        try:
            self.api_key.setText(self.store.api_key(self.settings))
        except Exception:
            pass
        translation_form.addRow("API 基础地址", self.api_url)
        translation_form.addRow("模型", self.api_model)
        translation_form.addRow("API 密钥", self.api_key)

        limits = QWidget()
        limits_layout = QHBoxLayout(limits)
        limits_layout.setContentsMargins(0, 0, 0, 0)
        self.api_threads = QSpinBox()
        self.api_threads.setRange(1, 100)
        self.api_threads.setValue(self.settings.api_threads)
        self.api_timeout = QSpinBox()
        self.api_timeout.setRange(10, 3600)
        self.api_timeout.setSuffix(" 秒")
        self.api_timeout.setValue(self.settings.api_timeout)
        limits_layout.addWidget(QLabel("并发"))
        limits_layout.addWidget(self.api_threads)
        limits_layout.addWidget(QLabel("超时"))
        limits_layout.addWidget(self.api_timeout)
        limits_layout.addStretch(1)
        translation_form.addRow("请求", limits)

        quotas = QWidget()
        quotas_layout = QHBoxLayout(quotas)
        quotas_layout.setContentsMargins(0, 0, 0, 0)
        self.api_rpm = QSpinBox()
        self.api_rpm.setRange(1, 1_000_000)
        self.api_rpm.setValue(self.settings.api_rpm)
        self.api_tpm = QSpinBox()
        self.api_tpm.setRange(1, 2_000_000_000)
        self.api_tpm.setGroupSeparatorShown(True)
        self.api_tpm.setValue(self.settings.api_tpm)
        self.test_button = QPushButton("测试翻译 API")
        self.test_button.clicked.connect(lambda: self._test_api(False))
        quotas_layout.addWidget(QLabel("RPM"))
        quotas_layout.addWidget(self.api_rpm)
        quotas_layout.addWidget(QLabel("TPM"))
        quotas_layout.addWidget(self.api_tpm)
        quotas_layout.addWidget(self.test_button)
        quotas_layout.addStretch(1)
        translation_form.addRow("请求限制", quotas)

        chunking = QWidget()
        chunking_layout = QHBoxLayout(chunking)
        chunking_layout.setContentsMargins(0, 0, 0, 0)
        self.translation_chunk_group = QButtonGroup(self)
        self.translation_chunk_group.setExclusive(True)
        self.translation_token_mode = QPushButton("Token")
        self.translation_line_mode = QPushButton("条目")
        for button in (self.translation_token_mode, self.translation_line_mode):
            button.setCheckable(True)
            button.setObjectName("segment")
            self.translation_chunk_group.addButton(button)
            chunking_layout.addWidget(button)

        self.translation_chunk_stack = QStackedWidget()
        self.translation_token_limit = QSpinBox()
        self.translation_token_limit.setRange(64, 8192)
        self.translation_token_limit.setSuffix(" Token")
        self.translation_token_limit.setValue(self.settings.translation_token_limit)
        self.translation_chunk_stack.addWidget(self.translation_token_limit)

        line_limits = QWidget()
        line_limits_layout = QHBoxLayout(line_limits)
        line_limits_layout.setContentsMargins(0, 0, 0, 0)
        self.translation_line_limit = QSpinBox()
        self.translation_line_limit.setRange(1, 100)
        self.translation_line_limit.setSuffix(" 条")
        self.translation_line_limit.setValue(self.settings.translation_line_limit)
        self.translation_retry_min_lines = QSpinBox()
        self.translation_retry_min_lines.setRange(1, 100)
        self.translation_retry_min_lines.setSuffix(" 条")
        self.translation_retry_min_lines.setValue(self.settings.translation_retry_min_lines)
        line_limits_layout.addWidget(self.translation_line_limit)
        line_limits_layout.addWidget(QLabel("重试最小"))
        line_limits_layout.addWidget(self.translation_retry_min_lines)
        self.translation_chunk_stack.addWidget(line_limits)
        chunking_layout.addWidget(self.translation_chunk_stack)
        chunking_layout.addStretch(1)
        token_mode = self.settings.translation_chunk_mode == "token"
        self.translation_token_mode.setChecked(token_mode)
        self.translation_line_mode.setChecked(not token_mode)
        self.translation_chunk_stack.setCurrentIndex(0 if token_mode else 1)
        self.translation_token_mode.clicked.connect(lambda: self.translation_chunk_stack.setCurrentIndex(0))
        self.translation_line_mode.clicked.connect(lambda: self.translation_chunk_stack.setCurrentIndex(1))
        translation_form.addRow("翻译分块", chunking)

        self.translation_rounds = QSpinBox()
        self.translation_rounds.setRange(1, 20)
        self.translation_rounds.setValue(self.settings.translation_rounds)
        translation_form.addRow("单次最大轮次", self.translation_rounds)
        self.api_tabs.addTab(translation_page, "AiNiee 翻译 API")
        layout.addWidget(self.api_tabs)

        self.license_check = QCheckBox("我确认仅将 FreeGames 工具用于其许可范围内的免费游戏")
        self.license_check.setChecked(self.settings.license_accepted)
        layout.addWidget(self.license_check)
        self.activity = QLabel("")
        self.activity.setObjectName("secondaryText")
        layout.addWidget(self.activity)
        self.install_progress = QProgressBar()
        self.install_progress.hide()
        layout.addWidget(self.install_progress)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        self.save_button = buttons.button(QDialogButtonBox.Save)
        layout.addWidget(buttons)

    def _choose_wolf(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择官方工具", self.wolf_path.text(), "Programs (*.exe)")
        if path:
            self.wolf_path.setText(path)

    def _choose_editor(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 WOLF RPG Editor",
            self.editor_path.text(),
            "Editor.exe (Editor.exe)",
        )
        if path:
            self.editor_path.setText(path)
            self._probe_editor()

    def _probe_editor(self) -> None:
        try:
            info = inspect_wolf_editor(self.editor_path.text().strip())
            self.editor_status.setText(f"已识别 WOLF RPG Editor {info.version}")
        except (OSError, ValueError) as error:
            self.editor_status.setText(str(error) if self.editor_path.text().strip() else "尚未指定 Editor.exe")

    def _choose_ainiee(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择 AiNiee 安装或源码目录", self.ainiee_path.text())
        if not path:
            return
        try:
            source = str(locate_ainiee_source(path))
            self._start_ainiee_setup(False, source)
        except Exception as exc:
            QMessageBox.critical(self, "AiNiee 不兼容", str(exc))

    def _install_editor(self) -> None:
        if self._installation_running():
            return
        self._set_install_controls_enabled(False)
        self.install_progress.setRange(0, 0)
        self.install_progress.show()
        self.activity.setText("正在安装 WOLF RPG Editor...")
        self.editor_install_thread = EditorInstallThread(
            local_data_dir() / "packages" / "editor"
        )
        self.editor_install_thread.progress_changed.connect(self._install_progress_changed)
        self.editor_install_thread.log_line.connect(self.activity.setText)
        self.editor_install_thread.installed.connect(self._editor_installed)
        self.editor_install_thread.failed.connect(self._editor_install_failed)
        self.editor_install_thread.start()

    def _editor_installed(self, path: str) -> None:
        self.editor_path.setText(path)
        self._probe_editor()
        self.activity.setText(f"WOLF RPG Editor 已就绪：{path}")
        self._finish_install()

    def _editor_install_failed(self, detail: str) -> None:
        self.activity.setText("WOLF RPG Editor 安装失败")
        self._finish_install()
        QMessageBox.critical(self, "安装失败", detail[-4000:])

    def _choose_projects_root(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择项目目录", self.projects_root.text())
        if path:
            self.projects_root.setText(path)

    def _choose_ascii_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择纯 ASCII 目录", self.ascii_dir.text())
        if path:
            self.ascii_dir.setText(path)

    def _install_ainiee(self, repair: bool) -> None:
        self._start_ainiee_setup(repair)

    def _start_ainiee_setup(self, repair: bool, source: str = "") -> None:
        if self._installation_running():
            return
        self._set_install_controls_enabled(False)
        self.install_progress.setRange(0, 0)
        self.install_progress.show()
        self.activity.setText("正在准备 AiNiee 源码与运行依赖...")
        self.install_thread = InstallThread(
            local_data_dir() / "packages" / "ainiee",
            local_data_dir() / "runtime" / "ainiee",
            repair,
            source,
        )
        self.install_thread.progress_changed.connect(self._install_progress_changed)
        self.install_thread.log_line.connect(self.activity.setText)
        self.install_thread.installed.connect(self._ainiee_installed)
        self.install_thread.failed.connect(self._ainiee_install_failed)
        self.install_thread.start()

    def _install_progress_changed(self, received: int, total: int) -> None:
        if total:
            self.install_progress.setRange(0, total)
            self.install_progress.setValue(received)
        else:
            self.install_progress.setRange(0, 0)

    def _ainiee_installed(self, path: str) -> None:
        self.ainiee_path.setText(path)
        self.activity.setText(f"AiNiee 已就绪：{path}")
        self._finish_install()

    def _ainiee_install_failed(self, detail: str) -> None:
        self.activity.setText("AiNiee 安装失败")
        self._finish_install()
        QMessageBox.critical(self, "安装失败", detail[-4000:])

    def _installation_running(self) -> bool:
        return bool(
            (self.install_thread and self.install_thread.isRunning())
            or (self.editor_install_thread and self.editor_install_thread.isRunning())
        )

    def _set_install_controls_enabled(self, enabled: bool) -> None:
        self.install_button.setEnabled(enabled)
        self.select_ainiee_button.setEnabled(enabled)
        self.repair_button.setEnabled(enabled)
        self.select_editor_button.setEnabled(enabled)
        self.editor_install_button.setEnabled(enabled)
        self.save_button.setEnabled(enabled)

    def _finish_install(self) -> None:
        self.install_progress.hide()
        self._set_install_controls_enabled(True)

    def _open_ainiee(self) -> None:
        path = Path(self.ainiee_path.text())
        if path.exists():
            os.startfile(path if path.is_dir() else path.parent)

    def _remove_ainiee(self) -> None:
        path = Path(self.ainiee_path.text()).resolve()
        managed_root = (local_data_dir() / "packages" / "ainiee").resolve()
        if not path.exists() or os.path.commonpath([str(path), str(managed_root)]) != str(managed_root):
            QMessageBox.information(self, "未移除", "当前路径不是 WOLFLator 托管版本。")
            return
        if QMessageBox.question(self, "移除 AiNiee", f"移除托管源码与隔离运行时？\n{path}") != QMessageBox.Yes:
            return
        remove_managed_ainiee(
            path,
            managed_root,
            local_data_dir() / "runtime" / "ainiee",
        )
        self.settings.ainiee_source = ""
        self.store.save(self.settings)
        self.ainiee_path.clear()
        self.activity.setText("已移除托管版本与隔离运行时")

    def _current_settings(self):
        item = self.settings
        item.wolf_tool_path = self.wolf_path.text().strip()
        item.wolf_editor_path = self.editor_path.text().strip()
        item.ainiee_source = self.ainiee_path.text().strip()
        item.api_base_url = self.api_url.text().strip().rstrip("/")
        item.api_model = self.api_model.text().strip()
        item.api_threads = self.api_threads.value()
        item.api_timeout = self.api_timeout.value()
        item.api_rpm = self.api_rpm.value()
        item.api_tpm = self.api_tpm.value()
        item.translation_chunk_mode = "token" if self.translation_token_mode.isChecked() else "line"
        item.translation_token_limit = self.translation_token_limit.value()
        item.translation_line_limit = self.translation_line_limit.value()
        item.translation_retry_min_lines = self.translation_retry_min_lines.value()
        item.translation_rounds = self.translation_rounds.value()
        item.glossary_api_base_url = self.glossary_api_url.text().strip().rstrip("/")
        item.glossary_api_model = self.glossary_api_model.text().strip()
        item.glossary_api_threads = self.glossary_api_threads.value()
        item.glossary_api_timeout = self.glossary_api_timeout.value()
        item.glossary_chunk_chars = self.glossary_chunk_chars.value()
        item.glossary_api_max_tokens = self.glossary_api_max_tokens.value()
        item.projects_root = self.projects_root.text().strip()
        item.ascii_runner_dir = self.ascii_dir.text().strip()
        item.license_accepted = self.license_check.isChecked()
        return item

    def _test_api(self, glossary: bool = False) -> None:
        item = self._current_settings()
        key = (self.glossary_api_key if glossary else self.api_key).text().strip()
        if not key:
            QMessageBox.warning(self, "API", "请填写 API 密钥。")
            return
        self.api_test_target = "glossary" if glossary else "translation"
        self.test_button.setEnabled(False)
        self.glossary_test_button.setEnabled(False)
        self.activity.setText("正在测试术语 API..." if glossary else "正在测试翻译 API...")
        self.api_thread = ApiTestThread(item, key, glossary=glossary)
        self.api_thread.succeeded.connect(self._api_succeeded)
        self.api_thread.failed.connect(self._api_failed)
        self.api_thread.start()

    def _api_succeeded(self, response: str) -> None:
        self.test_button.setEnabled(True)
        self.glossary_test_button.setEnabled(True)
        label = "术语 API" if self.api_test_target == "glossary" else "翻译 API"
        self.activity.setText(f"{label} 连接成功")
        preview = response[:500] + ("..." if len(response) > 500 else "")
        QMessageBox.information(self, f"{label} 连接成功", f"模型已返回正文：\n\n{preview}")

    def _api_failed(self, error: str) -> None:
        self.test_button.setEnabled(True)
        self.glossary_test_button.setEnabled(True)
        label = "术语 API" if self.api_test_target == "glossary" else "翻译 API"
        self.activity.setText(f"{label} 测试失败")
        QMessageBox.critical(self, f"{label} 测试失败", error)

    def _save(self) -> None:
        item = self._current_settings()
        try:
            self.store.set_api_key(item, self.api_key.text())
            self.store.set_glossary_api_key(item, self.glossary_api_key.text())
            errors = validate_settings(item)
            if errors:
                QMessageBox.warning(self, "设置未完成", "\n".join(errors))
                return
            Path(item.projects_root).mkdir(parents=True, exist_ok=True)
            Path(item.ascii_runner_dir).mkdir(parents=True, exist_ok=True)
            self.store.save(item)
            self.settings = item
            self.accept()
        except Exception as exc:
            QMessageBox.critical(self, "无法保存设置", str(exc))

    def reject(self) -> None:
        running = self._installation_running() or (self.api_thread and self.api_thread.isRunning())
        if running:
            QMessageBox.information(self, "任务运行中", "请等待当前安装或测试结束。")
            return
        super().reject()


def _configure_table(table: QTableWidget) -> None:
    table.setAlternatingRowColors(True)
    table.setSelectionBehavior(QTableWidget.SelectRows)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.store = SettingsStore()
        self.settings = self.store.load()
        self.pipeline: Pipeline | None = None
        self.pipeline_thread: PipelineThread | None = None
        self.font_scan_thread: FontScanThread | None = None
        self.font_context: dict[str, object] | None = None
        self.font_apply_active = False
        self.font_application_ids: list[int] = []
        self.active_step_stage: Stage | None = None
        self.current_manifest_path: Path | None = None
        self.setWindowTitle("WOLFLator")
        self.resize(1120, 760)
        self.setMinimumSize(880, 620)
        self._build_ui()
        self._refresh_projects()
        if validate_settings(self.settings):
            QTimer.singleShot(0, lambda: self._open_settings(first_run=True))

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(14)

        header = QHBoxLayout()
        brand = QLabel("WOLFLator")
        brand.setObjectName("brand")
        header.addWidget(brand)
        header.addStretch(1)
        self.settings_button = QToolButton()
        self.settings_button.setIcon(self.style().standardIcon(QStyle.SP_FileDialogDetailedView))
        self.settings_button.setToolTip("设置")
        self.settings_button.clicked.connect(self._open_settings)
        header.addWidget(self.settings_button)
        layout.addLayout(header)

        project_row = QHBoxLayout()
        self.project_combo = QComboBox()
        self.project_combo.setMinimumWidth(340)
        self.project_combo.currentIndexChanged.connect(self._project_changed)
        self.new_project_button = QPushButton("新建项目")
        self.new_project_button.setIcon(self.style().standardIcon(QStyle.SP_FileIcon))
        self.new_project_button.clicked.connect(self._new_project)
        self.add_version_button = QPushButton("添加版本")
        self.add_version_button.setIcon(self.style().standardIcon(QStyle.SP_FileDialogNewFolder))
        self.add_version_button.clicked.connect(self._add_version)
        project_row.addWidget(self.project_combo, 1)
        project_row.addWidget(self.new_project_button)
        project_row.addWidget(self.add_version_button)
        layout.addLayout(project_row)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._workflow_tab(), "流程")
        self.tabs.addTab(self._glossary_tab(), "术语")
        self.tabs.addTab(self._scope_tab(), "范围")
        self.tabs.addTab(self._font_tab(), "修改字体")
        layout.addWidget(self.tabs, 1)

        footer = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, len(STAGE_ORDER))
        self.progress.setTextVisible(False)
        self.status_label = QLabel("就绪")
        self.status_label.setMinimumWidth(130)
        self.retry_button = QToolButton()
        self.retry_button.setIcon(self.style().standardIcon(QStyle.SP_BrowserReload))
        self.retry_button.setToolTip("重试失败阶段")
        self.retry_button.setEnabled(False)
        self.retry_button.clicked.connect(self._retry)
        self.stop_button = QToolButton()
        self.stop_button.setIcon(self.style().standardIcon(QStyle.SP_MediaStop))
        self.stop_button.setToolTip("停止")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop)
        self.open_release_button = QPushButton("打开发布目录")
        self.open_release_button.setIcon(self.style().standardIcon(QStyle.SP_DirOpenIcon))
        self.open_release_button.clicked.connect(self._open_release)
        footer.addWidget(self.status_label)
        footer.addWidget(self.progress, 1)
        footer.addWidget(self.retry_button)
        footer.addWidget(self.stop_button)
        footer.addWidget(self.open_release_button)
        layout.addLayout(footer)

    def _workflow_tab(self) -> QWidget:
        page = QWidget()
        self.workflow_page = page
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(14)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("运行方式"))
        self.workflow_mode_group = QButtonGroup(self)
        self.workflow_mode_group.setExclusive(True)
        self.one_click = QPushButton("一键模式")
        self.step_mode = QPushButton("分步模式")
        for button in (self.one_click, self.step_mode):
            button.setCheckable(True)
            button.setObjectName("segment")
            self.workflow_mode_group.addButton(button)
            mode_row.addWidget(button)
        mode_row.addStretch(1)
        layout.addLayout(mode_row)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setObjectName("workflowSplitter")
        self.workflow_splitter = splitter

        self.workflow_stack = QStackedWidget()
        self.workflow_stack.addWidget(self._one_click_panel())
        self.workflow_stack.addWidget(self._step_panel())
        self.workflow_stack.setMinimumWidth(500)
        splitter.addWidget(self.workflow_stack)

        log_panel = QWidget()
        log_layout = QVBoxLayout(log_panel)
        log_layout.setContentsMargins(12, 4, 0, 0)
        log_layout.setSpacing(8)
        log_header = QHBoxLayout()
        log_title = QLabel("实时日志")
        log_title.setObjectName("panelTitle")
        log_header.addWidget(log_title)
        log_header.addStretch(1)
        self.open_logs_button = QToolButton()
        self.open_logs_button.setIcon(self.style().standardIcon(QStyle.SP_DirOpenIcon))
        self.open_logs_button.setToolTip("打开日志目录")
        self.open_logs_button.setEnabled(False)
        self.open_logs_button.clicked.connect(self._open_log_dir)
        log_header.addWidget(self.open_logs_button)
        log_layout.addLayout(log_header)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(10_000)
        self.log_view.setMinimumWidth(250)
        log_layout.addWidget(self.log_view, 1)
        splitter.addWidget(log_panel)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([640, 400])
        layout.addWidget(splitter, 1)
        self.one_click.setChecked(True)
        self.one_click.clicked.connect(lambda: self._select_workflow_mode(RunMode.ONE_CLICK))
        self.step_mode.clicked.connect(lambda: self._select_workflow_mode(RunMode.STEP))
        return page

    def _one_click_panel(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 16, 0, 0)
        layout.setSpacing(24)

        stages = QGridLayout()
        stages.setHorizontalSpacing(8)
        stages.setVerticalSpacing(8)
        self.easy_stage_status: dict[Stage, QLabel] = {}
        for index, stage in enumerate(STAGE_ORDER, start=1):
            node = QFrame()
            node.setObjectName("stageNode")
            node_layout = QVBoxLayout(node)
            node_layout.setContentsMargins(8, 10, 8, 9)
            node_layout.setSpacing(5)
            number = QLabel(f"{index:02d}")
            number.setObjectName("stepNumber")
            title = QLabel(STAGE_LABELS[stage])
            title.setAlignment(Qt.AlignCenter)
            status = QLabel(STATUS_LABELS[StageStatus.PENDING])
            status.setObjectName("stageStatus")
            status.setAlignment(Qt.AlignCenter)
            node_layout.addWidget(number, alignment=Qt.AlignCenter)
            node_layout.addWidget(title)
            node_layout.addWidget(status)
            self.easy_stage_status[stage] = status
            stages.addWidget(node, (index - 1) // 4, (index - 1) % 4)
            stages.setColumnStretch((index - 1) % 4, 1)
        layout.addLayout(stages)
        layout.addStretch(1)

        self.easy_summary = QLabel("选择项目后即可开始")
        self.easy_summary.setObjectName("secondaryText")
        self.easy_summary.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.easy_summary)
        self.start_button = QPushButton("开始翻译")
        self.start_button.setObjectName("primaryButton")
        self.start_button.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.start_button.setMinimumWidth(160)
        self.start_button.clicked.connect(lambda: self._start())
        layout.addWidget(self.start_button, alignment=Qt.AlignCenter)
        layout.addStretch(1)
        return page

    def _step_panel(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(0)
        self.step_status_labels: dict[Stage, QLabel] = {}
        self.step_buttons: dict[Stage, QPushButton] = {}
        self.step_result_buttons: dict[Stage, QPushButton] = {}
        for index, stage in enumerate(STAGE_ORDER, start=1):
            row = QFrame()
            row.setObjectName("stageRow")
            row_layout = QGridLayout(row)
            row_layout.setContentsMargins(8, 6, 8, 6)
            row_layout.setHorizontalSpacing(8)
            number = QLabel(str(index))
            number.setObjectName("stepNumberLarge")
            number.setAlignment(Qt.AlignCenter)
            number.setFixedSize(28, 28)
            title = QLabel(STAGE_LABELS[stage])
            title.setObjectName("stageTitle")
            title.setMinimumWidth(64)
            description = QLabel(STAGE_DESCRIPTIONS[stage])
            description.setObjectName("secondaryText")
            description.setToolTip(STAGE_DESCRIPTIONS[stage])
            status = QLabel(STATUS_LABELS[StageStatus.PENDING])
            status.setObjectName("stageStatus")
            status.setAlignment(Qt.AlignCenter)
            status.setMinimumWidth(64)
            run_button = QPushButton("执行")
            run_button.setFixedWidth(80)
            run_button.clicked.connect(lambda _checked=False, target=stage: self._start(target))
            result_button = QPushButton(STAGE_RESULT_LABELS[stage])
            result_button.setFixedWidth(80)
            result_button.clicked.connect(
                lambda _checked=False, target=stage: self._open_stage_result(target)
            )
            row_layout.addWidget(number, 0, 0)
            row_layout.addWidget(title, 0, 1)
            row_layout.addWidget(description, 0, 2)
            row_layout.addWidget(status, 0, 3)
            row_layout.addWidget(result_button, 0, 4)
            row_layout.addWidget(run_button, 0, 5)
            row_layout.setColumnStretch(2, 1)
            self.step_status_labels[stage] = status
            self.step_buttons[stage] = run_button
            self.step_result_buttons[stage] = result_button
            layout.addWidget(row)
        layout.addStretch(1)
        return page

    def _glossary_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 12, 0, 0)
        views = QTabWidget()
        terms_page = QWidget()
        terms_layout = QVBoxLayout(terms_page)
        terms_layout.setContentsMargins(0, 8, 0, 0)
        self.terms_table = QTableWidget(0, 3)
        self.terms_table.setHorizontalHeaderLabels(["原文", "译文", "说明"])
        _configure_table(self.terms_table)
        terms_layout.addWidget(self.terms_table)
        term_buttons = QHBoxLayout()
        add_term = QPushButton("添加")
        add_term.clicked.connect(lambda: self.terms_table.insertRow(self.terms_table.rowCount()))
        remove_term = QPushButton("删除")
        remove_term.clicked.connect(lambda: self.terms_table.removeRow(self.terms_table.currentRow()))
        term_buttons.addWidget(add_term)
        term_buttons.addWidget(remove_term)
        term_buttons.addStretch(1)
        terms_layout.addLayout(term_buttons)
        views.addTab(terms_page, "术语表")

        characters_page = QWidget()
        characters_layout = QVBoxLayout(characters_page)
        characters_layout.setContentsMargins(0, 8, 0, 0)
        self.characters_table = QTableWidget(0, 6)
        self.characters_table.setHorizontalHeaderLabels(["原名", "译名", "性别", "性格", "口吻", "补充"])
        _configure_table(self.characters_table)
        characters_layout.addWidget(self.characters_table)
        character_buttons = QHBoxLayout()
        add_character = QPushButton("添加")
        add_character.clicked.connect(lambda: self.characters_table.insertRow(self.characters_table.rowCount()))
        remove_character = QPushButton("删除")
        remove_character.clicked.connect(lambda: self.characters_table.removeRow(self.characters_table.currentRow()))
        character_buttons.addWidget(add_character)
        character_buttons.addWidget(remove_character)
        character_buttons.addStretch(1)
        characters_layout.addLayout(character_buttons)
        views.addTab(characters_page, "人物")
        layout.addWidget(views)
        self.save_glossary_button = QPushButton("保存术语")
        self.save_glossary_button.clicked.connect(self._save_glossary)
        layout.addWidget(self.save_glossary_button, alignment=Qt.AlignRight)
        return page

    def _scope_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 20, 18, 18)
        layout.setSpacing(14)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("范围类型"))
        self.scope_mode_group = QButtonGroup(self)
        self.scope_mode_group.setExclusive(True)
        self.export_scope_button = QPushButton("导出范围")
        self.translation_scope_button = QPushButton("翻译范围")
        self.import_scope_button = QPushButton("导入范围")
        for button in (
            self.export_scope_button,
            self.translation_scope_button,
            self.import_scope_button,
        ):
            button.setCheckable(True)
            button.setObjectName("segment")
            self.scope_mode_group.addButton(button)
            mode_row.addWidget(button)
        mode_row.addStretch(1)
        layout.addLayout(mode_row)

        self.scope_stack = QStackedWidget()
        self.export_scope_checks = self._scope_panel(self.scope_stack, "export")
        self.translation_scope_checks = self._scope_panel(self.scope_stack, "translation")
        self.import_scope_checks = self._scope_panel(self.scope_stack, "import")
        layout.addWidget(self.scope_stack, 1)
        self.export_scope_button.setChecked(True)
        self.export_scope_button.clicked.connect(lambda: self.scope_stack.setCurrentIndex(0))
        self.translation_scope_button.clicked.connect(lambda: self.scope_stack.setCurrentIndex(1))
        self.import_scope_button.clicked.connect(lambda: self.scope_stack.setCurrentIndex(2))
        return page

    def _scope_panel(self, stack: QStackedWidget, target: str) -> dict[str, QCheckBox]:
        panel = QWidget()
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(0, 6, 0, 0)
        checks = {
            "display": QCheckBox("显示文本"),
            "external": QCheckBox("外部 TXT / CSV"),
            "optional_name": QCheckBox("数据库、地图和事件名称"),
            "halfwidth": QCheckBox("纯半角字符串"),
            "filename": QCheckBox("文件名引用"),
        }
        defaults = default_export_scope() if target == "export" else ImportScope()
        for name, check in checks.items():
            check.setChecked(bool(getattr(defaults, name)))
        for key, check in checks.items():
            check.toggled.connect(lambda _checked=False, scope_target=target: self._save_scope(scope_target))
            panel_layout.addWidget(check)
            if target == "export" and key == "external":
                self.external_filter_options = QWidget()
                filter_layout = QHBoxLayout(self.external_filter_options)
                filter_layout.setContentsMargins(26, 0, 0, 8)
                filter_layout.setSpacing(8)
                self.exclude_large_external_files = QCheckBox("自动排除超过")
                self.exclude_large_external_files.setChecked(True)
                self.external_file_limit_kb = QSpinBox()
                self.external_file_limit_kb.setRange(1, MAX_EXTERNAL_FILE_LIMIT_KB)
                self.external_file_limit_kb.setValue(DEFAULT_EXTERNAL_FILE_LIMIT_KB)
                filter_suffix = QLabel("KB 的文件")
                filter_layout.addWidget(self.exclude_large_external_files)
                filter_layout.addWidget(self.external_file_limit_kb)
                filter_layout.addWidget(filter_suffix)
                filter_layout.addStretch(1)
                panel_layout.addWidget(self.external_filter_options)
                check.toggled.connect(self._update_external_filter_controls)
                self.exclude_large_external_files.toggled.connect(
                    self._external_filter_changed
                )
                self.external_file_limit_kb.valueChanged.connect(
                    lambda _value: self._save_scope("export")
                )
            if target == "import" and key == "filename":
                warning = QLabel("启用文件名导入前，发布副本中必须存在对应的目标文件。")
                warning.setObjectName("warningText")
                panel_layout.addWidget(warning)
        if target == "import":
            self._add_import_protection_controls(panel_layout)
        else:
            panel_layout.addStretch(1)
        stack.addWidget(panel)
        if target == "export":
            self.external_filter_options.setVisible(checks["external"].isChecked())
            self.external_file_limit_kb.setEnabled(
                checks["external"].isChecked()
                and self.exclude_large_external_files.isChecked()
            )
        return checks

    def _update_external_filter_controls(self) -> None:
        visible = self.export_scope_checks["external"].isChecked()
        self.external_filter_options.setVisible(visible)
        self.external_file_limit_kb.setEnabled(
            visible and self.exclude_large_external_files.isChecked()
        )

    def _external_filter_changed(self, _checked: bool) -> None:
        self._update_external_filter_controls()
        self._save_scope("export")

    def _add_import_protection_controls(self, layout: QVBoxLayout) -> None:
        title = QLabel("导入保护规则")
        title.setObjectName("panelTitle")
        layout.addWidget(title)
        self.protect_external_references = QCheckBox("保留外部脚本引用名称")
        self.protect_paths_and_commands = QCheckBox("保留路径与脚本命令")
        self.protect_logic_references = QCheckBox("按 WOLF 事件逻辑保护分支相关文本")
        self.allow_copy_condition_groups = QCheckBox("允许 COPY-FROM 条件/混合范围组整体翻译")
        for control in (
            self.protect_external_references,
            self.protect_paths_and_commands,
            self.protect_logic_references,
            self.allow_copy_condition_groups,
        ):
            control.setChecked(True)
            control.toggled.connect(self._save_import_protection)
            layout.addWidget(control)
        copy_note = QLabel("COPY-FROM 选项会改变 AiNiee 输入；修改后将重置术语及后续阶段。")
        copy_note.setObjectName("secondaryText")
        layout.addWidget(copy_note)

        identifier_row = QHBoxLayout()
        identifier_row.addWidget(QLabel("可疑标识符"))
        self.suspicious_identifier_action = QComboBox()
        self.suspicious_identifier_action.addItem("不处理", "ignore")
        self.suspicious_identifier_action.addItem("仅警告", "warn")
        self.suspicious_identifier_action.addItem("保留原文", "protect")
        self.suspicious_identifier_action.setCurrentIndex(1)
        self.suspicious_identifier_action.currentIndexChanged.connect(
            self._save_import_protection
        )
        identifier_row.addWidget(self.suspicious_identifier_action)
        identifier_row.addStretch(1)
        layout.addLayout(identifier_row)

        preview_row = QHBoxLayout()
        self.import_protection_summary = QLabel("完成翻译后可预览实际匹配项")
        self.import_protection_summary.setObjectName("secondaryText")
        preview_row.addWidget(self.import_protection_summary, 1)
        self.preview_import_protection_button = QPushButton("预览匹配项")
        self.preview_import_protection_button.clicked.connect(
            self._preview_import_protection
        )
        preview_row.addWidget(self.preview_import_protection_button)
        layout.addLayout(preview_row)

        self.import_protection_table = QTableWidget(0, 4)
        self.import_protection_table.setHorizontalHeaderLabels(
            ["动作", "代码", "原文", "原因"]
        )
        self.import_protection_table.setMinimumHeight(180)
        _configure_table(self.import_protection_table)
        layout.addWidget(self.import_protection_table, 1)

    def _current_import_protection_rules(self) -> ImportProtectionRules:
        return ImportProtectionRules(
            protect_external_references=self.protect_external_references.isChecked(),
            protect_paths_and_commands=self.protect_paths_and_commands.isChecked(),
            protect_logic_references=self.protect_logic_references.isChecked(),
            allow_copy_condition_groups=self.allow_copy_condition_groups.isChecked(),
            suspicious_identifiers=str(
                self.suspicious_identifier_action.currentData() or "warn"
            ),
        )

    def _save_import_protection(self, _value: object = None) -> None:
        if not self.current_manifest_path or (self.pipeline_thread and self.pipeline_thread.isRunning()):
            return
        pipeline = Pipeline(
            self.current_manifest_path,
            self.settings,
            "",
            local_data_dir(),
            glossary_api_key="",
        )
        pipeline.set_import_protection(self._current_import_protection_rules())
        if self.pipeline:
            self.pipeline.manifest = pipeline.manifest
        self.import_protection_summary.setText("规则已保存；点击预览重新分析")

    def _preview_import_protection(self) -> None:
        self.import_protection_table.setRowCount(0)
        if not self.current_manifest_path:
            self.import_protection_summary.setText("请先选择项目")
            return
        try:
            manifest = load_manifest(self.current_manifest_path)
            artifacts = manifest.version.stage(Stage.VALIDATE).artifacts
            items_path = artifacts.get("items", "")
            if not items_path or not Path(items_path).is_file():
                items_path = manifest.version.stage(Stage.TRANSLATE).artifacts.get("items", "")
            if not items_path or not Path(items_path).is_file():
                raise RuntimeError("完成翻译后才能预览实际匹配项。")
            version_dir = self.current_manifest_path.parent / "versions" / manifest.active_version
            game_root = version_dir / "work"
            if not game_root.is_dir():
                game_root = version_dir / "source"
            report = analyze_import_protection(
                load_items(items_path),
                manifest.import_scope,
                game_root,
                manifest.import_protection,
                _load_editor_analysis(manifest),
                block_on_logic_issue=False,
            )
            entries = report["entries"]
            self.import_protection_table.setRowCount(min(len(entries), 500))
            for row, entry in enumerate(entries[:500]):
                reason = IMPORT_PROTECTION_REASON_LABELS.get(
                    entry["reason"], entry["reason"]
                )
                if entry.get("evidence"):
                    reason += f"（{entry['evidence']}）"
                values = (
                    IMPORT_PROTECTION_ACTION_LABELS.get(entry["action"], entry["action"]),
                    entry["code"],
                    entry["original"],
                    reason,
                )
                for column, value in enumerate(values):
                    self.import_protection_table.setItem(
                        row, column, QTableWidgetItem(str(value))
                    )
            summary = report["summary"]
            suffix = "；表格仅显示前 500 项" if len(entries) > 500 else ""
            self.import_protection_summary.setText(
                f"保留 {summary['protected']} 组，警告 {summary['warnings']} 组，"
                f"逻辑依赖 {summary.get('logic_dependencies', 0)} 组，"
                f"实际逻辑保护 {summary.get('logic_protected', 0)} 组，"
                f"阻断问题 {summary.get('logic_blocking_relevant', 0)} 组，"
                f"未知语义 {summary.get('unknown_logic_semantics', 0)} 类，"
                f"整体翻译 {summary['atomic_groups']} 组{suffix}"
            )
        except Exception as exc:
            self.import_protection_summary.setText(str(exc))

    def _font_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 16, 18, 18)
        layout.setSpacing(12)

        header = QHBoxLayout()
        self.font_status = QLabel("选择项目后读取字体")
        self.font_status.setObjectName("secondaryText")
        header.addWidget(self.font_status, 1)
        self.show_incompatible_fonts = QCheckBox("显示不兼容字体")
        self.show_incompatible_fonts.toggled.connect(self._populate_font_choices)
        header.addWidget(self.show_incompatible_fonts)
        self.refresh_fonts_button = QToolButton()
        self.refresh_fonts_button.setIcon(self.style().standardIcon(QStyle.SP_BrowserReload))
        self.refresh_fonts_button.setToolTip("刷新字体目录")
        self.refresh_fonts_button.clicked.connect(lambda: self._refresh_font_tab(force=True))
        header.addWidget(self.refresh_fonts_button)
        layout.addLayout(header)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        for column, title in enumerate(("字体槽位", "原字体", "待应用字体", "字符覆盖", "预览")):
            label = QLabel(title)
            label.setObjectName("panelTitle")
            grid.addWidget(label, 0, column)
        self.font_original_labels: list[QLabel] = []
        self.font_combos: list[QComboBox] = []
        self.font_coverage_labels: list[QLabel] = []
        self.font_preview_original: list[QLabel] = []
        self.font_preview_selected: list[QLabel] = []
        for index, slot_name in enumerate(FONT_SLOT_NAMES, start=1):
            slot = QLabel(slot_name)
            original = QLabel("-")
            original.setObjectName("secondaryText")
            combo = QComboBox()
            combo.setMinimumWidth(230)
            combo.currentIndexChanged.connect(self._update_font_rows)
            coverage = QLabel("-")
            coverage.setObjectName("secondaryText")
            preview = QWidget()
            preview_layout = QVBoxLayout(preview)
            preview_layout.setContentsMargins(0, 0, 0, 0)
            preview_layout.setSpacing(2)
            original_preview = QLabel("")
            selected_preview = QLabel("")
            preview_layout.addWidget(original_preview)
            preview_layout.addWidget(selected_preview)
            grid.addWidget(slot, index, 0)
            grid.addWidget(original, index, 1)
            grid.addWidget(combo, index, 2)
            grid.addWidget(coverage, index, 3)
            grid.addWidget(preview, index, 4)
            self.font_original_labels.append(original)
            self.font_combos.append(combo)
            self.font_coverage_labels.append(coverage)
            self.font_preview_original.append(original_preview)
            self.font_preview_selected.append(selected_preview)
        grid.setColumnStretch(2, 2)
        grid.setColumnStretch(3, 2)
        grid.setColumnStretch(4, 2)
        layout.addLayout(grid)

        preview_row = QHBoxLayout()
        preview_row.addWidget(QLabel("预览文本"))
        self.font_preview_text = QLineEdit("你好，世界。漢字")
        self.font_preview_text.textChanged.connect(self._update_font_rows)
        preview_row.addWidget(self.font_preview_text, 1)
        layout.addLayout(preview_row)
        layout.addStretch(1)

        actions = QHBoxLayout()
        self.restore_fonts_button = QPushButton("恢复项目原字体")
        self.restore_fonts_button.clicked.connect(self._restore_fonts)
        self.save_fonts_button = QPushButton("保存方案")
        self.save_fonts_button.clicked.connect(lambda: self._save_font_scheme())
        self.apply_fonts_button = QPushButton("应用到发布目录")
        self.apply_fonts_button.setObjectName("primaryButton")
        self.apply_fonts_button.clicked.connect(self._apply_fonts)
        self.open_font_release_button = QPushButton("打开发布目录")
        self.open_font_release_button.clicked.connect(self._open_release)
        actions.addWidget(self.restore_fonts_button)
        actions.addStretch(1)
        actions.addWidget(self.open_font_release_button)
        actions.addWidget(self.save_fonts_button)
        actions.addWidget(self.apply_fonts_button)
        layout.addLayout(actions)
        self._set_font_controls_enabled(False)
        return page

    def _set_font_controls_enabled(self, enabled: bool) -> None:
        running = bool(self.pipeline_thread and self.pipeline_thread.isRunning())
        for combo in getattr(self, "font_combos", []):
            combo.setEnabled(enabled and not running)
        for widget in (
            getattr(self, "show_incompatible_fonts", None),
            getattr(self, "refresh_fonts_button", None),
            getattr(self, "restore_fonts_button", None),
            getattr(self, "save_fonts_button", None),
        ):
            if widget is not None:
                widget.setEnabled(enabled and not running)
        can_apply = False
        if enabled and not running and self.current_manifest_path:
            manifest = load_manifest(self.current_manifest_path)
            import_record = manifest.version.stage(Stage.IMPORT)
            translated = import_record.artifacts.get("translated_game", "")
            can_apply = import_record.status is StageStatus.COMPLETED and bool(
                translated and Path(translated).is_dir()
            )
        if hasattr(self, "apply_fonts_button"):
            self.apply_fonts_button.setEnabled(can_apply)

    def _release_font_previews(self) -> None:
        for font_id in self.font_application_ids:
            QFontDatabase.removeApplicationFont(font_id)
        self.font_application_ids.clear()

    def _clear_font_view(self, message: str = "选择项目后读取字体") -> None:
        self.font_context = None
        self._release_font_previews()
        if hasattr(self, "font_status"):
            self.font_status.setText(message)
            self.font_status.setToolTip("")
            for label in self.font_original_labels + self.font_coverage_labels:
                label.setText("-")
                label.setToolTip("")
            for combo in self.font_combos:
                combo.blockSignals(True)
                combo.clear()
                combo.blockSignals(False)
            for label in self.font_preview_original + self.font_preview_selected:
                label.clear()
            self._set_font_controls_enabled(False)

    def _refresh_font_tab(self, *, force: bool = False) -> None:
        if self.pipeline_thread and self.pipeline_thread.isRunning():
            self._set_font_controls_enabled(False)
            return
        if not self.current_manifest_path:
            self._clear_font_view()
            return
        current = str(self.current_manifest_path)
        if not force and self.font_context and self.font_context.get("manifest") == current:
            self._set_font_controls_enabled(True)
            return
        manifest = load_manifest(self.current_manifest_path)
        validate_record = manifest.version.stage(Stage.VALIDATE)
        if validate_record.status is not StageStatus.COMPLETED:
            self._clear_font_view("完成“校验译文”后才能检查和修改字体")
            return
        self.font_context = None
        self._set_font_controls_enabled(False)
        self.font_status.setText("正在扫描游戏、随附和系统字体...")
        if self.font_scan_thread and self.font_scan_thread.isRunning():
            return
        self.font_scan_thread = FontScanThread(self.current_manifest_path)
        self.font_scan_thread.succeeded.connect(self._font_scan_succeeded)
        self.font_scan_thread.failed.connect(self._font_scan_failed)
        self.font_scan_thread.finished.connect(self._font_scan_finished)
        self.font_scan_thread.start()

    def _font_scan_succeeded(self, context: object) -> None:
        if self.pipeline_thread and self.pipeline_thread.isRunning():
            return
        if not isinstance(context, dict) or not self.current_manifest_path:
            return
        if context.get("manifest") != str(self.current_manifest_path):
            return
        self.font_context = context
        self._release_font_previews()
        for candidate in context["candidates"]:
            if candidate.source == "system":
                continue
            for path in candidate.files:
                font_id = QFontDatabase.addApplicationFont(str(path))
                if font_id >= 0:
                    self.font_application_ids.append(font_id)
        warning_value = str(context.get("font_warning_count", "0"))
        warning_count = int(warning_value) if warning_value.isdigit() else 0
        if warning_count:
            self.font_status.setText(f"已扫描 {len(context['candidates'])} 个字体；发布版有 {warning_count} 个缺字警告")
            self.font_status.setToolTip(str(context.get("font_warnings", "")))
        else:
            self.font_status.setText(
                f"已扫描 {len(context['candidates'])} 个字体，检查 {len(context['required'])} 个实际文本字符"
            )
            self.font_status.setToolTip("")
        self._populate_font_choices()
        self._set_font_controls_enabled(True)

    def _font_scan_failed(self, manifest_path: str, error: str) -> None:
        if self.current_manifest_path and manifest_path == str(self.current_manifest_path):
            self._clear_font_view(error)

    def _font_scan_finished(self) -> None:
        thread = self.sender()
        scanned = thread.manifest_path if isinstance(thread, FontScanThread) else None
        if self.font_scan_thread is thread:
            self.font_scan_thread = None
        if self.pipeline_thread and self.pipeline_thread.isRunning():
            return
        if self.current_manifest_path and scanned != self.current_manifest_path:
            self._refresh_font_tab(force=True)

    def _populate_font_choices(self) -> None:
        if not self.font_context:
            return
        candidates: list[FontCandidate] = self.font_context["candidates"]
        show_all = self.show_incompatible_fonts.isChecked()
        visible = [
            candidate
            for candidate in candidates
            if show_all or candidate.source != "system" or len(candidate.missing) <= 10
        ]
        scheme = self.font_context.get("scheme")
        slots = scheme["slots"] if isinstance(scheme, dict) else [{"mode": "keep"}] * 4
        for index, combo in enumerate(self.font_combos):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem("保持项目原字体", None)
            labels: set[str] = set()
            for candidate in visible:
                label = candidate.label
                suffix = 2
                base = label
                while label in labels:
                    label = f"{base} ({suffix})"
                    suffix += 1
                labels.add(label)
                combo.addItem(label, candidate)
                combo.setItemData(combo.count() - 1, label, Qt.ToolTipRole)
            selection = slots[index]
            selected_index = 0
            if selection["mode"] == "font":
                for candidate_index in range(1, combo.count()):
                    candidate = combo.itemData(candidate_index)
                    if (
                        candidate.source == selection["provenance"]
                        and candidate.family.casefold() == str(selection["family"]).casefold()
                    ):
                        selected_index = candidate_index
                        break
                if selected_index == 0:
                    candidate = next(
                        (
                            item
                            for item in candidates
                            if item.source == selection["provenance"]
                            and item.family.casefold() == str(selection["family"]).casefold()
                        ),
                        None,
                    )
                    if candidate:
                        combo.addItem(f"[当前方案] {candidate.family}", candidate)
                        selected_index = combo.count() - 1
            combo.setCurrentIndex(selected_index)
            combo.blockSignals(False)
        self._update_font_rows()

    def _update_font_rows(self) -> None:
        if not self.font_context:
            return
        required: set[str] = self.font_context["required"]
        candidates: list[FontCandidate] = self.font_context["candidates"]
        original_slots: list[str] = self.font_context["original_slots"]
        sample = self.font_preview_text.text() or "字体预览"
        for index, combo in enumerate(self.font_combos):
            self.font_original_labels[index].setText(original_slots[index] or "未设置")
            self.font_original_labels[index].setToolTip(original_slots[index])
            selection = combo.currentData()
            candidate = selection or candidate_for_family(candidates, original_slots[index])
            family = selection.family if selection else original_slots[index]
            if candidate is None:
                text = "无法定位字体文件"
                missing = required
            else:
                missing = set(candidate.missing)
                ordered_missing = sorted(missing, key=ord)
                text = (
                    f"覆盖全部 {len(required)} 字"
                    if not missing
                    else f"缺少 {len(missing)} 字："
                    + json.dumps("".join(ordered_missing[:8]), ensure_ascii=False)
                    + (" 等" if len(missing) > 8 else "")
                )
            ordered_missing = sorted(missing, key=ord)
            tooltip_characters = ordered_missing[:256]
            self.font_coverage_labels[index].setText(text)
            self.font_coverage_labels[index].setToolTip(
                "缺少字符：\n"
                + "\n".join(
                    json.dumps("".join(tooltip_characters[offset : offset + 32]), ensure_ascii=False)
                    for offset in range(0, len(tooltip_characters), 32)
                )
                + (f"\n其余 {len(ordered_missing) - 256} 字未显示" if len(ordered_missing) > 256 else "")
                if missing
                else ""
            )
            original_family = original_slots[index] or QApplication.font().family()
            selected_family = family or QApplication.font().family()
            self.font_preview_original[index].setText("原  " + sample)
            self.font_preview_selected[index].setText("新  " + sample)
            self.font_preview_original[index].setFont(QFont(original_family, 12))
            self.font_preview_selected[index].setFont(QFont(selected_family, 12))

    def _selected_font_candidates(self) -> list[FontCandidate | None]:
        return [combo.currentData() for combo in self.font_combos]

    def _store_font_scheme(self, *, refresh: bool) -> bool:
        if not self.current_manifest_path or not self.font_context:
            return False
        selections = self._selected_font_candidates()
        system_families = sorted(
            {candidate.family for candidate in selections if candidate and candidate.source == "system"}
        )
        if system_families:
            answer = QMessageBox.question(
                self,
                "系统字体授权确认",
                "将把以下系统字体复制到项目和发布目录：\n"
                + "\n".join(system_families)
                + "\n\n请确认你有权随译版分发这些字体。",
            )
            if answer != QMessageBox.Yes:
                return False
        missing_count = 0
        required: set[str] = self.font_context["required"]
        candidates: list[FontCandidate] = self.font_context["candidates"]
        original_slots: list[str] = self.font_context["original_slots"]
        for index, candidate in enumerate(selections):
            effective = candidate or candidate_for_family(candidates, original_slots[index])
            missing_count += len(effective.missing) if effective else len(required)
        if missing_count:
            answer = QMessageBox.warning(
                self,
                "字体仍有缺字",
                f"四个字体槽合计缺少 {missing_count} 个字符覆盖。发布可以继续，但游戏可能依赖字体回退。",
                QMessageBox.Ok | QMessageBox.Cancel,
            )
            if answer != QMessageBox.Ok:
                return False
        with project_lock(self.current_manifest_path, "set-font-scheme"):
            slots = [
                {"mode": "keep"}
                if candidate is None
                else materialize_candidate(self.current_manifest_path.parent, candidate)
                for candidate in selections
            ]
            scheme: dict[str, object] = {
                "schema": 1,
                "origin": "user",
                "slots": slots,
                "coverage_ack": None,
            }
            if missing_count:
                scheme["coverage_ack"] = {
                    "fingerprint": coverage_fingerprint(required, scheme),
                    "missing_count": missing_count,
                }
            pipeline = Pipeline(
                self.current_manifest_path,
                self.settings,
                "",
                local_data_dir(),
                glossary_api_key="",
            )
            pipeline.set_font_scheme(scheme)
        if self.pipeline:
            self.pipeline.manifest = pipeline.manifest
        self.status_label.setText("字体方案已保存")
        self.font_context = None
        if refresh:
            self._load_project_view()
            self._refresh_font_tab(force=True)
        return True

    def _save_font_scheme(self) -> None:
        try:
            self._store_font_scheme(refresh=True)
        except Exception as exc:
            QMessageBox.critical(self, "无法保存字体方案", str(exc))

    def _restore_fonts(self) -> None:
        for combo in self.font_combos:
            combo.setCurrentIndex(0)
        self._save_font_scheme()

    def _apply_fonts(self) -> None:
        try:
            if not self._store_font_scheme(refresh=False):
                return
            self.font_apply_active = True
            self._start(Stage.RELEASE, switch_to_step=False)
        except Exception as exc:
            self.font_apply_active = False
            QMessageBox.critical(self, "无法应用字体", str(exc))

    def _open_settings(self, _checked=False, first_run: bool = False) -> None:
        if self.pipeline_thread and self.pipeline_thread.isRunning():
            return
        dialog = SettingsDialog(self.store, self)
        if dialog.exec() == QDialog.Accepted:
            self.settings = self.store.load()
            self._refresh_projects()
        elif first_run:
            self.status_label.setText("设置未完成")

    def _refresh_projects(self, select: str | Path | None = None) -> None:
        selected = str(select or self.settings.last_project)
        self.project_combo.blockSignals(True)
        self.project_combo.clear()
        self.project_combo.addItem("选择项目", "")
        root = Path(self.settings.projects_root)
        invalid: list[str] = []
        if root.is_dir():
            for path in sorted(root.glob("*/project.json")):
                try:
                    manifest = load_manifest(path)
                    self.project_combo.addItem(manifest.name, str(path))
                except Exception as exc:
                    invalid.append(f"{path.parent.name}: {exc}")
        index = self.project_combo.findData(selected)
        self.project_combo.setCurrentIndex(index if index >= 0 else 0)
        self.project_combo.blockSignals(False)
        self._project_changed(self.project_combo.currentIndex())
        if invalid and not self.current_manifest_path:
            self.status_label.setText(f"已拒绝 {len(invalid)} 个不兼容的项目清单，请重新创建项目")
            self.status_label.setToolTip("\n".join(invalid[:10]))

    def _project_changed(self, _index: int) -> None:
        if self.pipeline_thread and self.pipeline_thread.isRunning():
            return
        value = self.project_combo.currentData()
        self.font_context = None
        self.current_manifest_path = Path(value) if value else None
        if not self.current_manifest_path:
            self._clear_project_view()
            return
        self.settings.last_project = str(self.current_manifest_path)
        self.store.save(self.settings)
        self._load_project_view()

    @staticmethod
    def _update_stage_status(
        label: QLabel, status: StageStatus, detail: str = "", warning_count: int = 0
    ) -> None:
        display_status = {
            StageStatus.RUNNING: StageStatus.PENDING,
            StageStatus.CANCELLED: StageStatus.FAILED,
        }.get(status, status)
        warning = display_status is StageStatus.COMPLETED and warning_count > 0
        label.setText(f"已完成（{warning_count} 个警告）" if warning else STATUS_LABELS[display_status])
        label.setProperty("state", "warning" if warning else display_status.value)
        label.setToolTip(detail)
        label.style().unpolish(label)
        label.style().polish(label)

    def _clear_project_view(self) -> None:
        for stage in STAGE_ORDER:
            self._update_stage_status(self.easy_stage_status[stage], StageStatus.PENDING)
            self._update_stage_status(self.step_status_labels[stage], StageStatus.PENDING)
            self.step_buttons[stage].setEnabled(False)
            self.step_result_buttons[stage].setEnabled(False)
        self.terms_table.setRowCount(0)
        self.characters_table.setRowCount(0)
        self.progress.setValue(0)
        self.retry_button.setEnabled(False)
        self.open_logs_button.setEnabled(False)
        self.easy_summary.setText("选择项目后即可开始")
        self.start_button.setText("开始翻译")
        self.start_button.setEnabled(False)
        self._clear_font_view()

    def _load_project_view(self) -> None:
        if self.pipeline_thread and self.pipeline_thread.isRunning():
            return
        if not self.current_manifest_path:
            return
        manifest = load_manifest(self.current_manifest_path)
        log_dir = (
            self.current_manifest_path.parent
            / "versions"
            / manifest.active_version
            / "artifacts"
            / "logs"
        )
        self.open_logs_button.setEnabled(log_dir.is_dir())
        self.one_click.setChecked(manifest.run_mode is RunMode.ONE_CLICK)
        self.step_mode.setChecked(manifest.run_mode is RunMode.STEP)
        self.workflow_stack.setCurrentIndex(0 if manifest.run_mode is RunMode.ONE_CLICK else 1)
        running = bool(self.pipeline_thread and self.pipeline_thread.isRunning())
        completed = 0
        next_stage = None
        failed_stages: list[Stage] = []
        for stage in STAGE_ORDER:
            record = manifest.version.stage(stage)
            official_warning = record.artifacts.get("official_warning_count", "0")
            font_warning = record.artifacts.get("font_warning_count", "0")
            editor_warning = record.artifacts.get("editor_warning_count", "0")
            warning_count = sum(
                int(value) if value.isdigit() else 0
                for value in (official_warning, font_warning, editor_warning)
            )
            detail = record.error or record.artifacts.get(
                "official_warnings",
                record.artifacts.get(
                    "font_warnings",
                    record.artifacts.get(
                        "editor_warnings", next(iter(record.artifacts.values()), "")
                    ),
                ),
            )
            self._update_stage_status(
                self.easy_stage_status[stage], record.status, detail, warning_count
            )
            self._update_stage_status(
                self.step_status_labels[stage], record.status, detail, warning_count
            )
            self.step_buttons[stage].setEnabled(not running)
            result_path = self._stage_result_path(stage, record.artifacts)
            self.step_result_buttons[stage].setEnabled(
                not running
                and record.status is StageStatus.COMPLETED
                and result_path is not None
                and result_path.exists()
            )
            if record.status in {StageStatus.FAILED, StageStatus.CANCELLED}:
                failed_stages.append(stage)
            if record.status is StageStatus.COMPLETED:
                completed += 1
            elif next_stage is None:
                next_stage = stage
        if not running:
            if manifest.run_mode is RunMode.ONE_CLICK:
                self.progress.setRange(0, len(STAGE_ORDER))
                self.progress.setValue(completed)
            else:
                self.progress.setRange(0, 1)
                target_record = manifest.version.stage(self.active_step_stage) if self.active_step_stage else None
                self.progress.setValue(1 if target_record and target_record.status is StageStatus.COMPLETED else 0)
        if next_stage is None:
            self.easy_summary.setText("全部阶段已完成")
        else:
            self.easy_summary.setText(f"下一阶段：{STAGE_LABELS[next_stage]}")
        self.start_button.setText("继续翻译" if completed else "开始翻译")
        self.start_button.setEnabled(not running and completed < len(STAGE_ORDER))
        if manifest.run_mode is RunMode.STEP:
            if failed_stages and self.active_step_stage not in failed_stages:
                self.active_step_stage = max(
                    failed_stages,
                    key=lambda stage: manifest.version.stage(stage).finished_at,
                )
            self.retry_button.setToolTip("重试出错步骤")
            self.retry_button.setEnabled(not running and bool(failed_stages))
        else:
            self.retry_button.setToolTip("重试失败阶段")
            self.retry_button.setEnabled(not running and bool(failed_stages))
        for scope, checks in (
            (manifest.export_scope, self.export_scope_checks),
            (manifest.translation_scope, self.translation_scope_checks),
            (manifest.import_scope, self.import_scope_checks),
        ):
            for name, check in checks.items():
                check.blockSignals(True)
                check.setChecked(bool(getattr(scope, name)))
                check.blockSignals(False)
        self.exclude_large_external_files.blockSignals(True)
        self.exclude_large_external_files.setChecked(manifest.exclude_large_external_files)
        self.exclude_large_external_files.blockSignals(False)
        self.external_file_limit_kb.blockSignals(True)
        self.external_file_limit_kb.setValue(manifest.external_file_limit_kb)
        self.external_file_limit_kb.blockSignals(False)
        self._update_external_filter_controls()
        protection_controls = (
            (self.protect_external_references, manifest.import_protection.protect_external_references),
            (self.protect_paths_and_commands, manifest.import_protection.protect_paths_and_commands),
            (self.protect_logic_references, manifest.import_protection.protect_logic_references),
            (self.allow_copy_condition_groups, manifest.import_protection.allow_copy_condition_groups),
        )
        for control, checked in protection_controls:
            control.blockSignals(True)
            control.setChecked(checked)
            control.blockSignals(False)
        self.suspicious_identifier_action.blockSignals(True)
        identifier_index = self.suspicious_identifier_action.findData(
            manifest.import_protection.suspicious_identifiers
        )
        self.suspicious_identifier_action.setCurrentIndex(max(identifier_index, 0))
        self.suspicious_identifier_action.blockSignals(False)
        self.import_protection_table.setRowCount(0)
        self.import_protection_summary.setText("点击预览分析当前译文")
        self._load_glossary()
        self._refresh_font_tab()

    def _new_project(self) -> None:
        if self.pipeline_thread and self.pipeline_thread.isRunning():
            return
        errors = validate_settings(self.settings)
        if errors:
            QMessageBox.warning(self, "设置未完成", "\n".join(errors))
            self._open_settings()
            return
        game = QFileDialog.getExistingDirectory(self, "选择 WOLF 游戏目录")
        if not game:
            return
        try:
            path = create_project(self.settings.projects_root, game)
            self._refresh_projects(path)
        except Exception as exc:
            QMessageBox.critical(self, "无法创建项目", str(exc))

    def _add_version(self) -> None:
        if not self.current_manifest_path or (self.pipeline_thread and self.pipeline_thread.isRunning()):
            return
        game = QFileDialog.getExistingDirectory(self, "选择新版本游戏目录")
        if not game:
            return
        try:
            add_version(self.current_manifest_path, game)
            self._load_project_view()
        except Exception as exc:
            QMessageBox.critical(self, "无法添加版本", str(exc))

    def _set_mode(self, mode: RunMode) -> None:
        if not self.current_manifest_path or (self.pipeline_thread and self.pipeline_thread.isRunning()):
            return
        pipeline = Pipeline(
            self.current_manifest_path,
            self.settings,
            "",
            local_data_dir(),
            glossary_api_key="",
        )
        pipeline.set_run_mode(mode)

    def _select_workflow_mode(self, mode: RunMode) -> None:
        self.workflow_stack.setCurrentIndex(0 if mode is RunMode.ONE_CLICK else 1)
        self._set_mode(mode)
        self._load_project_view()

    def _save_scope(self, target: str) -> None:
        if not self.current_manifest_path or (self.pipeline_thread and self.pipeline_thread.isRunning()):
            return
        checks = {
            "export": self.export_scope_checks,
            "translation": self.translation_scope_checks,
            "import": self.import_scope_checks,
        }[target]
        if target == "import" and checks["filename"].isChecked():
            answer = QMessageBox.warning(
                self,
                "文件名导入",
                "官方工具不会重命名真实文件。仅在发布副本已准备好目标文件时启用。",
                QMessageBox.Ok | QMessageBox.Cancel,
            )
            if answer != QMessageBox.Ok:
                checks["filename"].blockSignals(True)
                checks["filename"].setChecked(False)
                checks["filename"].blockSignals(False)
        new_scope = ImportScope(**{name: check.isChecked() for name, check in checks.items()})
        pipeline = Pipeline(
            self.current_manifest_path,
            self.settings,
            "",
            local_data_dir(),
            glossary_api_key="",
        )
        if target == "export":
            pipeline.set_export_scope(
                new_scope,
                exclude_large_external_files=self.exclude_large_external_files.isChecked(),
                external_file_limit_kb=self.external_file_limit_kb.value(),
            )
        elif target == "translation":
            pipeline.set_translation_scope(new_scope)
        else:
            pipeline.set_import_scope(new_scope)
        if self.pipeline:
            self.pipeline.manifest = pipeline.manifest

    def _load_glossary(self) -> None:
        self.terms_table.setRowCount(0)
        self.characters_table.setRowCount(0)
        if not self.current_manifest_path:
            return
        path = self.current_manifest_path.parent / "glossary.json"
        if not path.is_file():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return
        for row_data in data.get("prompt_dictionary_data", []):
            row = self.terms_table.rowCount()
            self.terms_table.insertRow(row)
            for column, key in enumerate(("src", "dst", "info")):
                self.terms_table.setItem(row, column, QTableWidgetItem(str(row_data.get(key, ""))))
        character_keys = ("original_name", "translated_name", "gender", "personality", "speech_style", "additional_info")
        for row_data in data.get("characterization_data", []):
            row = self.characters_table.rowCount()
            self.characters_table.insertRow(row)
            for column, key in enumerate(character_keys):
                self.characters_table.setItem(row, column, QTableWidgetItem(str(row_data.get(key, ""))))

    @staticmethod
    def _cell(table: QTableWidget, row: int, column: int) -> str:
        item = table.item(row, column)
        return item.text().strip() if item else ""

    def _save_glossary(self) -> None:
        if not self.current_manifest_path:
            return
        if self.pipeline_thread and self.pipeline_thread.isRunning():
            QMessageBox.warning(self, "任务运行中", "请在当前阶段结束后再修改术语。")
            return
        path = self.current_manifest_path.parent / "glossary.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        terms = []
        for row in range(self.terms_table.rowCount()):
            src = self._cell(self.terms_table, row, 0)
            if src:
                terms.append({"src": src, "dst": self._cell(self.terms_table, row, 1), "info": self._cell(self.terms_table, row, 2)})
        characters = []
        keys = ("original_name", "translated_name", "gender", "personality", "speech_style", "additional_info")
        for row in range(self.characters_table.rowCount()):
            original = self._cell(self.characters_table, row, 0)
            if original:
                item = {key: self._cell(self.characters_table, row, column) for column, key in enumerate(keys)}
                item.update({"aliases": [], "age": "", "pronouns": "", "speech_quirks": ""})
                characters.append(item)
        data.update(
            {
                "prompt_dictionary_switch": True,
                "characterization_switch": True,
                "prompt_dictionary_data": terms,
                "characterization_data": characters,
            }
        )
        pipeline = Pipeline(
            self.current_manifest_path,
            self.settings,
            "",
            local_data_dir(),
            glossary_api_key="",
        )
        pipeline.set_glossary(data)
        self._load_project_view()
        self.status_label.setText("术语已保存")

    def _stop_font_scan_for_pipeline(self) -> None:
        thread = self.font_scan_thread
        if not thread or not thread.isRunning():
            return
        thread.requestInterruption()
        if not thread.wait(5000):
            raise RuntimeError("字体扫描在 5 秒内没有停止，流水线未启动。")
        if self.font_scan_thread is thread:
            self.font_scan_thread = None

    def _set_pipeline_ui_locked(self, locked: bool) -> None:
        enabled = not locked
        for control in (
            self.settings_button,
            self.project_combo,
            self.new_project_button,
            self.add_version_button,
            self.one_click,
            self.step_mode,
            self.start_button,
            self.retry_button,
            self.open_release_button,
            self.open_font_release_button,
            self.save_glossary_button,
        ):
            control.setEnabled(enabled)
        for button in (*self.step_buttons.values(), *self.step_result_buttons.values()):
            button.setEnabled(enabled)
        for checks in (
            self.export_scope_checks,
            self.translation_scope_checks,
            self.import_scope_checks,
        ):
            for check in checks.values():
                check.setEnabled(enabled)
        self.exclude_large_external_files.setEnabled(enabled)
        self.external_file_limit_kb.setEnabled(enabled)
        for control in (
            self.protect_external_references,
            self.protect_paths_and_commands,
            self.protect_logic_references,
            self.allow_copy_condition_groups,
            self.suspicious_identifier_action,
            self.preview_import_protection_button,
        ):
            control.setEnabled(enabled)
        self.export_scope_button.setEnabled(enabled)
        self.translation_scope_button.setEnabled(enabled)
        self.import_scope_button.setEnabled(enabled)
        for index in range(1, self.tabs.count()):
            self.tabs.setTabEnabled(index, enabled)
        if locked:
            self.tabs.setCurrentIndex(0)
            self._set_font_controls_enabled(False)
        else:
            self._update_external_filter_controls()
        self.stop_button.setEnabled(locked)

    def _start(self, stage: Stage | None = None, *, switch_to_step: bool = True) -> None:
        if not self.current_manifest_path or (self.pipeline_thread and self.pipeline_thread.isRunning()):
            return
        errors = validate_settings(self.settings) if stage is None else []
        if stage is Stage.EXTRACT:
            try:
                inspect_wolf_editor(self.settings.wolf_editor_path)
            except (OSError, ValueError) as error:
                errors.append(f"WOLF RPG Editor：{error}")
        if errors:
            QMessageBox.warning(self, "设置未完成", "\n".join(errors))
            return
        try:
            self._stop_font_scan_for_pipeline()
            if stage is not None and switch_to_step:
                self.active_step_stage = stage
                self.step_mode.setChecked(True)
                self.workflow_stack.setCurrentIndex(1)
                self._set_mode(RunMode.STEP)
            key = ""
            glossary_key = ""
            if stage is None or stage is Stage.TRANSLATE:
                key = self.store.api_key(self.settings)
            if stage is None or stage is Stage.GLOSSARY:
                glossary_key = self.store.glossary_api_key(self.settings)
            self.pipeline = Pipeline(
                self.current_manifest_path,
                self.settings,
                key,
                local_data_dir(),
                glossary_api_key=glossary_key,
            )
            self.pipeline_thread = PipelineThread(self.pipeline, stage)
            self.pipeline_thread.log_line.connect(self._append_log)
            self.pipeline_thread.stage_progress.connect(self._stage_progress)
            self.pipeline_thread.stage_state.connect(self._stage_state)
            self.pipeline_thread.result_ready.connect(self._pipeline_result)
            self.pipeline_thread.failed.connect(self._pipeline_failed)
            self.pipeline_thread.finished.connect(self._pipeline_finished)
            self._set_pipeline_ui_locked(True)
            self.status_label.setText(
                "正在应用字体"
                if self.font_apply_active
                else (f"正在执行：{STAGE_LABELS[stage]}" if stage is not None else "运行中")
            )
            self.pipeline_thread.start()
        except Exception as exc:
            self.pipeline_thread = None
            self.pipeline = None
            self._set_pipeline_ui_locked(False)
            self._load_project_view()
            QMessageBox.critical(self, "无法启动", str(exc))

    def _append_log(self, message: str) -> None:
        level = "INFO"
        text = message
        for prefix, candidate in (("[WARNING] ", "WARNING"), ("[ERROR] ", "ERROR")):
            if message.startswith(prefix):
                level = candidate
                text = message[len(prefix) :]
                break
        labels = {"WARNING": "警告  ", "ERROR": "错误  "}
        colors = {"INFO": "#24322c", "WARNING": "#a24625", "ERROR": "#b42318"}
        cursor = self.log_view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        text_format = QTextCharFormat()
        text_format.setForeground(QColor(colors[level]))
        if level != "INFO":
            text_format.setFontWeight(QFont.Weight.DemiBold)
        cursor.insertText(labels.get(level, "") + text + "\n", text_format)
        self.log_view.setTextCursor(cursor)
        self.log_view.ensureCursorVisible()
        if text.startswith("日志文件:"):
            self.open_logs_button.setEnabled(True)

    def _open_log_dir(self) -> None:
        if not self.current_manifest_path:
            return
        manifest = load_manifest(self.current_manifest_path)
        path = (
            self.current_manifest_path.parent
            / "versions"
            / manifest.active_version
            / "artifacts"
            / "logs"
        )
        if path.is_dir():
            os.startfile(path)
        else:
            QMessageBox.information(self, "日志目录", "当前版本还没有运行日志。")

    @staticmethod
    def _stage_result_path(stage: Stage, artifacts: dict[str, str]) -> Path | None:
        value = artifacts.get(STAGE_RESULT_ARTIFACTS[stage], "")
        if not value:
            return None
        path = Path(value)
        return path / "Game.exe" if stage is Stage.RELEASE else path

    def _open_stage_result(self, stage: Stage) -> None:
        if not self.current_manifest_path or (self.pipeline_thread and self.pipeline_thread.isRunning()):
            return
        try:
            manifest = load_manifest(self.current_manifest_path)
            record = manifest.version.stage(stage)
            path = self._stage_result_path(stage, record.artifacts)
            if record.status is not StageStatus.COMPLETED or path is None or not path.exists():
                QMessageBox.information(self, "阶段结果", "该阶段当前没有可用结果。")
                return
            if stage is Stage.GLOSSARY:
                self.tabs.setCurrentIndex(1)
            elif stage is Stage.TRANSLATE:
                subprocess.Popen(["explorer.exe", "/select,", str(path)])
            elif stage is Stage.RELEASE:
                os.startfile(str(path), cwd=str(path.parent))
            else:
                os.startfile(str(path))
        except Exception as exc:
            QMessageBox.critical(self, "无法打开阶段结果", str(exc))

    def _stage_progress(self, current: int, total: int, _stage: str) -> None:
        if total == 0:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, total)
            self.progress.setValue(current)

    def _stage_state(self, event: object) -> None:
        if not isinstance(event, PipelineStateEvent):
            return
        self._update_stage_status(
            self.easy_stage_status[event.stage],
            event.status,
            event.detail,
            event.warnings,
        )
        self._update_stage_status(
            self.step_status_labels[event.stage],
            event.status,
            event.detail,
            event.warnings,
        )
        if event.status is StageStatus.RUNNING:
            self.easy_summary.setText(f"正在执行：{STAGE_LABELS[event.stage]}")
        elif event.status is StageStatus.COMPLETED:
            self.easy_summary.setText(f"已完成：{STAGE_LABELS[event.stage]}")

    def _pipeline_result(self, result: str) -> None:
        target = self.pipeline_thread.stage if self.pipeline_thread else None
        self.status_label.setText(
            "字体已应用"
            if self.font_apply_active
            else (f"已完成：{STAGE_LABELS[target]}" if target is not None else "已完成")
        )

    def _pipeline_failed(self, detail: str) -> None:
        target = self.pipeline_thread.stage if self.pipeline_thread else None
        self.status_label.setText(
            "字体应用错误"
            if self.font_apply_active
            else (f"出现错误：{STAGE_LABELS[target]}" if target is not None else "出现错误")
        )
        if not self.font_apply_active:
            self.tabs.setCurrentIndex(0)
        title = "字体应用错误" if self.font_apply_active else ("步骤执行错误" if target is not None else "流水线失败")
        QMessageBox.critical(self, title, detail.splitlines()[-1] if detail.splitlines() else detail)

    def _pipeline_finished(self) -> None:
        self.pipeline_thread = None
        self.pipeline = None
        self.font_apply_active = False
        self.font_context = None
        self._set_pipeline_ui_locked(False)
        self._load_project_view()

    def _stop(self) -> None:
        if self.pipeline:
            self.pipeline.cancel()
            self.status_label.setText("正在停止")

    def _retry(self) -> None:
        if not self.current_manifest_path:
            return
        try:
            manifest = load_manifest(self.current_manifest_path)
            if manifest.run_mode is RunMode.STEP:
                failed = [
                    stage
                    for stage in STAGE_ORDER
                    if manifest.version.stage(stage).status in {StageStatus.FAILED, StageStatus.CANCELLED}
                ]
                if not failed:
                    return
                target = self.active_step_stage if self.active_step_stage in failed else max(
                    failed,
                    key=lambda stage: manifest.version.stage(stage).finished_at,
                )
                self._start(target)
                return
            pipeline = Pipeline(
                self.current_manifest_path,
                self.settings,
                self.store.api_key(self.settings),
                local_data_dir(),
                glossary_api_key=self.store.glossary_api_key(self.settings),
            )
            pipeline.retry_failed()
            self._load_project_view()
            self._start()
        except Exception as exc:
            QMessageBox.critical(self, "无法重试", str(exc))

    def _open_release(self) -> None:
        if not self.current_manifest_path or (self.pipeline_thread and self.pipeline_thread.isRunning()):
            return
        manifest = load_manifest(self.current_manifest_path)
        path = manifest.version.stage(Stage.RELEASE).artifacts.get("release", "")
        if path and Path(path).is_dir():
            os.startfile(path)
        else:
            QMessageBox.information(self, "发布目录", "当前版本尚未发布。")

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.pipeline_thread and self.pipeline_thread.isRunning():
            if QMessageBox.question(self, "退出", "任务仍在运行，停止并退出？") != QMessageBox.Yes:
                event.ignore()
                return
            self.pipeline.cancel()
            self.pipeline_thread.wait(5000)
            if self.pipeline_thread.isRunning():
                QMessageBox.warning(self, "仍在停止", "外部进程尚未完全退出，请稍后再次关闭窗口。")
                event.ignore()
                return
        if self.font_scan_thread and self.font_scan_thread.isRunning():
            self.font_scan_thread.requestInterruption()
            self.font_scan_thread.wait(5000)
            if self.font_scan_thread.isRunning():
                event.ignore()
                return
        self._release_font_previews()
        event.accept()


STYLE = """
QWidget { color: #18211b; background: #f6f8f6; font-family: "Segoe UI", "Microsoft YaHei UI"; font-size: 14px; }
QMainWindow, QDialog { background: #f6f8f6; }
QLabel#brand { font-size: 25px; font-weight: 700; color: #142219; }
QLabel#dialogTitle { font-size: 20px; font-weight: 650; }
QLabel#panelTitle { font-weight: 600; color: #24342a; }
QLabel#secondaryText { color: #647168; }
QLabel#warningText { color: #a24625; padding: 2px 0 10px 26px; }
QLineEdit, QComboBox, QSpinBox, QPlainTextEdit, QTableWidget {
    background: #ffffff; border: 1px solid #cfd7d1; border-radius: 6px; padding: 7px;
    selection-background-color: #dceae0; selection-color: #18211b;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QPlainTextEdit:focus { border: 1px solid #267247; }
QPushButton, QToolButton {
    background: #ffffff; border: 1px solid #c5cec7; border-radius: 6px; padding: 7px 12px; min-height: 20px;
}
QPushButton:hover, QToolButton:hover { background: #eef3ef; border-color: #91a398; }
QPushButton:pressed, QToolButton:pressed { background: #e2eae4; }
QPushButton:disabled, QToolButton:disabled { color: #9aa49d; background: #edf0ed; }
QPushButton#primaryButton { background: #246b43; color: white; border-color: #246b43; font-weight: 600; }
QPushButton#primaryButton:hover { background: #1d5b38; }
QPushButton#segment { border-radius: 0; min-width: 48px; }
QPushButton#segment:checked { background: #dceae0; border-color: #5d8d6f; color: #17482d; }
QFrame#stageNode { background: #ffffff; border: 1px solid #d9e0db; border-radius: 6px; }
QFrame#stageRow { background: transparent; border: 0; border-bottom: 1px solid #e2e7e3; }
QLabel#stepNumber { color: #637068; font-size: 12px; font-weight: 600; }
QLabel#stepNumberLarge { background: #e1eee5; color: #1d5b38; border-radius: 15px; font-weight: 700; }
QLabel#stageTitle { font-weight: 600; color: #18211b; }
QLabel#stageArrow { color: #8b9890; font-size: 18px; }
QLabel#stageStatus { color: #6d776f; font-size: 12px; }
QLabel#stageStatus[state="running"] { color: #1769aa; font-weight: 600; }
QLabel#stageStatus[state="completed"] { color: #247047; font-weight: 600; }
QLabel#stageStatus[state="warning"] { color: #a24625; font-weight: 600; }
QLabel#stageStatus[state="failed"], QLabel#stageStatus[state="cancelled"] { color: #b14132; font-weight: 600; }
QTabWidget::pane { border: 1px solid #d5ddd7; background: #ffffff; border-radius: 6px; }
QTabBar::tab { background: transparent; padding: 9px 18px; color: #5b685f; }
QTabBar::tab:selected { color: #17482d; border-bottom: 2px solid #267247; font-weight: 600; }
QHeaderView::section { background: #edf1ee; border: 0; border-bottom: 1px solid #d4dbd6; padding: 8px; font-weight: 600; }
QTableWidget { border: 0; border-radius: 0; gridline-color: #e4e9e5; alternate-background-color: #f8faf8; }
QProgressBar { background: #e2e7e3; border: 0; border-radius: 3px; height: 6px; }
QProgressBar::chunk { background: #2b7a4c; border-radius: 3px; }
QCheckBox { spacing: 10px; padding: 6px 0; }
"""


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--console-capture-worker":
        from wolf_tools import console_capture_worker

        return console_capture_worker(int(sys.argv[2]), sys.argv[3])
    app = QApplication(sys.argv)
    app.setApplicationName("WOLFLator")
    app.setOrganizationName("WOLFLator")
    app.setStyle("Fusion")
    font_path = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "msyh.ttc"
    if font_path.is_file():
        font_id = QFontDatabase.addApplicationFont(str(font_path))
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            app.setFont(QFont(families[0], 10))
    app.setStyleSheet(STYLE)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
