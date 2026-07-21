from __future__ import annotations

import json
import os
import shutil
import sys
import traceback
from pathlib import Path

from PySide6.QtCore import QThread, QTimer, Qt, Signal
from PySide6.QtGui import QCloseEvent, QFont, QFontDatabase
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
    test_api,
)
from models import ImportScope, RunMode, STAGE_ORDER, Stage, StageStatus
from pipeline import Pipeline, add_version, create_project, load_manifest
from settings import SettingsStore, local_data_dir, validate_settings


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
    Stage.EXTRACT: "通过官方工具导出 XLSX",
    Stage.GLOSSARY: "从完整语料生成角色与术语",
    Stage.TRANSLATE: "调用 AiNiee 翻译文本",
    Stage.VALIDATE: "校验键、译文与控制符",
    Stage.IMPORT: "按选定范围回填游戏",
    Stage.RELEASE: "生成可直接运行的发布目录",
}


class PipelineThread(QThread):
    log_line = Signal(str)
    stage_progress = Signal(int, int, str)
    result_ready = Signal(str)
    failed = Signal(str)

    def __init__(self, pipeline: Pipeline, stage: Stage | None = None):
        super().__init__()
        self.pipeline = pipeline
        self.stage = stage
        self.pipeline.set_log_sink(self.log_line.emit)
        self.pipeline.progress = lambda current, total, stage: self.stage_progress.emit(current, total, stage.value)

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


class ApiTestThread(QThread):
    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(self, settings, api_key: str):
        super().__init__()
        self.settings = settings
        self.api_key = api_key

    def run(self) -> None:
        try:
            self.succeeded.emit(test_api(self.settings, self.api_key))
        except Exception as exc:
            self.failed.emit(str(exc))


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
        self.api_thread: ApiTestThread | None = None
        self.setWindowTitle("WOLFLator 设置")
        self.setMinimumWidth(720)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 20)
        layout.setSpacing(16)

        title = QLabel("工具与 API")
        title.setObjectName("dialogTitle")
        layout.addWidget(title)

        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(12)
        self.wolf_path = QLineEdit(self.settings.wolf_tool_path)
        form.addRow("官方 WOLF 工具", _path_row(self.wolf_path, "选择 EXE", self._choose_wolf))

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

        self.api_url = QLineEdit(self.settings.api_base_url)
        self.api_url.setPlaceholderText("https://example.com/v1")
        self.api_model = QLineEdit(self.settings.api_model)
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.Password)
        try:
            self.api_key.setText(self.store.api_key(self.settings))
        except Exception:
            pass
        form.addRow("API 基础地址", self.api_url)
        form.addRow("模型", self.api_model)
        form.addRow("API 密钥", self.api_key)

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
        self.test_button = QPushButton("测试 API")
        self.test_button.clicked.connect(self._test_api)
        limits_layout.addWidget(QLabel("并发"))
        limits_layout.addWidget(self.api_threads)
        limits_layout.addWidget(QLabel("超时"))
        limits_layout.addWidget(self.api_timeout)
        limits_layout.addWidget(self.test_button)
        limits_layout.addStretch(1)
        form.addRow("请求", limits)

        self.projects_root = QLineEdit(self.settings.projects_root)
        form.addRow("项目目录", _path_row(self.projects_root, "选择", self._choose_projects_root))
        self.ascii_dir = QLineEdit(self.settings.ascii_runner_dir)
        form.addRow("ASCII 执行目录", _path_row(self.ascii_dir, "选择", self._choose_ascii_dir))
        layout.addLayout(form)

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

    def _choose_ainiee(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择 AiNiee 安装或源码目录", self.ainiee_path.text())
        if not path:
            return
        try:
            source = str(locate_ainiee_source(path))
            self._start_ainiee_setup(False, source)
        except Exception as exc:
            QMessageBox.critical(self, "AiNiee 不兼容", str(exc))

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
        if self.install_thread and self.install_thread.isRunning():
            return
        self.install_button.setEnabled(False)
        self.select_ainiee_button.setEnabled(False)
        self.repair_button.setEnabled(False)
        self.save_button.setEnabled(False)
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
        self.install_progress.hide()
        self.install_button.setEnabled(True)
        self.select_ainiee_button.setEnabled(True)
        self.repair_button.setEnabled(True)
        self.save_button.setEnabled(True)

    def _ainiee_install_failed(self, detail: str) -> None:
        self.activity.setText("AiNiee 安装失败")
        self.install_progress.hide()
        self.install_button.setEnabled(True)
        self.select_ainiee_button.setEnabled(True)
        self.repair_button.setEnabled(True)
        self.save_button.setEnabled(True)
        QMessageBox.critical(self, "安装失败", detail[-4000:])

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
        if QMessageBox.question(self, "移除 AiNiee", f"移除托管目录？\n{path}") != QMessageBox.Yes:
            return
        shutil.rmtree(path)
        self.ainiee_path.clear()
        self.activity.setText("已移除托管版本")

    def _current_settings(self):
        item = self.settings
        item.wolf_tool_path = self.wolf_path.text().strip()
        item.ainiee_source = self.ainiee_path.text().strip()
        item.api_base_url = self.api_url.text().strip().rstrip("/")
        item.api_model = self.api_model.text().strip()
        item.api_threads = self.api_threads.value()
        item.api_timeout = self.api_timeout.value()
        item.projects_root = self.projects_root.text().strip()
        item.ascii_runner_dir = self.ascii_dir.text().strip()
        item.license_accepted = self.license_check.isChecked()
        return item

    def _test_api(self) -> None:
        item = self._current_settings()
        key = self.api_key.text().strip()
        if not key:
            QMessageBox.warning(self, "API", "请填写 API 密钥。")
            return
        self.test_button.setEnabled(False)
        self.activity.setText("正在测试 API...")
        self.api_thread = ApiTestThread(item, key)
        self.api_thread.succeeded.connect(self._api_succeeded)
        self.api_thread.failed.connect(self._api_failed)
        self.api_thread.start()

    def _api_succeeded(self, response: str) -> None:
        self.test_button.setEnabled(True)
        self.activity.setText("API 连接成功")
        preview = response[:500] + ("..." if len(response) > 500 else "")
        QMessageBox.information(self, "API 连接成功", f"模型已返回正文：\n\n{preview}")

    def _api_failed(self, error: str) -> None:
        self.test_button.setEnabled(True)
        self.activity.setText("API 测试失败")
        QMessageBox.critical(self, "API 测试失败", error)

    def _save(self) -> None:
        item = self._current_settings()
        try:
            self.store.set_api_key(item, self.api_key.text())
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
        running = (self.install_thread and self.install_thread.isRunning()) or (
            self.api_thread and self.api_thread.isRunning()
        )
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
        settings_button = QToolButton()
        settings_button.setIcon(self.style().standardIcon(QStyle.SP_FileDialogDetailedView))
        settings_button.setToolTip("设置")
        settings_button.clicked.connect(self._open_settings)
        header.addWidget(settings_button)
        layout.addLayout(header)

        project_row = QHBoxLayout()
        self.project_combo = QComboBox()
        self.project_combo.setMinimumWidth(340)
        self.project_combo.currentIndexChanged.connect(self._project_changed)
        new_button = QPushButton("新建项目")
        new_button.setIcon(self.style().standardIcon(QStyle.SP_FileIcon))
        new_button.clicked.connect(self._new_project)
        version_button = QPushButton("添加版本")
        version_button.setIcon(self.style().standardIcon(QStyle.SP_FileDialogNewFolder))
        version_button.clicked.connect(self._add_version)
        project_row.addWidget(self.project_combo, 1)
        project_row.addWidget(new_button)
        project_row.addWidget(version_button)
        layout.addLayout(project_row)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._workflow_tab(), "流程")
        self.tabs.addTab(self._glossary_tab(), "术语")
        self.tabs.addTab(self._scope_tab(), "导入范围")
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
        self.step_skip_buttons: dict[Stage, QPushButton] = {}
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
            run_button.setFixedWidth(52)
            run_button.clicked.connect(lambda _checked=False, target=stage: self._start(target))
            skip_button = QPushButton("跳过")
            skip_button.setFixedWidth(52)
            skip_button.clicked.connect(lambda _checked=False, target=stage: self._skip_stage(target))
            row_layout.addWidget(number, 0, 0)
            row_layout.addWidget(title, 0, 1)
            row_layout.addWidget(description, 0, 2)
            row_layout.addWidget(status, 0, 3)
            row_layout.addWidget(run_button, 0, 4)
            row_layout.addWidget(skip_button, 0, 5)
            row_layout.setColumnStretch(2, 1)
            self.step_status_labels[stage] = status
            self.step_buttons[stage] = run_button
            self.step_skip_buttons[stage] = skip_button
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
        save_button = QPushButton("保存术语")
        save_button.clicked.connect(self._save_glossary)
        layout.addWidget(save_button, alignment=Qt.AlignRight)
        return page

    def _scope_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(18, 20, 18, 18)
        self.scope_checks = {
            "display": QCheckBox("显示文本"),
            "external": QCheckBox("外部 TXT / CSV"),
            "optional_name": QCheckBox("数据库、地图和事件名称"),
            "halfwidth": QCheckBox("纯半角字符串"),
            "filename": QCheckBox("文件名引用"),
        }
        self.scope_checks["display"].setChecked(True)
        for key, check in self.scope_checks.items():
            check.toggled.connect(self._save_scope)
            layout.addWidget(check)
            if key == "filename":
                warning = QLabel("启用文件名导入前，发布副本中必须存在对应的目标文件。")
                warning.setObjectName("warningText")
                layout.addWidget(warning)
        layout.addStretch(1)
        return page

    def _open_settings(self, _checked=False, first_run: bool = False) -> None:
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
        if root.is_dir():
            for path in sorted(root.glob("*/project.json")):
                try:
                    manifest = load_manifest(path)
                    self.project_combo.addItem(manifest.name, str(path))
                except Exception:
                    continue
        index = self.project_combo.findData(selected)
        self.project_combo.setCurrentIndex(index if index >= 0 else 0)
        self.project_combo.blockSignals(False)
        self._project_changed(self.project_combo.currentIndex())

    def _project_changed(self, _index: int) -> None:
        value = self.project_combo.currentData()
        self.current_manifest_path = Path(value) if value else None
        if not self.current_manifest_path:
            self._clear_project_view()
            return
        self.settings.last_project = str(self.current_manifest_path)
        self.store.save(self.settings)
        self._load_project_view()

    @staticmethod
    def _update_stage_status(label: QLabel, status: StageStatus, detail: str = "") -> None:
        display_status = {
            StageStatus.RUNNING: StageStatus.PENDING,
            StageStatus.CANCELLED: StageStatus.FAILED,
        }.get(status, status)
        label.setText(STATUS_LABELS[display_status])
        label.setProperty("state", display_status.value)
        label.setToolTip(detail)
        label.style().unpolish(label)
        label.style().polish(label)

    def _clear_project_view(self) -> None:
        for stage in STAGE_ORDER:
            self._update_stage_status(self.easy_stage_status[stage], StageStatus.PENDING)
            self._update_stage_status(self.step_status_labels[stage], StageStatus.PENDING)
            self.step_buttons[stage].setEnabled(False)
            self.step_skip_buttons[stage].setEnabled(False)
        self.terms_table.setRowCount(0)
        self.characters_table.setRowCount(0)
        self.progress.setValue(0)
        self.retry_button.setEnabled(False)
        self.open_logs_button.setEnabled(False)
        self.easy_summary.setText("选择项目后即可开始")
        self.start_button.setText("开始翻译")
        self.start_button.setEnabled(False)

    def _load_project_view(self) -> None:
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
            skipped = record.artifacts.get("skipped") == "true"
            detail = "已手动跳过" if skipped else record.error or next(iter(record.artifacts.values()), "")
            easy_status = StageStatus.PENDING if skipped else record.status
            self._update_stage_status(self.easy_stage_status[stage], easy_status, detail)
            self._update_stage_status(self.step_status_labels[stage], record.status, detail)
            self.step_buttons[stage].setEnabled(not running)
            self.step_skip_buttons[stage].setEnabled(not running)
            if record.status in {StageStatus.FAILED, StageStatus.CANCELLED}:
                failed_stages.append(stage)
            if record.status is StageStatus.COMPLETED and not skipped:
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
        for name, check in self.scope_checks.items():
            check.blockSignals(True)
            check.setChecked(bool(getattr(manifest.import_scope, name)))
            check.blockSignals(False)
        self._load_glossary()

    def _new_project(self) -> None:
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
        if not self.current_manifest_path:
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
        if not self.current_manifest_path:
            return
        if self.pipeline:
            self.pipeline.set_run_mode(mode)
        else:
            manifest = load_manifest(self.current_manifest_path)
            manifest.run_mode = mode
            temporary = self.current_manifest_path.with_name("project.json.tmp")
            temporary.write_text(json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.current_manifest_path)

    def _select_workflow_mode(self, mode: RunMode) -> None:
        self.workflow_stack.setCurrentIndex(0 if mode is RunMode.ONE_CLICK else 1)
        self._set_mode(mode)
        self._load_project_view()

    def _save_scope(self) -> None:
        if not self.current_manifest_path:
            return
        if self.scope_checks["filename"].isChecked():
            answer = QMessageBox.warning(
                self,
                "文件名导入",
                "官方工具不会重命名真实文件。仅在发布副本已准备好目标文件时启用。",
                QMessageBox.Ok | QMessageBox.Cancel,
            )
            if answer != QMessageBox.Ok:
                self.scope_checks["filename"].blockSignals(True)
                self.scope_checks["filename"].setChecked(False)
                self.scope_checks["filename"].blockSignals(False)
        new_scope = ImportScope(**{name: check.isChecked() for name, check in self.scope_checks.items()})
        pipeline = Pipeline(
            self.current_manifest_path,
            self.settings,
            "",
            local_data_dir(),
        )
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
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
        manifest = load_manifest(self.current_manifest_path)
        reset = False
        for stage in STAGE_ORDER:
            if stage is Stage.TRANSLATE:
                reset = True
            if reset:
                record = manifest.version.stage(stage)
                record.status = StageStatus.PENDING
                record.error = ""
        manifest_path_tmp = self.current_manifest_path.with_name("project.json.tmp")
        manifest_path_tmp.write_text(json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(manifest_path_tmp, self.current_manifest_path)
        self._load_project_view()
        self.status_label.setText("术语已保存")

    def _start(self, stage: Stage | None = None) -> None:
        if not self.current_manifest_path or (self.pipeline_thread and self.pipeline_thread.isRunning()):
            return
        if stage is not None:
            self.active_step_stage = stage
            self.step_mode.setChecked(True)
            self._select_workflow_mode(RunMode.STEP)
        errors = validate_settings(self.settings) if stage is None else []
        if errors:
            QMessageBox.warning(self, "设置未完成", "\n".join(errors))
            return
        try:
            try:
                key = self.store.api_key(self.settings)
            except Exception:
                if stage is None:
                    raise
                key = ""
            self.pipeline = Pipeline(
                self.current_manifest_path,
                self.settings,
                key,
                local_data_dir(),
            )
            self.pipeline_thread = PipelineThread(self.pipeline, stage)
            self.pipeline_thread.log_line.connect(self._append_log)
            self.pipeline_thread.stage_progress.connect(self._stage_progress)
            self.pipeline_thread.result_ready.connect(self._pipeline_result)
            self.pipeline_thread.failed.connect(self._pipeline_failed)
            self.pipeline_thread.finished.connect(self._pipeline_finished)
            self.start_button.setEnabled(False)
            for button in self.step_buttons.values():
                button.setEnabled(False)
            for button in self.step_skip_buttons.values():
                button.setEnabled(False)
            self.retry_button.setEnabled(False)
            self.stop_button.setEnabled(True)
            for check in self.scope_checks.values():
                check.setEnabled(False)
            self.status_label.setText(
                f"正在执行：{STAGE_LABELS[stage]}" if stage is not None else "运行中"
            )
            self.pipeline_thread.start()
        except Exception as exc:
            QMessageBox.critical(self, "无法启动", str(exc))

    def _append_log(self, message: str) -> None:
        self.log_view.appendPlainText(message)

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

    def _skip_stage(self, stage: Stage) -> None:
        if not self.current_manifest_path or (self.pipeline_thread and self.pipeline_thread.isRunning()):
            return
        try:
            pipeline = Pipeline(
                self.current_manifest_path,
                self.settings,
                "",
                local_data_dir(),
                log=self._append_log,
            )
            pipeline.skip_stage(stage)
            self.active_step_stage = stage
            self.status_label.setText(f"已跳过：{STAGE_LABELS[stage]}")
            self._load_project_view()
        except Exception as exc:
            QMessageBox.critical(self, "无法跳过", str(exc))

    def _stage_progress(self, current: int, total: int, _stage: str) -> None:
        if total == 0:
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, total)
            self.progress.setValue(current)
        self._load_project_view()

    def _pipeline_result(self, result: str) -> None:
        target = self.pipeline_thread.stage if self.pipeline_thread else None
        self.status_label.setText(f"已完成：{STAGE_LABELS[target]}" if target is not None else "已完成")
        self._load_project_view()

    def _pipeline_failed(self, detail: str) -> None:
        target = self.pipeline_thread.stage if self.pipeline_thread else None
        self.status_label.setText(f"出现错误：{STAGE_LABELS[target]}" if target is not None else "出现错误")
        self.tabs.setCurrentIndex(0)
        self._load_project_view()
        title = "步骤执行错误" if target is not None else "流水线失败"
        QMessageBox.critical(self, title, detail.splitlines()[-1] if detail.splitlines() else detail)

    def _pipeline_finished(self) -> None:
        self.stop_button.setEnabled(False)
        for check in self.scope_checks.values():
            check.setEnabled(True)
        self.pipeline_thread = None
        self.pipeline = None
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
            )
            pipeline.retry_failed()
            self._load_project_view()
            self._start()
        except Exception as exc:
            QMessageBox.critical(self, "无法重试", str(exc))

    def _open_release(self) -> None:
        if not self.current_manifest_path:
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
