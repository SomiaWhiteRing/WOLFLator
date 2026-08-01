from __future__ import annotations

import hashlib
import re
from collections import Counter, deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable

from formats import ARTIFACT_EPOCH
from models import TranslationItem
from process_tools import hash_directory
from wolf_analysis import ANALYSIS_ENGINE
from wolf_auto import AutoProject, _Command, _CommandBlock, _DatabaseType, _database_index, _event_blocks
from wolf_command_catalog import VERIFIED_EDITOR_VERSION, command_semantics
from wolf_semantics_engine import (
    _AnalysisAudit, _AnalysisMetrics, _AnalysisState, _BlockAnalyzer, _BlockPlanCache,
    _CFG_CONTROL_OPCODES, _CFG_IMPLEMENTED_OPCODES, _CSELF_REFERENCE_RE,
    _CallArgumentPool, _CallCache, _EntryPlanCache, _EventItemCache,
    _ExternalTextSource, _PersistentReadCache, _STRING_REFERENCE_RE, _StringValue,
    _VALUE_LIMIT, _WOLF_PATH_REFERENCE_RE, _command_string_roles, _condition_operator,
    _event_codes, _items_for_event_codes, _map_ids_from_databases, _merge_numbers,
    _merge_states, _merge_strings, _normalize_external_path,
    _persistent_inputs_for_block, _state_has_values, _states_semantically_equal,
    _string_variable_for_escape,
)


_GLOBAL_STRING_FLOW_MAX_ITERATIONS = 32


_WORKBOOK_DB_CODE_RE = re.compile(r"^(?P<database>UDB|CDB|SDB)-(?P<type>\d+)-(?P<data>\d+)-(?P<field>\d+)$", re.IGNORECASE)


_EXTERNAL_FILE_CODE_RE = re.compile(
    r'^(?:SEGMENT_\d+-)?(?P<kind>TXTFILE|CSVFILE)-"(?P<path>.+)"$',
    re.IGNORECASE,
)


_EXTERNAL_SOURCE_PREFIX = "external-file:"


@dataclass(frozen=True)
class _CompiledAutoProgram:
    root: Path
    editor: EditorInfo
    input_hash: str
    project: AutoProject
    common_counts: dict[str, int]
    map_counts: dict[str, int]
    database_counts: dict[str, dict[str, int]]
    database_types: dict[str, dict[int, _DatabaseType]]
    database_report: dict[str, object]
    output_hash: str


def _external_text_sources(items: list[TranslationItem]) -> tuple[_ExternalTextSource, ...]:
    groups: dict[str, list[TranslationItem]] = {}
    paths: dict[str, str] = {}
    for item in items:
        match = _EXTERNAL_FILE_CODE_RE.fullmatch(item.code)
        if match is None:
            continue
        path = _normalize_external_path(match.group("path"))
        groups.setdefault(path, []).append(item)
        paths.setdefault(path, match.group("path").replace("\\", "/"))

    sources: list[_ExternalTextSource] = []
    for path, group in sorted(groups.items()):
        by_code = {item.code.upper(): item for item in group}
        roots = [item for item in group if not item.code.upper().startswith("SEGMENT_")]
        if len(roots) != 1 or len(by_code) != len(group):
            continue
        ordered: list[TranslationItem] = []
        seen: set[str] = set()
        current = roots[0]
        while current.code.upper() not in seen:
            seen.add(current.code.upper())
            ordered.append(current)
            match = re.search(r"(?:^|\r?\n)NEXT=([^\r\n]+)", current.flag, re.IGNORECASE)
            if match is None:
                break
            following = by_code.get(match.group(1).upper())
            if following is None:
                ordered = []
                break
            current = following
        if len(ordered) != len(group):
            continue
        digest = hashlib.sha256(path.encode("utf-8", "surrogatepass")).hexdigest()[:24]
        sources.append(
            _ExternalTextSource(
                paths[path],
                f"{_EXTERNAL_SOURCE_PREFIX}{digest}",
                tuple(ordered),
            )
        )
    return tuple(sources)


def _analyze_blocks(
    blocks: Iterable[_CommandBlock],
    items: list[TranslationItem],
    databases: dict[str, dict[int, _DatabaseType]],
    candidate_values: dict[str, str] | None = None,
    external_sources: tuple[_ExternalTextSource, ...] = (),
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
    persistent_read_cache: _PersistentReadCache = {}
    block_plan_cache: _BlockPlanCache = {}
    event_item_cache: _EventItemCache = {}
    entry_plan_cache: _EntryPlanCache = {}
    audit = _AnalysisAudit.empty()
    metrics = _AnalysisMetrics()
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
            persistent_read_cache=persistent_read_cache,
            metrics=metrics,
            block_plan_cache=block_plan_cache,
            event_item_cache=event_item_cache,
            entry_plan_cache=entry_plan_cache,
            external_sources=external_sources,
        )
        metrics.basic_blocks += len(analyzer._basic_block_starts)
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
            if not 1_600_000 <= key < 1_600_100 and value.identities
        }
        merged.strings = {
            key: value
            for key, value in merged.strings.items()
            if not 1_600_000 <= key < 1_600_100
        }
        return merged

    def accumulate_persistent_state(
        current: _AnalysisState, update: _AnalysisState
    ) -> _AnalysisState:
        merged = current.copy()

        def merge_values(target: dict, source: dict, merge: Callable) -> None:
            for key, value in source.items():
                combined = merge(target.get(key), value)
                if combined is not None:
                    target[key] = combined

        merge_values(
            merged.numbers,
            {
                key: value
                for key, value in update.numbers.items()
                if not 1_600_000 <= key < 1_600_100 and value.identities
            },
            _merge_numbers,
        )
        merge_values(
            merged.strings,
            {
                key: value
                for key, value in update.strings.items()
                if not 1_600_000 <= key < 1_600_100
            },
            _merge_strings,
        )
        merge_values(merged.database_strings, update.database_strings, _merge_strings)
        merge_values(merged.database_numbers, update.database_numbers, _merge_numbers)
        merge_values(
            merged.dynamic_database_numbers,
            update.dynamic_database_numbers,
            _merge_numbers,
        )
        merge_values(
            merged.dynamic_database_strings,
            update.dynamic_database_strings,
            _merge_strings,
        )
        merged.unknown_scopes |= update.unknown_scopes
        merged.unknown_reasons |= update.unknown_reasons
        return merged

    # Persistent state may be written by one root event and consumed by another.
    # ponytail: Root writes are joined without an event-order model; scheduling
    # analysis can regain approvals if a project needs that precision.
    global_state = persistent_state(root_states)
    global_iterations = 0
    global_converged = True
    propagated_by_block: dict[int, list[dict[str, object]]] = {}
    previous_inputs: dict[int, _AnalysisState] = {}
    block_evaluations = 0
    while _state_has_values(global_state):
        if global_iterations >= _GLOBAL_STRING_FLOW_MAX_ITERATIONS:
            global_converged = False
            break
        iteration_start = global_state
        next_global_state = global_state
        for block_index, block in enumerate(blocks):
            projected = _persistent_inputs_for_block(
                block,
                next_global_state,
                databases,
                read_cache=persistent_read_cache,
            )
            if not _state_has_values(projected):
                continue
            previous = previous_inputs.get(block_index)
            if previous is not None and _states_semantically_equal(previous, projected):
                continue
            if previous is not None:
                metrics.incremental_invalidations += 1
            previous_inputs[block_index] = projected.copy()
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
                persistent_read_cache=persistent_read_cache,
                metrics=metrics,
                block_plan_cache=block_plan_cache,
                event_item_cache=event_item_cache,
                entry_plan_cache=entry_plan_cache,
                external_sources=external_sources,
            )
            block_dependencies, _block_blocking, _block_unknown = analyzer.run(
                projected
            )
            propagated_by_block[block_index] = block_dependencies
            block_evaluations += 1
            next_global_state = accumulate_persistent_state(
                next_global_state, analyzer.output_state
            )
        global_iterations += 1
        global_state = next_global_state
        if _states_semantically_equal(iteration_start, global_state):
            for block_index in sorted(propagated_by_block):
                dependencies.extend(propagated_by_block[block_index])
            break
    global_string_flow = {
        "converged": global_converged,
        "iterations": global_iterations,
        "variables": len(global_state.strings),
        "numbers": len(global_state.numbers),
        "database_cells": len(global_state.database_strings),
        "database_numbers": len(global_state.database_numbers),
        "dynamic_database_numbers": len(global_state.dynamic_database_numbers),
        "dynamic_database_strings": len(global_state.dynamic_database_strings),
        "block_evaluations": block_evaluations,
        "max_iterations": _GLOBAL_STRING_FLOW_MAX_ITERATIONS,
        "metrics": {
            "basic_blocks": metrics.basic_blocks,
            "transfers": metrics.transfers,
            "merges": metrics.merges,
            "summary_hits": metrics.summary_hits,
            "summary_misses": metrics.summary_misses,
            "incremental_invalidations": metrics.incremental_invalidations,
        },
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
            tuple(map(str, dependency.get("left_templates", ()))),
            tuple(map(str, dependency.get("right_templates", ()))),
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
            current["_left_templates"] = set(dependency.get("left_templates", []))
            current["_right_templates"] = set(dependency.get("right_templates", []))
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
        current["_left_templates"].update(dependency.get("left_templates", []))
        current["_right_templates"].update(dependency.get("right_templates", []))
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
        for field in ("left_values", "right_values", "left_templates", "right_templates"):
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
    display_storage_fields: set[tuple[str, int, int]] = set()
    non_display_cells: set[tuple[str, int, int, int]] = set()
    non_display_selectors: set[tuple[str, int, int, str, str, str, int, int]] = set()
    non_display_fields: set[tuple[str, int, int]] = set()
    display_cell_consumers: dict[tuple[str, int, int, int], list[dict[str, object]]] = {}
    display_selector_consumers: dict[tuple[str, int, int, str, str, str, int, int], list[dict[str, object]]] = {}
    display_field_consumers: dict[tuple[str, int, int], list[dict[str, object]]] = {}
    non_display_cell_consumers: dict[tuple[str, int, int, int], list[dict[str, object]]] = {}
    non_display_selector_consumers: dict[tuple[str, int, int, str, str, str, int, int], list[dict[str, object]]] = {}
    non_display_field_consumers: dict[tuple[str, int, int], list[dict[str, object]]] = {}
    for dependency in dependencies:
        kind = str(dependency.get("kind", "condition"))
        cells = database_cells(dependency, "database_cells")
        cells.update(database_cells(dependency, "right_database_cells"))
        selectors = database_selectors(dependency, "database_selectors")
        selectors.update(database_selectors(dependency, "right_database_selectors"))
        fields = {
            (cell[0], cell[1], cell[3]) for cell in cells
        } | {
            (selector[0], selector[1], selector[2]) for selector in selectors
        }
        reference = consumer_reference(dependency)
        if kind == "display":
            display_storage_cells.update(cells)
            display_selectors.update(selectors)
            display_storage_fields.update(fields)
            for cell in cells:
                display_cell_consumers.setdefault(cell, []).append(reference)
            for selector in selectors:
                display_selector_consumers.setdefault(selector, []).append(reference)
            for field in fields:
                display_field_consumers.setdefault(field, []).append(reference)
        elif kind in {"condition", "call", "resource", "database", "control_flow", "opaque"} and not (
            kind == "resource"
            and dependency.get("resource_role") == "database_string_write"
        ):
            non_display_cells.update(cells)
            non_display_selectors.update(selectors)
            non_display_fields.update(fields)
            for cell in cells:
                non_display_cell_consumers.setdefault(cell, []).append(reference)
            for selector in selectors:
                non_display_selector_consumers.setdefault(selector, []).append(reference)
            for field in fields:
                non_display_field_consumers.setdefault(field, []).append(reference)
    for block in blocks:
        for index, command in enumerate(block.commands, start=1):
            if not command.strings:
                continue
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
            target_fields = {
                (cell[0], cell[1], cell[3]) for cell in target_cells
            } | {
                (selector[0], selector[1], selector[2])
                for selector in target_selectors
            }
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
            fields_proven = (
                bool(target_fields)
                and target_fields <= display_storage_fields
                and target_fields.isdisjoint(non_display_fields)
            )
            dependency["display_consumers"] = [
                consumer
                for cell in sorted(target_cells)
                for consumer in display_cell_consumers.get(cell, ())
            ] + [
                consumer
                for selector in sorted(target_selectors)
                for consumer in display_selector_consumers.get(selector, ())
            ] + [
                consumer
                for field in sorted(target_fields)
                for consumer in display_field_consumers.get(field, ())
            ]
            dependency["non_display_consumers"] = [
                consumer
                for cell in sorted(target_cells)
                for consumer in non_display_cell_consumers.get(cell, ())
            ] + [
                consumer
                for selector in sorted(target_selectors)
                for consumer in non_display_selector_consumers.get(selector, ())
            ] + [
                consumer
                for field in sorted(target_fields)
                for consumer in non_display_field_consumers.get(field, ())
            ]
            dependency["display_sink_proven"] = (
                cells_proven or selectors_proven or fields_proven
            )
            if cells_proven:
                dependency["display_sink_basis"] = "exact_cell"
            elif selectors_proven:
                dependency["display_sink_basis"] = "dynamic_selector"
            elif fields_proven:
                dependency["display_sink_basis"] = "database_field"
            else:
                dependency["display_sink_basis"] = ""
            if not dependency["display_sink_proven"]:
                dependency["display_sink_reason"] = (
                    "动态数据库地址存在非显示读取"
                    if (
                        target_cells & non_display_cells
                        or target_selectors & non_display_selectors
                        or target_fields & non_display_fields
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


def _external_text_flow_report(
    blocks: tuple[_CommandBlock, ...],
    external_sources: tuple[_ExternalTextSource, ...],
    databases: dict[str, dict[int, _DatabaseType]],
    dependencies: list[dict[str, object]],
) -> list[dict[str, object]]:
    if not external_sources:
        return []
    common_by_id = {
        block.event_id: block for block in blocks if block.event_type == "common"
    }
    common_by_name: dict[str, list[_CommandBlock]] = {}
    for block in common_by_id.values():
        common_by_name.setdefault(block.event_name, []).append(block)

    def database_signature(command: _Command) -> tuple[str, str, str, bool] | None:
        if command.opcode != 250 or len(command.ints) != 5:
            return None
        byte1 = (command.ints[3] >> 8) & 0xFF
        database = {0: "CDB", 1: "SDB", 2: "UDB"}.get(byte1 & 0x0F)
        if database is None or len(command.strings) < 4:
            return None
        return database, command.strings[1], command.strings[3], bool(byte1 & 0x10)

    def database_fields(signature: tuple[str, str, str]) -> set[tuple[str, int, int]]:
        database, type_name, field_name = signature
        return {
            (database, type_id, field_id)
            for type_id, db_type in databases.get(database, {}).items()
            if db_type.name == type_name
            for field_id, name in db_type.field_names.items()
            if name == field_name
        }

    def dependency_fields(dependency: dict[str, object]) -> set[tuple[str, int, int]]:
        fields = {
            (str(cell["database"]), int(cell["type"]), int(cell["field"]))
            for name in (
                "database_cells", "right_database_cells", "target_database_cells"
            )
            for cell in dependency.get(name, ())
            if isinstance(cell, dict)
            and all(key in cell for key in ("database", "type", "field"))
        }
        fields.update(
            (str(selector["database"]), int(selector["type"]), int(selector["field"]))
            for name in (
                "database_selectors", "right_database_selectors",
                "target_database_selectors",
            )
            for selector in dependency.get(name, ())
            if isinstance(selector, dict)
            and all(key in selector for key in ("database", "type", "field"))
        )
        return fields

    def exact_descendants(root: _CommandBlock) -> tuple[set[int], bool]:
        reached = {root.event_id}
        queue = deque((root,))
        dynamic = False
        while queue:
            block = queue.popleft()
            for command in block.commands:
                if command.opcode == 300 and command.strings:
                    targets = common_by_name.get(command.strings[0], ())
                    if len(targets) != 1:
                        dynamic = True
                        continue
                    target = targets[0]
                    if target.event_id not in reached:
                        reached.add(target.event_id)
                        queue.append(target)
                elif command.opcode == 210:
                    reference = command.ints[0] if command.ints else -1
                    target_id = reference - 500_000 if 500_000 <= reference < 600_000 else -1
                    target = common_by_id.get(target_id)
                    if target is None:
                        dynamic = True
                    elif target.event_id not in reached:
                        reached.add(target.event_id)
                        queue.append(target)
        return reached, dynamic

    def dispatcher_base(
        block: _CommandBlock, command_signature: tuple[str, str, str]
    ) -> int | None:
        for call_index, command in enumerate(block.commands):
            if command.opcode != 210 or not command.ints or command.ints[0] < 1_000_000:
                continue
            variable = command.ints[0] & 0x00FFFFFF
            added_reference = any(
                prior.opcode == 121
                and len(prior.ints) >= 4
                and prior.ints[0] == variable
                and prior.ints[1] == 500_000
                and ((prior.ints[3] >> 8) & 0x0F) == 1
                for prior in block.commands[:call_index]
            )
            added_current_event = any(
                prior.opcode == 124
                and len(prior.ints) >= 3
                and prior.ints[0] == variable
                and prior.ints[2] == 17
                and ((prior.ints[1] >> 8) & 0x0F) == 1
                for prior in block.commands[:call_index]
            )
            read_command_number = any(
                (signature := database_signature(prior)) is not None
                and signature[3]
                and signature[:3] == command_signature
                and prior.ints[4] == variable
                for prior in block.commands[:call_index]
            )
            if added_reference and added_current_event and read_command_number:
                return block.event_id
        return None

    reports: list[dict[str, object]] = []
    source_by_path = {
        _normalize_external_path(source.path): source for source in external_sources
    }
    for read in dependencies:
        paths = tuple(map(str, read.get("external_file_paths", ())))
        if read.get("resource_role") != "file_path_runtime_read" or not paths:
            continue
        block = next(
            (
                item
                for item in blocks
                if item.source == read.get("auto_file")
                and item.event_type == read.get("event_type")
                and item.event_id == read.get("event_id")
                and item.page == read.get("page")
            ),
            None,
        )
        command_index = int(read.get("command", 0)) - 1
        if block is None or not 0 <= command_index < len(block.commands):
            continue
        read_command = block.commands[command_index]
        if read_command.opcode != 122 or len(read_command.ints) < 2:
            continue
        content_variable = read_command.ints[0]
        replacements: list[tuple[str, str, int]] = []
        for command in block.commands[command_index + 1 :]:
            if (
                command.opcode == 122
                and len(command.ints) >= 2
                and command.ints[0] == content_variable
                and ((command.ints[1] >> 8) & 0x0F) == 9
                and len(command.strings) >= 2
                and (label := re.fullmatch(r"\D*(-?\d+)", command.strings[1]))
            ):
                replacements.append((command.strings[0], command.strings[1], int(label.group(1))))
        labels = {
            int(command.strings[0]): index
            for index, command in enumerate(block.commands)
            if command.opcode == 212
            and len(command.strings) == 1
            and re.fullmatch(r"-?\d+", command.strings[0])
        }
        if not replacements or not labels or not any(
            command.opcode == 213 and any(
                pattern.search(command.strings[0])
                for pattern in (_CSELF_REFERENCE_RE, _STRING_REFERENCE_RE)
            )
            for command in block.commands
            if command.strings
        ):
            continue
        jump_variables = {
            _string_variable_for_escape("cself", int(match.group(1)))
            for command in block.commands
            if command.opcode == 213 and command.strings
            if (match := _CSELF_REFERENCE_RE.fullmatch(command.strings[0]))
        }
        command_signatures = {
            signature[:3]
            for command in block.commands
            if (signature := database_signature(command)) is not None
            and not signature[3]
            and command.ints[4] in jump_variables
        }
        if len(command_signatures) != 1:
            continue
        command_signature = next(iter(command_signatures))
        callers = [
            caller
            for caller in common_by_id.values()
            if any(
                command.opcode == 300
                and command.strings
                and command.strings[0] == block.event_name
                for command in caller.commands
            )
            and dispatcher_base(caller, command_signature) is not None
        ]
        if len(callers) != 1:
            continue
        base = dispatcher_base(callers[0], command_signature)
        if base is None:
            continue

        mappings: list[dict[str, object]] = []
        label_positions = sorted((position, label) for label, position in labels.items())
        for marker, dispatch_token, label in replacements:
            label_position = labels.get(label)
            target = common_by_id.get(base + label)
            if (
                label_position is None
                or target is None
                or not target.event_name.startswith(dispatch_token)
                or len(target.event_name) > len(dispatch_token)
                and target.event_name[len(dispatch_token)].isdigit()
            ):
                continue
            end = next(
                (position for position, _value in label_positions if position > label_position),
                len(block.commands),
            )
            handler_commands = block.commands[label_position + 1 : end]
            string_variables = {content_variable}
            for command in handler_commands:
                if command.opcode != 122 or len(command.ints) < 3:
                    continue
                source_kind = command.ints[1] & 0x0F
                source = command.ints[2] & 0x00FFFFFF
                if source_kind == 1 and source in string_variables:
                    string_variables.add(command.ints[0])
            payload_signatures = {
                signature[:3]
                for command in handler_commands
                if (signature := database_signature(command)) is not None
                and not signature[3]
                and command.ints[4] in string_variables
            }
            if len(payload_signatures) != 1:
                continue
            payload_signature = next(iter(payload_signatures))
            payload_fields = database_fields(payload_signature)
            if not payload_fields or not any(
                (signature := database_signature(command)) is not None
                and signature[:3] == payload_signature
                and signature[3]
                for command in target.commands
            ):
                continue
            descendants, dynamic_calls = exact_descendants(target)
            relevant = [
                dependency
                for dependency in dependencies
                if dependency.get("event_type") == "common"
                and dependency.get("event_id") in descendants
                and dependency_fields(dependency) & payload_fields
            ]
            has_display = any(item.get("kind") == "display" for item in relevant)
            unsafe = dynamic_calls or any(
                item.get("kind") not in {"display", "condition", "flow", "state", "resource"}
                or item.get("kind") == "resource"
                and item.get("resource_role") != "database_string_write"
                for item in relevant
            )
            conditions = sorted(
                {
                    (str(item.get("operator", "unknown")), str(item.get("literal", "")))
                    for item in relevant
                    if item.get("kind") == "condition"
                }
            )
            mappings.append({
                "marker": marker,
                "dispatch_token": dispatch_token,
                "label": label,
                "target_event": target.event_id,
                "target_name": target.event_name,
                "safe_display_sink": has_display and not unsafe,
                "conditions": [list(condition) for condition in conditions],
            })

        section_prefixes = sorted({
            prefix
            for command in block.commands[command_index + 1 :]
            for literal in command.strings
            if (reference := _WOLF_PATH_REFERENCE_RE.search(literal)) is not None
            and (prefix := literal[: reference.start()])
        })
        structural_prefixes = sorted({
            literal
            for command in block.commands
            if command.opcode == 112
            for encoded, literal in zip(command.ints[1:], command.strings)
            if _condition_operator(encoded)[1] == "starts_with" and literal
        })
        for path in paths:
            source = source_by_path.get(_normalize_external_path(path))
            if source is None:
                continue
            reports.append({
                "path": source.path,
                "source_key": source.source_key,
                "item_keys": [item.key for item in source.items],
                "reader": {
                    "auto_file": block.source,
                    "event_type": block.event_type,
                    "event_id": block.event_id,
                    "page": block.page,
                    "command": command_index + 1,
                    "templates": list(read.get("source_templates") or ()),
                },
                "dispatcher_event": callers[0].event_id,
                "mappings": mappings,
                "section_prefixes": section_prefixes,
                "structural_prefixes": structural_prefixes,
            })
    return reports


def _external_text_observer_report(
    flows: list[dict[str, object]],
    dependencies: list[dict[str, object]],
) -> list[dict[str, object]]:
    flows_by_path: dict[str, list[dict[str, object]]] = {}
    primary_readers: set[tuple[object, ...]] = set()
    for flow in flows:
        path = _normalize_external_path(str(flow.get("path", "")))
        flows_by_path.setdefault(path, []).append(flow)
        reader = flow.get("reader", {})
        if isinstance(reader, dict):
            primary_readers.add((
                path,
                reader.get("auto_file"),
                reader.get("event_type"),
                reader.get("event_id"),
                reader.get("page"),
                reader.get("command"),
            ))

    observers: list[dict[str, object]] = []
    for read in dependencies:
        if read.get("resource_role") != "file_path_runtime_read":
            continue
        reader = {
            "auto_file": read.get("auto_file"),
            "event_type": read.get("event_type"),
            "event_id": read.get("event_id"),
            "page": read.get("page"),
            "command": read.get("command"),
        }
        trace = (
            f"{reader['auto_file']} event={reader['event_id']} page={reader['page']} "
            f"command={reader['command']} opcode=122 external-file-content"
        )
        for raw_path in read.get("external_file_paths", ()):
            path = _normalize_external_path(str(raw_path))
            path_flows = flows_by_path.get(path, ())
            identity = (path, *reader.values())
            if not path_flows or identity in primary_readers:
                continue
            source_keys = {
                str(flow.get("source_key", "")) for flow in path_flows
            } - {""}
            prefixes = {
                str(prefix)
                for flow in path_flows
                for prefix in flow.get("section_prefixes", ())
                if prefix
            }
            uses = [
                dependency
                for dependency in dependencies
                if trace in dependency.get("trace", ())
                or trace in dependency.get("right_trace", ())
            ]
            predicates: list[dict[str, object]] = []
            for dependency in uses:
                left_sources = set(map(str, dependency.get("source_keys", ())))
                right_sources = set(map(str, dependency.get("right_source_keys", ())))
                templates = tuple(map(str, dependency.get("right_templates", ())))
                if (
                    dependency.get("kind") != "condition"
                    or dependency.get("operator") != "contains"
                    or not source_keys & left_sources
                    or source_keys & right_sources
                    or not templates
                    or any(
                        not any(template.startswith(prefix) for prefix in prefixes)
                        for template in templates
                    )
                ):
                    predicates = []
                    break
                predicates.append({
                    "operator": "contains",
                    "right_templates": list(templates),
                })
            if predicates:
                observers.append({
                    "path": str(raw_path),
                    "reader": reader,
                    "predicates": predicates,
                })
    return observers


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


def _compile_auto_program(
    auto_dir: str | Path,
    editor: EditorInfo,
    *,
    input_hash: str,
) -> _CompiledAutoProgram:
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
    for name, code in (
        ("DataBase", "UDB"),
        ("CDataBase", "CDB"),
        ("SysDataBase", "SDB"),
    ):
        path = root / "BasicData" / f"{name}.Auto.txt"
        if not path.is_file():
            continue
        index, counts = _database_index(path, code)
        database_types[code] = index
        database_report[code] = {
            str(type_id): {
                "name": item.name,
                "fields": {str(key): value for key, value in item.field_names.items()},
                "field_types": {
                    str(key): value for key, value in item.field_types.items()
                },
                "data_count": len(item.rows),
            }
            for type_id, item in index.items()
        }
        database_counts[name] = counts

    map_ids = _map_ids_from_databases(database_types)
    blocks = [
        replace(
            block,
            map_id=map_ids[block.source.casefold()][0],
            map_ids=map_ids[block.source.casefold()],
        )
        if block.event_type == "map" and block.source.casefold() in map_ids
        else block
        for block in blocks
    ]
    project = AutoProject(editor.version, tuple(blocks), tuple(sorted(database_types)))
    return _CompiledAutoProgram(
        root,
        editor,
        input_hash,
        project,
        common_counts,
        map_counts,
        database_counts,
        database_types,
        database_report,
        hash_directory(root),
    )


def _analyze_compiled_program(
    compiled: _CompiledAutoProgram,
    items: list[TranslationItem],
    candidate_values: dict[str, str] | None = None,
) -> dict[str, object]:
    root = compiled.root
    editor = compiled.editor
    input_hash = compiled.input_hash
    project = compiled.project
    common_counts = compiled.common_counts
    map_counts = compiled.map_counts
    database_counts = compiled.database_counts
    database_types = compiled.database_types
    database_report = compiled.database_report
    external_sources = _external_text_sources(items)
    dependencies, blocking, warnings, audit, global_string_flow = _analyze_blocks(
        project.events,
        items,
        database_types,
        candidate_values,
        external_sources,
    )
    call_graph, event_summaries = _call_graph_report(list(project.events))
    usage_by_key, proven_display = _translation_usage_report(
        project.events, items, dependencies
    )
    external_text_flows = _external_text_flow_report(
        project.events,
        external_sources,
        database_types,
        dependencies,
    )
    external_text_observers = _external_text_observer_report(
        external_text_flows, dependencies
    )
    external_readers: dict[str, set[tuple[object, ...]]] = {}
    for dependency in dependencies:
        if dependency.get("resource_role") != "file_path_runtime_read":
            continue
        reader = (
            dependency.get("auto_file"),
            dependency.get("event_type"),
            dependency.get("event_id"),
            dependency.get("page"),
            dependency.get("command"),
        )
        for path in dependency.get("external_file_paths", ()):
            external_readers.setdefault(_normalize_external_path(str(path)), set()).add(reader)
    modeled_external_readers: dict[str, set[tuple[object, ...]]] = {}
    for flow in external_text_flows:
        reader = flow.get("reader", {})
        if not isinstance(reader, dict):
            continue
        path = _normalize_external_path(str(flow.get("path", "")))
        modeled_external_readers.setdefault(path, set()).add((
            reader.get("auto_file"),
            reader.get("event_type"),
            reader.get("event_id"),
            reader.get("page"),
            reader.get("command"),
        ))
    for observer in external_text_observers:
        reader = observer.get("reader", {})
        if not isinstance(reader, dict):
            continue
        path = _normalize_external_path(str(observer.get("path", "")))
        modeled_external_readers.setdefault(path, set()).add((
            reader.get("auto_file"),
            reader.get("event_type"),
            reader.get("event_id"),
            reader.get("page"),
            reader.get("command"),
        ))
    external_text_flow_coverage = {
        path: {
            "readers": len(external_readers.get(path, ())),
            "modeled": len(modeled_external_readers.get(path, ())),
        }
        for path in sorted(external_readers.keys() | modeled_external_readers.keys())
    }
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
    return {
        "kind": "editor-analysis",
        "epoch": ARTIFACT_EPOCH,
        "engine": ANALYSIS_ENGINE,
        "metrics": dict(global_string_flow["metrics"]),
        "editor": {
            "path": str(editor.path),
            "version": editor.version,
            "sha256": editor.sha256,
        },
        "command_catalog": {
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
        "output_hash": compiled.output_hash,
        "counts": {
            "common_events": common_counts["events"],
            "common_pages": common_counts["pages"],
            "common_commands": common_counts["commands"],
            **{f"map_{key}": value for key, value in map_counts.items()},
            "database": database_counts,
        },
        "databases": database_report,
        "global_string_flow": global_string_flow,
        "external_text_flows": external_text_flows,
        "external_text_observers": external_text_observers,
        "external_text_flow_coverage": external_text_flow_coverage,
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


def analyze_auto_export(
    auto_dir: str | Path,
    items: list[TranslationItem],
    editor: EditorInfo,
    *,
    input_hash: str,
    candidate_values: dict[str, str] | None = None,
) -> dict[str, object]:
    compiled = _compile_auto_program(auto_dir, editor, input_hash=input_hash)
    return _analyze_compiled_program(compiled, items, candidate_values)


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
        elif scope.startswith("external:"):
            expected = _normalize_external_path(scope.split(":", 1)[1])
            for item in items:
                match = _EXTERNAL_FILE_CODE_RE.fullmatch(item.code)
                if match and _normalize_external_path(match.group("path")) == expected:
                    matched.add(item.key)
        else:
            matched.update(item.key for item in items)
        if cache is not None:
            cache[scope] = frozenset(matched)
        selected.update(matched)
    return selected
