from __future__ import annotations

import argparse
import json
import sys
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Iterator


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QCoreApplication  # noqa: E402

import ainiee  # noqa: E402
import ainiee_translation  # noqa: E402
from ainiee_runtime import _atomic_bytes  # noqa: E402
from models import ImportScope, TranslationItem  # noqa: E402
from safe_io import atomic_write_json  # noqa: E402
from settings import SettingsStore, local_data_dir  # noqa: E402
from wolf_workbook import load_items, retryable_translation_errors  # noqa: E402


SCOPE = ImportScope(display=True, external=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one hard-bounded AiNiee throughput trial on an existing real input."
    )
    parser.add_argument("--input", type=Path, required=True, help="Existing AiNiee Paratranz input JSON")
    parser.add_argument("--items", type=Path, required=True, help="Matching WOLFLator items JSON")
    parser.add_argument("--glossary", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--token-limit", type=int, required=True)
    parser.add_argument("--threads", type=int, required=True)
    parser.add_argument("--rounds", type=int, default=1)
    parser.add_argument("--request-timeout", type=int, default=120)
    parser.add_argument("--request-retries", type=int, default=3)
    parser.add_argument("--transport", choices=("openai-sdk", "httpx"), default="openai-sdk")
    parser.add_argument("--sdk-max-retries", type=int, default=2)
    parser.add_argument("--thinking", choices=("enabled", "disabled"), default="disabled")
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high"), default="low")
    parser.add_argument("--fragment-structure", action="store_true")
    parser.add_argument("--hard-timeout", type=int, default=600)
    return parser


def _session_log_stats(output: Path) -> dict[str, object]:
    logs = sorted((output / "logs").glob("session_*.log"))
    log = logs[-1] if logs else None
    text = log.read_text(encoding="utf-8", errors="replace") if log else ""
    return {
        "session_log": str(log) if log else "",
        "request_errors": text.count("Request error ("),
        "request_timeouts": text.count("Request timed out."),
    }


def _cache_stats(output: Path) -> dict[str, object]:
    path = output / "cache" / "AinieeCacheData.json"
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return dict(data.get("stats_data") or {}) if isinstance(data, dict) else {}


def _selected_items(items_path: Path, input_rows: list[dict[str, object]]) -> list[TranslationItem]:
    by_key = {item.key: item for item in load_items(items_path)}
    missing = [str(row.get("key", "")) for row in input_rows if str(row.get("key", "")) not in by_key]
    if missing:
        raise ValueError(f"Input keys missing from items file: {missing[:3]}")
    return [by_key[str(row["key"])] for row in input_rows]


@contextmanager
def _benchmark_profile(
    transport: str,
    request_retries: int,
    thinking: str,
    reasoning_effort: str,
) -> Iterator[None]:
    original = ainiee_translation._session_profile

    def profile(settings: object, api_key: str) -> dict[str, object]:
        result = original(settings, api_key)  # type: ignore[arg-type]
        use_sdk = transport == "openai-sdk"
        result["sdk_request_mode"] = "openai" if use_sdk else "httpx"
        result["use_openai_sdk"] = use_sdk
        result["auto_complete"] = not use_sdk
        result["retry_count"] = request_retries
        result["enable_retry_backoff"] = request_retries > 1
        platform = result["platforms"][result["target_platform"]]  # type: ignore[index]
        platform["think_switch"] = thinking == "enabled"  # type: ignore[index]
        platform["think_depth"] = reasoning_effort  # type: ignore[index]
        return result

    ainiee_translation._session_profile = profile  # type: ignore[assignment]
    try:
        yield
    finally:
        ainiee_translation._session_profile = original


@contextmanager
def _sdk_retry_limit(runtime: Path, max_retries: int) -> Iterator[None]:
    path = (
        runtime
        / "ModuleFolders"
        / "Infrastructure"
        / "LLMRequester"
        / "LLMClientFactory.py"
    )
    original = path.read_bytes()
    text = original.decode("utf-8")
    needle = "        return OpenAI(\n            base_url=config.get(\"api_url\"),"
    replacement = (
        "        return OpenAI(\n"
        f"            max_retries={max_retries},\n"
        "            base_url=config.get(\"api_url\"),"
    )
    if text.count(needle) != 1:
        raise RuntimeError("Could not locate the OpenAI client constructor for benchmark patching")
    _atomic_bytes(path, text.replace(needle, replacement, 1).encode("utf-8"))
    try:
        yield
    finally:
        _atomic_bytes(path, original)


def main() -> int:
    args = _parser().parse_args()
    if args.output_root.exists():
        raise FileExistsError(f"Refusing to overwrite benchmark directory: {args.output_root}")
    if min(
        args.token_limit,
        args.threads,
        args.rounds,
        args.request_timeout,
        args.request_retries,
        args.hard_timeout,
    ) < 1:
        raise ValueError("Numeric benchmark options must be positive")
    if args.sdk_max_retries < 0:
        raise ValueError("sdk-max-retries must be non-negative")

    input_rows = json.loads(args.input.read_text(encoding="utf-8"))
    glossary = json.loads(args.glossary.read_text(encoding="utf-8"))
    if not isinstance(input_rows, list) or not all(isinstance(row, dict) for row in input_rows):
        raise ValueError("Input JSON must be an array of objects")
    if not isinstance(glossary, dict):
        raise ValueError("Glossary JSON must be an object")
    items = _selected_items(args.items, input_rows)

    QCoreApplication.setApplicationName("WOLFLator")
    QCoreApplication.setOrganizationName("WOLFLator")
    store = SettingsStore()
    saved = store.load()
    settings = replace(
        saved,
        api_threads=args.threads,
        api_timeout=args.request_timeout,
        translation_chunk_mode="token",
        translation_token_limit=args.token_limit,
        translation_rounds=args.rounds,
    )
    api_key = store.api_key(settings)
    if not api_key:
        raise ValueError("The saved WOLFLator API key is empty")
    runtime = ainiee.require_managed_runtime(
        settings.ainiee_source,
        local_data_dir() / "runtime" / "ainiee",
    )

    args.output_root.mkdir(parents=True)
    run_input = args.input
    fragment_ledger: dict[str, list[dict[str, str]]] = {}
    fragment_rows: list[dict[str, object]] = []
    if args.fragment_structure:
        fragment_rows, fragment_ledger = ainiee_translation.fragment_translation_rows(input_rows)
        run_input = args.output_root / "fragment-input.json"
        atomic_write_json(run_input, fragment_rows)
        atomic_write_json(args.output_root / "fragment-ledger.json", fragment_ledger)
    output = args.output_root / "ainiee-output"

    def bounded_runner(*run_args: object, **run_kwargs: object) -> object:
        run_kwargs["timeout"] = args.hard_timeout
        return ainiee.run_process(*run_args, **run_kwargs)

    started = time.perf_counter()
    translated: list[dict[str, object]] = []
    missing_fragments: list[str] = []
    process_error = ""
    try:
        with _benchmark_profile(
            args.transport,
            args.request_retries,
            args.thinking,
            args.reasoning_effort,
        ), _sdk_retry_limit(runtime, args.sdk_max_retries):
            translated = ainiee_translation.run_translation(
                runtime,
                run_input,
                output,
                glossary,
                f"throughput-{args.token_limit}-{args.threads}",
                settings,
                api_key,
                validate_source=lambda _path: runtime,
                uv_locator=ainiee.locate_uv,
                process_runner=bounded_runner,
            )
    except Exception as exc:
        process_error = f"{type(exc).__name__}: {exc}"
    elapsed = time.perf_counter() - started

    if args.fragment_structure and translated:
        translated, missing_by_parent = ainiee_translation.merge_fragmented_rows(
            input_rows, translated, fragment_ledger
        )
        missing_fragments = [key for keys in missing_by_parent.values() for key in keys]

    validation_errors: dict[str, str] = {}
    if translated:
        validation_errors = retryable_translation_errors(items, translated, SCOPE)
        atomic_write_json(args.output_root / "translated.json", translated)
        if validation_errors:
            retry_rows = [row for row in input_rows if str(row.get("key", "")) in validation_errors]
            atomic_write_json(args.output_root / "retry-input.json", retry_rows)

    visible_chars = sum(len(str(row.get("translation", ""))) for row in translated)
    result = {
        "kind": "ainiee-throughput-benchmark",
        "input": str(args.input.resolve()),
        "rows": len(input_rows),
        "model": settings.api_model,
        "token_limit": args.token_limit,
        "threads": args.threads,
        "rounds": args.rounds,
        "request_timeout_seconds": args.request_timeout,
        "request_retries": args.request_retries,
        "transport": args.transport,
        "sdk_max_retries": args.sdk_max_retries,
        "thinking": args.thinking,
        "reasoning_effort": args.reasoning_effort,
        "hard_timeout_seconds": args.hard_timeout,
        "fragment_structure": args.fragment_structure,
        "fragment_rows": len(fragment_rows),
        "missing_fragment_count": len(missing_fragments),
        "missing_fragments": missing_fragments,
        "elapsed_seconds": round(elapsed, 3),
        "process_error": process_error,
        "output_rows": len(translated),
        "visible_output_chars": visible_chars,
        "validation_error_count": len(validation_errors),
        "validation_errors": validation_errors,
        "ainiee": _cache_stats(output),
        **_session_log_stats(output),
    }
    atomic_write_json(args.output_root / "benchmark-result.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    return 0 if translated and not validation_errors and not process_error else 1


if __name__ == "__main__":
    raise SystemExit(main())
