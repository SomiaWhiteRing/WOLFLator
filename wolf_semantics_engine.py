from __future__ import annotations

import ntpath
import re
from collections import Counter, deque
from dataclasses import replace
from typing import Iterable

from models import TranslationItem
from wolf_auto import _Command, _CommandBlock, _DatabaseType
from wolf_command_catalog import command_semantics
from wolf_semantics import (
    _CALL_DEPTH_LIMIT,
    _CFG_CONTROL_OPCODES,
    _CFG_IMPLEMENTED_OPCODES,
    _CFG_STATE_VISIT_LIMIT,
    _GLOBAL_STRING_FLOW_MAX_ITERATIONS,
    _STRING_LITERAL_LIMIT,
    _VALUE_LIMIT,
    _AnalysisAudit,
    _AnalysisState,
    _CallArgumentPool,
    _CallCache,
    _CallSummary,
    _NumberValue,
    _StringValue,
    _address_variables_for_block,
    _block_map_id,
    _block_map_ids,
    _calculate_numbers,
    _command_string_roles,
    _condition_operator,
    _concat_literals,
    _event_code,
    _event_codes,
    _event_name_codes,
    _expand_string_references,
    _items_for_event_codes,
    _limited,
    _loop_identity,
    _merge_numbers,
    _merge_states,
    _merge_strings,
    _number_argument,
    _number_offset_identity,
    _number_semantic_key,
    _state_cache_key,
    _states_semantically_equal,
    _string_semantic_key,
    _string_reference_value,
    _string_value_status,
    _string_variable_for_escape,
    _with_literals,
)

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
        call_argument_pool: _CallArgumentPool | None = None,
        candidate_values: dict[str, str] | None = None,
        audit: _AnalysisAudit | None = None,
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
        self.call_argument_pool = (
            call_argument_pool if call_argument_pool is not None else {}
        )
        self.candidate_values = candidate_values or {}
        self.audit = audit if audit is not None else _AnalysisAudit.empty()
        self.dependencies: list[dict[str, object]] = []
        self.blocking: list[dict[str, object]] = []
        self.unknown = Counter()
        self.unknown_locations: dict[tuple[int, str], list[str]] = {}
        self._unknown_seen: set[tuple[int, str, str]] = set()
        self.summary_failed = ""
        self.output_state = _AnalysisState({}, {}, {})
        self._address_variables = _address_variables_for_block(block)
        labels: dict[str, list[int]] = {}
        for position, command in enumerate(block.commands):
            if command.opcode == 212 and len(command.strings) == 1:
                for label in self._candidate_literal_values(command, position, 0):
                    labels.setdefault(label, []).append(position)
        self.labels = {name: tuple(positions) for name, positions in labels.items()}
        self._condition_regions: dict[int, tuple[int, tuple[tuple[int, int], ...]]] = {}
        self._branch_exits: dict[int, int] = {}
        self._loop_ends: dict[int, int] = {}
        self._loop_starts: dict[int, int] = {}
        self._enclosing_loops: dict[int, tuple[int, int]] = {}
        self._index_control_flow()

    def _command_id(self, index: int) -> str:
        return f"{self.block.source}:{self.block.event_type}:{self.block.event_id}:{self.block.page}:{index + 1}"

    def _number(self, raw: int, state: _AnalysisState) -> _NumberValue:
        """Resolve ordinary numbers without widening call summaries by identity."""
        return _number_argument(raw, state)

    def _address_number(self, raw: int, state: _AnalysisState) -> _NumberValue:
        """Resolve a number that statically reaches a dynamic database address."""
        return _number_argument(
            raw,
            state,
            identity_scope=(
                f"{self.block.source}:{self.block.event_type}:"
                f"{self.block.event_id}:{self.block.page}"
            ),
        )

    def _record_call(
        self, command_id: str, status: str, targets: Iterable[str]
    ) -> None:
        previous = self.audit.calls.get(command_id)
        merged_targets = set(map(str, targets))
        if previous is not None:
            merged_targets.update(previous[1])
            if previous[0] != "exact":
                status = "conservative"
        merged = (status, tuple(sorted(merged_targets)))
        self.audit.calls[command_id] = merged
        self.audit.data_effects[command_id] = merged

    def _candidate_literal_values(
        self, command: _Command, index: int, string_index: int
    ) -> frozenset[str]:
        literal = command.strings[string_index] if string_index < len(command.strings) else ""
        values = {
            self.candidate_values.get(item.key, literal)
            for item in _items_for_event_codes(
                self.event_items, self.block, index + 1, string_index
            )
            if item.original == literal
        }
        return frozenset(values or {literal})

    def _index_control_flow(self) -> None:
        commands = self.block.commands
        for index, command in enumerate(commands):
            if command.opcode in {102, 111, 112}:
                closing = self._matching(index, len(commands), 499)
                if closing is None:
                    continue
                markers = tuple(
                    position
                    for position in range(index + 1, closing)
                    if commands[position].indent == command.indent
                    and commands[position].opcode in {401, 402, 420, 421}
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
            resource_values = sorted(value.literals or ())
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
                    "literal": (
                        resource_values[0]
                        if len(resource_values) == 1
                        else command.strings[string_index]
                    ),
                    "resource_values": resource_values,
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
                "database_selectors": [
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
                    for item in sorted(value.database_selectors)
                ],
                "right_database_selectors": [],
                "target_database_selectors": [],
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
        global_string_variable: int | None = None,
        target_database_cells: Iterable[tuple[str, int, int, int]] = (),
        target_database_selectors: Iterable[
            tuple[str, int, int, str, str, str, int, int]
        ] = (),
        resource_path_values: frozenset[str] | None = None,
    ) -> None:
        if not value.tracked and role not in {
            "file_path_runtime_read",
            "file_path_runtime_write",
            "file_content_runtime_write",
        }:
            return
        source_keys = value.source_keys
        if role == "file_content_runtime_write":
            source_keys = source_keys | value.loop_source_keys
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
            "source_keys": sorted(source_keys),
            "right_source_keys": [],
            "database_cells": [
                {"database": cell[0], "type": cell[1], "data": cell[2], "field": cell[3]}
                for cell in sorted(value.cells)
            ],
            "right_database_cells": [],
            "target_database_cells": [
                {"database": cell[0], "type": cell[1], "data": cell[2], "field": cell[3]}
                for cell in sorted(target_database_cells)
            ],
            "database_selectors": [
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
                for item in sorted(value.database_selectors)
            ],
            "right_database_selectors": [],
            "target_database_selectors": [
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
                for item in sorted(target_database_selectors)
            ],
            "trace": list(value.trace),
            "right_trace": [],
            "unresolved_scopes": sorted(value.scopes | scopes),
            "status": status,
            "reason": reason,
            "resource_role": role,
            "global_string_variable": global_string_variable,
            "source_values": (
                sorted(value.literals) if value.literals is not None else None
            ),
            "resource_path_values": (
                sorted(resource_path_values)
                if resource_path_values is not None
                else None
            ),
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
        call_target_kind: str | None = None,
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
        if call_target_kind is not None:
            dependency["call_target_kind"] = call_target_kind
        self.dependencies.append(dependency)
        if status == "blocking":
            self.blocking.append(dependency)

    def _type_ids(self, database: str, command: _Command, flags: int, state: _AnalysisState) -> set[int] | None:
        types = self.databases.get(database, {})
        if flags & 0x01:
            name = command.strings[1] if len(command.strings) > 1 else ""
            return {type_id for type_id, item in types.items() if item.name == name}
        value = self._number(command.ints[0], state)
        if value.values is None:
            return None
        selected = set(value.values)
        if any(type_id < 0 for type_id in selected):
            return None
        return selected & set(types)

    def _selector(
        self, raw: int, state: _AnalysisState, *, unknown_means_all: bool
    ) -> set[int] | None:
        value = self._number(raw, state)
        if value.values is None:
            return set() if unknown_means_all else None
        return set(value.values)

    def _dynamic_database_selectors(
        self,
        database: str,
        type_ids: Iterable[int],
        field_ids: Iterable[int],
        data_raw: int,
        state: _AnalysisState,
    ) -> frozenset[tuple[str, int, int, str, str, str, int, int]]:
        """Return a canonical selector only when its numeric source is exact."""
        value = self._address_number(data_raw, state)
        if not value.identity:
            return frozenset()
        return frozenset(
            (
                database,
                type_id,
                field_id,
                value.identity,
                "",
                "address-expression",
                -1,
                -1,
            )
            for type_id in type_ids
            for field_id in field_ids
        )

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
        write_value = (
            state.strings.get(command.ints[4] & 0x00FFFFFF)
            if len(command.ints) == 5
            else None
        )
        is_read = len(command.ints) == 5 and (
            bool(byte1 & 0x10)
            or (not any(command.strings) and write_value is None)
        )
        if not is_read:
            write_type_ids = self._type_ids(database, command, byte2, state)
            write_field_ids = (
                {
                    field_id
                    for type_id in write_type_ids or ()
                    for field_id, name in self.databases[database][type_id].field_names.items()
                    if name == (command.strings[3] if len(command.strings) > 3 else "")
                }
                if byte2 & 0x04
                else self._selector(command.ints[2], state, unknown_means_all=False) or set()
            )
            string_write = any(
                self.databases[database][type_id].field_types.get(field_id, 0) >= 2000
                for type_id in write_type_ids or ()
                for field_id in write_field_ids
            )
            if string_write or write_value is not None:
                self._write_database_string(
                    command, index, state, database, byte2, write_value
                )
            elif write_type_ids and write_field_ids:
                self._write_database_number(command, state, database, byte2)
            else:
                scopes = frozenset(
                    f"database:{database}:{type_id}:*:*"
                    for type_id in (write_type_ids or self.databases.get(database, {}))
                ) or frozenset({f"database:{database}:*:*:*"})
                # Numeric DB writes cannot carry translated strings, but their
                # affected range is still part of the side-effect ledger.
                if not write_type_ids:
                    self.audit.data_effects[self._command_id(index)] = (
                        "conservative",
                        tuple(sorted(scopes)),
                    )
            return
        # In the 3.713 Auto form, byte1 bit 0x10 marks a database read. The
        # otherwise identical five-integer form writes its final string slot.
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
                        names.discard(db_type.data_names[data_id])
                        names.update(
                            self.candidate_values.get(
                                key, db_type.data_names[data_id]
                            )
                            for key in coordinate_keys
                        )
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
                literals=(
                    frozenset(names)
                    if len(names) <= _STRING_LITERAL_LIMIT
                    else None
                ),
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
        numeric_coordinates: set[tuple[str, int, int, int]] = set()
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
                            cells.add(coordinate)
                            cells.update(runtime_value.cells)
                            if runtime_value.literals is not None:
                                string_values.update(runtime_value.literals)
                        elif coordinate_keys:
                            cells.add(coordinate)
                            keys.update(coordinate_keys)
                            original_value = db_type.rows[data_id][field_id]
                            string_values.update(
                                self.candidate_values.get(key, original_value)
                                for key in coordinate_keys
                            )
                        else:
                            # Keep the field identity even when its current
                            # value is not a workbook item: a runtime writer
                            # may feed this exact storage slot to a display.
                            cells.add(coordinate)
                            string_values.add(db_type.rows[data_id][field_id])
                    else:
                        numeric_coordinates.add(coordinate)
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
            database_selectors = (
                self._dynamic_database_selectors(
                    database, type_ids, field_ids, data_raw, state
                )
                if data_all
                else frozenset()
            )
            selector_identity = (
                self._address_number(data_raw, state).identity if data_all else ""
            )
            runtime_value: _StringValue | None = None
            if selector_identity:
                for type_id in type_ids:
                    for field_id in field_ids:
                        stored = state.dynamic_database_strings.get(
                            (database, type_id, field_id, selector_identity)
                        )
                        if stored is not None:
                            runtime_value = _merge_strings(runtime_value, stored) or stored
            if runtime_value is not None:
                # A matching dynamic write overwrites the selected row. Do not
                # union unrelated static rows merely because the row number is
                # unknown to the static analyzer.
                state.strings[destination] = _StringValue(
                    runtime_value.source_keys,
                    runtime_value.cells,
                    tuple(dict.fromkeys((*runtime_value.trace, *trace))),
                    runtime_value.unknown,
                    runtime_value.symbolic_all,
                    runtime_value.scopes,
                    runtime_value.literals,
                    runtime_value.database_selectors | database_selectors,
                    runtime_value.loop_source_keys,
                )
                return
            if len(keys) + len(cells) > _VALUE_LIMIT:
                state.strings[destination] = _StringValue(
                    trace=trace,
                    unknown="数据库字符串来源集合超过 256 项",
                    symbolic_all=True,
                    scopes=scopes,
                    literals=None,
                    database_selectors=database_selectors,
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
                        if len(string_values) <= _STRING_LITERAL_LIMIT
                        else None
                    ),
                    database_selectors=database_selectors,
                )
        elif numeric_values:
            values = _limited(numeric_values)
            selector_identity = (
                self._address_number(data_raw, state).identity if data_all else ""
            )
            runtime_values = [
                state.dynamic_database_numbers[(database, type_id, field_id, selector_identity)]
                for type_id in type_ids
                for field_id in field_ids
                if selector_identity
                and (database, type_id, field_id, selector_identity)
                in state.dynamic_database_numbers
            ]
            if not data_all:
                runtime_values.extend(
                    state.database_numbers[coordinate]
                    for coordinate in numeric_coordinates
                    if coordinate in state.database_numbers
                )
            if runtime_values:
                state.numbers[destination] = _merge_states(
                    [
                        _AnalysisState({destination: value}, {}, {})
                        for value in runtime_values
                    ]
                ).numbers[destination]
            else:
                identity = (
                    f"database-read:{database}:{next(iter(type_ids))}:"
                    f"{next(iter(field_ids))}:{selector_identity}"
                    if destination in self._address_variables
                    and data_all
                    and selector_identity
                    and len(type_ids) == len(field_ids) == 1
                    else ""
                )
                state.numbers[destination] = _NumberValue(
                    values,
                    "数据库数值集合超过 256 项" if values is None else "",
                    True,
                    identity,
                )

    def _write_database_number(
        self,
        command: _Command,
        state: _AnalysisState,
        database: str,
        selector_flags: int,
    ) -> None:
        type_ids = self._type_ids(database, command, selector_flags, state)
        if not type_ids or len(command.ints) < 5:
            return
        data_ids = (
            {
                data_id
                for type_id in type_ids
                for data_id, name in enumerate(self.databases[database][type_id].data_names)
                if name == (command.strings[2] if len(command.strings) > 2 else "")
            }
            if selector_flags & 0x02
            else self._selector(command.ints[1], state, unknown_means_all=False) or set()
        )
        field_ids = (
            {
                field_id
                for type_id in type_ids
                for field_id, name in self.databases[database][type_id].field_names.items()
                if name == (command.strings[3] if len(command.strings) > 3 else "")
            }
            if selector_flags & 0x04
            else self._selector(command.ints[2], state, unknown_means_all=False) or set()
        )
        value = self._number(command.ints[4], state)
        if not value.identity:
            # ponytail: Literal numeric writes cannot preserve a dynamic address
            # relationship, so retaining them only multiplies call summaries.
            return
        for type_id in type_ids:
            for field_id in field_ids:
                if data_ids:
                    for data_id in data_ids:
                        state.database_numbers[(database, type_id, data_id, field_id)] = value
                    continue
                selector_identity = self._address_number(command.ints[1], state).identity
                if selector_identity:
                    state.dynamic_database_numbers[
                        (database, type_id, field_id, selector_identity)
                    ] = value

    def _write_database_string(
        self,
        command: _Command,
        index: int,
        state: _AnalysisState,
        database: str,
        selector_flags: int,
        value: _StringValue | None = None,
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
        database_selectors = (
            self._dynamic_database_selectors(
                database, type_ids, field_ids, command.ints[1], state
            )
            if not data_ids
            else frozenset()
        )
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
        if value is None:
            value = self._literal_string(command, index, 0, state)
        for coordinate in coordinates:
            state.database_strings[coordinate] = value
        for selector in database_selectors:
            state.dynamic_database_strings[selector[:4]] = value
        field_scopes = frozenset(
            f"database:{database}:{type_id}:*:{field_id}"
            for type_id in type_ids
            for field_id in field_ids
        )
        self._value_boundary_reference(
            command,
            index,
            value,
            "database_string_write",
            field_scopes
            if not coordinates
            else frozenset(
                f"database:{item[0]}:{item[1]}:{item[2]}:{item[3]}"
                for item in coordinates
            ),
            target_database_cells=coordinates,
            target_database_selectors=database_selectors,
        )

    def _set_runtime_value(
        self,
        command: _Command,
        index: int,
        state: _AnalysisState,
        *,
        string_result: bool | None,
    ) -> None:
        if not command.ints:
            self._record_unknown(command, index, "missing-destination")
            return
        destination = command.ints[0] & 0x00FFFFFF
        if string_result is not False:
            state.strings[destination] = _StringValue(
                trace=(self._location(index),),
                unknown=f"字符串由运行时命令 opcode={command.opcode} 取得",
                literals=None,
            )
        if string_result is not True:
            state.numbers[destination] = _NumberValue(
                None, f"数值由运行时命令 opcode={command.opcode} 取得"
            )

    def _download(
        self, command: _Command, index: int, state: _AnalysisState
    ) -> None:
        if len(command.ints) != 3 or len(command.strings) != 3:
            self._record_unknown(command, index, "invalid-260")
            return
        destination = command.ints[1]
        if destination >= 1_000_000:
            state.strings[destination & 0x00FFFFFF] = _StringValue(
                trace=(f"{self._location(index)} opcode=260 network response",),
                unknown="下载响应是运行时字符串",
                scopes=frozenset({"external:network"}),
                literals=None,
            )
        self.audit.data_effects[self._command_id(index)] = (
            "conservative",
            ("resource:network", "string"),
        )

    def _set_number(self, command: _Command, index: int, state: _AnalysisState) -> None:
        if len(command.ints) < 4:
            self._record_unknown(command, index, "invalid-121")
            return
        if len(command.ints) == 7:
            # ponytail: Editor's variable-target form can address any numeric
            # slot selected at runtime. Invalidating known numbers is the exact
            # safe abstraction; a decoded numeric address domain can narrow it.
            tracked = any(value.tracked for value in state.numbers.values())
            state.numbers = {
                variable: _NumberValue(
                    None,
                    "121 动态代入目标使当前数值变为运行时值",
                    value.tracked or tracked,
                )
                for variable, value in state.numbers.items()
            }
            self.audit.data_effects[self._command_id(index)] = (
                "conservative",
                ("number:*",),
            )
            return
        destination, left_raw, right_raw, flags = command.ints[:4]
        byte0 = flags & 0xFF
        byte1 = (flags >> 8) & 0xFF
        resolver = (
            self._address_number
            if destination in self._address_variables
            else self._number
        )
        left = resolver(left_raw, state)
        right = resolver(right_raw, state)
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
        if (
            destination in self._address_variables
            and not value.identity
            and value.values is not None
            and len(value.values) == 1
        ):
            value = replace(value, identity=f"const:{next(iter(value.values))}")
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
            for item in _items_for_event_codes(
                self.event_items, self.block, index + 1, string_index
            )
            if item.original == literal
        )
        if self.block.event_type == "common":
            source_scope = f"common:{self.block.event_id}"
        else:
            map_ids = ",".join(map(str, _block_map_ids(self.block)))
            source_scope = f"map:{map_ids}:{self.block.event_id}:{self.block.page}"
        candidate_literals = self._candidate_literal_values(
            command, index, string_index
        )
        value = _StringValue(
            keys,
            trace=(f"{self._location(index)} opcode={command.opcode} literal",),
            scopes=frozenset({source_scope}) if keys else frozenset(),
            literals=candidate_literals,
        )
        referenced = _string_reference_value(literal, state)
        if referenced is not None:
            value = _merge_strings(value, referenced) or value
        concrete_values: set[str] = set()
        concrete_known = True
        for candidate_literal in candidate_literals:
            expanded = _expand_string_references(
                frozenset({candidate_literal}), state
            )
            if expanded is None:
                concrete_known = False
                break
            concrete_values.update(expanded)
        concrete = frozenset(concrete_values) if concrete_known else None
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
            pointer = self._number(source_raw, state)
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
                current.database_selectors if current else frozenset(),
                current.loop_source_keys if current else frozenset(),
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
                merged.database_selectors,
                merged.loop_source_keys,
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
            content = derived(current, note="op=6 file-content")
            self._value_boundary_reference(
                command,
                index,
                value,
                "file_path_runtime_write",
            )
            self._value_boundary_reference(
                command,
                index,
                content,
                "file_content_runtime_write",
                resource_path_values=value.literals,
            )
            if current is not None:
                state.strings[destination] = current
        elif assignment == 9 and source_kind == 0:
            literal_keys = {
                item.key
                for string_index, literal in enumerate(command.strings)
                for item in _items_for_event_codes(
                    self.event_items, self.block, index + 1, string_index
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
                value.database_selectors,
                value.loop_source_keys,
            )
        result = state.strings.get(destination)
        if result is None:
            return
        writes_global = not 1_600_000 <= destination < 1_600_100
        if assignment in {3, 4} and source_kind == 1:
            source = source_raw & 0x00FFFFFF
            writes_global = writes_global or not 1_600_000 <= source < 1_600_100
        global_destinations = {destination}
        if assignment in {3, 4} and source_kind == 1:
            global_destinations.add(source_raw & 0x00FFFFFF)
        if writes_global:
            for global_destination in sorted(global_destinations):
                if not 1_600_000 <= global_destination < 1_600_100:
                    self._value_boundary_reference(
                        command,
                        index,
                        result,
                        "global_string_write",
                        global_string_variable=global_destination,
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
            condition_literal = self._literal_string(
                command, index, condition_index, state
            )
            literal_values = condition_literal.literals
            condition_keys = sorted(
                item.key
                for item in _items_for_event_codes(
                    self.event_items, self.block, index + 1, condition_index
                )
                if item.original == command.strings[condition_index]
            )
            value = state.strings.get(variable)
            literal = (
                next(iter(literal_values))
                if literal_values is not None and len(literal_values) == 1
                else command.strings[condition_index]
            )
            right_value: _StringValue | None = None
            if right_is_variable:
                right_index = count + 1 + condition_index
                right_variable = command.ints[right_index] & 0x00FFFFFF if right_index < len(command.ints) else -1
                right_value = state.strings.get(right_variable)
            if state.unknown_scopes:
                opaque = any(
                    marker in item
                    for item in state.unknown_reasons
                    for marker in ("未校准", "未支持", "不透明")
                )
                status = "blocking" if opaque else "dynamic"
                reason = (
                    "条件执行前经过可能读写字符串的不透明命令"
                    if opaque
                    else "条件执行前存在已保守定位的动态副作用"
                )
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
                "database_selectors": [
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
                    for item in sorted(value.database_selectors if value else ())
                ],
                "right_database_selectors": [
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
                    for item in sorted(
                        right_value.database_selectors if right_value else ()
                    )
                ],
                "target_database_selectors": [],
                "trace": list(value.trace if value else ()),
                "right_trace": list(right_value.trace if right_value else ()),
                "left_values": sorted(value.literals) if value and value.literals is not None else [],
                "right_values": (
                    sorted(right_value.literals)
                    if right_value and right_value.literals is not None
                    else sorted(literal_values or ())
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

    def _numeric_condition_truth(
        self, command: _Command, state: _AnalysisState
    ) -> bool | None:
        # Editor 3.713 pretty output confirms flag 2 means numeric equality.
        if len(command.ints) != 4 or command.ints[0] != 1 or command.ints[3] != 2:
            return None
        left = self._number(command.ints[1], state)
        right = self._number(command.ints[2], state)
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
                    current.database_selectors,
                    current.loop_source_keys,
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

    def _apply_conservative_scopes(
        self,
        state: _AnalysisState,
        scopes: frozenset[str],
        reason: str,
    ) -> None:
        if not scopes:
            scopes = frozenset({"project"})
        state.unknown_scopes = state.unknown_scopes | scopes
        state.unknown_reasons = state.unknown_reasons | frozenset({reason})
        affects_globals = "project" in scopes or any(
            scope.startswith("common:") for scope in scopes
        )
        if affects_globals:
            for variable, current in tuple(state.strings.items()):
                if 1_600_000 <= variable < 1_600_100:
                    continue
                state.strings[variable] = _StringValue(
                    current.source_keys,
                    current.cells,
                    current.trace,
                    reason,
                    True,
                    current.scopes | scopes,
                    None,
                    current.database_selectors,
                    current.loop_source_keys,
                )
            for variable, current in tuple(state.numbers.items()):
                if 1_600_000 <= variable < 1_600_100:
                    continue
                state.numbers[variable] = _NumberValue(
                    None, reason, current.tracked
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
        call_target_kind: str | None = None,
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
            command,
            index,
            "call",
            reason,
            scopes,
            input_values,
            status=status,
            call_target_kind=call_target_kind,
        )
        if taint_state:
            self._apply_conservative_scopes(
                state,
                scopes,
                f"{self._location(index)} opcode={command.opcode}: {reason}",
            )
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
        self, command: _Command, index: int, state: _AnalysisState
    ) -> tuple[_CommandBlock, int | None] | None:
        if len(command.ints) < 2:
            return None
        if command.opcode == 300:
            if not command.strings:
                return None
            target_value = self._literal_string(command, index, 0, state)
            target_names = target_value.literals
            if target_names is None:
                referenced = _string_reference_value(command.strings[0], state)
                if referenced is not None:
                    target_names = referenced.literals
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
            self._number(command.ints[2], state)
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
        command_id = self._command_id(index)
        has_return = len(command.ints) >= 2 and bool(command.ints[1] & 0x01000000)
        resolved = self._call_target(command, index, state)
        if resolved is None:
            if command.opcode == 300 and command.strings:
                names = self._literal_string(command, index, 0, state).literals
                if names is not None and not any(
                    self.common_by_name.get(name) for name in names
                ):
                    # The official manual specifies that an invalid name does
                    # nothing. Old projects commonly retain optional calls.
                    self._record_call(command_id, "exact", ("noop",))
                    return
                value = self._literal_string(command, index, 0, state)
                self._blocking_scope_dependency(
                    command,
                    index,
                    "call",
                    "公共事件目标为运行时动态值，已保守保护全部公共事件范围",
                    frozenset({"common:*"}),
                    (value,),
                    status="dynamic",
                    call_target_kind="event_name",
                )
            elif command.opcode == 210 and command.ints and command.ints[0] < 0:
                self._record_call(command_id, "exact", ("noop",))
                return
            else:
                self._blocking_scope_dependency(
                    command,
                    index,
                    "call",
                    "公共事件目标为运行时动态值，已保守保护全部公共事件范围",
                    frozenset({"common:*"}),
                    status="dynamic",
                    call_target_kind="numeric_id",
                )
            self._record_call(command_id, "conservative", ("common:*",))
            self._set_unknown_target_return(command, state)
            return
        target, choice = resolved
        # Every common event is also analyzed independently. At a call site we
        # only need the callee's own text and actual argument provenance; DB and
        # global writes are covered by their value-boundary dependencies.
        target_scopes = frozenset({f"common:{target.event_id}"})
        self._record_call(command_id, "exact", (f"common:{target.event_id}",))
        if command.opcode == 300 and command.strings:
            target_value = self._literal_string(command, index, 0, state)
            if target_value.tracked:
                self._blocking_scope_dependency(
                    command,
                    index,
                    "call",
                    "公共事件名称已精确解析",
                    frozenset(),
                    (target_value,),
                    status="resolved",
                )
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
            self._record_call(command_id, "conservative", target_scopes)
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
            self._record_call(
                command_id,
                "conservative",
                self.event_scopes.get(target.event_id, target_scopes),
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
            self._record_call(
                command_id,
                "conservative",
                self.event_scopes.get(target.event_id, target_scopes),
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

        callee_state = _AnalysisState(
            {},
            {},
            dict(state.database_strings),
            dict(state.database_numbers),
            dict(state.dynamic_database_numbers),
            dict(state.dynamic_database_strings),
        )
        for offset, raw in enumerate(command.ints[2:string_start]):
            resolver = (
                self._address_number
                if 1_600_000 + offset in _address_variables_for_block(target)
                else self._number
            )
            callee_state.numbers[1_600_000 + offset] = resolver(raw, state)
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
                pooled = self.call_argument_pool.get(command_id, ())
                callee_state.strings[destination] = (
                    pooled[offset]
                    if offset < len(pooled)
                    else self._literal_string(
                        command,
                        index,
                        string_offset + offset,
                        state,
                    )
                )

        carries_persistent_state = (
            any(value.tracked for value in callee_state.strings.values())
            or any(value.identity for value in callee_state.numbers.values())
        )
        if not has_return and not carries_persistent_state:
            # ponytail: every public event is analyzed once on its own. Re-enter
            # only calls carrying translated provenance or an address expression;
            # root fixed-point propagation covers persistent DB state without
            # multiplying every unrelated call site.
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
                self.call_argument_pool,
                self.candidate_values,
                self.audit,
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
        state.database_numbers = dict(result.database_numbers)
        state.dynamic_database_numbers = dict(result.dynamic_database_numbers)
        state.dynamic_database_strings = dict(result.dynamic_database_strings)
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
        command_id = self._command_id(index)
        if not command.ints:
            self._record_call(command_id, "conservative", ("common:*",))
            self._unknown_call(
                command, index, state, "预约公共事件目标缺失", frozenset({"common:*"})
            )
            return
        reference = command.ints[0]
        if reference >= 1_000_000:
            self._record_call(command_id, "conservative", ("common:*",))
            self._unknown_call(
                command,
                index,
                state,
                "预约公共事件目标为运行时动态值",
                frozenset({"common:*"}),
                status="dynamic",
                call_target_kind="numeric_id",
            )
            return
        target_id = (
            reference - 500_000
            if 500_000 <= reference < 600_000
            else reference
        )
        target = self.common_by_id.get(target_id)
        if target is None:
            self._record_call(command_id, "exact", ("noop",))
            return
        self._record_call(command_id, "exact", (f"common:{target.event_id}",))
        # The target is fixed. Its delayed effects are analyzed in the target
        # event; attaching their broad scope here would taint unrelated text.

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
            status="dynamic",
        )
        self.audit.data_effects[self._command_id(index)] = (
            "conservative", tuple(sorted(scopes))
        )

    def _transform_database(
        self, command: _Command, index: int, state: _AnalysisState
    ) -> None:
        flags = command.ints[3] if len(command.ints) > 3 else 0
        selector_flags = (flags >> 16) & 0xFF
        database = {0: "CDB", 1: "SDB", 2: "UDB"}.get((flags >> 8) & 0x0F)
        if database is None:
            scopes = frozenset(
                {
                    "database:CDB:*:*:*",
                    "database:SDB:*:*:*",
                    "database:UDB:*:*:*",
                }
            )
        else:
            type_ids = self._type_ids(database, command, selector_flags, state)
            scopes = frozenset(
                f"database:{database}:{type_id}:*:*"
                for type_id in (type_ids or self.databases.get(database, {}))
            ) or frozenset({f"database:{database}:*:*:*"})
        values = tuple(
            self._literal_string(command, index, string_index, state)
            for string_index, text in enumerate(command.strings)
            if text
        )
        self._blocking_scope_dependency(
            command,
            index,
            "database",
            "数据库数据操作会重排或改写可定位的数据库范围",
            scopes,
            values,
            status="dynamic",
        )
        self.audit.data_effects[self._command_id(index)] = (
            "conservative",
            tuple(sorted(scopes)),
        )

    def _xy_array(
        self, command: _Command, index: int, state: _AnalysisState
    ) -> None:
        selector = (
            self._literal_string(command, index, 1, state)
            if len(command.strings) > 1 and command.strings[1]
            else _StringValue(literals=frozenset())
        )
        scopes = frozenset({"array:*"})
        if selector.tracked:
            self._blocking_scope_dependency(
                command,
                index,
                "database",
                "XY 数值数组名称来自可翻译字符串",
                scopes,
                (selector,),
                status="dynamic",
            )
        if len(command.ints) > 4:
            destination = command.ints[4] & 0x00FFFFFF
            state.numbers[destination] = _NumberValue(
                None, "XY 数值数组操作结果为运行时动态值", selector.tracked
            )
        self.audit.data_effects[self._command_id(index)] = (
            "conservative",
            tuple(sorted(scopes)),
        )

    def _transfer_command(
        self,
        index: int,
        state: _AnalysisState,
        exits: list[_AnalysisState] | None = None,
    ) -> bool:
        end = index + 1
        while index < end:
            command = self.block.commands[index]
            command_id = self._command_id(index)
            semantics = command_semantics(
                command.opcode, len(command.ints), len(command.strings)
            )
            if semantics is None:
                self.audit.transfers[command_id] = "opaque"
                self.audit.data_effects[command_id] = ("opaque", ("project",))
                self._record_unknown(command, index)
                self._taint_unknown(command, index, state)
                index += 1
                continue
            shape_key = (
                command.opcode,
                f"ints={len(command.ints)},strings={len(command.strings)}",
            )
            unknown_before = self.unknown[shape_key]
            self.audit.transfers[command_id] = str(semantics["transfer"])
            self._display_reference(command, index, state, semantics)
            self._resource_reference(command, index, state, semantics)
            if command.opcode == 121:
                self._set_number(command, index, state)
            elif command.opcode == 122:
                self._set_string(command, index, state)
            elif command.opcode == 123:
                self._set_runtime_value(command, index, state, string_result=False)
            elif command.opcode == 124:
                # Editor 3.713 exposes both numeric and string-valued queries.
                # Keeping both abstract destinations is conservative and avoids
                # guessing an uncalibrated runtime category's result type.
                self._set_runtime_value(command, index, state, string_result=None)
            elif command.opcode == 250:
                self._database(command, index, state)
            elif command.opcode == 251:
                self._import_database(command, index, state)
            elif command.opcode == 252:
                self._transform_database(command, index, state)
            elif command.opcode in {255, 257}:
                self._xy_array(command, index, state)
            elif command.opcode == 260:
                self._download(command, index, state)
            elif command.opcode == 221:
                self._set_runtime_value(
                    command,
                    index,
                    state,
                    string_result=None,
                )
            elif command.opcode == 112:
                self._condition(command, index, state)
            elif command.opcode == 111:
                pass
            elif command.opcode in {210, 300}:
                self._call_event(command, index, state)
            elif command.opcode == 211:
                self._reserve_event(command, index, state)
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
            if self.unknown[shape_key] > unknown_before:
                self.audit.transfers[command_id] = "opaque"
                self.audit.data_effects[command_id] = ("opaque", ("project",))
            elif semantics.get("data_effects") and command_id not in self.audit.data_effects:
                call = self.audit.calls.get(command_id)
                if call is not None:
                    self.audit.data_effects[command_id] = call
                else:
                    self.audit.data_effects[command_id] = (
                        "exact",
                        tuple(map(str, semantics["data_effects"])),
                    )
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
                    _loop_identity(left, right),
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
                database_selectors=value.database_selectors,
                loop_source_keys=value.loop_source_keys | value.source_keys,
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
        if (
            previous.database_numbers != current.database_numbers
            or previous.dynamic_database_numbers != current.dynamic_database_numbers
            or previous.dynamic_database_strings != current.dynamic_database_strings
        ):
            # ponytail: A loop-changing dynamic store has no bounded address
            # relation here; discard it rather than reusing a stale selector.
            merged.database_numbers = {}
            merged.dynamic_database_numbers = {}
            merged.dynamic_database_strings = {}
        return merged

    def _cfg_successors(
        self,
        index: int,
        limit: int,
        state: _AnalysisState,
        exits: list[_AnalysisState] | None,
    ) -> tuple[tuple[int | None, int], ...]:
        command = self.block.commands[index]
        if command.opcode in {102, 111, 112}:
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
            if command.opcode == 102:
                selected = branches
            elif truth is True:
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
            has_cancel = any(
                self.block.commands[branch_start].opcode == 421
                for branch_start, _branch_end in branches
            )
            if (
                command.opcode == 102 and not has_cancel
                or command.opcode != 102
                and ((truth is False and not selected) or (truth is None and not has_else))
            ):
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
                self._number(command.ints[0], state)
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
                scope = self._current_scope()
                self._blocking_scope_dependency(
                    command,
                    index,
                    "control_flow",
                    "循环控制由标签跳转进入，已保守终止当前事件路径",
                    scope,
                    status="dynamic",
                )
                command_id = self._command_id(index)
                self.audit.cfg[command_id] = "conservative"
                self.audit.cfg_edges.add((command_id, "CONSERVATIVE:event"))
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
            target_value = (
                self._literal_string(command, index, 0, state)
                if len(command.strings) == 1
                else _StringValue(literals=None)
            )
            target_literals = target_value.literals
            if target_literals == frozenset({"END"}):
                if exits is not None:
                    exits.append(state.copy())
                return ()
            target_names = target_literals
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
        processed: dict[tuple[int, int], _AnalysisState] = {}
        fallthrough: list[_AnalysisState] = []
        structural = {170, 171, 172, 173, 174, 175, 176, 179, 213}

        while pending:
            key = pending.popleft()
            index, limit = key
            current = states[key].copy()
            visits[key] += 1
            if visits[key] > _CFG_STATE_VISIT_LIMIT:
                previous = processed.get(key)
                if previous is not None:
                    # ponytail: widen only the hot join's changing domains. A
                    # path-sensitive BDD can replace this if coverage ever needs
                    # exact values across hundreds of incoming branches.
                    current = self._widen_back_edge(previous, current)
                    states[key] = current.copy()
                visits[key] = 0
            processed[key] = current.copy()

            command = self.block.commands[index]
            if command.opcode not in structural:
                self._transfer_command(index, current, exits)
            else:
                semantics = command_semantics(
                    command.opcode, len(command.ints), len(command.strings)
                )
                command_id = self._command_id(index)
                if semantics is None:
                    self.audit.transfers[command_id] = "opaque"
                    self.audit.data_effects[command_id] = ("opaque", ("project",))
                else:
                    self.audit.transfers[command_id] = str(semantics["transfer"])
            successors = self._cfg_successors(index, limit, current, exits)
            if command.opcode in _CFG_CONTROL_OPCODES:
                command_id = self._command_id(index)
                terminal = command.opcode in {172, 173, 174, 175} or (
                    command.opcode == 213 and command.strings == ("END",)
                )
                if successors or terminal:
                    self.audit.cfg[command_id] = "exact"
                elif self.audit.cfg.get(command_id) == "conservative":
                    pass
                elif command.opcode == 213 and self._current_scope():
                    self.audit.cfg[command_id] = "conservative"
                else:
                    self.audit.cfg[command_id] = "opaque"
                for successor, _successor_limit in successors:
                    target = "END" if successor is None else self._command_id(successor)
                    self.audit.cfg_edges.add((command_id, target))
                if terminal:
                    self.audit.cfg_edges.add((command_id, "END"))
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
        state.database_numbers = result.database_numbers
        state.dynamic_database_numbers = result.dynamic_database_numbers
        state.dynamic_database_strings = result.dynamic_database_strings
        state.unknown_scopes = result.unknown_scopes
        state.unknown_reasons = result.unknown_reasons
        return True

    def run(
        self, initial_state: _AnalysisState | None = None
    ) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
        initial_state = initial_state.copy() if initial_state is not None else _AnalysisState({}, {}, {})
        outputs: list[_AnalysisState] = []
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
                prefix_state = initial_state.copy()
                self._execute(0, first, prefix_state)
                outputs.append(prefix_state)
            for label_index, (start, choice) in enumerate(entry_labels):
                end = (
                    entry_labels[label_index + 1][0]
                    if label_index + 1 < len(entry_labels)
                    else len(self.block.commands)
                )
                state = initial_state.copy()
                state.numbers[1_600_000] = _NumberValue(frozenset({choice}))
                if dispatcher is not None:
                    self._execute(dispatcher, len(self.block.commands), state)
                else:
                    self._execute(start + 1, end, state)
                outputs.append(state)
        else:
            state = initial_state.copy()
            self._execute(0, len(self.block.commands), state)
            outputs.append(state)
        self.output_state = _merge_states(outputs) if outputs else initial_state
        # Commands excluded by a proven branch still need a syntactic CFG and
        # semantic ledger entry. They are safe because they are unreachable,
        # not because the coverage denominator forgot them.
        for index, command in enumerate(self.block.commands):
            command_id = self._command_id(index)
            semantics = command_semantics(
                command.opcode, len(command.ints), len(command.strings)
            )
            if semantics is None:
                continue
            if command_id not in self.audit.transfers:
                self.audit.transfers[command_id] = "unreachable"
                if semantics.get("data_effects"):
                    self.audit.data_effects[command_id] = (
                        "exact", ("unreachable",)
                    )
            if command.opcode not in _CFG_CONTROL_OPCODES or command_id in self.audit.cfg:
                continue
            state = _AnalysisState({}, {}, {})
            successors = self._cfg_successors(
                index, len(self.block.commands), state, None
            )
            terminal = command.opcode in {172, 173, 174, 175} or (
                command.opcode == 213 and command.strings == ("END",)
            )
            if successors or terminal:
                self.audit.cfg[command_id] = "exact"
            elif self.audit.cfg.get(command_id) == "conservative":
                pass
            elif command.opcode == 213 and self._current_scope():
                self.audit.cfg[command_id] = "conservative"
            else:
                self.audit.cfg[command_id] = "opaque"
            for successor, _successor_limit in successors:
                target = "END" if successor is None else self._command_id(successor)
                self.audit.cfg_edges.add((command_id, target))
            if terminal:
                self.audit.cfg_edges.add((command_id, "END"))
        warnings = [
            {"opcode": opcode, "shape": shape, "count": count,
             "locations": self.unknown_locations[(opcode, shape)][:5]}
            for (opcode, shape), count in sorted(self.unknown.items())
        ]
        return self.dependencies, self.blocking, warnings


