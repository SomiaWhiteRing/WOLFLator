from __future__ import annotations

import json
import os
import re
import shutil
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from ainiee_runtime import _atomic_bytes, _atomic_json, locate_uv, validate_ainiee_source
from formats import ARTIFACT_EPOCH, require_format
from models import AppSettings
from process_tools import resource_path, run_process
from safe_io import atomic_write_json, runtime_lock


SESSION_PROFILE = "WOLFLator_session"


COMMON_PROMPT_ID = 100


CONTROL_PLACEHOLDER_REGEX = r"[\uE100-\uF7FF]"
_STRUCTURAL_FRAGMENT_TRIGGER_RE = re.compile(r"[\uE100-\uF7FF]|\r\n|\r|\n")
_STRUCTURAL_FRAGMENT_PART_RE = re.compile(r"([\uE100-\uF7FF]|\r\n|\r|\n|[ \t]+)")


RULE_DEFAULTS = {
    "pre_translation_data": [],
    "post_translation_data": [],
    "prompt_dictionary_data": [],
    "exclusion_list_data": [],
    "characterization_data": [],
    "world_building_content": "",
    "world_building_history": [],
    "writing_style_content": "",
    "writing_style_history": [],
    "translation_example_data": [],
    "pre_translation_switch": False,
    "post_translation_switch": False,
    "prompt_dictionary_switch": True,
    "exclusion_list_switch": False,
    "characterization_switch": True,
    "world_building_switch": False,
    "writing_style_switch": False,
    "translation_example_switch": False,
}


def fragment_translation_rows(
    input_rows: list[dict[str, object]],
) -> tuple[list[dict[str, object]], dict[str, list[dict[str, str]]]]:
    fragments: list[dict[str, object]] = []
    ledger: dict[str, list[dict[str, str]]] = {}
    for row in input_rows:
        parent_key = str(row["key"])
        original = str(row.get("original", ""))
        values = (
            _STRUCTURAL_FRAGMENT_PART_RE.split(original)
            if _STRUCTURAL_FRAGMENT_TRIGGER_RE.search(original)
            else [original]
        )
        parts: list[dict[str, str]] = []
        child_index = 0
        for value in values:
            if not value:
                continue
            if _STRUCTURAL_FRAGMENT_PART_RE.fullmatch(value):
                parts.append({"literal": value})
                continue
            child_index += 1
            child_key = f"{parent_key}::fragment:{child_index}"
            parts.append({"child_key": child_key, "source": value})
            fragments.append(
                {
                    "key": child_key,
                    "original": value,
                    "translation": "",
                    "context": f"{row.get('context', '')} | Fragment {child_index}",
                    "stage": 0,
                }
            )
        ledger[parent_key] = parts
    return fragments, ledger


def merge_fragmented_rows(
    input_rows: list[dict[str, object]],
    translated: list[dict[str, object]],
    ledger: dict[str, list[dict[str, str]]],
) -> tuple[list[dict[str, object]], dict[str, list[str]]]:
    translated_by_key = {str(row.get("key", "")): row for row in translated}
    merged: list[dict[str, object]] = []
    missing: dict[str, list[str]] = {}
    for parent in input_rows:
        parent_key = str(parent["key"])
        pieces: list[str] = []
        for part in ledger[parent_key]:
            if "literal" in part:
                pieces.append(part["literal"])
                continue
            child_key = part["child_key"]
            child = translated_by_key.get(child_key)
            value = str(child.get("translation", "")) if child else ""
            if not value:
                missing.setdefault(parent_key, []).append(child_key)
                value = part["source"]
            pieces.append(value)
        row = dict(parent)
        row["translation"] = "".join(pieces)
        merged.append(row)
    return merged, missing


def _rules_name(project_id: str) -> str:
    safe = re.sub(r'[^A-Za-z0-9_.-]+', "_", project_id).strip("._") or "project"
    return f"WOLFLator_{safe[:80]}"


def cleanup_session_profiles(runtime: str | Path) -> None:
    root = Path(runtime)
    profiles = root / "Resource" / "profiles"
    if profiles.is_dir():
        for path in profiles.glob("WOLFLator_session*.json"):
            path.unlink(missing_ok=True)


def _rules_with_control_protection(rules: dict[str, object]) -> dict[str, object]:
    result = dict(RULE_DEFAULTS)
    result.update(rules)
    exclusions = [dict(item) for item in result.get("exclusion_list_data", []) if isinstance(item, dict)]
    if not any(item.get("regex") == CONTROL_PLACEHOLDER_REGEX for item in exclusions):
        exclusions.append(
            {
                "markers": "",
                "info": "WOLFLator control placeholder",
                "regex": CONTROL_PLACEHOLDER_REGEX,
            }
        )
    # AiNiee uses this as the master switch for every rules-profile feature.
    result["prompt_dictionary_switch"] = True
    result["exclusion_list_switch"] = True
    result["exclusion_list_data"] = exclusions
    return result


@contextmanager
def _active_session_profile(
    root: Path,
    profile: dict[str, object],
    rules_name: str,
    rules: dict[str, object],
):
    with runtime_lock(root.parent, "session-profile"):
        profiles = root / "Resource" / "profiles"
        rules_profiles = root / "Resource" / "rules_profiles"
        config_path = root / "Resource" / "config.json"
        profiles.mkdir(parents=True, exist_ok=True)
        rules_profiles.mkdir(parents=True, exist_ok=True)
        original = config_path.read_bytes() if config_path.is_file() else None
        root_config = json.loads(original.decode("utf-8-sig")) if original else {}
        if not isinstance(root_config, dict):
            raise ValueError("AiNiee Resource/config.json 不是 JSON 对象。")
        restore = original
        if str(root_config.get("active_profile", "")).startswith("WOLFLator_session"):
            root_config["active_profile"] = "default"
            restore = json.dumps(root_config, ensure_ascii=False, indent=2).encode("utf-8")

        cleanup_session_profiles(root)
        profile_path = profiles / f"{SESSION_PROFILE}.json"
        try:
            _atomic_json(profile_path, profile)
            _atomic_json(rules_profiles / f"{rules_name}.json", rules)
            session_root = dict(root_config)
            session_root["active_profile"] = SESSION_PROFILE
            session_root["active_rules_profile"] = rules_name
            _atomic_json(config_path, session_root)
            yield
        finally:
            profile_path.unlink(missing_ok=True)
            if restore is None:
                config_path.unlink(missing_ok=True)
            else:
                _atomic_bytes(config_path, restore)


def _session_profile(settings: AppSettings, api_key: str) -> dict[str, object]:
    base_url = settings.api_base_url.rstrip("/")
    think_depth: str | int = settings.api_think_depth.strip()
    if think_depth.isdigit():
        think_depth = int(think_depth)
    # ponytail: Keep one fixed AiNiee-compatible profile; provider detection is intentionally removed.
    platform_tag = "deepseek"
    platform = {
        "tag": platform_tag,
        "group": "online",
        "name": "DeepSeek",
        "api_url": base_url,
        "api_key": api_key,
        "api_format": "OpenAI",
        "icon": "deepseek",
        "rpm_limit": max(1, settings.api_rpm),
        "tpm_limit": max(1, settings.api_tpm),
        "model": settings.api_model,
        "model_datas": [settings.api_model],
        "top_p": settings.api_top_p,
        "temperature": settings.api_temperature,
        "presence_penalty": settings.api_presence_penalty,
        "frequency_penalty": settings.api_frequency_penalty,
        "think_switch": settings.api_think_switch,
        "think_depth": think_depth,
        "structured_output_mode": 0,
        "auto_complete": False,
        "key_in_settings": [
            "api_url",
            "api_key",
            "model",
            "rpm_limit",
            "tpm_limit",
            "top_p",
            "temperature",
            "presence_penalty",
            "frequency_penalty",
            "think_switch",
            "think_depth",
        ],
    }
    return {
        "interface_language": "zh_CN",
        "source_language": "Japanese",
        "target_language": "Chinese",
        "target_platform": platform_tag,
        "api_settings": {"translate": platform_tag, "polish": platform_tag},
        "platforms": {platform_tag: platform},
        "base_url": base_url,
        "model": settings.api_model,
        "api_key": api_key,
        "translation_project": "Paratranz",
        "interactive_mode": False,
        "user_thread_counts": max(1, settings.api_threads),
        "request_timeout": max(10, settings.api_timeout),
        "think_switch": settings.api_think_switch,
        "think_depth": think_depth,
        "enable_api_failover": False,
        "enable_session_logging": True,
        "show_detailed_logs": True,
        "translation_prompt_selection": {"last_selected_id": COMMON_PROMPT_ID},
        "sdk_request_mode": "openai",
        "use_openai_sdk": True,
        "auto_set_output_path": False,
        "response_conversion_toggle": False,
        "auto_process_text_code_segment": True,
        "response_check_switch": {"newline_character_count_check": False},
        "tokens_limit_switch": settings.translation_chunk_mode == "token",
        "tokens_limit": settings.translation_token_limit,
        "lines_limit": settings.translation_line_limit,
        "retry_split_min_lines": settings.translation_retry_min_lines,
        "retry_count": settings.translation_retry_count,
        "pre_line_counts": settings.translation_pre_line_counts,
        "round_limit": settings.translation_rounds,
        "enable_smart_round_limit": settings.translation_enable_smart_round_limit,
        "smart_round_limit_multiplier": settings.translation_smart_round_limit_multiplier,
        "enable_retry_backoff": settings.translation_enable_retry_backoff,
        # ponytail: WOLF safety rules and prompt data stay outside this generic profile projection.
    }


def _report_ainiee_logs(
    output: Path,
    diagnostic_log: Callable[[str], None] | None,
    *,
    include_tail: bool,
) -> None:
    if not diagnostic_log:
        return
    files = sorted(
        (path for path in (output / "logs").glob("*") if path.is_file()),
        key=lambda path: path.stat().st_mtime_ns,
    )
    diagnostic_log(
        f"ainiee.session_logs count={len(files)} paths="
        + json.dumps([str(path) for path in files], ensure_ascii=False)
    )
    if not include_tail or not files:
        return
    latest = files[-1]
    try:
        text = latest.read_text(encoding="utf-8", errors="replace")[-65_536:]
    except OSError as exc:
        diagnostic_log(f"ainiee.session_log.read_failed path={latest} error={exc}")
        return
    diagnostic_log(f"ainiee.session_log.tail path={latest} chars={len(text)}")
    for line in text.splitlines()[-200:]:
        diagnostic_log(f"ainiee.session {line}")


def _restore_excluded_rows(
    input_rows: list[dict[str, object]],
    translated: list[dict[str, object]],
    output: Path,
    diagnostic_log: Callable[[str], None] | None,
) -> list[dict[str, object]]:
    expected: dict[str, dict[str, object]] = {}
    for row in input_rows:
        key = str(row.get("key", ""))
        if not key or key in expected:
            raise ValueError(f"AiNiee 输入包含空键或重复键: {key!r}")
        expected[key] = row
    actual: set[str] = set()
    for row in translated:
        key = str(row.get("key", ""))
        if not key or key in actual:
            raise ValueError(f"AiNiee 输出包含空键或重复键: {key!r}")
        actual.add(key)
    missing = set(expected) - actual
    if not missing:
        return translated

    cache_path = output / "cache" / "AinieeCacheData.json"
    if not cache_path.is_file():
        return translated
    cache = json.loads(cache_path.read_text(encoding="utf-8-sig"))
    files = cache.get("files") if isinstance(cache, dict) else None
    if not isinstance(files, dict):
        raise ValueError("AiNiee 缓存缺少 files 对象。")

    restored: dict[str, dict[str, object]] = {}
    for file_data in files.values():
        cache_items = file_data.get("items") if isinstance(file_data, dict) else None
        if not isinstance(cache_items, list):
            raise ValueError("AiNiee 缓存文件缺少 items 数组。")
        for cache_item in cache_items:
            if not isinstance(cache_item, dict) or cache_item.get("translation_status") != 7:
                continue
            extra = cache_item.get("extra")
            key = str(extra.get("key", "")) if isinstance(extra, dict) else ""
            if key not in missing:
                continue
            source = str(cache_item.get("source_text", ""))
            original = str(expected[key].get("original", ""))
            if source != original:
                raise ValueError(f"AiNiee 排除项与输入原文不一致: {key}")
            if key in restored:
                raise ValueError(f"AiNiee 缓存包含重复排除键: {key}")
            restored[key] = {
                **expected[key],
                "translation": original,
                "stage": 1,
                "wolflator_excluded": True,
            }
    if diagnostic_log:
        diagnostic_log(
            f"ainiee.translate.excluded restored={len(restored)} "
            f"unresolved={len(missing - set(restored))}"
        )
    return translated + list(restored.values())


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
    progress: Callable[[dict[str, object]], None] | None = None,
    validate_source: Callable[[str | Path], Path] = validate_ainiee_source,
    uv_locator: Callable[[], Path] = locate_uv,
    process_runner: Callable[..., object] = run_process,
) -> list[dict[str, object]]:
    root = Path(runtime).resolve()
    # ponytail: AiNiee has one shared active profile; use per-session runtime copies if parallel translation is needed.
    with runtime_lock(root.parent, "translate"):
        return _run_translation_locked(
            root,
            input_json,
            output_dir,
            rules,
            project_id,
            settings,
            api_key,
            cancel_event=cancel_event,
            log=log,
            diagnostic_log=diagnostic_log,
            progress=progress,
            validate_source=validate_source,
            uv_locator=uv_locator,
            process_runner=process_runner,
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
    progress: Callable[[dict[str, object]], None] | None,
    validate_source: Callable[[str | Path], Path] = validate_ainiee_source,
    uv_locator: Callable[[], Path] = locate_uv,
    process_runner: Callable[..., object] = run_process,
) -> list[dict[str, object]]:
    root = validate_source(runtime)
    rules_name = _rules_name(project_id)
    managed_rules = _rules_with_control_protection(rules)
    output = Path(output_dir)
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)
    if diagnostic_log:
        diagnostic_log(
            f"ainiee.translate.start runtime={root} input={Path(input_json).resolve()} "
            f"input_bytes={Path(input_json).stat().st_size} output={output.resolve()} "
            f"profile={SESSION_PROFILE} rules_profile={rules_name} "
            f"chunk_mode={settings.translation_chunk_mode} "
            f"chunk_limit={settings.translation_token_limit if settings.translation_chunk_mode == 'token' else settings.translation_line_limit} "
            f"rounds={settings.translation_rounds}"
        )
    child_env = os.environ.copy()
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONIOENCODING"] = "utf-8"
    with _active_session_profile(root, _session_profile(settings, api_key), rules_name, managed_rules):
        try:
            command = [
                str(uv_locator()),
                "run",
                "--frozen",
                "--no-sync",
                "ainiee_cli.py",
                "translate",
                str(Path(input_json).resolve()),
                "-o",
                str(output.resolve()),
                "-s",
                "Japanese",
                "-t",
                "Chinese",
                "--type",
                "Paratranz",
                "--web-mode",
                "--rounds",
                str(settings.translation_rounds),
                "--yes",
            ]
            if settings.translation_chunk_mode == "token":
                command.extend(["--tokens", str(settings.translation_token_limit)])
            else:
                command.extend(["--lines", str(settings.translation_line_limit)])

            def receive_output(stream: str, line: str) -> None:
                if stream != "stdout" or progress is None:
                    return
                match = re.search(r"Progress:\s*(\d+)\s*/\s*(\d+)", line)
                if match:
                    progress(
                        {
                            "phase": "translate",
                            "status": "running",
                            "current": int(match.group(1)),
                            "total": int(match.group(2)),
                        }
                    )

            if progress is not None:
                progress(
                    {
                        "phase": "translate",
                        "status": "running",
                        "current": 0,
                        "total": len(
                            json.loads(Path(input_json).read_text(encoding="utf-8-sig"))
                        ),
                    }
                )
            process_runner(
                command,
                cwd=root,
                timeout=24 * 3600,
                cancel_event=cancel_event,
                log=log,
                diagnostic_log=diagnostic_log,
                env=child_env,
                output_line=receive_output,
            )
        except Exception:
            _report_ainiee_logs(output, diagnostic_log, include_tail=True)
            raise
        _report_ainiee_logs(output, diagnostic_log, include_tail=False)
        expected_name = Path(input_json).name
        result_path = output / expected_name
        if diagnostic_log:
            diagnostic_log(
                f"ainiee.translate.output expected={result_path} exists={result_path.is_file()}"
            )
        if not result_path.is_file():
            raise RuntimeError(f"AiNiee 返回成功，但没有生成 {expected_name}。")
        data = json.loads(result_path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
            raise ValueError("AiNiee 输出不是 Paratranz 对象数组。")
        input_rows = json.loads(Path(input_json).read_text(encoding="utf-8-sig"))
        if not isinstance(input_rows, list) or not all(isinstance(row, dict) for row in input_rows):
            raise ValueError("AiNiee 输入不是 Paratranz 对象数组。")
        data = _restore_excluded_rows(input_rows, data, output, diagnostic_log)
        if diagnostic_log:
            diagnostic_log(f"ainiee.translate.complete rows={len(data)}")
        return data


PROOFREAD_EVENT_PREFIX = "WOLFLATOR_PROOFREAD_EVENT "


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
    validate_source: Callable[[str | Path], Path] = validate_ainiee_source,
    uv_locator: Callable[[], Path] = locate_uv,
    process_runner: Callable[..., object] = run_process,
) -> dict[str, object]:
    root = validate_source(runtime)
    mode = settings.proofread_mode
    if mode not in {"rules", "rules_ai"}:
        raise ValueError(f"不支持的校对方式: {mode}")
    payload = json.loads(Path(input_json).read_text(encoding="utf-8"))
    payload = require_format(
        payload,
        kind="proofread-worker-input",
        version_key="epoch",
        version=ARTIFACT_EPOCH,
        label="AiNiee 校对 worker 输入",
    )
    if set(payload) != {"kind", "epoch", "source_sha256", "rows"} or not isinstance(
        payload["source_sha256"], str
    ) or not isinstance(payload["rows"], list):
        raise ValueError("AiNiee 校对 worker 输入字段不匹配。")
    managed_rules = _rules_with_control_protection(rules)
    payload["config"] = _session_profile(settings, "")
    payload["glossary"] = managed_rules.get("prompt_dictionary_data", [])
    atomic_write_json(input_json, payload)
    output = Path(output_json).resolve()
    output.unlink(missing_ok=True)
    child_env = os.environ.copy()
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONIOENCODING"] = "utf-8"
    if mode == "rules_ai":
        child_env["WOLFLATOR_PROOFREAD_API_KEY"] = api_key

    def receive_output(stream: str, line: str) -> None:
        if stream != "stdout" or not line.startswith(PROOFREAD_EVENT_PREFIX):
            return
        try:
            event = json.loads(line[len(PROOFREAD_EVENT_PREFIX) :])
        except json.JSONDecodeError:
            return
        if isinstance(event, dict) and progress is not None:
            progress(event)

    command = [
        str(uv_locator()),
        "run",
        "--frozen",
        "--no-sync",
        "python",
        str(resource_path("ainiee_proofread_worker.py").resolve()),
        "--runtime",
        str(root),
        "--input",
        str(Path(input_json).resolve()),
        "--output",
        str(output),
        "--mode",
        mode,
        "--batch-size",
        str(settings.proofread_batch_size),
        "--confidence",
        str(settings.proofread_confidence_percent),
        "--threads",
        str(max(1, settings.api_threads)),
    ]
    # ponytail: AiNiee owns one managed runtime. Serialize proofread and translation;
    # use separate runtime copies if simultaneous project jobs become a requirement.
    with runtime_lock(root.parent, "proofread"):
        process_runner(
            command,
            cwd=root,
            timeout=24 * 3600,
            cancel_event=cancel_event,
            log=log,
            diagnostic_log=diagnostic_log,
            env=child_env,
            hide_window=True,
            output_line=receive_output,
        )
    if not output.is_file():
        raise RuntimeError("AiNiee 校对 worker 返回成功，但没有生成结果。")
    result = json.loads(output.read_text(encoding="utf-8"))
    require_format(
        result,
        kind="proofread-worker-output",
        version_key="epoch",
        version=ARTIFACT_EPOCH,
        label="AiNiee 校对 worker 输出",
    )
    if (
        set(result) != {"kind", "epoch", "entries", "failed_batches"}
        or not isinstance(result.get("entries"), dict)
        or not isinstance(result.get("failed_batches"), list)
    ):
        raise ValueError("AiNiee 校对 worker 输出结构不匹配。")
    return result
