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
import zipfile
from pathlib import Path
from typing import Callable, Iterable

from models import ToolResult
from safe_io import atomic_write_json, read_text_with_retry, replace_with_retry

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
OFFICIAL_MISALIGNED_MESSAGE = "The command line seems to be misaligned."


class OfficialToolDialogError(RuntimeError):
    def __init__(self, dialogs: list[str]):
        self.dialogs = tuple(dialogs)
        super().__init__("官方工具弹出错误对话框：" + "；".join(dialogs))


def official_dialogs_indicate_legacy_game(dialogs: Iterable[str]) -> bool:
    return any(
        "editor.exe version used to create the game data seems to be old"
        in " ".join(dialog.casefold().split())
        for dialog in dialogs
    )


class OfficialArtifactMissingError(FileNotFoundError):
    def __init__(self, artifact: str | Path, diagnostics: list[str]):
        self.artifact = Path(artifact)
        self.diagnostics = tuple(diagnostics)
        detail = "；".join(diagnostics[:5])
        super().__init__(f"官方工具没有生成 {self.artifact}；控制台诊断：{detail}")


class ToolProcessError(RuntimeError):
    def __init__(
        self,
        command: list[str],
        return_code: int,
        *,
        stdout: str = "",
        stderr: str = "",
        console_output: str = "",
    ):
        self.command = tuple(command)
        self.return_code = return_code
        self.stdout = stdout
        self.stderr = stderr
        self.console_output = console_output
        detail = "\n".join(
            part for part in (console_output, stderr, stdout) if part
        ).strip()[-2000:]
        super().__init__(f"外部工具退出码 {return_code}: {detail}")


def _dismiss_process_dialogs(process_id: int) -> list[str]:
    if os.name != "nt":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    dialogs: list[str] = []

    def window_text(window) -> str:
        length = user32.GetWindowTextLengthW(window)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(window, buffer, length + 1)
        return buffer.value.strip()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit_window(window, _parameter):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window, ctypes.byref(owner))
        if owner.value != process_id:
            return True
        class_name = ctypes.create_unicode_buffer(64)
        user32.GetClassNameW(window, class_name, len(class_name))
        if class_name.value != "#32770":
            return True
        title = window_text(window)
        body: list[str] = []
        buttons: list[int] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def visit_child(child, _child_parameter):
            child_class = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(child, child_class, len(child_class))
            text = window_text(child)
            if child_class.value == "Button":
                buttons.append(child)
            elif text:
                body.append(text)
            return True

        user32.EnumChildWindows(window, visit_child, 0)
        message = " | ".join(part for part in (title, *body) if part)
        dialogs.append(message or "未命名错误对话框")
        if buttons:
            user32.PostMessageW(buttons[0], 0x00F5, 0, 0)  # BM_CLICK
        return True

    user32.EnumWindows(visit_window, 0)
    return dialogs


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


def parse_official_diagnostics(text: str) -> list[dict[str, str]]:
    compact = re.sub(r"\s+", "", text)
    markers = list(re.finditer(r"\[Error!\](?P<code>[A-Za-z0-9_-]+)=>", compact))
    diagnostics = []
    compact_misaligned = re.sub(r"\s+", "", OFFICIAL_MISALIGNED_MESSAGE)
    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(compact)
        body = compact[marker.end() : end]
        source, separator, detail = body.partition("=>")
        if compact_misaligned in detail:
            message = OFFICIAL_MISALIGNED_MESSAGE
        else:
            message = detail[:500] if separator else "官方工具报告了无法完整解析的错误。"
        diagnostics.append(
            {
                "code": marker.group("code"),
                "source": source[:500],
                "message": message,
            }
        )
    return diagnostics


def parse_official_map_failures(text: str) -> list[str]:
    compact = re.sub(r"\s+", "", text)
    failures = []
    for match in re.finditer(
        r"Map(?P<map>\d+):(?P<path>Data[\\/]+MapData[\\/]+.{1,500}?)"
        r"が読み込めませんでした(?P<detail>.{0,500}?)=>Failed",
        compact,
    ):
        detail = match.group("detail")
        if "破損" not in detail and "アクセス権限" not in detail:
            continue
        failures.append(
            f"Map {match.group('map')} {match.group('path')}: 読み込み失敗 ({detail[:160]})"
        )
    return failures


def _write_console_snapshot(path: Path, *, text: str = "", done: bool = False, error: str = "") -> None:
    atomic_write_json(path, {"text": text, "done": done, "error": error}, indent=None)


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
    invalid_handle = ctypes.c_void_p(-1).value
    if output == invalid_handle:
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
            if kernel32.WaitForSingleObject(process_handle, 0) == 0:
                _write_console_snapshot(snapshot_path, text=text, done=True)
                return 0
            time.sleep(0.05)
    finally:
        for handle in (process_handle, output):
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
    slow_warning_after: float | None = None,
    slow_warning: Callable[[float], None] | None = None,
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
    console_history: list[str] = []
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
            console_history.append(line)
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
    slow_warning_sent = False
    seen_dialogs: set[str] = set()
    try:
        while process.poll() is None or len(finished_streams) < len(readers):
            read_console_snapshot()
            if capture_console and process.poll() is None:
                for dialog in _dismiss_process_dialogs(process.pid):
                    if dialog not in seen_dialogs:
                        seen_dialogs.add(dialog)
                        _emit_log(detail, f"process.dialog pid={process.pid} text={dialog}")
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
            if (
                not slow_warning_sent
                and slow_warning is not None
                and slow_warning_after is not None
                and elapsed >= slow_warning_after
                and process.poll() is None
            ):
                slow_warning_sent = True
                slow_warning(elapsed)
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
                for pattern in (
                    f"{console_snapshot.name}.*.tmp",
                    f".{console_snapshot.name}.*.tmp",
                ):
                    for temporary in console_snapshot.parent.glob(pattern):
                        temporary.unlink(missing_ok=True)
    stdout = "\n".join(captured["stdout"])
    stderr = "\n".join(captured["stderr"])
    if console_helper and console_helper.returncode not in (None, 0):
        raise RuntimeError(f"官方工具控制台捕获进程异常退出：{console_helper.returncode}")
    result = ToolResult(
        command,
        process.returncode or 0,
        stdout,
        stderr,
        time.monotonic() - started,
        console_text,
        console_history,
    )
    if console_text:
        _emit_log(
            detail,
            f"process.console.final pid={process.pid} text="
            + json.dumps(console_text, ensure_ascii=False),
        )
    _emit_log(
        detail,
        f"process.exit pid={process.pid} code={result.return_code} duration={result.duration_seconds:.3f}s "
        f"stdout_lines={len(captured['stdout'])} stderr_lines={len(captured['stderr'])} "
        f"console_lines={len(console_text.splitlines())}",
    )
    if result.return_code != 0:
        raise ToolProcessError(
            command,
            result.return_code,
            stdout=stdout,
            stderr=stderr,
            console_output=console_text,
        )
    if seen_dialogs:
        raise OfficialToolDialogError(sorted(seen_dialogs))
    _emit_log(log, f"外部工具完成，耗时 {result.duration_seconds:.1f} 秒。")
    return result
