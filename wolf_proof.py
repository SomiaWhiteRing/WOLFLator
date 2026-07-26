from __future__ import annotations

import ntpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from models import TranslationItem
from wolf_auto import AutoProject, parse_auto_project
from wolf_semantics import AUTO_ANALYSIS_SCHEMA, _WORKBOOK_DB_CODE_RE, analyze_project
from wolf_tools import hash_directory


TRANSLATION_SAFETY_SCHEMA = 3


@dataclass(frozen=True)
class ProofResult:
    """The fixed-point safety decision shared by preview and import paths."""

    safe_keys: frozenset[str]
    protected_keys: frozenset[str]
    report: dict[str, object]


@dataclass(frozen=True)
class _EditorIdentity:
    path: Path
    version: str
    version_tuple: tuple[int, ...]
    sha256: str

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


def _replay_changed_commands(
    original: dict[str, object], candidate: dict[str, object]
) -> dict[str, set[str]]:
    def grouped(
        records: Iterable[tuple[object, ...]], field: str
    ) -> dict[str, set[tuple[object, ...]]]:
        result: dict[str, set[tuple[object, ...]]] = {}
        for record in records:
            command_id = (
                str(record[0])
                if field in {"cfg_edges", "calls", "data_effects"}
                else ":".join(map(str, record[:5]))
            )
            result.setdefault(command_id, set()).add(record)
        return result

    changed: dict[str, set[str]] = {}
    for field in ("cfg_edges", "calls", "data_effects", "conditions", "resources"):
        left = grouped(original.get(field, ()), field)
        right = grouped(candidate.get(field, ()), field)
        command_ids = {
            command_id
            for command_id in left.keys() | right.keys()
            if left.get(command_id, set()) != right.get(command_id, set())
        }
        if command_ids:
            changed[field] = command_ids
    if original.get("opaque_effects") != candidate.get("opaque_effects"):
        changed["opaque_effects"] = {"project"}
    return changed


def analyze_translation_safety(
    auto_dir: str | Path,
    items: list[TranslationItem],
    candidate_values: dict[str, str],
    policy: str,
    *,
    analysis: dict[str, object],
    project: AutoProject | None = None,
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
    safe = set(direct_safe)
    if complete_ledger:
        # The official translation workbook defines the display surface. Static
        # analysis subtracts every observed logic/resource use below; requiring
        # an Auto read for DB labels would incorrectly reject engine-rendered UI.
        safe.update(official_display)
    base_protected = set(candidates) - safe
    forced: set[str] = set()
    reasons: dict[str, set[str]] = {}

    def final_value(key: str) -> str:
        if key in base_protected or key in forced:
            return originals[key]
        return candidates.get(key, originals[key])

    def same_event_target(key: str) -> bool:
        return event_targets.get(originals[key]) == event_targets.get(final_value(key))

    def has_display_evidence(key: str) -> bool:
        return "display_only" in set(map(str, usage_by_key.get(key, ())))

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
            if not dependency.get("display_sink_proven"):
                protect(
                    [*source_keys, *right_keys],
                    "database_storage_without_display_sink",
                )
            elif not global_string_flow_converged:
                protect(
                    [*source_keys, *right_keys],
                    "database_string_flow_not_converged",
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

    editor_data = analysis.get("editor", {})
    if not isinstance(editor_data, dict):
        raise ValueError("Editor 分析报告缺少 Editor 身份。")
    editor_version = str(editor_data.get("version", ""))
    editor = _EditorIdentity(
        Path(str(editor_data.get("path", "Editor.exe"))),
        editor_version,
        tuple(int(value) for value in editor_version.split(".") if value.isdigit()),
        str(editor_data.get("sha256", "")),
    )
    project = project or parse_auto_project(root, editor.version)
    baseline_report = analysis
    original_signature = _semantic_replay_signature(baseline_report)
    replay_report = baseline_report
    replay_signature = original_signature
    replay_differences: list[str] = []
    replay_history: list[dict[str, object]] = []
    previous_candidates: dict[str, str] | None = None
    iterations = 0
    while True:
        protected_before = len(forced)
        for dependency in dependencies:
            evaluate_dependency(dependency)
        active_candidates = {
            key: final_value(key)
            for key in candidates
            if final_value(key) != originals[key]
        }
        if active_candidates != previous_candidates:
            replay_report = analyze_project(
                project,
                items,
                editor,
                input_hash=str(analysis.get("input_hash", "")),
                output_hash=str(analysis.get("output_hash", "")),
                candidate_values=active_candidates,
            ).report
            replay_global_flow = replay_report.get("global_string_flow")
            if not (
                isinstance(replay_global_flow, dict)
                and replay_global_flow.get("converged") is True
            ):
                protect(active_candidates, "global_string_flow_not_converged")
            replay_signature = _semantic_replay_signature(replay_report)
            previous_candidates = dict(active_candidates)
        replay_changes = _replay_changed_commands(
            original_signature, replay_signature
        )
        replay_differences = sorted(replay_changes)
        abstract_differences: list[str] = []
        if replay_differences:
            changed_commands = {
                command_id
                for command_ids in replay_changes.values()
                for command_id in command_ids
                if command_id != "project"
            }
            affected: set[str] = set()
            for dependency in [*dependencies, *replay_report.get("dependencies", ())]:
                if not isinstance(dependency, dict):
                    continue
                dependency_id = ":".join(map(str, (
                    dependency.get("auto_file", ""),
                    dependency.get("event_type", ""),
                    dependency.get("event_id", ""),
                    dependency.get("page", ""),
                    dependency.get("command", ""),
                )))
                if dependency_id not in changed_commands:
                    continue
                dependency_keys: set[str] = set()
                for field in ("condition_keys", "source_keys", "right_source_keys"):
                    dependency_keys.update(map(str, dependency.get(field, ())))
                kind = str(dependency.get("kind", ""))
                scoped = _scope_keys(
                    items,
                    tuple(map(str, dependency.get("unresolved_scopes", ()))),
                    scope_cache,
                )
                if kind == "call":
                    affected.update(
                        key
                        for key in dependency_keys | scoped
                        if key in active_candidates and not same_event_target(key)
                    )
                elif kind != "condition":
                    active_keys = dependency_keys & active_candidates.keys()
                    affected.update(active_keys or (scoped & active_candidates.keys()))
            affected.intersection_update(active_candidates)
            if not affected:
                non_condition = set(replay_differences) - {"conditions"}
                if non_condition:
                    # A semantic delta without provenance is an analyzer defect,
                    # not evidence that every unrelated translation is unsafe.
                    raise RuntimeError(
                        "WOLF 候选译文重放出现无法定位来源的语义差异："
                        + ", ".join(sorted(non_condition))
                    )
                # Dynamic abstract domains can contain different strings while
                # every candidate predicate remains equivalent. The explicit
                # item-wise checks above are the safety property we care about.
                abstract_differences.append("conditions")
                replay_differences = []
            else:
                protect(affected, "semantic_replay_difference")
        replay_history.append({
            "iteration": iterations + 1,
            "active_candidates": len(active_candidates),
            "differences": sorted(replay_changes),
            "abstract_differences": abstract_differences,
            "changed_commands": {
                field: sorted(command_ids)[:20]
                for field, command_ids in sorted(replay_changes.items())
            },
        })
        iterations += 1
        if len(forced) == protected_before and not replay_differences:
            break
        if iterations > len(candidates) + 1:
            raise RuntimeError("WOLF 候选译文安全保护集合无法收敛。")

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
    safe_contract = safe - safe_display - safe_equivalent
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
        "schema": TRANSLATION_SAFETY_SCHEMA,
        "safe_to_translate": sorted(safe),
        "approvals": {
            "direct_display": sorted(safe_display),
            "official_display_contract": sorted(safe_contract),
            "semantic_equivalence": sorted(safe_equivalent),
        },
        "keep_original": sorted(protected),
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
            "differences": replay_differences,
            "history": replay_history,
        },
        "reasons": {key: sorted(value) for key, value in sorted(reasons.items())},
    }


def prove_translations(
    project: AutoProject,
    items: list[TranslationItem],
    candidates: dict[str, str],
    baseline: dict[str, object],
    policy: str,
) -> ProofResult:
    """Run the candidate fixed point against one immutable parsed project."""
    if project.source_dir is None:
        raise ValueError("ProofResult 需要由 parse_auto_project() 生成的 AutoProject。")
    report = analyze_translation_safety(
        project.source_dir,
        items,
        candidates,
        policy,
        analysis=baseline,
        project=project,
    )
    return ProofResult(
        frozenset(map(str, report["safe_to_translate"])),
        frozenset(map(str, report["keep_original"])),
        report,
    )


