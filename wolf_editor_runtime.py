from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, replace
from html.parser import HTMLParser
from pathlib import Path, PurePath
from typing import Callable, Iterator

from formats import ARTIFACT_EPOCH, require_format
from models import TranslationItem
from process_tools import (
    CancelledError, _kill_process_tree, _process_startupinfo, hash_directory,
    run_process, sha256_file,
)
from safe_io import (
    ResourceBusyError, ResourceLock, atomic_output_path, atomic_write_json,
    package_lock, replace_with_retry,
)
from wolf_auto import _CommandBlock, _database_index, _event_blocks
from wolf_semantics import analyze_auto_export
from wolf_semantics_engine import _event_codes, _event_name_codes, _map_ids_from_databases
from wolf_workbook import COPY_FROM_RE


EDITOR_DOWNLOAD_URL = "https://silversecond.com/WolfRPGEditor/Download.shtml"


MAX_EDITOR_PAGE_BYTES = 2 * 1024 * 1024


MAX_EDITOR_ARCHIVE_BYTES = 256 * 1024 * 1024


MIN_EDITOR_VERSION = (3, 500)


@contextmanager
def _editor_execution_lock(
    editor: EditorInfo,
    *,
    cancel_event: threading.Event | None,
    diagnostic_log: Callable[[str], None] | None,
    warning: Callable[[str], None] | None,
) -> Iterator[None]:
    lock_root = Path(
        os.environ.get("LOCALAPPDATA", tempfile.gettempdir())
    ) / "WOLFLator" / "locks"
    lock_path = lock_root / f"editor-{editor.sha256}.lock"
    started = time.monotonic()
    warned = False
    queued_logged = False
    lock: ResourceLock | None = None
    while lock is None:
        candidate = ResourceLock(
            lock_path,
            "editor-export",
            resource_path=editor.path,
        )
        try:
            candidate.__enter__()
        except ResourceBusyError:
            elapsed = time.monotonic() - started
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledError("等待 WOLF RPG Editor 时任务已取消。")
            if elapsed >= 1800:
                raise TimeoutError("等待 WOLF RPG Editor 独占执行超过 1800 秒。")
            if diagnostic_log and not queued_logged:
                queued_logged = True
                diagnostic_log(f"editor.queue.wait lock={lock_path}")
            if not warned and elapsed >= 300:
                warned = True
                if warning:
                    warning("等待其他 WOLF RPG Editor 任务已超过 5 分钟。")
            time.sleep(0.1)
        else:
            lock = candidate
    waited = time.monotonic() - started
    if diagnostic_log:
        diagnostic_log(
            f"editor.queue.acquired lock={lock_path} waited={waited:.3f}s"
        )
    try:
        yield
    finally:
        lock.__exit__(None, None, None)
        if diagnostic_log:
            diagnostic_log(f"editor.queue.released lock={lock_path}")


_OFFICIAL_EDITOR_HOSTS = {"silversecond.com", "www.silversecond.com"}


_EDITOR_ARCHIVE_RE = re.compile(
    r"^WolfRPGEditor_(?P<version>\d+(?:\.\d+)+)(?P<mini>mini)?\.zip$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class EditorInfo:
    path: Path
    version: str
    version_tuple: tuple[int, ...]
    sha256: str


@dataclass(frozen=True)
class EditorRelease:
    version: str
    version_tuple: tuple[int, ...]
    url: str
    mini: bool


@dataclass(frozen=True)
class EditorExportResult:
    auto_dir: Path
    analysis_path: Path
    editor: EditorInfo
    warning_count: int
    warnings: list[dict[str, object]]


@dataclass(frozen=True)
class LegacyConversionResult:
    editor: EditorInfo
    runtime_path: Path
    runtime_sha256: str
    log_path: Path
    report_path: Path
    before_hash: str
    after_hash: str
    converted_files: int


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = next((value for key, value in attrs if key.lower() == "href"), None)
        if href:
            self.hrefs.append(href)


class _VSFixedFileInfo(ctypes.Structure):
    _fields_ = [
        ("dwSignature", ctypes.c_uint32),
        ("dwStrucVersion", ctypes.c_uint32),
        ("dwFileVersionMS", ctypes.c_uint32),
        ("dwFileVersionLS", ctypes.c_uint32),
        ("dwProductVersionMS", ctypes.c_uint32),
        ("dwProductVersionLS", ctypes.c_uint32),
        ("dwFileFlagsMask", ctypes.c_uint32),
        ("dwFileFlags", ctypes.c_uint32),
        ("dwFileOS", ctypes.c_uint32),
        ("dwFileType", ctypes.c_uint32),
        ("dwFileSubtype", ctypes.c_uint32),
        ("dwFileDateMS", ctypes.c_uint32),
        ("dwFileDateLS", ctypes.c_uint32),
    ]


def _windows_version_resource(path: Path) -> tuple[str, tuple[int, ...], str]:
    if os.name != "nt":
        raise OSError("WOLF RPG Editor 版本探测仅支持 Windows。")
    version = ctypes.WinDLL("version", use_last_error=True)
    size = version.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        raise ValueError("Editor.exe 缺少 Windows 版本资源。")
    buffer = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(str(path), 0, size, buffer):
        raise ctypes.WinError(ctypes.get_last_error())

    pointer = ctypes.c_void_p()
    length = ctypes.c_uint()
    if not version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
        raise ValueError("Editor.exe 缺少固定版本信息。")
    fixed = ctypes.cast(pointer, ctypes.POINTER(_VSFixedFileInfo)).contents
    parts = (
        fixed.dwFileVersionMS >> 16,
        fixed.dwFileVersionMS & 0xFFFF,
        fixed.dwFileVersionLS >> 16,
        fixed.dwFileVersionLS & 0xFFFF,
    )
    display_parts = list(parts)
    while len(display_parts) > 2 and display_parts[-1] == 0:
        display_parts.pop()
    version_text = ".".join(str(value) for value in display_parts)

    description = ""
    translations_pointer = ctypes.c_void_p()
    translations_length = ctypes.c_uint()
    if version.VerQueryValueW(
        buffer,
        "\\VarFileInfo\\Translation",
        ctypes.byref(translations_pointer),
        ctypes.byref(translations_length),
    ):
        translations = ctypes.cast(
            translations_pointer, ctypes.POINTER(ctypes.c_ushort)
        )
        for index in range(0, translations_length.value // 2, 2):
            key = f"\\StringFileInfo\\{translations[index]:04x}{translations[index + 1]:04x}\\FileDescription"
            value_pointer = ctypes.c_void_p()
            value_length = ctypes.c_uint()
            if version.VerQueryValueW(
                buffer, key, ctypes.byref(value_pointer), ctypes.byref(value_length)
            ):
                description = ctypes.wstring_at(value_pointer, value_length.value).rstrip("\0")
                if description:
                    break
    return version_text, parts, description


def inspect_wolf_editor(
    path: str | Path,
    *,
    version_resource: Callable[[Path], tuple[str, tuple[int, ...], str]] = _windows_version_resource,
) -> EditorInfo:
    executable = Path(path).resolve()
    if executable.name.lower() != "editor.exe" or not executable.is_file():
        raise ValueError("请选择名为 Editor.exe 的 WOLF RPG Editor。")
    version, version_tuple, description = version_resource(executable)
    if description != "WOLF RPG Editor":
        raise ValueError(f"文件说明不是 WOLF RPG Editor：{description or '缺失'}")
    if version_tuple[:2] < MIN_EDITOR_VERSION:
        raise ValueError(f"WOLF RPG Editor 版本过旧：{version}，最低需要 3.500。")
    return EditorInfo(executable, version, version_tuple, sha256_file(executable))


def _official_url(url: str) -> bool:
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme == "https" and (parsed.hostname or "").lower() in _OFFICIAL_EDITOR_HOSTS


def _release_from_url(url: str) -> EditorRelease | None:
    if not _official_url(url):
        return None
    filename = urllib.parse.unquote(Path(urllib.parse.urlparse(url).path).name)
    match = _EDITOR_ARCHIVE_RE.fullmatch(filename)
    if not match:
        return None
    version = match.group("version")
    version_tuple = tuple(int(value) for value in version.split("."))
    if version_tuple[:2] < MIN_EDITOR_VERSION:
        return None
    return EditorRelease(version, version_tuple, url, bool(match.group("mini")))


def latest_editor_release_from_html(html: str, base_url: str = EDITOR_DOWNLOAD_URL) -> EditorRelease:
    parser = _LinkParser()
    parser.feed(html)
    releases = [
        release
        for href in parser.hrefs
        if (release := _release_from_url(urllib.parse.urljoin(base_url, href))) is not None
    ]
    if not releases:
        raise ValueError("WOLF RPG Editor 官网没有可识别的 3.500 以上下载包。")
    # ponytail: Prefer mini only after choosing the highest numeric version.
    return max(releases, key=lambda item: (item.version_tuple, item.mini))


def discover_latest_editor_release() -> EditorRelease:
    request = urllib.request.Request(
        EDITOR_DOWNLOAD_URL,
        headers={"User-Agent": "WOLFLator/1.0"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        final_url = response.geturl()
        if not _official_url(final_url):
            raise ValueError(f"Editor 官网被重定向到非官方地址：{final_url}")
        data = response.read(MAX_EDITOR_PAGE_BYTES + 1)
    if len(data) > MAX_EDITOR_PAGE_BYTES:
        raise ValueError("Editor 官网页面超过允许大小。")
    return latest_editor_release_from_html(data.decode("utf-8", errors="replace"), final_url)


def _download_editor_archive(
    release: EditorRelease,
    target: Path,
    *,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[str, int]:
    if not _official_url(release.url):
        raise ValueError(f"Editor 下载地址不是官方网站：{release.url}")
    request = urllib.request.Request(release.url, headers={"User-Agent": "WOLFLator/1.0"})
    digest = hashlib.sha256()
    received = 0
    with urllib.request.urlopen(request, timeout=60) as response, target.open("wb") as writer:
        final_url = response.geturl()
        if not _official_url(final_url):
            raise ValueError(f"Editor 下载被重定向到非官方地址：{final_url}")
        total = int(response.headers.get("Content-Length", "0") or 0)
        if total > MAX_EDITOR_ARCHIVE_BYTES:
            raise ValueError("Editor 下载包超过允许大小。")
        while True:
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            received += len(chunk)
            if received > MAX_EDITOR_ARCHIVE_BYTES:
                raise ValueError("Editor 下载包超过允许大小。")
            writer.write(chunk)
            digest.update(chunk)
            if progress:
                progress(received, total)
    if total and received != total:
        raise ValueError(f"Editor 下载包大小不完整：预期 {total}，实际 {received}")
    return digest.hexdigest(), received


def _extract_managed_editor(
    archive: Path,
    destination: Path,
    release: EditorRelease,
    *,
    inspect_editor: Callable[[str | Path], EditorInfo] = inspect_wolf_editor,
) -> EditorInfo:
    # ponytail: WOLFLator needs only Editor.exe; the mini package's authoring extras stay optional.
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as package:
        members = [
            member
            for member in package.infolist()
            if member.filename.replace("\\", "/") == "Editor.exe"
        ]
        if len(members) != 1:
            raise ValueError("Editor 官方包缺少唯一的顶层 Editor.exe。")
        member = members[0]
        file_type = (member.external_attr >> 16) & 0o170000
        if member.is_dir() or file_type == 0o120000 or member.file_size > MAX_EDITOR_ARCHIVE_BYTES:
            raise ValueError("Editor 官方包中的 Editor.exe 结构异常。")
        executable = destination / "Editor.exe"
        with package.open(member) as source, executable.open("wb") as target:
            shutil.copyfileobj(source, target, 1024 * 1024)
    info = inspect_editor(executable)
    if info.version_tuple[: len(release.version_tuple)] != release.version_tuple:
        raise ValueError(
            f"Editor.exe 版本 {info.version} 与下载包版本 {release.version} 不一致。"
        )
    return info


def _validate_managed_editor(
    root: Path,
    release: EditorRelease | None = None,
    *,
    inspect_editor: Callable[[str | Path], EditorInfo] = inspect_wolf_editor,
) -> Path:
    metadata_path = root / "wolflator-package.json"
    if not metadata_path.is_file():
        raise ValueError("WOLF RPG Editor 托管包缺少安装元数据。")
    metadata = require_format(
        json.loads(metadata_path.read_text(encoding="utf-8")),
        kind="editor-package",
        version_key="epoch",
        version=ARTIFACT_EPOCH,
        label="WOLF RPG Editor 托管包元数据",
    )
    expected_fields = {
        "kind",
        "epoch",
        "version",
        "source_url",
        "archive_size",
        "archive_sha256",
        "editor_sha256",
        "installed_at",
    }
    if set(metadata) != expected_fields or not isinstance(metadata["archive_size"], int) or not isinstance(
        metadata["installed_at"], (int, float)
    ):
        raise ValueError("WOLF RPG Editor 托管包元数据字段不匹配。")
    try:
        version = str(metadata["version"])
        source_url = str(metadata["source_url"])
        archive_sha256 = str(metadata["archive_sha256"])
        editor_sha256 = str(metadata["editor_sha256"])
    except KeyError as error:
        raise ValueError(f"WOLF RPG Editor 托管包元数据缺少：{error.args[0]}") from error
    metadata_release = _release_from_url(source_url)
    if (
        metadata_release is None
        or metadata_release.version != version
        or not re.fullmatch(r"[0-9a-f]{64}", archive_sha256)
        or not re.fullmatch(r"[0-9a-f]{64}", editor_sha256)
    ):
        raise ValueError("WOLF RPG Editor 托管包元数据不匹配。")
    if release and metadata_release != release:
        raise ValueError("WOLF RPG Editor 托管包不是官网当前版本。")
    executable = root / "Editor.exe"
    info = inspect_editor(executable)
    if info.version_tuple[: len(metadata_release.version_tuple)] != metadata_release.version_tuple:
        raise ValueError("托管 Editor.exe 版本与安装元数据不匹配。")
    if info.sha256 != editor_sha256:
        raise ValueError(f"托管 Editor.exe SHA-256 不匹配：{info.sha256}")
    return executable


def install_supported_editor(
    packages_root: str | Path,
    *,
    repair: bool = False,
    progress: Callable[[int, int], None] | None = None,
    log: Callable[[str], None] | None = None,
    discover_release: Callable[[], EditorRelease] = discover_latest_editor_release,
    download_archive: Callable[..., tuple[str, int]] = _download_editor_archive,
    inspect_editor: Callable[[str | Path], EditorInfo] = inspect_wolf_editor,
) -> Path:
    packages = Path(packages_root)
    with package_lock(packages, "install-wolf-editor"):
        packages.mkdir(parents=True, exist_ok=True)
        if log:
            log("正在检查 WOLF RPG Editor 官网最新版本...")
        release = discover_release()
        final = packages / release.version
        if final.exists() and not repair:
            try:
                executable = _validate_managed_editor(
                    final,
                    release,
                    inspect_editor=inspect_editor,
                )
                if log:
                    log(f"WOLF RPG Editor {release.version} 已是官网最新版本。")
                return executable
            except (OSError, ValueError, json.JSONDecodeError):
                pass

        part = packages / f".{release.version}.zip.part"
        staging = Path(tempfile.mkdtemp(prefix=f".{release.version}.", dir=packages))
        if log:
            kind = "mini 包" if release.mini else "完整包"
            log(f"正在从官方网站下载 WOLF RPG Editor {release.version} {kind}...")
        try:
            part.unlink(missing_ok=True)
            archive_sha256, archive_size = download_archive(
                release,
                part,
                progress=progress,
            )
            info = _extract_managed_editor(
                part,
                staging,
                release,
                inspect_editor=inspect_editor,
            )
            atomic_write_json(
                staging / "wolflator-package.json",
                {
                    "kind": "editor-package",
                    "epoch": ARTIFACT_EPOCH,
                    "version": release.version,
                    "source_url": release.url,
                    "archive_size": archive_size,
                    "archive_sha256": archive_sha256,
                    "editor_sha256": info.sha256,
                    "installed_at": time.time(),
                },
            )
            _validate_managed_editor(staging, release, inspect_editor=inspect_editor)
            if final.exists():
                shutil.rmtree(final)
            replace_with_retry(staging, final)
            if log:
                log(f"WOLF RPG Editor 已安装到 {final}")
            return _validate_managed_editor(
                final,
                release,
                inspect_editor=inspect_editor,
            )
        finally:
            part.unlink(missing_ok=True)
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)


def _inspect_matching_runtime(
    path: Path,
    editor: EditorInfo,
    *,
    version_resource: Callable[[Path], tuple[str, tuple[int, ...], str]] = _windows_version_resource,
) -> str:
    version, version_tuple, description = version_resource(path)
    if path.name.casefold() != "game.exe" or description != "Game / WOLF RPG Editor":
        raise ValueError("文件不是 WOLF RPG Editor 的 Game.exe。")
    if version_tuple != editor.version_tuple:
        raise ValueError(
            f"Game.exe 版本 {version} 与 Editor.exe {editor.version} 不一致。"
        )
    return sha256_file(path)


def _matching_editor_runtime(
    editor: EditorInfo,
    cache_root: str | Path,
    *,
    log: Callable[[str], None] | None,
) -> tuple[Path, str]:
    sibling = editor.path.with_name("Game.exe")
    try:
        return sibling, _inspect_matching_runtime(sibling, editor)
    except (OSError, ValueError):
        pass

    cache = Path(cache_root).resolve()
    version = ".".join(editor.version.split(".")[:2])
    release = EditorRelease(
        version,
        tuple(int(part) for part in version.split(".")),
        f"https://www.silversecond.com/WolfRPGEditor/Data/WolfRPGEditor_{version}.zip",
        False,
    )
    final = cache / version
    metadata_path = final / "wolflator-runtime.json"
    with package_lock(cache, "install-wolf-runtime"):
        try:
            metadata = require_format(
                json.loads(metadata_path.read_text(encoding="utf-8")),
                kind="editor-runtime",
                version_key="epoch",
                version=ARTIFACT_EPOCH,
                label="Editor 运行时元数据",
            )
            if set(metadata) != {
                "kind",
                "epoch",
                "source_url",
                "archive_size",
                "archive_sha256",
                "editor_version",
                "game_sha256",
            } or type(metadata["archive_size"]) is not int or any(
                not isinstance(metadata[name], str)
                for name in (
                    "source_url",
                    "archive_sha256",
                    "editor_version",
                    "game_sha256",
                )
            ):
                raise ValueError("Editor 运行时元数据字段不匹配。")
            runtime = final / "Game.exe"
            runtime_sha256 = _inspect_matching_runtime(runtime, editor)
            if (
                metadata.get("source_url") == release.url
                and metadata.get("editor_version") == editor.version
                and metadata.get("game_sha256") == runtime_sha256
            ):
                return runtime, runtime_sha256
        except (OSError, ValueError, json.JSONDecodeError):
            pass

        cache.mkdir(parents=True, exist_ok=True)
        part = cache / f".{version}.runtime.zip.part"
        staging = Path(tempfile.mkdtemp(prefix=f".{version}.runtime.", dir=cache))
        if log:
            log(f"Editor 目录缺少配套 Game.exe，正在下载官方 {version} 完整包...")
        try:
            part.unlink(missing_ok=True)
            archive_sha256, archive_size = _download_editor_archive(release, part)
            with zipfile.ZipFile(part) as package:
                members = [
                    member
                    for member in package.infolist()
                    if PurePath(member.filename.replace("\\", "/")).name.casefold()
                    == "game.exe"
                ]
                if len(members) != 1:
                    raise ValueError("Editor 官方完整包缺少唯一的 Game.exe。")
                member = members[0]
                file_type = (member.external_attr >> 16) & 0o170000
                if (
                    member.is_dir()
                    or file_type == 0o120000
                    or member.file_size > MAX_EDITOR_ARCHIVE_BYTES
                ):
                    raise ValueError("Editor 官方包中的 Game.exe 结构异常。")
                runtime = staging / "Game.exe"
                with package.open(member) as source, runtime.open("wb") as target:
                    shutil.copyfileobj(source, target, 1024 * 1024)
            runtime_sha256 = _inspect_matching_runtime(runtime, editor)
            atomic_write_json(
                staging / "wolflator-runtime.json",
                {
                    "kind": "editor-runtime",
                    "epoch": ARTIFACT_EPOCH,
                    "source_url": release.url,
                    "archive_size": archive_size,
                    "archive_sha256": archive_sha256,
                    "editor_version": editor.version,
                    "game_sha256": runtime_sha256,
                },
            )
            shutil.rmtree(final, ignore_errors=True)
            replace_with_retry(staging, final)
            return final / "Game.exe", runtime_sha256
        finally:
            part.unlink(missing_ok=True)
            shutil.rmtree(staging, ignore_errors=True)


# ponytail: This fail-closed dialog contract is calibrated against the official
# Japanese 3.713 UI; recalibrate the tokens if a later Editor changes the flow.
def _legacy_conversion_action(
    title: str,
    text: str,
    has_buttons: bool,
    *,
    started: bool,
) -> tuple[str, str] | None:
    message = f"{title} | {text}"
    if "Ver2以前" in title and "コンバート" in title:
        if not has_buttons:
            return None
        return None if started else ("start", "start")
    if "Ver3では挙動が大きく変わります" in message:
        return ("legacy-behavior", "no")
    for token, action in (
        ("旧Ver2.29時点の挙動", "legacy-confirmed"),
        ("バックアップを開始します", "backup-start"),
        ("バックアップが完了しました", "backup-complete"),
        ("コンバート作業を開始します", "conversion-start"),
        ("コンバート作業が完了しました", "conversion-complete"),
    ):
        if token in message:
            return (action, "ok")
    if not text.strip():
        return None
    if has_buttons:
        raise RuntimeError(f"Editor 自动转换出现未识别的对话框：{message[:1000]}")
    return None


def _legacy_dialog_button(
    buttons: list[tuple[int, int, str]], role: str
) -> tuple[int, int, str] | None:
    if role == "no":
        return next(
            (
                button
                for button in buttons
                if button[0] == 7 or button[2].replace("&", "") in {"いいえ", "No"}
            ),
            None,
        )
    if role == "ok":
        selected = next(
            (
                button
                for button in buttons
                if button[0] == 1
                or button[2].replace("&", "").casefold() == "ok"
                or button[2] == "确定"
            ),
            None,
        )
        return selected or (buttons[0] if len(buttons) == 1 else None)
    candidates = [
        button
        for button in buttons
        if "コンバート" in button[2] or "開始" in button[2]
    ]
    return candidates[0] if len(candidates) == 1 else (buttons[0] if len(buttons) == 1 else None)


def _drive_legacy_conversion(
    process: subprocess.Popen[bytes],
    game_root: Path,
    *,
    timeout: int,
    cancel_event: threading.Event | None,
    diagnostic_log: Callable[[str], None] | None,
    warning: Callable[[str], None] | None,
) -> tuple[Path, set[str]]:
    if os.name != "nt":
        raise OSError("WOLF RPG Editor 自动转换仅支持 Windows。")
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    started_at = time.monotonic()
    actions: set[str] = set()
    slow_warning_sent = False
    main_windows: set[int] = set()

    def window_text(window: int) -> str:
        length = user32.GetWindowTextLengthW(window)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(window, buffer, length + 1)
        return buffer.value.strip()

    def process_windows() -> list[tuple[int, str, str, list[tuple[int, int, str]]]]:
        found: list[tuple[int, str, str, list[tuple[int, int, str]]]] = []

        @callback_type
        def visit_window(window, _parameter):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(window, ctypes.byref(owner))
            if owner.value != process.pid:
                return True
            user32.ShowWindow(window, 0)
            class_name = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(window, class_name, len(class_name))
            title = window_text(window)
            body: list[str] = []
            buttons: list[tuple[int, int, str]] = []

            @callback_type
            def visit_child(child, _child_parameter):
                child_class = ctypes.create_unicode_buffer(128)
                user32.GetClassNameW(child, child_class, len(child_class))
                child_text = window_text(child)
                if "button" in child_class.value.casefold():
                    buttons.append(
                        (int(user32.GetDlgCtrlID(child)), int(child), child_text)
                    )
                elif child_text:
                    body.append(child_text)
                return True

            user32.EnumChildWindows(window, visit_child, 0)
            found.append((int(window), title, " | ".join(body), buttons))
            return True

        user32.EnumWindows(visit_window, 0)
        return found

    conversion_log = game_root / "Backup_Before_Ver3" / "ConvertLog.txt"
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise CancelledError("Editor 自动转换已取消。")
            elapsed = time.monotonic() - started_at
            if elapsed > timeout:
                raise TimeoutError(f"Editor 自动转换超过 {timeout} 秒。")
            if not slow_warning_sent and elapsed >= 300:
                slow_warning_sent = True
                if warning:
                    warning("WOLF RPG Editor 自动转换已运行超过 5 分钟，请继续等待。")
            if process.poll() is not None:
                raise RuntimeError(
                    f"Editor 在转换完成前退出，退出码 {process.returncode}。"
                )

            windows = process_windows()
            for window, title, body, buttons in windows:
                if "Ver2以前" in title and "コンバート" in title:
                    main_windows.add(window)
                action = _legacy_conversion_action(
                    title,
                    body,
                    bool(buttons),
                    started="start" in actions,
                )
                if action is None or action[0] in actions:
                    continue
                name, role = action
                selected = _legacy_dialog_button(buttons, role)
                button = selected[1] if selected else None
                if button is None:
                    raise RuntimeError(
                        f"Editor 自动转换对话框缺少 {role} 按钮：{title} | {body}；"
                        f"buttons={[(item[0], item[2]) for item in buttons]}"
                    )
                actions.add(name)
                if diagnostic_log:
                    diagnostic_log(
                        f"editor.legacy.action name={name} window={window} "
                        f"button_id={selected[0]} button_text={selected[2]}"
                    )
                user32.SendMessageW(button, 0x00F5, 0, 0)  # BM_CLICK

            if "conversion-complete" in actions and conversion_log.is_file():
                break
            time.sleep(0.05)
    finally:
        if process.poll() is None:
            for window in main_windows:
                user32.PostMessageW(window, 0x0010, 0, 0)  # WM_CLOSE
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _kill_process_tree(process)
                process.wait()
    return conversion_log, actions


def convert_legacy_game(
    editor_path: str | Path,
    game_root: str | Path,
    evidence_dir: str | Path,
    *,
    runtime_cache: str | Path | None = None,
    cancel_event: threading.Event | None = None,
    log: Callable[[str], None] | None = None,
    diagnostic_log: Callable[[str], None] | None = None,
    warning: Callable[[str], None] | None = None,
) -> LegacyConversionResult:
    editor = inspect_wolf_editor(editor_path)
    game = Path(game_root).resolve()
    source_data = game / "Data"
    if not (source_data / "BasicData" / "Game.dat").is_file():
        raise ValueError("Editor 自动转换需要松散 Data/BasicData/Game.dat。")
    game_executable = game / "Game.exe"
    if not game_executable.is_file():
        raise ValueError("Editor 自动转换需要工作副本中的 Game.exe。")
    output = Path(evidence_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    runtime_root = runtime_cache or (
        Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()))
        / "WOLFLator"
        / "packages"
        / "editor-runtime"
    )
    runtime, runtime_sha256 = _matching_editor_runtime(
        editor,
        runtime_root,
        log=log,
    )
    temporary_root = Path(
        tempfile.mkdtemp(prefix=".wolflator-legacy-", dir=game.parent)
    )
    staged_game = temporary_root / "game"
    backup_data = game.parent / f".{game.name}.legacy-backup-{time.time_ns():x}"
    backup_executable = game.parent / f".{game.name}.legacy-game-{time.time_ns():x}.exe"
    before_hash = hash_directory(source_data)
    before_executable_hash = sha256_file(game_executable)
    promoted = False
    try:
        if log:
            log("正在后台复制工作副本并调用官方 Editor 转换 Ver2 数据...")
        shutil.copytree(game, staged_game)
        shutil.copy2(editor.path, staged_game / "Editor.exe")
        shutil.copy2(runtime, staged_game / "Game.exe")
        shutil.rmtree(staged_game / "Backup_Before_Ver3", ignore_errors=True)
        with _editor_execution_lock(
            editor,
            cancel_event=cancel_event,
            diagnostic_log=diagnostic_log,
            warning=warning,
        ):
            process = subprocess.Popen(
                [str(staged_game / "Editor.exe")],
                cwd=staged_game,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                startupinfo=_process_startupinfo(True),
            )
            if diagnostic_log:
                diagnostic_log(
                    f"editor.legacy.start pid={process.pid} game={staged_game} editor={editor.path}"
                )
            conversion_log, actions = _drive_legacy_conversion(
                process,
                staged_game,
                timeout=1800,
                cancel_event=cancel_event,
                diagnostic_log=diagnostic_log,
                warning=warning,
            )

        log_text = conversion_log.read_text(encoding="utf-8")
        converted_files = log_text.count("●変換OK!")
        if (
            converted_files < 1
            or "Data/BasicData/Game.dat" not in log_text
            or re.search(r"失敗|エラー|\bError\b", log_text, re.IGNORECASE)
        ):
            raise RuntimeError("Editor 自动转换日志未通过校验。")
        required_actions = {
            "start",
            "legacy-behavior",
            "conversion-start",
            "conversion-complete",
        }
        missing_actions = required_actions - actions
        if missing_actions:
            raise RuntimeError(
                "Editor 自动转换没有完成预期步骤：" + ", ".join(sorted(missing_actions))
            )
        converted_data = staged_game / "Data"
        after_hash = hash_directory(converted_data)
        if before_hash == after_hash:
            raise RuntimeError("Editor 报告转换完成，但 Data 内容没有变化。")
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError("Editor 自动转换已取消。")

        evidence_log = output / "ConvertLog.txt"
        with atomic_output_path(evidence_log) as temporary:
            shutil.copy2(conversion_log, temporary)
        report_path = output / "conversion.json"
        atomic_write_json(
            report_path,
            {
                "kind": "legacy-conversion-report",
                "epoch": ARTIFACT_EPOCH,
                "editor_version": editor.version,
                "editor_sha256": editor.sha256,
                "before_data_hash": before_hash,
                "after_data_hash": after_hash,
                "before_game_exe_sha256": before_executable_hash,
                "after_game_exe_sha256": runtime_sha256,
                "runtime_path": str(runtime),
                "converted_files": converted_files,
                "runtime_behavior": "Ver2.29",
                "actions": sorted(actions),
                "log": str(evidence_log),
            },
        )

        shutil.copy2(game_executable, backup_executable)
        replace_with_retry(source_data, backup_data)
        try:
            replace_with_retry(converted_data, source_data)
            with atomic_output_path(game_executable) as temporary:
                shutil.copy2(runtime, temporary)
            if hash_directory(source_data) != after_hash:
                raise RuntimeError("转换后的 Data 提升校验失败。")
            if sha256_file(game_executable) != runtime_sha256:
                raise RuntimeError("Ver3 Game.exe 提升校验失败。")
        except Exception:
            shutil.rmtree(source_data, ignore_errors=True)
            replace_with_retry(backup_data, source_data)
            with atomic_output_path(game_executable) as temporary:
                shutil.copy2(backup_executable, temporary)
            raise
        promoted = True
        shutil.rmtree(backup_data, ignore_errors=True)
        backup_executable.unlink(missing_ok=True)
        if log:
            log(f"旧版数据转换完成，共转换 {converted_files} 个文件。")
        if diagnostic_log:
            diagnostic_log(
                f"editor.legacy.complete before={before_hash} after={after_hash} "
                f"files={converted_files} report={report_path}"
            )
        return LegacyConversionResult(
            editor,
            runtime,
            runtime_sha256,
            evidence_log,
            report_path,
            before_hash,
            after_hash,
            converted_files,
        )
    finally:
        if backup_data.exists():
            if promoted and source_data.exists():
                shutil.rmtree(backup_data, ignore_errors=True)
            elif not source_data.exists():
                replace_with_retry(backup_data, source_data)
        if backup_executable.exists():
            if promoted and game_executable.exists():
                backup_executable.unlink(missing_ok=True)
            elif not game_executable.exists():
                replace_with_retry(backup_executable, game_executable)
        shutil.rmtree(temporary_root, ignore_errors=True)


def _copy_editor_sandbox(editor: Path, game_root: Path, sandbox: Path) -> list[Path]:
    shutil.copy2(editor, sandbox / "Editor.exe")
    source_data = game_root / "Data"
    basic_data = source_data / "BasicData"
    if not basic_data.is_dir():
        raise ValueError("Editor 事件导出需要松散 Data/BasicData。")
    target_basic = sandbox / "Data" / "BasicData"
    target_basic.mkdir(parents=True)
    for source in sorted(basic_data.iterdir()):
        if source.is_file() and source.suffix.lower() in {".dat", ".project"}:
            shutil.copy2(source, target_basic / source.name)
    maps: list[Path] = []
    for index, source in enumerate(sorted(source_data.rglob("*.mps"))):
        if not source.is_file():
            continue
        relative = source.relative_to(source_data)
        # ponytail: Editor 3.713 fast-fails on some otherwise valid Unicode map
        # filenames. Auto output is byte-identical after an ASCII sandbox rename;
        # restore the original relative path immediately after export.
        target = sandbox / "Data" / "MapData" / f"WOLFLatorMap{index:08d}.mps"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        maps.append(relative)
    return maps


def _restore_editor_map_paths(auto_dir: Path, maps: list[Path]) -> None:
    for index, relative in enumerate(maps):
        generated = (
            auto_dir
            / "MapData"
            / f"WOLFLatorMap{index:08d}.mps.Auto.txt"
        )
        if not generated.is_file():
            raise ValueError(f"Editor 未生成地图事件 Auto.txt：{relative.as_posix()}")
        restored = auto_dir / relative.parent / f"{relative.name}.Auto.txt"
        restored.parent.mkdir(parents=True, exist_ok=True)
        replace_with_retry(generated, restored)


def compare_auto_structure(
    before_dir: str | Path,
    after_dir: str | Path,
    items: list[TranslationItem],
    approved_keys: set[str],
) -> dict[str, object]:
    """Compare Editor round-trips while masking only explicitly approved text slots."""
    before_root = Path(before_dir).resolve()
    after_root = Path(after_dir).resolve()
    by_code: dict[str, list[TranslationItem]] = {}
    for item in items:
        by_code.setdefault(item.code.upper(), []).append(item)
    approved_codes = {
        code
        for code, code_items in by_code.items()
        if any(item.key in approved_keys for item in code_items)
    }
    copy_targets: dict[str, set[str]] = {}
    for code, code_items in by_code.items():
        for item in code_items:
            match = COPY_FROM_RE.search(item.flag)
            if match is None:
                continue
            copy_targets.setdefault(match.group(1).upper(), set()).add(code)
    queue = deque(approved_codes)
    while queue:
        for target in copy_targets.get(queue.popleft(), ()):
            if target not in approved_codes:
                approved_codes.add(target)
                queue.append(target)

    def segment_chain(code: str) -> list[TranslationItem]:
        candidates = by_code.get(code, [])
        if len(candidates) != 1:
            return []
        current = candidates[0]
        parts = [current]
        seen_segments = {code}
        while True:
            match = re.search(
                r"(?:^|\r?\n)NEXT=([^\r\n]+)",
                current.flag,
                re.IGNORECASE,
            )
            if match is None:
                break
            next_code = match.group(1).upper()
            candidates = by_code.get(next_code, [])
            if len(candidates) != 1 or next_code in seen_segments:
                return []
            seen_segments.add(next_code)
            current = candidates[0]
            parts.append(current)
        return parts

    def copy_source(code: str) -> str | None:
        seen = {code}
        while True:
            candidates = by_code.get(code, [])
            if len(candidates) != 1:
                return None
            match = COPY_FROM_RE.search(candidates[0].flag)
            if match is None:
                return code
            code = match.group(1).upper()
            if code in seen:
                return None
            seen.add(code)

    segment_expected: dict[str, str] = {}
    for code in by_code:
        if code.startswith("SEGMENT_"):
            continue
        target_parts = segment_chain(code)
        if len(target_parts) <= 1:
            continue
        source_code = copy_source(code)
        parts = segment_chain(source_code) if source_code is not None else []
        if len(parts) <= 1:
            continue
        expected = "".join(
            part.translation
            if part.key in approved_keys and part.translation
            else part.original
            for part in parts
        )
        segment_expected[code] = (
            expected.replace("\r\n", "\n")
            .replace("\r", "\n")
            .replace("\n", r"<\n>")
        )
        if any(part.key in approved_keys for part in parts + target_parts):
            approved_codes.add(code)
    queue = deque(approved_codes)
    while queue:
        for target in copy_targets.get(queue.popleft(), ()):
            if target not in approved_codes:
                approved_codes.add(target)
                queue.append(target)

    def event_index(root: Path) -> dict[tuple[str, int, int], _CommandBlock]:
        result: dict[tuple[str, int, int], _CommandBlock] = {}
        sdb_path = root / "BasicData" / "SysDataBase.Auto.txt"
        map_ids = (
            _map_ids_from_databases({"SDB": _database_index(sdb_path, "SDB")[0]})
            if sdb_path.is_file()
            else {}
        )
        paths = [root / "BasicData" / "CommonEvent.dat.Auto.txt"]
        paths.extend(sorted((root / "MapData").rglob("*.mps.Auto.txt")))
        for path in paths:
            if not path.is_file():
                continue
            event_type = "common" if path.name == "CommonEvent.dat.Auto.txt" else "map"
            for block in _event_blocks(path, event_type, source=path.relative_to(root).as_posix())[0]:
                if event_type == "map":
                    aliases = map_ids.get(block.source.casefold())
                    if aliases:
                        block = replace(block, map_id=aliases[0], map_ids=aliases)
                result[(block.source, block.event_id, block.page)] = block
        return result

    differences: list[dict[str, object]] = []
    difference_count = 0

    def add(kind: str, location: str, before: object, after: object) -> None:
        nonlocal difference_count
        difference_count += 1
        if len(differences) < 200:
            differences.append({"kind": kind, "location": location, "before": before, "after": after})

    before_events = event_index(before_root)
    after_events = event_index(after_root)
    before_files = {
        path.relative_to(before_root).as_posix()
        for path in (
            [before_root / "BasicData" / "CommonEvent.dat.Auto.txt"]
            + sorted((before_root / "MapData").rglob("*.mps.Auto.txt"))
        )
        if path.is_file()
    }
    after_files = {
        path.relative_to(after_root).as_posix()
        for path in (
            [after_root / "BasicData" / "CommonEvent.dat.Auto.txt"]
            + sorted((after_root / "MapData").rglob("*.mps.Auto.txt"))
        )
        if path.is_file()
    }
    if before_files != after_files:
        add(
            "auto_file_set",
            "AutoProject",
            {
                "count": len(before_files),
                "missing_from_after": sorted(before_files - after_files)[:50],
            },
            {
                "count": len(after_files),
                "added_in_after": sorted(after_files - before_files)[:50],
            },
        )
    if set(before_events) != set(after_events):
        before_only = sorted(map(str, set(before_events) - set(after_events)))
        after_only = sorted(map(str, set(after_events) - set(before_events)))
        add(
            "event_set",
            "AutoProject",
            {
                "count": len(before_events),
                "missing_from_after_count": len(before_only),
                "missing_from_after": before_only[:50],
            },
            {
                "count": len(after_events),
                "added_in_after_count": len(after_only),
                "added_in_after": after_only[:50],
            },
        )
    for key in sorted(set(before_events) & set(after_events)):
        before = before_events[key]
        after = after_events[key]
        location = f"{before.source} event={before.event_id} page={before.page}"
        if before.event_name != after.event_name:
            if not any(
                code.upper() in approved_codes for code in _event_name_codes(before)
            ):
                add("event_name", location, before.event_name, after.event_name)
        if len(before.commands) != len(after.commands):
            add("command_count", location, len(before.commands), len(after.commands))
            continue
        for index, (left, right) in enumerate(zip(before.commands, after.commands, strict=True), start=1):
            command_location = f"{location} command={index}"
            if (left.opcode, left.ints, left.indent) != (right.opcode, right.ints, right.indent):
                add(
                    "command_structure",
                    command_location,
                    [left.opcode, list(left.ints), left.indent],
                    [right.opcode, list(right.ints), right.indent],
                )
                continue
            if len(left.strings) != len(right.strings):
                add("string_count", command_location, len(left.strings), len(right.strings))
                continue
            for string_index, (left_text, right_text) in enumerate(zip(left.strings, right.strings, strict=True)):
                if left_text == right_text:
                    continue
                codes = tuple(
                    code.upper()
                    for code in _event_codes(before, index, string_index)
                )
                expected_values = {
                    segment_expected[code]
                    for code in codes
                    if code in segment_expected
                }
                if expected_values:
                    if right_text not in expected_values:
                        add(
                            "segmented_string",
                            f"{command_location} string={string_index}",
                            sorted(expected_values),
                            right_text,
                        )
                    continue
                if not any(code in approved_codes for code in codes):
                    add("unapproved_string", f"{command_location} string={string_index}", left_text, right_text)

    database_names = (("DataBase", "UDB"), ("CDataBase", "CDB"), ("SysDataBase", "SDB"))
    for filename, database in database_names:
        left_path = before_root / "BasicData" / f"{filename}.Auto.txt"
        right_path = after_root / "BasicData" / f"{filename}.Auto.txt"
        if left_path.is_file() != right_path.is_file():
            add("database_file", filename, left_path.is_file(), right_path.is_file())
            continue
        if not left_path.is_file():
            continue
        left_types = _database_index(left_path, database)[0]
        right_types = _database_index(right_path, database)[0]
        if set(left_types) != set(right_types):
            add("database_types", database, sorted(left_types), sorted(right_types))
            continue
        for type_id in sorted(left_types):
            left_type = left_types[type_id]
            right_type = right_types[type_id]
            type_location = f"{database}[{type_id}]"
            if left_type.field_types != right_type.field_types:
                add(
                    "database_field_types",
                    type_location,
                    left_type.field_types,
                    right_type.field_types,
                )
            if len(left_type.rows) != len(right_type.rows):
                add(
                    "database_row_count",
                    type_location,
                    len(left_type.rows),
                    len(right_type.rows),
                )
            if (
                left_type.name != right_type.name
                and f"NAME-T-{database}-{type_id}".upper() not in approved_codes
            ):
                add("database_type_name", type_location, left_type.name, right_type.name)
            for field_id in sorted(set(left_type.field_names) | set(right_type.field_names)):
                left_name = left_type.field_names.get(field_id)
                right_name = right_type.field_names.get(field_id)
                if (
                    left_name != right_name
                    and f"NAME-I-{database}-{type_id}-{field_id}".upper()
                    not in approved_codes
                ):
                    add(
                        "database_field_name",
                        f"{type_location}[field={field_id}]",
                        left_name,
                        right_name,
                    )
            if len(left_type.data_names) != len(right_type.data_names):
                add(
                    "database_data_name_count",
                    type_location,
                    len(left_type.data_names),
                    len(right_type.data_names),
                )
            for data_id, (left_name, right_name) in enumerate(
                zip(left_type.data_names, right_type.data_names)
            ):
                if (
                    left_name != right_name
                    and f"NAME-D-{database}-{type_id}-{data_id}".upper()
                    not in approved_codes
                ):
                    add(
                        "database_data_name",
                        f"{type_location}[data={data_id}]",
                        left_name,
                        right_name,
                    )
            for data_id, (left_row, right_row) in enumerate(zip(left_type.rows, right_type.rows)):
                if len(left_row) != len(right_row):
                    add("database_width", f"{type_location}[{data_id}]", len(left_row), len(right_row))
                    continue
                for field_id, (left_text, right_text) in enumerate(zip(left_row, right_row)):
                    if left_text == right_text:
                        continue
                    code = f"{database}-{type_id}-{data_id}-{field_id}".upper()
                    if code not in approved_codes:
                        add("unapproved_database_string", code, left_text, right_text)

    return {
        "status": "passed" if not differences else "failed",
        "approved_keys": len(approved_keys),
        "differences": differences,
        "difference_count": difference_count,
        "before_hash": hash_directory(before_root),
        "after_hash": hash_directory(after_root),
    }


def _validate_outputs(sandbox: Path, auto_dir: Path, maps: list[Path]) -> None:
    common = auto_dir / "BasicData" / "CommonEvent.dat.Auto.txt"
    if not common.is_file():
        raise ValueError("Editor 未生成公共事件 Auto.txt。")
    for relative in maps:
        output = auto_dir / relative.parent / f"{relative.name}.Auto.txt"
        if not output.is_file():
            raise ValueError(f"Editor 未生成地图事件 Auto.txt：{relative.as_posix()}")
    required_databases = {
        "DataBase.dat": "DataBase.Auto.txt",
        "CDataBase.dat": "CDataBase.Auto.txt",
        "SysDatabase.dat": "SysDataBase.Auto.txt",
    }
    for source_name, output_name in required_databases.items():
        if (sandbox / "Data" / "BasicData" / source_name).is_file() and not (
            auto_dir / "BasicData" / output_name
        ).is_file():
            raise ValueError(f"Editor 未生成数据库 Auto.txt：{output_name}")


def export_and_analyze(
    editor_path: str | Path,
    game_root: str | Path,
    destination: str | Path,
    items: list[TranslationItem],
    *,
    cancel_event: threading.Event | None = None,
    log: Callable[[str], None] | None = None,
    diagnostic_log: Callable[[str], None] | None = None,
    warning: Callable[[str], None] | None = None,
) -> EditorExportResult:
    editor = inspect_wolf_editor(editor_path)
    game = Path(game_root).resolve()
    output = Path(destination).resolve()
    output.mkdir(parents=True, exist_ok=False)
    sandbox = Path(tempfile.mkdtemp(prefix="wolflator-editor-"))
    try:
        maps = _copy_editor_sandbox(editor.path, game, sandbox)
        if diagnostic_log:
            diagnostic_log(
                f"editor.sandbox path={sandbox} maps={len(maps)} input_hash={hash_directory(sandbox / 'Data')}"
            )
        with _editor_execution_lock(
            editor,
            cancel_event=cancel_event,
            diagnostic_log=diagnostic_log,
            warning=warning,
        ):
            result = run_process(
                [
                    str(sandbox / "Editor.exe"),
                    "-txtoutput",
                    "-txt_folder",
                    "Auto",
                    "-target",
                    "ALL",
                    "-f",
                    "Data",
                ],
                cwd=sandbox,
                timeout=1800,
                cancel_event=cancel_event,
                log=log,
                diagnostic_log=diagnostic_log,
                hide_window=True,
                slow_warning_after=300,
                slow_warning=(
                    (
                        lambda elapsed: warning(
                            "WOLF RPG Editor 全事件导出已运行 "
                            f"{elapsed / 60:.1f} 分钟，请继续等待。"
                        )
                    )
                    if warning
                    else None
                ),
            )
        if result.return_code != 0:
            raise RuntimeError(f"WOLF RPG Editor 退出码为 {result.return_code}。")
        auto_source = sandbox / "Auto"
        _restore_editor_map_paths(auto_source, maps)
        _validate_outputs(sandbox, auto_source, maps)
        auto_target = output / "editor-auto"
        shutil.copytree(auto_source, auto_target)
        input_hash = hash_directory(sandbox / "Data")
        analysis = analyze_auto_export(auto_target, items, editor, input_hash=input_hash)
        analysis_path = output / "editor-analysis.json"
        atomic_write_json(analysis_path, analysis, indent=None)
        if diagnostic_log:
            for path in sorted(auto_target.rglob("*.Auto.txt")):
                diagnostic_log(
                    f"editor.output path={path.relative_to(auto_target).as_posix()} "
                    f"bytes={path.stat().st_size} sha256={sha256_file(path)}"
                )
            for unknown in analysis["unknown_commands"]:
                diagnostic_log(
                    "editor.unknown "
                    + json.dumps(unknown, ensure_ascii=False, sort_keys=True)
                )
            diagnostic_log(
                "editor.complete "
                + json.dumps(
                    {
                        "version": editor.version,
                        "sha256": editor.sha256,
                        "duration": result.duration_seconds,
                        "files": len(list(auto_target.rglob("*.Auto.txt"))),
                        "counts": analysis["counts"],
                        "warnings": len(analysis["warnings"]),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        return EditorExportResult(
            auto_target,
            analysis_path,
            editor,
            len(analysis["warnings"]),
            list(analysis["unknown_commands"]),
        )
    except Exception:
        shutil.rmtree(output, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
