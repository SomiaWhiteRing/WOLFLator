from __future__ import annotations

import base64
import ctypes
import os
from ctypes import wintypes
from pathlib import Path

from PySide6.QtCore import QSettings, QStandardPaths

from models import AppSettings


APP_NAME = "WOLFLator"


def config_dir() -> Path:
    path = Path(QStandardPaths.writableLocation(QStandardPaths.AppConfigLocation))
    path.mkdir(parents=True, exist_ok=True)
    return path


def local_data_dir() -> Path:
    path = Path(QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation))
    path.mkdir(parents=True, exist_ok=True)
    return path


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


if os.name == "nt":
    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL
    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    _kernel32.LocalFree.restype = wintypes.HLOCAL


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    value = _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    return value, buffer


def protect_secret(secret: str) -> str:
    if not secret:
        return ""
    if os.name != "nt":
        raise OSError("API 密钥持久化仅支持 Windows DPAPI。")
    source, source_buffer = _blob(secret.encode("utf-8"))
    output = _DataBlob()
    if not _crypt32.CryptProtectData(
        ctypes.byref(source), APP_NAME, None, None, None, 1, ctypes.byref(output)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        encrypted = ctypes.string_at(output.pbData, output.cbData)
        return base64.b64encode(encrypted).decode("ascii")
    finally:
        _kernel32.LocalFree(ctypes.cast(output.pbData, wintypes.HLOCAL))
        del source_buffer


def unprotect_secret(encoded: str) -> str:
    if not encoded:
        return ""
    if os.name != "nt":
        raise OSError("API 密钥持久化仅支持 Windows DPAPI。")
    source, source_buffer = _blob(base64.b64decode(encoded))
    output = _DataBlob()
    if not _crypt32.CryptUnprotectData(
        ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(output.pbData, output.cbData).decode("utf-8")
    finally:
        _kernel32.LocalFree(ctypes.cast(output.pbData, wintypes.HLOCAL))
        del source_buffer


def default_ascii_runner_dir() -> str:
    public = os.environ.get("PUBLIC", r"C:\Users\Public")
    return str(Path(public) / APP_NAME / "bin")


class SettingsStore:
    KEYS = tuple(AppSettings.__dataclass_fields__)

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else config_dir() / "settings.ini"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._settings = QSettings(str(self.path), QSettings.IniFormat)

    def load(self) -> AppSettings:
        values: dict[str, object] = {}
        for name, field_info in AppSettings.__dataclass_fields__.items():
            default = field_info.default
            raw = self._settings.value(name, default)
            if isinstance(default, bool):
                raw = str(raw).lower() in {"1", "true", "yes"}
            elif isinstance(default, int):
                raw = int(raw or default)
            else:
                raw = str(raw or "")
            values[name] = raw
        item = AppSettings(**values)
        if not self._settings.contains("glossary_api_base_url"):
            item.glossary_api_base_url = item.api_base_url
        if not self._settings.contains("glossary_api_model"):
            item.glossary_api_model = item.api_model
        if not self._settings.contains("glossary_api_key_blob"):
            item.glossary_api_key_blob = item.api_key_blob
        if not self._settings.contains("glossary_api_timeout"):
            item.glossary_api_timeout = item.api_timeout
        if not item.ascii_runner_dir:
            item.ascii_runner_dir = default_ascii_runner_dir()
        if not item.projects_root:
            item.projects_root = str(Path.home() / "Documents" / APP_NAME)
        return item

    def save(self, item: AppSettings) -> None:
        for name in self.KEYS:
            self._settings.setValue(name, getattr(item, name))
        self._settings.sync()
        if self._settings.status() != QSettings.NoError:
            raise OSError(f"无法保存设置: {self.path}")

    def set_api_key(self, item: AppSettings, secret: str) -> None:
        item.api_key_blob = protect_secret(secret.strip())

    def set_glossary_api_key(self, item: AppSettings, secret: str) -> None:
        item.glossary_api_key_blob = protect_secret(secret.strip())

    @staticmethod
    def api_key(item: AppSettings) -> str:
        return unprotect_secret(item.api_key_blob)

    @staticmethod
    def glossary_api_key(item: AppSettings) -> str:
        return unprotect_secret(item.glossary_api_key_blob)


def validate_settings(item: AppSettings, require_api: bool = True) -> list[str]:
    errors: list[str] = []
    wolf_path = Path(item.wolf_tool_path)
    if not wolf_path.is_file():
        errors.append("请选择官方 WOLF Translation Support Tool。")
    elif not (wolf_path.parent / "LibXL.dll").is_file():
        errors.append("官方工具目录缺少 LibXL.dll。")
    if not item.ainiee_source or not Path(item.ainiee_source).exists():
        errors.append("请选择或安装 AiNiee-Next。")
    if require_api:
        for label, base_url, model, key_blob in (
            ("术语生成", item.glossary_api_base_url, item.glossary_api_model, item.glossary_api_key_blob),
            ("AiNiee 翻译", item.api_base_url, item.api_model, item.api_key_blob),
        ):
            if not base_url.strip() or not model.strip():
                errors.append(f"请填写{label} API 基础地址和模型。")
            try:
                key = unprotect_secret(key_blob)
            except Exception:
                key = ""
            if not key:
                errors.append(f"请填写{label} API 密钥。")
    if not item.license_accepted:
        errors.append("请确认 FreeGames 工具许可范围。")
    runner = Path(item.ascii_runner_dir)
    if not str(runner).isascii():
        errors.append("UberWolf 执行目录必须是纯 ASCII 路径。")
    return errors
