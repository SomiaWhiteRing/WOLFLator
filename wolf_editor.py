from __future__ import annotations

import csv
import ctypes
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from collections import Counter, deque
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable, Iterator

from models import TranslationItem
from safe_io import atomic_write_json, package_lock, replace_with_retry
from wolf_command_catalog import (
    CATALOG_SCHEMA,
    VERIFIED_EDITOR_VERSION,
    command_semantics,
)
from wolf_tools import hash_directory, run_process, sha256_file


EDITOR_DOWNLOAD_URL = "https://silversecond.com/WolfRPGEditor/Download.shtml"
MAX_EDITOR_PAGE_BYTES = 2 * 1024 * 1024
# ponytail: This caps an official tool download, not game data; raise it if future packages outgrow 256 MiB.
MAX_EDITOR_ARCHIVE_BYTES = 256 * 1024 * 1024
MIN_EDITOR_VERSION = (3, 500)
AUTO_ANALYSIS_SCHEMA = 4
_VALUE_LIMIT = 256
_LOOP_LIMIT = 64
_CALL_DEPTH_LIMIT = 64
_CFG_STATE_VISIT_LIMIT = 64
_CFG_IMPLEMENTED_OPCODES = frozenset(
    {
        0, 111, 112, 170, 171, 172, 173, 174, 175, 176, 179,
        212, 213, 401, 402, 420, 421, 498, 499,
    }
)
_OFFICIAL_EDITOR_HOSTS = {"silversecond.com", "www.silversecond.com"}
_EDITOR_ARCHIVE_RE = re.compile(
    r"^WolfRPGEditor_(?P<version>\d+(?:\.\d+)+)(?P<mini>mini)?\.zip$",
    re.IGNORECASE,
)
_COMMAND_RE = re.compile(
    r'^\[(?P<opcode>\d+)]\[(?P<int_count>\d+),(?P<string_count>\d+)]'
    r'<(?P<indent>\d+)>\((?P<ints>.*?)\)(?P<tail>.*)$'
)
_WORKBOOK_DB_CODE_RE = re.compile(r"^(?P<database>UDB|CDB|SDB)-(?P<type>\d+)-(?P<data>\d+)-(?P<field>\d+)$", re.IGNORECASE)
_CSELF_REFERENCE_RE = re.compile(r"\\cself\[(\d+)]", re.IGNORECASE)
_STRING_REFERENCE_RE = re.compile(r"\\s\[(\d+)]", re.IGNORECASE)


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
class _Command:
    opcode: int
    ints: tuple[int, ...]
    strings: tuple[str, ...]
    indent: int
    raw: str


# Public IR names are deliberately small value objects. The parser still uses the
# private aliases below so this upgrade does not duplicate the proven parser.
AutoCommand = _Command


@dataclass(frozen=True)
class _CommandBlock:
    source: str
    event_type: str
    event_id: int
    event_name: str
    page: int
    commands: tuple[_Command, ...]
    value_inputs: int = 0
    string_inputs: int = 0
    return_target: int = -1


AutoEvent = _CommandBlock


@dataclass(frozen=True)
class AutoLabel:
    name: str
    target_command: int
    scope: str


@dataclass(frozen=True)
class AutoDatabaseCoordinate:
    database: str
    type_id: int
    data_id: int
    field_id: int


@dataclass(frozen=True)
class AutoEdge:
    source: str
    target: str
    kind: str


@dataclass(frozen=True)
class AutoProject:
    editor_version: str
    events: tuple[AutoEvent, ...]
    databases: tuple[str, ...]
    edges: tuple[AutoEdge, ...] = ()


@dataclass(frozen=True)
class _DatabaseType:
    database: str
    type_id: int
    name: str
    field_names: dict[int, str]
    field_types: dict[int, int]
    rows: tuple[tuple[str, ...], ...]
    data_names: tuple[str, ...]


@dataclass(frozen=True)
class _NumberValue:
    values: frozenset[int] | None
    reason: str = ""
    tracked: bool = False


@dataclass(frozen=True)
class _StringValue:
    source_keys: frozenset[str] = frozenset()
    cells: frozenset[tuple[str, int, int, int]] = frozenset()
    trace: tuple[str, ...] = ()
    unknown: str = ""
    symbolic_all: bool = False
    scopes: frozenset[str] = frozenset()
    literals: frozenset[str] | None = frozenset()

    @property
    def tracked(self) -> bool:
        return bool(self.source_keys or self.cells or self.symbolic_all or self.scopes)


@dataclass
class _AnalysisState:
    numbers: dict[int, _NumberValue]
    strings: dict[int, _StringValue]
    database_strings: dict[tuple[str, int, int, int], _StringValue]
    unknown_scopes: frozenset[str] = frozenset()
    unknown_reasons: frozenset[str] = frozenset()

    def copy(self) -> "_AnalysisState":
        return _AnalysisState(
            dict(self.numbers),
            dict(self.strings),
            dict(self.database_strings),
            self.unknown_scopes,
            self.unknown_reasons,
        )


@dataclass(frozen=True)
class _CallSummary:
    fell_through: bool
    exits: tuple[_AnalysisState, ...]
    summary_failed: str
    dependencies: tuple[dict[str, object], ...]
    blocking: tuple[dict[str, object], ...]
    unknown: Counter
    unknown_locations: tuple[tuple[tuple[int, str], tuple[str, ...]], ...]


_CallCache = dict[tuple[object, ...], _CallSummary]


class _BreakLoop(Exception):
    pass


class _ContinueLoop(Exception):
    pass


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


def inspect_wolf_editor(path: str | Path) -> EditorInfo:
    executable = Path(path).resolve()
    if executable.name.lower() != "editor.exe" or not executable.is_file():
        raise ValueError("请选择名为 Editor.exe 的 WOLF RPG Editor。")
    version, version_tuple, description = _windows_version_resource(executable)
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
    info = inspect_wolf_editor(executable)
    if info.version_tuple[: len(release.version_tuple)] != release.version_tuple:
        raise ValueError(
            f"Editor.exe 版本 {info.version} 与下载包版本 {release.version} 不一致。"
        )
    return info


def _validate_managed_editor(root: Path, release: EditorRelease | None = None) -> Path:
    metadata_path = root / "wolflator-package.json"
    if not metadata_path.is_file():
        raise ValueError("WOLF RPG Editor 托管包缺少安装元数据。")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
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
    info = inspect_wolf_editor(executable)
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
) -> Path:
    packages = Path(packages_root)
    with package_lock(packages, "install-wolf-editor"):
        packages.mkdir(parents=True, exist_ok=True)
        if log:
            log("正在检查 WOLF RPG Editor 官网最新版本...")
        release = discover_latest_editor_release()
        final = packages / release.version
        if final.exists() and not repair:
            try:
                executable = _validate_managed_editor(final, release)
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
            archive_sha256, archive_size = _download_editor_archive(
                release,
                part,
                progress=progress,
            )
            info = _extract_managed_editor(part, staging, release)
            atomic_write_json(
                staging / "wolflator-package.json",
                {
                    "version": release.version,
                    "source_url": release.url,
                    "archive_size": archive_size,
                    "archive_sha256": archive_sha256,
                    "editor_sha256": info.sha256,
                    "installed_at": time.time(),
                },
            )
            _validate_managed_editor(staging, release)
            if final.exists():
                shutil.rmtree(final)
            replace_with_retry(staging, final)
            if log:
                log(f"WOLF RPG Editor 已安装到 {final}")
            return _validate_managed_editor(final, release)
        finally:
            part.unlink(missing_ok=True)
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)


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
    for source in sorted(source_data.rglob("*.mps")):
        if not source.is_file():
            continue
        relative = source.relative_to(source_data)
        target = sandbox / "Data" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        maps.append(relative)
    return maps


def _read_lines(path: Path) -> Iterator[str]:
    with path.open("r", encoding="utf-8", newline="") as stream:
        for line in stream:
            yield line.rstrip("\r\n")


def _parse_command(line: str, location: str) -> _Command:
    match = _COMMAND_RE.fullmatch(line)
    if not match:
        raise ValueError(f"Auto.txt 命令记录损坏：{location}: {line[:120]}")
    ints_text = match.group("ints")
    ints = tuple(int(value) for value in ints_text.split(",") if value != "")
    tail = match.group("tail")
    if not tail.startswith("("):
        raise ValueError(f"Auto.txt 字符串参数缺失：{location}")
    quoted = False
    closing = -1
    index = 1
    while index < len(tail):
        char = tail[index]
        if char == '"':
            if quoted and index + 1 < len(tail) and tail[index + 1] == '"':
                index += 2
                continue
            quoted = not quoted
        elif char == ")" and not quoted:
            closing = index
            break
        index += 1
    if closing < 0:
        raise ValueError(f"Auto.txt 字符串参数未结束：{location}")
    strings_text = tail[1:closing]
    try:
        strings = tuple(next(csv.reader([strings_text], strict=True))) if strings_text else ()
    except csv.Error as error:
        raise ValueError(f"Auto.txt 字符串参数损坏：{location}: {error}") from error
    if len(ints) != int(match.group("int_count")):
        raise ValueError(f"Auto.txt 整数参数数量不符：{location}")
    if len(strings) != int(match.group("string_count")):
        raise ValueError(f"Auto.txt 字符串参数数量不符：{location}")
    return _Command(
        int(match.group("opcode")),
        ints,
        strings,
        int(match.group("indent")),
        line,
    )


def _event_blocks(
    path: Path, event_type: str, *, source: str | None = None
) -> tuple[list[_CommandBlock], dict[str, int]]:
    lines = iter(_read_lines(path))
    expected_header = (
        "[COMMON_EVENT_TEXT_OUTPUT]" if event_type == "common" else "[MAPDATA_TEXT_OUTPUT]"
    )
    try:
        header = next(lines)
    except StopIteration as error:
        raise ValueError(f"Auto.txt 为空：{path}") from error
    if header != expected_header:
        raise ValueError(f"Auto.txt 文件头错误：{path}")

    declared_events: int | None = None
    current_id = -1
    current_name = ""
    current_page = 0
    expected_commands: int | None = None
    value_inputs = 0
    string_inputs = 0
    return_target = -1
    blocks: list[_CommandBlock] = []
    event_ids: set[int] = set()
    command_count = 0
    line_number = 1
    for line in lines:
        line_number += 1
        if line.startswith("COMMON_EVENT_NUM=") or line.startswith("EVENT_NUM="):
            declared_events = int(line.split("=", 1)[1])
        elif line.startswith("COMMON_ID=") or line.startswith("EVENT_ID="):
            current_id = int(line.split("=", 1)[1])
            current_name = ""
            current_page = 0
            value_inputs = 0
            string_inputs = 0
            return_target = -1
            event_ids.add(current_id)
        elif line.startswith("COMMON_NAME=") or line.startswith("EVENT_NAME="):
            current_name = line.split("=", 1)[1]
        elif event_type == "common" and line.startswith("VALINPUT_NUM="):
            value_inputs = int(line.split("=", 1)[1])
        elif event_type == "common" and line.startswith("STRINPUT_NUM="):
            string_inputs = int(line.split("=", 1)[1])
        elif event_type == "common" and line.startswith("RETURN_VAL_TARGET="):
            return_target = int(line.split("=", 1)[1])
        elif line.startswith("COMMAND_NUM="):
            if current_id < 0:
                raise ValueError(f"COMMAND_NUM 缺少事件上下文：{path}:{line_number}")
            expected_commands = int(line.split("=", 1)[1])
        elif line == "WoditorEvCOMMAND_START":
            if expected_commands is None:
                raise ValueError(f"命令块缺少 COMMAND_NUM：{path}:{line_number}")
            commands: list[_Command] = []
            for raw in lines:
                line_number += 1
                if raw == "WoditorEvCOMMAND_END":
                    break
                commands.append(_parse_command(raw, f"{path}:{line_number}"))
            else:
                raise ValueError(f"命令块未结束：{path}:{line_number}")
            if len(commands) != expected_commands:
                raise ValueError(
                    f"COMMAND_NUM 不符：{path}:{line_number} 声明 {expected_commands}，实际 {len(commands)}"
                )
            current_page += 1
            blocks.append(
                _CommandBlock(
                    source or path.as_posix(),
                    event_type,
                    current_id,
                    current_name,
                    current_page,
                    tuple(commands),
                    value_inputs,
                    string_inputs,
                    return_target,
                )
            )
            command_count += len(commands)
            expected_commands = None

    if declared_events is None or len(event_ids) != declared_events:
        raise ValueError(
            f"事件数量不符：{path} 声明 {declared_events}，实际 {len(event_ids)}"
        )
    return blocks, {
        "events": len(event_ids),
        "pages": len(blocks),
        "commands": command_count,
    }


def _database_index(
    path: Path, database: str
) -> tuple[dict[int, _DatabaseType], dict[str, int]]:
    lines = iter(_read_lines(path))
    try:
        if next(lines) != "[DATABASE_TEXT_OUTPUT]":
            raise ValueError(f"数据库 Auto.txt 文件头错误：{path}")
    except StopIteration as error:
        raise ValueError(f"数据库 Auto.txt 为空：{path}") from error

    declared_types: int | None = None
    current_id: int | None = None
    current_name = ""
    item_num: int | None = None
    data_num: int | None = None
    field_names: dict[int, str] = {}
    field_types: dict[int, int] = {}
    rows: list[tuple[str, ...]] | None = None
    types: dict[int, _DatabaseType] = {}
    csv_rows = 0

    def finish_type() -> None:
        nonlocal csv_rows
        if current_id is None:
            return
        if item_num is None or data_num is None or rows is None:
            raise ValueError(f"数据库类型 {current_id} 缺少 ITEM_NUM、DATA_NUM 或 CSV：{path}")
        if item_num and (
            set(field_names) != set(range(item_num))
            or set(field_types) != set(range(item_num))
        ):
            raise ValueError(f"数据库类型 {current_id} 字段声明不完整：{path}")
        expected_rows = data_num + (1 if item_num else 0)
        if len(rows) != expected_rows:
            raise ValueError(
                f"数据库类型 {current_id} DATA_NUM 不符：声明 {data_num}，实际 {max(0, len(rows) - (1 if item_num else 0))}"
            )
        if any(
            len(row) not in {item_num + 1, item_num + 2}
            or (len(row) == item_num + 2 and row[-1] != "")
            for row in rows
        ):
            raise ValueError(f"数据库类型 {current_id} CSV 列数不符：{path}")
        if item_num:
            header = rows[0]
            if tuple(field_names[index] for index in range(item_num)) != header[:item_num]:
                raise ValueError(f"数据库类型 {current_id} CSV 表头与字段声明不符：{path}")
            content_rows = rows[1:]
            stored_names = dict(field_names)
            stored_types = dict(field_types)
        else:
            content_rows = rows
            stored_names = {}
            stored_types = {}
        data_rows = tuple(row[:item_num] for row in content_rows)
        data_names = tuple(row[item_num] for row in content_rows)
        types[current_id] = _DatabaseType(
            database,
            current_id,
            current_name,
            stored_names,
            stored_types,
            data_rows,
            data_names,
        )
        csv_rows += len(data_rows)

    for line in lines:
        if line.startswith("TYPE_NUM="):
            declared_types = int(line.split("=", 1)[1])
        elif line.startswith("TYPE_ID="):
            finish_type()
            current_id = int(line.split("=", 1)[1])
            current_name = ""
            item_num = None
            data_num = None
            field_names = {}
            field_types = {}
            rows = None
        elif line.startswith("TYPENAME="):
            current_name = line.split("=", 1)[1]
        elif line.startswith("ITEM_NUM="):
            item_num = int(line.split("=", 1)[1])
        elif line.startswith("DATA_NUM="):
            data_num = int(line.split("=", 1)[1])
        elif line.startswith("DATATYPE_") and "=" in line:
            key, value = line.split("=", 1)
            suffix = key.removeprefix("DATATYPE_")
            if suffix.isdigit():
                field_types[int(suffix)] = int(value)
        elif line.startswith("ITEMNAME") and "=" in line:
            key, value = line.split("=", 1)
            suffix = key.removeprefix("ITEMNAME")
            if suffix.isdigit():
                field_names[int(suffix)] = value
        elif line == "<<--CSV_START-->>":
            csv_lines: list[str] = []
            for csv_line in lines:
                if csv_line == "<<--CSV_END-->>":
                    break
                csv_lines.append(csv_line)
            else:
                raise ValueError(f"数据库 CSV 未结束：{path}")
            try:
                rows = [
                    tuple(row)
                    for row in csv.reader((value + "\n" for value in csv_lines), strict=True)
                    if row
                ]
            except csv.Error as error:
                raise ValueError(f"数据库 CSV 损坏：{path}: {error}") from error
    finish_type()
    if declared_types is None or len(types) != declared_types:
        raise ValueError(f"数据库类型数量不符：{path} 声明 {declared_types}，实际 {len(types)}")
    return types, {"types": len(types), "csv_rows": csv_rows}


def _condition_operator(encoded: int) -> tuple[int, str | None, bool]:
    operators = {
        0x00: "equals",
        0x10: "not_equals",
        0x20: "contains",
        0x30: "starts_with",
    }
    flags = (encoded >> 24) & 0xFF
    return encoded & 0x00FFFFFF, operators.get(flags & 0xF0), (flags & 0x0F) == 1


def _limited(values: set[int]) -> frozenset[int] | None:
    return frozenset(values) if len(values) <= _VALUE_LIMIT else None


def _number_argument(raw: int, state: _AnalysisState) -> _NumberValue:
    if raw < 1_000_000:
        return _NumberValue(frozenset({raw}))
    return state.numbers.get(raw, _NumberValue(None, f"变量 {raw} 的数值来源未知"))


def _calculate_numbers(left: _NumberValue, right: _NumberValue, operator: int) -> _NumberValue:
    tracked = left.tracked or right.tracked
    if left.values is None or right.values is None:
        return _NumberValue(None, left.reason or right.reason or "数值运算来源未知", tracked)
    output: set[int] = set()
    try:
        for a in left.values:
            for b in right.values:
                output.add({0: lambda: a + b, 1: lambda: a - b, 2: lambda: a * b,
                            3: lambda: a // b, 4: lambda: a % b}[operator]())
                if len(output) > _VALUE_LIMIT:
                    return _NumberValue(None, "数值集合超过 256 项", tracked)
    except (KeyError, ZeroDivisionError):
        return _NumberValue(None, f"未支持或无效的数值运算 {operator}", tracked)
    return _NumberValue(frozenset(output), tracked=tracked)


def _merge_numbers(left: _NumberValue | None, right: _NumberValue | None) -> _NumberValue | None:
    if left is None and right is None:
        return None
    if (
        left == right
        and left is not None
        and (left.values is None or len(left.values) <= _VALUE_LIMIT)
    ):
        return left
    if left is None or right is None:
        value = left or right
        assert value is not None
        return _NumberValue(None, "控制流仅在部分分支赋值", value.tracked)
    if left.values is None or right.values is None:
        return _NumberValue(None, left.reason or right.reason, left.tracked or right.tracked)
    values = _limited(set(left.values) | set(right.values))
    return _NumberValue(values, "数值集合超过 256 项" if values is None else "", left.tracked or right.tracked)


def _merge_strings(left: _StringValue | None, right: _StringValue | None) -> _StringValue | None:
    if left is None and right is None:
        return None
    if (
        left == right
        and left is not None
        and (left.symbolic_all or len(left.source_keys) + len(left.cells) <= _VALUE_LIMIT)
        and (left.literals is None or len(left.literals) <= _VALUE_LIMIT)
        and (left.tracked or not left.unknown)
        and len(left.trace) <= _VALUE_LIMIT
        and len(left.trace) == len(set(left.trace))
    ):
        return left
    if left is None or right is None:
        value = left or right
        assert value is not None
        return _StringValue(
            value.source_keys,
            value.cells,
            tuple(dict.fromkeys(value.trace + ("控制流部分分支赋值",))),
            value.unknown,
            value.symbolic_all,
            value.scopes,
            value.literals,
        )
    keys = set(left.source_keys) | set(right.source_keys)
    cells = set(left.cells) | set(right.cells)
    symbolic_all = left.symbolic_all or right.symbolic_all
    scopes = left.scopes | right.scopes
    literals = (
        None
        if left.literals is None or right.literals is None
        else frozenset(set(left.literals) | set(right.literals))
    )
    if literals is not None and len(literals) > _VALUE_LIMIT:
        literals = None
    if len(keys) + len(cells) > _VALUE_LIMIT and not symbolic_all:
        return _StringValue(
            trace=(left.trace + right.trace)[-_VALUE_LIMIT:],
            unknown="字符串来源集合超过 256 项",
            symbolic_all=True,
            scopes=scopes or frozenset({"project"}),
            literals=None,
        )
    return _StringValue(
        frozenset(keys),
        frozenset(cells),
        tuple(dict.fromkeys(left.trace + right.trace))[-_VALUE_LIMIT:],
        (left.unknown if left.tracked else "") or (right.unknown if right.tracked else ""),
        symbolic_all,
        scopes,
        literals,
    )


def _with_literals(
    value: _StringValue, literals: frozenset[str] | None
) -> _StringValue:
    return _StringValue(
        value.source_keys,
        value.cells,
        value.trace,
        value.unknown,
        value.symbolic_all,
        value.scopes,
        literals,
    )


def _string_value_status(value: _StringValue) -> tuple[str, str]:
    if not value.unknown and not value.symbolic_all:
        return "resolved", ""
    opaque_prefixes = (
        "来源经过未支持命令",
        "来源经过未解释的公共事件调用",
        "未支持的 122",
    )
    if value.unknown.startswith(opaque_prefixes):
        return "blocking", value.unknown
    return (
        "dynamic",
        value.unknown or "字符串来源已扩大为可定位的运行时符号范围",
    )


def _command_string_roles(
    command: _Command, semantics: dict[str, object] | None
) -> list[str]:
    roles = list(semantics.get("string_roles", [])) if semantics else []
    if command.opcode == 150 and command.strings:
        if not roles:
            roles = ["resource_path"]
        # Editor 3.713 stores the Picture content kind in the low byte.
        roles[0] = (
            "display_text"
            if command.ints and command.ints[0] & 0xFF == 0x20
            else "resource_path"
        )
    return roles


def _concat_literals(
    left: frozenset[str] | None, right: frozenset[str] | None
) -> frozenset[str] | None:
    if left is None or right is None:
        return None
    output = {a + b for a in left for b in right}
    return frozenset(output) if len(output) <= _VALUE_LIMIT else None


def _string_variable_for_escape(kind: str, index: int) -> int:
    if kind.lower() == "cself":
        return 1_600_000 + index
    return 3_000_000 + index


def _string_reference_value(literal: str, state: _AnalysisState) -> _StringValue | None:
    value: _StringValue | None = None
    for kind, pattern in (
        ("cself", _CSELF_REFERENCE_RE),
        ("s", _STRING_REFERENCE_RE),
    ):
        for reference in pattern.findall(literal):
            referenced = state.strings.get(_string_variable_for_escape(kind, int(reference)))
            if referenced is not None:
                value = _merge_strings(value, referenced)
    return value


def _expand_string_references(
    literals: frozenset[str] | None, state: _AnalysisState
) -> frozenset[str] | None:
    if literals is None:
        return None
    concrete = set(literals)
    changed = True
    while changed:
        changed = False
        for pattern, prefix in (
            (_CSELF_REFERENCE_RE, "cself"),
            (_STRING_REFERENCE_RE, "s"),
        ):
            for text in tuple(concrete):
                match = pattern.search(text)
                if match is None:
                    continue
                variable = _string_variable_for_escape(prefix, int(match.group(1)))
                value = state.strings.get(variable)
                replacements: frozenset[str] | None = value.literals if value else None
                if replacements is None and prefix == "cself":
                    number = state.numbers.get(variable)
                    if number is not None and number.values is not None:
                        replacements = frozenset(str(item) for item in number.values)
                if replacements is None:
                    return None
                token = match.group(0)
                concrete.remove(text)
                concrete.update(text.replace(token, replacement) for replacement in replacements)
                if len(concrete) > _VALUE_LIMIT:
                    return None
                changed = True
                break
            if changed:
                break
    return frozenset(concrete)


def _merge_states(states: list[_AnalysisState]) -> _AnalysisState:
    if not states:
        return _AnalysisState({}, {}, {})
    result = states[0].copy()
    for state in states[1:]:
        result.numbers = {
            key: value
            for key in set(result.numbers) | set(state.numbers)
            if (value := _merge_numbers(result.numbers.get(key), state.numbers.get(key))) is not None
        }
        result.strings = {
            key: value
            for key in set(result.strings) | set(state.strings)
            if (value := _merge_strings(result.strings.get(key), state.strings.get(key))) is not None
        }
        result.database_strings = {
            key: value
            for key in set(result.database_strings) | set(state.database_strings)
            if (
                value := _merge_strings(
                    result.database_strings.get(key), state.database_strings.get(key)
                )
            ) is not None
        }
        result.unknown_scopes = result.unknown_scopes | state.unknown_scopes
        result.unknown_reasons = result.unknown_reasons | state.unknown_reasons
    return result


def _state_cache_key(state: _AnalysisState) -> tuple[object, ...]:
    local_numbers = tuple(
        sorted(
            (key, value.values, value.tracked)
            for key, value in state.numbers.items()
            if 1_600_000 <= key < 1_600_100
        )
    )
    local_strings = tuple(
        sorted(
            (key, _string_semantic_key(value))
            for key, value in state.strings.items()
            if 1_600_000 <= key < 1_600_100
        )
    )
    database = tuple(
        sorted(
            (key, _string_semantic_key(value))
            for key, value in state.database_strings.items()
        )
    )
    return (local_numbers, local_strings, database)


def _string_semantic_key(value: _StringValue) -> tuple[object, ...]:
    return (
        value.source_keys,
        value.cells,
        _string_value_status(value)[0],
        value.symbolic_all,
        value.scopes,
        value.literals,
    )


def _states_semantically_equal(left: _AnalysisState, right: _AnalysisState) -> bool:
    if left.unknown_scopes != right.unknown_scopes:
        return False
    if left.numbers.keys() != right.numbers.keys():
        return False
    if any(
        value.values != right.numbers[key].values
        or value.tracked != right.numbers[key].tracked
        for key, value in left.numbers.items()
    ):
        return False
    if left.strings.keys() != right.strings.keys() or any(
        _string_semantic_key(value) != _string_semantic_key(right.strings[key])
        for key, value in left.strings.items()
    ):
        return False
    return left.database_strings.keys() == right.database_strings.keys() and not any(
        _string_semantic_key(value)
        != _string_semantic_key(right.database_strings[key])
        for key, value in left.database_strings.items()
    )


def _event_code(block: _CommandBlock, command_index: int, string_index: int) -> str:
    if block.event_type == "common":
        return f"COMMON-{block.event_id}-{command_index - 1}-{string_index}"
    match = re.search(r"Map(\d+)\.mps\.Auto\.txt$", block.source, re.IGNORECASE)
    map_id = int(match.group(1)) if match else 0
    return f"MAP-{map_id}-Ev{block.event_id:03d}-Page{block.page}-{command_index - 1}-{string_index}"


class _BlockAnalyzer:
    def __init__(
        self,
        block: _CommandBlock,
        databases: dict[str, dict[int, _DatabaseType]],
        database_keys: dict[tuple[str, int, int, int], frozenset[str]],
        event_items: dict[str, tuple[TranslationItem, ...]],
        common_by_id: dict[int, _CommandBlock] | None = None,
        common_by_name: dict[str, tuple[_CommandBlock, ...]] | None = None,
        event_scopes: dict[int, frozenset[str]] | None = None,
        call_stack: tuple[tuple[int, int | None], ...] = (),
        call_cache: _CallCache | None = None,
    ) -> None:
        self.block = block
        self.databases = databases
        self.database_keys = database_keys
        self.event_items = event_items
        self.common_by_id = common_by_id or {}
        self.common_by_name = common_by_name or {}
        self.event_scopes = event_scopes or {}
        self.call_stack = call_stack
        self.call_cache = call_cache if call_cache is not None else {}
        self.dependencies: list[dict[str, object]] = []
        self.blocking: list[dict[str, object]] = []
        self.unknown = Counter()
        self.unknown_locations: dict[tuple[int, str], list[str]] = {}
        self._unknown_seen: set[tuple[int, str, str]] = set()
        self.summary_failed = ""
        labels: dict[str, list[int]] = {}
        for position, command in enumerate(block.commands):
            if command.opcode == 212 and len(command.strings) == 1:
                labels.setdefault(command.strings[0], []).append(position)
        self.labels = {name: tuple(positions) for name, positions in labels.items()}
        self._condition_regions: dict[int, tuple[int, tuple[tuple[int, int], ...]]] = {}
        self._branch_exits: dict[int, int] = {}
        self._loop_ends: dict[int, int] = {}
        self._loop_starts: dict[int, int] = {}
        self._enclosing_loops: dict[int, tuple[int, int]] = {}
        self._index_control_flow()

    def _index_control_flow(self) -> None:
        commands = self.block.commands
        for index, command in enumerate(commands):
            if command.opcode in {111, 112}:
                closing = self._matching(index, len(commands), 499)
                if closing is None:
                    continue
                markers = tuple(
                    position
                    for position in range(index + 1, closing)
                    if commands[position].indent == command.indent
                    and commands[position].opcode in {401, 420, 421}
                )
                branches: list[tuple[int, int]] = []
                for offset, marker in enumerate(markers):
                    branch_end = markers[offset + 1] if offset + 1 < len(markers) else closing
                    branches.append((marker, branch_end))
                    if marker + 1 < branch_end:
                        last = branch_end - 1
                        self._branch_exits[last] = max(
                            self._branch_exits.get(last, 0), closing + 1
                        )
                self._condition_regions[index] = (closing, tuple(branches))
            elif command.opcode in {170, 179}:
                closing = self._matching(index, len(commands), 498)
                if closing is not None:
                    self._loop_ends[index] = closing
                    self._loop_starts[closing] = index

        loops = sorted(
            self._loop_ends.items(), key=lambda item: (item[1] - item[0], -item[0])
        )
        for index in range(len(commands)):
            enclosing = next(
                ((start, closing) for start, closing in loops if start < index < closing),
                None,
            )
            if enclosing is not None:
                self._enclosing_loops[index] = enclosing

    def _dynamic_entry_dispatcher(
        self, block: _CommandBlock | None = None
    ) -> int | None:
        target = block or self.block
        if target.event_type != "common":
            return None
        for index, command in enumerate(target.commands[:-1]):
            following = target.commands[index + 1]
            if (
                command.indent == 0
                and following.indent == 0
                and command.opcode == 122
                and command.ints[:2] == (3_000_001, 0)
                and command.strings == ("cmd:\\cself[0]",)
                and following.opcode == 213
                and following.strings == ("\\s[1]",)
            ):
                return index
        return None

    def _resource_reference(
        self,
        command: _Command,
        index: int,
        state: _AnalysisState,
        semantics: dict[str, object] | None,
    ) -> None:
        if not semantics:
            return
        roles = _command_string_roles(command, semantics)
        protected_roles = {
            "resource_path",
            "file_path",
            "label",
            "label_target",
        }
        for string_index, role in enumerate(roles):
            if role not in protected_roles or string_index >= len(command.strings):
                continue
            value = self._literal_string(command, index, string_index, state)
            if not value.tracked:
                continue
            status, reason = _string_value_status(value)
            code = _event_code(self.block, index + 1, string_index).upper()
            dependency = {
                    "kind": "resource",
                    "auto_file": self.block.source,
                    "event_type": self.block.event_type,
                    "event_id": self.block.event_id,
                    "event_name": self.block.event_name,
                    "page": self.block.page,
                    "command": index + 1,
                    "string_index": string_index,
                    "condition_code": code,
                    "condition_keys": [],
                    "operator": "resource_reference",
                    "literal": command.strings[string_index],
                    "right_is_variable": False,
                    "source_keys": sorted(value.source_keys),
                    "right_source_keys": [],
                    "database_cells": [],
                    "right_database_cells": [],
                    "trace": list(value.trace),
                    "right_trace": [],
                    "unresolved_scopes": sorted(value.scopes),
                    "status": status,
                    "reason": reason,
                    "resource_role": role,
            }
            self.dependencies.append(dependency)
            if dependency["status"] == "blocking":
                self.blocking.append(dependency)

    def _display_reference(
        self,
        command: _Command,
        index: int,
        state: _AnalysisState,
        semantics: dict[str, object] | None,
    ) -> None:
        if not semantics:
            return
        roles = _command_string_roles(command, semantics)
        for string_index, role in enumerate(roles):
            if role != "display_text" or string_index >= len(command.strings):
                continue
            value = self._literal_string(command, index, string_index, state)
            if not value.tracked:
                continue
            status, reason = _string_value_status(value)
            self.dependencies.append({
                "kind": "display",
                "auto_file": self.block.source,
                "event_type": self.block.event_type,
                "event_id": self.block.event_id,
                "event_name": self.block.event_name,
                "page": self.block.page,
                "command": index + 1,
                "string_index": string_index,
                "condition_code": _event_code(
                    self.block, index + 1, string_index
                ).upper(),
                "condition_keys": [],
                "operator": "display",
                "literal": command.strings[string_index],
                "right_is_variable": False,
                "source_keys": sorted(value.source_keys),
                "right_source_keys": [],
                "database_cells": [
                    {
                        "database": cell[0],
                        "type": cell[1],
                        "data": cell[2],
                        "field": cell[3],
                    }
                    for cell in sorted(value.cells)
                ],
                "right_database_cells": [],
                "trace": list(value.trace),
                "right_trace": [],
                "unresolved_scopes": sorted(value.scopes),
                "status": status,
                "reason": reason,
            })

    def _value_boundary_reference(
        self,
        command: _Command,
        index: int,
        value: _StringValue,
        role: str,
        scopes: frozenset[str] = frozenset(),
    ) -> None:
        if not value.tracked:
            return
        status, reason = _string_value_status(value)
        dependency = {
            "kind": (
                "flow"
                if role == "common_event_return"
                else "state"
                if role == "global_string_write"
                else "resource"
            ),
            "auto_file": self.block.source,
            "event_type": self.block.event_type,
            "event_id": self.block.event_id,
            "event_name": self.block.event_name,
            "page": self.block.page,
            "command": index + 1,
            "string_index": -1,
            "condition_code": "",
            "condition_keys": [],
            "operator": "value_boundary",
            "literal": "",
            "right_is_variable": False,
            "source_keys": sorted(value.source_keys),
            "right_source_keys": [],
            "database_cells": [
                {"database": cell[0], "type": cell[1], "data": cell[2], "field": cell[3]}
                for cell in sorted(value.cells)
            ],
            "right_database_cells": [],
            "trace": list(value.trace),
            "right_trace": [],
            "unresolved_scopes": sorted(value.scopes | scopes),
            "status": status,
            "reason": reason,
            "resource_role": role,
        }
        self.dependencies.append(dependency)
        if dependency["status"] == "blocking":
            self.blocking.append(dependency)

    def _location(self, index: int) -> str:
        return (
            f"{self.block.source} event={self.block.event_id} page={self.block.page} "
            f"command={index + 1}"
        )

    def _current_scope(self) -> frozenset[str]:
        if self.block.event_type == "common":
            return frozenset({f"common:{self.block.event_id}"})
        match = re.search(
            r"Map(\d+)\.mps\.Auto\.txt$", self.block.source, re.IGNORECASE
        )
        map_id = int(match.group(1)) if match else 0
        return frozenset(
            {f"map:{map_id}:{self.block.event_id}:{self.block.page}"}
        )

    def _record_unknown(self, command: _Command, index: int, shape: str | None = None) -> None:
        description = shape or f"ints={len(command.ints)},strings={len(command.strings)}"
        location = self._location(index)
        seen_key = (command.opcode, description, location)
        if seen_key in self._unknown_seen:
            return
        self._unknown_seen.add(seen_key)
        key = (command.opcode, description)
        self.unknown[key] += 1
        self.unknown_locations.setdefault(key, []).append(location)

    def _blocking_scope_dependency(
        self,
        command: _Command,
        index: int,
        kind: str,
        reason: str,
        scopes: frozenset[str],
        values: Iterable[_StringValue] = (),
        *,
        status: str = "blocking",
    ) -> None:
        values = tuple(values)
        source_keys = sorted({key for value in values for key in value.source_keys})
        cells = sorted({cell for value in values for cell in value.cells})
        scopes = scopes | frozenset(
            scope for value in values for scope in value.scopes
        )
        dependency = {
            "kind": kind,
            "auto_file": self.block.source,
            "event_type": self.block.event_type,
            "event_id": self.block.event_id,
            "event_name": self.block.event_name,
            "page": self.block.page,
            "command": index + 1,
            "string_index": -1,
            "condition_code": "",
            "condition_keys": [],
            "operator": "opaque_effect" if kind == "opaque" else "event_call",
            "literal": command.strings[0] if command.strings else "",
            "right_is_variable": False,
            "source_keys": source_keys,
            "right_source_keys": [],
            "database_cells": [
                {"database": cell[0], "type": cell[1], "data": cell[2], "field": cell[3]}
                for cell in cells
            ],
            "right_database_cells": [],
            "trace": [self._location(index)],
            "right_trace": [],
            "unresolved_scopes": sorted(scopes),
            "unresolved_reasons": [reason],
            "status": status,
            "reason": reason,
        }
        self.dependencies.append(dependency)
        if status == "blocking":
            self.blocking.append(dependency)

    def _type_ids(self, database: str, command: _Command, flags: int, state: _AnalysisState) -> set[int] | None:
        types = self.databases.get(database, {})
        if flags & 0x01:
            name = command.strings[1] if len(command.strings) > 1 else ""
            return {type_id for type_id, item in types.items() if item.name == name}
        value = _number_argument(command.ints[0], state)
        return set(value.values) if value.values is not None else None

    def _selector(
        self, raw: int, state: _AnalysisState, *, unknown_means_all: bool
    ) -> set[int] | None:
        value = _number_argument(raw, state)
        if value.values is None:
            return set() if unknown_means_all else None
        return set(value.values)

    def _database(self, command: _Command, index: int, state: _AnalysisState) -> None:
        if len(command.ints) not in {4, 5}:
            self._record_unknown(command, index, "invalid-250")
            return
        flags = command.ints[3]
        byte1 = (flags >> 8) & 0xFF
        byte2 = (flags >> 16) & 0xFF
        database = {0: "CDB", 1: "SDB", 2: "UDB"}.get(byte1 & 0x0F)
        if database is None:
            self._record_unknown(command, index, f"250-flags-{flags:08x}")
            return
        if byte1 & 0xF0 != 0x10:
            if len(command.ints) == 4 and command.strings:
                self._write_database_string(command, index, state, database, byte2)
            return
        if len(command.ints) != 5:
            self._record_unknown(command, index, "invalid-250-read")
            return
        destination = command.ints[4] & 0x00FFFFFF
        selected_type_ids = self._type_ids(database, command, byte2, state)
        if selected_type_ids == set():
            state.strings[destination] = _StringValue(literals=frozenset())
            return
        type_ids = (
            set(self.databases.get(database, {}))
            if selected_type_ids is None
            else selected_type_ids
        )

        data_raw, field_raw = command.ints[1], command.ints[2]
        if data_raw == -3 and field_raw == -3:
            state.numbers[destination] = _NumberValue(
                _limited(type_ids),
                "数据库类型名称无法唯一解析" if len(type_ids) > _VALUE_LIMIT else "",
                True,
            )
            return
        if data_raw == -3 and field_raw != -3:
            names = {command.strings[3]} if byte2 & 0x04 and len(command.strings) > 3 else set()
            fields = {
                field_id
                for type_id in type_ids
                for field_id, name in self.databases[database].get(type_id, _DatabaseType(database, type_id, "", {}, {}, (), ())).field_names.items()
                if name in names
            }
            state.numbers[destination] = _NumberValue(
                _limited(fields),
                "数据库字段名称无法唯一解析" if not fields or len(fields) > _VALUE_LIMIT else "",
                True,
            )
            return

        if field_raw == -3:
            if byte2 & 0x02:
                data_name = command.strings[2] if len(command.strings) > 2 else ""
                data_ids = {
                    data_id
                    for type_id in type_ids
                    for data_id, name in enumerate(
                        self.databases[database][type_id].data_names
                    )
                    if name == data_name
                }
                state.numbers[destination] = _NumberValue(
                    _limited(data_ids),
                    "数据库数据名称无法唯一解析" if len(data_ids) > _VALUE_LIMIT else "",
                    True,
                )
                return
            selected = self._selector(data_raw, state, unknown_means_all=True)
            data_ids = selected if selected else {
                data_id
                for type_id in type_ids
                for data_id in range(len(self.databases[database][type_id].rows))
            }
            keys: set[str] = set()
            cells: set[tuple[str, int, int, int]] = set()
            names: set[str] = set()
            for type_id in type_ids:
                db_type = self.databases[database].get(type_id)
                if db_type is None:
                    continue
                for data_id in data_ids:
                    if not 0 <= data_id < len(db_type.data_names):
                        continue
                    names.add(db_type.data_names[data_id])
                    coordinate = (database, type_id, data_id, 0)
                    coordinate_keys = self.database_keys.get(coordinate, ())
                    if coordinate_keys:
                        keys.update(coordinate_keys)
                        cells.add(coordinate)
            scopes = frozenset(
                f"database:{database}:{type_id}:*:0" for type_id in type_ids
            )
            symbolic = not selected or len(keys) + len(cells) > _VALUE_LIMIT
            state.strings[destination] = _StringValue(
                frozenset() if symbolic else frozenset(keys),
                frozenset() if symbolic else frozenset(cells),
                (f"{self._location(index)} opcode=250 {database} data-name",),
                unknown=("数据库数据名来源集合超过 256 项" if symbolic else ""),
                symbolic_all=symbolic,
                scopes=scopes if symbolic else frozenset(),
                literals=(frozenset(names) if len(names) <= _VALUE_LIMIT else None),
            )
            return

        data_all = False
        if byte2 & 0x02:
            data_name = command.strings[2] if len(command.strings) > 2 else ""
            data_ids = {
                data_id
                for type_id in type_ids
                for data_id, name in enumerate(self.databases[database][type_id].data_names)
                if name == data_name
            }
        else:
            selected = self._selector(data_raw, state, unknown_means_all=True)
            data_all = not selected
            data_ids = selected if selected else {
                data_id
                for type_id in type_ids
                for data_id in range(len(self.databases[database][type_id].rows))
            }
        if byte2 & 0x04:
            field_name = command.strings[3] if len(command.strings) > 3 else ""
            field_ids = {
                field_id
                for type_id in type_ids
                for field_id, name in self.databases[database][type_id].field_names.items()
                if name == field_name
            }
        else:
            selected = self._selector(field_raw, state, unknown_means_all=False)
            field_ids = selected or set()
        if not field_ids:
            state.strings[destination] = _StringValue(
                trace=(self._location(index),),
                unknown="数据库字段选择器无法解析",
                symbolic_all=True,
                scopes=frozenset(
                    f"database:{database}:{type_id}:*:*" for type_id in type_ids
                ),
                literals=None,
            )
            return

        cells: set[tuple[str, int, int, int]] = set()
        keys: set[str] = set()
        string_values: set[str] = set()
        numeric_values: set[int] = set()
        string_field = False
        for type_id in type_ids:
            db_type = self.databases[database].get(type_id)
            if db_type is None:
                continue
            for data_id in data_ids:
                if not 0 <= data_id < len(db_type.rows):
                    continue
                for field_id in field_ids:
                    if field_id not in db_type.field_types:
                        continue
                    coordinate = (database, type_id, data_id, field_id)
                    if db_type.field_types[field_id] >= 2000:
                        string_field = True
                        coordinate_keys = self.database_keys.get(coordinate, ())
                        runtime_value = state.database_strings.get(coordinate)
                        if runtime_value is not None:
                            keys.update(runtime_value.source_keys)
                            cells.update(runtime_value.cells)
                            if runtime_value.literals is not None:
                                string_values.update(runtime_value.literals)
                        elif coordinate_keys:
                            cells.add(coordinate)
                            keys.update(coordinate_keys)
                            string_values.add(db_type.rows[data_id][field_id])
                    else:
                        try:
                            numeric_values.add(int(db_type.rows[data_id][field_id]))
                        except ValueError:
                            pass
        trace = (
            f"{self._location(index)} opcode=250 {database} types={sorted(type_ids)} "
            f"data={'all' if not data_ids else sorted(data_ids)[:8]} fields={sorted(field_ids)}",
        )
        if string_field:
            scopes = frozenset(
                f"database:{database}:{type_id}:*:{field_id}"
                for type_id in type_ids
                for field_id in field_ids
            )
            if len(keys) + len(cells) > _VALUE_LIMIT:
                state.strings[destination] = _StringValue(
                    trace=trace,
                    unknown="数据库字符串来源集合超过 256 项",
                    symbolic_all=True,
                    scopes=scopes,
                    literals=None,
                )
            else:
                state.strings[destination] = _StringValue(
                    frozenset(keys),
                    frozenset(cells),
                    trace,
                    symbolic_all=data_all,
                    scopes=scopes if data_all else frozenset(),
                    literals=(
                        frozenset(string_values)
                        if len(string_values) <= _VALUE_LIMIT
                        else None
                    ),
                )
        elif numeric_values:
            values = _limited(numeric_values)
            state.numbers[destination] = _NumberValue(
                values, "数据库数值集合超过 256 项" if values is None else "", True
            )

    def _write_database_string(
        self,
        command: _Command,
        index: int,
        state: _AnalysisState,
        database: str,
        selector_flags: int,
    ) -> None:
        type_ids = self._type_ids(database, command, selector_flags, state)
        if not type_ids:
            self._blocking_scope_dependency(
                command,
                index,
                "database",
                "数据库写入类型无法解析",
                frozenset({f"database:{database}:*:*:*"}),
            )
            return
        if selector_flags & 0x02:
            data_name = command.strings[2] if len(command.strings) > 2 else ""
            data_ids = {
                data_id
                for type_id in type_ids
                for data_id, name in enumerate(self.databases[database][type_id].data_names)
                if name == data_name
            }
        else:
            data_ids = self._selector(command.ints[1], state, unknown_means_all=False) or set()
        if selector_flags & 0x04:
            field_name = command.strings[3] if len(command.strings) > 3 else ""
            field_ids = {
                field_id
                for type_id in type_ids
                for field_id, name in self.databases[database][type_id].field_names.items()
                if name == field_name
            }
        else:
            field_ids = self._selector(command.ints[2], state, unknown_means_all=False) or set()
        coordinates = {
            (database, type_id, data_id, field_id)
            for type_id in type_ids
            for data_id in data_ids
            for field_id in field_ids
        }
        if len(coordinates) > _VALUE_LIMIT:
            scopes = frozenset(
                f"database:{database}:{type_id}:*:{field_id}"
                for type_id in type_ids
                for field_id in field_ids
            ) or frozenset({f"database:{database}:*:*:*"})
            self._blocking_scope_dependency(
                command,
                index,
                "database",
                "数据库写入坐标超过静态展开上限",
                scopes,
            )
            return
        value = self._literal_string(command, index, 0, state)
        for coordinate in coordinates:
            state.database_strings[coordinate] = value
        self._value_boundary_reference(
            command,
            index,
            value,
            "database_string_write",
            frozenset(
                f"database:{item[0]}:{item[1]}:{item[2]}:{item[3]}"
                for item in coordinates
            ),
        )

    def _set_runtime_value(
        self,
        command: _Command,
        index: int,
        state: _AnalysisState,
        *,
        string_result: bool,
    ) -> None:
        if not command.ints:
            self._record_unknown(command, index, "missing-destination")
            return
        destination = command.ints[0] & 0x00FFFFFF
        if string_result:
            state.strings[destination] = _StringValue(
                trace=(self._location(index),),
                unknown=f"字符串由运行时命令 opcode={command.opcode} 取得",
                literals=None,
            )
        else:
            state.numbers[destination] = _NumberValue(
                None, f"数值由运行时命令 opcode={command.opcode} 取得"
            )

    def _set_number(self, command: _Command, index: int, state: _AnalysisState) -> None:
        if len(command.ints) < 4:
            self._record_unknown(command, index, "invalid-121")
            return
        destination, left_raw, right_raw, flags = command.ints[:4]
        byte0 = flags & 0xFF
        byte1 = (flags >> 8) & 0xFF
        left = _number_argument(left_raw, state)
        right = _number_argument(right_raw, state)
        if byte0:
            state.numbers[destination] = _NumberValue(
                None,
                f"121 运行时数值模式 flags={flags}",
                left.tracked or right.tracked,
            )
            return
        value = _calculate_numbers(
            left,
            right,
            (byte1 >> 4) & 0x0F,
        )
        assignment = byte1 & 0x0F
        if assignment == 0:
            state.numbers[destination] = value
        elif assignment in {1, 2}:
            current = state.numbers.get(destination, _NumberValue(None, "复合赋值前值未知"))
            state.numbers[destination] = _calculate_numbers(current, value, assignment - 1)
        else:
            state.numbers[destination] = _NumberValue(
                None,
                f"121 运行时赋值模式 {assignment}",
                value.tracked,
            )

    def _literal_string(
        self,
        command: _Command,
        index: int,
        string_index: int,
        state: _AnalysisState,
    ) -> _StringValue:
        literal = command.strings[string_index] if string_index < len(command.strings) else ""
        keys = frozenset(
            item.key
            for item in self.event_items.get(
                _event_code(self.block, index + 1, string_index).upper(), ()
            )
            if item.original == literal
        )
        if self.block.event_type == "common":
            source_scope = f"common:{self.block.event_id}"
        else:
            match = re.search(r"Map(\d+)\.mps\.Auto\.txt$", self.block.source, re.IGNORECASE)
            map_id = int(match.group(1)) if match else 0
            source_scope = f"map:{map_id}:{self.block.event_id}:{self.block.page}"
        value = _StringValue(
            keys,
            trace=(f"{self._location(index)} opcode={command.opcode} literal",),
            scopes=frozenset({source_scope}) if keys else frozenset(),
            literals=frozenset({literal}),
        )
        referenced = _string_reference_value(literal, state)
        if referenced is not None:
            value = _merge_strings(value, referenced) or value
        concrete = _expand_string_references(frozenset({literal}), state)
        value = _with_literals(value, concrete)
        return value

    def _set_string(self, command: _Command, index: int, state: _AnalysisState) -> None:
        if len(command.ints) < 2:
            self._record_unknown(command, index, "invalid-122")
            return
        destination, flags = command.ints[:2]
        source_raw = command.ints[2] if len(command.ints) > 2 else 0
        source_kind = flags & 0x0F
        assignment = (flags >> 8) & 0x0F
        if source_kind == 1 and len(command.ints) < 3:
            self._record_unknown(command, index, "invalid-122-variable-source")
            return
        if source_kind == 0:
            value = self._literal_string(command, index, 0, state)
        elif source_kind == 1:
            value = state.strings.get(
                source_raw & 0x00FFFFFF,
                _StringValue(
                    unknown=f"字符串变量 {source_raw & 0x00FFFFFF} 来源未知",
                    literals=None,
                ),
            )
        elif source_kind == 2:
            pointer = _number_argument(source_raw, state)
            pointed_values: list[_StringValue] = []
            if pointer.values is not None:
                for raw in pointer.values:
                    pointed = state.strings.get(raw & 0x00FFFFFF)
                    if pointed is not None:
                        pointed_values.append(pointed)
            if pointed_values:
                value = None
                for pointed in pointed_values:
                    value = _merge_strings(value, pointed)
                value = value or _StringValue(literals=None)
            else:
                # ponytail: WOLF can load the source string variable through a
                # numeric variable. If that pointer is dynamic we keep the value
                # dynamic, not opaque; later safety replay will preserve anything
                # whose logic depends on it.
                value = _StringValue(
                    trace=(f"{self._location(index)} opcode=122 dynamic source pointer",),
                    scopes=frozenset(),
                    literals=None,
                )
        elif source_kind == 3:
            value = _StringValue(
                trace=(f"{self._location(index)} opcode=122 runtime string input",),
                literals=None,
            )
        else:
            current = state.strings.get(destination)
            state.strings[destination] = _StringValue(
                current.source_keys if current else frozenset(),
                current.cells if current else frozenset(),
                current.trace if current else (),
                f"未支持的 122 来源模式 {source_kind}",
                current.symbolic_all if current else False,
                current.scopes if current else frozenset(),
                current.literals if current else None,
            )
            return
        current = state.strings.get(destination)
        literal_operands: _StringValue | None = None
        for string_index in range(len(command.strings)):
            literal_operands = _merge_strings(
                literal_operands,
                self._literal_string(command, index, string_index, state),
            )
        extended_string_operation = bool(flags & 0x00040000)

        def derived(*values: _StringValue | None, note: str) -> _StringValue:
            merged: _StringValue | None = None
            for item in values:
                if item is not None:
                    merged = _merge_strings(merged, item)
            merged = merged or _StringValue(literals=None)
            return _StringValue(
                merged.source_keys,
                merged.cells,
                tuple(dict.fromkeys(merged.trace + (f"{self._location(index)} opcode=122 {note}",))),
                merged.unknown,
                merged.symbolic_all,
                merged.scopes,
                None,
            )

        if extended_string_operation and assignment in {3, 4, 5}:
            traced = derived(current, value, literal_operands, note=f"extended-op={assignment}")
            state.strings[destination] = traced
            if assignment == 3 and source_kind == 1:
                state.strings[source_raw & 0x00FFFFFF] = traced
        elif assignment == 0:
            state.strings[destination] = value
        elif assignment == 1:
            merged = _merge_strings(current, value) or value
            state.strings[destination] = _with_literals(
                merged,
                _concat_literals(
                    current.literals if current else frozenset({""}), value.literals
                ),
            )
        elif assignment in {2, 3, 4, 10, 11}:
            # ponytail: Auto protection tracks provenance, not WOLF's concrete string values.
            traced = derived(current if assignment in {10, 11} else None, value, note=f"op={assignment}")
            state.strings[destination] = traced
            if assignment in {3, 4} and source_kind == 1:
                state.strings[source_raw & 0x00FFFFFF] = traced
        elif assignment in {5, 7, 8}:
            self._value_boundary_reference(command, index, value, "file_path_runtime_read")
            state.strings[destination] = derived(value, note=f"op={assignment} runtime-read")
        elif assignment == 6:
            self._value_boundary_reference(
                command,
                index,
                derived(current, value, note="op=6 file-write"),
                "file_path_runtime_write",
            )
            if current is not None:
                state.strings[destination] = current
        elif assignment == 9 and source_kind == 0:
            literal_keys = {
                item.key
                for string_index, literal in enumerate(command.strings)
                for item in self.event_items.get(
                    _event_code(self.block, index + 1, string_index).upper(), ()
                )
                if item.original == literal
            }
            replacement = _StringValue(
                frozenset(literal_keys),
                trace=(f"{self._location(index)} opcode=122 op=9",),
                literals=frozenset(command.strings),
            )
            state.strings[destination] = derived(current, replacement, note="op=9")
        else:
            state.strings[destination] = _StringValue(
                value.source_keys, value.cells, value.trace,
                f"未支持的 122 赋值运算 {assignment}", value.symbolic_all,
                value.scopes,
                None,
            )
        result = state.strings.get(destination)
        if result is None:
            return
        writes_global = not 1_600_000 <= destination < 1_600_100
        if assignment in {3, 4} and source_kind == 1:
            source = source_raw & 0x00FFFFFF
            writes_global = writes_global or not 1_600_000 <= source < 1_600_100
        if writes_global:
            self._value_boundary_reference(
                command, index, result, "global_string_write"
            )
        if (
            self.block.event_type == "common"
            and self.block.return_target >= 5
            and destination == 1_600_000 + self.block.return_target
        ):
            self._value_boundary_reference(
                command, index, result, "common_event_return"
            )

    def _condition(self, command: _Command, index: int, state: _AnalysisState) -> None:
        if not command.ints:
            self._record_unknown(command, index, "invalid-112")
            return
        # Editor 3.713 uses both a bare count and 0x10 | count.
        count = command.ints[0] & 0x0F
        if count < 0 or len(command.ints) < count + 1 or len(command.strings) < count:
            self._record_unknown(command, index, "invalid-112-count")
            return
        for condition_index in range(count):
            variable, operator, right_is_variable = _condition_operator(command.ints[condition_index + 1])
            condition_code = _event_code(self.block, index + 1, condition_index).upper()
            condition_keys = sorted(
                item.key
                for item in self.event_items.get(condition_code, ())
                if item.original == command.strings[condition_index]
            )
            value = state.strings.get(variable)
            literal = command.strings[condition_index]
            right_value: _StringValue | None = None
            if right_is_variable:
                right_index = count + 1 + condition_index
                right_variable = command.ints[right_index] & 0x00FFFFFF if right_index < len(command.ints) else -1
                right_value = state.strings.get(right_variable)
            if state.unknown_scopes:
                status = "blocking"
                reason = "条件执行前经过可能读写字符串的不透明命令"
            elif operator is None:
                status = "blocking" if value and value.tracked else "untracked"
                reason = "未支持的字符串比较编码"
            elif right_is_variable and (
                value is None or right_value is None or not value.tracked or not right_value.tracked
            ):
                status = "untracked"
                reason = "字符串变量比较的一侧来源未知"
            elif right_is_variable and (value.unknown or right_value.unknown):
                left_status, left_reason = _string_value_status(value)
                right_status, right_reason = _string_value_status(right_value)
                status = "blocking" if "blocking" in {left_status, right_status} else "dynamic"
                reason = left_reason or right_reason
            elif right_is_variable and (
                value.literals is None or right_value.literals is None
            ):
                status = "dynamic"
                reason = "字符串变量比较的具体值为运行时动态值"
            elif value is None or not value.tracked:
                status = "untracked"
                reason = f"条件变量 {variable} 从事件入口进入"
            elif value.unknown or value.symbolic_all:
                status, reason = _string_value_status(value)
            elif value.literals is None:
                status = "dynamic"
                reason = "条件字符串的具体值为运行时动态值"
            else:
                status = "resolved"
                reason = ""
            dependency = {
                "kind": "condition",
                "auto_file": self.block.source,
                "event_type": self.block.event_type,
                "event_id": self.block.event_id,
                "event_name": self.block.event_name,
                "page": self.block.page,
                "command": index + 1,
                "string_index": condition_index,
                "condition_code": condition_code,
                "condition_keys": condition_keys,
                "operator": operator or "unknown",
                "literal": literal,
                "right_is_variable": right_is_variable,
                "source_keys": sorted(value.source_keys) if value else [],
                "right_source_keys": sorted(right_value.source_keys) if right_value else [],
                "database_cells": [
                    {"database": cell[0], "type": cell[1], "data": cell[2], "field": cell[3]}
                    for cell in sorted(value.cells if value else ())
                ],
                "right_database_cells": [
                    {"database": cell[0], "type": cell[1], "data": cell[2], "field": cell[3]}
                    for cell in sorted(right_value.cells if right_value else ())
                ],
                "trace": list(value.trace if value else ()),
                "right_trace": list(right_value.trace if right_value else ()),
                "left_values": sorted(value.literals) if value and value.literals is not None else [],
                "right_values": (
                    sorted(right_value.literals)
                    if right_value and right_value.literals is not None
                    else []
                ),
                "source_scopes": sorted(value.scopes if value else ()),
                "right_source_scopes": sorted(
                    right_value.scopes if right_value else ()
                ),
                "unresolved_scopes": sorted(
                    state.unknown_scopes
                    |
                    (value.scopes if value else frozenset())
                    | (right_value.scopes if right_value else frozenset())
                ),
                "unresolved_reasons": sorted(state.unknown_reasons),
                "status": status,
                "reason": reason,
            }
            self.dependencies.append(dependency)
            if status == "blocking":
                self.blocking.append(dependency)

    def _matching(self, start: int, end: int, opcode: int) -> int | None:
        indent = self.block.commands[start].indent
        for index in range(start + 1, end):
            command = self.block.commands[index]
            if command.opcode == opcode and command.indent == indent:
                return index
        return None

    def _branches(
        self,
        start: int,
        end: int,
        state: _AnalysisState,
        exits: list[_AnalysisState] | None = None,
        truth: bool | None = None,
    ) -> tuple[_AnalysisState | None, int] | None:
        closing = self._matching(start, end, 499)
        if closing is None:
            return None
        indent = self.block.commands[start].indent
        all_markers = [
            index for index in range(start + 1, closing)
            if self.block.commands[index].indent == indent
            and self.block.commands[index].opcode in {401, 420, 421}
        ]
        if not all_markers:
            return state, closing + 1
        if truth is True:
            markers = all_markers[:1]
        elif truth is False:
            markers = [
                marker for marker in all_markers
                if self.block.commands[marker].opcode in {420, 421}
            ]
            if not markers:
                return state, closing + 1
        else:
            markers = all_markers
        branch_states: list[_AnalysisState] = []
        for marker in markers:
            position = all_markers.index(marker)
            branch_end = (
                all_markers[position + 1]
                if position + 1 < len(all_markers)
                else closing
            )
            branch_state = state.copy()
            if self._execute(marker + 1, branch_end, branch_state, exits):
                branch_states.append(branch_state)
        if truth is None and not any(
            self.block.commands[index].opcode in {420, 421} for index in markers
        ):
            branch_states.append(state.copy())
        return (_merge_states(branch_states) if branch_states else None), closing + 1

    @staticmethod
    def _numeric_condition_truth(
        command: _Command, state: _AnalysisState
    ) -> bool | None:
        # Editor 3.713 pretty output confirms flag 2 means numeric equality.
        if len(command.ints) != 4 or command.ints[0] != 1 or command.ints[3] != 2:
            return None
        left = _number_argument(command.ints[1], state)
        right = _number_argument(command.ints[2], state)
        if left.values is None or right.values is None:
            return None
        if left.values.isdisjoint(right.values):
            return False
        if len(left.values) == len(right.values) == 1:
            return True
        return None

    def _taint_unknown(
        self,
        command: _Command,
        index: int,
        state: _AnalysisState,
        *,
        strings: bool = True,
    ) -> None:
        affected = {value & 0x00FFFFFF for value in command.ints if value >= 1_000_000}
        if strings:
            for variable in affected & set(state.strings):
                current = state.strings[variable]
                state.strings[variable] = _StringValue(
                    current.source_keys,
                    current.cells,
                    current.trace + (self._location(index),),
                    f"来源经过未支持命令 opcode={command.opcode}",
                    current.symbolic_all,
                    current.scopes,
                    None,
                )
        if strings:
            scopes = frozenset({"project"})
            state.unknown_scopes = state.unknown_scopes | scopes
            state.unknown_reasons = state.unknown_reasons | frozenset({
                f"{self._location(index)} opcode={command.opcode} 参数形状不透明"
            })
            self._blocking_scope_dependency(
                command,
                index,
                "opaque",
                f"未校准或不透明命令 opcode={command.opcode}",
                scopes,
            )
        for variable in affected & set(state.numbers):
            current = state.numbers[variable]
            state.numbers[variable] = _NumberValue(
                None, f"数值经过未支持命令 opcode={command.opcode}", current.tracked
            )

    def _unknown_call(
        self,
        command: _Command,
        index: int,
        state: _AnalysisState,
        reason: str,
        scopes: frozenset[str] | None = None,
        *,
        status: str = "blocking",
        taint_state: bool = True,
    ) -> None:
        if scopes is None:
            scopes = frozenset({"project"})
        input_values = tuple(
            state.strings[raw & 0x00FFFFFF]
            for raw in command.ints[2:-1]
            if raw >= 1_000_000 and (raw & 0x00FFFFFF) in state.strings
        )
        literal_start = 1 if command.opcode == 300 else 0
        input_values += tuple(
            self._literal_string(command, index, string_index, state)
            for string_index in range(literal_start, len(command.strings))
        )
        self._blocking_scope_dependency(
            command, index, "call", reason, scopes, input_values, status=status
        )
        if taint_state:
            state.unknown_scopes = state.unknown_scopes | scopes
            state.unknown_reasons = state.unknown_reasons | frozenset({
                f"{self._location(index)} opcode={command.opcode}: {reason}"
            })
        if len(command.ints) < 2 or not command.ints[1] & 0x01000000:
            return
        destination = command.ints[-1] & 0x00FFFFFF
        value: _StringValue | None = None
        for raw in command.ints[2:-1]:
            source = state.strings.get(raw & 0x00FFFFFF) if raw >= 1_000_000 else None
            if source is not None:
                value = _merge_strings(value, source)
        value = value or _StringValue()
        state.strings[destination] = _StringValue(
            value.source_keys,
            value.cells,
            tuple(dict.fromkeys(value.trace + (self._location(index),))),
            (
                f"来源经过未解释的公共事件调用 opcode={command.opcode}: {reason}"
                if status == "blocking"
                else f"公共事件返回值为运行时动态值 opcode={command.opcode}: {reason}"
            ),
            "project" in scopes,
            value.scopes | scopes,
            None,
        )
        numeric_inputs = [
            state.numbers[raw & 0x00FFFFFF]
            for raw in command.ints[2:-1]
            if raw >= 1_000_000 and (raw & 0x00FFFFFF) in state.numbers
        ]
        state.numbers[destination] = _NumberValue(
            None,
            f"数值来自未解释的公共事件调用 opcode={command.opcode}",
            any(item.tracked for item in numeric_inputs),
        )

    def _call_target(
        self, command: _Command, state: _AnalysisState
    ) -> tuple[_CommandBlock, int | None] | None:
        if len(command.ints) < 2:
            return None
        if command.opcode == 300:
            if not command.strings:
                return None
            target_names = _expand_string_references(
                frozenset({command.strings[0]}), state
            )
            matches = {
                max(group, key=lambda block: block.event_id).event_id:
                max(group, key=lambda block: block.event_id)
                for name in (target_names or ())
                if (group := self.common_by_name.get(name, ()))
            }
            target = next(iter(matches.values())) if len(matches) == 1 else None
        else:
            reference = command.ints[0]
            if 599_000 <= reference < 601_000 and self.block.event_type == "common":
                target_id = self.block.event_id + reference - 600_100
            elif 500_000 <= reference < 600_000:
                target_id = reference - 500_000
            else:
                return None
            target = self.common_by_id.get(target_id)
        if target is None:
            return None
        choice_value = (
            _number_argument(command.ints[2], state)
            if len(command.ints) >= 3
            else _NumberValue(frozenset({0}))
        )
        choice = (
            next(iter(choice_value.values))
            if choice_value.values is not None and len(choice_value.values) == 1
            else None
        )
        return target, choice

    def _call_event(self, command: _Command, index: int, state: _AnalysisState) -> None:
        has_return = len(command.ints) >= 2 and bool(command.ints[1] & 0x01000000)
        resolved = self._call_target(command, state)
        if resolved is None:
            if command.opcode == 300 and command.strings:
                names = _expand_string_references(
                    frozenset({command.strings[0]}), state
                )
                if names is not None and not any(
                    self.common_by_name.get(name) for name in names
                ):
                    # The official manual specifies that an invalid name does
                    # nothing. Old projects commonly retain optional calls.
                    return
                value = self._literal_string(command, index, 0, state)
                self._blocking_scope_dependency(
                    command,
                    index,
                    "call",
                    "公共事件目标为运行时动态值，已保守保护全部公共事件范围",
                    frozenset(),
                    (value,),
                    status="dynamic",
                )
            elif command.opcode == 210 and command.ints and command.ints[0] < 0:
                return
            else:
                self._blocking_scope_dependency(
                    command,
                    index,
                    "call",
                    "公共事件目标为运行时动态值，已保守保护全部公共事件范围",
                    frozenset({"common:*"}),
                    status="dynamic",
                )
            self._set_unknown_target_return(command, state)
            return
        target, choice = resolved
        # Every common event is also analyzed independently. At a call site we
        # only need the callee's own text and actual argument provenance; DB and
        # global writes are covered by their value-boundary dependencies.
        target_scopes = frozenset({f"common:{target.event_id}"})
        target_has_dispatcher = any(
            command.indent == 0
            and following.indent == 0
            and command.opcode == 122
            and command.ints[:2] == (3_000_001, 0)
            and command.strings == ("cmd:\\cself[0]",)
            and following.opcode == 213
            and following.strings == ("\\s[1]",)
            for command, following in zip(target.commands, target.commands[1:])
        )
        target_has_entry_labels = any(
            item.opcode == 212
            and item.indent == 0
            and len(item.strings) == 1
            and item.strings[0].startswith("cmd:")
            for item in target.commands
        )
        if choice is None and (target_has_dispatcher or target_has_entry_labels):
            self._blocking_scope_dependency(
                command,
                index,
                "call",
                "调用入口为运行时动态值，已保守保护目标事件范围",
                target_scopes,
                status="dynamic",
            )
            self._set_dynamic_call_return(command, state, target, target_scopes)
            return
        call_key = (target.event_id, choice)
        if len(self.call_stack) >= _CALL_DEPTH_LIMIT:
            # ponytail: recursive value summaries widen to the precomputed call
            # closure; parameterized SCC summaries can recover more coverage.
            self._unknown_call(
                command,
                index,
                state,
                "递归调用摘要扩大为可达范围",
                self.event_scopes.get(target.event_id, target_scopes),
                status="dynamic",
                taint_state=False,
            )
            return
        if call_key in self.call_stack:
            self._unknown_call(
                command,
                index,
                state,
                "递归调用摘要扩大为可达范围",
                self.event_scopes.get(target.event_id, target_scopes),
                status="dynamic",
                taint_state=False,
            )
            return
        if has_return and target.return_target < 0:
            self._blocking_scope_dependency(
                command,
                index,
                "call",
                "调用声明返回值但目标事件没有返回槽，已按运行时动态值处理",
                target_scopes,
                status="dynamic",
            )
            self._set_dynamic_call_return(command, state, target, target_scopes)
            return

        flags = command.ints[1]
        numeric_slots = flags & 0x0F
        string_count = (flags >> 4) & 0x0F
        # ponytail: The call record is authoritative for this entry point. Common
        # events may expose fewer inputs than old call sites still carry. WOLF stores
        # zero-input calls as two integers and otherwise counts all numeric slots.
        string_start = 2 + numeric_slots
        string_end = string_start + string_count
        expected_ints = string_end + int(has_return)
        if len(command.ints) != expected_ints:
            self._unknown_call(command, index, state, "实参数量与 Auto 头部不符", target_scopes)
            return
        string_offset = 1
        string_arguments = command.ints[string_start:string_end]
        if any(raw < 1_000_000 for raw in string_arguments) and len(command.strings) < string_offset + string_count:
            self._unknown_call(command, index, state, "字符串实参数量与 Auto 头部不符", target_scopes)
            return

        callee_state = _AnalysisState({}, {}, dict(state.database_strings))
        for offset, raw in enumerate(command.ints[2:string_start]):
            callee_state.numbers[1_600_000 + offset] = _number_argument(raw, state)
        for offset, raw in enumerate(string_arguments):
            destination = 1_600_005 + offset
            if raw >= 1_000_000:
                callee_state.strings[destination] = state.strings.get(
                    raw & 0x00FFFFFF,
                    _StringValue(
                        unknown=f"字符串实参 {raw & 0x00FFFFFF} 来源未知",
                        literals=None,
                    ),
                )
            else:
                callee_state.strings[destination] = self._literal_string(
                    command,
                    index,
                    string_offset + offset,
                    state,
                )

        if not has_return:
            inputs = tuple(callee_state.strings.values())
            if any(value.tracked for value in inputs):
                self._blocking_scope_dependency(
                    command,
                    index,
                    "call",
                    "无返回公共事件副作用已按事件摘要保守定位",
                    frozenset(),
                    inputs,
                    status="dynamic",
                )
            return

        dispatcher = self._dynamic_entry_dispatcher(target)
        if dispatcher is not None:
            start, end = dispatcher, len(target.commands)
        else:
            entry_labels = [
                position
                for position, item in enumerate(target.commands)
                if item.opcode == 212
                and item.indent == 0
                and len(item.strings) == 1
                and item.strings[0].startswith("cmd:")
            ]
            if entry_labels:
                label = next(
                    (
                        position
                        for position in entry_labels
                        if target.commands[position].strings == (f"cmd:{choice}",)
                    ),
                    None,
                )
                if label is None:
                    self._unknown_call(
                        command, index, state, f"缺少 cmd:{choice} 标签", target_scopes
                    )
                    return
                start = label + 1
                end = next(
                    (position for position in entry_labels if position > label),
                    len(target.commands),
                )
            else:
                start, end = 0, len(target.commands)
        cache_key = (target.event_id, choice, start, end, _state_cache_key(callee_state))
        cached = self.call_cache.get(cache_key)
        if cached is None:
            child = _BlockAnalyzer(
                target,
                self.databases,
                self.database_keys,
                self.event_items,
                self.common_by_id,
                self.common_by_name,
                self.event_scopes,
                self.call_stack + (call_key,),
                self.call_cache,
            )
            exits: list[_AnalysisState] = []
            fell_through = child._execute(start, end, callee_state, exits)
            if fell_through:
                exits.append(callee_state.copy())
            cached = _CallSummary(
                fell_through,
                tuple(item.copy() for item in exits),
                child.summary_failed,
                tuple(child.dependencies),
                tuple(child.blocking),
                Counter(child.unknown),
                tuple(
                    (key, tuple(values))
                    for key, values in child.unknown_locations.items()
                ),
            )
            self.call_cache[cache_key] = cached
        self.dependencies.extend(cached.dependencies)
        self.blocking.extend(cached.blocking)
        self.unknown.update(cached.unknown)
        for key, values in cached.unknown_locations:
            self.unknown_locations.setdefault(key, []).extend(values)
        if cached.summary_failed:
            self._unknown_call(
                command,
                index,
                state,
                cached.summary_failed or "公共事件摘要没有在 END 返回",
                target_scopes,
            )
            return
        if not cached.exits:
            self._blocking_scope_dependency(
                command,
                index,
                "call",
                "目标事件控制流为运行时动态路径，已保守保护事件范围",
                target_scopes,
                status="dynamic",
            )
            self._set_dynamic_call_return(command, state, target, target_scopes)
            return

        result = _merge_states(list(cached.exits))
        state.database_strings = dict(result.database_strings)
        state.unknown_scopes = state.unknown_scopes | result.unknown_scopes
        state.unknown_reasons = state.unknown_reasons | result.unknown_reasons
        # Global variables are shared across public-event frames; CSelf slots are not.
        for variable, value in result.strings.items():
            if not 1_600_000 <= variable < 1_600_100:
                state.strings[variable] = value
        for variable, value in result.numbers.items():
            if not 1_600_000 <= variable < 1_600_100:
                state.numbers[variable] = value
        if not has_return:
            return
        return_variable = 1_600_000 + target.return_target
        destination = command.ints[-1] & 0x00FFFFFF
        call_trace = f"{self._location(index)} -> common={target.event_id} cmd={choice}"
        if target.return_target >= 5:
            value = result.strings.get(return_variable)
            if value is None:
                self._set_dynamic_call_return(command, state, target, target_scopes)
                return
            state.strings[destination] = _StringValue(
                value.source_keys,
                value.cells,
                tuple(dict.fromkeys(value.trace + (call_trace,))),
                value.unknown,
                value.symbolic_all,
                value.scopes,
                value.literals,
            )
        else:
            value = result.numbers.get(return_variable)
            if value is None:
                self._set_dynamic_call_return(command, state, target, target_scopes)
                return
            state.numbers[destination] = value

    @staticmethod
    def _set_unknown_target_return(
        command: _Command, state: _AnalysisState
    ) -> None:
        if len(command.ints) < 3 or not command.ints[1] & 0x01000000:
            return
        destination = command.ints[-1] & 0x00FFFFFF
        state.strings[destination] = _StringValue(
            unknown="公共事件目标为运行时动态值",
            scopes=frozenset({"common:*"}),
            literals=None,
        )
        state.numbers[destination] = _NumberValue(
            None, "公共事件目标为运行时动态值"
        )

    def _set_dynamic_call_return(
        self,
        command: _Command,
        state: _AnalysisState,
        target: _CommandBlock,
        scopes: frozenset[str],
    ) -> None:
        if len(command.ints) < 3 or not command.ints[1] & 0x01000000:
            return
        destination = command.ints[-1] & 0x00FFFFFF
        if target.return_target >= 5:
            state.strings[destination] = _StringValue(
                unknown="公共事件返回值为运行时动态值",
                scopes=scopes,
                literals=None,
            )
        else:
            state.numbers[destination] = _NumberValue(
                None, "公共事件返回值为运行时动态值"
            )

    def _reserve_event(
        self, command: _Command, index: int, state: _AnalysisState
    ) -> None:
        if not command.ints:
            self._unknown_call(
                command, index, state, "预约公共事件目标缺失", frozenset({"common:*"})
            )
            return
        reference = command.ints[0]
        target_id = reference - 500_000 if 500_000 <= reference < 600_000 else reference
        target = self.common_by_id.get(target_id)
        if target is None:
            self._unknown_call(
                command, index, state, "预约公共事件目标无法解析", frozenset({"common:*"})
            )
            return
        scopes = frozenset({f"common:{target.event_id}"})
        if scopes:
            self._blocking_scope_dependency(
                command,
                index,
                "call",
                "预约公共事件存在延迟的全局或数据库副作用",
                scopes,
                status="dynamic",
            )

    def _import_database(
        self, command: _Command, index: int, state: _AnalysisState
    ) -> None:
        selector = command.ints[0] if command.ints else -1
        database = {0: "CDB", 1: "SDB", 2: "UDB"}.get(selector)
        scopes = (
            frozenset({f"database:{database}:*:*:*"})
            if database
            else frozenset(
                {
                    "database:UDB:*:*:*",
                    "database:CDB:*:*:*",
                    "database:SDB:*:*:*",
                }
            )
        )
        self._blocking_scope_dependency(
            command,
            index,
            "database",
            "CSV 数据库操作可能在运行时改写数据库字符串",
            scopes,
        )

    def _transfer_command(
        self,
        index: int,
        state: _AnalysisState,
        exits: list[_AnalysisState] | None = None,
    ) -> bool:
        start = index
        end = index + 1
        jump_counts: Counter[int] = Counter()
        while index < end:
            command = self.block.commands[index]
            semantics = command_semantics(
                command.opcode, len(command.ints), len(command.strings)
            )
            if semantics is None:
                self._record_unknown(command, index)
                self._taint_unknown(command, index, state)
                index += 1
                continue
            self._display_reference(command, index, state, semantics)
            self._resource_reference(command, index, state, semantics)
            if command.opcode == 121:
                self._set_number(command, index, state)
            elif command.opcode == 122:
                self._set_string(command, index, state)
            elif command.opcode == 123:
                self._set_runtime_value(command, index, state, string_result=False)
            elif command.opcode == 124:
                # Editor 3.713 exposes both numeric and string-valued system queries.
                # A query overwrites prior provenance; runtime strings are not
                # WOLFLator-editable sources and therefore remain untracked.
                category = command.ints[1] if len(command.ints) > 1 else -1
                field = command.ints[3] if len(command.ints) > 3 else -1
                string_result = category == 4096 and field == 9
                self._set_runtime_value(command, index, state, string_result=string_result)
            elif command.opcode == 250:
                self._database(command, index, state)
            elif command.opcode == 251:
                self._import_database(command, index, state)
            elif command.opcode == 221:
                self._set_runtime_value(
                    command,
                    index,
                    state,
                    string_result=bool(command.ints and command.ints[-1] == 1),
                )
            elif command.opcode == 112:
                self._condition(command, index, state)
                branch = self._branches(index, end, state, exits)
                if branch:
                    merged, index = branch
                    if merged is None:
                        return False
                    state.numbers = merged.numbers
                    state.strings = merged.strings
                    state.database_strings = merged.database_strings
                    state.unknown_scopes = merged.unknown_scopes
                    state.unknown_reasons = merged.unknown_reasons
                    continue
            elif command.opcode == 111:
                branch = self._branches(
                    index,
                    end,
                    state,
                    exits,
                    truth=self._numeric_condition_truth(command, state),
                )
                if branch:
                    merged, index = branch
                    if merged is None:
                        return False
                    state.numbers = merged.numbers
                    state.strings = merged.strings
                    state.database_strings = merged.database_strings
                    state.unknown_scopes = merged.unknown_scopes
                    state.unknown_reasons = merged.unknown_reasons
                    continue
            elif command.opcode in {210, 300}:
                self._call_event(command, index, state)
            elif command.opcode == 211:
                self._reserve_event(command, index, state)
            elif command.opcode in {170, 179}:
                closing = self._matching(index, end, 498)
                if closing is None:
                    self._record_unknown(command, index, "loop-without-end")
                else:
                    count_value = (
                        _number_argument(command.ints[0], state)
                        if command.opcode == 179 and command.ints
                        else _NumberValue(None, "无限循环")
                    )
                    iterations = (
                        next(iter(count_value.values))
                        if count_value.values is not None and len(count_value.values) == 1
                        else _LOOP_LIMIT + 1
                    )
                    before = state.copy()
                    stable = False
                    previous = before
                    for _ in range(min(max(iterations, 0), _LOOP_LIMIT)):
                        previous = state.copy()
                        try:
                            if not self._execute(index + 1, closing, state, exits):
                                return False
                        except _BreakLoop:
                            stable = True
                            break
                        except _ContinueLoop:
                            pass
                        if state == previous:
                            stable = True
                            break
                    if iterations > _LOOP_LIMIT or command.opcode == 170:
                        states = [before, state]
                        if not stable:
                            widened = state.copy()
                            for variable in set(previous.numbers) | set(state.numbers):
                                if previous.numbers.get(variable) != state.numbers.get(variable):
                                    current = state.numbers.get(variable)
                                    widened.numbers[variable] = _NumberValue(
                                        None,
                                        "循环数值未稳定",
                                        current.tracked if current else False,
                                    )
                            widened_before = widened.copy()
                            try:
                                if not self._execute(index + 1, closing, widened, exits):
                                    return False
                            except _BreakLoop:
                                stable = True
                            except _ContinueLoop:
                                pass
                            stable = widened == widened_before
                            states.append(widened)
                            previous = widened_before
                            state = widened
                        merged = _merge_states(states)
                        for variable in set(before.strings) | set(state.strings):
                            if not stable and previous.strings.get(variable) != state.strings.get(variable):
                                value = merged.strings.get(variable) or _StringValue()
                                if value.symbolic_all:
                                    continue
                                merged.strings[variable] = _StringValue(
                                    value.source_keys, value.cells, value.trace,
                                    "循环扩大后字符串仍未稳定", True,
                                    value.scopes,
                                    None,
                                )
                        state.numbers = merged.numbers
                        state.strings = merged.strings
                        state.database_strings = merged.database_strings
                        state.unknown_scopes = merged.unknown_scopes
                        state.unknown_reasons = merged.unknown_reasons
                    index = closing + 1
                    continue
            elif command.opcode == 171:
                raise _BreakLoop
            elif command.opcode == 176:
                raise _ContinueLoop
            elif command.opcode == 172:
                if exits is not None:
                    exits.append(state.copy())
                return False
            elif command.opcode in {173, 174, 175}:
                return False
            elif command.opcode == 213:
                if command.strings == ("END",):
                    if exits is not None:
                        exits.append(state.copy())
                    return False
                target_names = (
                    _expand_string_references(
                        frozenset({command.strings[0]}), state
                    )
                    if len(command.strings) == 1
                    else None
                )
                target_name = (
                    next(iter(target_names))
                    if target_names is not None and len(target_names) == 1
                    else command.strings[0] if len(command.strings) == 1 else ""
                )
                targets = tuple(sorted({
                    position
                    for name in (target_names or ())
                    for position in self.labels.get(name, ())
                }))
                if len(targets) == 1:
                    target = targets[0]
                    jump_counts[target] += 1
                    if jump_counts[target] <= _LOOP_LIMIT:
                        end = len(self.block.commands)
                        index = target + 1
                        continue
                    scopes = self._current_scope()
                    state.unknown_scopes = state.unknown_scopes | scopes
                    state.unknown_reasons = state.unknown_reasons | frozenset({
                        f"{self._location(index)}: 标签跳转 {target_name!r} 未收敛"
                    })
                    self.summary_failed = f"标签跳转 {target_name!r} 超过 64 次仍未收敛"
                    self._blocking_scope_dependency(
                        command,
                        index,
                        "control_flow",
                        self.summary_failed,
                        scopes,
                    )
                    return False
                scopes = self._current_scope()
                self._blocking_scope_dependency(
                    command,
                    index,
                    "control_flow",
                    f"标签目标为运行时动态值，已保守保护当前事件范围 {target_name!r}",
                    scopes,
                    status="dynamic",
                )
                return False
            elif command.opcode not in {0, 401, 420, 421, 498, 499}:
                effect = str(semantics["effect"]) if semantics else None
                if effect in {"no_write", "control_flow"}:
                    pass
                elif effect == "numeric_write":
                    self._set_runtime_value(command, index, state, string_result=False)
                elif effect == "event_call":
                    self._unknown_call(
                        command,
                        index,
                        state,
                        "未内联的公共事件调用",
                        frozenset(
                            {
                                "common:*",
                                "database:UDB:*:*:*",
                                "database:CDB:*:*:*",
                                "database:SDB:*:*:*",
                            }
                        ),
                    )
                else:
                    if any(command.strings) or any(
                        value >= 1_000_000 for value in command.ints
                    ):
                        self._record_unknown(command, index)
                    self._taint_unknown(command, index, state)
            index += 1
        return True

    def _cfg_failure(
        self,
        command: _Command,
        index: int,
        state: _AnalysisState,
        reason: str,
    ) -> None:
        scopes = self._current_scope()
        state.unknown_scopes = state.unknown_scopes | scopes
        state.unknown_reasons = state.unknown_reasons | frozenset(
            {f"{self._location(index)}: {reason}"}
        )
        self.summary_failed = reason
        self._blocking_scope_dependency(
            command, index, "control_flow", reason, scopes
        )

    @staticmethod
    def _bounded_successor(target: int, limit: int) -> tuple[int | None, int]:
        return (target, limit) if target < limit else (None, limit)

    def _widen_back_edge(
        self,
        previous: _AnalysisState,
        current: _AnalysisState,
    ) -> _AnalysisState:
        merged = _merge_states([previous, current])
        for variable in set(previous.numbers) | set(current.numbers):
            left = previous.numbers.get(variable)
            right = current.numbers.get(variable)
            if left != right:
                value = merged.numbers.get(variable)
                merged.numbers[variable] = _NumberValue(
                    None,
                    "控制流回边扩大为运行时数值",
                    bool(value and value.tracked),
                )
        scope = self._current_scope()
        for variable in set(previous.strings) | set(current.strings):
            left = previous.strings.get(variable)
            right = current.strings.get(variable)
            if left is not None and right is not None:
                unchanged = _string_semantic_key(left) == _string_semantic_key(right)
            else:
                unchanged = left is right
            if unchanged:
                continue
            value = merged.strings.get(variable) or _StringValue()
            database_scopes = frozenset(
                f"database:{database}:{type_id}:*:{field_id}"
                for database, type_id, _data_id, field_id in value.cells
            )
            merged.strings[variable] = _StringValue(
                trace=value.trace,
                unknown="控制流回边扩大为运行时字符串",
                symbolic_all=True,
                scopes=value.scopes | database_scopes | scope,
                literals=None,
            )
        previous_coordinates = set(previous.database_strings)
        current_coordinates = set(current.database_strings)
        growing_databases = {
            coordinate[0]
            for coordinate in previous_coordinates ^ current_coordinates
        }
        if growing_databases:
            merged.database_strings = {
                coordinate: value
                for coordinate, value in merged.database_strings.items()
                if coordinate[0] not in growing_databases
            }
            merged.unknown_scopes = merged.unknown_scopes | scope | frozenset(
                f"database:{database}:*:*:*" for database in growing_databases
            )
        for coordinate in set(previous.database_strings) | set(current.database_strings):
            if coordinate[0] in growing_databases:
                continue
            left = previous.database_strings.get(coordinate)
            right = current.database_strings.get(coordinate)
            if left is not None and right is not None:
                unchanged = _string_semantic_key(left) == _string_semantic_key(right)
            else:
                unchanged = left is right
            if unchanged:
                continue
            value = merged.database_strings.get(coordinate) or _StringValue()
            database, type_id, data_id, field_id = coordinate
            merged.database_strings[coordinate] = _StringValue(
                trace=value.trace,
                unknown="控制流回边扩大为运行时数据库字符串",
                symbolic_all=True,
                scopes=value.scopes
                | frozenset(
                    {f"database:{database}:{type_id}:{data_id}:{field_id}"}
                ),
                literals=None,
            )
        return merged

    def _cfg_successors(
        self,
        index: int,
        limit: int,
        state: _AnalysisState,
        exits: list[_AnalysisState] | None,
    ) -> tuple[tuple[int | None, int], ...]:
        command = self.block.commands[index]
        if command.opcode in {111, 112}:
            region = self._condition_regions.get(index)
            if region is None:
                return (self._bounded_successor(index + 1, limit),)
            closing, branches = region
            if not branches:
                return (self._bounded_successor(closing + 1, limit),)
            truth = (
                self._numeric_condition_truth(command, state)
                if command.opcode == 111
                else None
            )
            if truth is True:
                selected = branches[:1]
            elif truth is False:
                selected = tuple(
                    branch
                    for branch in branches
                    if self.block.commands[branch[0]].opcode in {420, 421}
                )
            else:
                selected = branches
            targets = [
                branch_start + 1 if branch_start + 1 < branch_end else closing + 1
                for branch_start, branch_end in selected
            ]
            has_else = any(
                self.block.commands[branch_start].opcode in {420, 421}
                for branch_start, _branch_end in branches
            )
            if (truth is False and not selected) or (truth is None and not has_else):
                targets.append(closing + 1)
            return tuple(
                dict.fromkeys(self._bounded_successor(target, limit) for target in targets)
            )

        if command.opcode in {170, 179}:
            closing = self._loop_ends.get(index)
            if closing is None:
                self._cfg_failure(command, index, state, "循环缺少配对的循环结束")
                return ()
            body = self._bounded_successor(index + 1, limit)
            if command.opcode == 170:
                return (body,)
            count = (
                _number_argument(command.ints[0], state)
                if command.ints
                else _NumberValue(None, "循环次数缺失")
            )
            after = self._bounded_successor(closing + 1, limit)
            if count.values is not None and all(value <= 0 for value in count.values):
                return (after,)
            return tuple(dict.fromkeys((body, after)))

        if command.opcode == 498:
            start = self._loop_starts.get(index)
            if start is None:
                self._cfg_failure(command, index, state, "循环结束缺少配对的循环入口")
                return ()
            return (self._bounded_successor(start, limit),)

        if command.opcode in {171, 176}:
            loop = self._enclosing_loops.get(index)
            if loop is None:
                self._cfg_failure(command, index, state, "循环控制命令不在循环结构内")
                return ()
            start, closing = loop
            target = closing + 1 if command.opcode == 171 else start
            return (self._bounded_successor(target, limit),)

        if command.opcode == 172:
            if exits is not None:
                exits.append(state.copy())
            return ()
        if command.opcode in {173, 174, 175}:
            return ()

        if command.opcode == 213:
            if command.strings == ("END",):
                if exits is not None:
                    exits.append(state.copy())
                return ()
            target_names = (
                _expand_string_references(frozenset({command.strings[0]}), state)
                if len(command.strings) == 1
                else None
            )
            targets = tuple(sorted({
                position
                for name in (target_names or ())
                for position in self.labels.get(name, ())
            }))
            if len(targets) == 1:
                target = targets[0] + 1
                return ((target, len(self.block.commands)),) if target < len(self.block.commands) else ((None, len(self.block.commands)),)
            target_name = command.strings[0] if len(command.strings) == 1 else ""
            self._blocking_scope_dependency(
                command,
                index,
                "control_flow",
                f"标签目标为运行时动态值，已保守保护当前事件范围 {target_name!r}",
                self._current_scope(),
                status="dynamic",
            )
            return ()

        target = self._branch_exits.get(index, index + 1)
        return (self._bounded_successor(target, limit),)

    def _execute(
        self,
        start: int,
        end: int,
        state: _AnalysisState,
        exits: list[_AnalysisState] | None = None,
    ) -> bool:
        if start >= len(self.block.commands):
            return True
        initial_limit = min(max(end, start + 1), len(self.block.commands))
        states: dict[tuple[int, int], _AnalysisState] = {
            (start, initial_limit): state.copy()
        }
        pending: deque[tuple[int, int]] = deque(((start, initial_limit),))
        visits: Counter[tuple[int, int]] = Counter()
        fallthrough: list[_AnalysisState] = []
        structural = {170, 171, 172, 173, 174, 175, 176, 179, 213}

        while pending:
            key = pending.popleft()
            index, limit = key
            current = states[key].copy()
            visits[key] += 1
            if visits[key] > _CFG_STATE_VISIT_LIMIT:
                self._cfg_failure(
                    self.block.commands[index],
                    index,
                    current,
                    f"控制流固定点超过 {_CFG_STATE_VISIT_LIMIT} 次仍未收敛",
                )
                continue

            command = self.block.commands[index]
            if command.opcode not in structural:
                self._transfer_command(index, current, exits)
            successors = self._cfg_successors(index, limit, current, exits)
            for successor, successor_limit in successors:
                if successor is None:
                    fallthrough.append(current.copy())
                    continue
                successor_key = (successor, successor_limit)
                previous = states.get(successor_key)
                merged = (
                    current.copy()
                    if previous is None
                    else self._widen_back_edge(previous, current)
                    if successor <= index
                    else _merge_states([previous, current])
                )
                if previous is not None:
                    if _states_semantically_equal(merged, previous):
                        states[successor_key] = merged
                        continue
                states[successor_key] = merged
                pending.append(successor_key)

        if not fallthrough:
            return False
        result = _merge_states(fallthrough)
        state.numbers = result.numbers
        state.strings = result.strings
        state.database_strings = result.database_strings
        state.unknown_scopes = result.unknown_scopes
        state.unknown_reasons = result.unknown_reasons
        return True

    def run(self) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
        entry_labels = [
            (index, int(command.strings[0].removeprefix("cmd:")))
            for index, command in enumerate(self.block.commands)
            if self.block.event_type == "common"
            and command.opcode == 212
            and command.indent == 0
            and len(command.strings) == 1
            and command.strings[0].startswith("cmd:")
            and command.strings[0].removeprefix("cmd:").isdigit()
        ]
        if entry_labels:
            first = entry_labels[0][0]
            dispatcher = self._dynamic_entry_dispatcher()
            if first and dispatcher is None:
                self._execute(0, first, _AnalysisState({}, {}, {}))
            for label_index, (start, choice) in enumerate(entry_labels):
                end = (
                    entry_labels[label_index + 1][0]
                    if label_index + 1 < len(entry_labels)
                    else len(self.block.commands)
                )
                state = _AnalysisState(
                    {1_600_000: _NumberValue(frozenset({choice}))}, {}, {}
                )
                if dispatcher is not None:
                    self._execute(dispatcher, len(self.block.commands), state)
                else:
                    self._execute(start + 1, end, state)
        else:
            self._execute(0, len(self.block.commands), _AnalysisState({}, {}, {}))
        warnings = [
            {"opcode": opcode, "shape": shape, "count": count,
             "locations": self.unknown_locations[(opcode, shape)][:5]}
            for (opcode, shape), count in sorted(self.unknown.items())
        ]
        return self.dependencies, self.blocking, warnings


def _analyze_blocks(
    blocks: Iterable[_CommandBlock],
    items: list[TranslationItem],
    databases: dict[str, dict[int, _DatabaseType]],
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
    blocks = list(blocks)
    database_keys: dict[tuple[str, int, int, int], set[str]] = {}
    event_items: dict[str, list[TranslationItem]] = {}
    for item in items:
        match = _WORKBOOK_DB_CODE_RE.fullmatch(item.code)
        if match:
            coordinate = (
                match.group("database").upper(),
                int(match.group("type")),
                int(match.group("data")),
                int(match.group("field")),
            )
            database_keys.setdefault(coordinate, set()).add(item.key)
        event_items.setdefault(item.code.upper(), []).append(item)
    frozen_database_keys = {key: frozenset(value) for key, value in database_keys.items()}
    frozen_event_items = {key: tuple(value) for key, value in event_items.items()}
    common_groups: dict[int, list[_CommandBlock]] = {}
    common_names: dict[str, list[_CommandBlock]] = {}
    for block in blocks:
        if block.event_type != "common":
            continue
        common_groups.setdefault(block.event_id, []).append(block)
        common_names.setdefault(block.event_name, []).append(block)
    common_by_id = {
        event_id: group[0]
        for event_id, group in common_groups.items()
        if len(group) == 1
    }
    common_by_name = {name: tuple(group) for name, group in common_names.items()}
    event_scopes = _conservative_event_scopes(blocks, common_by_id, common_by_name)
    dependencies: list[dict[str, object]] = []
    unknown = Counter()
    locations: dict[tuple[int, str], list[str]] = {}
    call_cache: _CallCache = {}
    for block in blocks:
        analyzer = _BlockAnalyzer(
            block,
            databases,
            frozen_database_keys,
            frozen_event_items,
            common_by_id,
            common_by_name,
            event_scopes,
            call_cache=call_cache,
        )
        block_dependencies, _block_blocking, block_unknown = analyzer.run()
        dependencies.extend(block_dependencies)
        for warning in block_unknown:
            key = (int(warning["opcode"]), str(warning["shape"]))
            unknown[key] += int(warning["count"])
            locations.setdefault(key, []).extend(str(value) for value in warning["locations"])
    warnings = [
        {"opcode": opcode, "shape": shape, "count": count, "locations": locations[(opcode, shape)][:5]}
        for (opcode, shape), count in sorted(unknown.items())
    ]
    merged_dependencies: dict[tuple[object, ...], dict[str, object]] = {}
    for dependency in dependencies:
        identity = (
            dependency["auto_file"], dependency["event_type"], dependency["event_id"],
            dependency["page"], dependency["command"], dependency["string_index"],
        )
        current = merged_dependencies.get(identity)
        if current is None:
            current = dict(dependency)
            current["_condition_keys"] = set(dependency["condition_keys"])
            current["_source_keys"] = set(dependency["source_keys"])
            current["_right_source_keys"] = set(
                dependency.get("right_source_keys", [])
            )
            current["_database_cells"] = {
                (cell["database"], cell["type"], cell["data"], cell["field"])
                for cell in dependency["database_cells"]
            }
            current["_right_database_cells"] = {
                (cell["database"], cell["type"], cell["data"], cell["field"])
                for cell in dependency.get("right_database_cells", [])
            }
            current["_trace"] = dict.fromkeys(dependency["trace"])
            current["_left_values"] = set(dependency.get("left_values", []))
            current["_right_values"] = set(dependency.get("right_values", []))
            current["_source_scopes"] = set(dependency.get("source_scopes", []))
            current["_right_source_scopes"] = set(
                dependency.get("right_source_scopes", [])
            )
            current["_unresolved_scopes"] = set(
                dependency.get("unresolved_scopes", [])
            )
            current["_unresolved_reasons"] = dict.fromkeys(
                dependency.get("unresolved_reasons", [])
            )
            merged_dependencies[identity] = current
            continue
        current["_condition_keys"].update(dependency["condition_keys"])
        current["_source_keys"].update(dependency["source_keys"])
        current["_right_source_keys"].update(dependency.get("right_source_keys", []))
        current["_database_cells"].update(
            (cell["database"], cell["type"], cell["data"], cell["field"])
            for cell in dependency["database_cells"]
        )
        current["_right_database_cells"].update(
            (cell["database"], cell["type"], cell["data"], cell["field"])
            for cell in dependency.get("right_database_cells", [])
        )
        current["_trace"].update(dict.fromkeys(dependency["trace"]))
        current["_left_values"].update(dependency.get("left_values", []))
        current["_right_values"].update(dependency.get("right_values", []))
        current["_source_scopes"].update(dependency.get("source_scopes", []))
        current["_right_source_scopes"].update(
            dependency.get("right_source_scopes", [])
        )
        current["_unresolved_scopes"].update(
            dependency.get("unresolved_scopes", [])
        )
        current["_unresolved_reasons"].update(
            dict.fromkeys(dependency.get("unresolved_reasons", []))
        )
        rank = {"resolved": 0, "untracked": 1, "dynamic": 2, "blocking": 3}
        if rank.get(str(dependency["status"]), 3) > rank.get(str(current["status"]), 3):
            current["status"] = dependency["status"]
            current["reason"] = dependency["reason"]
    dependencies = []
    for current in merged_dependencies.values():
        current["condition_keys"] = sorted(current.pop("_condition_keys"))
        current["source_keys"] = sorted(current.pop("_source_keys"))
        current["right_source_keys"] = sorted(current.pop("_right_source_keys"))
        for field in ("database_cells", "right_database_cells"):
            current[field] = [
                {"database": cell[0], "type": cell[1], "data": cell[2], "field": cell[3]}
                for cell in sorted(current.pop(f"_{field}"))
            ]
        current["trace"] = list(current.pop("_trace"))[:_VALUE_LIMIT]
        for field in ("left_values", "right_values"):
            current[field] = sorted(current.pop(f"_{field}"))[:_VALUE_LIMIT]
        for field in ("source_scopes", "right_source_scopes", "unresolved_scopes"):
            current[field] = sorted(current.pop(f"_{field}"))
        current["unresolved_reasons"] = list(
            current.pop("_unresolved_reasons")
        )[:_VALUE_LIMIT]
        dependencies.append(current)
    blocking = [item for item in dependencies if item["status"] == "blocking"]
    return dependencies, blocking, warnings


def _translation_usage_report(
    blocks: Iterable[_CommandBlock],
    items: list[TranslationItem],
    dependencies: list[dict[str, object]],
) -> tuple[dict[str, list[str]], list[str]]:
    by_code: dict[str, list[TranslationItem]] = {}
    for item in items:
        by_code.setdefault(item.code.upper(), []).append(item)
    usages: dict[str, set[str]] = {}
    scope_cache: dict[str, frozenset[str]] = {}
    for block in blocks:
        for index, command in enumerate(block.commands, start=1):
            semantics = command_semantics(
                command.opcode, len(command.ints), len(command.strings)
            )
            roles = _command_string_roles(command, semantics)
            for string_index, text in enumerate(command.strings):
                code = _event_code(block, index, string_index).upper()
                role = roles[string_index] if string_index < len(roles) else "unresolved"
                if role in {
                    "assignment_literal",
                    "call_argument",
                    "database_selector_or_value",
                }:
                    continue
                usage = "display_only" if role == "display_text" else (
                    "display_only" if role == "comment" else (
                    "logic" if role == "condition_literal" else (
                        "event_target" if role == "common_event_name" else (
                            "resource" if role in {"resource_path", "file_path"} else (
                                "control_flow" if role in {"label", "label_target"} else "unresolved"
                            )
                        )
                    )
                    )
                )
                for item in by_code.get(code, ()):
                    if item.original == text:
                        usages.setdefault(item.key, set()).add(usage)
    for dependency in dependencies:
        kind = str(dependency.get("kind", "condition"))
        usage = {
            "display": "display_only",
            "condition": "logic",
            "call": "event_target",
            "resource": "resource",
            "database": "database_selector",
            "control_flow": "control_flow",
            "opaque": "unresolved",
            "flow": "flow",
            "state": "logic",
        }.get(kind, "unresolved")
        if usage == "flow":
            continue
        for field in ("condition_keys", "source_keys", "right_source_keys"):
            for key in dependency.get(field, []):
                usages.setdefault(str(key), set()).add(usage)
        if dependency.get("status") != "resolved":
            database_scopes = tuple(
                scope
                for scope in map(str, dependency.get("unresolved_scopes", ()))
                if scope.startswith("database:")
            )
            for key in _scope_keys(items, database_scopes, scope_cache):
                usages.setdefault(key, set()).add(usage)
    proven_display = sorted(
        key for key, values in usages.items() if values == {"display_only"}
    )
    return ({key: sorted(values) for key, values in sorted(usages.items())}, proven_display)


def _command_transfer_complete(command: _Command) -> bool:
    semantics = command_semantics(
        command.opcode, len(command.ints), len(command.strings)
    )
    if not semantics or semantics.get("semantic_complete") is not True:
        return False
    if command.opcode == 121:
        return len(command.ints) in {4, 5}
    if command.opcode == 122:
        if len(command.ints) < 2:
            return False
        flags = command.ints[1]
        source_kind = flags & 0x0F
        assignment = (flags >> 8) & 0x0F
        return source_kind in {0, 1, 2, 3} and assignment in set(range(12))
    if command.opcode == 250:
        if len(command.ints) not in {4, 5}:
            return False
        return ((command.ints[3] >> 8) & 0x0F) in {0, 1, 2}
    return semantics.get("transfer") != "opaque"


def _conservative_event_scopes(
    blocks: list[_CommandBlock],
    common_by_id: dict[int, _CommandBlock],
    common_by_name: dict[str, tuple[_CommandBlock, ...]],
) -> dict[int, frozenset[str]]:
    direct: dict[int, set[str]] = {}
    calls: dict[int, set[int]] = {}
    database_scopes = {"database:UDB:*:*:*", "database:CDB:*:*:*", "database:SDB:*:*:*"}
    for event_id, block in common_by_id.items():
        scopes: set[str] = set()
        targets: set[int] = set()
        for command in block.commands:
            semantics = command_semantics(command.opcode, len(command.ints), len(command.strings))
            if semantics is None:
                scopes.add("project")
            if command.opcode == 122 and command.ints:
                destination = command.ints[0] & 0x00FFFFFF
                if not 1_600_000 <= destination < 1_600_100:
                    scopes.add(f"common:{event_id}")
            if command.opcode == 250 and len(command.ints) >= 4:
                byte1 = (command.ints[3] >> 8) & 0xFF
                database = {0: "CDB", 1: "SDB", 2: "UDB"}.get(byte1 & 0x0F)
                if database and byte1 & 0xF0 != 0x10:
                    scopes.add(f"database:{database}:*:*:*")
            if command.opcode not in {210, 211, 300} or not command.ints:
                continue
            target: _CommandBlock | None = None
            if command.opcode == 300 and command.strings:
                matches = common_by_name.get(command.strings[0], ())
                target = matches[0] if len(matches) == 1 else None
            elif command.opcode == 211:
                reference = command.ints[0]
                target_id = reference - 500_000 if 500_000 <= reference < 600_000 else reference
                target = common_by_id.get(target_id)
            elif command.opcode == 210:
                if len(command.ints) < 3:
                    scopes.update(database_scopes | {"common:*"})
                    continue
                reference = command.ints[0]
                if 599_000 <= reference < 601_000:
                    target_id = event_id + reference - 600_100
                elif 500_000 <= reference < 600_000:
                    target_id = reference - 500_000
                else:
                    target_id = -1
                target = common_by_id.get(target_id)
            if target is None:
                scopes.update(database_scopes | {"common:*"})
            else:
                targets.add(target.event_id)
        direct[event_id] = scopes
        calls[event_id] = targets

    summaries: dict[int, set[str]] = {}
    # ponytail: Effects are monotone set unions, so graph reachability is the
    # exact SCC fixed point and avoids repeatedly interpreting recursive bodies.
    for event_id in direct:
        pending = [event_id]
        visited: set[int] = set()
        merged: set[str] = set()
        while pending:
            current = pending.pop()
            if current in visited:
                continue
            visited.add(current)
            merged.update(direct.get(current, set()))
            pending.extend(calls.get(current, ()))
            if len(merged) > _VALUE_LIMIT:
                if "project" in merged:
                    merged = {"project"}
                else:
                    database = {
                        scope for scope in merged if scope.startswith("database:")
                    }
                    merged = database | (
                        {"common:*"}
                        if any(scope.startswith("common:") for scope in merged)
                        else set()
                    )
                break
        summaries[event_id] = merged
    return {event_id: frozenset(scopes) for event_id, scopes in summaries.items()}


def _event_node(block: _CommandBlock) -> str:
    return f"{block.event_type}:{block.source}:{block.event_id}:{block.page}"


def _call_graph_report(blocks: list[_CommandBlock]) -> tuple[dict[str, object], list[dict[str, object]]]:
    common_by_id = {
        block.event_id: block for block in blocks if block.event_type == "common"
    }
    common_by_name: dict[str, list[_CommandBlock]] = {}
    for block in common_by_id.values():
        common_by_name.setdefault(block.event_name, []).append(block)
    conservative_scopes = _conservative_event_scopes(
        blocks,
        common_by_id,
        {name: tuple(group) for name, group in common_by_name.items()},
    )
    edges: list[dict[str, object]] = []
    summaries: list[dict[str, object]] = []
    adjacency: dict[str, set[str]] = {_event_node(block): set() for block in common_by_id.values()}
    calibrated = 0
    command_total = 0
    for block in blocks:
        calls: list[dict[str, object]] = []
        reads = writes = opaque = 0
        for index, command in enumerate(block.commands, start=1):
            command_total += 1
            semantics = command_semantics(command.opcode, len(command.ints), len(command.strings))
            if semantics:
                calibrated += 1
                reads += int(bool(semantics["reads_variables"]))
                writes += int(bool(semantics["writes_variables"]))
            else:
                opaque += 1
            if command.opcode not in {210, 211, 300}:
                continue
            targets: list[_CommandBlock] = []
            if command.opcode == 300 and command.strings:
                targets = common_by_name.get(command.strings[0], [])
            elif command.opcode == 211 and command.ints:
                reference = command.ints[0]
                target_id = reference - 500_000 if 500_000 <= reference < 600_000 else reference
                if target_id in common_by_id:
                    targets = [common_by_id[target_id]]
            elif command.ints:
                reference = command.ints[0]
                target_id = None
                if 599_000 <= reference < 601_000 and block.event_type == "common":
                    target_id = block.event_id + reference - 600_100
                elif 500_000 <= reference < 600_000:
                    target_id = reference - 500_000
                if target_id in common_by_id:
                    targets = [common_by_id[target_id]]
            edge = {
                "source": _event_node(block),
                "command": index,
                "opcode": command.opcode,
                "targets": [_event_node(target) for target in targets],
                "dynamic": len(targets) != 1,
            }
            calls.append(edge)
            edges.append(edge)
            if block.event_type == "common":
                adjacency.setdefault(_event_node(block), set()).update(edge["targets"])
        summaries.append(
            {
                "event": _event_node(block),
                "event_name": block.event_name,
                "commands": len(block.commands),
                "variable_reads": reads,
                "variable_writes": writes,
                "opaque_commands": opaque,
                "calls": calls,
                "conservative_scopes": sorted(
                    conservative_scopes.get(block.event_id, frozenset())
                    if block.event_type == "common"
                    else frozenset()
                ),
            }
        )

    # Tarjan is small and deterministic; it exposes recursion without interpreting WOLF runtime.
    index = 0
    stack: list[str] = []
    indices: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    components: list[list[str]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = low[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for target in sorted(adjacency.get(node, ())):
            if target not in indices:
                visit(target)
                low[node] = min(low[node], low[target])
            elif target in on_stack:
                low[node] = min(low[node], indices[target])
        if low[node] == indices[node]:
            component: list[str] = []
            while True:
                target = stack.pop()
                on_stack.remove(target)
                component.append(target)
                if target == node:
                    break
            components.append(sorted(component))

    for node in sorted(adjacency):
        if node not in indices:
            visit(node)
    recursive = [
        component for component in components
        if len(component) > 1 or any(node in adjacency.get(node, ()) for node in component)
    ]
    return (
        {
            "nodes": len(adjacency),
            "edges": edges,
            "dynamic_edges": sum(bool(edge["dynamic"]) for edge in edges),
            "recursive_sccs": recursive,
            "coverage": {
                "commands": command_total,
                "calibrated": calibrated,
                "ratio": (calibrated / command_total) if command_total else 1.0,
            },
        },
        summaries,
    )


def analyze_auto_export(
    auto_dir: str | Path,
    items: list[TranslationItem],
    editor: EditorInfo,
    *,
    input_hash: str,
) -> dict[str, object]:
    root = Path(auto_dir).resolve()
    common = root / "BasicData" / "CommonEvent.dat.Auto.txt"
    if not common.is_file():
        raise ValueError("Editor 未生成 BasicData/CommonEvent.dat.Auto.txt。")
    blocks, common_counts = _event_blocks(
        common, "common", source=common.relative_to(root).as_posix()
    )
    map_counts = {"maps": 0, "events": 0, "pages": 0, "commands": 0}
    for map_path in sorted((root / "MapData").rglob("*.mps.Auto.txt")):
        map_blocks, counts = _event_blocks(
            map_path, "map", source=map_path.relative_to(root).as_posix()
        )
        blocks.extend(map_blocks)
        map_counts["maps"] += 1
        for key in ("events", "pages", "commands"):
            map_counts[key] += counts[key]

    database_counts: dict[str, dict[str, int]] = {}
    database_types: dict[str, dict[int, _DatabaseType]] = {}
    database_report: dict[str, object] = {}
    for name, code in (("DataBase", "UDB"), ("CDataBase", "CDB"), ("SysDataBase", "SDB")):
        path = root / "BasicData" / f"{name}.Auto.txt"
        if not path.is_file():
            continue
        index, counts = _database_index(path, code)
        database_types[code] = index
        database_report[code] = {
            str(type_id): {
                "name": item.name,
                "fields": {str(key): value for key, value in item.field_names.items()},
                "field_types": {str(key): value for key, value in item.field_types.items()},
                "data_count": len(item.rows),
            }
            for type_id, item in index.items()
        }
        database_counts[name] = counts

    project = AutoProject(editor.version, tuple(blocks), tuple(sorted(database_types)))
    dependencies, blocking, warnings = _analyze_blocks(project.events, items, database_types)
    call_graph, event_summaries = _call_graph_report(list(project.events))
    usage_by_key, proven_display = _translation_usage_report(
        project.events, items, dependencies
    )
    all_commands = [
        command for block in project.events for command in block.commands
    ]
    semantic_missing = [
        command
        for command in all_commands
        if not _command_transfer_complete(command)
    ]
    control_commands = [
        command
        for command in all_commands
        if (
            (semantics := command_semantics(
                command.opcode, len(command.ints), len(command.strings)
            ))
            and semantics.get("effect") == "control_flow"
        )
    ]
    covered_control_commands = sum(
        command.opcode in _CFG_IMPLEMENTED_OPCODES
        for command in control_commands
    )
    call_edges = list(call_graph.get("edges", []))
    resolved_calls = sum(
        bool(edge.get("targets")) and not edge.get("dynamic")
        for edge in call_edges
        if isinstance(edge, dict)
    )
    unresolved_scopes = sorted({
        str(scope)
        for dependency in dependencies
        for scope in dependency.get("unresolved_scopes", [])
    })
    verified_version = tuple(int(value) for value in VERIFIED_EDITOR_VERSION.split("."))
    newer_editor = editor.version_tuple > verified_version
    catalog_warnings = (
        [
            f"当前命令表仅验证至 Editor {VERIFIED_EDITOR_VERSION}；"
            f"{editor.version} 的新参数形状仍按未知命令处理。"
        ]
        if newer_editor
        else []
    )
    return {
        "schema": AUTO_ANALYSIS_SCHEMA,
        "editor": {
            "path": str(editor.path),
            "version": editor.version,
            "sha256": editor.sha256,
        },
        "command_catalog": {
            "schema": CATALOG_SCHEMA,
            "verified_through": VERIFIED_EDITOR_VERSION,
            "newer_editor": newer_editor,
            "shape_coverage": {
                "commands": len(all_commands),
                "covered": len(all_commands) - len(semantic_missing),
                "ratio": (
                    (len(all_commands) - len(semantic_missing)) / len(all_commands)
                    if all_commands else 1.0
                ),
            },
            "semantic_coverage": {
                "commands": len(all_commands),
                "covered": len(all_commands) - len(semantic_missing),
                "missing": len(semantic_missing),
                "ratio": (
                    (len(all_commands) - len(semantic_missing)) / len(all_commands)
                    if all_commands else 1.0
                ),
            },
            "cfg_coverage": {
                "control_commands": len(control_commands),
                "covered": covered_control_commands,
                "missing": len(control_commands) - covered_control_commands,
                "ratio": (
                    covered_control_commands / len(control_commands)
                    if control_commands else 1.0
                ),
            },
            "call_summary_coverage": {
                "calls": len(call_edges),
                "resolved": resolved_calls,
                "conservative": len(call_edges) - resolved_calls,
                "ratio": resolved_calls / len(call_edges) if call_edges else 1.0,
            },
        },
        "input_hash": input_hash,
        "output_hash": hash_directory(root),
        "counts": {
            "common_events": common_counts["events"],
            "common_pages": common_counts["pages"],
            "common_commands": common_counts["commands"],
            **{f"map_{key}": value for key, value in map_counts.items()},
            "database": database_counts,
        },
        "databases": database_report,
        "dependencies": dependencies,
        "blocking_issues": blocking,
        "event_summaries": event_summaries,
        "call_graph": call_graph,
        "reachable_scopes": unresolved_scopes,
        "usage_by_key": usage_by_key,
        "safe_to_translate": proven_display,
        "keep_original": sorted(set(usage_by_key) - set(proven_display)),
        "unresolved_scopes": unresolved_scopes,
        "unknown_commands": warnings,
        "warnings": catalog_warnings + [
            f"未解释的字符串命令 opcode={warning['opcode']} {warning['shape']} ×{warning['count']}"
            for warning in warnings
        ],
    }


def _safety_predicate(operator: str, left: str, right: str) -> bool:
    if operator == "equals":
        return left == right
    if operator == "not_equals":
        return left != right
    if operator == "contains":
        return right in left
    if operator == "starts_with":
        return left.startswith(right)
    raise ValueError(f"Editor 分析报告包含未知字符串比较操作符：{operator}")


def _scope_keys(
    items: list[TranslationItem],
    scopes: Iterable[object],
    cache: dict[str, frozenset[str]] | None = None,
) -> set[str]:
    selected: set[str] = set()
    for raw_scope in scopes:
        scope = str(raw_scope)
        if cache is not None and scope in cache:
            selected.update(cache[scope])
            continue
        matched: set[str] = set()
        if scope == "project":
            matched.update(item.key for item in items)
        elif scope == "common:*":
            matched.update(
                item.key for item in items if item.code.upper().startswith("COMMON-")
            )
        elif scope.startswith("common:"):
            prefix = f"COMMON-{scope.split(':', 1)[1]}-"
            matched.update(
                item.key for item in items if item.code.upper().startswith(prefix)
            )
        elif scope.startswith("map:"):
            _, map_id, event_id, page = scope.split(":", 3)
            prefix = f"MAP-{map_id}-EV{int(event_id):03d}-PAGE{page}-"
            matched.update(
                item.key for item in items if item.code.upper().startswith(prefix)
            )
        elif scope.startswith("database:"):
            parts = scope.split(":")
            if len(parts) != 5:
                matched.update(item.key for item in items)
            else:
                _, database, type_id, data_id, field_id = parts
                for item in items:
                    match = _WORKBOOK_DB_CODE_RE.fullmatch(item.code)
                    if not match:
                        continue
                    if (
                        match.group("database").upper() == database.upper()
                        and (type_id == "*" or match.group("type") == type_id)
                        and (data_id == "*" or match.group("data") == data_id)
                        and (field_id == "*" or match.group("field") == field_id)
                    ):
                        matched.add(item.key)
        else:
            matched.update(item.key for item in items)
        if cache is not None:
            cache[scope] = frozenset(matched)
        selected.update(matched)
    return selected


def analyze_translation_safety(
    auto_dir: str | Path,
    items: list[TranslationItem],
    candidate_values: dict[str, str],
    policy: str,
    *,
    analysis: dict[str, object],
) -> dict[str, object]:
    """Approve only candidate strings whose Auto uses are statically proven safe."""
    if policy not in {"warn", "block"}:
        raise ValueError(f"未知 WOLF 逻辑安全策略：{policy}")
    if analysis.get("schema") != AUTO_ANALYSIS_SCHEMA:
        raise ValueError(
            f"WOLF 事件逻辑保护需要 schema {AUTO_ANALYSIS_SCHEMA} Editor 分析报告，请重新执行导出文本。"
        )
    root = Path(auto_dir).resolve()
    if hash_directory(root) != analysis.get("output_hash"):
        raise ValueError("Editor Auto 目录已变化，请重新执行导出文本。")
    usage_by_key = analysis.get("usage_by_key")
    dependencies = analysis.get("dependencies")
    if not isinstance(usage_by_key, dict) or not isinstance(dependencies, list):
        raise ValueError("Editor 分析报告缺少翻译用途或依赖数据。")

    originals = {item.key: item.original for item in items}
    candidates = {
        key: value
        for key, value in candidate_values.items()
        if key in originals and value and value != originals[key]
    }
    event_targets: dict[str, int] = {}
    summaries = analysis.get("event_summaries", [])
    if isinstance(summaries, list):
        for summary in summaries:
            if not isinstance(summary, dict):
                continue
            name = str(summary.get("event_name", ""))
            node = str(summary.get("event", ""))
            match = re.search(r":(\d+):\d+$", node)
            if name and node.startswith("common:") and match:
                event_targets[name] = max(
                    event_targets.get(name, -1), int(match.group(1))
                )

    safe = {
        key
        for key in candidates
        if (uses := set(map(str, usage_by_key.get(key, ()))))
        and uses <= {"display_only", "logic", "event_target"}
        and (
            "event_target" not in uses
            or event_targets.get(originals[key]) == event_targets.get(candidates[key])
        )
    }
    base_protected = set(candidates) - safe
    forced: set[str] = set()
    reasons: dict[str, set[str]] = {}

    def final_value(key: str) -> str:
        if key in base_protected or key in forced:
            return originals[key]
        return candidates.get(key, originals[key])

    def same_event_target(key: str) -> bool:
        return event_targets.get(originals[key]) == event_targets.get(final_value(key))
    scope_sets: dict[str, set[str]] = {"project": set(originals)}
    for item in items:
        upper = item.code.upper()
        common = re.match(r"COMMON-(\d+)-", upper)
        if common:
            scope_sets.setdefault("common:*", set()).add(item.key)
            scope_sets.setdefault(f"common:{common.group(1)}", set()).add(item.key)
        map_item = re.match(r"MAP-(\d+)-EV(\d+)-PAGE(\d+)-", upper)
        if map_item:
            scope_sets.setdefault(
                f"map:{int(map_item.group(1))}:{int(map_item.group(2))}:{int(map_item.group(3))}",
                set(),
            ).add(item.key)
        database = _WORKBOOK_DB_CODE_RE.fullmatch(item.code)
        if database:
            db = database.group("database").upper()
            type_id = database.group("type")
            data_id = database.group("data")
            field_id = database.group("field")
            for scope in (
                f"database:{db}:*:*:*",
                f"database:{db}:{type_id}:*:*",
                f"database:{db}:{type_id}:{data_id}:*",
                f"database:{db}:{type_id}:*:{field_id}",
                f"database:{db}:{type_id}:{data_id}:{field_id}",
            ):
                scope_sets.setdefault(scope, set()).add(item.key)
    scope_cache = {
        scope: frozenset(keys) for scope, keys in scope_sets.items()
    }

    def protect(keys: Iterable[object], reason: str) -> None:
        for raw_key in keys:
            key = str(raw_key)
            if key in candidates:
                forced.add(key)
                reasons.setdefault(key, set()).add(reason)

    def scoped_keys(dependency: dict[str, object], side: str) -> set[str]:
        field = "source_scopes" if side == "left" else "right_source_scopes"
        raw_scopes = tuple(map(str, dependency.get(field, ())))
        if not raw_scopes and dependency.get("right_is_variable"):
            raw_scopes = tuple(map(str, dependency.get("unresolved_scopes", ())))
        return _scope_keys(items, raw_scopes, scope_cache)

    def condition_domain(
        keys: Iterable[str], values: Iterable[object]
    ) -> list[tuple[str | None, str, str]]:
        records = {
            (key, originals[key], final_value(key))
            for key in keys
            if key in originals
        }
        records.update((None, str(value), str(value)) for value in values)
        return sorted(records, key=lambda item: (item[0] or "", item[1], item[2]))

    def replay_dynamic_condition(dependency: dict[str, object]) -> bool:
        operator = str(dependency.get("operator", "unknown"))
        if operator not in {"equals", "not_equals", "contains", "starts_with"}:
            return False
        left_keys = set(map(str, dependency.get("source_keys", ())))
        left_keys.update(scoped_keys(dependency, "left"))
        if not dependency.get("right_is_variable"):
            literal = str(dependency.get("literal", ""))
            for key in left_keys & candidates.keys():
                if _safety_predicate(operator, originals[key], literal) != _safety_predicate(
                    operator, final_value(key), literal
                ):
                    protect((key,), "condition_truth_change")
            return True

        right_keys = set(map(str, dependency.get("right_source_keys", ())))
        right_keys.update(scoped_keys(dependency, "right"))
        left = condition_domain(left_keys, dependency.get("left_values", ()))
        right = condition_domain(right_keys, dependency.get("right_values", ()))
        changed_candidates = (left_keys | right_keys) & candidates.keys()
        if not changed_candidates:
            return True
        if not left or not right:
            protect(changed_candidates, "dynamic_condition_operand_unknown")
            return True
        if operator in {"equals", "not_equals"}:
            right_by_original: dict[str, list[tuple[str | None, str]]] = {}
            right_by_final: dict[str, list[tuple[str | None, str]]] = {}
            for key, original, final in right:
                right_by_original.setdefault(original, []).append((key, final))
                right_by_final.setdefault(final, []).append((key, original))
            for left_key, left_original, left_final in left:
                for right_key, right_final in right_by_original.get(left_original, ()):
                    if left_final != right_final:
                        protect((left_key, right_key), "condition_truth_change")
                for right_key, right_original in right_by_final.get(left_final, ()):
                    if left_original != right_original:
                        protect((left_key, right_key), "condition_truth_change")
            return True
        if len(left) * len(right) > _VALUE_LIMIT * _VALUE_LIMIT:
            # ponytail: non-equality cross-products stay bounded; a symbolic
            # string-relation domain can replace this conservative fallback.
            protect(changed_candidates, "dynamic_condition_domain_too_large")
            return True
        for left_key, left_original, left_final in left:
            for right_key, right_original, right_final in right:
                if _safety_predicate(operator, left_original, right_original) != _safety_predicate(
                    operator, left_final, right_final
                ):
                    protect((left_key, right_key), "condition_truth_change")
        return True

    def evaluate_dependency(dependency: object) -> None:
        if not isinstance(dependency, dict):
            raise ValueError("Editor 分析报告包含损坏的依赖记录。")
        status = str(dependency.get("status", "blocking"))
        kind = str(dependency.get("kind", "condition"))
        source_keys = list(map(str, dependency.get("source_keys", ())))
        right_keys = list(map(str, dependency.get("right_source_keys", ())))
        condition_keys = list(map(str, dependency.get("condition_keys", ())))
        protect(condition_keys, "condition_literal")
        if kind in {"display", "flow"}:
            return
        if status == "dynamic" and kind == "condition" and replay_dynamic_condition(dependency):
            return
        if status != "resolved":
            reason = str(dependency.get("reason", "unresolved"))
            target_equivalence = (
                kind == "call" and reason.startswith("公共事件目标为运行时动态值")
            )
            protect(
                (
                    key
                    for key in [*source_keys, *right_keys]
                    if not target_equivalence
                    or key not in candidates
                    or not same_event_target(key)
                ),
                reason,
            )
            raw_scopes = tuple(dependency.get("unresolved_scopes", ()))
            if raw_scopes or status == "blocking":
                scoped = _scope_keys(
                    items,
                    raw_scopes or ("project",),
                    scope_cache,
                )
                protect(
                    (
                        key
                        for key in scoped
                        if set(map(str, usage_by_key.get(key, ()))) != {"display_only"}
                        and (
                            not target_equivalence
                            or key not in candidates
                            or not same_event_target(key)
                        )
                    ),
                    reason,
                )
            return
        if kind != "condition":
            protect([*source_keys, *right_keys], kind)
            return
        operator = str(dependency.get("operator", "unknown"))
        literal = str(dependency.get("literal", ""))
        if operator not in {"equals", "not_equals", "contains", "starts_with"}:
            protect([*source_keys, *right_keys], "unsupported_condition")
            return
        if right_keys:
            for left_key in source_keys:
                for right_key in right_keys:
                    if left_key not in candidates and right_key not in candidates:
                        continue
                    left_original = originals.get(left_key, "")
                    right_original = originals.get(right_key, "")
                    left_final = final_value(left_key) if left_key in originals else left_original
                    right_final = final_value(right_key) if right_key in originals else right_original
                    if _safety_predicate(operator, left_original, right_original) != _safety_predicate(
                        operator, left_final, right_final
                    ):
                        protect((left_key, right_key), "condition_truth_change")
            return
        for key in source_keys:
            if key not in candidates:
                continue
            original = originals[key]
            if _safety_predicate(operator, original, literal) != _safety_predicate(
                operator, final_value(key), literal
            ):
                protect((key,), "condition_truth_change")
            elif set(map(str, usage_by_key.get(key, ()))) <= {"logic"}:
                safe.add(key)

    iterations = 0
    while True:
        protected_before = len(forced)
        for dependency in dependencies:
            evaluate_dependency(dependency)
        iterations += 1
        if len(forced) == protected_before:
            break
        if iterations > len(candidates) + 1:
            raise RuntimeError("WOLF 候选译文安全保护集合无法收敛。")

    protected = set(base_protected)
    protected.update(forced)
    safe.difference_update(protected)
    unresolved = sorted(
        {
            str(scope)
            for dependency in dependencies
            if isinstance(dependency, dict) and dependency.get("status") != "resolved"
            for scope in dependency.get("unresolved_scopes", ())
        }
    )
    if policy == "block" and protected:
        first = sorted(protected)[0]
        raise RuntimeError(
            "WOLF 静态安全分析无法证明全部候选译文安全，已阻止导入："
            f"{first}（{', '.join(sorted(reasons.get(first, {'not_proven_safe'})))}）。"
        )
    return {
        "schema": 1,
        "safe_to_translate": sorted(safe),
        "keep_original": sorted(protected),
        "unresolved_scopes": unresolved,
        "replay": {
            "iterations": iterations,
            "candidate_changes": len(candidates),
            "safe_changes": len(safe),
            "protected_changes": len(protected),
            "control_flow_equivalent": True,
        },
        "reasons": {key: sorted(value) for key, value in sorted(reasons.items())},
    }


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
    for code, code_items in by_code.items():
        for item in code_items:
            if not item.flag.upper().startswith("COPY-FROM-"):
                continue
            source_code = item.flag[len("COPY-FROM-"):].upper()
            if source_code in approved_codes:
                approved_codes.add(code)

    def event_index(root: Path) -> dict[tuple[str, int, int], _CommandBlock]:
        result: dict[tuple[str, int, int], _CommandBlock] = {}
        paths = [root / "BasicData" / "CommonEvent.dat.Auto.txt"]
        paths.extend(sorted((root / "MapData").rglob("*.mps.Auto.txt")))
        for path in paths:
            if not path.is_file():
                continue
            event_type = "common" if path.name == "CommonEvent.dat.Auto.txt" else "map"
            for block in _event_blocks(path, event_type, source=path.relative_to(root).as_posix())[0]:
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
    if set(before_events) != set(after_events):
        add("event_set", "AutoProject", sorted(map(str, before_events)), sorted(map(str, after_events)))
    for key in sorted(set(before_events) & set(after_events)):
        before = before_events[key]
        after = after_events[key]
        location = f"{before.source} event={before.event_id} page={before.page}"
        if before.event_name != after.event_name:
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
                code = _event_code(before, index, string_index).upper()
                if code not in approved_codes:
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
            if (
                left_type.name != right_type.name
                or left_type.field_names != right_type.field_names
                or left_type.field_types != right_type.field_types
                or left_type.data_names != right_type.data_names
                or len(left_type.rows) != len(right_type.rows)
            ):
                add("database_metadata", type_location, left_type.name, right_type.name)
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
                (lambda elapsed: warning(f"WOLF RPG Editor 全事件导出已运行 {elapsed / 60:.1f} 分钟，请继续等待。"))
                if warning
                else None
            ),
        )
        if result.return_code != 0:
            raise RuntimeError(f"WOLF RPG Editor 退出码为 {result.return_code}。")
        auto_source = sandbox / "Auto"
        _validate_outputs(sandbox, auto_source, maps)
        auto_target = output / "editor-auto"
        shutil.copytree(auto_source, auto_target)
        input_hash = hash_directory(sandbox / "Data")
        analysis = analyze_auto_export(auto_target, items, editor, input_hash=input_hash)
        analysis_path = output / "editor-analysis.json"
        atomic_write_json(analysis_path, analysis)
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
