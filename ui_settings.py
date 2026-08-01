from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QProgressBar,
    QPushButton, QSizePolicy, QSpinBox, QStackedWidget, QTabWidget, QTableWidget,
    QVBoxLayout, QWidget,
)

from ainiee import AINIEE_VERSION, locate_ainiee_source, remove_managed_ainiee
from settings import SettingsStore, local_data_dir, validate_settings
from ui_workers import ApiTestThread, EditorInstallThread, InstallThread
from wolf_editor import EDITOR_DOWNLOAD_URL, inspect_wolf_editor


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

        self.settings_tabs = QTabWidget()

        tools_page = QWidget()
        form = QFormLayout(tools_page)
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
        self.auto_convert_legacy_games = QCheckBox("自动转换 Ver2 及更早版本的游戏")
        self.auto_convert_legacy_games.setChecked(
            self.settings.auto_convert_legacy_games
        )
        form.addRow("", self.auto_convert_legacy_games)
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
        self.settings_tabs.addTab(tools_page, "工具与目录")

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
        self.settings_tabs.addTab(glossary_page, "术语生成 API")

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
        chunking.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
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
        self.translation_chunk_stack.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Fixed
        )
        token_limits = QWidget()
        token_limits_layout = QHBoxLayout(token_limits)
        token_limits_layout.setContentsMargins(0, 0, 0, 0)
        self.translation_token_limit = QSpinBox()
        self.translation_token_limit.setRange(64, 8192)
        self.translation_token_limit.setFixedWidth(140)
        self.translation_token_limit.setValue(self.settings.translation_token_limit)
        self.translation_token_unit = QLabel("Token")
        token_limits_layout.addWidget(self.translation_token_limit)
        token_limits_layout.addWidget(self.translation_token_unit)
        token_limits_layout.addStretch(1)
        self.translation_chunk_stack.addWidget(token_limits)

        line_limits = QWidget()
        line_limits_layout = QHBoxLayout(line_limits)
        line_limits_layout.setContentsMargins(0, 0, 0, 0)
        self.translation_line_limit = QSpinBox()
        self.translation_line_limit.setRange(1, 100)
        self.translation_line_limit.setFixedWidth(90)
        self.translation_line_limit.setValue(self.settings.translation_line_limit)
        self.translation_line_unit = QLabel("条")
        self.translation_retry_min_lines = QSpinBox()
        self.translation_retry_min_lines.setRange(1, 100)
        self.translation_retry_min_lines.setFixedWidth(90)
        self.translation_retry_min_lines.setValue(self.settings.translation_retry_min_lines)
        self.translation_retry_unit = QLabel("条")
        line_limits_layout.addWidget(self.translation_line_limit)
        line_limits_layout.addWidget(self.translation_line_unit)
        line_limits_layout.addWidget(QLabel("重试最小"))
        line_limits_layout.addWidget(self.translation_retry_min_lines)
        line_limits_layout.addWidget(self.translation_retry_unit)
        line_limits_layout.addStretch(1)
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

        rounds = QWidget()
        rounds_layout = QHBoxLayout(rounds)
        rounds_layout.setContentsMargins(0, 0, 0, 0)
        self.translation_rounds = QSpinBox()
        self.translation_rounds.setRange(1, 20)
        self.translation_rounds.setFixedWidth(140)
        self.translation_rounds.setValue(self.settings.translation_rounds)
        self.translation_rounds_unit = QLabel("轮")
        rounds_layout.addWidget(self.translation_rounds)
        rounds_layout.addWidget(self.translation_rounds_unit)
        rounds_layout.addStretch(1)
        translation_form.addRow("单次最大轮次", rounds)
        self.settings_tabs.addTab(translation_page, "AiNiee 翻译 API")
        layout.addWidget(self.settings_tabs)

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
        item.auto_convert_legacy_games = self.auto_convert_legacy_games.isChecked()
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
