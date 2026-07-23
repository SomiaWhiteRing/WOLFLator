from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile
from collections import Counter, defaultdict
from ctypes import wintypes
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from safe_io import atomic_write_json, atomic_write_text  # noqa: E402
from wolf_command_catalog import (  # noqa: E402
    CALIBRATED_SHAPES,
    CATALOG_SCHEMA,
    COMMAND_CATALOG,
    EVIDENCE_RANK,
    EXCLUDED_COMMANDS,
    MANUAL_CALIBRATION_CASES,
    PRO_OPCODE,
    VERIFIED_EDITOR_SHA256,
    VERIFIED_EDITOR_VERSION,
    catalog_record,
    command_effect,
)
from wolf_editor import inspect_wolf_editor  # noqa: E402
from wolf_tools import sha256_file  # noqa: E402


TOOL_SCHEMA = 1
OFFICIAL_RESOURCES = {
    "full": {
        "url": "https://www.silversecond.com/WolfRPGEditor/Data/WolfRPGEditor_3.713.zip",
        "filename": "WolfRPGEditor_3.713.zip",
        "size": 32_727_538,
        "sha256": "da7ed0dee09663123ca83fa53bbbbf89e3cda01a74d8318b782fe74154a84c73",
    },
    "language": {
        "url": "https://www.silversecond.com/WolfRPGEditor/Data/Woditor_Editor.Lang_ver3.713.zip",
        "filename": "Woditor_Editor.Lang_ver3.713.zip",
        "size": 3_933_970,
        "sha256": "9910026c72e049fc2d3382dc8fb0b66e65ff510acba483c97df721cd8561984e",
    },
    "manual": {
        "url": "https://smokingwolf.github.io/tool_wolf_rpg_editor/help/old_manual_zip/WOLF_RPG_Editor_Ver3.39_Manual_HTML.zip",
        "filename": "WOLF_RPG_Editor_Ver3.39_Manual_HTML.zip",
        "size": 6_506_094,
        "sha256": "ebdb5c2c5c62bc0783b22af05621d3dc53441f7ad8f3c2fc9e1ef086dd56e79d",
    },
}
MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024
COMMAND_RE = re.compile(
    r'^\[(?P<opcode>\d+)]\[(?P<ints>\d+),(?P<strings>\d+)]<(?P<indent>\d+)>'
)
RUN_TIMEOUT = 5 * 60
MAX_ATTEMPTS = 2
EDITOR_START_TIMEOUT = 60
UI_OPERATION_TIMEOUT = 10
PASTE_EVENT_CODE_COMMAND = 32957
COPY_EVENT_CODE_COMMAND = 32963
REQUIRED_DIFFERENTIAL_EFFECTS = {
    "string_read", "string_write", "condition", "database", "event_call"
}


class CalibrationError(RuntimeError):
    pass


class _Win32EditorDriver:
    """Small, version-pinned MFC driver for the official roundtrip gate."""

    def __init__(self, editor: Path, project: Path):
        if os.name != "nt":
            raise CalibrationError("Editor UI 校准只支持 Windows。")
        self.editor = editor
        self.project = project
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self._enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.kernel.GlobalAlloc.argtypes = (ctypes.c_uint, ctypes.c_size_t)
        self.kernel.GlobalAlloc.restype = ctypes.c_void_p
        self.kernel.GlobalLock.argtypes = (ctypes.c_void_p,)
        self.kernel.GlobalLock.restype = ctypes.c_void_p
        self.kernel.GlobalUnlock.argtypes = (ctypes.c_void_p,)
        self.kernel.GlobalFree.argtypes = (ctypes.c_void_p,)
        self.kernel.GlobalFree.restype = ctypes.c_void_p
        self.user.GetClipboardData.argtypes = (ctypes.c_uint,)
        self.user.GetClipboardData.restype = ctypes.c_void_p
        self.user.SetClipboardData.argtypes = (ctypes.c_uint, ctypes.c_void_p)
        self.user.SetClipboardData.restype = ctypes.c_void_p
        self.process: subprocess.Popen[str] | None = None
        self.main_window: int | None = None

    def _text(self, window: int) -> str:
        length = self.user.GetWindowTextLengthW(window)
        buffer = ctypes.create_unicode_buffer(length + 1)
        self.user.GetWindowTextW(window, buffer, len(buffer))
        return buffer.value

    def _class(self, window: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        self.user.GetClassNameW(window, buffer, len(buffer))
        return buffer.value

    def _pid(self, window: int) -> int:
        value = ctypes.c_ulong()
        self.user.GetWindowThreadProcessId(window, ctypes.byref(value))
        return int(value.value)

    def _windows(self, *, children_of: int | None = None) -> list[int]:
        windows: list[int] = []

        @self._enum_proc
        def callback(window, _parameter):
            if children_of is not None or (
                self.process is not None and self._pid(window) == self.process.pid
            ):
                windows.append(int(window))
            return True

        if children_of is None:
            self.user.EnumWindows(callback, 0)
        else:
            self.user.EnumChildWindows(children_of, callback, 0)
        return windows

    def _wait_window(self, title: str) -> int:
        deadline = time.monotonic() + EDITOR_START_TIMEOUT
        while time.monotonic() < deadline:
            for window in self._windows():
                if self._text(window) == title:
                    return window
            if self.process is not None and self.process.poll() is not None:
                raise CalibrationError(
                    f"Editor 在等待 {title} 时退出：{self.process.returncode}"
                )
            time.sleep(0.1)
        raise CalibrationError(f"Editor {EDITOR_START_TIMEOUT} 秒内未打开：{title}")

    def _main_window(self) -> int:
        deadline = time.monotonic() + EDITOR_START_TIMEOUT
        while time.monotonic() < deadline:
            for window in self._windows():
                if "WOLF RPGエディター" in self._text(window) and self._class(window).startswith("Afx:"):
                    return window
            time.sleep(0.1)
        raise CalibrationError("Editor 60 秒内未完成工程初始化。")

    def _close_startup_windows(self) -> None:
        for window in self._windows():
            if self.user.IsWindowVisible(window) and self._text(window) in {
                "スタートガイド", "タスクリスト", "マップ選択", "仕様変更情報"
            }:
                self.user.SendMessageW(window, 0x0010, 0, 0)  # WM_CLOSE

    def _largest_list(self, parent: int) -> int:
        candidates = [
            window for window in self._windows(children_of=parent)
            if self._class(window) == "ListBox"
        ]
        if not candidates:
            raise CalibrationError("コモンイベントの命令 ListBox が見つかりません。")

        def area(window: int) -> int:
            rectangle = wintypes.RECT()
            self.user.GetWindowRect(window, ctypes.byref(rectangle))
            return (rectangle.right - rectangle.left) * (rectangle.bottom - rectangle.top)

        return max(candidates, key=area)

    def _button(self, parent: int, text: str) -> int:
        for window in self._windows(children_of=parent):
            if self._class(window) == "Button" and self._text(window) == text:
                return window
        raise CalibrationError(f"Editor 按钮不存在：{text}")

    def _set_clipboard(self, value: str) -> None:
        raw = value.encode("utf-16-le") + b"\0\0"
        handle = self.kernel.GlobalAlloc(0x0002, len(raw))  # GMEM_MOVEABLE
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        pointer = self.kernel.GlobalLock(handle)
        if not pointer:
            raise ctypes.WinError(ctypes.get_last_error())
        ctypes.memmove(pointer, raw, len(raw))
        self.kernel.GlobalUnlock(handle)
        deadline = time.monotonic() + 2
        while not self.user.OpenClipboard(0):
            if time.monotonic() >= deadline:
                raise CalibrationError("无法获得 Windows 剪贴板。")
            time.sleep(0.02)
        try:
            self.user.EmptyClipboard()
            if not self.user.SetClipboardData(13, handle):  # CF_UNICODETEXT
                raise ctypes.WinError(ctypes.get_last_error())
            handle = None
        finally:
            self.user.CloseClipboard()
            if handle:
                self.kernel.GlobalFree(handle)

    def _get_clipboard(self) -> str:
        deadline = time.monotonic() + 2
        while not self.user.OpenClipboard(0):
            if time.monotonic() >= deadline:
                raise CalibrationError("无法读取 Windows 剪贴板。")
            time.sleep(0.02)
        try:
            handle = self.user.GetClipboardData(13)
            if not handle:
                raise CalibrationError("Editor 没有写入 Unicode 事件代码。")
            pointer = self.kernel.GlobalLock(handle)
            if not pointer:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                return ctypes.wstring_at(pointer)
            finally:
                self.kernel.GlobalUnlock(handle)
        finally:
            self.user.CloseClipboard()

    def _error_dialog(self) -> str | None:
        for window in self._windows():
            if not self.user.IsWindowVisible(window) or self._text(window) not in {"Error", "エラー"}:
                continue
            messages = [
                self._text(child) for child in self._windows(children_of=window)
                if self._class(child) == "Static" and self._text(child)
            ]
            return " ".join(messages) or self._text(window)
        return None

    def start(self) -> tuple[int, int, int]:
        self.process = subprocess.Popen(
            [str(self.editor)], cwd=self.project,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        main = self._main_window()
        self.main_window = main
        common = self._wait_window("コモンイベントエディタ")
        self._close_startup_windows()
        self.user.ShowWindow(common, 5)  # SW_SHOW
        command_list = self._largest_list(common)
        return main, common, command_list

    def paste(self, cases: tuple[dict[str, object], ...]) -> int:
        _main, common, command_list = self.start()
        before = int(self.user.SendMessageW(command_list, 0x018B, 0, 0))  # LB_GETCOUNT
        self.user.SendMessageW(command_list, 0x0186, max(0, before - 1), 0)
        lines = ["WoditorEvCOMMAND_START"]
        for case in cases:
            lines.extend((f'[103][0,1]<0>()("{case["id"]}")', str(case["record"])))
        lines.append("WoditorEvCOMMAND_END")
        self._set_clipboard("\r\n".join(lines) + "\r\n")
        if not self.user.PostMessageW(common, 0x0111, PASTE_EVENT_CODE_COMMAND, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        deadline = time.monotonic() + UI_OPERATION_TIMEOUT
        expected = before + len(cases) * 2
        while time.monotonic() < deadline:
            if message := self._error_dialog():
                raise CalibrationError(f"Editor 拒绝校准事件代码：{message}")
            current = int(self.user.SendMessageW(command_list, 0x018B, 0, 0))
            if current >= expected:
                break
            time.sleep(0.1)
        else:
            raise CalibrationError("Editor 特殊粘贴 10 秒内没有增加预期命令。")
        inserted_at = current - len(cases) * 2
        ok = self._button(common, "OK")
        if not self.user.PostMessageW(ok, 0x00F5, 0, 0):  # BM_CLICK
            raise ctypes.WinError(ctypes.get_last_error())
        deadline = time.monotonic() + UI_OPERATION_TIMEOUT
        while time.monotonic() < deadline and self.user.IsWindowVisible(common):
            time.sleep(0.05)
        if self.user.IsWindowVisible(common):
            raise CalibrationError("Editor 10 秒内没有保存公共事件编辑结果。")
        self.close(require_clean=True)
        return inserted_at

    def copy_after_reopen(self, first_index: int) -> str:
        _main, common, command_list = self.start()
        count = int(self.user.SendMessageW(command_list, 0x018B, 0, 0))
        if count <= first_index:
            raise CalibrationError("重开 Editor 后校准命令消失。")
        # Special paste inserts before the caret, not necessarily at the tail.
        # Selecting the whole event and locating unique CAL markers is exact.
        if self.user.SendMessageW(command_list, 0x0185, 1, -1) == -1:  # LB_SETSEL
            raise CalibrationError("Editor 命令列表不支持全选，无法取得完整往返证据。")
        self._set_clipboard("")
        if not self.user.PostMessageW(common, 0x0111, COPY_EVENT_CODE_COMMAND, 0):
            raise ctypes.WinError(ctypes.get_last_error())
        deadline = time.monotonic() + UI_OPERATION_TIMEOUT
        while time.monotonic() < deadline:
            copied = self._get_clipboard()
            if "WoditorEvCOMMAND_START" in copied:
                self.close(require_clean=True)
                return copied
            time.sleep(0.1)
        raise CalibrationError("Editor 特殊复制 10 秒内没有返回事件代码。")

    def close(self, *, require_clean: bool) -> None:
        if self.process is None:
            return
        if self.process.poll() is None:
            if self.main_window and self.user.IsWindow(self.main_window):
                self.user.PostMessageW(self.main_window, 0x0010, 0, 0)
            try:
                self.process.wait(timeout=UI_OPERATION_TIMEOUT)
            except subprocess.TimeoutExpired:
                self._kill()
                if require_clean:
                    raise CalibrationError("Editor 无法在 10 秒内正常关闭。")
        self.process = None
        self.main_window = None

    def _kill(self) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        # ponytail: calibration owns one disposable Editor process tree. If its
        # MFC UI stops responding, taskkill is the native bounded cleanup path.
        subprocess.run(
            ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def abort(self) -> None:
        self._kill()
        self.process = None


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _qa_root() -> Path:
    return ROOT / "_qa" / "editor-calibration" / "3.713"


def _cache_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".cache")) / "WOLFLator" / "calibration-cache" / "3.713"


def _exact_editor(path: str | Path):
    info = inspect_wolf_editor(path)
    if info.version != VERIFIED_EDITOR_VERSION or info.sha256.lower() != VERIFIED_EDITOR_SHA256:
        raise CalibrationError(
            "校准只接受 Editor "
            f"{VERIFIED_EDITOR_VERSION} / {VERIFIED_EDITOR_SHA256}，实际为 "
            f"{info.version} / {info.sha256}"
        )
    return info


def _download(resource: dict[str, object], target: Path) -> None:
    expected_size = int(resource["size"])
    expected_hash = str(resource["sha256"])
    if target.is_file() and target.stat().st_size == expected_size and sha256_file(target) == expected_hash:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(f".{target.name}.{os.getpid()}.part")
    part.unlink(missing_ok=True)
    request = urllib.request.Request(str(resource["url"]), headers={"User-Agent": "WOLFLator-calibration/1"})
    digest = hashlib.sha256()
    size = 0
    try:
        with urllib.request.urlopen(request, timeout=60) as response, part.open("wb") as stream:
            final = urllib.parse.urlparse(response.geturl())
            requested = urllib.parse.urlparse(str(resource["url"]))
            if final.scheme != "https" or final.hostname != requested.hostname:
                raise CalibrationError(f"官方资源发生跨站重定向：{response.geturl()}")
            declared = int(response.headers.get("Content-Length", "0") or 0)
            if declared and declared != expected_size:
                raise CalibrationError(f"官方资源大小变化：预期 {expected_size}，实际 {declared}")
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_DOWNLOAD_BYTES:
                    raise CalibrationError("官方资源超过 128 MiB 上限。")
                stream.write(chunk)
                digest.update(chunk)
        if size != expected_size or digest.hexdigest() != expected_hash:
            raise CalibrationError(
                f"官方资源校验失败：size={size}, sha256={digest.hexdigest()}"
            )
        os.replace(part, target)
    finally:
        part.unlink(missing_ok=True)


def _safe_extract(archive: Path, destination: Path) -> None:
    marker = destination / ".wolflator-calibration-source.json"
    archive_hash = sha256_file(archive)
    if destination.is_dir() and marker.is_file():
        try:
            if json.loads(marker.read_text(encoding="utf-8")) == {
                "archive_sha256": archive_hash
            }:
                return
        except (OSError, ValueError):
            pass
    staging = destination.with_name(f".{destination.name}.{os.getpid()}.part")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        with zipfile.ZipFile(archive) as package:
            total = 0
            for member in package.infolist():
                name = member.filename.replace("\\", "/")
                path = Path(name)
                mode = (member.external_attr >> 16) & 0o170000
                if path.is_absolute() or ".." in path.parts or mode == 0o120000:
                    raise CalibrationError(f"ZIP 包含不安全路径：{name}")
                total += member.file_size
                if total > 512 * 1024 * 1024:
                    raise CalibrationError("ZIP 解压内容超过 512 MiB 上限。")
                target = staging.joinpath(*path.parts)
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with package.open(member) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output, 1024 * 1024)
        atomic_write_json(
            staging / ".wolflator-calibration-source.json",
            {"archive_sha256": archive_hash},
        )
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def _prepare_resources() -> dict[str, dict[str, object]]:
    cache = _cache_root()
    result: dict[str, dict[str, object]] = {}
    for name, resource in OFFICIAL_RESOURCES.items():
        archive = cache / str(resource["filename"])
        _download(resource, archive)
        extracted = cache / name
        _safe_extract(archive, extracted)
        result[name] = {
            "url": resource["url"],
            "archive": str(archive),
            "size": archive.stat().st_size,
            "sha256": sha256_file(archive),
            "extracted": str(extracted),
        }
    return result


def _iter_auto(roots: list[Path]):
    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        candidates = [root] if root.is_file() else sorted(root.rglob("*.Auto.txt"))
        for path in candidates:
            resolved = path.resolve()
            if resolved not in seen and resolved.is_file():
                seen.add(resolved)
                yield resolved


def _scan_auto(roots: list[Path]) -> dict[str, object]:
    shapes: Counter[tuple[int, int, int]] = Counter()
    examples: dict[tuple[int, int, int], list[dict[str, object]]] = defaultdict(list)
    files: list[dict[str, object]] = []
    for path in _iter_auto(roots):
        command_count = 0
        with path.open("r", encoding="utf-8-sig", errors="strict") as stream:
            for line_number, line in enumerate(stream, 1):
                match = COMMAND_RE.match(line)
                if not match:
                    continue
                shape = tuple(int(match.group(name)) for name in ("opcode", "ints", "strings"))
                shapes[shape] += 1
                command_count += 1
                if len(examples[shape]) < 3:
                    examples[shape].append({
                        "file": str(path), "line": line_number, "record": line.rstrip("\r\n")[:2048]
                    })
        files.append({"path": str(path), "sha256": sha256_file(path), "commands": command_count})
    observed = []
    for (opcode, ints, strings), count in sorted(shapes.items()):
        observed.append({
            "opcode": opcode,
            "int_count": ints,
            "string_count": strings,
            "count": count,
            "examples": examples[(opcode, ints, strings)],
            "catalog_effect": _effect_for_shape(opcode, ints, strings),
        })
    return {
        "files": files,
        "file_count": len(files),
        "command_count": sum(shapes.values()),
        "opcode_count": len({key[0] for key in shapes}),
        "shape_count": len(shapes),
        "shapes": observed,
    }


def _effect_for_shape(opcode: int, ints: int, strings: int) -> str | None:
    item = COMMAND_CATALOG.get(opcode)
    if item and (ints, strings) in CALIBRATED_SHAPES.get(opcode, ()):
        return item[1]
    return None


def _catalog_report(scan: dict[str, object]) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    by_opcode: dict[int, list[dict[str, object]]] = defaultdict(list)
    for item in scan["shapes"]:
        by_opcode[int(item["opcode"])].append(item)
    commands: list[dict[str, object]] = []
    unresolved: list[dict[str, object]] = []
    for opcode in sorted(COMMAND_CATALOG):
        record = catalog_record(opcode)
        assert record is not None
        record["observed"] = by_opcode.get(opcode, [])
        needs_differential = record["effect"] in REQUIRED_DIFFERENTIAL_EFFECTS
        enough_evidence = EVIDENCE_RANK.get(str(record["evidence"]), -1) >= (
            EVIDENCE_RANK["differential"] if needs_differential else EVIDENCE_RANK["roundtrip"]
        )
        uncovered = [
            item for item in record["observed"]
            if command_effect(opcode, int(item["int_count"]), int(item["string_count"])) is None
        ]
        record["status"] = (
            "verified"
            if record["shapes"] and enough_evidence and not uncovered
            else "manual_required"
        )
        commands.append(record)
        if record["status"] != "verified":
            if uncovered:
                for item in uncovered:
                    unresolved.append({
                        "opcode": opcode,
                        "name": record["name"],
                        "reason": "语料出现未经校准的参数形状",
                        "int_count": item["int_count"],
                        "string_count": item["string_count"],
                    })
            else:
                reason = "缺少官方差分证据" if record["shapes"] else "缺少官方往返参数形状"
                unresolved.append({"opcode": opcode, "name": record["name"], "reason": reason})
    for opcode, items in sorted(by_opcode.items()):
        if opcode not in COMMAND_CATALOG and opcode != PRO_OPCODE:
            unresolved.append({"opcode": opcode, "name": "unknown", "reason": "语料出现目录外 opcode", "shapes": items})
    return commands, unresolved


def _resource_strings(module_path: Path) -> list[dict[str, object]]:
    if os.name != "nt" or not module_path.is_file():
        return []
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel.LoadLibraryExW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_uint]
    kernel.LoadLibraryExW.restype = ctypes.c_void_p
    kernel.FreeLibrary.argtypes = [ctypes.c_void_p]
    user.LoadStringW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_int]
    user.LoadStringW.restype = ctypes.c_int
    handle = kernel.LoadLibraryExW(str(module_path), None, 0x22)
    if not handle:
        return []
    strings: list[dict[str, object]] = []
    try:
        buffer = ctypes.create_unicode_buffer(8192)
        # MFC uses the standard 16-string resource blocks; scanning IDs is faster
        # and less fragile than parsing PE resources ourselves.
        for string_id in range(1, 65536):
            length = user.LoadStringW(handle, string_id, buffer, len(buffer))
            if length:
                strings.append({"id": string_id, "text": buffer.value})
    finally:
        kernel.FreeLibrary(handle)
    return strings


def _inventory(args: argparse.Namespace) -> int:
    editor = _exact_editor(args.editor)
    resources = _prepare_resources()
    extracted_full = Path(resources["full"]["extracted"])
    packaged_editor = next(extracted_full.rglob("Editor.exe"), None)
    if packaged_editor is None or not (packaged_editor.parent / "Data" / "BasicData").is_dir():
        raise CalibrationError("官方完整包中找不到唯一的 Editor.exe + Data/BasicData 工程。")
    full_root = packaged_editor.parent
    sample_auto = _cache_root() / "sample-auto"
    roots = [sample_auto, *(Path(value) for value in args.corpus)]
    scan = _scan_auto(roots)
    commands, unresolved = _catalog_report(scan)
    language_dll = next(Path(resources["language"]["extracted"]).rglob("Editor.Lang.dll"), None)
    resource_strings = _resource_strings(language_dll) if language_dll else []
    report = {
        "schema": TOOL_SCHEMA,
        "catalog_schema": CATALOG_SCHEMA,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "editor": {"path": str(editor.path), "version": editor.version, "sha256": editor.sha256},
        "official_resources": resources,
        "full_package_root": str(full_root),
        "corpus": scan,
        "commands": commands,
        "excluded": EXCLUDED_COMMANDS,
        "unresolved": unresolved,
        "resource_strings_sha256": _sha256_bytes(_canonical_json(resource_strings)),
        "resource_string_count": len(resource_strings),
    }
    output = Path(args.output) if args.output else _qa_root() / "inventory.json"
    atomic_write_json(output, report)
    print(f"inventory: {output}")
    print(
        f"opcodes={scan['opcode_count']} shapes={scan['shape_count']} "
        f"commands={scan['command_count']} unresolved={len(unresolved)}"
    )
    return 0


def _run_editor_export(editor: Path, project: Path, output_name: str) -> tuple[Path, str]:
    output = project / output_name
    if output.exists():
        shutil.rmtree(output)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    result = subprocess.run(
        [str(editor), "-txtoutput", "-txt_folder", output_name, "-target", "ALL", "-f", "Data"],
        cwd=project,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=RUN_TIMEOUT,
        check=False,
        creationflags=creationflags,
    )
    if result.returncode:
        raise CalibrationError(f"Editor Auto 导出失败：exit={result.returncode}\n{result.stdout[-4000:]}")
    if not (output / "BasicData" / "CommonEvent.dat.Auto.txt").is_file():
        raise CalibrationError("Editor 返回成功但未生成 CommonEvent.dat.Auto.txt。")
    return output, result.stdout


def _directory_hash(path: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*"), key=lambda value: value.as_posix()):
        if item.is_file():
            digest.update(item.relative_to(path).as_posix().encode("utf-8", "surrogatepass"))
            digest.update(b"\0")
            digest.update(bytes.fromhex(sha256_file(item)))
    return digest.hexdigest()


_CASE_COMMENT_RE = re.compile(r'^\[103]\[0,1]<\d+>\(\)\("(?P<id>CAL-[^"]+)"\)$')


def _case_records(event_code: str) -> dict[str, str]:
    lines = [line.strip() for line in event_code.splitlines()]
    result: dict[str, str] = {}
    for index, line in enumerate(lines):
        match = _CASE_COMMENT_RE.match(line)
        if not match:
            continue
        following = next(
            (candidate for candidate in lines[index + 1:] if candidate.startswith("[")),
            None,
        )
        if following is None:
            raise CalibrationError(f"案例 {match.group('id')} 后没有机器记录。")
        result[match.group("id")] = following
    return result


def _shape(record: str) -> tuple[int, int, int]:
    match = COMMAND_RE.match(record)
    if not match:
        raise CalibrationError(f"非法 Editor 机器记录：{record[:200]}")
    return tuple(int(match.group(name)) for name in ("opcode", "ints", "strings"))


def _validate_manual_cases(copied: str) -> list[dict[str, object]]:
    official = _case_records(copied)
    evidence: list[dict[str, object]] = []
    for case in MANUAL_CALIBRATION_CASES:
        case_id = str(case["id"])
        record = official.get(case_id)
        if record is None:
            raise CalibrationError(f"Editor 重开后缺少案例：{case_id}")
        opcode, int_count, string_count = _shape(record)
        if opcode != int(case["opcode"]):
            raise CalibrationError(
                f"案例 {case_id} opcode 被 Editor 改为 {opcode}。"
            )
        evidence.append({
            "id": case_id,
            "opcode": opcode,
            "shape": [int_count, string_count],
            "input": str(case["record"]),
            "official": record,
            "input_sha256": _sha256_bytes(str(case["record"]).encode("utf-8")),
            "official_sha256": _sha256_bytes(record.encode("utf-8")),
            "level": "differential" if case.get("differential") else "roundtrip",
        })
    csv_a = official.get("CAL-251-CSV-A", "")
    csv_b = official.get("CAL-251-CSV-B", "")
    if not csv_a or csv_a.replace("CAL-251-A.csv", "CAL-251-B.csv") != csv_b:
        raise CalibrationError("opcode 251 文件名差分未被 Editor 原样保留。")
    event_a = official.get("CAL-211-RESERVE-A", "")
    event_b = official.get("CAL-211-RESERVE-B", "")
    if not event_a or event_a.replace("(0,0)", "(1,0)") != event_b:
        raise CalibrationError("opcode 211 事件 ID 差分未被 Editor 原样保留。")
    return evidence


def _validate_auto_cases(auto_root: Path, expected: list[dict[str, object]]) -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8-sig", errors="strict")
        for path in _iter_auto([auto_root])
    )
    records = _case_records(combined)
    for case in expected:
        if records.get(str(case["id"])) != case["official"]:
            raise CalibrationError(
                f"Auto 导出未保留官方案例：{case['id']}"
            )


_GENERATED_REGION_RE = re.compile(
    r"(?s)(# BEGIN WOLFLATOR EDITOR CALIBRATION\n).*?"
    r"(# END WOLFLATOR EDITOR CALIBRATION)"
)


def _render_promoted_catalog(source: str, cases: list[dict[str, object]]) -> str:
    shapes: dict[int, set[tuple[int, int]]] = defaultdict(set)
    evidence: dict[int, str] = {}
    for case in cases:
        opcode = int(case["opcode"])
        shape = tuple(int(value) for value in case["shape"])
        shapes[opcode].add(shape)
        level = str(case["level"])
        if EVIDENCE_RANK[level] > EVIDENCE_RANK.get(evidence.get(opcode, "manual"), 0):
            evidence[opcode] = level
    shape_literal = repr({key: tuple(sorted(value)) for key, value in sorted(shapes.items())})
    evidence_literal = repr(dict(sorted(evidence.items())))
    replacement = (
        "\\1# Generated only from official Editor save/reopen/copy and Auto evidence.\n"
        f"GENERATED_MANUAL_SHAPES: dict[int, tuple[tuple[int, int], ...]] = {shape_literal}\n"
        f"GENERATED_MANUAL_EVIDENCE: dict[int, str] = {evidence_literal}\n"
        "\\2"
    )
    updated, count = _GENERATED_REGION_RE.subn(replacement, source, count=1)
    if count != 1:
        raise CalibrationError("生产命令表缺少唯一的校准生成区域。")
    return updated


def _promote_manual_cases(cases: list[dict[str, object]]) -> None:
    target = ROOT / "wolf_command_catalog.py"
    source = target.read_text(encoding="utf-8")
    updated = _render_promoted_catalog(source, cases)
    atomic_write_text(target, updated)


def _calibrate(args: argparse.Namespace) -> int:
    editor = _exact_editor(args.editor)
    inventory_path = Path(args.inventory) if args.inventory else _qa_root() / "inventory.json"
    if not inventory_path.is_file():
        raise CalibrationError("缺少 inventory.json，请先执行 inventory。")
    inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
    source_root = Path(inventory["full_package_root"])
    if not (source_root / "Data" / "BasicData").is_dir():
        raise CalibrationError("官方完整包缺少最小工程 Data/BasicData。")
    case_hash = _sha256_bytes(_canonical_json({
        "editor": editor.sha256,
        "catalog": {
            opcode: {"name": value[0], "effect": value[1]}
            for opcode, value in sorted(COMMAND_CATALOG.items())
        },
        "cases": MANUAL_CALIBRATION_CASES,
        "source": _directory_hash(source_root / "Data"),
    }))
    run_id = args.run_id or case_hash[:12]
    run_root = _qa_root() / run_id
    checkpoint = run_root / "checkpoint.json"
    if args.resume and checkpoint.is_file():
        state = json.loads(checkpoint.read_text(encoding="utf-8"))
        if state.get("case_hash") != case_hash or state.get("editor_sha256") != editor.sha256:
            raise CalibrationError("断点与当前 Editor、工程或案例目录不一致。")
    else:
        state = {
            "schema": TOOL_SCHEMA,
            "case_hash": case_hash,
            "editor_sha256": editor.sha256,
            "attempts": 0,
            "roundtrips": [],
            "cases": [],
            "ui_attempts": 0,
            "manual_required": list(inventory.get("unresolved", [])),
        }
    run_root.mkdir(parents=True, exist_ok=True)
    project = Path(tempfile.mkdtemp(prefix="wolflator-calibration-"))
    driver: _Win32EditorDriver | None = None
    try:
        shutil.copy2(editor.path, project / "Editor.exe")
        saved_project = run_root / "project"
        project_source = saved_project if (saved_project / "Data").is_dir() else source_root
        shutil.copytree(project_source / "Data", project / "Data")
        if not state.get("cases"):
            last_error = ""
            for ui_attempt in range(int(state.get("ui_attempts", 0)), MAX_ATTEMPTS):
                state["ui_attempts"] = ui_attempt + 1
                atomic_write_json(checkpoint, state)
                driver = _Win32EditorDriver(project / "Editor.exe", project)
                try:
                    first_index = driver.paste(MANUAL_CALIBRATION_CASES)
                    copied = driver.copy_after_reopen(first_index)
                    atomic_write_text(run_root / "official-copy.txt", copied)
                    cases = _validate_manual_cases(copied)
                    state["cases"] = cases
                    resolved = {int(case["opcode"]) for case in cases}
                    state["manual_required"] = [
                        item for item in state["manual_required"]
                        if int(item.get("opcode", -1)) not in resolved
                    ]
                    if saved_project.exists():
                        shutil.rmtree(saved_project)
                    shutil.copytree(project, saved_project, ignore=shutil.ignore_patterns("Editor.exe"))
                    atomic_write_json(checkpoint, state)
                    break
                except (CalibrationError, OSError) as error:
                    last_error = f"{type(error).__name__}: {error}"
                    state["last_ui_error"] = last_error
                    atomic_write_json(checkpoint, state)
                    if driver:
                        driver.abort()
                    if ui_attempt + 1 < MAX_ATTEMPTS:
                        if project.exists():
                            shutil.rmtree(project)
                        project.mkdir()
                        shutil.copy2(editor.path, project / "Editor.exe")
                        shutil.copytree(source_root / "Data", project / "Data")
            else:
                print(
                    f"manual_required={len(state['manual_required'])}; "
                    f"reason={last_error}; checkpoint={checkpoint}"
                )
                return 2
        if state.get("manual_required"):
            print(f"manual_required={len(state['manual_required'])}; checkpoint={checkpoint}")
            return 2
        while len(state["roundtrips"]) < MAX_ATTEMPTS:
            attempt = len(state["roundtrips"]) + 1
            output, console = _run_editor_export(project / "Editor.exe", project, f"Auto-{attempt}")
            _validate_auto_cases(output, list(state.get("cases", [])))
            evidence = run_root / f"roundtrip-{attempt}"
            if evidence.exists():
                shutil.rmtree(evidence)
            shutil.copytree(output, evidence)
            atomic_write_text(run_root / f"roundtrip-{attempt}.log", console)
            state["roundtrips"].append({
                "attempt": attempt,
                "hash": _directory_hash(evidence),
                "path": str(evidence),
            })
            state["attempts"] = attempt
            atomic_write_json(checkpoint, state)
        hashes = {item["hash"] for item in state["roundtrips"]}
        if len(hashes) != 1:
            raise CalibrationError("同一官方工程的两次 Auto 导出不一致。")
        _promote_manual_cases(list(state.get("cases", [])))
        state["production_table_sha256"] = sha256_file(ROOT / "wolf_command_catalog.py")
        atomic_write_json(checkpoint, state)
        print(f"calibrate: verified {len(COMMAND_CATALOG)} free commands at {run_root}")
        return 0
    finally:
        if driver:
            driver.abort()
        shutil.rmtree(project, ignore_errors=True)


def _verify(args: argparse.Namespace) -> int:
    editor = _exact_editor(args.editor)
    inventory_path = Path(args.inventory) if args.inventory else _qa_root() / "inventory.json"
    checkpoint = Path(args.checkpoint) if args.checkpoint else None
    if checkpoint is None:
        candidates = sorted(_qa_root().glob("*/checkpoint.json"), key=lambda path: path.stat().st_mtime)
        checkpoint = candidates[-1] if candidates else None
    errors: list[str] = []
    state: dict[str, object] = {}
    if checkpoint is not None and checkpoint.is_file():
        state = json.loads(checkpoint.read_text(encoding="utf-8"))
    if not inventory_path.is_file():
        errors.append(f"缺少目录：{inventory_path}")
        inventory: dict[str, object] = {}
    else:
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        if inventory.get("schema") != TOOL_SCHEMA or inventory.get("catalog_schema") != CATALOG_SCHEMA:
            errors.append("目录 schema 不匹配")
        if inventory.get("editor", {}).get("sha256") != editor.sha256:
            errors.append("目录使用的 Editor 哈希不匹配")
    classifications = Counter(item[1] for item in COMMAND_CATALOG.values())
    valid_effects = {"no_write", "numeric_write", "string_read", "string_write", "condition", "control_flow", "database", "event_call", "opaque"}
    invalid = sorted(effect for effect in classifications if effect not in valid_effects)
    if invalid:
        errors.append(f"无效副作用分类：{invalid}")
    if PRO_OPCODE in COMMAND_CATALOG:
        errors.append("Pro opcode 1000 不得进入免费版目录")
    for opcode, (_, effect, evidence) in sorted(COMMAND_CATALOG.items()):
        if not CALIBRATED_SHAPES.get(opcode):
            errors.append(f"opcode {opcode} 缺少参数形状")
        required = effect in REQUIRED_DIFFERENTIAL_EFFECTS or effect in {"condition", "database", "event_call"}
        if required and EVIDENCE_RANK.get(evidence, -1) < EVIDENCE_RANK["differential"]:
            errors.append(f"opcode {opcode} 字符串相关证据不足：{evidence}")
    resolved_opcodes = {int(case["opcode"]) for case in state.get("cases", [])}
    remaining_inventory = [
        item for item in inventory.get("unresolved", [])
        if int(item.get("opcode", -1)) not in resolved_opcodes
    ]
    if remaining_inventory:
        errors.append(f"目录仍有 {len(remaining_inventory)} 个未解决命令")
    uncovered_shapes = [
        item for item in inventory.get("corpus", {}).get("shapes", [])
        if int(item.get("opcode", -1)) != PRO_OPCODE
        and command_effect(
            int(item.get("opcode", -1)),
            int(item.get("int_count", -1)),
            int(item.get("string_count", -1)),
        ) is None
    ]
    if uncovered_shapes:
        errors.append(f"语料仍有 {len(uncovered_shapes)} 个未经校准的参数形状")
    if checkpoint is None or not checkpoint.is_file():
        errors.append("缺少双次往返 checkpoint")
    else:
        hashes = {item.get("hash") for item in state.get("roundtrips", [])}
        if len(state.get("roundtrips", [])) != 2 or len(hashes) != 1:
            errors.append("官方 Auto 双次往返证据不一致")
        if state.get("manual_required"):
            errors.append(f"仍有 {len(state['manual_required'])} 个 manual_required")
        expected_ids = {str(case["id"]) for case in MANUAL_CALIBRATION_CASES}
        actual_ids = {str(case["id"]) for case in state.get("cases", [])}
        if actual_ids != expected_ids:
            errors.append(
                f"校准案例集合不完整：缺少 {sorted(expected_ids - actual_ids)}，"
                f"多出 {sorted(actual_ids - expected_ids)}"
            )
        if state.get("editor_sha256") != editor.sha256:
            errors.append("checkpoint 的 Editor 哈希不匹配")
        if state.get("production_table_sha256") != sha256_file(ROOT / "wolf_command_catalog.py"):
            errors.append("生产命令表与校准完成时的哈希不一致")
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print(f"verify: {len(COMMAND_CATALOG)} free commands, 0 unresolved, editor={editor.version}")
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="WOLF RPG Editor 3.713 free-command calibration")
    subparsers = parser.add_subparsers(dest="command", required=True)
    inventory = subparsers.add_parser("inventory", help="download official evidence and scan Auto corpora")
    inventory.add_argument("--editor", required=True)
    inventory.add_argument("--corpus", action="append", default=[])
    inventory.add_argument("--output")
    inventory.set_defaults(handler=_inventory)
    calibrate = subparsers.add_parser("calibrate", help="perform bounded official roundtrip calibration")
    calibrate.add_argument("--editor", required=True)
    calibrate.add_argument("--inventory")
    calibrate.add_argument("--run-id")
    calibrate.add_argument("--resume", action="store_true")
    calibrate.set_defaults(handler=_calibrate)
    verify = subparsers.add_parser("verify", help="verify catalog and evidence before production promotion")
    verify.add_argument("--editor", required=True)
    verify.add_argument("--inventory")
    verify.add_argument("--checkpoint")
    verify.set_defaults(handler=_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except (CalibrationError, OSError, ValueError, zipfile.BadZipFile, json.JSONDecodeError, subprocess.TimeoutExpired) as error:
        print(f"ERROR: {type(error).__name__}: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
