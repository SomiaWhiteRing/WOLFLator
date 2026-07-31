from __future__ import annotations

import argparse
import builtins
import json
import os
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from formats import ARTIFACT_EPOCH, require_format


EVENT_PREFIX = "WOLFLATOR_PROOFREAD_EVENT "
_AI_PRINT_STATE = threading.local()
_ORIGINAL_PRINT = builtins.print
_BATCH_OUTPUT_CONTRACT = r"""

## WOLFLator 批量校对协议（优先于上文的单条输出格式）
本次输入由多条 Line 组成。只返回 JSON 列表，不要返回 Markdown 或解释：
[
  {
    "line_id": 0,
    "issues": [
      {
        "type": "terminology|omission|hallucination|logic_error|format_error",
        "severity": "high|medium|low",
        "description": "具体问题",
        "suggestion": "具体修改说明",
        "confidence": 0.95
      }
    ],
    "corrected_translation": "已修复全部问题、可直接替换的完整译文"
  }
]
凡返回 issues，必须同时返回非空且确实修复问题的 corrected_translation；即使问题为
medium 或 low 也不能省略。WOLFLator 已核验的规则问题必须保留并修复，其中的标点、
括号、占位符、控制字符和换行问题不受上文“忽略标点差异”限制。不得只返回修改说明或
局部片段。line_id 必须使用输入中的 Line 编号。没有问题的行不要返回。
"""


def emit(event: str, **values: object) -> None:
    print(EVENT_PREFIX + json.dumps({"event": event, **values}, ensure_ascii=False), flush=True)


def _capture_ai_print(*values: object, **kwargs: object) -> None:
    text = " ".join(map(str, values))
    messages = getattr(_AI_PRINT_STATE, "errors", None)
    if messages is not None and "[AI批量校对错误]" in text:
        messages.append(text)
    _ORIGINAL_PRINT(*values, **kwargs)


def write_json_atomic(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def _serialize_rule(issue: object) -> dict[str, object]:
    return {
        "source": "rule",
        "type": str(issue.rule_name),
        "severity": str(issue.severity).lower(),
        "description": str(issue.description),
        "suggestion": str(issue.fix_suggestion),
        "confidence": 1.0,
    }


def _serialize_ai(issue: object) -> dict[str, object]:
    return {
        "source": "ai",
        "type": str(issue.type),
        "severity": str(issue.severity).lower(),
        "description": str(issue.description),
        "suggestion": str(issue.suggestion),
        "confidence": float(issue.confidence),
    }


def _batch_context(rows: list[dict[str, object]]) -> str:
    seen: set[tuple[str, str]] = set()
    context: list[dict[str, str]] = []
    rule_findings: list[dict[str, object]] = []
    for row in rows:
        findings = row.get("rule_issues", [])
        if isinstance(findings, list) and findings:
            rule_findings.append({"line_id": int(row["index"]), "issues": findings})
        for nearby in row.get("context", []):
            if not isinstance(nearby, dict):
                continue
            pair = (str(nearby.get("original", "")), str(nearby.get("translation", "")))
            if pair in seen:
                continue
            seen.add(pair)
            context.append({"原文": pair[0], "译文": pair[1]})
    return json.dumps(
        {"相邻上下文": context, "WOLFLator已核验规则问题": rule_findings},
        ensure_ascii=False,
    )


def _run_ai_batch(
    number: int,
    rows: list[dict[str, object]],
    config: dict[str, object],
    glossary: list[dict[str, object]],
    confidence: float,
) -> tuple[int, dict[str, dict[str, object]], list[str]]:
    from ModuleFolders.Service.Proofreader.AIProofreader import AIProofreader

    checker = AIProofreader(config)
    checker.prompt_template = str(getattr(checker, "prompt_template", "")) + _BATCH_OUTPUT_CONTRACT
    items = [
        {
            "index": int(row["index"]),
            "source": str(row["original"]),
            "translation": str(row["translation"]),
        }
        for row in rows
    ]
    context = "相邻上下文，仅供参考，不属于待校对文本：\n" + _batch_context(rows)
    _AI_PRINT_STATE.errors = []
    try:
        raw = checker.proofread_lines_block(
            items,
            glossary=glossary,
            world_building=context,
        )
        if _AI_PRINT_STATE.errors:
            raise RuntimeError(_AI_PRINT_STATE.errors[-1])
    finally:
        _AI_PRINT_STATE.errors = None
    by_index = {int(row["index"]): row for row in rows}
    output: dict[str, dict[str, object]] = {}
    for index, result in raw.items():
        row = by_index.get(int(index))
        if row is None:
            continue
        issues = [_serialize_ai(issue) for issue in result.issues if float(issue.confidence) >= confidence]
        if issues:
            output[str(row["key"])] = {
                "issues": issues,
                "suggested_translation": str(result.corrected_translation or ""),
            }
    translations_by_key = {str(row["key"]): str(row["translation"]) for row in rows}
    missing_corrections = [
        key
        for key, entry in output.items()
        if not entry["suggested_translation"]
        or entry["suggested_translation"] == translations_by_key[key]
    ]
    expected_rule_keys = {str(row["key"]) for row in rows if row.get("rule_issues")}
    missing_corrections.extend(sorted(expected_rule_keys - set(output)))
    return number, output, sorted(set(missing_corrections))


def run(args: argparse.Namespace) -> int:
    runtime = Path(args.runtime).resolve()
    os.chdir(runtime)
    sys.path.insert(0, str(runtime))
    payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    require_format(
        payload,
        kind="proofread-worker-input",
        version_key="epoch",
        version=ARTIFACT_EPOCH,
        label="校对 worker 输入",
    )
    if set(payload) != {"kind", "epoch", "source_sha256", "rows", "config", "glossary"} or not isinstance(
        payload["source_sha256"], str
    ):
        raise ValueError("校对 worker 输入字段不匹配。")
    rows = payload.get("rows", [])
    config = payload.get("config", {})
    glossary = payload.get("glossary", [])
    if not isinstance(rows, list) or not isinstance(config, dict):
        raise ValueError("校对 worker 输入结构不匹配。")
    if not isinstance(glossary, list):
        glossary = []

    secret = os.environ.pop("WOLFLATOR_PROOFREAD_API_KEY", "")
    platform_tag = str(config.get("target_platform", ""))
    platforms = config.get("platforms")
    if isinstance(platforms, dict) and isinstance(platforms.get(platform_tag), dict):
        platforms[platform_tag]["api_key"] = secret
    config["api_key"] = secret
    config["proofread_confidence_threshold"] = args.confidence / 100
    config["prompt_dictionary_switch"] = True

    from ModuleFolders.Service.Proofreader.RuleBasedChecker import RuleBasedChecker

    entries: dict[str, dict[str, object]] = {}
    rule_checker = RuleBasedChecker()
    emit("started", total=len(rows), batches=(len(rows) + args.batch_size - 1) // args.batch_size)
    for offset, row in enumerate(rows, 1):
        issues = rule_checker.check(str(row.get("original", "")), str(row.get("translation", "")))
        if issues:
            serialized = [_serialize_rule(issue) for issue in issues]
            row["rule_issues"] = [
                {"type": issue["type"], "description": issue["description"]}
                for issue in serialized
            ]
            entries[str(row["key"])] = {
                "issues": serialized,
                "suggested_translation": "",
            }
        if args.mode == "rules" and (offset % args.batch_size == 0 or offset == len(rows)):
            emit("progress", current=offset, total=len(rows), failed_batches=0)

    failed_batches: list[dict[str, object]] = []
    batches = [rows[index : index + args.batch_size] for index in range(0, len(rows), args.batch_size)]
    if args.mode == "rules_ai" and batches:
        completed_rows = 0
        builtins.print = _capture_ai_print
        try:
            with ThreadPoolExecutor(max_workers=min(args.threads, len(batches))) as executor:
                pending = {
                    executor.submit(
                        _run_ai_batch,
                        number,
                        batch,
                        config,
                        glossary,
                        args.confidence / 100,
                    ): (number, batch)
                    for number, batch in enumerate(batches, 1)
                }
                for future in as_completed(pending):
                    number, batch = pending[future]
                    try:
                        _, ai_entries, missing_corrections = future.result()
                        for key, ai_entry in ai_entries.items():
                            target = entries.setdefault(
                                key,
                                {"issues": [], "suggested_translation": ai_entry["suggested_translation"]},
                            )
                            target["issues"].extend(ai_entry["issues"])
                            if ai_entry["suggested_translation"]:
                                target["suggested_translation"] = ai_entry["suggested_translation"]
                        if missing_corrections:
                            failed_batches.append(
                                {
                                    "batch": number,
                                    "keys": missing_corrections,
                                    "error": "AI 报告了问题，但未返回可直接替换的完整修订译文。",
                                }
                            )
                    except Exception as exc:
                        error = f"{type(exc).__name__}: {exc}"
                        if secret:
                            error = error.replace(secret, "[REDACTED]")
                        failed_batches.append(
                            {
                                "batch": number,
                                "keys": [str(row.get("key", "")) for row in batch],
                                "error": error,
                            }
                        )
                    completed_rows += len(batch)
                    emit(
                        "progress",
                        current=completed_rows,
                        total=len(rows),
                        failed_batches=len(failed_batches),
                    )
        finally:
            builtins.print = _ORIGINAL_PRINT

    write_json_atomic(
        Path(args.output),
        {
            "kind": "proofread-worker-output",
            "epoch": ARTIFACT_EPOCH,
            "entries": entries,
            "failed_batches": failed_batches,
        },
    )
    emit("finished", current=len(rows), total=len(rows), failed_batches=len(failed_batches))
    return 0


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser()
    value.add_argument("--runtime", required=True)
    value.add_argument("--input", required=True)
    value.add_argument("--output", required=True)
    value.add_argument("--mode", choices=("rules", "rules_ai"), required=True)
    value.add_argument("--batch-size", type=int, choices=range(1, 101), required=True)
    value.add_argument("--confidence", type=int, choices=range(0, 101), required=True)
    value.add_argument("--threads", type=int, choices=range(1, 101), required=True)
    return value


if __name__ == "__main__":
    raise SystemExit(run(parser().parse_args()))
