from __future__ import annotations

import csv
import ctypes
import hashlib
import json
import ntpath
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from collections import Counter, deque
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path, PurePath
from typing import Callable, Iterable, Iterator

from formats import ARTIFACT_EPOCH, require_format
from models import TranslationItem
from safe_io import (
    ResourceBusyError,
    ResourceLock,
    atomic_output_path,
    atomic_write_json,
    package_lock,
    replace_with_retry,
)
from wolf_command_catalog import (
    VERIFIED_EDITOR_VERSION,
    command_semantics,
)
from wolf_analysis import (
    ANALYSIS_ENGINE,
    validate_editor_analysis,
)
from wolf_tools import (
    COPY_FROM_RE,
    CancelledError,
    _scan_control_tokens,
    _kill_process_tree,
    _process_startupinfo,
    hash_directory,
    run_process,
    sha256_file,
)


from wolf_editor_runtime import (
    EDITOR_DOWNLOAD_URL,
    MAX_EDITOR_ARCHIVE_BYTES,
    MAX_EDITOR_PAGE_BYTES,
    MIN_EDITOR_VERSION,
    EditorExportResult,
    EditorInfo,
    EditorRelease,
    LegacyConversionResult,
    _EDITOR_ARCHIVE_RE,
    _OFFICIAL_EDITOR_HOSTS,
    _LinkParser,
    _VSFixedFileInfo,
    _copy_editor_sandbox,
    _download_editor_archive,
    _drive_legacy_conversion,
    _editor_execution_lock,
    _extract_managed_editor as _extract_managed_editor_impl,
    _inspect_matching_runtime as _inspect_matching_runtime_impl,
    _legacy_conversion_action,
    _legacy_dialog_button,
    _matching_editor_runtime,
    _official_url,
    _release_from_url,
    _restore_editor_map_paths,
    _validate_managed_editor as _validate_managed_editor_impl,
    _validate_outputs,
    _windows_version_resource,
    compare_auto_structure,
    convert_legacy_game,
    discover_latest_editor_release,
    export_and_analyze,
    inspect_wolf_editor as _inspect_wolf_editor_impl,
    install_supported_editor as _install_supported_editor_impl,
    latest_editor_release_from_html,
)


def inspect_wolf_editor(path: str | Path) -> EditorInfo:
    return _inspect_wolf_editor_impl(
        path,
        version_resource=_windows_version_resource,
    )


def _inspect_matching_runtime(path: Path, editor: EditorInfo) -> str:
    return _inspect_matching_runtime_impl(
        path,
        editor,
        version_resource=_windows_version_resource,
    )


def _extract_managed_editor(
    archive: Path,
    destination: Path,
    release: EditorRelease,
) -> EditorInfo:
    return _extract_managed_editor_impl(
        archive,
        destination,
        release,
        inspect_editor=inspect_wolf_editor,
    )


def _validate_managed_editor(
    root: Path,
    release: EditorRelease | None = None,
) -> Path:
    return _validate_managed_editor_impl(
        root,
        release,
        inspect_editor=inspect_wolf_editor,
    )


def install_supported_editor(
    packages_root: str | Path,
    *,
    repair: bool = False,
    progress: Callable[[int, int], None] | None = None,
    log: Callable[[str], None] | None = None,
) -> Path:
    return _install_supported_editor_impl(
        packages_root,
        repair=repair,
        progress=progress,
        log=log,
        discover_release=discover_latest_editor_release,
        download_archive=_download_editor_archive,
        inspect_editor=inspect_wolf_editor,
    )

from wolf_auto import (
    AutoCommand,
    AutoDatabaseCoordinate,
    AutoEdge,
    AutoEvent,
    AutoLabel,
    AutoProject,
    _Command,
    _CommandBlock,
    _DatabaseType,
    _database_index,
    _event_blocks,
    _parse_command,
    _read_lines,
)
from wolf_semantics_engine import (
    _AnalysisAudit,
    _AnalysisMetrics,
    _AnalysisState,
    _BlockAnalyzer,
    _BlockPlan,
    _BlockPlanCache,
    _CALL_DEPTH_LIMIT,
    _CFG_CONTROL_OPCODES,
    _CFG_IMPLEMENTED_OPCODES,
    _CFG_STATE_VISIT_LIMIT,
    _CSELF_REFERENCE_RE,
    _CallArgumentPool,
    _CallCache,
    _CallSummary,
    _EntryPlan,
    _EntryPlanCache,
    _EventItemCache,
    _ExternalTextSource,
    _NumberValue,
    _PersistentReadCache,
    _PersistentReadPlan,
    _STRING_LITERAL_LIMIT,
    _STRING_REFERENCE_RE,
    _StringValue,
    _VALUE_LIMIT,
    _WOLF_PATH_REFERENCE_RE,
    _address_variables_for_block,
    _apply_persistent_read_plan,
    _block_map_id,
    _block_map_ids,
    _calculate_numbers,
    _command_string_roles,
    _compile_persistent_read_plan,
    _concat_literals,
    _condition_operator,
    _event_code,
    _event_codes,
    _event_name_code,
    _event_name_codes,
    _expand_string_references,
    _expand_string_templates,
    _external_template_matches,
    _items_for_event_codes,
    _limited,
    _loop_identity,
    _map_ids_from_databases,
    _merge_numbers,
    _merge_states,
    _merge_strings,
    _merge_value_maps,
    _normalize_external_path,
    _number_argument,
    _number_offset_identity,
    _number_semantic_key,
    _persistent_inputs_for_block,
    _state_cache_key,
    _state_has_values,
    _states_semantically_equal,
    _string_reference_value,
    _string_semantic_key,
    _string_value_status,
    _string_variable_for_escape,
    _with_literals,
)
from wolf_semantics import (
    _CompiledAutoProgram,
    _EXTERNAL_FILE_CODE_RE,
    _EXTERNAL_SOURCE_PREFIX,
    _GLOBAL_STRING_FLOW_MAX_ITERATIONS,
    _WORKBOOK_DB_CODE_RE,
    _analyze_blocks,
    _analyze_compiled_program,
    _call_graph_report,
    _command_transfer_complete,
    _compile_auto_program,
    _conservative_event_scopes,
    _event_node,
    _external_text_flow_report,
    _external_text_observer_report,
    _external_text_sources,
    _scope_keys,
    _translation_usage_report,
    analyze_auto_export,
)
from wolf_proof import (
    _analyze_compiled_translation_safety,
    _condition_truth_signature,
    _finish_compiled_translation_safety,
    _safety_predicate,
    _semantic_replay_signature,
    analyze_translation_safety,
)
