from __future__ import annotations

import errno
import hashlib
import json
import os
import queue
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterable, Iterator

from openpyxl import load_workbook

from fonts import FONT_CODES
from formats import ARTIFACT_EPOCH, require_format
from models import (
    MAX_EXTERNAL_FILE_LIMIT_KB,
    ImportCategory,
    ImportProtectionRules,
    ImportScope,
    ToolResult,
    TranslationItem,
)
from safe_io import (
    atomic_output_path,
    atomic_write_bytes,
    atomic_write_json,
    read_text_with_retry,
    replace_with_retry,
)
from wolf_analysis import ANALYSIS_ENGINE, validate_editor_analysis

from wolf_workbook import (
    classify_optional_name_delta,
    CODE_HEADER,
    COPY_FROM_RE,
    dump_items,
    EXPECTED_TARGET,
    FLAG_HEADER,
    full_export_scope,
    INFO_HEADER,
    is_font_setting,
    load_items,
    locate_workbook,
    merge_ainiee_output,
    name_baseline_scope,
    normalize_import_display_middle_dots,
    ORIGINAL_HEADER,
    protect_control_tokens,
    PUA_END,
    PUA_START,
    read_font_slots,
    read_translation_items,
    reconcile_incremental,
    restore_control_tokens,
    retryable_translation_errors,
    selected_translation_items,
    selected_translation_requirements,
    SPECIAL_ESCAPES,
    stable_key,
    SUPPORT_DIR,
    TARGET_PREFIX,
    to_paratranz,
    TYPE_HEADER,
    WORKBOOK_NAME,
    write_font_workbook,
    write_full_workbook,
    _category,
    _content_category,
    _copy_source,
    _EXTERNAL_DISPLAY_COMMAND_RE,
    _external_script_control_spans,
    _header_map,
    _index_ainiee_rows,
    _item_control_spans,
    _iter_data_rows,
    _location_identities,
    _normalize_xlsx_shared_strings,
    _protect_item_tokens,
    _protect_spans,
    _restore_item_tokens,
    _restore_tokens,
    _save_workbook_atomic,
    _scan_control_spans,
    _scan_control_tokens,
    _set_literal_cell,
    _validated_ainiee_translation,
)

from process_tools import (
    CancelledError,
    CONSOLE_CAPTURE_ARG,
    console_capture_worker,
    hash_directory,
    official_dialogs_indicate_legacy_game,
    OFFICIAL_MISALIGNED_MESSAGE,
    OfficialArtifactMissingError,
    OfficialToolDialogError,
    parse_official_diagnostics,
    parse_official_map_failures,
    resource_path,
    run_process,
    sha256_file,
    ToolProcessError,
    verified_vendor_file,
    _console_capture_command,
    _console_capture_worker_windows,
    _console_delta,
    _dismiss_process_dialogs,
    _emit_log,
    _kill_process_tree,
    _pe_import_name_offset,
    _process_startupinfo,
    _silent_official_executable as _silent_official_executable_impl,
    _write_console_snapshot,
)

from wolf_official import (
    GAME_CONFIG_NAME,
    locate_translated_game,
    OfficialToolRunner as _OfficialToolRunner,
    prepare_official_tool,
    prepare_uberwolf,
    temporary_external_filter_view,
    UberWolfRunner as _UberWolfRunner,
    write_official_game_config,
    _official_config_text,
)


def _silent_official_executable(path: str | Path) -> bytes:
    return _silent_official_executable_impl(path, _pe_import_name_offset)


class UberWolfRunner(_UberWolfRunner):
    @staticmethod
    def _run_process(*args, **kwargs) -> ToolResult:
        return run_process(*args, **kwargs)


class OfficialToolRunner(_OfficialToolRunner):
    @staticmethod
    def _run_process(*args, **kwargs) -> ToolResult:
        return run_process(*args, **kwargs)


from wolf_import_protection import (
    analyze_import_protection,
    EXTERNAL_DIRECTIVE_RE,
    imported_display_texts,
    load_import_protection,
    PATH_OR_COMMAND_RE,
    validate_import_protection,
    write_scoped_workbook,
    _external_reference_evidence,
    _filename_target_exists,
    _logic_predicate,
    _looks_like_identifier,
    _looks_like_path_or_command,
)

if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == CONSOLE_CAPTURE_ARG:
        raise SystemExit(console_capture_worker(int(sys.argv[2]), sys.argv[3]))
    raise SystemExit(2)
