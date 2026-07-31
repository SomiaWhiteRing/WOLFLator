from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from formats import ARTIFACT_EPOCH, require_format
from models import TranslationItem
from safe_io import atomic_write_json


ANALYSIS_ENGINE = "sparse-relational-v2"


def validate_editor_analysis(report: object) -> dict[str, object]:
    value = require_format(
        report,
        kind="editor-analysis",
        version_key="epoch",
        version=ARTIFACT_EPOCH,
        label="Editor 分析报告",
    )
    editor = value.get("editor")
    if (
        value.get("engine") != ANALYSIS_ENGINE
        or not isinstance(editor, dict)
        or not isinstance(editor.get("version"), str)
        or not isinstance(editor.get("sha256"), str)
        or not isinstance(value.get("input_hash"), str)
        or not isinstance(value.get("output_hash"), str)
    ):
        raise ValueError("Editor 分析报告结构不匹配。")
    return value


def load_editor_analysis(path: str | Path) -> dict[str, object]:
    try:
        report = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Editor 分析报告损坏。") from exc
    return validate_editor_analysis(report)


def source_structure_fingerprint(items: list[TranslationItem]) -> str:
    """Hash translation sources without making candidate text part of the cache key."""
    structure = [
        (
            item.key,
            item.original,
            item.code,
            item.context,
            item.flag,
            item.type,
            item.info,
            item.category.value,
            item.copy_category.value if item.copy_category else None,
            item.control_signature,
        )
        for item in items
    ]
    encoded = json.dumps(
        structure,
        ensure_ascii=False,
        sort_keys=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_program_cache(
    path: str | Path,
    analysis_path: str | Path,
    items: list[TranslationItem],
) -> Path:
    output = Path(path).resolve()
    report_path = Path(analysis_path).resolve()
    report = load_editor_analysis(report_path)
    editor = report.get("editor")
    if not isinstance(editor, dict):
        raise ValueError("Editor 分析报告缺少 Editor 身份。")
    try:
        relative_report = report_path.relative_to(output.parent).as_posix()
    except ValueError as exc:
        raise ValueError("Editor 分析报告必须位于程序缓存目录内。") from exc
    manifest = {
        "kind": "editor-program-cache",
        "epoch": ARTIFACT_EPOCH,
        "engine": ANALYSIS_ENGINE,
        "input_hash": str(report.get("input_hash", "")),
        "output_hash": str(report.get("output_hash", "")),
        "editor": {
            "version": str(editor.get("version", "")),
            "sha256": str(editor.get("sha256", "")),
        },
        "source_fingerprint": source_structure_fingerprint(items),
        "analysis_file": relative_report,
        "analysis_sha256": _sha256_file(report_path),
    }
    return atomic_write_json(output, manifest, indent=None)


def load_program_cache(
    path: str | Path,
    *,
    items: list[TranslationItem] | None = None,
    input_hash: str | None = None,
    editor_version: str | None = None,
    editor_sha256: str | None = None,
) -> dict[str, object]:
    manifest_path = Path(path).resolve()
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Editor 程序缓存损坏。") from exc
    manifest = require_format(
        manifest,
        kind="editor-program-cache",
        version_key="epoch",
        version=ARTIFACT_EPOCH,
        label="Editor 程序缓存",
    )
    if set(manifest) != {
        "kind",
        "epoch",
        "engine",
        "input_hash",
        "output_hash",
        "editor",
        "source_fingerprint",
        "analysis_file",
        "analysis_sha256",
    }:
        raise ValueError("Editor 程序缓存字段不匹配。")
    if manifest.get("engine") != ANALYSIS_ENGINE:
        raise ValueError("Editor 程序缓存分析引擎已过期。")
    if input_hash is not None and manifest.get("input_hash") != input_hash:
        raise ValueError("Editor 程序缓存输入哈希不匹配。")
    editor = manifest.get("editor")
    if not isinstance(editor, dict) or set(editor) != {"version", "sha256"} or not all(
        isinstance(editor[name], str) for name in editor
    ):
        raise ValueError("Editor 程序缓存缺少 Editor 身份。")
    if editor_version is not None and editor.get("version") != editor_version:
        raise ValueError("Editor 程序缓存版本不匹配。")
    if editor_sha256 is not None and editor.get("sha256") != editor_sha256:
        raise ValueError("Editor 程序缓存文件哈希不匹配。")
    if (
        items is not None
        and manifest.get("source_fingerprint") != source_structure_fingerprint(items)
    ):
        raise ValueError("Editor 程序缓存翻译源结构不匹配。")

    relative = manifest.get("analysis_file")
    if not isinstance(relative, str) or not relative:
        raise ValueError("Editor 程序缓存缺少分析报告路径。")
    report_path = (manifest_path.parent / relative).resolve()
    if os.path.commonpath((str(manifest_path.parent), str(report_path))) != str(
        manifest_path.parent
    ):
        raise ValueError("Editor 程序缓存引用了目录外文件。")
    if not report_path.is_file() or _sha256_file(report_path) != manifest.get(
        "analysis_sha256"
    ):
        raise ValueError("Editor 程序缓存分析报告缺失或哈希不匹配。")
    report = load_editor_analysis(report_path)
    report_editor = report.get("editor")
    if (
        report.get("input_hash") != manifest.get("input_hash")
        or report.get("output_hash") != manifest.get("output_hash")
        or not isinstance(report_editor, dict)
        or report_editor.get("version") != editor.get("version")
        or report_editor.get("sha256") != editor.get("sha256")
    ):
        raise ValueError("Editor 程序缓存头与分析报告不一致。")
    return report
