from __future__ import annotations

import json
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from ainiee import require_managed_runtime, run_proofread
from models import AppSettings, ImportCategory, ProjectManifest, Stage, StageStatus, TranslationItem
from safe_io import atomic_write_json, read_text_with_retry
from wolf_tools import (
    load_items,
    protect_control_tokens,
    restore_control_tokens,
    selected_translation_items,
    sha256_file,
)


PROOFREAD_SCHEMA = 1
PROOFREAD_MODES = {"rules", "rules_ai"}
DECISIONS = {"pending", "accept", "keep"}
SEVERITY_ORDER = {"low": 1, "medium": 2, "high": 3}
REPORT_FIELDS = {
    "schema",
    "status",
    "created_at",
    "source_sha256",
    "mode",
    "model",
    "config",
    "summary",
    "failed_batches",
    "entries",
}
ENTRY_FIELDS = {
    "key",
    "code",
    "original",
    "translation",
    "suggested_translation",
    "edited_translation",
    "issues",
    "severity",
    "decision",
    "applicable",
    "apply_error",
}
ISSUE_FIELDS = {"source", "type", "severity", "description", "suggestion", "confidence"}
SUMMARY_FIELDS = {
    "checked",
    "affected",
    "high",
    "medium",
    "low",
    "accepted",
    "kept",
    "pending",
    "failed_batches",
}


def _exact_fields(value: object, expected: set[str], label: str) -> dict:
    if not isinstance(value, dict) or set(value) != expected:
        actual = sorted(value) if isinstance(value, dict) else type(value).__name__
        raise ValueError(f"{label}字段不匹配: {actual}")
    return value


def proofread_paths(manifest_path: str | Path, manifest: ProjectManifest) -> tuple[Path, Path]:
    root = (
        Path(manifest_path).resolve().parent
        / "versions"
        / manifest.active_version
        / "artifacts"
        / "proofread"
    )
    return root / "report.json", root / "proofread.log"


def build_worker_input(
    items_path: str | Path,
    manifest: ProjectManifest,
    *,
    context_lines: int,
) -> dict[str, object]:
    if not 0 <= context_lines <= 20:
        raise ValueError("校对上下文行数必须在 0 到 20 之间。")
    items = selected_translation_items(
        load_items(items_path),
        manifest.translation_scope,
        allow_copy_condition_groups=manifest.import_protection.allow_copy_condition_groups,
    )
    items = [
        item
        for item in items
        if item.translation and item.category is not ImportCategory.COPY
    ]
    prepared: list[tuple[TranslationItem, str, str, list[str]]] = []
    for item in items:
        original, tokens = protect_control_tokens(item.original)
        translation, translated_tokens = protect_control_tokens(item.translation)
        if tokens != item.control_signature or translated_tokens != tokens:
            raise ValueError(f"控制符签名发生变化，无法校对: {item.code or item.key}")
        prepared.append((item, original, translation, tokens))
    rows: list[dict[str, object]] = []
    for index, (item, original, translation, tokens) in enumerate(prepared):
        start = max(0, index - context_lines)
        end = min(len(prepared), index + context_lines + 1)
        context = [
            {
                "original": prepared[nearby][1],
                "translation": prepared[nearby][2],
            }
            for nearby in range(start, end)
            if nearby != index
        ]
        rows.append(
            {
                "index": index,
                "key": item.key,
                "code": item.code,
                "original": original,
                "translation": translation,
                "context": context,
                "tokens": tokens,
            }
        )
    return {
        "schema": PROOFREAD_SCHEMA,
        "source_sha256": sha256_file(items_path),
        "rows": rows,
    }


def write_worker_input(path: str | Path, payload: dict[str, object]) -> Path:
    return atomic_write_json(path, payload)


def _restore_optional(text: object, tokens: list[str]) -> str:
    value = str(text or "")
    if not value:
        return ""
    try:
        return restore_control_tokens(value, tokens)
    except ValueError:
        # Issue-level suggestions are often fragments, so restore any placeholders
        # they reference without treating the fragment as a full translation.
        for index, token in enumerate(tokens):
            value = value.replace(chr(0xE100 + index), token)
        return value


def make_report(
    worker_input: dict[str, object],
    worker_result: dict[str, object],
    *,
    mode: str,
    model: str,
    batch_size: int,
    context_lines: int,
    confidence_percent: int,
) -> dict[str, object]:
    if mode not in PROOFREAD_MODES:
        raise ValueError(f"不支持的校对方式: {mode}")
    rows = worker_input.get("rows")
    by_key = worker_result.get("entries")
    failed_batches = worker_result.get("failed_batches", [])
    if not isinstance(rows, list) or not isinstance(by_key, dict) or not isinstance(failed_batches, list):
        raise ValueError("校对 worker 输出结构不匹配。")
    row_keys = {str(row.get("key", "")) for row in rows if isinstance(row, dict)}
    unknown = set(map(str, by_key)) - row_keys
    if unknown:
        raise ValueError(f"校对 worker 返回未知键: {sorted(unknown)}")
    entries: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("校对输入行不是对象。")
        key = str(row.get("key", ""))
        result = by_key.get(key)
        if not isinstance(result, dict):
            continue
        issues_value = result.get("issues", [])
        if not isinstance(issues_value, list) or not issues_value:
            continue
        tokens = list(map(str, row.get("tokens", [])))
        issues: list[dict[str, object]] = []
        for raw in issues_value:
            if not isinstance(raw, dict):
                continue
            severity = str(raw.get("severity", "low")).lower()
            if severity not in SEVERITY_ORDER:
                severity = "low"
            confidence = raw.get("confidence", 1.0)
            confidence = float(confidence) if isinstance(confidence, (int, float)) else 1.0
            issues.append(
                {
                    "source": str(raw.get("source", "rule")),
                    "type": str(raw.get("type", "unknown")),
                    "severity": severity,
                    "description": str(raw.get("description", "")),
                    "suggestion": _restore_optional(raw.get("suggestion", ""), tokens),
                    "confidence": max(0.0, min(confidence, 1.0)),
                }
            )
        if not issues:
            continue
        suggested = str(result.get("suggested_translation", "") or row.get("translation", ""))
        applicable = True
        apply_error = ""
        try:
            suggested = restore_control_tokens(suggested, tokens)
            if not suggested:
                raise ValueError("建议译文为空。")
        except ValueError as exc:
            suggested = restore_control_tokens(str(row.get("translation", "")), tokens)
            applicable = False
            apply_error = str(exc)
        severity = max(issues, key=lambda issue: SEVERITY_ORDER[str(issue["severity"])])["severity"]
        entries.append(
            {
                "key": key,
                "code": str(row.get("code", "")),
                "original": restore_control_tokens(str(row.get("original", "")), tokens),
                "translation": restore_control_tokens(str(row.get("translation", "")), tokens),
                "suggested_translation": suggested,
                "edited_translation": suggested,
                "issues": issues,
                "severity": severity,
                "decision": "pending",
                "applicable": applicable,
                "apply_error": apply_error,
            }
        )
    report: dict[str, object] = {
        "schema": PROOFREAD_SCHEMA,
        "status": "partial" if failed_batches else "completed",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_sha256": str(worker_input.get("source_sha256", "")),
        "mode": mode,
        "model": model,
        "config": {
            "batch_size": batch_size,
            "context_lines": context_lines,
            "confidence_percent": confidence_percent,
        },
        "summary": {},
        "failed_batches": failed_batches,
        "entries": entries,
    }
    refresh_summary(report, checked=len(rows))
    validate_report(report)
    return report


def refresh_summary(report: dict[str, object], *, checked: int | None = None) -> None:
    entries = report.get("entries", [])
    failed = report.get("failed_batches", [])
    old = report.get("summary", {})
    checked_value = checked if checked is not None else int(old.get("checked", 0))
    report["summary"] = {
        "checked": checked_value,
        "affected": len(entries),
        "high": sum(entry.get("severity") == "high" for entry in entries),
        "medium": sum(entry.get("severity") == "medium" for entry in entries),
        "low": sum(entry.get("severity") == "low" for entry in entries),
        "accepted": sum(entry.get("decision") == "accept" for entry in entries),
        "kept": sum(entry.get("decision") == "keep" for entry in entries),
        "pending": sum(entry.get("decision") == "pending" for entry in entries),
        "failed_batches": len(failed),
    }


def validate_report(report: object) -> dict[str, object]:
    value = _exact_fields(report, REPORT_FIELDS, "校对报告")
    if value["schema"] != PROOFREAD_SCHEMA:
        raise ValueError(f"不支持的校对报告 schema: {value['schema']}")
    if value["status"] not in {"completed", "partial"} or value["mode"] not in PROOFREAD_MODES:
        raise ValueError("校对报告状态或方式无效。")
    if not all(isinstance(value[name], str) for name in ("created_at", "source_sha256", "model")):
        raise ValueError("校对报告文本字段类型不匹配。")
    config = _exact_fields(value["config"], {"batch_size", "context_lines", "confidence_percent"}, "校对配置")
    if not (
        type(config["batch_size"]) is int
        and 1 <= config["batch_size"] <= 100
        and type(config["context_lines"]) is int
        and 0 <= config["context_lines"] <= 20
        and type(config["confidence_percent"]) is int
        and 0 <= config["confidence_percent"] <= 100
    ):
        raise ValueError("校对报告配置超出范围。")
    summary = _exact_fields(value["summary"], SUMMARY_FIELDS, "校对汇总")
    if any(type(number) is not int or number < 0 for number in summary.values()):
        raise ValueError("校对汇总必须是非负整数。")
    if not isinstance(value["failed_batches"], list):
        raise ValueError("失败批次必须是对象数组。")
    for batch_value in value["failed_batches"]:
        batch = _exact_fields(batch_value, {"batch", "keys", "error"}, "失败批次")
        if (
            type(batch["batch"]) is not int
            or batch["batch"] < 1
            or not isinstance(batch["keys"], list)
            or not all(isinstance(key, str) and key for key in batch["keys"])
            or not isinstance(batch["error"], str)
        ):
            raise ValueError("失败批次字段类型不匹配。")
    entries = value["entries"]
    if not isinstance(entries, list):
        raise ValueError("校对条目必须是数组。")
    keys: set[str] = set()
    for entry_value in entries:
        entry = _exact_fields(entry_value, ENTRY_FIELDS, "校对条目")
        if not all(isinstance(entry[name], str) for name in (
            "key", "code", "original", "translation", "suggested_translation",
            "edited_translation", "severity", "decision", "apply_error",
        )):
            raise ValueError("校对条目文本字段类型不匹配。")
        if not entry["key"] or entry["key"] in keys:
            raise ValueError("校对条目包含空键或重复键。")
        keys.add(entry["key"])
        if entry["severity"] not in SEVERITY_ORDER or entry["decision"] not in DECISIONS:
            raise ValueError("校对条目严重程度或决定无效。")
        if type(entry["applicable"]) is not bool or not isinstance(entry["issues"], list):
            raise ValueError("校对条目可应用标记或问题数组无效。")
        for issue_value in entry["issues"]:
            issue = _exact_fields(issue_value, ISSUE_FIELDS, "校对问题")
            if not all(isinstance(issue[name], str) for name in ISSUE_FIELDS - {"confidence"}):
                raise ValueError("校对问题文本字段类型不匹配。")
            if issue["severity"] not in SEVERITY_ORDER or not isinstance(issue["confidence"], (int, float)):
                raise ValueError("校对问题严重程度或置信度无效。")
    expected_summary = {
        "affected": len(entries),
        "high": sum(entry["severity"] == "high" for entry in entries),
        "medium": sum(entry["severity"] == "medium" for entry in entries),
        "low": sum(entry["severity"] == "low" for entry in entries),
        "accepted": sum(entry["decision"] == "accept" for entry in entries),
        "kept": sum(entry["decision"] == "keep" for entry in entries),
        "pending": sum(entry["decision"] == "pending" for entry in entries),
        "failed_batches": len(value["failed_batches"]),
    }
    if any(summary[name] != expected for name, expected in expected_summary.items()):
        raise ValueError("校对汇总与条目内容不一致。")
    if summary["checked"] < summary["affected"]:
        raise ValueError("校对检查数不能小于受影响条目数。")
    return value


def load_report(path: str | Path) -> dict[str, object]:
    return validate_report(json.loads(read_text_with_retry(path, encoding="utf-8")))


def save_report(path: str | Path, report: dict[str, object]) -> Path:
    refresh_summary(report)
    validate_report(report)
    return atomic_write_json(path, report)


def report_is_stale(report: dict[str, object], items_path: str | Path) -> bool:
    return str(report.get("source_sha256", "")) != sha256_file(items_path)


def accepted_translations(
    report: dict[str, object],
    items: Iterable[TranslationItem],
    *,
    source_sha256: str,
) -> dict[str, str]:
    validate_report(report)
    if report["source_sha256"] != source_sha256:
        raise ValueError("校对报告已过期，请重新校对。")
    by_key = {item.key: item for item in items}
    output: dict[str, str] = {}
    for entry in report["entries"]:
        if entry["decision"] != "accept":
            continue
        item = by_key.get(entry["key"])
        if item is None or item.translation != entry["translation"]:
            raise ValueError(f"校对条目源译文已变化: {entry['code'] or entry['key']}")
        text = entry["edited_translation"]
        if not text:
            raise ValueError(f"建议译文为空: {entry['code'] or entry['key']}")
        _protected, tokens = protect_control_tokens(text)
        if tokens != item.control_signature:
            raise ValueError(f"建议译文控制符数量或顺序不一致: {entry['code'] or entry['key']}")
        output[item.key] = text
    return output


def run_project_proofread(
    manifest_path: str | Path,
    manifest: ProjectManifest,
    settings: AppSettings,
    api_key: str,
    cache_root: str | Path,
    *,
    cancel_event: threading.Event | None = None,
    progress: Callable[[dict[str, object]], None] | None = None,
    log: Callable[[str], None] | None = None,
) -> Path:
    translate = manifest.version.stage(Stage.TRANSLATE)
    if translate.status is not StageStatus.COMPLETED:
        raise RuntimeError("完成 AI 翻译后即可使用校对功能。")
    items_value = translate.artifacts.get("items", "")
    items_path = Path(items_value)
    if not items_value or not items_path.is_file():
        raise FileNotFoundError("AI 翻译产物不存在，无法开始校对。")
    if settings.proofread_mode == "rules_ai" and not api_key:
        raise ValueError("AI 校对需要当前翻译 API 密钥。")

    report_path, log_path = proofread_paths(manifest_path, manifest)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    input_path = report_path.parent / "worker-input.json"
    result_path = report_path.parent / ".worker-result.json"
    result_path.unlink(missing_ok=True)
    worker_input = build_worker_input(
        items_path,
        manifest,
        context_lines=settings.proofread_context_lines,
    )
    write_worker_input(input_path, worker_input)
    glossary_path = Path(manifest_path).resolve().parent / "glossary.json"
    rules: dict[str, object] = {}
    if glossary_path.is_file():
        loaded = json.loads(glossary_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            rules = loaded
    runtime = require_managed_runtime(
        settings.ainiee_source,
        Path(cache_root) / "runtime" / "ainiee",
    )

    def write_log(message: str) -> None:
        if api_key:
            message = message.replace(api_key, "[REDACTED]")
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        with log_path.open("a", encoding="utf-8") as stream:
            stream.write(f"{stamp} {message.rstrip()}\n")
        if log is not None:
            log(message)

    log_path.write_text(
        "WOLFLator proofread log\n"
        f"started_at={datetime.now().astimezone().isoformat(timespec='seconds')}\n"
        f"mode={settings.proofread_mode}\nmodel={settings.api_model}\n",
        encoding="utf-8",
    )
    try:
        result = run_proofread(
            runtime,
            input_path,
            result_path,
            rules,
            settings,
            api_key,
            cancel_event=cancel_event,
            progress=progress,
            log=write_log,
            diagnostic_log=write_log,
        )
        report = make_report(
            worker_input,
            result,
            mode=settings.proofread_mode,
            model=settings.api_model,
            batch_size=settings.proofread_batch_size,
            context_lines=settings.proofread_context_lines,
            confidence_percent=settings.proofread_confidence_percent,
        )
        save_report(report_path, report)
        write_log(
            f"校对完成：检查 {report['summary']['checked']} 条，"
            f"发现 {report['summary']['affected']} 条问题，"
            f"失败批次 {report['summary']['failed_batches']}。"
        )
        return report_path
    except Exception:
        write_log("校对未发布新报告：\n" + traceback.format_exc())
        raise
    finally:
        result_path.unlink(missing_ok=True)
