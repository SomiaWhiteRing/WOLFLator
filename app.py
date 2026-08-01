from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import traceback
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QSortFilterProxyModel,
    QThread,
    QTimer,
    Qt,
    QUrl,
    Signal,
)
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
    QAbstractItemView,
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
    QSizePolicy,
    QSplitter,
    QSpinBox,
    QStackedWidget,
    QStyle,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTableView,
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
    font_file_faces,
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
    TranslationItem,
    default_export_scope,
    default_processing_scope,
)
from formats import PROJECT_SCHEMA
from pipeline import Pipeline, PipelineStateEvent, add_version, create_project, load_manifest
from proofread import (
    load_report,
    proofread_paths,
    report_is_stale,
    run_project_proofread,
    save_report,
)
from safe_io import project_lock
from settings import SettingsStore, local_data_dir, validate_settings
from wolf_editor import (
    EDITOR_DOWNLOAD_URL,
    analyze_translation_safety,
    inspect_wolf_editor,
    install_supported_editor,
)
from wolf_tools import (
    analyze_import_protection,
    imported_display_texts,
    load_import_protection,
    load_items,
    protect_control_tokens,
    read_font_slots,
    selected_translation_requirements,
    sha256_file,
)
from wolf_analysis import load_editor_analysis


from ui_workers import (
    ApiTestThread, EditorInstallThread, FontScanThread, InstallThread, PipelineThread,
    ProofreadThread, _completed_import_protection, _font_required_characters,
    _load_editor_analysis, _translation_safety_for_manifest,
)
from ui_settings import SettingsDialog, _configure_table, _path_row
from ui_translation import TranslationEditModel

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


def _qt_preview_family(
    candidate: FontCandidate | None,
    fallback: str,
    available: dict[str, str] | None = None,
) -> str:
    if candidate is None:
        return fallback
    if available is None:
        available = {family.casefold(): family for family in QFontDatabase.families()}
    for alias in (candidate.preview_family, candidate.family, *candidate.aliases):
        if resolved := available.get(alias.casefold()):
            return resolved
    return fallback


def _qt_preview_font(
    candidate: FontCandidate | None,
    fallback: str,
    available: dict[str, str] | None = None,
) -> QFont:
    font = QFont(_qt_preview_family(candidate, fallback, available), 12)
    if candidate and candidate.style and candidate.weight and not candidate.weight_range:
        font.setStyleName(candidate.style)
    return font


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
    Stage.VALIDATE: "编辑译文",
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
    "logic_derived_value": "WOLF 派生条件值保留",
    "logic_untracked": "WOLF 条件来源未追踪",
    "logic_blocking": "WOLF 条件来源阻断",
    "logic_unresolved_scope": "WOLF 未知作用域自动保留",
    "logic_state_write": "WOLF 跨事件状态写入",
    "logic_safety": "WOLF 静态安全保护",
    "not_proven_safe": "未获得静态安全证明",
    "resource_reference": "WOLF 资源、标签或调用引用",
    "suspicious_identifier": "可疑标识符",
    "copy_mixed_scope_group": "COPY-FROM 条件/混合范围组",
}


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.store = SettingsStore()
        self.settings = self.store.load()
        self.pipeline: Pipeline | None = None
        self.pipeline_thread: PipelineThread | None = None
        self.proofread_thread: ProofreadThread | None = None
        self.proofread_report: dict[str, object] | None = None
        self.proofread_report_path: Path | None = None
        self.proofread_run_result: tuple[str, str] | None = None
        self._proofread_loading = False
        self.edit_source_path: Path | None = None
        self.edit_source_sha256 = ""
        self.edit_source_identity: tuple[str, int, int] | None = None
        self.edit_source_row = -1
        self.edit_action_status = ""
        self._edit_loading = False
        self.font_scan_thread: FontScanThread | None = None
        self.font_context: dict[str, object] | None = None
        self.font_apply_active = False
        self.font_application_ids: list[int] = []
        self.font_application_paths: set[str] = set()
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
        self.settings_button.setText("⚙️")
        self.settings_button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.settings_button.setAccessibleName("设置")
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
        self.proofread_tab_index = self.tabs.addTab(self._proofread_tab(), "校对")
        self.edit_tab_index = self.tabs.addTab(self._edit_tab(), "编辑")
        self.tabs.addTab(self._scope_tab(), "范围")
        self.font_tab_index = self.tabs.addTab(self._font_tab(), "修改字体")
        self.tabs.currentChanged.connect(self._main_tab_changed)
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
        self.step_checks: dict[Stage, QCheckBox] = {}
        self.step_buttons: dict[Stage, QPushButton] = {}
        self.step_result_buttons: dict[Stage, QPushButton] = {}
        for index, stage in enumerate(STAGE_ORDER, start=1):
            row = QFrame()
            row.setObjectName("stageRow")
            row_layout = QGridLayout(row)
            row_layout.setContentsMargins(8, 6, 8, 6)
            row_layout.setHorizontalSpacing(8)
            selected = QCheckBox()
            selected.setAccessibleName(f"选择{STAGE_LABELS[stage]}")
            selected.toggled.connect(self._update_step_range_controls)
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
            row_layout.addWidget(selected, 0, 0)
            row_layout.addWidget(number, 0, 1)
            row_layout.addWidget(title, 0, 2)
            row_layout.addWidget(description, 0, 3)
            row_layout.addWidget(status, 0, 4)
            row_layout.addWidget(result_button, 0, 5)
            row_layout.addWidget(run_button, 0, 6)
            row_layout.setColumnStretch(3, 1)
            self.step_checks[stage] = selected
            self.step_status_labels[stage] = status
            self.step_buttons[stage] = run_button
            self.step_result_buttons[stage] = result_button
            layout.addWidget(row)
        layout.addStretch(1)
        range_row = QHBoxLayout()
        self.step_range_summary = QLabel("未选择步骤")
        self.step_range_summary.setObjectName("secondaryText")
        self.run_range_button = QPushButton("连续执行")
        self.run_range_button.setObjectName("primaryButton")
        self.run_range_button.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.run_range_button.setEnabled(False)
        self.run_range_button.clicked.connect(self._start_selected_steps)
        range_row.addWidget(self.step_range_summary)
        range_row.addStretch(1)
        range_row.addWidget(self.run_range_button)
        layout.addLayout(range_row)
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

    def _proofread_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        self.proofread_gate_label = QLabel("完成 AI 翻译后即可使用校对功能")
        self.proofread_gate_label.setObjectName("secondaryText")
        self.proofread_gate_label.setAlignment(Qt.AlignCenter)
        self.proofread_gate_label.setMinimumHeight(34)
        layout.addWidget(self.proofread_gate_label)

        self.proofread_content = QWidget()
        content = QVBoxLayout(self.proofread_content)
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(8)

        self.proofread_options = QWidget()
        options = QVBoxLayout(self.proofread_options)
        options.setContentsMargins(0, 0, 0, 0)
        options.setSpacing(6)
        header = QHBoxLayout()
        title = QLabel("AI 校对")
        title.setObjectName("panelTitle")
        header.addWidget(title)
        self.proofread_summary = QLabel("受影响 0 · 高 0 · 中 0 · 低 0 · 已采纳 0 · 失败批次 0")
        self.proofread_summary.setObjectName("secondaryText")
        header.addWidget(self.proofread_summary)
        header.addStretch(1)
        self.proofread_settings_toggle = QToolButton()
        self.proofread_settings_toggle.setText("校对设置")
        self.proofread_settings_toggle.setArrowType(Qt.RightArrow)
        self.proofread_settings_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.proofread_settings_toggle.setCheckable(True)
        self.proofread_settings_toggle.toggled.connect(self._toggle_proofread_settings)
        header.addWidget(self.proofread_settings_toggle)

        self.start_proofread_button = QToolButton()
        self.start_proofread_button.setIcon(self.style().standardIcon(QStyle.SP_MediaPlay))
        self.start_proofread_button.setToolTip("开始校对")
        self.start_proofread_button.setObjectName("primaryButton")
        self.start_proofread_button.clicked.connect(self._start_proofread)
        self.stop_proofread_button = QToolButton()
        self.stop_proofread_button.setIcon(self.style().standardIcon(QStyle.SP_MediaStop))
        self.stop_proofread_button.setToolTip("停止校对")
        self.stop_proofread_button.setEnabled(False)
        self.stop_proofread_button.clicked.connect(self._stop_proofread)
        self.rerun_proofread_button = QToolButton()
        self.rerun_proofread_button.setIcon(self.style().standardIcon(QStyle.SP_BrowserReload))
        self.rerun_proofread_button.setToolTip("重新校对")
        self.rerun_proofread_button.clicked.connect(self._start_proofread)
        for button in (
            self.start_proofread_button,
            self.stop_proofread_button,
            self.rerun_proofread_button,
        ):
            button.setFixedSize(34, 34)
            header.addWidget(button)
        options.addLayout(header)

        self.proofread_settings_panel = QWidget()
        configuration = QHBoxLayout(self.proofread_settings_panel)
        configuration.setContentsMargins(0, 0, 0, 0)
        self.proofread_mode = QComboBox()
        self.proofread_mode.addItem("规则 + AI", "rules_ai")
        self.proofread_mode.addItem("仅规则", "rules")
        self.proofread_model = QLabel("-")
        self.proofread_model.setObjectName("secondaryText")
        self.proofread_batch_size = QSpinBox()
        self.proofread_batch_size.setRange(1, 100)
        self.proofread_context_lines = QSpinBox()
        self.proofread_context_lines.setRange(0, 20)
        self.proofread_confidence = QSpinBox()
        self.proofread_confidence.setRange(0, 100)
        self.proofread_confidence.setSuffix("%")
        configuration.addWidget(QLabel("检查方式"))
        configuration.addWidget(self.proofread_mode)
        configuration.addWidget(QLabel("模型"))
        configuration.addWidget(self.proofread_model)
        configuration.addStretch(1)
        for label, control in (
            ("每批", self.proofread_batch_size),
            ("上下文", self.proofread_context_lines),
            ("置信度", self.proofread_confidence),
        ):
            configuration.addWidget(QLabel(label))
            configuration.addWidget(control)
        self.proofread_settings_panel.hide()
        options.addWidget(self.proofread_settings_panel)
        content.addWidget(self.proofread_options)

        self.proofread_progress = QProgressBar()
        self.proofread_progress.setRange(0, 1)
        self.proofread_progress.setValue(0)
        self.proofread_progress.setFormat("尚未校对")
        content.addWidget(self.proofread_progress)

        filters = QHBoxLayout()
        self.proofread_search = QLineEdit()
        self.proofread_search.setPlaceholderText("搜索原文、译文、代码或问题")
        self.proofread_severity_filter = QComboBox()
        self.proofread_severity_filter.addItem("全部严重程度", "all")
        self.proofread_severity_filter.addItem("高风险", "high")
        self.proofread_severity_filter.addItem("中风险", "medium")
        self.proofread_severity_filter.addItem("低风险", "low")
        self.proofread_type_filter = QComboBox()
        self.proofread_type_filter.addItem("全部问题类型", "all")
        self.proofread_decision_filter = QComboBox()
        self.proofread_decision_filter.addItem("全部处理状态", "all")
        self.proofread_decision_filter.addItem("待处理", "pending")
        self.proofread_decision_filter.addItem("已采纳", "accept")
        self.proofread_decision_filter.addItem("保留现译", "keep")
        self.proofread_search.textChanged.connect(self._filter_proofread_rows)
        filters.addWidget(self.proofread_search, 1)
        for control in (
            self.proofread_severity_filter,
            self.proofread_type_filter,
            self.proofread_decision_filter,
        ):
            control.setMaximumWidth(150)
            control.currentIndexChanged.connect(self._filter_proofread_rows)
            filters.addWidget(control)
        content.addLayout(filters)

        splitter = QSplitter(Qt.Horizontal)
        list_panel = QWidget()
        list_layout = QVBoxLayout(list_panel)
        list_layout.setContentsMargins(0, 0, 4, 0)
        list_layout.setSpacing(5)
        list_header = QHBoxLayout()
        list_title = QLabel("问题列表")
        list_title.setObjectName("panelTitle")
        list_header.addWidget(list_title)
        list_header.addStretch(1)
        self.proofread_accept_selected = QPushButton("批量采纳")
        self.proofread_accept_selected.clicked.connect(lambda: self._decide_selected_proofread("accept"))
        self.proofread_keep_selected = QPushButton("批量保留")
        self.proofread_keep_selected.clicked.connect(lambda: self._decide_selected_proofread("keep"))
        list_header.addWidget(self.proofread_accept_selected)
        list_header.addWidget(self.proofread_keep_selected)
        list_layout.addLayout(list_header)

        self.proofread_table = QTableWidget(0, 5)
        self.proofread_table.setHorizontalHeaderLabels(
            ["选择", "风险", "WOLF 代码", "问题类型", "状态"]
        )
        _configure_table(self.proofread_table)
        self.proofread_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.proofread_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.proofread_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.proofread_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.proofread_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.proofread_table.itemSelectionChanged.connect(self._show_proofread_entry)
        list_layout.addWidget(self.proofread_table, 1)
        list_panel.setMinimumWidth(330)
        splitter.addWidget(list_panel)

        review = QWidget()
        review_layout = QVBoxLayout(review)
        review_layout.setContentsMargins(6, 0, 0, 0)
        review_layout.setSpacing(5)
        review_header = QHBoxLayout()
        review_title = QLabel("逐条审阅")
        review_title.setObjectName("panelTitle")
        review_header.addWidget(review_title)
        self.proofread_entry_status = QLabel("选择左侧问题")
        self.proofread_entry_status.setObjectName("secondaryText")
        review_header.addWidget(self.proofread_entry_status)
        review_header.addStretch(1)
        review_layout.addLayout(review_header)
        texts = QHBoxLayout()
        self.proofread_original = QPlainTextEdit()
        self.proofread_current = QPlainTextEdit()
        for title, editor in (("原文", self.proofread_original), ("当前译文", self.proofread_current)):
            column = QVBoxLayout()
            column.addWidget(QLabel(title))
            editor.setReadOnly(True)
            editor.setMinimumHeight(42)
            editor.setMaximumHeight(72)
            column.addWidget(editor)
            texts.addLayout(column, 1)
        review_layout.addLayout(texts)
        review_layout.addWidget(QLabel("问题说明"))
        self.proofread_issues = QPlainTextEdit()
        self.proofread_issues.setReadOnly(True)
        self.proofread_issues.setMinimumHeight(50)
        self.proofread_issues.setMaximumHeight(84)
        review_layout.addWidget(self.proofread_issues)
        suggestion_header = QHBoxLayout()
        suggestion_title = QLabel("建议译文（可编辑）")
        suggestion_title.setObjectName("panelTitle")
        suggestion_header.addWidget(suggestion_title)
        suggestion_header.addStretch(1)
        self.proofread_accept = QPushButton("采纳")
        self.proofread_accept.setObjectName("primaryButton")
        self.proofread_accept.clicked.connect(lambda: self._decide_current_proofread("accept"))
        self.proofread_keep = QPushButton("保留现译")
        self.proofread_keep.clicked.connect(lambda: self._decide_current_proofread("keep"))
        self.proofread_reset = QPushButton("撤销决定")
        self.proofread_reset.clicked.connect(lambda: self._decide_current_proofread("pending"))
        suggestion_header.addWidget(self.proofread_accept)
        suggestion_header.addWidget(self.proofread_keep)
        suggestion_header.addWidget(self.proofread_reset)
        review_layout.addLayout(suggestion_header)
        self.proofread_suggestion = QPlainTextEdit()
        self.proofread_suggestion.setObjectName("proofreadSuggestion")
        self.proofread_suggestion.setPlaceholderText("未生成完整建议时，可在此手动修订")
        self.proofread_suggestion.setMinimumHeight(64)
        self.proofread_suggestion.textChanged.connect(self._save_proofread_draft)
        review_layout.addWidget(self.proofread_suggestion, 1)
        review.setMinimumWidth(390)
        splitter.addWidget(review)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([410, 620])
        content.addWidget(splitter, 1)

        bottom = QHBoxLayout()
        self.open_proofread_report_button = QToolButton()
        self.open_proofread_report_button.setIcon(self.style().standardIcon(QStyle.SP_FileDialogDetailedView))
        self.open_proofread_report_button.setToolTip("打开校对报告")
        self.open_proofread_report_button.setAccessibleName("打开校对报告")
        self.open_proofread_report_button.clicked.connect(self._open_proofread_report)
        self.open_proofread_log_button = QToolButton()
        self.open_proofread_log_button.setIcon(self.style().standardIcon(QStyle.SP_FileIcon))
        self.open_proofread_log_button.setToolTip("打开校对日志")
        self.open_proofread_log_button.setAccessibleName("打开校对日志")
        self.open_proofread_log_button.clicked.connect(self._open_proofread_log)
        self.restore_ai_translation_button = QPushButton("恢复 AI 原译")
        self.restore_ai_translation_button.clicked.connect(self._restore_ai_translation)
        self.apply_proofread_button = QPushButton("应用已采纳修改")
        self.apply_proofread_button.setObjectName("primaryButton")
        self.apply_proofread_button.clicked.connect(self._apply_proofread)
        bottom.addWidget(self.open_proofread_report_button)
        bottom.addWidget(self.open_proofread_log_button)
        bottom.addStretch(1)
        bottom.addWidget(self.restore_ai_translation_button)
        bottom.addWidget(self.apply_proofread_button)
        content.addLayout(bottom)
        layout.addWidget(self.proofread_content, 1)

        for control in (
            self.proofread_mode,
            self.proofread_batch_size,
            self.proofread_context_lines,
            self.proofread_confidence,
        ):
            if isinstance(control, QComboBox):
                control.currentIndexChanged.connect(self._save_proofread_settings)
            else:
                control.valueChanged.connect(self._save_proofread_settings)
        return page

    def _edit_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        self.edit_gate_label = QLabel("完成 AI 翻译后即可编辑译文")
        self.edit_gate_label.setObjectName("secondaryText")
        self.edit_gate_label.setAlignment(Qt.AlignCenter)
        self.edit_gate_label.setMinimumHeight(34)
        layout.addWidget(self.edit_gate_label)

        self.edit_content = QWidget()
        content = QVBoxLayout(self.edit_content)
        content.setContentsMargins(0, 0, 0, 0)
        content.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("译文编辑")
        title.setObjectName("panelTitle")
        self.edit_summary = QLabel("共 0 条 · 匹配 0 条 · 已修改 0 条")
        self.edit_summary.setObjectName("secondaryText")

        self.edit_search_toolbar = QWidget()
        self.edit_search_toolbar.setMinimumWidth(380)
        self.edit_search_toolbar.setMaximumWidth(620)
        toolbar_layout = QHBoxLayout(self.edit_search_toolbar)
        toolbar_layout.setContentsMargins(0, 0, 0, 0)
        toolbar_layout.setSpacing(4)
        self.edit_replace_toggle = QToolButton()
        self.edit_replace_toggle.setObjectName("editReplaceToggle")
        self.edit_replace_toggle.setText("▼")
        self.edit_replace_toggle.setCheckable(True)
        self.edit_replace_toggle.setFixedSize(36, 36)
        toggle_font = self.edit_replace_toggle.font()
        toggle_font.setPointSize(15)
        self.edit_replace_toggle.setFont(toggle_font)
        self.edit_replace_toggle.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.edit_replace_toggle.setToolTip("展开替换")
        self.edit_replace_toggle.setAccessibleName("展开替换")
        self.edit_replace_toggle.toggled.connect(self._toggle_edit_replace)
        toolbar_layout.addWidget(self.edit_replace_toggle, 0, Qt.AlignTop)

        search_fields = QWidget()
        search_fields_layout = QVBoxLayout(search_fields)
        search_fields_layout.setContentsMargins(0, 0, 0, 0)
        search_fields_layout.setSpacing(4)
        self.edit_search = QLineEdit()
        self.edit_search.setObjectName("editSearch")
        self.edit_search.setFixedHeight(36)
        self.edit_search.setPlaceholderText("搜索原文、译文、WOLF 代码或类型")
        self.edit_search.setClearButtonEnabled(True)
        self.edit_search.textChanged.connect(self._filter_edit_rows)
        search_fields_layout.addWidget(self.edit_search)

        self.edit_replace_popup = QFrame(self.edit_content)
        self.edit_replace_popup.setObjectName("editReplacePopup")
        replace_layout = QHBoxLayout(self.edit_replace_popup)
        replace_layout.setContentsMargins(0, 0, 0, 0)
        replace_layout.setSpacing(4)
        replace_layout.addSpacing(40)
        self.edit_replace = QLineEdit()
        self.edit_replace.setObjectName("editReplace")
        self.edit_replace.setFixedHeight(36)
        self.edit_replace.setPlaceholderText("替换译文中的匹配项")
        self.edit_replace.setClearButtonEnabled(True)
        self.edit_replace.textChanged.connect(self._update_edit_replace_actions)
        self.edit_replace_one = QPushButton("替换")
        self.edit_replace_one.setObjectName("editReplaceButton")
        self.edit_replace_one.setFixedHeight(36)
        self.edit_replace_one.setToolTip("替换当前译文中的第一个匹配项")
        self.edit_replace_one.clicked.connect(self._replace_current_translation)
        self.edit_replace_all = QPushButton("全部替换")
        self.edit_replace_all.setObjectName("editReplaceButton")
        self.edit_replace_all.setFixedHeight(36)
        self.edit_replace_all.setToolTip("替换所有译文中的匹配项")
        self.edit_replace_all.clicked.connect(self._replace_all_translations)
        replace_layout.addWidget(self.edit_replace, 1)
        replace_layout.addWidget(self.edit_replace_one)
        replace_layout.addWidget(self.edit_replace_all)
        self.edit_replace_popup.hide()
        toolbar_layout.addWidget(search_fields, 1)

        header.addWidget(title)
        header.addWidget(self.edit_summary)
        header.addStretch(1)
        header.addWidget(self.edit_search_toolbar, 2)
        content.addLayout(header)

        splitter = QSplitter(Qt.Horizontal)
        self.edit_model = TranslationEditModel(self)
        self.edit_proxy = QSortFilterProxyModel(self)
        self.edit_proxy.setSourceModel(self.edit_model)
        self.edit_proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.edit_proxy.setFilterKeyColumn(-1)
        self.edit_proxy.setDynamicSortFilter(False)
        self.edit_table = QTableView()
        self.edit_table.setModel(self.edit_proxy)
        self.edit_table.setAlternatingRowColors(True)
        self.edit_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.edit_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.edit_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.edit_table.verticalHeader().setVisible(False)
        self.edit_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.edit_table.selectionModel().currentRowChanged.connect(self._show_edit_entry)
        splitter.addWidget(self.edit_table)

        editor = QWidget()
        editor_layout = QVBoxLayout(editor)
        editor_layout.setContentsMargins(8, 0, 0, 0)
        editor_layout.setSpacing(5)
        editor_header = QHBoxLayout()
        editor_title = QLabel("逐条编辑")
        editor_title.setObjectName("panelTitle")
        self.edit_entry_status = QLabel("选择左侧译文")
        self.edit_entry_status.setObjectName("secondaryText")
        editor_header.addWidget(editor_title)
        editor_header.addWidget(self.edit_entry_status)
        editor_header.addStretch(1)
        editor_layout.addLayout(editor_header)

        editor_layout.addWidget(QLabel("原文"))
        self.edit_original = QPlainTextEdit()
        self.edit_original.setReadOnly(True)
        self.edit_original.setMinimumHeight(72)
        self.edit_original.setMaximumHeight(130)
        editor_layout.addWidget(self.edit_original)
        editor_layout.addWidget(QLabel("上下文"))
        self.edit_context = QPlainTextEdit()
        self.edit_context.setReadOnly(True)
        self.edit_context.setMinimumHeight(48)
        self.edit_context.setMaximumHeight(90)
        editor_layout.addWidget(self.edit_context)

        translation_header = QHBoxLayout()
        translation_title = QLabel("译文")
        translation_title.setObjectName("panelTitle")
        self.edit_reset_current = QPushButton("恢复本条")
        self.edit_reset_current.clicked.connect(self._reset_current_edit)
        translation_header.addWidget(translation_title)
        translation_header.addStretch(1)
        translation_header.addWidget(self.edit_reset_current)
        editor_layout.addLayout(translation_header)
        self.edit_translation = QPlainTextEdit()
        self.edit_translation.setObjectName("translationEditor")
        self.edit_translation.setPlaceholderText("输入润色后的完整译文")
        self.edit_translation.textChanged.connect(self._edit_translation_changed)
        editor_layout.addWidget(self.edit_translation, 1)
        splitter.addWidget(editor)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([650, 430])
        content.addWidget(splitter, 1)

        bottom = QHBoxLayout()
        self.edit_status = QLabel("")
        self.edit_status.setObjectName("secondaryText")
        self.edit_discard = QPushButton("撤销未保存修改")
        self.edit_discard.clicked.connect(self._discard_translation_edits)
        self.edit_save = QPushButton("保存修改")
        self.edit_save.setObjectName("primaryButton")
        self.edit_save.clicked.connect(self._save_translation_edits)
        bottom.addWidget(self.edit_status, 1)
        bottom.addWidget(self.edit_discard)
        bottom.addWidget(self.edit_save)
        content.addLayout(bottom)
        layout.addWidget(self.edit_content, 1)
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
        if target == "import":
            panel_layout = QHBoxLayout(panel)
            panel_layout.setContentsMargins(0, 6, 0, 0)
            panel_layout.setSpacing(24)

            self.import_scope_column = QWidget()
            self.import_scope_column.setMinimumWidth(250)
            self.import_scope_column.setMaximumWidth(310)
            scope_layout = QVBoxLayout(self.import_scope_column)
            scope_layout.setContentsMargins(0, 0, 0, 0)
            scope_layout.setSpacing(4)
            scope_title = QLabel("导入内容")
            scope_title.setObjectName("panelTitle")
            scope_layout.addWidget(scope_title)
            panel_layout.addWidget(self.import_scope_column)

            self.import_protection_column = QWidget()
            protection_layout = QVBoxLayout(self.import_protection_column)
            protection_layout.setContentsMargins(0, 0, 0, 0)
            protection_layout.setSpacing(6)
            panel_layout.addWidget(self.import_protection_column, 1)
        else:
            panel_layout = QVBoxLayout(panel)
            panel_layout.setContentsMargins(0, 6, 0, 0)
            scope_layout = panel_layout
        checks = {
            "display": QCheckBox("显示文本"),
            "external": QCheckBox("外部 TXT / CSV"),
            "optional_name": QCheckBox("数据库、地图和事件名称"),
            "halfwidth": QCheckBox("纯半角字符串"),
            "filename": QCheckBox("文件名引用"),
        }
        defaults = default_export_scope() if target == "export" else default_processing_scope()
        for name, check in checks.items():
            check.setChecked(bool(getattr(defaults, name)))
        for key, check in checks.items():
            check.toggled.connect(lambda _checked=False, scope_target=target: self._save_scope(scope_target))
            scope_layout.addWidget(check)
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
                scope_layout.addWidget(self.external_filter_options)
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
                warning.setWordWrap(True)
                scope_layout.addWidget(warning)
        if target == "import":
            scope_layout.addStretch(1)
            self._add_import_protection_controls(protection_layout)
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
        protection_checks = QGridLayout()
        protection_checks.setContentsMargins(0, 0, 0, 0)
        protection_checks.setHorizontalSpacing(18)
        protection_checks.setVerticalSpacing(0)
        protection_checks.addWidget(self.protect_external_references, 0, 0)
        protection_checks.addWidget(self.protect_paths_and_commands, 0, 1)
        protection_checks.addWidget(self.protect_logic_references, 1, 0, 1, 2)
        protection_checks.addWidget(self.allow_copy_condition_groups, 2, 0, 1, 2)
        layout.addLayout(protection_checks)
        copy_note = QLabel("COPY-FROM 选项会改变 AiNiee 输入；修改后将重置术语及后续阶段。")
        copy_note.setObjectName("secondaryText")
        copy_note.setWordWrap(True)
        layout.addWidget(copy_note)

        policy_row = QGridLayout()
        policy_row.setContentsMargins(0, 0, 0, 0)
        policy_row.setHorizontalSpacing(8)
        policy_row.addWidget(QLabel("未知事件逻辑"), 0, 0)
        self.logic_unknown_policy = QComboBox()
        self.logic_unknown_policy.addItem("严格：阻止导入", "block")
        self.logic_unknown_policy.addItem("保守：保留风险原文后继续", "warn")
        self.logic_unknown_policy.currentIndexChanged.connect(
            self._save_import_protection
        )
        self.protect_logic_references.toggled.connect(
            self.logic_unknown_policy.setEnabled
        )
        policy_row.addWidget(self.logic_unknown_policy, 0, 1)
        policy_row.addWidget(QLabel("可疑标识符"), 0, 2)
        self.suspicious_identifier_action = QComboBox()
        self.suspicious_identifier_action.addItem("不处理", "ignore")
        self.suspicious_identifier_action.addItem("仅警告", "warn")
        self.suspicious_identifier_action.addItem("保留原文", "protect")
        self.suspicious_identifier_action.setCurrentIndex(1)
        self.suspicious_identifier_action.currentIndexChanged.connect(
            self._save_import_protection
        )
        policy_row.addWidget(self.suspicious_identifier_action, 0, 3)
        policy_row.setColumnStretch(1, 1)
        policy_row.setColumnStretch(3, 1)
        layout.addLayout(policy_row)

        preview_row = QHBoxLayout()
        self.import_protection_summary = QLabel("完成翻译后可预览实际匹配项")
        self.import_protection_summary.setObjectName("secondaryText")
        self.import_protection_summary.setWordWrap(True)
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
            logic_unknown_policy=str(
                self.logic_unknown_policy.currentData() or "warn"
            ),
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
                (items := load_items(items_path)),
                manifest.import_scope,
                game_root,
                manifest.import_protection,
                _load_editor_analysis(manifest),
                logic_safety=_translation_safety_for_manifest(
                    manifest, items, policy="warn"
                ),
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
            logic_issue_label = (
                "自动保留范围"
                if manifest.import_protection.logic_unknown_policy == "warn"
                else "阻断问题"
            )
            suffix = "；表格仅显示前 500 项" if len(entries) > 500 else ""
            self.import_protection_summary.setText(
                f"保留 {summary['protected']} 组，警告 {summary['warnings']} 组，"
                f"逻辑依赖 {summary.get('logic_dependencies', 0)} 组，"
                f"实际逻辑保护 {summary.get('logic_protected', 0)} 组，"
                f"直接显示 {summary.get('logic_direct_display', 0)} 组，"
                f"官方显示契约 {summary.get('logic_display_contract', 0)} 组，"
                f"语义等价 {summary.get('logic_semantic_equivalence', 0)} 组，"
                f"外部显示链路 {summary.get('logic_external_text_flow', 0)} 组，"
                f"外部部分合并 {summary.get('logic_external_partial_merge', 0)} 组，"
                f"未证明 {summary.get('logic_not_proven', 0)} 组，"
                f"{logic_issue_label} {summary.get('logic_blocking_relevant', 0)} 组，"
                f"已证明可翻译 {len(report.get('safe_to_translate', []))} 组，"
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
        self.font_application_paths.clear()

    def _ensure_font_preview_registered(self, candidate: FontCandidate | None) -> None:
        if candidate is None:
            return
        for path in candidate.files:
            key = str(path.resolve()).casefold()
            if key in self.font_application_paths:
                continue
            font_id = QFontDatabase.addApplicationFont(str(path))
            if font_id >= 0:
                self.font_application_ids.append(font_id)
                self.font_application_paths.add(key)

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

    def _main_tab_changed(self, index: int) -> None:
        if index == self.edit_tab_index:
            self._refresh_edit_tab()
        elif index == self.font_tab_index:
            self._refresh_font_tab()

    def _clear_edit_view(self, message: str = "完成 AI 翻译后即可编辑译文") -> None:
        self.edit_replace_toggle.setChecked(False)
        self._edit_loading = True
        try:
            self.edit_source_path = None
            self.edit_source_sha256 = ""
            self.edit_source_identity = None
            self.edit_source_row = -1
            self.edit_action_status = ""
            self.edit_model.set_items([])
            self.edit_original.clear()
            self.edit_context.clear()
            self.edit_translation.clear()
            self.edit_summary.setText("共 0 条 · 匹配 0 条 · 已修改 0 条")
            self.edit_entry_status.setText("选择左侧译文")
            self.edit_status.clear()
            self.edit_gate_label.setText(message)
            self.edit_gate_label.setVisible(True)
            self.edit_content.setEnabled(False)
            self.edit_save.setEnabled(False)
            self.edit_discard.setEnabled(False)
            self.edit_reset_current.setEnabled(False)
        finally:
            self._edit_loading = False

    def _refresh_edit_tab(self, *, force: bool = False, manifest=None) -> None:
        if self.pipeline_thread and self.pipeline_thread.isRunning():
            self.edit_content.setEnabled(False)
            return
        if not self.current_manifest_path:
            self._clear_edit_view()
            return
        manifest = manifest or load_manifest(self.current_manifest_path)
        translate = manifest.version.stage(Stage.TRANSLATE)
        items_value = translate.artifacts.get("items", "")
        path = Path(items_value) if items_value else None
        if translate.status is not StageStatus.COMPLETED or not path or not path.is_file():
            self._clear_edit_view()
            return

        stat = path.stat()
        identity = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
        self.edit_gate_label.setVisible(False)
        self.edit_content.setEnabled(True)
        if not force and identity == self.edit_source_identity:
            self._update_edit_summary()
            return

        items = load_items(path)
        self._edit_loading = True
        try:
            self.edit_source_path = path.resolve()
            self.edit_source_sha256 = sha256_file(path)
            self.edit_source_identity = identity
            self.edit_source_row = -1
            self.edit_action_status = ""
            self.edit_model.set_items(items)
            self.edit_original.clear()
            self.edit_context.clear()
            self.edit_translation.clear()
            self.edit_entry_status.setText("选择左侧译文")
            self.edit_status.clear()
        finally:
            self._edit_loading = False
        self._filter_edit_rows(self.edit_search.text())
        if self.edit_proxy.rowCount():
            self.edit_table.selectRow(0)
        self._update_edit_actions()

    def _filter_edit_rows(self, text: str) -> None:
        if not hasattr(self, "edit_proxy"):
            return
        self.edit_action_status = ""
        self.edit_proxy.setFilterFixedString(text.strip())
        self._update_edit_summary()
        if self.edit_proxy.rowCount() and not self.edit_table.currentIndex().isValid():
            self.edit_table.selectRow(0)
        self._update_edit_replace_actions()

    def _toggle_edit_replace(self, visible: bool) -> None:
        self.edit_replace_toggle.setText("▲" if visible else "▼")
        self.edit_replace_toggle.setToolTip("收起替换" if visible else "展开替换")
        self.edit_replace_toggle.setAccessibleName("收起替换" if visible else "展开替换")
        self.edit_replace_popup.setVisible(visible)
        if visible:
            self._position_edit_replace_popup()
            self.edit_replace_popup.raise_()
            QTimer.singleShot(0, self.edit_replace.setFocus)

    def _position_edit_replace_popup(self) -> None:
        point = self.edit_search_toolbar.mapTo(
            self.edit_content,
            self.edit_search_toolbar.rect().bottomLeft(),
        )
        self.edit_replace_popup.setGeometry(
            point.x(),
            point.y() + 4,
            self.edit_search_toolbar.width(),
            self.edit_replace_popup.sizeHint().height(),
        )
        self.edit_replace_popup.layout().activate()

    def _update_edit_replace_actions(self) -> None:
        if not hasattr(self, "edit_replace_one"):
            return
        needle = self.edit_search.text().strip()
        folded = needle.casefold()
        current = self.edit_model.translation(self.edit_source_row)
        self.edit_replace_one.setEnabled(bool(needle and folded in current.casefold()))
        self.edit_replace_all.setEnabled(
            bool(
                needle
                and any(
                    folded in self.edit_model.translation(row).casefold()
                    for row in range(self.edit_model.rowCount())
                )
            )
        )

    @staticmethod
    def _replace_translation_text(
        text: str,
        needle: str,
        replacement: str,
        *,
        count: int = 0,
    ) -> tuple[str, int]:
        return re.subn(
            re.escape(needle),
            lambda _match: replacement,
            text,
            count=count,
            flags=re.IGNORECASE,
        )

    def _replace_current_translation(self) -> None:
        item = self.edit_model.item(self.edit_source_row)
        needle = self.edit_search.text().strip()
        if not item or not needle:
            return
        updated, replaced = self._replace_translation_text(
            self.edit_model.translation(self.edit_source_row),
            needle,
            self.edit_replace.text(),
            count=1,
        )
        if not replaced:
            return
        error = self._translation_edit_error(item, updated)
        if error:
            QMessageBox.warning(self, "无法替换", f"{item.code or item.key}: {error}")
            return
        self.edit_translation.setPlainText(updated)
        self._filter_edit_rows(self.edit_search.text())
        self.edit_action_status = "已替换当前译文中的 1 处匹配。"
        self._update_edit_actions()

    def _replace_all_translations(self) -> None:
        needle = self.edit_search.text().strip()
        if not needle:
            return
        replacements: dict[int, str] = {}
        occurrences = 0
        for row, item in enumerate(self.edit_model.items):
            updated, replaced = self._replace_translation_text(
                self.edit_model.translation(row),
                needle,
                self.edit_replace.text(),
            )
            if not replaced:
                continue
            error = self._translation_edit_error(item, updated)
            if error:
                QMessageBox.warning(self, "无法全部替换", f"{item.code or item.key}: {error}")
                return
            replacements[row] = updated
            occurrences += replaced
        if not replacements:
            return
        answer = QMessageBox.question(
            self,
            "确认全部替换",
            f"将在 {len(replacements)} 条译文中替换 {occurrences} 处匹配。\n\n"
            f"查找：{needle}\n替换为：{self.edit_replace.text() or '（空文本）'}\n\n"
            "确定继续吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.edit_model.set_translations(replacements)
        current = self.edit_model.item(self.edit_source_row)
        if current and self.edit_source_row in replacements:
            self._edit_loading = True
            try:
                self.edit_translation.setPlainText(
                    self.edit_model.translation(self.edit_source_row)
                )
            finally:
                self._edit_loading = False
        self._filter_edit_rows(self.edit_search.text())
        self.edit_action_status = (
            f"已替换 {len(replacements)} 条译文中的 {occurrences} 处匹配。"
        )
        self._update_edit_actions()

    def _show_edit_entry(self, current: QModelIndex, _previous: QModelIndex) -> None:
        source = self.edit_proxy.mapToSource(current) if current.isValid() else QModelIndex()
        row = source.row() if source.isValid() else -1
        item = self.edit_model.item(row)
        self._edit_loading = True
        try:
            self.edit_source_row = row
            self.edit_original.setPlainText(item.original if item else "")
            self.edit_context.setPlainText(
                "\n".join(part for part in ((item.context if item else ""), (item.info if item else "")) if part)
            )
            self.edit_translation.setPlainText(self.edit_model.translation(row))
        finally:
            self._edit_loading = False
        self._update_edit_actions()

    @staticmethod
    def _translation_edit_error(item: TranslationItem, text: str) -> str:
        if not text:
            return "译文不能为空。"
        _protected, tokens = protect_control_tokens(text)
        if tokens != item.control_signature:
            return "控制符数量或顺序与原文不一致。"
        return ""

    def _edit_translation_changed(self) -> None:
        if self._edit_loading or self.edit_source_row < 0:
            return
        self.edit_action_status = ""
        self.edit_model.set_translation(
            self.edit_source_row,
            self.edit_translation.toPlainText(),
        )
        self._update_edit_actions()

    def _update_edit_summary(self) -> None:
        if not hasattr(self, "edit_model"):
            return
        self.edit_summary.setText(
            f"共 {self.edit_model.rowCount()} 条 · 匹配 {self.edit_proxy.rowCount()} 条 · "
            f"已修改 {len(self.edit_model.edits)} 条"
        )

    def _update_edit_actions(self) -> None:
        self._update_edit_summary()
        item = self.edit_model.item(self.edit_source_row)
        current_error = ""
        if item:
            current_error = self._translation_edit_error(
                item,
                self.edit_model.translation(self.edit_source_row),
            )
            state = "已修改" if item.key in self.edit_model.edits else "未修改"
            self.edit_entry_status.setText(f"{item.code or item.key} · {state}")
        else:
            self.edit_entry_status.setText("选择左侧译文")
        errors = [
            self._translation_edit_error(entry, self.edit_model.edits[entry.key])
            for entry in self.edit_model.items
            if entry.key in self.edit_model.edits
        ]
        invalid = next((error for error in errors if error), "")
        self.edit_status.setText(current_error or invalid or self.edit_action_status)
        dirty = bool(self.edit_model.edits)
        self.edit_save.setEnabled(dirty and not invalid)
        self.edit_discard.setEnabled(dirty)
        self.edit_reset_current.setEnabled(bool(item and item.key in self.edit_model.edits))
        self._update_edit_replace_actions()

    def _reset_current_edit(self) -> None:
        item = self.edit_model.item(self.edit_source_row)
        if item:
            self.edit_translation.setPlainText(item.translation)

    def _discard_translation_edits(self) -> None:
        row = self.edit_source_row
        self.edit_action_status = ""
        self._edit_loading = True
        try:
            self.edit_model.discard_edits()
        finally:
            self._edit_loading = False
        if row >= 0:
            source = self.edit_model.index(row, 0)
            proxy = self.edit_proxy.mapFromSource(source)
            if proxy.isValid():
                self.edit_table.selectRow(proxy.row())
                self._show_edit_entry(proxy, QModelIndex())
        self._update_edit_actions()

    def _save_translation_edits(self) -> None:
        if not self.current_manifest_path or not self.edit_model.edits:
            return
        try:
            pipeline = Pipeline(
                self.current_manifest_path,
                self.settings,
                "",
                local_data_dir(),
                glossary_api_key="",
            )
            output = pipeline.apply_translation_edits(
                dict(self.edit_model.edits),
                source_sha256=self.edit_source_sha256,
            )
            self.edit_source_identity = None
            self.status_label.setText(f"已保存译文：{output.name}")
            self._load_project_view()
            self.tabs.setCurrentIndex(self.edit_tab_index)
        except Exception as exc:
            QMessageBox.critical(self, "无法保存译文", str(exc))

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
        self.font_scan_thread = FontScanThread(self.current_manifest_path, refresh=force)
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
        warning_value = str(context.get("font_warning_count", "0"))
        warning_count = int(warning_value) if warning_value.isdigit() else 0
        if warning_count:
            self.font_status.setText(f"已扫描 {len(context['candidates'])} 个字体；发布版有 {warning_count} 个缺字警告")
            self.font_status.setToolTip(str(context.get("font_warnings", "")))
        else:
            corpus_label = (
                "实际文本" if context.get("exact_coverage") else "原文/译文保守全集"
            )
            self.font_status.setText(
                f"已扫描 {len(context['candidates'])} 个字体，"
                f"检查 {len(context['required'])} 个{corpus_label}字符"
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
        if (
            self.current_manifest_path
            and scanned != self.current_manifest_path
            and self.tabs.currentIndex() == self.font_tab_index
        ):
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
            if not self.font_context["original_slots"][index]:
                combo.addItem("该游戏不存在此字体槽", None)
                combo.setEnabled(False)
                combo.setToolTip("官方导出中没有此字体槽")
                combo.blockSignals(False)
                continue
            combo.setEnabled(True)
            combo.setToolTip("")
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
            if not original_slots[index]:
                continue
            original_candidate = candidate_for_family(candidates, original_slots[index])
            self._ensure_font_preview_registered(original_candidate)
            self._ensure_font_preview_registered(combo.currentData() or original_candidate)
        available = {family.casefold(): family for family in QFontDatabase.families()}
        for index, combo in enumerate(self.font_combos):
            if not original_slots[index]:
                self.font_original_labels[index].setText("不存在")
                self.font_original_labels[index].setToolTip("官方导出中没有此字体槽")
                self.font_coverage_labels[index].setText("不适用")
                self.font_coverage_labels[index].setToolTip("")
                self.font_preview_original[index].setText("原  （无此槽位）")
                self.font_preview_selected[index].setText("新  （无此槽位）")
                continue
            self.font_original_labels[index].setText(original_slots[index] or "未设置")
            self.font_original_labels[index].setToolTip(original_slots[index])
            selection = combo.currentData()
            original_candidate = candidate_for_family(candidates, original_slots[index])
            candidate = selection or original_candidate
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
            self.font_preview_original[index].setFont(
                _qt_preview_font(original_candidate, original_family, available)
            )
            self.font_preview_selected[index].setFont(
                _qt_preview_font(candidate, selected_family, available)
            )

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
            if not original_slots[index]:
                continue
            effective = candidate or candidate_for_family(candidates, original_slots[index])
            missing_count += len(effective.missing) if effective else len(required)
        if missing_count:
            answer = QMessageBox.warning(
                self,
                "字体仍有缺字",
                f"实际字体槽合计缺少 {missing_count} 个字符覆盖。发布可以继续，但游戏可能依赖字体回退。",
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
                "kind": "font-scheme",
                "schema": PROJECT_SCHEMA,
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
        if (self.pipeline_thread and self.pipeline_thread.isRunning()) or (
            self.proofread_thread and self.proofread_thread.isRunning()
        ):
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
        if (self.pipeline_thread and self.pipeline_thread.isRunning()) or (
            self.proofread_thread and self.proofread_thread.isRunning()
        ):
            return
        value = self.project_combo.currentData()
        if (
            self.current_manifest_path
            and str(value) != str(self.current_manifest_path)
            and self.edit_model.edits
        ):
            answer = QMessageBox.question(
                self,
                "未保存的译文",
                "当前译文修改尚未保存，放弃修改并切换项目？",
            )
            if answer != QMessageBox.Yes:
                previous = self.project_combo.findData(str(self.current_manifest_path))
                self.project_combo.blockSignals(True)
                self.project_combo.setCurrentIndex(max(0, previous))
                self.project_combo.blockSignals(False)
                return
            self.edit_model.discard_edits()
            self.edit_source_identity = None
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
        self._update_step_range_controls()
        self.proofread_gate_label.setVisible(True)
        self.proofread_content.setEnabled(False)
        self._clear_proofread_view()
        self._clear_edit_view()
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
            if stage is Stage.VALIDATE:
                translate = manifest.version.stage(Stage.TRANSLATE)
                items_value = translate.artifacts.get("items", "")
                result_available = (
                    translate.status is StageStatus.COMPLETED
                    and bool(items_value)
                    and Path(items_value).is_file()
                )
            else:
                result_available = (
                    record.status is StageStatus.COMPLETED
                    and result_path is not None
                    and result_path.exists()
                )
            self.step_result_buttons[stage].setEnabled(not running and result_available)
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
        self._update_step_range_controls()
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
        self.logic_unknown_policy.blockSignals(True)
        logic_policy_index = self.logic_unknown_policy.findData(
            manifest.import_protection.logic_unknown_policy
        )
        self.logic_unknown_policy.setCurrentIndex(max(logic_policy_index, 0))
        self.logic_unknown_policy.blockSignals(False)
        self.logic_unknown_policy.setEnabled(
            self.protect_logic_references.isChecked()
        )
        self.import_protection_table.setRowCount(0)
        self.import_protection_summary.setText("点击预览分析当前译文")
        self._load_glossary()
        self._load_proofread_view(manifest)
        if self.tabs.currentIndex() == self.edit_tab_index:
            self._refresh_edit_tab(manifest=manifest)
        else:
            translate = manifest.version.stage(Stage.TRANSLATE)
            items_value = translate.artifacts.get("items", "")
            available = (
                translate.status is StageStatus.COMPLETED
                and bool(items_value)
                and Path(items_value).is_file()
            )
            self.edit_gate_label.setVisible(not available)
            self.edit_content.setEnabled(available)
        # Preload while the workflow page is visible so the first font-tab click
        # only paints already-complete choices and coverage.
        self._refresh_font_tab()

    def _toggle_proofread_settings(self, visible: bool) -> None:
        self.proofread_settings_panel.setVisible(visible)
        self.proofread_settings_toggle.setArrowType(Qt.DownArrow if visible else Qt.RightArrow)

    def _clear_proofread_view(self) -> None:
        self.proofread_report = None
        self.proofread_report_path = None
        self.proofread_table.setRowCount(0)
        self.proofread_original.clear()
        self.proofread_current.clear()
        self.proofread_issues.clear()
        self.proofread_suggestion.clear()
        self.proofread_progress.setRange(0, 1)
        self.proofread_progress.setValue(0)
        self.proofread_progress.setFormat("尚未校对")
        self.proofread_summary.setText("受影响 0 · 高 0 · 中 0 · 低 0 · 已采纳 0 · 失败批次 0")
        self.proofread_entry_status.setText("选择左侧问题")

    def _load_proofread_view(self, manifest=None) -> None:
        self._proofread_loading = True
        try:
            controls = (
                (self.proofread_mode, self.settings.proofread_mode),
                (self.proofread_batch_size, self.settings.proofread_batch_size),
                (self.proofread_context_lines, self.settings.proofread_context_lines),
                (self.proofread_confidence, self.settings.proofread_confidence_percent),
            )
            for control, value in controls:
                control.blockSignals(True)
                if isinstance(control, QComboBox):
                    control.setCurrentIndex(max(0, control.findData(value)))
                else:
                    control.setValue(value)
                control.blockSignals(False)
            self.proofread_model.setText(self.settings.api_model or "未设置")
            if manifest is None and self.current_manifest_path:
                manifest = load_manifest(self.current_manifest_path)
            translated = bool(
                manifest
                and manifest.version.stage(Stage.TRANSLATE).status is StageStatus.COMPLETED
            )
            self.proofread_gate_label.setVisible(not translated)
            self.proofread_content.setEnabled(translated)
            if not translated or not self.current_manifest_path:
                self._clear_proofread_view()
                return
            report_path, log_path = proofread_paths(self.current_manifest_path, manifest)
            self.proofread_report_path = report_path
            self.open_proofread_report_button.setEnabled(report_path.is_file())
            self.open_proofread_log_button.setEnabled(log_path.is_file())
            translate = manifest.version.stage(Stage.TRANSLATE)
            current_value = translate.artifacts.get("items", "")
            current = Path(current_value)
            stale = False
            if report_path.is_file():
                try:
                    self.proofread_report = load_report(report_path)
                    changed = False
                    for entry in self.proofread_report["entries"]:
                        before = (entry["applicable"], entry["apply_error"], entry["decision"])
                        self._update_entry_applicability(entry)
                        if entry["decision"] == "accept" and not entry["applicable"]:
                            entry["decision"] = "pending"
                        changed = changed or before != (
                            entry["applicable"], entry["apply_error"], entry["decision"]
                        )
                    if changed:
                        save_report(report_path, self.proofread_report)
                    stale = not current_value or not current.is_file() or report_is_stale(
                        self.proofread_report, current
                    )
                except Exception as exc:
                    self.proofread_report = None
                    self.proofread_progress.setFormat(f"报告无效：{exc}")
            else:
                self.proofread_report = None
            self._populate_proofread_table()
            report = self.proofread_report
            summary = report["summary"] if report else {}
            total = int(summary.get("checked", 0))
            self.proofread_progress.setRange(0, max(1, total))
            self.proofread_progress.setValue(total if report else 0)
            if stale:
                self.proofread_progress.setFormat("报告已过期，请重新校对")
            elif report:
                self.proofread_progress.setFormat(
                    "部分完成 %v/%m" if report["status"] == "partial" else "已完成 %v/%m"
                )
            else:
                self.proofread_progress.setFormat("尚未校对")
            self._update_proofread_summary()
            self.start_proofread_button.setEnabled(report is None)
            self.rerun_proofread_button.setEnabled(report is not None)
            self.apply_proofread_button.setEnabled(
                bool(report and not stale and summary.get("accepted", 0))
            )
            self.restore_ai_translation_button.setEnabled(
                bool(translate.artifacts.get("items_ai_translation", ""))
            )
        finally:
            self._proofread_loading = False

    def _populate_proofread_table(self) -> None:
        self.proofread_table.setRowCount(0)
        report = self.proofread_report
        current_type = self.proofread_type_filter.currentData()
        self.proofread_type_filter.blockSignals(True)
        self.proofread_type_filter.clear()
        self.proofread_type_filter.addItem("全部问题类型", "all")
        types = sorted(
            {
                issue["type"]
                for entry in (report or {}).get("entries", [])
                for issue in entry["issues"]
            }
        )
        for issue_type in types:
            self.proofread_type_filter.addItem(issue_type, issue_type)
        selected_type = self.proofread_type_filter.findData(current_type)
        self.proofread_type_filter.setCurrentIndex(max(0, selected_type))
        self.proofread_type_filter.blockSignals(False)
        if not report:
            return
        severity_labels = {"high": "高", "medium": "中", "low": "低"}
        decision_labels = {"pending": "待处理", "accept": "已采纳", "keep": "保留现译"}
        for index, entry in enumerate(report["entries"]):
            row = self.proofread_table.rowCount()
            self.proofread_table.insertRow(row)
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
            check.setCheckState(Qt.Unchecked)
            check.setData(Qt.UserRole, index)
            issue_types = "、".join(sorted({issue["type"] for issue in entry["issues"]}))
            values = (
                check,
                QTableWidgetItem(severity_labels[entry["severity"]]),
                QTableWidgetItem(entry["code"]),
                QTableWidgetItem(issue_types),
                QTableWidgetItem(decision_labels[entry["decision"]]),
            )
            for column, item in enumerate(values):
                if column:
                    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                self.proofread_table.setItem(row, column, item)
        self._filter_proofread_rows()
        for row in range(self.proofread_table.rowCount()):
            if not self.proofread_table.isRowHidden(row):
                self.proofread_table.selectRow(row)
                break

    def _proofread_entry_at_row(self, row: int) -> dict[str, object] | None:
        if not self.proofread_report or row < 0:
            return None
        item = self.proofread_table.item(row, 0)
        index = item.data(Qt.UserRole) if item else None
        entries = self.proofread_report["entries"]
        return entries[index] if isinstance(index, int) and 0 <= index < len(entries) else None

    def _current_proofread_entry(self) -> dict[str, object] | None:
        return self._proofread_entry_at_row(self.proofread_table.currentRow())

    def _show_proofread_entry(self) -> None:
        entry = self._current_proofread_entry()
        self._proofread_loading = True
        try:
            self.proofread_original.setPlainText(entry["original"] if entry else "")
            self.proofread_current.setPlainText(entry["translation"] if entry else "")
            self.proofread_suggestion.setPlainText(entry["edited_translation"] if entry else "")
            self._update_proofread_entry_state(entry)
        finally:
            self._proofread_loading = False

    def _update_proofread_entry_state(self, entry: dict[str, object] | None) -> None:
        if not entry:
            self.proofread_issues.clear()
            self.proofread_entry_status.setText("选择左侧问题")
            self.proofread_entry_status.setToolTip("")
            for button in (self.proofread_accept, self.proofread_keep, self.proofread_reset):
                button.setEnabled(False)
            return
        descriptions = []
        for issue in entry["issues"]:
            confidence = round(float(issue["confidence"]) * 100)
            descriptions.append(
                f"[{issue['severity'].upper()} · {issue['type']} · {confidence}%] "
                f"{issue['description']}"
                + (f"\n建议：{issue['suggestion']}" if issue["suggestion"] else "")
            )
        if entry["apply_error"]:
            descriptions.append("[需要人工编辑] " + entry["apply_error"])
        self.proofread_issues.setPlainText("\n\n".join(descriptions))
        decision = {"pending": "待处理", "accept": "已采纳", "keep": "保留现译"}[entry["decision"]]
        state = "可采纳" if entry["applicable"] else "需编辑"
        self.proofread_entry_status.setText(f"{decision} · {state}")
        self.proofread_entry_status.setToolTip(str(entry["apply_error"]))
        self.proofread_accept.setEnabled(bool(entry["applicable"]))
        self.proofread_keep.setEnabled(True)
        self.proofread_reset.setEnabled(True)

    def _filter_proofread_rows(self) -> None:
        if not hasattr(self, "proofread_table"):
            return
        severity = self.proofread_severity_filter.currentData()
        issue_type = self.proofread_type_filter.currentData()
        decision = self.proofread_decision_filter.currentData()
        needle = self.proofread_search.text().strip().casefold()
        for row in range(self.proofread_table.rowCount()):
            entry = self._proofread_entry_at_row(row)
            types = {issue["type"] for issue in entry["issues"]} if entry else set()
            haystack = "\n".join(
                [
                    entry["key"], entry["code"], entry["original"], entry["translation"],
                    *(issue["description"] for issue in entry["issues"]),
                ]
            ).casefold() if entry else ""
            hidden = bool(
                not entry
                or (severity != "all" and entry["severity"] != severity)
                or (issue_type != "all" and issue_type not in types)
                or (decision != "all" and entry["decision"] != decision)
                or (needle and needle not in haystack)
            )
            self.proofread_table.setRowHidden(row, hidden)

    def _update_entry_applicability(self, entry: dict[str, object]) -> None:
        text = str(entry["edited_translation"])
        if not text:
            entry["applicable"] = False
            entry["apply_error"] = "AI 未生成可直接替换的完整修订译文，请重新校对或手动编辑。"
            return
        if text == str(entry["translation"]):
            entry["applicable"] = False
            entry["apply_error"] = "建议译文没有产生任何修改，请重新校对或手动编辑。"
            return
        _, expected = protect_control_tokens(str(entry["translation"]))
        _, actual = protect_control_tokens(text)
        entry["applicable"] = actual == expected
        entry["apply_error"] = "" if actual == expected else "建议译文控制符数量或顺序不一致。"

    def _save_proofread_draft(self) -> None:
        if self._proofread_loading or not self.proofread_report or not self.proofread_report_path:
            return
        entry = self._current_proofread_entry()
        if not entry:
            return
        entry["edited_translation"] = self.proofread_suggestion.toPlainText()
        self._update_entry_applicability(entry)
        save_report(self.proofread_report_path, self.proofread_report)
        self._update_proofread_entry_state(entry)

    def _save_proofread_decisions(self) -> None:
        if not self.proofread_report or not self.proofread_report_path:
            return
        save_report(self.proofread_report_path, self.proofread_report)
        self._populate_proofread_table()
        self._update_proofread_summary()

    def _decide_current_proofread(self, decision: str) -> None:
        entry = self._current_proofread_entry()
        if not entry:
            return
        self._update_entry_applicability(entry)
        if decision == "accept" and not entry["applicable"]:
            QMessageBox.warning(self, "无法采纳", entry["apply_error"])
            return
        entry["decision"] = decision
        self._save_proofread_decisions()

    def _decide_selected_proofread(self, decision: str) -> None:
        selected = []
        for row in range(self.proofread_table.rowCount()):
            item = self.proofread_table.item(row, 0)
            if item and item.checkState() == Qt.Checked:
                selected.append(self._proofread_entry_at_row(row))
        skipped = 0
        for entry in filter(None, selected):
            self._update_entry_applicability(entry)
            if decision == "accept" and not entry["applicable"]:
                skipped += 1
                continue
            entry["decision"] = decision
        if selected:
            self._save_proofread_decisions()
        if skipped:
            QMessageBox.warning(
                self,
                "部分未处理",
                f"{skipped} 条建议因没有有效修订或控制符不一致而未被采纳。",
            )

    def _update_proofread_summary(self) -> None:
        summary = self.proofread_report["summary"] if self.proofread_report else {}
        self.proofread_summary.setText(
            f"受影响 {summary.get('affected', 0)} · 高 {summary.get('high', 0)} · "
            f"中 {summary.get('medium', 0)} · 低 {summary.get('low', 0)} · "
            f"已采纳 {summary.get('accepted', 0)} · 失败批次 {summary.get('failed_batches', 0)}"
        )
        if self.proofread_report and self.current_manifest_path:
            manifest = load_manifest(self.current_manifest_path)
            items_value = manifest.version.stage(Stage.TRANSLATE).artifacts.get("items", "")
            stale = not items_value or not Path(items_value).is_file() or report_is_stale(
                self.proofread_report, items_value
            )
            self.apply_proofread_button.setEnabled(
                bool(not stale and summary.get("accepted", 0))
            )

    def _save_proofread_settings(self) -> None:
        if self._proofread_loading:
            return
        self.settings.proofread_mode = str(self.proofread_mode.currentData())
        self.settings.proofread_batch_size = self.proofread_batch_size.value()
        self.settings.proofread_context_lines = self.proofread_context_lines.value()
        self.settings.proofread_confidence_percent = self.proofread_confidence.value()
        self.store.save(self.settings)

    def _set_proofread_ui_locked(self, locked: bool) -> None:
        enabled = not locked
        for control in (
            self.settings_button, self.project_combo, self.new_project_button,
            self.add_version_button, self.one_click, self.step_mode, self.start_button,
            self.retry_button, self.open_release_button, self.save_glossary_button,
        ):
            control.setEnabled(enabled)
        for button in (*self.step_buttons.values(), *self.step_result_buttons.values()):
            button.setEnabled(enabled)
        for index in range(self.tabs.count()):
            self.tabs.setTabEnabled(index, enabled or index == self.proofread_tab_index)
        self.proofread_options.setEnabled(True)
        for control in (
            self.proofread_mode,
            self.proofread_batch_size,
            self.proofread_context_lines,
            self.proofread_confidence,
        ):
            control.setEnabled(enabled)
        self.start_proofread_button.setEnabled(False)
        self.rerun_proofread_button.setEnabled(False)
        self.proofread_table.setEnabled(enabled)
        self.proofread_suggestion.setEnabled(enabled)
        for control in (
            self.proofread_severity_filter, self.proofread_type_filter,
            self.proofread_decision_filter, self.proofread_search,
            self.proofread_accept_selected, self.proofread_keep_selected,
            self.proofread_accept, self.proofread_keep, self.proofread_reset,
            self.apply_proofread_button, self.restore_ai_translation_button,
        ):
            control.setEnabled(enabled)
        self.stop_proofread_button.setEnabled(locked)

    def _start_proofread(self) -> None:
        if not self.current_manifest_path or (
            self.proofread_thread and self.proofread_thread.isRunning()
        ) or (self.pipeline_thread and self.pipeline_thread.isRunning()):
            return
        if self.edit_model.edits:
            self.tabs.setCurrentIndex(self.edit_tab_index)
            QMessageBox.information(self, "译文尚未保存", "请先保存或撤销当前译文修改。")
            return
        self._save_proofread_settings()
        errors = []
        if not self.settings.ainiee_source or not Path(self.settings.ainiee_source).exists():
            errors.append("请选择或安装 AiNiee-Next。")
        key = ""
        if self.settings.proofread_mode == "rules_ai":
            if not self.settings.api_base_url.strip() or not self.settings.api_model.strip():
                errors.append("请填写 AiNiee 翻译 API 基础地址和模型。")
            try:
                key = self.store.api_key(self.settings)
            except Exception:
                key = ""
            if not key:
                errors.append("请填写 AiNiee 翻译 API 密钥。")
        if errors:
            QMessageBox.warning(self, "校对设置未完成", "\n".join(errors))
            return
        self.proofread_run_result = None
        self.proofread_thread = ProofreadThread(
            self.current_manifest_path,
            self.settings,
            key,
            local_data_dir(),
        )
        self.proofread_thread.progress_event.connect(self._proofread_progress_event)
        self.proofread_thread.log_line.connect(self._append_log)
        self.proofread_thread.succeeded.connect(
            lambda path: setattr(self, "proofread_run_result", ("success", path))
        )
        self.proofread_thread.cancelled.connect(
            lambda: setattr(self, "proofread_run_result", ("cancelled", ""))
        )
        self.proofread_thread.failed.connect(
            lambda detail: setattr(self, "proofread_run_result", ("failed", detail))
        )
        self.proofread_thread.finished.connect(self._proofread_finished)
        self._set_proofread_ui_locked(True)
        self.tabs.setCurrentIndex(self.proofread_tab_index)
        self.proofread_progress.setRange(0, 0)
        self.proofread_progress.setFormat("正在准备校对…")
        self.status_label.setText("正在校对")
        self.proofread_thread.start()

    def _proofread_progress_event(self, event: dict[str, object]) -> None:
        total = int(event.get("total", 0))
        current = int(event.get("current", 0))
        self.proofread_progress.setRange(0, max(1, total))
        self.proofread_progress.setValue(current)
        self.proofread_progress.setFormat("正在校对 %v/%m")

    def _stop_proofread(self) -> None:
        if self.proofread_thread and self.proofread_thread.isRunning():
            self.proofread_thread.cancel()
            self.stop_proofread_button.setEnabled(False)
            self.proofread_progress.setFormat("正在停止…")

    def _proofread_finished(self) -> None:
        result = self.proofread_run_result or ("failed", "校对线程未返回结果。")
        self.proofread_thread = None
        self._set_proofread_ui_locked(False)
        self._load_project_view()
        self.tabs.setCurrentIndex(self.proofread_tab_index)
        if result[0] == "success":
            self.status_label.setText("校对完成")
        elif result[0] == "cancelled":
            self.status_label.setText("校对已停止")
        else:
            self.status_label.setText("校对失败")
            detail = result[1]
            QMessageBox.critical(
                self,
                "校对失败",
                detail.splitlines()[-1] if detail.splitlines() else detail,
            )

    def _apply_proofread(self) -> None:
        if not self.current_manifest_path or not self.proofread_report_path or not self.proofread_report:
            return
        if self.proofread_report["failed_batches"]:
            answer = QMessageBox.warning(
                self,
                "报告仅部分完成",
                f"有 {len(self.proofread_report['failed_batches'])} 个批次未检查。仍要应用已采纳修改吗？",
                QMessageBox.Yes | QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        try:
            pipeline = Pipeline(
                self.current_manifest_path,
                self.settings,
                "",
                local_data_dir(),
                glossary_api_key="",
            )
            output = pipeline.apply_proofread(self.proofread_report_path)
            self.status_label.setText(f"已应用校对：{output.name}")
            self._load_project_view()
            self.tabs.setCurrentIndex(self.proofread_tab_index)
        except Exception as exc:
            QMessageBox.critical(self, "无法应用校对", str(exc))

    def _restore_ai_translation(self) -> None:
        if not self.current_manifest_path:
            return
        answer = QMessageBox.question(
            self,
            "恢复 AI 原译",
            "将取消当前校对版本，并把校验、导入和发布重置为待完成。继续吗？",
        )
        if answer != QMessageBox.Yes:
            return
        try:
            pipeline = Pipeline(
                self.current_manifest_path,
                self.settings,
                "",
                local_data_dir(),
                glossary_api_key="",
            )
            pipeline.restore_ai_translation()
            self.status_label.setText("已恢复 AI 原译")
            self._load_project_view()
            self.tabs.setCurrentIndex(self.proofread_tab_index)
        except Exception as exc:
            QMessageBox.critical(self, "无法恢复", str(exc))

    def _open_proofread_report(self) -> None:
        if self.proofread_report_path and self.proofread_report_path.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.proofread_report_path)))

    def _open_proofread_log(self) -> None:
        if not self.current_manifest_path:
            return
        manifest = load_manifest(self.current_manifest_path)
        _, path = proofread_paths(self.current_manifest_path, manifest)
        if path.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _new_project(self) -> None:
        if self.pipeline_thread and self.pipeline_thread.isRunning():
            return
        if self.edit_model.edits:
            self.tabs.setCurrentIndex(self.edit_tab_index)
            QMessageBox.information(self, "译文尚未保存", "请先保存或撤销当前译文修改。")
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
        if self.edit_model.edits:
            self.tabs.setCurrentIndex(self.edit_tab_index)
            QMessageBox.information(self, "译文尚未保存", "请先保存或撤销当前译文修改。")
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

    def _selected_step_range(self) -> tuple[Stage, ...] | None:
        selected = tuple(stage for stage in STAGE_ORDER if self.step_checks[stage].isChecked())
        if not selected:
            return ()
        start = STAGE_ORDER.index(selected[0])
        expected = STAGE_ORDER[start : start + len(selected)]
        return selected if selected == expected else None

    def _update_step_range_controls(self, _checked: bool = False) -> None:
        selected = self._selected_step_range()
        running = bool(self.pipeline_thread and self.pipeline_thread.isRunning())
        if selected is None:
            self.step_range_summary.setText("选择不连续")
        elif not selected:
            self.step_range_summary.setText("未选择步骤")
        elif len(selected) == 1:
            self.step_range_summary.setText(f"已选择：{STAGE_LABELS[selected[0]]}")
        else:
            self.step_range_summary.setText(
                f"已选择：{STAGE_LABELS[selected[0]]} 至 {STAGE_LABELS[selected[-1]]}"
            )
        self.run_range_button.setEnabled(
            bool(selected) and self.current_manifest_path is not None and not running
        )

    def _start_selected_steps(self) -> None:
        stages = self._selected_step_range()
        if stages is None:
            QMessageBox.warning(self, "无法连续执行", "请选择一段连续的步骤。")
            return
        if stages:
            self._start(stages=stages)

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
            self.run_range_button,
            self.retry_button,
            self.open_release_button,
            self.open_font_release_button,
            self.save_glossary_button,
        ):
            control.setEnabled(enabled)
        for button in (*self.step_buttons.values(), *self.step_result_buttons.values()):
            button.setEnabled(enabled)
        for check in self.step_checks.values():
            check.setEnabled(enabled)
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
            self.logic_unknown_policy,
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
            self.logic_unknown_policy.setEnabled(
                self.protect_logic_references.isChecked()
            )
            self._update_step_range_controls()
        self.stop_button.setEnabled(locked)

    def _start(
        self,
        stage: Stage | None = None,
        *,
        stages: tuple[Stage, ...] = (),
        switch_to_step: bool = True,
    ) -> None:
        if (
            not self.current_manifest_path
            or (self.pipeline_thread and self.pipeline_thread.isRunning())
            or (self.proofread_thread and self.proofread_thread.isRunning())
        ):
            return
        if self.edit_model.edits:
            self.tabs.setCurrentIndex(self.edit_tab_index)
            QMessageBox.information(self, "译文尚未保存", "请先保存或撤销当前译文修改。")
            return
        selected = stages or ((stage,) if stage is not None else ())
        errors = validate_settings(self.settings) if not selected else []
        if Stage.EXTRACT in selected:
            try:
                inspect_wolf_editor(self.settings.wolf_editor_path)
            except (OSError, ValueError) as error:
                errors.append(f"WOLF RPG Editor：{error}")
        if errors:
            QMessageBox.warning(self, "设置未完成", "\n".join(errors))
            return
        try:
            self._stop_font_scan_for_pipeline()
            if selected and switch_to_step:
                self.active_step_stage = selected[0]
                self.step_mode.setChecked(True)
                self.workflow_stack.setCurrentIndex(1)
                self._set_mode(RunMode.STEP)
            key = ""
            glossary_key = ""
            if not selected or Stage.TRANSLATE in selected:
                key = self.store.api_key(self.settings)
            if not selected or Stage.GLOSSARY in selected:
                glossary_key = self.store.glossary_api_key(self.settings)
            self.pipeline = Pipeline(
                self.current_manifest_path,
                self.settings,
                key,
                local_data_dir(),
                glossary_api_key=glossary_key,
            )
            self.pipeline_thread = PipelineThread(self.pipeline, stage, stages)
            self.pipeline_thread.log_line.connect(self._append_log)
            self.pipeline_thread.stage_progress.connect(self._stage_progress)
            self.pipeline_thread.stage_state.connect(self._stage_state)
            self.pipeline_thread.result_ready.connect(self._pipeline_result)
            self.pipeline_thread.failed.connect(self._pipeline_failed)
            self.pipeline_thread.finished.connect(self._pipeline_finished)
            self._set_pipeline_ui_locked(True)
            if self.font_apply_active:
                status = "正在应用字体"
            elif stages:
                status = f"正在连续执行：{STAGE_LABELS[stages[0]]} 至 {STAGE_LABELS[stages[-1]]}"
            elif stage is not None:
                status = f"正在执行：{STAGE_LABELS[stage]}"
            else:
                status = "运行中"
            self.status_label.setText(status)
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
            if stage is Stage.VALIDATE:
                translate = manifest.version.stage(Stage.TRANSLATE)
                items_value = translate.artifacts.get("items", "")
                if (
                    translate.status is not StageStatus.COMPLETED
                    or not items_value
                    or not Path(items_value).is_file()
                ):
                    QMessageBox.information(self, "编辑译文", "完成 AI 翻译后即可编辑译文。")
                    return
                self.tabs.setCurrentIndex(self.edit_tab_index)
                self._refresh_edit_tab(manifest=manifest)
                return
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
        stages = self.pipeline_thread.stages if self.pipeline_thread else ()
        self.status_label.setText(
            "字体已应用"
            if self.font_apply_active
            else (
                f"已完成：{STAGE_LABELS[stages[0]]} 至 {STAGE_LABELS[stages[-1]]}"
                if stages
                else (f"已完成：{STAGE_LABELS[target]}" if target is not None else "已完成")
            )
        )

    def _pipeline_failed(self, detail: str) -> None:
        target = self.pipeline_thread.stage if self.pipeline_thread else None
        stages = self.pipeline_thread.stages if self.pipeline_thread else ()
        self.status_label.setText(
            "字体应用错误"
            if self.font_apply_active
            else (
                f"连续执行出错：{STAGE_LABELS[stages[0]]} 至 {STAGE_LABELS[stages[-1]]}"
                if stages
                else (f"出现错误：{STAGE_LABELS[target]}" if target is not None else "出现错误")
            )
        )
        if not self.font_apply_active:
            self.tabs.setCurrentIndex(0)
        title = (
            "字体应用错误"
            if self.font_apply_active
            else ("步骤执行错误" if target is not None or stages else "流水线失败")
        )
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

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if (
            hasattr(self, "edit_replace_toggle")
            and self.edit_replace_toggle.isChecked()
        ):
            QTimer.singleShot(0, self._position_edit_replace_popup)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.edit_model.edits:
            answer = QMessageBox.question(self, "退出", "译文修改尚未保存，仍要退出？")
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        if self.proofread_thread and self.proofread_thread.isRunning():
            if QMessageBox.question(self, "退出", "校对仍在运行，停止并退出？") != QMessageBox.Yes:
                event.ignore()
                return
            self.proofread_thread.cancel()
            self.proofread_thread.wait(5000)
            if self.proofread_thread.isRunning():
                QMessageBox.warning(self, "仍在停止", "校对子进程尚未完全退出，请稍后再次关闭窗口。")
                event.ignore()
                return
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
QPlainTextEdit#proofreadSuggestion { border: 2px solid #79a98a; background: #fbfefc; }
QPlainTextEdit#proofreadSuggestion:focus { border-color: #267247; }
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled, QPlainTextEdit:disabled, QTableWidget:disabled {
    color: #9aa49d; background: #edf0ed; border-color: #d8ddd9;
}
QPushButton, QToolButton {
    background: #ffffff; border: 1px solid #c5cec7; border-radius: 6px; padding: 7px 12px; min-height: 20px;
}
QPushButton:hover, QToolButton:hover { background: #eef3ef; border-color: #91a398; }
QPushButton:pressed, QToolButton:pressed { background: #e2eae4; }
QPushButton:disabled, QToolButton:disabled { color: #9aa49d; background: #edf0ed; }
QPushButton#primaryButton { background: #246b43; color: white; border-color: #246b43; font-weight: 600; }
QToolButton#primaryButton { background: #246b43; color: white; border-color: #246b43; }
QPushButton#primaryButton:disabled, QToolButton#primaryButton:disabled {
    color: #9aa49d; background: #edf0ed; border-color: #c5cec7;
}
QPushButton#primaryButton:hover { background: #1d5b38; }
QPushButton#segment { border-radius: 0; min-width: 48px; }
QPushButton#segment:checked { background: #dceae0; border-color: #5d8d6f; color: #17482d; }
QFrame#editReplacePopup { background: #f6f8f6; border: 0; }
QToolButton#editReplaceToggle { padding: 0; }
QPushButton#editReplaceButton { font-size: 15px; }
QLineEdit#editSearch QToolButton, QLineEdit#editReplace QToolButton {
    background: transparent; border: 0; padding: 0; margin: 0;
    min-width: 18px; max-width: 18px; min-height: 18px; max-height: 18px;
}
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
