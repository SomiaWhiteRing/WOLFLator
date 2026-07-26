from __future__ import annotations

import ntpath
import re
from collections import Counter, deque
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Mapping

from models import TranslationItem
from wolf_auto import AutoProject, _Command, _CommandBlock, _DatabaseType, parse_auto_project
from wolf_command_catalog import CATALOG_SCHEMA, VERIFIED_EDITOR_VERSION, command_semantics
from wolf_tools import hash_directory

if TYPE_CHECKING:
    from wolf_editor import EditorInfo


AUTO_ANALYSIS_SCHEMA = 14
_VALUE_LIMIT = 256
# ponytail: concrete string names are cheap and materially improve dynamic DB
# selectors; switch to a symbolic string-set domain if a corpus exceeds this cap.
_STRING_LITERAL_LIMIT = 4096
# ponytail: global writes are joined for at most 32 rounds; non-convergence
# remains protected. A summarized call graph can raise this bound if needed.
_GLOBAL_STRING_FLOW_MAX_ITERATIONS = 32
_CALL_DEPTH_LIMIT = 64
_CFG_STATE_VISIT_LIMIT = 64
_CFG_IMPLEMENTED_OPCODES = frozenset(
    {0, 102, 104, 111, 112, 170, 171, 172, 173, 174, 175, 176, 179, 212, 213, 401, 402, 420, 421, 498, 499}
)
_CFG_CONTROL_OPCODES = _CFG_IMPLEMENTED_OPCODES
_WORKBOOK_DB_CODE_RE = re.compile(
    r"^(?P<database>UDB|CDB|SDB)-(?P<type>\d+)-(?P<data>\d+)-(?P<field>\d+)$",
    re.IGNORECASE,
)
_CSELF_REFERENCE_RE = re.compile(r"\\cself\[(\d+)]", re.IGNORECASE)
_STRING_REFERENCE_RE = re.compile(r"\\s\[(\d+)]", re.IGNORECASE)


@dataclass(frozen=True)
class Dependency:
    """Typed boundary for a recorded semantic dependency and its source scope."""

    kind: str
    status: str
    source_keys: tuple[str, ...] = ()
    source_scopes: tuple[str, ...] = ()
    location: str = ""


@dataclass(frozen=True)
class SemanticSnapshot:
    """One analysis result for an immutable AutoProject and candidate set."""

    project: AutoProject
    report: Mapping[str, object]


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
            matched.update(item.key for item in items if item.code.upper().startswith("COMMON-"))
        elif scope.startswith("common:"):
            prefix = f"COMMON-{scope.split(':', 1)[1]}-"
            matched.update(item.key for item in items if item.code.upper().startswith(prefix))
        elif scope.startswith("map:"):
            _, map_id, event_id, page = scope.split(":", 3)
            prefix = f"MAP-{map_id}-EV{int(event_id):03d}-PAGE{page}-"
            matched.update(item.key for item in items if item.code.upper().startswith(prefix))
        elif scope.startswith("database:"):
            parts = scope.split(":")
            if len(parts) != 5:
                matched.update(item.key for item in items)
            else:
                _, database, type_id, data_id, field_id = parts
                for item in items:
                    match = _WORKBOOK_DB_CODE_RE.fullmatch(item.code)
                    if match and (
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

@dataclass(frozen=True)
class _NumberValue:
    values: frozenset[int] | None
    reason: str = ""
    tracked: bool = False
    identity: str = ""


@dataclass(frozen=True)
class _StringValue:
    source_keys: frozenset[str] = frozenset()
    cells: frozenset[tuple[str, int, int, int]] = frozenset()
    trace: tuple[str, ...] = ()
    unknown: str = ""
    symbolic_all: bool = False
    scopes: frozenset[str] = frozenset()
    literals: frozenset[str] | None = frozenset()
    database_selectors: frozenset[tuple[str, int, int, str, str, str, int, int]] = frozenset()
    loop_source_keys: frozenset[str] = frozenset()

    @property
    def tracked(self) -> bool:
        return bool(self.source_keys or self.cells or self.symbolic_all or self.scopes)


@dataclass
class _AnalysisState:
    numbers: dict[int, _NumberValue]
    strings: dict[int, _StringValue]
    database_strings: dict[tuple[str, int, int, int], _StringValue]
    database_numbers: dict[tuple[str, int, int, int], _NumberValue] = field(
        default_factory=dict
    )
    dynamic_database_numbers: dict[tuple[str, int, int, str], _NumberValue] = field(
        default_factory=dict
    )
    dynamic_database_strings: dict[tuple[str, int, int, str], _StringValue] = field(
        default_factory=dict
    )
    unknown_scopes: frozenset[str] = frozenset()
    unknown_reasons: frozenset[str] = frozenset()

    def copy(self) -> "_AnalysisState":
        return _AnalysisState(
            dict(self.numbers),
            dict(self.strings),
            dict(self.database_strings),
            dict(self.database_numbers),
            dict(self.dynamic_database_numbers),
            dict(self.dynamic_database_strings),
            self.unknown_scopes,
            self.unknown_reasons,
        )


@dataclass
class _AnalysisAudit:
    transfers: dict[str, str]
    cfg: dict[str, str]
    cfg_edges: set[tuple[str, str]]
    calls: dict[str, tuple[str, tuple[str, ...]]]
    data_effects: dict[str, tuple[str, tuple[str, ...]]]

    @classmethod
    def empty(cls) -> "_AnalysisAudit":
        return cls({}, {}, set(), {}, {})


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
_CallArgumentPool = dict[str, tuple[_StringValue, ...]]


def _condition_operator(encoded: int) -> tuple[int, str | None, bool]:
    operators = {
        0x00: "equals",
        0x10: "not_equals",
        0x20: "contains",
        0x30: "starts_with",
        0x40: "ends_with",
    }
    flags = (encoded >> 24) & 0xFF
    return encoded & 0x00FFFFFF, operators.get(flags & 0xF0), (flags & 0x0F) == 1


def _limited(values: set[int]) -> frozenset[int] | None:
    return frozenset(values) if len(values) <= _VALUE_LIMIT else None


def _number_argument(
    raw: int, state: _AnalysisState, *, identity_scope: str = ""
) -> _NumberValue:
    if raw < 1_000_000:
        return _NumberValue(frozenset({raw}))
    return state.numbers.get(
        raw,
        _NumberValue(
            None,
            f"变量 {raw} 的数值来源未知",
            identity=(
                f"event-input:{identity_scope}:{raw}"
                if identity_scope
                else f"event-input:{raw}"
            ),
        ),
    )


def _number_offset_identity(value: _NumberValue, offset: int) -> str:
    if not value.identity:
        return ""
    if offset == 0:
        return value.identity
    return f"add:{value.identity}:{offset}"


def _loop_identity(left: _NumberValue | None, right: _NumberValue | None) -> str:
    if left is None or right is None or not left.identity or not right.identity:
        return ""
    prefix = f"add:{left.identity}:"
    if not right.identity.startswith(prefix):
        return ""
    try:
        step = int(right.identity[len(prefix):])
    except ValueError:
        return ""
    if step == 0:
        return ""
    return left.identity if left.identity.startswith("loop:") else f"loop:{left.identity}:{step}"


def _calculate_numbers(left: _NumberValue, right: _NumberValue, operator: int) -> _NumberValue:
    tracked = left.tracked or right.tracked
    identity = ""
    if operator == 0:
        if right.values is not None and len(right.values) == 1:
            identity = _number_offset_identity(left, next(iter(right.values)))
        elif left.values is not None and len(left.values) == 1:
            identity = _number_offset_identity(right, next(iter(left.values)))
    elif operator == 1 and right.values is not None and len(right.values) == 1:
        identity = _number_offset_identity(left, -next(iter(right.values)))
    if left.values is None or right.values is None:
        return _NumberValue(
            None, left.reason or right.reason or "数值运算来源未知", tracked, identity
        )
    output: set[int] = set()
    try:
        for a in left.values:
            for b in right.values:
                output.add({0: lambda: a + b, 1: lambda: a - b, 2: lambda: a * b,
                            3: lambda: a // b, 4: lambda: a % b}[operator]())
                if len(output) > _VALUE_LIMIT:
                    return _NumberValue(None, "数值集合超过 256 项", tracked, identity)
    except (KeyError, ZeroDivisionError):
        return _NumberValue(None, f"未支持或无效的数值运算 {operator}", tracked)
    return _NumberValue(frozenset(output), tracked=tracked, identity=identity)


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
        return _NumberValue(
            None,
            left.reason or right.reason,
            left.tracked or right.tracked,
            left.identity if left.identity == right.identity else "",
        )
    values = _limited(set(left.values) | set(right.values))
    return _NumberValue(
        values,
        "数值集合超过 256 项" if values is None else "",
        left.tracked or right.tracked,
        left.identity if left.identity == right.identity else "",
    )


def _number_semantic_key(value: _NumberValue) -> tuple[object, ...]:
    return value.values, value.tracked, value.identity


@lru_cache(maxsize=None)
def _address_variables_for_block(block: _CommandBlock) -> frozenset[int]:
    """Find numeric slots that can structurally reach a dynamic DB row selector."""
    variables: set[int] = set()
    for command in block.commands:
        if command.opcode != 250 or len(command.ints) not in {4, 5}:
            continue
        if command.ints[1] >= 1_000_000:
            variables.add(command.ints[1])
    changed = True
    while changed:
        changed = False
        for command in block.commands:
            if command.opcode == 121 and len(command.ints) >= 4:
                destination = command.ints[0] & 0x00FFFFFF
                if destination not in variables:
                    continue
                for raw in command.ints[1:3]:
                    if raw >= 1_000_000 and raw not in variables:
                        variables.add(raw)
                        changed = True
            elif command.opcode == 250 and len(command.ints) == 5:
                flags = command.ints[3]
                if not (flags >> 8 & 0x10):
                    continue
                destination = command.ints[4] & 0x00FFFFFF
                raw = command.ints[1]
                if destination in variables and raw >= 1_000_000 and raw not in variables:
                    variables.add(raw)
                    changed = True
    return frozenset(variables)


def _merge_strings(left: _StringValue | None, right: _StringValue | None) -> _StringValue | None:
    if left is None and right is None:
        return None
    if (
        left == right
        and left is not None
        and (left.symbolic_all or len(left.source_keys) + len(left.cells) <= _VALUE_LIMIT)
        and (left.literals is None or len(left.literals) <= _STRING_LITERAL_LIMIT)
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
            value.database_selectors,
            value.loop_source_keys,
        )
    keys = set(left.source_keys) | set(right.source_keys)
    cells = set(left.cells) | set(right.cells)
    symbolic_all = left.symbolic_all or right.symbolic_all
    scopes = left.scopes | right.scopes
    database_selectors = left.database_selectors | right.database_selectors
    loop_source_keys = left.loop_source_keys | right.loop_source_keys
    literals = (
        None
        if left.literals is None or right.literals is None
        else frozenset(set(left.literals) | set(right.literals))
    )
    if literals is not None and len(literals) > _STRING_LITERAL_LIMIT:
        literals = None
    if len(keys) + len(cells) > _VALUE_LIMIT and not symbolic_all:
        return _StringValue(
            trace=(left.trace + right.trace)[-_VALUE_LIMIT:],
            unknown="字符串来源集合超过 256 项",
            symbolic_all=True,
            scopes=scopes or frozenset({"project"}),
            literals=literals,
            database_selectors=database_selectors,
            loop_source_keys=loop_source_keys,
        )
    return _StringValue(
        frozenset(keys),
        frozenset(cells),
        tuple(dict.fromkeys(left.trace + right.trace))[-_VALUE_LIMIT:],
        (left.unknown if left.tracked else "") or (right.unknown if right.tracked else ""),
        symbolic_all,
        scopes,
        literals,
        database_selectors,
        loop_source_keys,
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
        value.database_selectors,
        value.loop_source_keys,
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
        result.database_numbers = {
            key: value
            for key in set(result.database_numbers) | set(state.database_numbers)
            if (
                value := _merge_numbers(
                    result.database_numbers.get(key), state.database_numbers.get(key)
                )
            ) is not None
        }
        result.dynamic_database_numbers = {
            key: value
            for key in set(result.dynamic_database_numbers) | set(state.dynamic_database_numbers)
            if (
                value := _merge_numbers(
                    result.dynamic_database_numbers.get(key),
                    state.dynamic_database_numbers.get(key),
                )
            ) is not None
        }
        result.dynamic_database_strings = {
            key: value
            for key in (
                set(result.dynamic_database_strings)
                | set(state.dynamic_database_strings)
            )
            if (
                value := _merge_strings(
                    result.dynamic_database_strings.get(key),
                    state.dynamic_database_strings.get(key),
                )
            ) is not None
        }
        result.unknown_scopes = result.unknown_scopes | state.unknown_scopes
        result.unknown_reasons = result.unknown_reasons | state.unknown_reasons
    return result


def _state_cache_key(state: _AnalysisState) -> tuple[object, ...]:
    local_numbers = tuple(
        sorted(
            (key, *_number_semantic_key(value))
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
    database_numbers = tuple(
        sorted((key, *_number_semantic_key(value)) for key, value in state.database_numbers.items())
    )
    dynamic_database_numbers = tuple(
        sorted(
            (key, *_number_semantic_key(value))
            for key, value in state.dynamic_database_numbers.items()
        )
    )
    dynamic_database_strings = tuple(
        sorted(
            (key, _string_semantic_key(value))
            for key, value in state.dynamic_database_strings.items()
        )
    )
    return (
        local_numbers,
        local_strings,
        database,
        database_numbers,
        dynamic_database_numbers,
        dynamic_database_strings,
    )


def _string_semantic_key(value: _StringValue) -> tuple[object, ...]:
    return (
        value.source_keys,
        value.cells,
        _string_value_status(value)[0],
        value.symbolic_all,
        value.scopes,
        value.literals,
        value.database_selectors,
    )


def _states_semantically_equal(left: _AnalysisState, right: _AnalysisState) -> bool:
    if left.unknown_scopes != right.unknown_scopes:
        return False
    if left.numbers.keys() != right.numbers.keys():
        return False
    if any(
        _number_semantic_key(value) != _number_semantic_key(right.numbers[key])
        for key, value in left.numbers.items()
    ):
        return False
    if left.strings.keys() != right.strings.keys() or any(
        _string_semantic_key(value) != _string_semantic_key(right.strings[key])
        for key, value in left.strings.items()
    ):
        return False
    if left.database_strings.keys() != right.database_strings.keys() or any(
        _string_semantic_key(value)
        != _string_semantic_key(right.database_strings[key])
        for key, value in left.database_strings.items()
    ):
        return False
    if left.database_numbers.keys() != right.database_numbers.keys() or any(
        _number_semantic_key(value) != _number_semantic_key(right.database_numbers[key])
        for key, value in left.database_numbers.items()
    ):
        return False
    if left.dynamic_database_numbers.keys() != right.dynamic_database_numbers.keys() or any(
        _number_semantic_key(value)
        != _number_semantic_key(right.dynamic_database_numbers[key])
        for key, value in left.dynamic_database_numbers.items()
    ):
        return False
    return left.dynamic_database_strings.keys() == right.dynamic_database_strings.keys() and not any(
        _string_semantic_key(value)
        != _string_semantic_key(right.dynamic_database_strings[key])
        for key, value in left.dynamic_database_strings.items()
    )


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


from wolf_semantics_engine import _BlockAnalyzer

def _analyze_blocks(
    blocks: Iterable[_CommandBlock],
    items: list[TranslationItem],
    databases: dict[str, dict[int, _DatabaseType]],
    candidate_values: dict[str, str] | None = None,
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    _AnalysisAudit,
    dict[str, object],
]:
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
    candidate_lookup = candidate_values or {}
    call_argument_pool: _CallArgumentPool = {}
    for block in blocks:
        for index, command in enumerate(block.commands):
            if command.opcode not in {210, 300} or len(command.ints) < 2:
                continue
            flags = command.ints[1]
            if flags & 0x01000000:
                continue
            target: _CommandBlock | None = None
            if command.opcode == 300 and command.strings:
                matches = common_by_name.get(command.strings[0], ())
                target = matches[0] if len(matches) == 1 else None
            elif command.opcode == 210:
                reference = command.ints[0]
                if 599_000 <= reference < 601_000 and block.event_type == "common":
                    target_id = block.event_id + reference - 600_100
                elif 500_000 <= reference < 600_000:
                    target_id = reference - 500_000
                else:
                    target_id = -1
                target = common_by_id.get(target_id)
            numeric_count = flags & 0x0F
            string_count = (flags >> 4) & 0x0F
            string_start = 2 + numeric_count
            string_end = string_start + string_count
            literal_offset = 1
            if (
                target is None
                or len(command.ints) != string_end
                or len(command.strings) < literal_offset + string_count
                or any(raw >= 1_000_000 for raw in command.ints[2:string_end])
            ):
                continue
            values: list[_StringValue] = []
            for string_index in range(string_count):
                text = command.strings[literal_offset + string_index]
                if _CSELF_REFERENCE_RE.search(text) or _STRING_REFERENCE_RE.search(text):
                    values = []
                    break
                keys = frozenset(
                    item.key
                    for item in _items_for_event_codes(
                        frozen_event_items,
                        block,
                        index + 1,
                        literal_offset + string_index,
                    )
                    if item.original == text
                )
                literals = frozenset(
                    candidate_lookup.get(key, text) for key in keys
                ) or frozenset({text})
                values.append(
                    _StringValue(
                        keys,
                        trace=(
                            f"{block.source} event={block.event_id} page={block.page} "
                            f"command={index + 1} call-argument={string_index}",
                        ),
                        literals=literals,
                    )
                )
            if len(values) != string_count:
                continue
            choice = command.ints[2] if numeric_count else 0
            command_id = (
                f"{block.source}:{block.event_type}:{block.event_id}:"
                f"{block.page}:{index + 1}"
            )
            # Keep each call site's provenance separate. A batch-level union can
            # make unrelated player-facing messages look like condition inputs.
            call_argument_pool[command_id] = tuple(values)
    event_scopes = _conservative_event_scopes(blocks, common_by_id, common_by_name)
    dependencies: list[dict[str, object]] = []
    unknown = Counter()
    locations: dict[tuple[int, str], list[str]] = {}
    call_cache: _CallCache = {}
    audit = _AnalysisAudit.empty()
    root_states: list[_AnalysisState] = []
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
            call_argument_pool=call_argument_pool,
            candidate_values=candidate_values,
            audit=audit,
        )
        block_dependencies, _block_blocking, block_unknown = analyzer.run()
        dependencies.extend(block_dependencies)
        root_states.append(analyzer.output_state)
        for warning in block_unknown:
            key = (int(warning["opcode"]), str(warning["shape"]))
            unknown[key] += int(warning["count"])
            locations.setdefault(key, []).extend(str(value) for value in warning["locations"])

    def persistent_state(states: Iterable[_AnalysisState]) -> _AnalysisState:
        merged = _merge_states(list(states))
        merged.numbers = {
            key: value
            for key, value in merged.numbers.items()
            if not 1_600_000 <= key < 1_600_100 and value.identity
        }
        merged.strings = {
            key: value
            for key, value in merged.strings.items()
            if not 1_600_000 <= key < 1_600_100
        }
        return merged

    # Persistent state may be written by one root event and consumed by another.
    # ponytail: Root writes are joined without an event-order model; scheduling
    # analysis can regain approvals if a project needs that precision.
    global_state = persistent_state(root_states)
    global_iterations = 0
    global_converged = True
    while (
        global_state.numbers
        or global_state.strings
        or global_state.database_strings
        or global_state.database_numbers
        or global_state.dynamic_database_numbers
        or global_state.dynamic_database_strings
    ):
        if global_iterations >= _GLOBAL_STRING_FLOW_MAX_ITERATIONS:
            global_converged = False
            break
        propagated: list[dict[str, object]] = []
        propagated_states: list[_AnalysisState] = []
        for block in blocks:
            analyzer = _BlockAnalyzer(
                block,
                databases,
                frozen_database_keys,
                frozen_event_items,
                common_by_id,
                common_by_name,
                event_scopes,
                call_cache={},
                call_argument_pool=call_argument_pool,
                candidate_values=candidate_values,
                audit=audit,
            )
            block_dependencies, _block_blocking, _block_unknown = analyzer.run(
                global_state
            )
            propagated.extend(block_dependencies)
            propagated_states.append(analyzer.output_state)
        global_iterations += 1
        next_global_state = persistent_state([*root_states, *propagated_states])
        if _states_semantically_equal(global_state, next_global_state):
            dependencies.extend(propagated)
            break
        global_state = next_global_state
    global_string_flow = {
        "converged": global_converged,
        "iterations": global_iterations,
        "variables": len(global_state.strings),
        "numbers": len(global_state.numbers),
        "database_cells": len(global_state.database_strings),
        "database_numbers": len(global_state.database_numbers),
        "dynamic_database_numbers": len(global_state.dynamic_database_numbers),
        "dynamic_database_strings": len(global_state.dynamic_database_strings),
        "max_iterations": _GLOBAL_STRING_FLOW_MAX_ITERATIONS,
    }
    warnings = [
        {"opcode": opcode, "shape": shape, "count": count, "locations": locations[(opcode, shape)][:5]}
        for (opcode, shape), count in sorted(unknown.items())
    ]
    def report_database_selectors(
        dependency: dict[str, object], field: str
    ) -> set[tuple[str, int, int, str, str, str, int, int]]:
        return {
            (
                str(item["database"]),
                int(item["type"]),
                int(item["field"]),
                str(item["selector"]),
                str(item["auto_file"]),
                str(item["event_type"]),
                int(item["event_id"]),
                int(item["page"]),
            )
            for item in dependency.get(field, ())
            if isinstance(item, dict)
            and all(
                key in item
                for key in (
                    "database", "type", "field", "selector", "auto_file",
                    "event_type", "event_id", "page",
                )
            )
        }

    merged_dependencies: dict[tuple[object, ...], dict[str, object]] = {}
    for dependency in dependencies:
        # ponytail: retain source correlation in the report. Collapsing every
        # invocation of one callee into a single union makes display arguments
        # look like each other's logic inputs; a compact provenance table can
        # replace these per-source records if a project makes reports too large.
        provenance = (
            tuple(map(str, dependency.get("condition_keys", ()))),
            tuple(map(str, dependency.get("source_keys", ()))),
            tuple(map(str, dependency.get("right_source_keys", ()))),
            tuple(map(str, dependency.get("left_values", ()))),
            tuple(map(str, dependency.get("right_values", ()))),
        )
        identity = (
            dependency["auto_file"], dependency["event_type"], dependency["event_id"],
            dependency["page"], dependency["command"], dependency["string_index"],
            provenance,
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
            current["_target_database_cells"] = {
                (cell["database"], cell["type"], cell["data"], cell["field"])
                for cell in dependency.get("target_database_cells", [])
            }
            current["_database_selectors"] = report_database_selectors(
                dependency, "database_selectors"
            )
            current["_right_database_selectors"] = report_database_selectors(
                dependency, "right_database_selectors"
            )
            current["_target_database_selectors"] = report_database_selectors(
                dependency, "target_database_selectors"
            )
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
        current["_target_database_cells"].update(
            (cell["database"], cell["type"], cell["data"], cell["field"])
            for cell in dependency.get("target_database_cells", [])
        )
        current["_database_selectors"].update(
            report_database_selectors(dependency, "database_selectors")
        )
        current["_right_database_selectors"].update(
            report_database_selectors(dependency, "right_database_selectors")
        )
        current["_target_database_selectors"].update(
            report_database_selectors(dependency, "target_database_selectors")
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
        if (
            current["kind"] == "condition"
            and current["status"] == "untracked"
            and current["_source_keys"]
        ):
            # Some paths enter with an external value while another root event
            # provides a tracked global value. Replay the tracked predicate;
            # the external path cannot change because it has no candidate text.
            current["status"] = "dynamic"
            current["reason"] = "条件变量同时存在入口值与可定位的全局写入路径"
    dependencies = []
    for current in merged_dependencies.values():
        current["condition_keys"] = sorted(current.pop("_condition_keys"))
        current["source_keys"] = sorted(current.pop("_source_keys"))
        current["right_source_keys"] = sorted(current.pop("_right_source_keys"))
        for field in (
            "database_cells",
            "right_database_cells",
            "target_database_cells",
        ):
            current[field] = [
                {"database": cell[0], "type": cell[1], "data": cell[2], "field": cell[3]}
                for cell in sorted(current.pop(f"_{field}"))
            ]
        for field in (
            "database_selectors",
            "right_database_selectors",
            "target_database_selectors",
        ):
            current[field] = [
                {
                    "database": item[0],
                    "type": item[1],
                    "field": item[2],
                    "selector": item[3],
                    "auto_file": item[4],
                    "event_type": item[5],
                    "event_id": item[6],
                    "page": item[7],
                }
                for item in sorted(current.pop(f"_{field}"))
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
    return dependencies, blocking, warnings, audit, global_string_flow


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

    def database_cells(
        dependency: dict[str, object], field: str
    ) -> set[tuple[str, int, int, int]]:
        cells: set[tuple[str, int, int, int]] = set()
        for cell in dependency.get(field, ()):
            if not isinstance(cell, dict) or not all(
                name in cell for name in ("database", "type", "data", "field")
            ):
                continue
            cells.add(
                (
                    str(cell["database"]),
                    int(cell["type"]),
                    int(cell["data"]),
                    int(cell["field"]),
                )
            )
        return cells

    def database_selectors(
        dependency: dict[str, object], field: str
    ) -> set[tuple[str, int, int, str, str, str, int, int]]:
        return {
            (
                str(item["database"]),
                int(item["type"]),
                int(item["field"]),
                str(item["selector"]),
                str(item["auto_file"]),
                str(item["event_type"]),
                int(item["event_id"]),
                int(item["page"]),
            )
            for item in dependency.get(field, ())
            if isinstance(item, dict)
            and all(
                name in item
                for name in (
                    "database", "type", "field", "selector", "auto_file",
                    "event_type", "event_id", "page",
                )
            )
        }

    def consumer_reference(dependency: dict[str, object]) -> dict[str, object]:
        return {
            field: dependency[field]
            for field in ("kind", "auto_file", "event_type", "event_id", "page", "command", "trace")
            if field in dependency
        }

    display_storage_cells: set[tuple[str, int, int, int]] = set()
    display_selectors: set[tuple[str, int, int, str, str, str, int, int]] = set()
    non_display_cells: set[tuple[str, int, int, int]] = set()
    non_display_selectors: set[tuple[str, int, int, str, str, str, int, int]] = set()
    display_cell_consumers: dict[tuple[str, int, int, int], list[dict[str, object]]] = {}
    display_selector_consumers: dict[tuple[str, int, int, str, str, str, int, int], list[dict[str, object]]] = {}
    non_display_cell_consumers: dict[tuple[str, int, int, int], list[dict[str, object]]] = {}
    non_display_selector_consumers: dict[tuple[str, int, int, str, str, str, int, int], list[dict[str, object]]] = {}
    for dependency in dependencies:
        kind = str(dependency.get("kind", "condition"))
        cells = database_cells(dependency, "database_cells")
        cells.update(database_cells(dependency, "right_database_cells"))
        selectors = database_selectors(dependency, "database_selectors")
        selectors.update(database_selectors(dependency, "right_database_selectors"))
        reference = consumer_reference(dependency)
        if kind == "display":
            display_storage_cells.update(cells)
            display_selectors.update(selectors)
            for cell in cells:
                display_cell_consumers.setdefault(cell, []).append(reference)
            for selector in selectors:
                display_selector_consumers.setdefault(selector, []).append(reference)
        elif kind in {"condition", "call", "resource", "database", "control_flow", "opaque"} and not (
            kind == "resource"
            and dependency.get("resource_role") == "database_string_write"
        ):
            non_display_cells.update(cells)
            non_display_selectors.update(selectors)
            for cell in cells:
                non_display_cell_consumers.setdefault(cell, []).append(reference)
            for selector in selectors:
                non_display_selector_consumers.setdefault(selector, []).append(reference)
    for block in blocks:
        for index, command in enumerate(block.commands, start=1):
            semantics = command_semantics(
                command.opcode, len(command.ints), len(command.strings)
            )
            roles = _command_string_roles(command, semantics)
            for string_index, text in enumerate(command.strings):
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
                matching_items: dict[str, TranslationItem] = {}
                for code in _event_codes(block, index, string_index):
                    for item in by_code.get(code.upper(), ()):
                        matching_items.setdefault(item.key, item)
                for item in matching_items.values():
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
        if (
            kind == "resource"
            and dependency.get("resource_role") == "database_string_write"
        ):
            usage = "display_storage"
            target_cells = database_cells(dependency, "target_database_cells")
            target_selectors = database_selectors(
                dependency, "target_database_selectors"
            )
            cells_proven = (
                bool(target_cells)
                and target_cells <= display_storage_cells
                and target_cells.isdisjoint(non_display_cells)
            )
            selectors_proven = (
                bool(target_selectors)
                and target_selectors <= display_selectors
                and target_selectors.isdisjoint(non_display_selectors)
            )
            dependency["display_consumers"] = [
                consumer
                for cell in sorted(target_cells)
                for consumer in display_cell_consumers.get(cell, ())
            ] + [
                consumer
                for selector in sorted(target_selectors)
                for consumer in display_selector_consumers.get(selector, ())
            ]
            dependency["non_display_consumers"] = [
                consumer
                for cell in sorted(target_cells)
                for consumer in non_display_cell_consumers.get(cell, ())
            ] + [
                consumer
                for selector in sorted(target_selectors)
                for consumer in non_display_selector_consumers.get(selector, ())
            ]
            dependency["display_sink_proven"] = cells_proven or selectors_proven
            if not dependency["display_sink_proven"]:
                dependency["display_sink_reason"] = (
                    "动态数据库地址存在非显示读取"
                    if (
                        target_cells & non_display_cells
                        or target_selectors & non_display_selectors
                    )
                    else "动态数据库地址没有可证明的显示读取"
                )
            if dependency["display_sink_proven"]:
                for key in dependency.get("source_keys", []):
                    usages.setdefault(str(key), set()).add("display_only")
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
        return len(command.ints) in {4, 5, 7}
    if command.opcode == 122:
        if len(command.ints) < 2:
            return False
        flags = command.ints[1]
        source_kind = flags & 0x0F
        assignment = (flags >> 8) & 0x0F
        return source_kind in {0, 1, 2, 3} and assignment in set(range(12))
    if command.opcode == 112:
        if not command.ints:
            return False
        count = command.ints[0] & 0x0F
        return (
            len(command.strings) >= count
            and len(command.ints) >= count + 1
            and all(
                _condition_operator(command.ints[index + 1])[1] is not None
                for index in range(count)
            )
        )
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
                "resolution": (
                    "exact"
                    if len(targets) == 1
                    else "exact_noop"
                    if command.opcode == 300
                    and command.strings
                    and not _CSELF_REFERENCE_RE.search(command.strings[0])
                    and not _STRING_REFERENCE_RE.search(command.strings[0])
                    and not targets
                    else "conservative"
                ),
                "conservative_scopes": (
                    []
                    if len(targets) == 1
                    else []
                    if command.opcode == 300
                    and command.strings
                    and not _CSELF_REFERENCE_RE.search(command.strings[0])
                    and not _STRING_REFERENCE_RE.search(command.strings[0])
                    and not targets
                    else ["common:*"]
                ),
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


def analyze_project(
    project: AutoProject,
    items: list[TranslationItem],
    editor: EditorInfo,
    *,
    input_hash: str,
    output_hash: str,
    candidate_values: dict[str, str] | None = None,
) -> SemanticSnapshot:
    database_types = project.database_index
    dependencies, blocking, warnings, audit, global_string_flow = _analyze_blocks(
        project.events, items, database_types, candidate_values
    )
    call_graph, event_summaries = _call_graph_report(list(project.events))
    usage_by_key, proven_display = _translation_usage_report(
        project.events, items, dependencies
    )
    command_records = [
        (
            f"{block.source}:{block.event_type}:{block.event_id}:{block.page}:{index + 1}",
            command,
        )
        for block in project.events
        for index, command in enumerate(block.commands)
    ]
    all_commands = [command for _command_id, command in command_records]
    for command_id, command in command_records:
        semantics = command_semantics(
            command.opcode, len(command.ints), len(command.strings)
        )
        if semantics is None or command_id in audit.transfers:
            continue
        audit.transfers[command_id] = "unreachable"
        if semantics.get("data_effects"):
            audit.data_effects[command_id] = ("exact", ("unreachable",))
    shape_missing = [
        command
        for command in all_commands
        if command_semantics(command.opcode, len(command.ints), len(command.strings))
        is None
    ]
    semantic_missing = [
        command
        for command_id, command in command_records
        if not _command_transfer_complete(command)
        or audit.transfers.get(command_id) == "opaque"
    ]
    control_records = [
        (command_id, command)
        for command_id, command in command_records
        if command.opcode in _CFG_CONTROL_OPCODES
    ]
    covered_control_commands = sum(
        audit.cfg.get(command_id) in {"exact", "conservative"}
        or command_id not in audit.transfers
        and command.opcode in _CFG_IMPLEMENTED_OPCODES
        and _command_transfer_complete(command)
        for command_id, command in control_records
    )
    call_edges = list(call_graph.get("edges", []))
    resolved_calls = sum(
        edge.get("resolution") in {"exact", "exact_noop"}
        for edge in call_edges
        if isinstance(edge, dict)
    )
    conservative_calls = sum(
        edge.get("resolution") == "conservative"
        and bool(edge.get("conservative_scopes"))
        for edge in call_edges
        if isinstance(edge, dict)
    )
    data_effect_records = [
        (command_id, command, semantics)
        for command_id, command in command_records
        if (
            (semantics := command_semantics(
                command.opcode, len(command.ints), len(command.strings)
            ))
            and semantics.get("data_effects")
        )
    ]
    covered_data_effects = sum(
        _command_transfer_complete(command)
        and audit.data_effects.get(command_id, ("opaque", ()))[0]
        in {"exact", "conservative"}
        for command_id, command, _semantics in data_effect_records
    )
    opaque_effects = [
        command_id
        for command_id, command in command_records
        if not _command_transfer_complete(command)
        or audit.transfers.get(command_id) == "opaque"
        or (
            (semantics := command_semantics(
                command.opcode, len(command.ints), len(command.strings)
            ))
            and semantics.get("data_effects")
            and audit.data_effects.get(command_id, ("opaque", ()))[0] == "opaque"
        )
    ]
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
    report = {
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
                "covered": len(all_commands) - len(shape_missing),
                "missing": len(shape_missing),
                "ratio": (
                    (len(all_commands) - len(shape_missing)) / len(all_commands)
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
                "control_commands": len(control_records),
                "covered": covered_control_commands,
                "missing": len(control_records) - covered_control_commands,
                "ratio": (
                    covered_control_commands / len(control_records)
                    if control_records else 1.0
                ),
            },
            "call_target_coverage": {
                "calls": len(call_edges),
                "exact": resolved_calls,
                "conservative": conservative_calls,
                "missing": len(call_edges) - resolved_calls - conservative_calls,
                "ratio": (
                    (resolved_calls + conservative_calls) / len(call_edges)
                    if call_edges else 1.0
                ),
            },
            "data_effect_coverage": {
                "commands": len(data_effect_records),
                "covered": covered_data_effects,
                "missing": len(data_effect_records) - covered_data_effects,
                "ratio": (
                    covered_data_effects / len(data_effect_records)
                    if data_effect_records else 1.0
                ),
            },
            "opaque_effects": len(opaque_effects),
            "opaque_locations": sorted(set(opaque_effects))[:50],
        },
        "input_hash": input_hash,
        "output_hash": output_hash,
        "counts": {
            key: dict(value) if isinstance(value, Mapping) else value
            for key, value in project.counts.items()
        },
        "databases": dict(project.database_report),
        "global_string_flow": global_string_flow,
        "dependencies": dependencies,
        "blocking_issues": blocking,
        "event_summaries": event_summaries,
        "call_graph": call_graph,
        "runtime_semantics": {
            "transfers": dict(sorted(audit.transfers.items())),
            "cfg": dict(sorted(audit.cfg.items())),
            "cfg_edges": [list(edge) for edge in sorted(audit.cfg_edges)],
            "calls": {
                key: {"status": value[0], "targets_or_scopes": list(value[1])}
                for key, value in sorted(audit.calls.items())
            },
            "data_effects": {
                key: {"status": value[0], "scopes": list(value[1])}
                for key, value in sorted(audit.data_effects.items())
            },
        },
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
    return SemanticSnapshot(project, report)


def analyze_auto_export(
    auto_dir: str | Path,
    items: list[TranslationItem],
    editor: EditorInfo,
    *,
    input_hash: str,
    candidate_values: dict[str, str] | None = None,
) -> dict[str, object]:
    """Compatibility entry point for a complete Auto export analysis."""
    root = Path(auto_dir).resolve()
    return analyze_project(
        parse_auto_project(root, editor.version),
        items,
        editor,
        input_hash=input_hash,
        output_hash=hash_directory(root),
        candidate_values=candidate_values,
    ).report


