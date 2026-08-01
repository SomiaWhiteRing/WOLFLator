from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager, nullcontext
from itertools import count
from pathlib import Path
from typing import Callable, Iterable

from formats import ARTIFACT_EPOCH, require_format
from models import AppSettings, ImportCategory, TranslationItem
from safe_io import (
    atomic_write_bytes,
    atomic_write_json,
    package_lock,
    replace_with_retry,
    runtime_lock,
)
from wolf_tools import CancelledError, resource_path, run_process, sha256_file, verified_vendor_file

from ainiee_runtime import (
    AINIEE_ARCHIVE_ETAG, AINIEE_ARCHIVE_SHA256, AINIEE_ARCHIVE_URL, AINIEE_COMMIT,
    AINIEE_EXECUTABLE_FILES, AINIEE_SOURCE_SHA256, AINIEE_TREE, AINIEE_VERSION,
    AINIEE_WEB_DIST_SHA256, AINIEE_WEB_DIST_SIZE, AINIEE_WEB_DIST_URL,
    MAX_ARCHIVE_BYTES, REQUIRED_PATHS, SOURCE_HASH_EXCLUDED,
    _atomic_bytes, _atomic_json, _check_cancel, _create_managed_runtime_locked,
    _download, _ensure_web_dist, _extract_zip_checked, _git_tree_from_zip,
    _install_supported_ainiee_locked, _load_runtime_metadata, _managed_runtime_path,
    _remove_managed_ainiee_locked, _runtime_fingerprint, _safe_extract,
    _safe_extract_web_dist, _source_code_hash, _sync_runtime_locked,
    _validate_managed_package as _validate_managed_package_impl, _web_dist_ready, create_managed_runtime,
    install_supported_ainiee, locate_ainiee_source, locate_uv, prepare_managed_runtime,
    remove_managed_ainiee, require_managed_runtime, sync_runtime,
    validate_ainiee_source as _validate_ainiee_source_impl,
)
from ainiee_translation import (
    COMMON_PROMPT_ID, CONTROL_PLACEHOLDER_REGEX, PROOFREAD_EVENT_PREFIX, RULE_DEFAULTS,
    SESSION_PROFILE, _active_session_profile, _report_ainiee_logs,
    _restore_excluded_rows, _rules_name, _rules_with_control_protection,
    _run_translation_locked as _run_translation_locked_impl, _session_profile,
    cleanup_session_profiles, run_proofread as _run_proofread_impl,
    run_translation as _run_translation_impl,
)


def validate_ainiee_source(path: str | Path) -> Path:
    return _validate_ainiee_source_impl(
        path,
        expected_source_sha256=AINIEE_SOURCE_SHA256,
    )


def _validate_managed_package(path: Path) -> Path:
    return _validate_managed_package_impl(
        path,
        validate_source=validate_ainiee_source,
    )


def run_translation(
    runtime: str | Path,
    input_json: str | Path,
    output_dir: str | Path,
    rules: dict[str, object],
    project_id: str,
    settings: AppSettings,
    api_key: str,
    *,
    cancel_event: threading.Event | None = None,
    log: Callable[[str], None] | None = None,
    diagnostic_log: Callable[[str], None] | None = None,
) -> list[dict[str, object]]:
    return _run_translation_impl(
        runtime,
        input_json,
        output_dir,
        rules,
        project_id,
        settings,
        api_key,
        cancel_event=cancel_event,
        log=log,
        diagnostic_log=diagnostic_log,
        validate_source=validate_ainiee_source,
        uv_locator=locate_uv,
        process_runner=run_process,
    )


def _run_translation_locked(
    runtime: str | Path,
    input_json: str | Path,
    output_dir: str | Path,
    rules: dict[str, object],
    project_id: str,
    settings: AppSettings,
    api_key: str,
    *,
    cancel_event: threading.Event | None,
    log: Callable[[str], None] | None,
    diagnostic_log: Callable[[str], None] | None,
) -> list[dict[str, object]]:
    return _run_translation_locked_impl(
        runtime,
        input_json,
        output_dir,
        rules,
        project_id,
        settings,
        api_key,
        cancel_event=cancel_event,
        log=log,
        diagnostic_log=diagnostic_log,
        validate_source=validate_ainiee_source,
        uv_locator=locate_uv,
        process_runner=run_process,
    )


def run_proofread(
    runtime: str | Path,
    input_json: str | Path,
    output_json: str | Path,
    rules: dict[str, object],
    settings: AppSettings,
    api_key: str,
    *,
    cancel_event: threading.Event | None = None,
    progress: Callable[[dict[str, object]], None] | None = None,
    log: Callable[[str], None] | None = None,
    diagnostic_log: Callable[[str], None] | None = None,
) -> dict[str, object]:
    return _run_proofread_impl(
        runtime,
        input_json,
        output_json,
        rules,
        settings,
        api_key,
        cancel_event=cancel_event,
        progress=progress,
        log=log,
        diagnostic_log=diagnostic_log,
        validate_source=validate_ainiee_source,
        uv_locator=locate_uv,
        process_runner=run_process,
    )
from ainiee_glossary import (
    ApiError, OpenAICompatibleClient, _chunks, _json_list, _merge_by_key,
    _parallel_stage, _read_response_body, _repair_invalid_json_escapes,
    _request_chunk, generate_glossary, test_api,
)
