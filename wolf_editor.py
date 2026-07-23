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
from collections import Counter
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable, Iterator

from models import TranslationItem
from safe_io import atomic_write_json, package_lock, replace_with_retry
from wolf_tools import hash_directory, run_process, sha256_file


EDITOR_DOWNLOAD_URL = "https://silversecond.com/WolfRPGEditor/Download.shtml"
MAX_EDITOR_PAGE_BYTES = 2 * 1024 * 1024
# ponytail: This caps an official tool download, not game data; raise it if future packages outgrow 256 MiB.
MAX_EDITOR_ARCHIVE_BYTES = 256 * 1024 * 1024
MIN_EDITOR_VERSION = (3, 500)
AUTO_ANALYSIS_SCHEMA = 2
_VALUE_LIMIT = 256
_LOOP_LIMIT = 32
_CALL_DEPTH_LIMIT = 8
# Editor 3.713 calibration and the official command manual confirm these
# commands never assign string variables. Numeric side effects remain tainted.
_NO_STRING_WRITE_OPCODES = frozenset({101, 103, 106, 140, 150, 212})
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

    @property
    def tracked(self) -> bool:
        return bool(self.source_keys or self.cells)


@dataclass
class _AnalysisState:
    numbers: dict[int, _NumberValue]
    strings: dict[int, _StringValue]

    def copy(self) -> "_AnalysisState":
        return _AnalysisState(dict(self.numbers), dict(self.strings))


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
    if left is None or right is None:
        value = left or right
        assert value is not None
        return _StringValue(
            value.source_keys,
            value.cells,
            tuple(dict.fromkeys(value.trace + ("控制流部分分支赋值",))),
            value.unknown,
            value.symbolic_all,
        )
    keys = set(left.source_keys) | set(right.source_keys)
    cells = set(left.cells) | set(right.cells)
    symbolic_all = left.symbolic_all or right.symbolic_all
    if len(keys) + len(cells) > _VALUE_LIMIT and not symbolic_all:
        return _StringValue(trace=(left.trace + right.trace)[-_VALUE_LIMIT:], unknown="字符串来源集合超过 256 项")
    return _StringValue(
        frozenset(keys),
        frozenset(cells),
        tuple(dict.fromkeys(left.trace + right.trace))[-_VALUE_LIMIT:],
        (left.unknown if left.tracked else "") or (right.unknown if right.tracked else ""),
        symbolic_all,
    )


def _merge_states(states: list[_AnalysisState]) -> _AnalysisState:
    if not states:
        return _AnalysisState({}, {})
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
    return result


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
        call_stack: tuple[tuple[int, int], ...] = (),
    ) -> None:
        self.block = block
        self.databases = databases
        self.database_keys = database_keys
        self.event_items = event_items
        self.common_by_id = common_by_id or {}
        self.common_by_name = common_by_name or {}
        self.call_stack = call_stack
        self.dependencies: list[dict[str, object]] = []
        self.blocking: list[dict[str, object]] = []
        self.unknown = Counter()
        self.unknown_locations: dict[tuple[int, str], list[str]] = {}
        self._unknown_seen: set[tuple[int, str, str]] = set()
        self.summary_failed = ""

    def _location(self, index: int) -> str:
        return (
            f"{self.block.source} event={self.block.event_id} page={self.block.page} "
            f"command={index + 1}"
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
        if len(command.ints) < 5:
            self._record_unknown(command, index, "invalid-250")
            return
        flags = command.ints[3]
        destination = command.ints[4] & 0x00FFFFFF
        byte1 = (flags >> 8) & 0xFF
        byte2 = (flags >> 16) & 0xFF
        database = {0: "CDB", 1: "SDB", 2: "UDB"}.get(byte1 & 0x0F)
        if database is None:
            self._record_unknown(command, index, f"250-flags-{flags:08x}")
            return
        if byte1 & 0xF0 != 0x10:
            # Database writes consume the final argument; they do not assign it.
            self._record_unknown(command, index, f"250-write-{flags:08x}")
            return
        type_ids = self._type_ids(database, command, byte2, state)
        if not type_ids:
            state.strings[destination] = _StringValue(trace=(self._location(index),), unknown="数据库类型无法解析")
            return

        data_raw, field_raw = command.ints[1], command.ints[2]
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
                trace=(self._location(index),), unknown="数据库字段选择器无法解析"
            )
            return

        cells: set[tuple[str, int, int, int]] = set()
        keys: set[str] = set()
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
                        if coordinate_keys:
                            cells.add(coordinate)
                            keys.update(coordinate_keys)
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
            state.strings[destination] = _StringValue(
                frozenset(keys), frozenset(cells), trace, symbolic_all=data_all,
            )
        elif numeric_values:
            values = _limited(numeric_values)
            state.numbers[destination] = _NumberValue(
                values, "数据库数值集合超过 256 项" if values is None else "", True
            )

    def _set_number(self, command: _Command, index: int, state: _AnalysisState) -> None:
        if len(command.ints) < 4:
            self._record_unknown(command, index, "invalid-121")
            return
        destination, left_raw, right_raw, flags = command.ints[:4]
        byte0 = flags & 0xFF
        byte1 = (flags >> 8) & 0xFF
        if byte0:
            state.numbers[destination] = _NumberValue(None, f"未支持的 121 间接标志 {byte0}", True)
            return
        value = _calculate_numbers(
            _number_argument(left_raw, state),
            _number_argument(right_raw, state),
            (byte1 >> 4) & 0x0F,
        )
        assignment = byte1 & 0x0F
        if assignment == 0:
            state.numbers[destination] = value
        elif assignment in {1, 2}:
            current = state.numbers.get(destination, _NumberValue(None, "复合赋值前值未知"))
            state.numbers[destination] = _calculate_numbers(current, value, assignment - 1)
        else:
            state.numbers[destination] = _NumberValue(None, f"未支持的 121 赋值运算 {assignment}", True)

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
        value = _StringValue(
            keys,
            trace=(f"{self._location(index)} opcode={command.opcode} literal",),
        )
        for reference in _CSELF_REFERENCE_RE.findall(literal):
            referenced = state.strings.get(1_600_000 + int(reference))
            if referenced is not None:
                value = _merge_strings(value, referenced) or value
        return value

    def _set_string(self, command: _Command, index: int, state: _AnalysisState) -> None:
        if len(command.ints) < 3:
            self._record_unknown(command, index, "invalid-122")
            return
        destination, flags, source_raw = command.ints[:3]
        source_kind = flags & 0x0F
        assignment = (flags >> 8) & 0x0F
        if source_kind == 0:
            value = self._literal_string(command, index, 0, state)
        elif source_kind == 1:
            value = state.strings.get(
                source_raw & 0x00FFFFFF,
                _StringValue(unknown=f"字符串变量 {source_raw & 0x00FFFFFF} 来源未知"),
            )
        else:
            current = state.strings.get(destination)
            state.strings[destination] = _StringValue(
                current.source_keys if current else frozenset(),
                current.cells if current else frozenset(),
                current.trace if current else (),
                f"未支持的 122 来源模式 {source_kind}",
                current.symbolic_all if current else False,
            )
            return
        if assignment == 0:
            state.strings[destination] = value
        elif assignment == 1:
            state.strings[destination] = _merge_strings(state.strings.get(destination), value) or value
        elif assignment == 3 and source_kind == 1:
            # ponytail: Auto protection tracks provenance, not WOLF's concrete string values.
            source = source_raw & 0x00FFFFFF
            traced = _StringValue(
                value.source_keys,
                value.cells,
                tuple(dict.fromkeys(value.trace + (f"{self._location(index)} opcode=122 op=3",))),
                value.unknown,
                value.symbolic_all,
            )
            state.strings[destination] = traced
            state.strings[source] = traced
        elif assignment == 10:
            current = state.strings.get(destination)
            current_is_translatable = bool(current and (current.source_keys or current.cells))
            merged = _merge_strings(current if current_is_translatable else None, value) or value
            state.strings[destination] = _StringValue(
                merged.source_keys,
                merged.cells,
                tuple(dict.fromkeys(merged.trace + (f"{self._location(index)} opcode=122 op=10",))),
                value.unknown,
                merged.symbolic_all,
            )
        elif assignment == 9 and source_kind == 0:
            current = state.strings.get(destination)
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
            )
            state.strings[destination] = _merge_strings(current, replacement) or replacement
        else:
            state.strings[destination] = _StringValue(
                value.source_keys, value.cells, value.trace,
                f"未支持的 122 赋值运算 {assignment}", value.symbolic_all
            )

    def _condition(self, command: _Command, index: int, state: _AnalysisState) -> None:
        if not command.ints:
            self._record_unknown(command, index, "invalid-112")
            return
        count = command.ints[0]
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
            if operator is None:
                status = "blocking" if value and value.tracked else "untracked"
                reason = "未支持的字符串比较编码"
            elif right_is_variable and (
                value is None or right_value is None or not value.tracked or not right_value.tracked
            ):
                status = "untracked"
                reason = "字符串变量比较的一侧来源未知"
            elif right_is_variable and (value.unknown or right_value.unknown):
                status = "blocking"
                reason = value.unknown or right_value.unknown
            elif value is None or not value.tracked:
                status = "untracked"
                reason = f"条件变量 {variable} 从事件入口进入"
            elif value.unknown:
                status = "blocking"
                reason = value.unknown
            else:
                status = "resolved"
                reason = ""
            dependency = {
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
    ) -> tuple[_AnalysisState | None, int] | None:
        closing = self._matching(start, end, 499)
        if closing is None:
            return None
        indent = self.block.commands[start].indent
        markers = [
            index for index in range(start + 1, closing)
            if self.block.commands[index].indent == indent
            and self.block.commands[index].opcode in {401, 420, 421}
        ]
        if not markers:
            return state, closing + 1
        branch_states: list[_AnalysisState] = []
        for marker_index, marker in enumerate(markers):
            branch_end = markers[marker_index + 1] if marker_index + 1 < len(markers) else closing
            branch_state = state.copy()
            if self._execute(marker + 1, branch_end, branch_state, exits):
                branch_states.append(branch_state)
        if not any(self.block.commands[index].opcode in {420, 421} for index in markers):
            branch_states.append(state.copy())
        return (_merge_states(branch_states) if branch_states else None), closing + 1

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
    ) -> None:
        self._record_unknown(command, index, f"call-not-inlined:{reason}")
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
            f"来源经过未解释的公共事件调用 opcode={command.opcode}: {reason}",
            value.symbolic_all,
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

    def _call_target(self, command: _Command) -> tuple[_CommandBlock, int] | None:
        if len(command.ints) < 3 or command.ints[2] >= 1_000_000:
            return None
        choice = command.ints[2]
        if command.opcode == 300:
            if not command.strings:
                return None
            matches = self.common_by_name.get(command.strings[0], ())
            return (matches[0], choice) if len(matches) == 1 else None
        reference = command.ints[0]
        if 500_000 <= reference < 600_000:
            target_id = reference - 500_000
        elif 599_000 <= reference < 601_000 and self.block.event_type == "common":
            target_id = self.block.event_id + reference - 600_100
        else:
            return None
        target = self.common_by_id.get(target_id)
        return (target, choice) if target is not None else None

    def _call_event(self, command: _Command, index: int, state: _AnalysisState) -> None:
        # ponytail: Only explicit return slots carry provenance across calls here.
        # Add global-string side-effect summaries if a real project proves they matter.
        if len(command.ints) < 2 or not command.ints[1] & 0x01000000:
            return
        resolved = self._call_target(command)
        if resolved is None:
            self._unknown_call(command, index, state, "调用目标无法解析")
            return
        target, choice = resolved
        call_key = (target.event_id, choice)
        if len(self.call_stack) >= _CALL_DEPTH_LIMIT:
            self._unknown_call(command, index, state, "调用深度超过 8 层")
            return
        if call_key in self.call_stack:
            self._unknown_call(command, index, state, "检测到递归调用")
            return
        if target.return_target < 0 or target.value_inputs < 1:
            self._unknown_call(command, index, state, "被调事件没有有效返回槽或命令参数")
            return

        flags = command.ints[1]
        numeric_slots = flags & 0x0F
        string_count = (flags >> 4) & 0x0F
        if not 1 <= numeric_slots <= target.value_inputs:
            self._unknown_call(command, index, state, "数值实参数量与 Auto 头部不符")
            return
        if string_count > target.string_inputs:
            self._unknown_call(command, index, state, "字符串实参数量与 Auto 头部不符")
            return
        # ponytail: The call record is authoritative for this entry point. Common
        # events may expose fewer inputs than their file-wide maximum; absent slots
        # stay unknown and are safe unless the selected command actually reads them.
        numeric_count = numeric_slots - 1
        string_start = 3 + numeric_count
        string_end = string_start + string_count
        if len(command.ints) != string_end + 1:
            self._unknown_call(command, index, state, "实参数量与 Auto 头部不符")
            return
        string_offset = 1
        string_arguments = command.ints[string_start:string_end]
        if any(raw < 1_000_000 for raw in string_arguments) and len(command.strings) < string_offset + string_count:
            self._unknown_call(command, index, state, "字符串实参数量与 Auto 头部不符")
            return

        callee_state = _AnalysisState(
            {1_600_000: _NumberValue(frozenset({choice}))},
            {},
        )
        for offset, raw in enumerate(command.ints[3:string_start], start=1):
            callee_state.numbers[1_600_000 + offset] = _number_argument(raw, state)
        for offset, raw in enumerate(string_arguments):
            destination = 1_600_005 + offset
            if raw >= 1_000_000:
                callee_state.strings[destination] = state.strings.get(
                    raw & 0x00FFFFFF,
                    _StringValue(unknown=f"字符串实参 {raw & 0x00FFFFFF} 来源未知"),
                )
            else:
                callee_state.strings[destination] = self._literal_string(
                    command,
                    index,
                    string_offset + offset,
                    state,
                )

        label = next(
            (
                position
                for position, item in enumerate(target.commands)
                if item.opcode == 212
                and item.indent == 0
                and item.strings == (f"cmd:{choice}",)
            ),
            None,
        )
        if label is None:
            self._unknown_call(command, index, state, f"缺少 cmd:{choice} 标签")
            return
        end = next(
            (
                position
                for position in range(label + 1, len(target.commands))
                if target.commands[position].opcode == 212
                and target.commands[position].indent == 0
            ),
            len(target.commands),
        )
        child = _BlockAnalyzer(
            target,
            self.databases,
            self.database_keys,
            self.event_items,
            self.common_by_id,
            self.common_by_name,
            self.call_stack + (call_key,),
        )
        exits: list[_AnalysisState] = []
        fell_through = child._execute(label + 1, end, callee_state, exits)
        if fell_through or child.summary_failed or not exits:
            self._unknown_call(
                command,
                index,
                state,
                child.summary_failed or "公共事件摘要没有在 END 返回",
            )
            return

        result = _merge_states(exits)
        return_variable = 1_600_000 + target.return_target
        destination = command.ints[-1] & 0x00FFFFFF
        call_trace = f"{self._location(index)} -> common={target.event_id} cmd={choice}"
        if target.return_target >= 5:
            value = result.strings.get(return_variable)
            if value is None:
                self._unknown_call(command, index, state, "字符串返回槽没有赋值")
                return
            state.strings[destination] = _StringValue(
                value.source_keys,
                value.cells,
                tuple(dict.fromkeys(value.trace + (call_trace,))),
                value.unknown,
                value.symbolic_all,
            )
        else:
            value = result.numbers.get(return_variable)
            if value is None:
                self._unknown_call(command, index, state, "数值返回槽没有赋值")
                return
            state.numbers[destination] = value

    def _execute(
        self,
        start: int,
        end: int,
        state: _AnalysisState,
        exits: list[_AnalysisState] | None = None,
    ) -> bool:
        index = start
        while index < end:
            command = self.block.commands[index]
            if command.opcode == 121:
                self._set_number(command, index, state)
            elif command.opcode == 122:
                self._set_string(command, index, state)
            elif command.opcode == 250:
                self._database(command, index, state)
            elif command.opcode == 112:
                self._condition(command, index, state)
                branch = self._branches(index, end, state, exits)
                if branch:
                    merged, index = branch
                    if merged is None:
                        return False
                    state.numbers, state.strings = merged.numbers, merged.strings
                    continue
            elif command.opcode == 111:
                branch = self._branches(index, end, state, exits)
                if branch:
                    merged, index = branch
                    if merged is None:
                        return False
                    state.numbers, state.strings = merged.numbers, merged.strings
                    continue
            elif command.opcode in {210, 300}:
                self._call_event(command, index, state)
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
                        if not self._execute(index + 1, closing, state, exits):
                            return False
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
                            if not self._execute(index + 1, closing, widened, exits):
                                return False
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
                                    "循环扩大后字符串仍未稳定", value.symbolic_all
                                )
                        state.numbers, state.strings = merged.numbers, merged.strings
                    index = closing + 1
                    continue
            elif command.opcode == 213 and exits is not None:
                if command.strings == ("END",):
                    exits.append(state.copy())
                else:
                    self.summary_failed = f"未支持的标签跳转 {command.strings!r}"
                    self._record_unknown(command, index, "summary-goto")
                return False
            elif command.opcode in _NO_STRING_WRITE_OPCODES or (
                command.opcode == 213 and command.strings == ("END",)
            ):
                self._taint_unknown(command, index, state, strings=False)
            elif command.opcode not in {0, 401, 420, 421, 498, 499}:
                if any(command.strings) or any(value >= 1_000_000 for value in command.ints):
                    self._record_unknown(command, index)
                self._taint_unknown(command, index, state)
            index += 1
        return True

    def run(self) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
        self._execute(0, len(self.block.commands), _AnalysisState({}, {}))
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
    dependencies: list[dict[str, object]] = []
    blocking: list[dict[str, object]] = []
    unknown = Counter()
    locations: dict[tuple[int, str], list[str]] = {}
    for block in blocks:
        analyzer = _BlockAnalyzer(
            block,
            databases,
            frozen_database_keys,
            frozen_event_items,
            common_by_id,
            common_by_name,
        )
        block_dependencies, block_blocking, block_unknown = analyzer.run()
        dependencies.extend(block_dependencies)
        blocking.extend(block_blocking)
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
            merged_dependencies[identity] = dependency
            continue
        current["condition_keys"] = sorted(set(current["condition_keys"]) | set(dependency["condition_keys"]))
        current["source_keys"] = sorted(set(current["source_keys"]) | set(dependency["source_keys"]))
        current["right_source_keys"] = sorted(
            set(current.get("right_source_keys", [])) | set(dependency.get("right_source_keys", []))
        )
        cells = {
            (cell["database"], cell["type"], cell["data"], cell["field"])
            for cell in [*current["database_cells"], *dependency["database_cells"]]
        }
        current["database_cells"] = [
            {"database": cell[0], "type": cell[1], "data": cell[2], "field": cell[3]}
            for cell in sorted(cells)
        ]
        right_cells = {
            (cell["database"], cell["type"], cell["data"], cell["field"])
            for cell in [*current.get("right_database_cells", []), *dependency.get("right_database_cells", [])]
        }
        current["right_database_cells"] = [
            {"database": cell[0], "type": cell[1], "data": cell[2], "field": cell[3]}
            for cell in sorted(right_cells)
        ]
        current["trace"] = list(dict.fromkeys([*current["trace"], *dependency["trace"]]))[:_VALUE_LIMIT]
        if current["status"] == "blocking":
            pass
        elif dependency["status"] == "blocking":
            current["status"] = "blocking"
            current["reason"] = dependency["reason"]
        elif "resolved" in {current["status"], dependency["status"]}:
            current["status"] = "resolved"
            current["reason"] = ""
    dependencies = list(merged_dependencies.values())
    blocking = [item for item in dependencies if item["status"] == "blocking"]
    return dependencies, blocking, warnings


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

    dependencies, blocking, warnings = _analyze_blocks(blocks, items, database_types)
    return {
        "schema": AUTO_ANALYSIS_SCHEMA,
        "editor": {
            "path": str(editor.path),
            "version": editor.version,
            "sha256": editor.sha256,
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
        "unknown_commands": warnings,
        "warnings": [
            f"未解释的字符串命令 opcode={warning['opcode']} {warning['shape']} ×{warning['count']}"
            for warning in warnings
        ],
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
