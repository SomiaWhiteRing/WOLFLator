from __future__ import annotations

import argparse
import json
import re
import sys
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterator


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QCoreApplication  # noqa: E402

import ainiee  # noqa: E402
import ainiee_translation  # noqa: E402
from models import ImportCategory, ImportScope, TranslationItem  # noqa: E402
from safe_io import atomic_write_json  # noqa: E402
from settings import SettingsStore, local_data_dir  # noqa: E402
from wolf_workbook import (  # noqa: E402
    _EXTERNAL_DISPLAY_COMMAND_RE,
    _protect_spans,
    _restore_tokens,
    _scan_control_spans,
    _validate_line_structure,
    dump_items,
    load_items,
    retryable_translation_errors,
    to_paratranz,
)


VARIANTS = ("raw-check-on", "raw-check-off", "legacy-pua-off")
CASE_LABELS = (
    "display-multiline",
    "display-controls",
    "segment-50",
    "external-plain-max",
    "external-title-command",
    "external-controls",
)
SCOPE = ImportScope(display=True, external=True)


def _newline_count(text: str) -> int:
    return len(re.findall(r"\r\n|\n|\r", text))


def _select_test_items(items: list[TranslationItem]) -> tuple[list[TranslationItem], list[str]]:
    selected: list[TranslationItem] = []
    labels: list[str] = []

    def pick(label: str, predicate: Callable[[TranslationItem], bool]) -> None:
        candidates = [item for item in items if item.key not in {row.key for row in selected} and predicate(item)]
        if not candidates:
            raise ValueError(f"No item found for benchmark case: {label}")
        item = max(candidates, key=lambda row: (_newline_count(row.original), len(row.original), row.code))
        selected.append(replace(item, translation="", stage=0))
        labels.append(label)

    pick(
        "display-multiline",
        lambda item: item.category is ImportCategory.DISPLAY
        and _newline_count(item.original) > 0
        and not item.control_signature,
    )
    pick(
        "display-controls",
        lambda item: item.category is ImportCategory.DISPLAY
        and _newline_count(item.original) > 0
        and bool(item.control_signature),
    )
    pick("segment-50", lambda item: item.code.startswith("SEGMENT_50-TXTFILE"))
    pick(
        "external-plain-max",
        lambda item: item.category is ImportCategory.EXTERNAL
        and _newline_count(item.original) > 0
        and not any(_EXTERNAL_DISPLAY_COMMAND_RE.match(line) for line in item.original.splitlines()),
    )
    pick(
        "external-title-command",
        lambda item: item.category is ImportCategory.EXTERNAL and "@\u30bf\u30a4\u30c8\u30eb\u30b3\u30fc\u30eb" in item.original,
    )
    pick(
        "external-controls",
        lambda item: item.category is ImportCategory.EXTERNAL and bool(item.control_signature),
    )
    return selected, labels


def _legacy_external_spans(text: str) -> list[tuple[int, int]]:
    lines = list(re.finditer(r".*?(?:\r\n|\n|\r|$)", text))
    if not any(_EXTERNAL_DISPLAY_COMMAND_RE.match(match.group(0).rstrip("\r\n")) for match in lines):
        return [match.span() for match in re.finditer(r"\r\n|\n|\r", text)]

    first = next((match.group(0).rstrip("\r\n").strip() for match in lines if match.group(0).strip()), "")
    display_payload = not first.startswith(("@", "\u25cf", "-", "//"))
    spans: list[tuple[int, int]] = []
    for match in lines:
        line = match.group(0)
        if not line:
            continue
        content = line.rstrip("\r\n")
        stripped = content.strip()
        line_end = match.start() + len(content)
        if stripped.startswith("@"):
            spans.append(match.span())
            display_payload = bool(_EXTERNAL_DISPLAY_COMMAND_RE.match(content))
        elif stripped.startswith("\u25cf") or (stripped and set(stripped) == {"-"}):
            spans.append(match.span())
            display_payload = False
        elif stripped.startswith("//") or not display_payload:
            spans.append(match.span())
        elif line_end < match.end():
            spans.append((line_end, match.end()))

    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _legacy_payload(
    items: list[TranslationItem],
) -> tuple[list[dict[str, object]], dict[str, list[str]]]:
    output: list[dict[str, object]] = []
    tokens_by_key: dict[str, list[str]] = {}
    for item in items:
        structural = _legacy_external_spans(item.original) if item.category is ImportCategory.EXTERNAL else []
        controls = [
            span
            for span in _scan_control_spans(item.original)
            if not any(span[0] < end and start < span[1] for start, end in structural)
        ]
        protected, tokens = _protect_spans(item.original, sorted((*structural, *controls)))
        tokens_by_key[item.key] = tokens
        output.append(
            {
                "key": item.key,
                "original": protected,
                "translation": "",
                "context": item.context,
                "stage": 0,
            }
        )
    return output, tokens_by_key


@contextmanager
def _newline_check(enabled: bool) -> Iterator[None]:
    original = ainiee_translation._session_profile

    def profile(settings: object, api_key: str) -> dict[str, object]:
        result = original(settings, api_key)  # type: ignore[arg-type]
        result["response_check_switch"] = {"newline_character_count_check": enabled}
        return result

    ainiee_translation._session_profile = profile  # type: ignore[assignment]
    try:
        yield
    finally:
        ainiee_translation._session_profile = original


def _legacy_validation_errors(
    items: list[TranslationItem],
    translated: list[dict[str, object]],
    tokens_by_key: dict[str, list[str]],
) -> dict[str, str]:
    rows = {str(row.get("key", "")): row for row in translated}
    errors: dict[str, str] = {}
    for item in items:
        row = rows.get(item.key)
        if row is None:
            errors[item.key] = "missing output"
            continue
        raw = str(row.get("translation", ""))
        if not raw.strip():
            errors[item.key] = "empty translation"
            continue
        try:
            restored = _restore_tokens(raw, tokens_by_key[item.key])
            _validate_line_structure(item.original, restored)
        except ValueError as exc:
            errors[item.key] = str(exc)
    return errors


def _read_run_stats(output_dir: Path) -> dict[str, object]:
    cache_path = output_dir / "cache" / "AiNieeCacheData.json"
    stats: dict[str, object] = {}
    if cache_path.is_file():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        if isinstance(cache, dict) and isinstance(cache.get("stats_data"), dict):
            stats = dict(cache["stats_data"])
    logs = sorted((output_dir / "logs").glob("session_*.log"))
    log_path = logs[-1] if logs else None
    log_text = log_path.read_text(encoding="utf-8", errors="replace") if log_path else ""
    return {
        "ainiee": stats,
        "log": str(log_path) if log_path else "",
        "request_error_lines": log_text.count("Request error"),
    }


def _run_variant(
    variant: str,
    items: list[TranslationItem],
    root: Path,
    runtime: Path,
    rules: dict[str, object],
    settings: object,
    api_key: str,
    timeout: int,
) -> dict[str, object]:
    variant_dir = root / variant
    variant_dir.mkdir()
    if variant == "legacy-pua-off":
        payload, legacy_tokens = _legacy_payload(items)
    else:
        payload = to_paratranz(items, SCOPE)
        legacy_tokens = {}
    input_path = variant_dir / "input.json"
    atomic_write_json(input_path, payload)
    output_dir = variant_dir / "output"

    def bounded_runner(*args: object, **kwargs: object) -> object:
        kwargs["timeout"] = timeout
        return ainiee.run_process(*args, **kwargs)

    started = time.perf_counter()
    translated: list[dict[str, object]] = []
    error = ""
    print(f"START {variant}: rows={len(payload)} timeout={timeout}s", flush=True)
    try:
        with _newline_check(variant == "raw-check-on"):
            translated = ainiee_translation.run_translation(
                runtime,
                input_path,
                output_dir,
                rules,
                f"wolflator-short-benchmark-{variant}",
                settings,  # type: ignore[arg-type]
                api_key,
                validate_source=ainiee.validate_ainiee_source,
                uv_locator=ainiee.locate_uv,
                process_runner=bounded_runner,
            )
    except Exception as exc:  # The result records bounded transport failures for comparison.
        error = f"{type(exc).__name__}: {exc}"
    elapsed = time.perf_counter() - started
    if translated:
        validation_errors = (
            _legacy_validation_errors(items, translated, legacy_tokens)
            if variant == "legacy-pua-off"
            else retryable_translation_errors(items, translated, SCOPE)
        )
        atomic_write_json(variant_dir / "translated.json", translated)
    else:
        validation_errors = {item.key: "no translated result" for item in items}
    result = {
        "variant": variant,
        "elapsed_seconds": round(elapsed, 3),
        "process_error": error,
        "validation_error_count": len(validation_errors),
        "validation_errors": validation_errors,
        **_read_run_stats(output_dir),
    }
    atomic_write_json(variant_dir / "result.json", result)
    print(
        f"DONE  {variant}: elapsed={elapsed:.1f}s process_error={bool(error)} "
        f"validation_errors={len(validation_errors)}",
        flush=True,
    )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bounded real-API benchmark for AiNiee multiline handling.")
    parser.add_argument("--items", type=Path, required=True, help="items-extracted.json from a real version")
    parser.add_argument("--glossary", type=Path, required=True, help="project glossary.json")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=240, help="hard limit per variant in seconds")
    parser.add_argument("--rounds", type=int, default=2, help="AiNiee rounds per variant")
    parser.add_argument("--threads", type=int, help="override saved AiNiee request concurrency")
    parser.add_argument("--token-limit", type=int, help="override saved token chunk limit")
    parser.add_argument("--cases", nargs="+", choices=CASE_LABELS, default=list(CASE_LABELS))
    parser.add_argument("--variants", nargs="+", choices=VARIANTS, default=list(VARIANTS))
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.output_root.exists():
        raise FileExistsError(f"Refusing to overwrite benchmark directory: {args.output_root}")
    if (
        args.timeout < 30
        or args.rounds < 1
        or (args.threads is not None and args.threads < 1)
        or (args.token_limit is not None and args.token_limit < 32)
    ):
        raise ValueError("timeout >= 30, rounds >= 1, threads >= 1, token-limit >= 32")

    QCoreApplication.setApplicationName("WOLFLator")
    QCoreApplication.setOrganizationName("WOLFLator")
    store = SettingsStore()
    saved_settings = store.load()
    settings = replace(
        saved_settings,
        translation_rounds=args.rounds,
        api_threads=args.threads if args.threads is not None else saved_settings.api_threads,
        translation_token_limit=(
            args.token_limit if args.token_limit is not None else saved_settings.translation_token_limit
        ),
    )
    api_key = store.api_key(settings)
    if not api_key:
        raise ValueError("The saved WOLFLator API key is empty.")
    runtime = ainiee.require_managed_runtime(
        settings.ainiee_source,
        local_data_dir() / "runtime" / "ainiee",
    )
    rules = json.loads(args.glossary.read_text(encoding="utf-8"))
    if not isinstance(rules, dict):
        raise ValueError("glossary.json must contain an object")

    items, labels = _select_test_items(load_items(args.items))
    chosen = set(args.cases)
    selected_pairs = [(item, label) for item, label in zip(items, labels) if label in chosen]
    items = [item for item, _label in selected_pairs]
    labels = [label for _item, label in selected_pairs]
    if not items:
        raise ValueError("At least one benchmark case is required.")
    args.output_root.mkdir(parents=True)
    dump_items(args.output_root / "short-test-items.json", items)
    cases = [
        {
            "label": label,
            "key": item.key,
            "code": item.code,
            "category": item.category.value,
            "characters": len(item.original),
            "newlines": _newline_count(item.original),
            "controls": len(item.control_signature),
        }
        for label, item in zip(labels, items)
    ]
    atomic_write_json(args.output_root / "short-test-cases.json", cases)
    print("TEST CASES", flush=True)
    for case in cases:
        print(
            f"  {case['label']}: {case['code']} chars={case['characters']} "
            f"newlines={case['newlines']} controls={case['controls']}",
            flush=True,
        )

    results = [
        _run_variant(
            variant,
            items,
            args.output_root,
            runtime,
            rules,
            settings,
            api_key,
            args.timeout,
        )
        for variant in args.variants
    ]
    summary = {
        "items": str(args.items.resolve()),
        "glossary": str(args.glossary.resolve()),
        "model": settings.api_model,
        "token_limit": settings.translation_token_limit,
        "threads": settings.api_threads,
        "rounds": settings.translation_rounds,
        "timeout_seconds": args.timeout,
        "cases": cases,
        "results": results,
    }
    atomic_write_json(args.output_root / "benchmark-summary.json", summary)
    return 0 if any(not row["process_error"] and row["validation_error_count"] == 0 for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
