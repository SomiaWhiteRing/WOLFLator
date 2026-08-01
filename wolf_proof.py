from __future__ import annotations

import ntpath
import re
from pathlib import Path
from typing import Iterable

from models import TranslationItem
from process_tools import hash_directory
from wolf_analysis import ANALYSIS_ENGINE, validate_editor_analysis
from wolf_semantics import _WORKBOOK_DB_CODE_RE, _external_text_sources, _scope_keys
from wolf_semantics_engine import _VALUE_LIMIT, _normalize_external_path
from wolf_workbook import _scan_control_tokens


def _safety_predicate(operator: str, left: str, right: str) -> bool:
    if operator == "equals":
        return left == right
    if operator == "not_equals":
        return left != right
    if operator == "contains":
        return right in left
    if operator == "starts_with":
        return left.startswith(right)
    if operator == "ends_with":
        return left.endswith(right)
    raise ValueError(f"Editor 分析报告包含未知字符串比较操作符：{operator}")


def _condition_truth_signature(dependency: dict[str, object]) -> tuple[object, ...]:
    operator = str(dependency.get("operator", ""))
    if operator not in {"equals", "not_equals", "contains", "starts_with", "ends_with"}:
        return ("opaque", operator)
    left = tuple(map(str, dependency.get("left_values", ())))
    right = tuple(map(str, dependency.get("right_values", ())))
    if not right and not dependency.get("right_is_variable"):
        right = (str(dependency.get("literal", "")),)
    if not left or not right:
        return ("dynamic", operator)
    return (
        "values",
        operator,
        tuple(
            sorted(
                {
                    _safety_predicate(operator, left_value, right_value)
                    for left_value in left
                    for right_value in right
                }
            )
        ),
    )


def _semantic_replay_signature(report: dict[str, object]) -> dict[str, object]:
    runtime = report.get("runtime_semantics", {})
    if not isinstance(runtime, dict):
        raise ValueError("Editor 分析报告缺少运行语义账本。")
    dependencies = report.get("dependencies", [])
    if not isinstance(dependencies, list):
        raise ValueError("Editor 分析报告缺少依赖记录。")
    conditions: list[tuple[object, ...]] = []
    resources: list[tuple[object, ...]] = []
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            raise ValueError("Editor 分析报告包含损坏的依赖记录。")
        identity = (
            dependency.get("auto_file"),
            dependency.get("event_type"),
            dependency.get("event_id"),
            dependency.get("page"),
            dependency.get("command"),
            dependency.get("string_index"),
        )
        kind = str(dependency.get("kind", ""))
        if kind == "condition":
            conditions.append((*identity, _condition_truth_signature(dependency)))
        elif kind == "resource":
            role = str(dependency.get("resource_role", ""))
            values = (
                dependency.get("resource_path_values", ())
                if role == "file_content_runtime_write"
                else dependency.get("resource_values", ())
            )
            resources.append((*identity, role, tuple(sorted(map(str, values or ())))))
    return {
        "cfg_edges": tuple(
            sorted(tuple(map(str, edge)) for edge in runtime.get("cfg_edges", ()))
        ),
        "calls": tuple(
            sorted(
                (
                    str(key),
                    str(value.get("status", "")),
                    tuple(map(str, value.get("targets_or_scopes", ()))),
                )
                for key, value in dict(runtime.get("calls", {})).items()
                if isinstance(value, dict)
            )
        ),
        "data_effects": tuple(
            sorted(
                (
                    str(key),
                    str(value.get("status", "")),
                    tuple(map(str, value.get("scopes", ()))),
                )
                for key, value in dict(runtime.get("data_effects", {})).items()
                if isinstance(value, dict)
            )
        ),
        "conditions": tuple(sorted(conditions, key=repr)),
        "resources": tuple(sorted(resources, key=repr)),
        "opaque_effects": int(
            dict(report.get("command_catalog", {})).get("opaque_effects", 0)
        ),
    }


def _analyze_compiled_translation_safety(
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
    validate_editor_analysis(analysis)
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

    direct_safe = {
        key
        for key in candidates
        if (uses := set(map(str, usage_by_key.get(key, ()))))
        and uses <= {"display_only", "display_storage", "logic", "event_target"}
        and ("display_storage" not in uses or "display_only" in uses)
        and (
            "event_target" not in uses
            or event_targets.get(originals[key]) == event_targets.get(candidates[key])
        )
    }
    catalog = analysis.get("command_catalog", {})
    coverage_fields = (
        "semantic_coverage",
        "cfg_coverage",
        "call_target_coverage",
        "data_effect_coverage",
    )
    complete_ledger = (
        isinstance(catalog, dict)
        and int(catalog.get("opaque_effects", 1)) == 0
        and all(
            isinstance(catalog.get(field), dict)
            and float(catalog[field].get("ratio", 0.0)) == 1.0
            for field in coverage_fields
        )
    )
    official_display = {
        item.key
        for item in items
        if item.key in candidates
        and getattr(item.category, "value", str(item.category)) == "display"
    }
    return _finish_compiled_translation_safety(
        items,
        candidates,
        analysis,
        policy=policy,
        originals=originals,
        event_targets=event_targets,
        direct_safe=direct_safe,
        complete_ledger=complete_ledger,
        official_display=official_display,
        usage_by_key=usage_by_key,
        dependencies=dependencies,
    )


def _finish_compiled_translation_safety(
    items: list[TranslationItem],
    candidate_values: dict[str, str],
    analysis: dict[str, object],
    *,
    policy: str,
    originals: dict[str, str],
    event_targets: dict[str, int],
    direct_safe: set[str],
    complete_ledger: bool,
    official_display: set[str],
    usage_by_key: dict[object, object],
    dependencies: list[object],
) -> dict[str, object]:
    candidates = candidate_values
    flows = analysis.get("external_text_flows")
    if not isinstance(flows, list):
        flows = []
    coverage = analysis.get("external_text_flow_coverage")
    if not isinstance(coverage, dict):
        coverage = {}
    sources = {
        _normalize_external_path(source.path): source
        for source in _external_text_sources(items)
    }
    safe: set[str] = set()
    rejected: dict[str, str] = {}
    external_overrides: dict[str, str] = {}

    def part_ranges(parts: list[str]) -> list[tuple[int, int]]:
        ranges: list[tuple[int, int]] = []
        offset = 0
        for part in parts:
            ranges.append((offset, offset + len(part)))
            offset += len(part)
        return ranges

    def line_ranges(text: str) -> tuple[list[str], list[tuple[int, int]]]:
        lines = text.splitlines(keepends=True)
        if not lines and text == "":
            return [], []
        if lines and sum(map(len, lines)) != len(text):
            lines.append(text[sum(map(len, lines)) :])
        ranges: list[tuple[int, int]] = []
        offset = 0
        for line in lines:
            ranges.append((offset, offset + len(line)))
            offset += len(line)
        return lines, ranges

    def overlapping_keys(
        line_range: tuple[int, int],
        ranges: list[tuple[int, int]],
        keys: list[str],
    ) -> set[str]:
        start, end = line_range
        return {
            key
            for key, (part_start, part_end) in zip(keys, ranges)
            if start < part_end and part_start < end
        }

    for flow in flows:
        if not isinstance(flow, dict):
            continue
        source = sources.get(_normalize_external_path(str(flow.get("path", ""))))
        if source is None or list(flow.get("item_keys", ())) != [
            item.key for item in source.items
        ]:
            continue
        changed_keys = {
            item.key
            for item in source.items
            if item.key in candidate_values
            and candidate_values[item.key]
            and candidate_values[item.key] != item.original
        }
        if not changed_keys:
            continue
        path = _normalize_external_path(source.path)
        path_coverage = coverage.get(path)
        if (
            not isinstance(path_coverage, dict)
            or not isinstance(path_coverage.get("readers"), int)
            or not isinstance(path_coverage.get("modeled"), int)
            or path_coverage["readers"] < 1
            or path_coverage["readers"] != path_coverage["modeled"]
        ):
            rejected.update(
                (key, "external_readers_not_fully_modeled") for key in changed_keys
            )
            continue
        mappings = {
            str(mapping.get("marker", "")): mapping
            for mapping in flow.get("mappings", ())
            if isinstance(mapping, dict) and mapping.get("marker")
        }
        markers = sorted(mappings, key=len, reverse=True)
        dispatch_prefixes = {
            match.group(1)
            for mapping in mappings.values()
            if (match := re.fullmatch(r"(\D*)-?\d+", str(mapping.get("dispatch_token", ""))))
            and match.group(1)
        }
        section_prefixes = tuple(map(str, flow.get("section_prefixes", ())))
        structural_prefixes = tuple(map(str, flow.get("structural_prefixes", ())))

        # ponytail: Only line-oriented external interpreters are approved here;
        # any mid-line marker effect is classified as structure and rejected.
        def looks_structural(line: str) -> bool:
            content = line.rstrip("\r\n")
            return (
                any(prefix in content for prefix in dispatch_prefixes)
                or any(prefix in content for prefix in section_prefixes)
                or any(content.startswith(prefix) for prefix in structural_prefixes)
            )

        original_parts = [item.original for item in source.items]
        candidate_parts: list[str] = []
        for item in source.items:
            candidate_part = candidate_values.get(item.key, item.original) or item.original
            original_item_lines = item.original.splitlines(keepends=True)
            candidate_item_lines = candidate_part.splitlines(keepends=True)
            if item.key in changed_keys and (
                re.findall(r"\r\n|\n|\r", item.original)
                != re.findall(r"\r\n|\n|\r", candidate_part)
                or _scan_control_tokens(item.original)
                != _scan_control_tokens(candidate_part)
                or any(
                    left != right and (looks_structural(left) or looks_structural(right))
                    for left, right in zip(original_item_lines, candidate_item_lines)
                )
            ):
                rejected[item.key] = "external_item_structure_changed"
                candidate_part = item.original
            candidate_parts.append(candidate_part)
        original = "".join(original_parts)
        candidate = "".join(candidate_parts)
        original_lines, original_line_ranges = line_ranges(original)
        candidate_lines, candidate_line_ranges = line_ranges(candidate)
        if len(original_lines) != len(candidate_lines):
            rejected.update((key, "external_line_structure_changed") for key in changed_keys)
            continue
        item_keys = [item.key for item in source.items]
        original_item_ranges = part_ranges(original_parts)
        candidate_item_ranges = part_ranges(candidate_parts)
        def classify(line: str) -> tuple[str, str | None]:
            content = line.rstrip("\r\n")
            if any(prefix in content for prefix in section_prefixes):
                return "structure", None
            for prefix in structural_prefixes:
                if content.startswith(prefix):
                    return "structure", None
            for marker in markers:
                if content.startswith(marker):
                    return "command", marker
            if any(marker in content for marker in markers) or any(
                prefix in content for prefix in dispatch_prefixes
            ):
                return "structure", None
            return "payload", None

        source_safe: set[str] = set()
        source_rejected: set[str] = set()
        safe_line_indices: set[int] = set()
        active_marker: str | None = None
        payload_original: list[str] = []
        payload_candidate: list[str] = []
        payload_keys: set[str] = set()
        payload_line_indices: list[int] = []

        def finish_payload() -> None:
            nonlocal payload_original, payload_candidate, payload_keys, payload_line_indices
            left = "".join(payload_original)
            right = "".join(payload_candidate)
            if left != right and payload_keys:
                mapping = mappings.get(active_marker or "", {})
                proven = bool(mapping.get("safe_display_sink"))
                equivalent = proven and all(
                    operator in {"equals", "not_equals", "contains", "starts_with", "ends_with"}
                    and _safety_predicate(operator, left, literal)
                    == _safety_predicate(operator, right, literal)
                    for operator, literal in (
                        tuple(map(str, condition))
                        for condition in mapping.get("conditions", ())
                        if isinstance(condition, list) and len(condition) == 2
                    )
                )
                (source_safe if equivalent else source_rejected).update(payload_keys)
                if equivalent:
                    safe_line_indices.update(payload_line_indices)
            payload_original = []
            payload_candidate = []
            payload_keys = set()
            payload_line_indices = []

        structure_changed = False
        for index, (left, right) in enumerate(zip(original_lines, candidate_lines)):
            left_kind, left_marker = classify(left)
            right_kind, right_marker = classify(right)
            if left_kind != "payload" or right_kind != "payload":
                finish_payload()
                if left != right or (left_kind, left_marker) != (right_kind, right_marker):
                    structure_changed = True
                    break
                active_marker = left_marker if left_kind == "command" else None
                continue
            if left != right:
                payload_keys.update(
                    overlapping_keys(
                        original_line_ranges[index], original_item_ranges, item_keys
                    )
                )
                payload_keys.update(
                    overlapping_keys(
                        candidate_line_ranges[index], candidate_item_ranges, item_keys
                    )
                )
            payload_original.append(left)
            payload_candidate.append(right)
            payload_line_indices.append(index)
        finish_payload()
        if structure_changed:
            rejected.update((key, "external_parser_signature_changed") for key in changed_keys)
            continue
        source_rejected &= changed_keys
        source_safe &= changed_keys
        partial_keys = source_safe & source_rejected
        if partial_keys:
            # ponytail: Official TXT segments in the observed protocol end on
            # line boundaries. A span rope is the upgrade if a future tool
            # splits one logical line across workbook rows.
            merged_parts = {key: [] for key in item_keys}
            partitionable = True
            for index, (left, right) in enumerate(zip(original_lines, candidate_lines)):
                left_keys = overlapping_keys(
                    original_line_ranges[index], original_item_ranges, item_keys
                )
                right_keys = overlapping_keys(
                    candidate_line_ranges[index], candidate_item_ranges, item_keys
                )
                if left_keys != right_keys or len(left_keys) != 1:
                    partitionable = False
                    break
                key = next(iter(left_keys))
                merged_parts[key].append(right if index in safe_line_indices else left)
            if partitionable:
                for key in tuple(partial_keys):
                    merged = "".join(merged_parts[key])
                    if merged != originals[key]:
                        external_overrides[key] = merged
                        candidates[key] = merged
                        source_rejected.discard(key)
        for key in changed_keys - source_safe:
            rejected.setdefault(key, "external_payload_not_proven_display_only")
        for key in source_rejected:
            rejected[key] = "external_payload_semantics_changed"
        safe.update(source_safe - source_rejected)
    safe.difference_update(rejected)
    external_safe = safe
    external_rejected = rejected
    safe = set(direct_safe)
    if complete_ledger:
        # The official translation workbook defines the display surface. Static
        # analysis subtracts every observed logic/resource use below; requiring
        # an Auto read for DB labels would incorrectly reject engine-rendered UI.
        safe.update(official_display)
        safe.update(external_safe)
    base_protected = set(candidates) - safe
    forced: set[str] = set()
    reasons: dict[str, set[str]] = {}
    for key, reason in external_rejected.items():
        if key in candidates:
            reasons.setdefault(key, set()).add(reason)

    def final_value(key: str) -> str:
        if key in base_protected or key in forced:
            return originals[key]
        return candidates.get(key, originals[key])

    def same_event_target(key: str) -> bool:
        return event_targets.get(originals[key]) == event_targets.get(final_value(key))

    def has_display_evidence(key: str) -> bool:
        return key in external_safe or "display_only" in set(
            map(str, usage_by_key.get(key, ()))
        )

    def has_display_transport_contract(key: str) -> bool:
        return complete_ledger and has_display_evidence(key)

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

    global_string_flow = analysis.get("global_string_flow")
    global_string_flow_converged = (
        isinstance(global_string_flow, dict)
        and global_string_flow.get("converged") is True
        and complete_ledger
    )
    def normalized_runtime_file_path(value: object) -> str:
        return ntpath.normcase(ntpath.normpath(str(value)))

    file_read_paths: set[str] = set()
    unknown_file_read_path = False
    for dependency in dependencies:
        if (
            dependency.get("kind") != "resource"
            or dependency.get("resource_role") != "file_path_runtime_read"
        ):
            continue
        values = dependency.get("source_values")
        if isinstance(values, list) and values:
            file_read_paths.update(map(normalized_runtime_file_path, values))
        else:
            unknown_file_read_path = True

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
        if operator not in {"equals", "not_equals", "contains", "starts_with", "ends_with"}:
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
        if (
            kind == "resource"
            and dependency.get("resource_role") == "database_string_write"
        ):
            transport_keys = [*source_keys, *right_keys]
            if not global_string_flow_converged:
                protect(
                    transport_keys,
                    "database_string_flow_not_converged",
                )
            elif not dependency.get("display_sink_proven"):
                # ponytail: split a merged runtime value per source key. Direct
                # display provenance is the ceiling; source-target relations can
                # later prove transports that never reach a display command.
                protect(
                    (
                        key
                        for key in transport_keys
                        if not has_display_transport_contract(key)
                    ),
                    "database_storage_without_display_sink",
                )
            return
        if (
            kind == "resource"
            and dependency.get("resource_role") == "file_content_runtime_write"
        ):
            path_values = dependency.get("resource_path_values")
            fixed_unread_path = (
                isinstance(path_values, list)
                and len(path_values) == 1
                and normalized_runtime_file_path(path_values[0]) not in file_read_paths
                and not unknown_file_read_path
            )
            loop_content_only = (
                status == "dynamic"
                and str(dependency.get("reason", ""))
                == "控制流回边扩大为运行时字符串"
            )
            if fixed_unread_path and (status == "resolved" or loop_content_only):
                return
            protect([*source_keys, *right_keys], "file_content_not_proven_display_only")
            return
        if (
            kind == "state"
            and dependency.get("resource_role") == "global_string_write"
            and global_string_flow_converged
        ):
            return
        if status == "dynamic" and kind == "condition" and replay_dynamic_condition(dependency):
            return
        if status != "resolved":
            reason = str(dependency.get("reason", "unresolved"))
            call_target_kind = str(dependency.get("call_target_kind", ""))
            target_equivalence = kind == "call" and call_target_kind == "event_name"
            protect(
                (
                    key
                    for key in [*source_keys, *right_keys]
                    if (
                        not target_equivalence
                        or key not in candidates
                        or not same_event_target(key)
                    )
                ),
                reason,
            )
            raw_scopes = tuple(
                scope
                for scope in dependency.get("unresolved_scopes", ())
                if not (
                    call_target_kind == "numeric_id" and scope == "common:*"
                )
            )
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
                        if not has_display_evidence(key)
                        and (
                            not target_equivalence
                            or key not in candidates
                            or not same_event_target(key)
                        )
                    ),
                    reason,
                )
            return
        if kind == "call":
            protect(
                (
                    key
                    for key in [*source_keys, *right_keys]
                    if key not in candidates or not same_event_target(key)
                ),
                "event_target_change",
            )
            return
        if kind != "condition":
            protect([*source_keys, *right_keys], kind)
            return
        operator = str(dependency.get("operator", "unknown"))
        literal = str(dependency.get("literal", ""))
        if operator not in {"equals", "not_equals", "contains", "starts_with", "ends_with"}:
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

    for key in base_protected:
        reasons.setdefault(key, set()).add("not_proven_safe")

    original_signature = _semantic_replay_signature(analysis)
    replay_report = analysis
    replay_signature = original_signature
    replay_differences: list[str] = []
    replay_history: list[dict[str, object]] = []
    iterations = 0
    pending_dependencies = dependencies
    while True:
        protected_before = len(forced)
        for dependency in pending_dependencies:
            evaluate_dependency(dependency)
        active_candidates = {
            key: final_value(key)
            for key in candidates
            if final_value(key) != originals[key]
        }
        replay_history.append({
            "iteration": iterations + 1,
            "active_candidates": len(active_candidates),
            "differences": [],
            "abstract_differences": [],
            "changed_commands": {},
        })
        iterations += 1
        if len(forced) == protected_before:
            break
        if iterations > len(candidates) + 1:
            raise RuntimeError("WOLF 候选译文安全保护集合无法收敛。")
        # ponytail: only conditions and named call targets observe restored
        # candidates; add a dependency-kind invalidation table if that expands.
        pending_dependencies = [
            dependency
            for dependency in dependencies
            if isinstance(dependency, dict)
            and dependency.get("kind", "condition") in {"condition", "call"}
        ]

    protected = set(base_protected)
    protected.update(forced)
    safe.difference_update(protected)
    safe_display = {
        key for key in safe
        if set(map(str, usage_by_key.get(key, ()))) == {"display_only"}
    }
    safe_equivalent = {
        key for key in safe
        if set(map(str, usage_by_key.get(key, ()))) & {"logic", "event_target"}
    }
    safe_external = safe & external_safe
    safe_contract = safe - safe_display - safe_equivalent - safe_external
    unresolved = sorted(
        {
            str(scope)
            for dependency in replay_report.get("dependencies", [])
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
        "engine": ANALYSIS_ENGINE,
        "safe_to_translate": sorted(safe),
        "approvals": {
            "direct_display": sorted(safe_display),
            "official_display_contract": sorted(safe_contract),
            "semantic_equivalence": sorted(safe_equivalent),
            "external_text_flow": sorted(safe_external),
        },
        "keep_original": sorted(protected),
        "translation_overrides": {
            key: external_overrides[key]
            for key in sorted(external_overrides.keys() & safe)
        },
        "unresolved_scopes": unresolved,
        "replay": {
            "iterations": iterations,
            "candidate_changes": len(candidates),
            "safe_changes": len(safe),
            "protected_changes": len(protected),
            "control_flow_equivalent": (
                original_signature["cfg_edges"] == replay_signature["cfg_edges"]
                and original_signature["calls"] == replay_signature["calls"]
            ),
            "data_effects_equivalent": (
                original_signature["data_effects"]
                == replay_signature["data_effects"]
            ),
            "condition_results_equivalent": (
                "conditions" not in replay_differences
            ),
            "resource_targets_equivalent": (
                original_signature["resources"] == replay_signature["resources"]
            ),
            "external_parser_equivalent": not bool(
                set(external_rejected) & set(candidates)
            ),
            "differences": replay_differences,
            "history": replay_history,
        },
        "reasons": {key: sorted(value) for key, value in sorted(reasons.items())},
    }


def analyze_translation_safety(
    auto_dir: str | Path,
    items: list[TranslationItem],
    candidate_values: dict[str, str],
    policy: str,
    *,
    analysis: dict[str, object],
) -> dict[str, object]:
    return _analyze_compiled_translation_safety(
        auto_dir,
        items,
        candidate_values,
        policy,
        analysis=analysis,
    )
