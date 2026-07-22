from __future__ import annotations

import errno
import hashlib
import json
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Callable, Iterable

from openpyxl import load_workbook

from models import ImportCategory, ImportScope, ToolResult, TranslationItem


CODE_HEADER = "Code (No Change)"
FLAG_HEADER = "Flag (No Change)"
TYPE_HEADER = "Type"
INFO_HEADER = "Info"
ORIGINAL_HEADER = "Original text (No Change)"
TARGET_PREFIX = "Translated text 1 /"
EXPECTED_TARGET = "Chinese (Simplified)"
SUPPORT_DIR = "WOLF_Translation_Support_Tool_Data"
WORKBOOK_NAME = "WOLF_Translation_Text.xlsx"
GAME_CONFIG_NAME = "WOLF_Translation_Game_Config.ini"
ITEMS_SCHEMA = 1
PUA_START = 0xE100
PUA_END = 0xF7FF
SPECIAL_ESCAPES = set("!.|^<>${}\\")
COPY_FROM_RE = re.compile(r"(?:^|\r?\n)COPY-FROM-([^\r\n]+)", re.IGNORECASE)
SIMPLIFIED_CHINESE_FONTS = {
    "BASICDATA-3": "KaiTi",
    "BASICDATA-4": "Microsoft YaHei",
    "BASICDATA-5": "Microsoft YaHei",
    "BASICDATA-6": "Microsoft YaHei",
}


def full_export_scope() -> ImportScope:
    return ImportScope(display=True, external=True, optional_name=True, halfwidth=True, filename=True)


def name_baseline_scope() -> ImportScope:
    return ImportScope(display=True, external=True, optional_name=False, halfwidth=True, filename=True)


class CancelledError(RuntimeError):
    pass


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def verified_vendor_file(filename: str, component: str, hash_field: str = "sha256") -> Path:
    manifest_path = resource_path("vendor/manifest.json")
    target = resource_path(f"vendor/{filename}")
    if not manifest_path.is_file() or not target.is_file():
        raise FileNotFoundError(f"发行资源缺少 {filename} 或 vendor/manifest.json。")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = str(manifest.get(component, {}).get(hash_field, "")).lower()
    actual = sha256_file(target).lower()
    if not expected or actual != expected:
        raise ValueError(f"{filename} SHA-256 不匹配: {actual}")
    return target


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_directory(path: str | Path) -> str:
    root = Path(path).resolve()
    digest = hashlib.sha256()
    for item in sorted((p for p in root.rglob("*") if p.is_file()), key=lambda p: p.as_posix()):
        relative = item.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8", "surrogatepass"))
        digest.update(b"\0")
        digest.update(str(item.stat().st_size).encode("ascii"))
        digest.update(b"\0")
        with item.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _kill_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    else:
        process.kill()


def _emit_log(sink: Callable[[str], None] | None, message: str) -> None:
    if not sink:
        return
    try:
        sink(message)
    except UnicodeEncodeError:
        # ponytail: Preserve process execution on narrow consoles; UTF-8 file sinks keep the original text.
        try:
            sink(message.encode("ascii", errors="backslashreplace").decode("ascii"))
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, errno.EPIPE}:
                raise
    except OSError as exc:
        # A detached CLI console must not turn a completed external process into a failed pipeline stage.
        if exc.errno not in {errno.EINVAL, errno.EPIPE}:
            raise


def _process_startupinfo(hide_window: bool):
    if not hide_window or os.name != "nt":
        return None
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return startupinfo


def _pe_import_name_offset(path: str | Path, library: str, function: str) -> int:
    data = Path(path).read_bytes()

    def unpack(fmt: str, offset: int):
        size = struct.calcsize(fmt)
        if offset < 0 or offset + size > len(data):
            raise ValueError("PE 结构越界。")
        return struct.unpack_from(fmt, data, offset)

    def text_at(offset: int) -> str:
        end = data.find(b"\0", offset)
        if offset < 0 or end < 0:
            raise ValueError("PE 字符串越界。")
        return data[offset:end].decode("ascii")

    pe_offset = unpack("<I", 0x3C)[0]
    if data[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise ValueError("不是有效的 PE 文件。")
    machine, section_count, _, _, _, optional_size, _ = unpack("<HHIIIHH", pe_offset + 4)
    optional = pe_offset + 24
    magic = unpack("<H", optional)[0]
    if magic == 0x10B:
        pointer_size = 4
        directories = optional + 96
    elif magic == 0x20B:
        pointer_size = 8
        directories = optional + 112
    else:
        raise ValueError(f"不支持的 PE 可选头：0x{magic:04x}")
    import_rva = unpack("<I", directories + 8)[0]
    sections = []
    section_table = optional + optional_size
    for index in range(section_count):
        offset = section_table + index * 40
        virtual_size, virtual_address, raw_size, raw_offset = unpack("<IIII", offset + 8)
        sections.append((virtual_address, max(virtual_size, raw_size), raw_offset))

    def file_offset(rva: int) -> int:
        for virtual_address, size, raw_offset in sections:
            if virtual_address <= rva < virtual_address + size:
                return raw_offset + rva - virtual_address
        if 0 <= rva < len(data):
            return rva
        raise ValueError(f"PE RVA 无法映射：0x{rva:x}")

    descriptor = file_offset(import_rva)
    for descriptor_index in range(4096):
        original_thunk, _, _, name_rva, first_thunk = unpack(
            "<IIIII", descriptor + descriptor_index * 20
        )
        if not any((original_thunk, name_rva, first_thunk)):
            break
        if text_at(file_offset(name_rva)).casefold() != library.casefold():
            continue
        thunk_rva = original_thunk or first_thunk
        thunk_offset = file_offset(thunk_rva)
        thunk_format = "<I" if pointer_size == 4 else "<Q"
        ordinal_mask = 0x80000000 if pointer_size == 4 else 0x8000000000000000
        for thunk_index in range(65536):
            value = unpack(thunk_format, thunk_offset + thunk_index * pointer_size)[0]
            if value == 0:
                break
            if value & ordinal_mask:
                continue
            if text_at(file_offset(value) + 2) == function:
                return file_offset(value) + 2
        break
    raise ValueError(f"PE 未导入 {library}!{function}。")


def _silent_official_executable(path: str | Path) -> bytes:
    data = bytearray(Path(path).read_bytes())
    offset = _pe_import_name_offset(path, "USER32.dll", "MessageBeep")
    original = b"MessageBeep\0"
    if data[offset : offset + len(original)] != original:
        raise ValueError("官方工具 MessageBeep 导入结构不匹配。")
    data[offset : offset + len(original)] = b"IsWindow\0".ljust(len(original), b"\0")
    return bytes(data)


CONSOLE_CAPTURE_ARG = "--console-capture-worker"
CONSOLE_CLOSE_PROMPT = "Press any key to close this window."


def _console_capture_command(process_id: int, snapshot_path: Path) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, CONSOLE_CAPTURE_ARG, str(process_id), str(snapshot_path)]
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        CONSOLE_CAPTURE_ARG,
        str(process_id),
        str(snapshot_path),
    ]


def _console_delta(previous: str, current: str) -> list[str]:
    old_lines = previous.splitlines()
    new_lines = current.splitlines()
    common = 0
    while common < min(len(old_lines), len(new_lines)) and old_lines[common] == new_lines[common]:
        common += 1
    return [line for line in new_lines[common:] if line]


def _write_console_snapshot(path: Path, *, text: str = "", done: bool = False, error: str = "") -> None:
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps({"text": text, "done": done, "error": error}, ensure_ascii=False),
        encoding="utf-8",
    )
    deadline = time.monotonic() + 1.0
    while True:
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def console_capture_worker(process_id: int, snapshot_path: str | Path) -> int:
    path = Path(snapshot_path)
    if os.name != "nt":
        _write_console_snapshot(path, done=True, error="控制台捕获仅支持 Windows。")
        return 1
    try:
        return _console_capture_worker_windows(process_id, path)
    except Exception as exc:
        _write_console_snapshot(path, done=True, error=f"{type(exc).__name__}: {exc}")
        return 1


def _console_capture_worker_windows(process_id: int, snapshot_path: Path) -> int:
    import ctypes
    from ctypes import wintypes

    class Coord(ctypes.Structure):
        _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

    class SmallRect(ctypes.Structure):
        _fields_ = [
            ("Left", wintypes.SHORT),
            ("Top", wintypes.SHORT),
            ("Right", wintypes.SHORT),
            ("Bottom", wintypes.SHORT),
        ]

    class ConsoleInfo(ctypes.Structure):
        _fields_ = [
            ("dwSize", Coord),
            ("dwCursorPosition", Coord),
            ("wAttributes", wintypes.WORD),
            ("srWindow", SmallRect),
            ("dwMaximumWindowSize", Coord),
        ]

    class CharUnion(ctypes.Union):
        _fields_ = [("UnicodeChar", wintypes.WCHAR), ("AsciiChar", wintypes.CHAR)]

    class KeyEvent(ctypes.Structure):
        _anonymous_ = ("character",)
        _fields_ = [
            ("bKeyDown", wintypes.BOOL),
            ("wRepeatCount", wintypes.WORD),
            ("wVirtualKeyCode", wintypes.WORD),
            ("wVirtualScanCode", wintypes.WORD),
            ("character", CharUnion),
            ("dwControlKeyState", wintypes.DWORD),
        ]

    class EventUnion(ctypes.Union):
        _fields_ = [("KeyEvent", KeyEvent), ("padding", ctypes.c_byte * 16)]

    class InputRecord(ctypes.Structure):
        _anonymous_ = ("event",)
        _fields_ = [("EventType", wintypes.WORD), ("event", EventUnion)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.AttachConsole.argtypes = [wintypes.DWORD]
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.ReadConsoleOutputCharacterW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        Coord,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetConsoleScreenBufferInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ConsoleInfo),
    ]
    kernel32.WriteConsoleInputW.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(InputRecord),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]

    kernel32.FreeConsole()
    attached = False
    for _ in range(100):
        if kernel32.AttachConsole(process_id):
            attached = True
            break
        time.sleep(0.1)
    if not attached:
        raise ctypes.WinError(ctypes.get_last_error())

    access = 0x80000000 | 0x40000000
    sharing = 0x00000001 | 0x00000002
    output = kernel32.CreateFileW("CONOUT$", access, sharing, None, 3, 0, None)
    input_handle = kernel32.CreateFileW("CONIN$", access, sharing, None, 3, 0, None)
    invalid_handle = ctypes.c_void_p(-1).value
    if output == invalid_handle or input_handle == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())

    process_handle = kernel32.OpenProcess(0x00100000, False, process_id)
    if not process_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    last_text = ""
    try:
        while True:
            info = ConsoleInfo()
            if not kernel32.GetConsoleScreenBufferInfo(output, ctypes.byref(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            width = max(1, info.dwSize.X)
            height = max(1, info.dwCursorPosition.Y + 1)
            size = width * height
            chars = ctypes.create_unicode_buffer(size + 1)
            count = wintypes.DWORD()
            if not kernel32.ReadConsoleOutputCharacterW(
                output, chars, size, Coord(0, 0), ctypes.byref(count)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            raw = chars[: count.value]
            lines = [raw[index : index + width].rstrip() for index in range(0, len(raw), width)]
            while lines and not lines[-1]:
                lines.pop()
            text = "\n".join(lines)
            if text != last_text:
                _write_console_snapshot(snapshot_path, text=text)
                last_text = text
            if re.sub(r"\s+", "", CONSOLE_CLOSE_PROMPT) in re.sub(r"\s+", "", text):
                records = (InputRecord * 2)()
                for index, key_down in enumerate((True, False)):
                    records[index].EventType = 0x0001
                    records[index].KeyEvent.bKeyDown = key_down
                    records[index].KeyEvent.wRepeatCount = 1
                    records[index].KeyEvent.wVirtualKeyCode = 0x0D
                    records[index].KeyEvent.wVirtualScanCode = 0x1C
                    records[index].KeyEvent.UnicodeChar = "\r"
                written = wintypes.DWORD()
                if not kernel32.WriteConsoleInputW(
                    input_handle, records, len(records), ctypes.byref(written)
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
                _write_console_snapshot(snapshot_path, text=text, done=True)
                return 0
            if kernel32.WaitForSingleObject(process_handle, 0) == 0:
                _write_console_snapshot(snapshot_path, text=text, done=True)
                return 0
            time.sleep(0.2)
    finally:
        for handle in (process_handle, input_handle, output):
            kernel32.CloseHandle(handle)


def run_process(
    command: list[str],
    *,
    cwd: str | Path | None = None,
    timeout: int = 3600,
    cancel_event: threading.Event | None = None,
    log: Callable[[str], None] | None = None,
    diagnostic_log: Callable[[str], None] | None = None,
    env: dict[str, str] | None = None,
    hide_window: bool = False,
    capture_console: bool = False,
) -> ToolResult:
    if capture_console and os.name != "nt":
        raise ValueError("控制台捕获仅支持 Windows。")
    detail = diagnostic_log or log
    safe_command = " ".join(f'"{arg}"' if " " in arg else arg for arg in command)
    _emit_log(log, f"> {safe_command}")
    started = time.monotonic()
    startupinfo = _process_startupinfo(hide_window or capture_console)
    creationflags = (
        getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
        if capture_console
        else getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    process = subprocess.Popen(
        command,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        creationflags=creationflags,
        startupinfo=startupinfo,
        bufsize=1,
    )
    console_snapshot: Path | None = None
    console_helper: subprocess.Popen[str] | None = None
    console_text = ""
    console_revision = 0
    console_done = False
    if capture_console:
        descriptor, snapshot_name = tempfile.mkstemp(prefix="wolflator-console-", suffix=".json")
        os.close(descriptor)
        console_snapshot = Path(snapshot_name)
        console_snapshot.unlink()
        try:
            console_helper = subprocess.Popen(
                _console_capture_command(process.pid, console_snapshot),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            _kill_process_tree(process)
            process.wait()
            console_snapshot.unlink(missing_ok=True)
            raise
    _emit_log(
        detail,
        f"process.start pid={process.pid} cwd={Path(cwd).resolve() if cwd else Path.cwd()} "
        f"timeout={timeout}s window={'hidden-console' if capture_console else ('hidden' if startupinfo else 'default')} "
        f"command={safe_command}",
    )
    if console_helper:
        _emit_log(detail, f"console.capture.start pid={process.pid} helper_pid={console_helper.pid}")
    output_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()
    captured: dict[str, list[str]] = {"stdout": [], "stderr": []}

    def read_console_snapshot() -> None:
        nonlocal console_text, console_revision, console_done
        if console_snapshot is None or not console_snapshot.is_file():
            return
        try:
            revision = console_snapshot.stat().st_mtime_ns
            if revision == console_revision:
                return
            payload = json.loads(console_snapshot.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        console_revision = revision
        error = str(payload.get("error", ""))
        if error:
            raise RuntimeError(f"官方工具控制台捕获失败：{error}")
        current = str(payload.get("text", ""))
        for line in _console_delta(console_text, current):
            _emit_log(detail, f"process.console pid={process.pid} {line}")
        console_text = current
        console_done = bool(payload.get("done", False))

    def read_stream(name: str, stream) -> None:
        try:
            for line in stream:
                output_queue.put((name, line.rstrip("\r\n")))
        finally:
            output_queue.put((name, None))

    readers = [
        threading.Thread(target=read_stream, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=read_stream, args=("stderr", process.stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()
    finished_streams: set[str] = set()
    try:
        while process.poll() is None or len(finished_streams) < len(readers):
            read_console_snapshot()
            if console_helper and console_helper.poll() is not None:
                read_console_snapshot()
                if not console_done and process.poll() is None:
                    raise RuntimeError(
                        f"官方工具控制台捕获进程异常退出：{console_helper.returncode}"
                    )
            if cancel_event and cancel_event.is_set():
                _emit_log(
                    detail,
                    f"process.cancel pid={process.pid} elapsed={time.monotonic() - started:.3f}s",
                )
                _kill_process_tree(process)
                raise CancelledError("任务已取消。")
            elapsed = time.monotonic() - started
            if elapsed > timeout:
                _emit_log(
                    detail,
                    f"process.timeout pid={process.pid} elapsed={elapsed:.3f}s limit={timeout}s",
                )
                _kill_process_tree(process)
                raise TimeoutError(f"外部工具运行超过 {timeout} 秒。")
            try:
                name, line = output_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                finished_streams.add(name)
                continue
            captured[name].append(line)
            _emit_log(detail, f"process.{name} pid={process.pid} {line}")
    finally:
        if process.poll() is None:
            _kill_process_tree(process)
        process.wait()
        for reader in readers:
            reader.join(timeout=1)
        for stream in (process.stdout, process.stderr):
            if stream and not stream.closed:
                stream.close()
        if console_helper:
            try:
                console_helper.wait(timeout=2)
            except subprocess.TimeoutExpired:
                console_helper.terminate()
                console_helper.wait(timeout=2)
        try:
            read_console_snapshot()
        finally:
            if console_snapshot:
                console_snapshot.unlink(missing_ok=True)
                for temporary in console_snapshot.parent.glob(f"{console_snapshot.name}.*.tmp"):
                    temporary.unlink(missing_ok=True)
    stdout = "\n".join(captured["stdout"])
    stderr = "\n".join(captured["stderr"])
    if console_helper and console_helper.returncode not in (None, 0):
        raise RuntimeError(f"官方工具控制台捕获进程异常退出：{console_helper.returncode}")
    result = ToolResult(command, process.returncode or 0, stdout, stderr, time.monotonic() - started)
    _emit_log(
        detail,
        f"process.exit pid={process.pid} code={result.return_code} duration={result.duration_seconds:.3f}s "
        f"stdout_lines={len(captured['stdout'])} stderr_lines={len(captured['stderr'])} "
        f"console_lines={len(console_text.splitlines())}",
    )
    if result.return_code != 0:
        error_detail = "\n".join(part for part in (console_text, stderr, stdout) if part).strip()[-2000:]
        raise RuntimeError(f"外部工具退出码 {result.return_code}: {error_detail}")
    _emit_log(log, f"外部工具完成，耗时 {result.duration_seconds:.1f} 秒。")
    return result


def prepare_uberwolf(ascii_dir: str | Path) -> Path:
    target_dir = Path(ascii_dir)
    if not str(target_dir).isascii():
        raise ValueError("UberWolf 执行目录必须是纯 ASCII 路径。")
    target_dir.mkdir(parents=True, exist_ok=True)
    override = os.environ.get("WOLFLATOR_UBERWOLF", "")
    source = Path(override) if override else verified_vendor_file("UberWolfCli.exe", "uberwolf")
    if not source.is_file():
        raise FileNotFoundError("未找到 UberWolfCli.exe。开发环境请运行 scripts/fetch_vendor.ps1。")
    target = target_dir / "UberWolfCli.exe"
    if not target.exists() or sha256_file(target) != sha256_file(source):
        shutil.copy2(source, target)
    return target


class UberWolfRunner:
    def __init__(self, executable: str | Path):
        self.executable = Path(executable)

    def unpack(
        self,
        game_root: str | Path,
        *,
        cancel_event: threading.Event | None = None,
        log: Callable[[str], None] | None = None,
        diagnostic_log: Callable[[str], None] | None = None,
    ) -> ToolResult | None:
        root = Path(game_root)
        if (root / "Data" / "BasicData" / "Game.dat").is_file() and not next(root.rglob("*.wolf"), None):
            if log:
                log("检测到完整松散 Data，跳过 UberWolf。")
            return None
        game_exe = root / "Game.exe"
        if not game_exe.is_file():
            raise FileNotFoundError("工作副本中没有 Game.exe。")
        result = run_process(
            [str(self.executable), str(game_exe)],
            cwd=self.executable.parent,
            cancel_event=cancel_event,
            log=log,
            diagnostic_log=diagnostic_log,
        )
        if not (root / "Data" / "BasicData" / "Game.dat").is_file():
            raise RuntimeError("UberWolf 返回成功，但没有生成 Data/BasicData/Game.dat。")
        return result


def _official_config_text(scope: ImportScope) -> str:
    include_external = "1" if scope.external else "0"
    include_names = "1" if scope.optional_name else "0"
    values = {
        "LastBackupFile": "",
        "LastDiffFile": "",
        "LastMakeTranslatedDir": "",
        "LastTargetLang": "1",
        **{f"NotTranslatedFlag{i}": "0" for i in range(1, 11)},
        "Original_Language": "1",
        "Tool_A_Get_CSV": include_external,
        "Tool_A_Get_CommonEvent_Name": include_names,
        "Tool_A_Get_DB_DataName": include_names,
        "Tool_A_Get_DB_ItemName": include_names,
        "Tool_A_Get_DB_TypeName": include_names,
        "Tool_A_Get_MapEvent_Name": include_names,
        "Tool_A_Get_TXT": include_external,
        "Tool_A_Include_CDB_Name": include_names,
        "Tool_A_Include_SDB_Name": include_names,
        "Tool_A_Include_UDB_Name": include_names,
        "Tool_A_Sort": "1",
        "Translated_Language_1": "4",
        **{f"Translated_Language_{i}": "0" for i in range(2, 11)},
    }
    return "[System]\r\n" + "".join(f"{key}={value}\r\n" for key, value in values.items())


def write_official_game_config(game_root: str | Path, scope: ImportScope) -> Path:
    support = Path(game_root) / SUPPORT_DIR
    support.mkdir(parents=True, exist_ok=True)
    path = support / GAME_CONFIG_NAME
    path.write_bytes(b"\xff\xfe" + _official_config_text(scope).encode("utf-16le"))
    return path


def prepare_official_tool(source_exe: str | Path, cache_root: str | Path) -> Path:
    source = Path(source_exe)
    lib = source.parent / "LibXL.dll"
    if not source.is_file() or not lib.is_file():
        raise FileNotFoundError("官方工具 EXE 或同目录 LibXL.dll 不存在。")
    fingerprint = sha256_file(source)[:16]
    target_dir = Path(cache_root) / fingerprint
    target_dir.mkdir(parents=True, exist_ok=True)
    target_exe = target_dir / source.name
    silent_executable = _silent_official_executable(source)
    if not target_exe.is_file() or target_exe.read_bytes() != silent_executable:
        temporary = target_exe.with_name(target_exe.name + ".tmp")
        temporary.write_bytes(silent_executable)
        shutil.copystat(source, temporary)
        os.replace(temporary, target_exe)
    target_lib = target_dir / lib.name
    if not target_lib.exists() or sha256_file(lib) != sha256_file(target_lib):
        shutil.copy2(lib, target_lib)
    return target_exe


class OfficialToolRunner:
    def __init__(self, executable: str | Path, scope: ImportScope):
        self.executable = Path(executable)
        self.scope = scope

    def run(
        self,
        mode: str,
        game_root: str | Path,
        *,
        language_index: int | None = None,
        cancel_event: threading.Event | None = None,
        log: Callable[[str], None] | None = None,
        diagnostic_log: Callable[[str], None] | None = None,
    ) -> ToolResult:
        root = Path(game_root).resolve()
        write_official_game_config(root, self.scope)
        _emit_log(
            diagnostic_log,
            "official.sound_suppression method=import-redirection source=MessageBeep target=IsWindow",
        )
        command = [
            str(self.executable),
            "-mode",
            mode,
        ]
        if language_index is not None:
            command.append(str(language_index))
        command.extend(["-gamedata", str(root) + os.sep, "-mes_lang", "EN"])
        command.append("-wait")
        return run_process(
            command,
            cwd=self.executable.parent,
            cancel_event=cancel_event,
            log=log,
            diagnostic_log=diagnostic_log,
            hide_window=True,
            capture_console=True,
        )

    def extract(self, game_root: str | Path, **kwargs) -> Path:
        existing = Path(game_root) / SUPPORT_DIR / WORKBOOK_NAME
        if existing.is_file():
            backup = existing.with_suffix(".pre-extract.bak")
            backup.unlink(missing_ok=True)
            os.replace(existing, backup)
        self.run("EXTRACT", game_root, **kwargs)
        return locate_workbook(game_root)

    def update_excel(self, game_root: str | Path, **kwargs) -> Path:
        self.run("UPDATE_EXCEL", game_root, **kwargs)
        return locate_workbook(game_root)

    def translate(self, game_root: str | Path, **kwargs) -> Path:
        root = Path(game_root)
        for path in root.glob("Translated*_Chinese (Simplified)"):
            if path.is_dir():
                shutil.rmtree(path)
        self.run("CREATE_FOLDER", game_root, language_index=0, **kwargs)
        self.run("TRANSLATE", game_root, language_index=0, **kwargs)
        return locate_translated_game(game_root)


def _header_map(worksheet) -> tuple[int, dict[str, int]]:
    for row_index, row in enumerate(worksheet.iter_rows(min_row=1, max_row=20, values_only=True), start=1):
        if CODE_HEADER in row and ORIGINAL_HEADER in row:
            mapping = {str(value): index + 1 for index, value in enumerate(row) if value is not None}
            missing = {CODE_HEADER, FLAG_HEADER, TYPE_HEADER, INFO_HEADER, ORIGINAL_HEADER} - mapping.keys()
            if missing:
                raise ValueError(f"官方工作簿缺少列: {', '.join(sorted(missing))}")
            targets = [name for name in mapping if name.startswith(TARGET_PREFIX)]
            if not targets or EXPECTED_TARGET not in targets[0]:
                raise ValueError("官方工作簿第一译文列不是简体中文。")
            mapping["__target__"] = mapping[targets[0]]
            return row_index, mapping
    raise ValueError("不是受支持的 WOLF Translation Support Tool 工作簿。")


def locate_workbook(game_root: str | Path) -> Path:
    support = Path(game_root) / SUPPORT_DIR
    workbook_path = support / WORKBOOK_NAME
    if not workbook_path.is_file():
        raise FileNotFoundError(f"官方工具没有生成 {workbook_path}。")
    workbook = load_workbook(workbook_path, read_only=False, data_only=False)
    try:
        _header_map(workbook.active)
    finally:
        workbook.close()
    return workbook_path


def _content_category(code: str, flag: str, type_name: str) -> ImportCategory:
    upper_flag = flag.upper()
    upper_code = code.upper()
    upper_type = type_name.upper()
    if "<FILENAME>" in upper_flag:
        return ImportCategory.FILENAME
    if "<HALF-WIDTH CHARACTERS ONLY>" in upper_flag:
        return ImportCategory.HALFWIDTH
    if upper_code.startswith("NAME-") or upper_code.endswith("-NAME"):
        return ImportCategory.OPTIONAL_NAME
    if upper_code.startswith(("TXT-", "CSV-")) or "TXT" in upper_type or "CSV" in upper_type:
        return ImportCategory.EXTERNAL
    return ImportCategory.DISPLAY


def _category(code: str, flag: str, type_name: str) -> ImportCategory:
    if "COPY-FROM-" in flag.upper():
        return ImportCategory.COPY
    return _content_category(code, flag, type_name)


def stable_key(code: str, flag: str, original: str, ordinal: int) -> str:
    payload = "\0".join((code, flag, original, str(ordinal))).encode("utf-8", "surrogatepass")
    return hashlib.sha256(payload).hexdigest()


def _iter_data_rows(worksheet) -> Iterable[tuple[int, dict[str, str], int]]:
    header_row, headers = _header_map(worksheet)
    counts: Counter[tuple[str, str, str]] = Counter()
    for row_index, row in enumerate(
        worksheet.iter_rows(min_row=header_row + 1, values_only=True),
        start=header_row + 1,
    ):
        def value(column: str):
            index = headers[column] - 1
            return row[index] if index < len(row) else None

        original = value(ORIGINAL_HEADER)
        if original is None:
            continue
        values = {
            "code": str(value(CODE_HEADER) or ""),
            "flag": str(value(FLAG_HEADER) or ""),
            "type": str(value(TYPE_HEADER) or ""),
            "info": str(value(INFO_HEADER) or ""),
            "original": str(original),
            "translation": str(value("__target__") or ""),
        }
        identity = (values["code"], values["flag"], values["original"])
        counts[identity] += 1
        yield row_index, values, counts[identity]


def _scan_control_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    index = 0
    while index < len(text):
        if text[index] != "\\":
            index += 1
            continue
        start = index
        index += 1
        if index >= len(text):
            tokens.append("\\")
            break
        char = text[index]
        if char in SPECIAL_ESCAPES:
            index += 1
        elif char.isascii() and (char.isalnum() or char == "_"):
            index += 1
            while index < len(text) and text[index].isascii() and (text[index].isalnum() or text[index] == "_"):
                index += 1
            while index < len(text) and text[index] == "[":
                depth = 0
                while index < len(text):
                    if text[index] == "[":
                        depth += 1
                    elif text[index] == "]":
                        depth -= 1
                        if depth == 0:
                            index += 1
                            break
                    elif text[index] in "\r\n":
                        break
                    index += 1
        else:
            # ponytail: Unknown backslash forms protect only the slash; upgrade the scanner if WOLF documents more syntax.
            index = start + 1
        tokens.append(text[start:index])
    return tokens


def protect_control_tokens(text: str) -> tuple[str, list[str]]:
    tokens = _scan_control_tokens(text)
    if not tokens:
        return text, []
    cursor = 0
    output: list[str] = []
    for offset, token in enumerate(tokens):
        codepoint = PUA_START + offset
        if codepoint > PUA_END:
            raise ValueError("单条文本的控制符数量超过占位符容量。")
        start = text.find(token, cursor)
        output.append(text[cursor:start])
        output.append(chr(codepoint))
        cursor = start + len(token)
    output.append(text[cursor:])
    return "".join(output), tokens


def restore_control_tokens(text: str, tokens: list[str]) -> str:
    expected = [chr(PUA_START + index) for index in range(len(tokens))]
    actual = [char for char in text if PUA_START <= ord(char) <= PUA_END]
    if actual != expected:
        raise ValueError(
            "控制符占位序列不一致: "
            f"expected={[f'U+{ord(c):04X}' for c in expected]}, "
            f"actual={[f'U+{ord(c):04X}' for c in actual]}"
        )
    restored = text
    for placeholder, token in zip(expected, tokens):
        restored = restored.replace(placeholder, token, 1)
    if _scan_control_tokens(restored) != tokens:
        raise ValueError("译文控制符序列与原文不一致。")
    return restored


def read_translation_items(workbook_path: str | Path) -> list[TranslationItem]:
    # Normal mode releases the underlying ZIP deterministically on Windows;
    # read-only iterators can retain the workbook handle until garbage collection.
    workbook = load_workbook(workbook_path, read_only=False, data_only=False)
    worksheet = workbook.active
    items: list[TranslationItem] = []
    for _row, values, ordinal in _iter_data_rows(worksheet):
        signature = _scan_control_tokens(values["original"])
        category = _category(values["code"], values["flag"], values["type"])
        copy_category = (
            _content_category(values["code"], values["flag"], values["type"])
            if category is ImportCategory.COPY
            else None
        )
        items.append(
            TranslationItem(
                key=stable_key(values["code"], values["flag"], values["original"], ordinal),
                original=values["original"],
                translation=values["translation"],
                context=" | ".join(
                    part for part in (values["type"], values["info"], f"Code={values['code']}", values["flag"])
                    if part
                ),
                stage=1 if values["translation"] else 0,
                code=values["code"],
                flag=values["flag"],
                type=values["type"],
                info=values["info"],
                category=category,
                copy_category=copy_category,
                control_signature=signature,
            )
        )
    workbook.close()
    return items


def is_managed_translation(item: TranslationItem) -> bool:
    return item.code.upper() in SIMPLIFIED_CHINESE_FONTS


def apply_managed_translations(items: list[TranslationItem]) -> None:
    for item in items:
        translation = SIMPLIFIED_CHINESE_FONTS.get(item.code.upper())
        if translation:
            item.translation = translation
            item.stage = 1


def _location_identities(items: list[TranslationItem]) -> list[tuple[str, str, int]]:
    counts: Counter[tuple[str, str]] = Counter()
    identities: list[tuple[str, str, int]] = []
    for item in items:
        identity = (item.code, item.original)
        counts[identity] += 1
        identities.append((item.code, item.original, counts[identity]))
    return identities


def classify_optional_name_delta(
    full_items: list[TranslationItem],
    baseline_items: list[TranslationItem],
) -> int:
    full_identities = _location_identities(full_items)
    full_set = set(full_identities)
    baseline_set = set(_location_identities(baseline_items))
    missing_from_full = baseline_set - full_set
    if missing_from_full:
        raise ValueError(f"基准导出包含全量导出中不存在的行，共 {len(missing_from_full)} 条。")

    optional_count = 0
    for item, identity in zip(full_items, full_identities):
        if identity not in baseline_set:
            if item.category is ImportCategory.COPY:
                item.copy_category = ImportCategory.OPTIONAL_NAME
            else:
                item.category = ImportCategory.OPTIONAL_NAME
            optional_count += 1
        elif item.category is ImportCategory.OPTIONAL_NAME:
            item.category = ImportCategory.DISPLAY
        elif item.category is ImportCategory.COPY and item.copy_category is ImportCategory.OPTIONAL_NAME:
            item.copy_category = ImportCategory.DISPLAY
    return optional_count


def _copy_source(item: TranslationItem, by_code: dict[str, list[TranslationItem]]) -> TranslationItem:
    current = item
    visited: set[str] = set()
    while current.category is ImportCategory.COPY:
        match = COPY_FROM_RE.search(current.flag)
        if not match:
            raise ValueError(f"COPY-FROM 行缺少来源代码: {current.code}")
        source_code = match.group(1)
        marker = f"{current.code}\0{source_code}\0{current.original}"
        if marker in visited:
            raise ValueError(f"COPY-FROM 出现循环引用: {current.code}")
        visited.add(marker)
        candidates = by_code.get(source_code, [])
        exact = [candidate for candidate in candidates if candidate.original == current.original]
        if len(exact) == 1:
            current = exact[0]
        else:
            raise ValueError(f"COPY-FROM 找不到唯一来源: {current.code} -> {source_code}")
    return current


def selected_translation_requirements(
    items: list[TranslationItem],
    scope: ImportScope,
) -> dict[str, set[ImportCategory]]:
    by_code: dict[str, list[TranslationItem]] = {}
    for item in items:
        by_code.setdefault(item.code, []).append(item)

    groups: dict[str, set[ImportCategory]] = {}
    sources: dict[str, TranslationItem] = {}
    for item in items:
        category = item.copy_category if item.category is ImportCategory.COPY else item.category
        if category is None:
            continue
        source = _copy_source(item, by_code) if item.category is ImportCategory.COPY else item
        sources[source.key] = source
        categories = groups.setdefault(source.key, set())
        categories.add(category)
        intrinsic = _content_category(source.code, source.flag, source.type)
        if intrinsic in {ImportCategory.FILENAME, ImportCategory.HALFWIDTH}:
            categories.add(intrinsic)

    requirements: dict[str, set[ImportCategory]] = {}
    for key, categories in groups.items():
        if is_managed_translation(sources[key]):
            requirements[key] = categories
            continue
        # ponytail: COPY-FROM cannot give duplicate uses different translations. Keep the
        # whole group untouched until dangerous filename/half-width uses are explicitly enabled.
        dangerous = {ImportCategory.FILENAME, ImportCategory.HALFWIDTH} & categories
        if any(not scope.allows(category) for category in dangerous):
            continue
        if any(scope.allows(category) for category in categories):
            requirements[key] = categories
    return requirements


def selected_translation_items(
    items: list[TranslationItem],
    scope: ImportScope,
) -> list[TranslationItem]:
    required = selected_translation_requirements(items, scope)
    return [item for item in items if item.key in required and not is_managed_translation(item)]


def to_paratranz(
    items: list[TranslationItem],
    scope: ImportScope,
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for item in selected_translation_items(items, scope):
        protected, tokens = protect_control_tokens(item.original)
        if tokens != item.control_signature:
            raise ValueError(f"控制符签名发生变化: {item.code}")
        translation = ""
        if item.translation:
            protected_translation, translated_tokens = protect_control_tokens(item.translation)
            if translated_tokens == tokens:
                translation = protected_translation
        output.append(
            {
                "key": item.key,
                "original": protected,
                "translation": translation,
                "context": item.context,
                "stage": 1 if translation else 0,
            }
        )
    return output


def _index_ainiee_rows(
    translated: list[dict[str, object]],
) -> dict[str, dict[str, object]]:
    actual: dict[str, dict[str, object]] = {}
    for row in translated:
        key = str(row.get("key", ""))
        if not key or key in actual:
            raise ValueError(f"AiNiee 输出包含空键或重复键: {key!r}")
        actual[key] = row
    return actual


def _validated_ainiee_translation(
    item: TranslationItem,
    row: dict[str, object],
) -> str:
    if row.get("wolflator_excluded") is True:
        protected, tokens = protect_control_tokens(item.original)
        if str(row.get("translation", "")) != protected or tokens != item.control_signature:
            raise ValueError(f"AiNiee 排除项不能安全原样回填: {item.code}")
        return item.original
    raw = str(row.get("translation", ""))
    if not raw.strip():
        raise ValueError(f"AiNiee 没有生成译文: {item.code} / {item.original[:80]}")
    try:
        return restore_control_tokens(raw, item.control_signature)
    except ValueError as exc:
        raise ValueError(f"AiNiee 译文控制符校验失败: {item.code}: {exc}") from exc


def retryable_translation_errors(
    items: list[TranslationItem],
    translated: list[dict[str, object]],
    scope: ImportScope,
) -> dict[str, str]:
    expected = {item.key: item for item in selected_translation_items(items, scope)}
    actual = _index_ainiee_rows(translated)
    extra = set(actual) - set(expected)
    if extra:
        raise ValueError(f"AiNiee 输出包含不属于当前输入的键: extra={len(extra)}")
    errors: dict[str, str] = {}
    for key, item in expected.items():
        if key not in actual:
            errors[key] = f"AiNiee 缺少输出: {item.code}"
            continue
        try:
            _validated_ainiee_translation(item, actual[key])
        except ValueError as exc:
            errors[key] = str(exc)
    return errors


def merge_ainiee_output(
    items: list[TranslationItem],
    translated: list[dict[str, object]],
    scope: ImportScope,
) -> list[TranslationItem]:
    expected = {item.key: item for item in selected_translation_items(items, scope)}
    actual = _index_ainiee_rows(translated)
    missing = set(expected) - set(actual)
    extra = set(actual) - set(expected)
    if missing or extra:
        raise ValueError(f"AiNiee 输出键集合不一致: missing={len(missing)}, extra={len(extra)}")
    for key, item in expected.items():
        item.translation = _validated_ainiee_translation(item, actual[key])
        item.stage = 1
    usage_categories = selected_translation_requirements(items, full_export_scope())
    unsafe_categories = {ImportCategory.FILENAME, ImportCategory.HALFWIDTH}
    for item in items:
        if item.category is ImportCategory.COPY:
            item.translation = ""
        elif item.translation and not (usage_categories.get(item.key, set()) & unsafe_categories):
            item.translation = item.translation.replace("・", "·")
    return items


def reconcile_incremental(
    previous: list[TranslationItem],
    current: list[TranslationItem],
) -> tuple[list[TranslationItem], list[dict[str, object]]]:
    previous_by_key = {item.key: item.translation for item in previous if item.translation}
    previous_by_original: dict[str, set[str]] = {}
    for item in previous:
        if item.translation and item.category is not ImportCategory.COPY:
            previous_by_original.setdefault(item.original, set()).add(item.translation)
    conflicts: list[dict[str, object]] = []
    for item in current:
        if item.category is ImportCategory.COPY:
            item.translation = ""
            continue
        exact = previous_by_key.get(item.key)
        if exact:
            item.translation = exact
            item.stage = 1
            continue
        candidates = sorted(previous_by_original.get(item.original, set()))
        if len(candidates) == 1:
            item.translation = candidates[0]
            item.stage = 1
        elif len(candidates) > 1:
            # ponytail: Ambiguous moved duplicates are retranslated instead of guessing; a future review UI may map candidates.
            item.translation = ""
            item.stage = 0
            conflicts.append(
                {
                    "key": item.key,
                    "code": item.code,
                    "original": item.original,
                    "candidates": candidates,
                }
            )
    return current, conflicts


def _save_workbook_atomic(workbook, output_path: str | Path) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    workbook.save(temporary)
    os.replace(temporary, output)
    return output


def write_full_workbook(
    template_path: str | Path,
    output_path: str | Path,
    items: list[TranslationItem],
) -> Path:
    translations = {item.key: item.translation for item in items}
    workbook = load_workbook(template_path)
    worksheet = workbook.active
    _header_row, headers = _header_map(worksheet)
    for row_index, values, ordinal in _iter_data_rows(worksheet):
        key = stable_key(values["code"], values["flag"], values["original"], ordinal)
        category = _category(values["code"], values["flag"], values["type"])
        worksheet.cell(row_index, headers["__target__"]).value = "" if category is ImportCategory.COPY else translations.get(key, "")
    return _save_workbook_atomic(workbook, output_path)


def _filename_target_exists(game_root: Path, translated_name: str) -> bool:
    name = translated_name.strip().replace("\\", "/").lstrip("/")
    if not name or ".." in Path(name).parts:
        return False
    data_root = game_root / "Data"
    direct = data_root.joinpath(*name.split("/"))
    return direct.is_file()


def write_scoped_workbook(
    full_path: str | Path,
    output_path: str | Path,
    scope: ImportScope,
    game_root: str | Path,
    items: list[TranslationItem],
) -> Path:
    workbook = load_workbook(full_path)
    worksheet = workbook.active
    _header_row, headers = _header_map(worksheet)
    requirements = selected_translation_requirements(items, scope)
    missing_filenames: list[str] = []
    for row_index, values, ordinal in _iter_data_rows(worksheet):
        key = stable_key(values["code"], values["flag"], values["original"], ordinal)
        category = _category(values["code"], values["flag"], values["type"])
        cell = worksheet.cell(row_index, headers["__target__"])
        required_categories = requirements.get(key, set())
        keep = bool(required_categories)
        if category is ImportCategory.COPY or not keep:
            cell.value = ""
            continue
        if ImportCategory.FILENAME in required_categories and cell.value:
            if not _filename_target_exists(Path(game_root), str(cell.value)):
                missing_filenames.append(str(cell.value))
    if missing_filenames:
        sample = ", ".join(missing_filenames[:5])
        raise ValueError(f"文件名译文没有对应真实文件，共 {len(missing_filenames)} 项，例如: {sample}")
    return _save_workbook_atomic(workbook, output_path)


def locate_translated_game(game_root: str | Path) -> Path:
    candidates = []
    for path in Path(game_root).glob("Translated*_Chinese (Simplified)"):
        if (path / "Game.exe").is_file() and (path / "Data").is_dir():
            candidates.append(path)
    if len(candidates) != 1:
        raise FileNotFoundError(
            f"官方工具返回成功，但简体中文 Translated 目录数量为 {len(candidates)}。"
        )
    return candidates[0]


def dump_items(path: str | Path, items: list[TranslationItem]) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(
        json.dumps(
            {"schema": ITEMS_SCHEMA, "items": [item.to_dict() for item in items]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    os.replace(temporary, output)
    return output


def load_items(path: str | Path) -> list[TranslationItem]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"schema", "items"}:
        raise ValueError("翻译条目文件结构不匹配。")
    if data["schema"] != ITEMS_SCHEMA:
        raise ValueError(f"不支持的翻译条目 schema: {data['schema']}")
    if not isinstance(data["items"], list):
        raise ValueError("翻译条目 items 不是数组。")
    return [TranslationItem.from_dict(item) for item in data["items"]]


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == CONSOLE_CAPTURE_ARG:
        raise SystemExit(console_capture_worker(int(sys.argv[2]), sys.argv[3]))
    raise SystemExit(2)
