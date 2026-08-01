from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


_COMMAND_RE = re.compile(
    r'^\[(?P<opcode>\d+)]\[(?P<int_count>\d+),(?P<string_count>\d+)]'
    r'<(?P<indent>\d+)>\((?P<ints>.*?)\)(?P<tail>.*)$'
)


@dataclass(frozen=True)
class _Command:
    opcode: int
    ints: tuple[int, ...]
    strings: tuple[str, ...]
    indent: int
    raw: str


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
