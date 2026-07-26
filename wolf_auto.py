from __future__ import annotations

import csv
import re
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Iterator, Mapping

from models import TranslationItem
from wolf_tools import COPY_FROM_RE, hash_directory


_COMMAND_RE = re.compile(
    r'^\[(?P<opcode>\d+)]\[(?P<int_count>\d+),(?P<string_count>\d+)]'
    r'<(?P<indent>\d+)>\((?P<ints>.*?)\)(?P<tail>.*)$'
)


@dataclass(frozen=True)
class AddressExpr:
    """A normalized runtime address used to correlate dynamic database access."""

    kind: str
    scope: str = ""
    value: int = 0
    offset: int = 0
    step: int = 0

    @classmethod
    def constant(cls, value: int) -> "AddressExpr":
        return cls("constant", value=value)

    @classmethod
    def event_input(cls, scope: str, value: int) -> "AddressExpr":
        return cls("event_input", scope=scope, value=value)

    def add(self, offset: int) -> "AddressExpr":
        return self if not offset else AddressExpr("offset", self.scope, self.value, self.offset + offset)


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
    map_id: int = -1
    map_ids: tuple[int, ...] = ()


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
    source_dir: Path | None = field(default=None, compare=False, repr=False)
    database_index: Mapping[str, Mapping[int, "_DatabaseType"]] = field(
        default_factory=dict, compare=False, repr=False
    )
    counts: Mapping[str, object] = field(default_factory=dict, compare=False, repr=False)
    database_report: Mapping[str, object] = field(
        default_factory=dict, compare=False, repr=False
    )


@dataclass(frozen=True)
class _DatabaseType:
    database: str
    type_id: int
    name: str
    field_names: dict[int, str]
    field_types: dict[int, int]
    rows: tuple[tuple[str, ...], ...]
    data_names: tuple[str, ...]

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
                if item_num == 0 and data_num is not None and not any(csv_lines):
                    # Editor 3.713 emits padding blank lines for a type with data
                    # names but no fields. DATA_NUM is the only lossless row count.
                    rows = [("",) for _index in range(data_num)]
                else:
                    rows = [
                        tuple(row)
                        for row in csv.reader(
                            (value + "\n" for value in csv_lines), strict=True
                        )
                        if row
                    ]
            except csv.Error as error:
                raise ValueError(f"数据库 CSV 损坏：{path}: {error}") from error
    finish_type()
    if declared_types is None or len(types) != declared_types:
        raise ValueError(f"数据库类型数量不符：{path} 声明 {declared_types}，实际 {len(types)}")
    return types, {"types": len(types), "csv_rows": csv_rows}
def _block_map_id(block: _CommandBlock) -> int:
    if block.map_ids:
        return block.map_ids[0]
    if block.map_id >= 0:
        return block.map_id
    match = re.search(r"Map(\d+)\.mps\.Auto\.txt$", block.source, re.IGNORECASE)
    return int(match.group(1)) if match else -1


def _block_map_ids(block: _CommandBlock) -> tuple[int, ...]:
    if block.map_ids:
        return block.map_ids
    return (_block_map_id(block),)


def _event_code(block: _CommandBlock, command_index: int, string_index: int) -> str:
    if block.event_type == "common":
        return f"COMMON-{block.event_id}-{command_index - 1}-{string_index}"
    map_id = _block_map_id(block)
    return f"MAP-{map_id}-Ev{block.event_id:03d}-Page{block.page}-{command_index - 1}-{string_index}"


def _event_codes(
    block: _CommandBlock, command_index: int, string_index: int
) -> tuple[str, ...]:
    if block.event_type == "common":
        return (_event_code(block, command_index, string_index),)
    return tuple(
        f"MAP-{map_id}-Ev{block.event_id:03d}-Page{block.page}-{command_index - 1}-{string_index}"
        for map_id in _block_map_ids(block)
    )


def _event_name_code(block: _CommandBlock) -> str:
    if block.event_type == "common":
        return f"COMMON-{block.event_id}-Name"
    return f"MAP-{_block_map_id(block)}-Ev{block.event_id:03d}-Name"


def _event_name_codes(block: _CommandBlock) -> tuple[str, ...]:
    if block.event_type == "common":
        return (_event_name_code(block),)
    return tuple(
        f"MAP-{map_id}-Ev{block.event_id:03d}-Name"
        for map_id in _block_map_ids(block)
    )


def _items_for_event_codes(
    event_items: dict[str, tuple[TranslationItem, ...]],
    block: _CommandBlock,
    command_index: int,
    string_index: int,
) -> tuple[TranslationItem, ...]:
    by_key: dict[str, TranslationItem] = {}
    for code in _event_codes(block, command_index, string_index):
        for item in event_items.get(code.upper(), ()):
            by_key.setdefault(item.key, item)
    return tuple(by_key.values())


def _map_ids_from_databases(
    databases: dict[str, dict[int, _DatabaseType]],
) -> dict[str, tuple[int, ...]]:
    table = databases.get("SDB", {}).get(0)
    if table is None:
        return {}
    result: dict[str, list[int]] = {}
    for map_id, row in enumerate(table.rows):
        if not row or not row[0].strip():
            continue
        relative = row[0].strip().replace("\\", "/").lstrip("/")
        if relative.casefold().startswith("data/"):
            relative = relative[5:]
        key = f"{relative}.Auto.txt".casefold()
        result.setdefault(key, []).append(map_id)
    return {key: tuple(values) for key, values in result.items()}


def parse_auto_project(auto_dir: str | Path, editor_version: str) -> AutoProject:
    """Parse one Editor Auto export into the immutable project IR."""
    root = Path(auto_dir).resolve()
    common = root / "BasicData" / "CommonEvent.dat.Auto.txt"
    if not common.is_file():
        raise ValueError("Editor 未生成 BasicData/CommonEvent.dat.Auto.txt。")
    blocks, common_counts = _event_blocks(
        common, "common", source=common.relative_to(root).as_posix()
    )
    map_counts = {"maps": 0, "events": 0, "pages": 0, "commands": 0}
    for map_path in sorted((root / "MapData").rglob("*.mps.Auto.txt")):
        map_blocks, parsed_counts = _event_blocks(
            map_path, "map", source=map_path.relative_to(root).as_posix()
        )
        blocks.extend(map_blocks)
        map_counts["maps"] += 1
        for key in ("events", "pages", "commands"):
            map_counts[key] += parsed_counts[key]
    database_index: dict[str, dict[int, _DatabaseType]] = {}
    database_counts: dict[str, dict[str, int]] = {}
    database_report: dict[str, object] = {}
    for name, code in (("DataBase", "UDB"), ("CDataBase", "CDB"), ("SysDataBase", "SDB")):
        path = root / "BasicData" / f"{name}.Auto.txt"
        if path.is_file():
            index, parsed_counts = _database_index(path, code)
            database_index[code] = index
            database_counts[name] = parsed_counts
            database_report[code] = {
                str(type_id): {
                    "name": entry.name,
                    "fields": {str(key): value for key, value in entry.field_names.items()},
                    "field_types": {str(key): value for key, value in entry.field_types.items()},
                    "data_count": len(entry.rows),
                }
                for type_id, entry in index.items()
            }
    map_ids = _map_ids_from_databases(database_index)
    events = tuple(
        replace_block_map_ids(block, map_ids) for block in blocks
    )
    return AutoProject(
        editor_version,
        events,
        tuple(sorted(database_index)),
        source_dir=root,
        database_index=MappingProxyType({
            code: MappingProxyType(dict(index)) for code, index in database_index.items()
        }),
        counts=MappingProxyType({
            "common_events": common_counts["events"],
            "common_pages": common_counts["pages"],
            "common_commands": common_counts["commands"],
            **{f"map_{key}": value for key, value in map_counts.items()},
            "database": MappingProxyType(dict(database_counts)),
        }),
        database_report=MappingProxyType(database_report),
    )


def replace_block_map_ids(
    block: AutoEvent, map_ids: Mapping[str, tuple[int, ...]]
) -> AutoEvent:
    if block.event_type != "map" or block.source.casefold() not in map_ids:
        return block
    values = map_ids[block.source.casefold()]
    return _CommandBlock(
        block.source,
        block.event_type,
        block.event_id,
        block.event_name,
        block.page,
        block.commands,
        block.value_inputs,
        block.string_inputs,
        block.return_target,
        values[0],
        values,
    )
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
