from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QCoreApplication  # noqa: E402

from models import ImportCategory, ImportScope, Stage, StageStatus  # noqa: E402
from pipeline import Pipeline, create_project, load_manifest  # noqa: E402
from safe_io import atomic_write_json, atomic_write_text  # noqa: E402
from settings import SettingsStore, local_data_dir  # noqa: E402
from wolf_command_catalog import VERIFIED_EDITOR_SHA256, VERIFIED_EDITOR_VERSION  # noqa: E402
from wolf_editor import analyze_auto_export, inspect_wolf_editor  # noqa: E402
from wolf_tools import (  # noqa: E402
    CancelledError,
    OfficialToolDialogError,
    dump_items,
    load_items,
    protect_control_tokens,
    restore_control_tokens,
    sha256_file,
)


CORPUS_SCHEMA = 1
RUN_SCHEMA = 1
_REPARSE_POINT = 0x400
_PSEUDO_ALPHABET = "伪译测试甲乙丙丁戊己庚辛壬癸"
_TERMINAL = {"PASS", "OUT_OF_SCOPE"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _json_hash(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _fixed_drives() -> list[Path]:
    if os.name != "nt":
        return [Path("/")]
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    return [
        Path(f"{chr(ord('A') + index)}:\\")
        for index in range(26)
        if mask & (1 << index)
        and ctypes.windll.kernel32.GetDriveTypeW(f"{chr(ord('A') + index)}:\\") == 3
    ]


def _is_reparse(entry: os.DirEntry[str]) -> bool:
    try:
        attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    except OSError:
        return True
    return entry.is_symlink() or bool(attributes & _REPARSE_POINT)


def _game_kind(path: Path) -> str | None:
    if not (path / "Game.exe").is_file():
        return None
    if (path / "Data.wolf").is_file():
        return "packed"
    if (path / "Data" / "BasicData" / "Game.dat").is_file():
        return "loose"
    return None


def _discover_root(root: Path, errors: list[dict[str, str]]) -> list[tuple[Path, str]]:
    found: list[tuple[Path, str]] = []
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            kind = _game_kind(current)
            if kind:
                found.append((current.resolve(), kind))
            with os.scandir(current) as entries:
                children = []
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False) and not _is_reparse(entry):
                            children.append(Path(entry.path))
                    except OSError as error:
                        errors.append({"path": entry.path, "error": str(error)})
                pending.extend(reversed(sorted(children, key=lambda value: str(value).lower())))
        except OSError as error:
            errors.append({"path": str(current), "error": str(error)})
    return found


def _hash_files(root: Path, paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda value: value.relative_to(root).as_posix().lower()):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8", errors="surrogatepass"))
        digest.update(b"\0")
        digest.update(path.stat().st_size.to_bytes(8, "little"))
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def game_fingerprint(path: str | Path, kind: str | None = None) -> str:
    root = Path(path).resolve()
    kind = kind or _game_kind(root)
    if kind == "packed":
        files = [root / "Game.exe", root / "Data.wolf"]
    elif kind == "loose":
        data = root / "Data"
        files = [root / "Game.exe"]
        files.extend((data / "BasicData").glob("*.dat"))
        files.extend((data / "MapData").rglob("*.mps"))
    else:
        raise ValueError(f"不是合格的 WOLF 游戏目录: {root}")
    return _hash_files(root, files)


def discover(roots: Iterable[Path], output: Path) -> dict[str, object]:
    errors: list[dict[str, str]] = []
    found: list[tuple[Path, str]] = []
    normalized_roots = [Path(root).resolve() for root in roots]
    for root in normalized_roots:
        found.extend(_discover_root(root, errors))

    groups: dict[str, list[tuple[Path, str]]] = {}
    for path, kind in sorted(set(found), key=lambda item: str(item[0]).lower()):
        try:
            fingerprint = game_fingerprint(path, kind)
        except OSError as error:
            errors.append({"path": str(path), "error": f"fingerprint: {error}"})
            continue
        groups.setdefault(fingerprint, []).append((path, kind))

    candidates = []
    for fingerprint, entries in sorted(groups.items()):
        primary, kind = entries[0]
        candidates.append(
            {
                "id": fingerprint,
                "fingerprint": fingerprint,
                "kind": kind,
                "path": str(primary),
                "duplicates": [str(path) for path, _kind in entries[1:]],
            }
        )
    manifest = {
        "schema": CORPUS_SCHEMA,
        "created_at": _now(),
        "roots": [str(root) for root in normalized_roots],
        "scan_complete": not errors,
        "access_errors": errors,
        "path_count": sum(1 + len(item["duplicates"]) for item in candidates),
        "unique_count": len(candidates),
        "candidates": candidates,
    }
    output.mkdir(parents=True, exist_ok=True)
    atomic_write_json(output / "corpus-manifest.json", manifest)
    return manifest


def pseudo_translation(original: str, key: str) -> str:
    protected, tokens = protect_control_tokens(original)
    digest = hashlib.sha256(key.encode("utf-8", errors="surrogatepass")).digest()
    offset = int.from_bytes(digest[:4], "little")
    changed = []
    for index, character in enumerate(protected):
        if character.isspace() or 0xE100 <= ord(character) <= 0xF7FF:
            changed.append(character)
        else:
            changed.append(_PSEUDO_ALPHABET[(offset + index) % len(_PSEUDO_ALPHABET)])
    result = restore_control_tokens("".join(changed), tokens)
    if result == original and original:
        result = restore_control_tokens(
            _PSEUDO_ALPHABET[offset % len(_PSEUDO_ALPHABET)] + protected, tokens
        )
    return result


def _coverage(analysis: dict[str, object]) -> dict[str, object]:
    catalog = analysis.get("command_catalog", {})
    if not isinstance(catalog, dict):
        return {}
    return {
        name: catalog.get(name, {})
        for name in (
            "shape_coverage",
            "semantic_coverage",
            "cfg_coverage",
            "call_target_coverage",
            "data_effect_coverage",
        )
    } | {
        "opaque_effects": catalog.get("opaque_effects", -1),
        "unexplained_data_side_effects": dict(
            catalog.get("data_effect_coverage", {})
        ).get("missing", -1),
    }


def _coverage_passes(coverage: dict[str, object]) -> bool:
    return (
        coverage.get("opaque_effects") == 0
        and coverage.get("unexplained_data_side_effects", 0) == 0
        and all(
        isinstance(coverage.get(name), dict)
        and coverage[name].get("ratio") == 1.0
        and coverage[name].get("missing") == 0
        for name in (
            "shape_coverage",
            "semantic_coverage",
            "cfg_coverage",
            "call_target_coverage",
            "data_effect_coverage",
        )
        )
    )


def _git_state() -> dict[str, object]:
    def run(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    commit = run("rev-parse", "HEAD")
    status = run("status", "--porcelain", "--untracked-files=all")
    return {
        "available": commit.returncode == status.returncode == 0,
        "commit": commit.stdout.strip() if commit.returncode == 0 else "",
        "worktree_clean": status.returncode == 0 and not status.stdout.strip(),
        "error": "\n".join(
            value.strip()
            for value in (commit.stderr, status.stderr)
            if value.strip()
        ),
    }


def _out_of_scope_evidence(
    analysis: dict[str, object], auto_dir: str | Path | None = None
) -> list[dict[str, object]]:
    evidence = []
    if auto_dir is not None:
        game_settings = Path(auto_dir) / "BasicData" / "Game.dat.Auto.txt"
        if game_settings.is_file() and re.search(
            r"(?m)^PRO_FLAG=1\s*$",
            game_settings.read_text(encoding="utf-8-sig", errors="strict"),
        ):
            evidence.append(
                {
                    "kind": "pro_project",
                    "source": str(game_settings),
                    "record": "PRO_FLAG=1",
                }
            )
    for unknown in analysis.get("unknown_commands", []):
        if not isinstance(unknown, dict):
            continue
        try:
            opcode = int(unknown["opcode"])
            shape = str(unknown["shape"])
        except (KeyError, TypeError, ValueError):
            continue
        shape_match = re.fullmatch(r"ints=(\d+),strings=(\d+)", shape)
        if not shape_match:
            continue
        int_count, string_count = map(int, shape_match.groups())
        if opcode >= 1000:
            kind = "pro_opcode"
        else:
            continue
        evidence.append(
            {
                "kind": kind,
                "opcode": opcode,
                "shape": [int_count, string_count],
                "count": int(unknown.get("count", 0)),
                "locations": list(unknown.get("locations", [])),
            }
        )
    return evidence


def _load_json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON 根节点不是对象: {path}")
    return value


def _pipeline(settings, manifest_path: Path) -> Pipeline:
    return Pipeline(
        manifest_path,
        settings,
        "",
        local_data_dir(),
        glossary_api_key="",
        log=lambda message: print(message, flush=True),
    )


def _sandbox_root(candidate_id: str) -> Path:
    public_root = Path(os.environ.get("PUBLIC", r"C:\Users\Public"))
    # ponytail: sequential QA only; add a run-id component before parallelizing games.
    return public_root / "WOLFLator" / "corpus-qa" / candidate_id[:16]


def _inject_pseudo_translation(pipeline: Pipeline) -> tuple[int, int]:
    extract = pipeline.manifest.version.stage(Stage.EXTRACT).artifacts
    items = load_items(extract["items"])
    translated = 0
    for item in items:
        if (
            item.original
            and item.category is not ImportCategory.COPY
            and item.code.upper() not in {"BASICDATA-3", "BASICDATA-4", "BASICDATA-5", "BASICDATA-6"}
        ):
            item.translation = pseudo_translation(item.original, item.key)
            translated += 1
        else:
            item.translation = ""
    output = dump_items(pipeline.artifacts_dir / "corpus-pseudo-items.json", items)
    with pipeline._mutation("corpus-qa-pseudo"):  # noqa: SLF001 - development harness
        for stage, artifacts in (
            (Stage.GLOSSARY, {"corpus_qa": "no-api"}),
            (Stage.TRANSLATE, {"items": str(output), "corpus_qa": "deterministic-pseudo"}),
        ):
            record = pipeline.manifest.version.stage(stage)
            record.status = StageStatus.COMPLETED
            record.started_at = _now()
            record.finished_at = _now()
            record.input_hash = f"corpus-qa-{stage.value}"
            record.error = ""
            record.artifacts = artifacts
        pipeline.save()
    return len(items), translated


def _run_candidate(
    candidate: dict[str, object],
    run_dir: Path,
    settings,
    editor,
) -> dict[str, object]:
    candidate_id = str(candidate["id"])
    source = Path(str(candidate["path"])).resolve()
    checkpoint_dir = run_dir / "games" / candidate_id
    checkpoint_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = checkpoint_dir / "report.json"
    started = time.monotonic()
    report: dict[str, object] = {
        "schema": RUN_SCHEMA,
        "candidate_id": candidate_id,
        "path": str(source),
        "started_at": _now(),
        "status": "INCOMPLETE",
    }
    project_parent = _sandbox_root(candidate_id)
    try:
        before = game_fingerprint(source, str(candidate["kind"]))
        if before != candidate["fingerprint"]:
            raise RuntimeError("发现清单冻结后源游戏逻辑文件发生变化。")
        if project_parent.exists():
            shutil.rmtree(project_parent)
        manifest_path = create_project(project_parent, source, name="corpus-game")
        pipeline = _pipeline(settings, manifest_path)
        all_scope = ImportScope(True, True, True, True, True)
        pipeline.set_export_scope(
            all_scope, exclude_large_external_files=False, external_file_limit_kb=128
        )
        pipeline.set_translation_scope(all_scope)
        pipeline.set_import_scope(all_scope)
        for stage in (Stage.COPY, Stage.UNPACK, Stage.EXTRACT):
            pipeline.run_stage(stage)

        extract = load_manifest(manifest_path).version.stage(Stage.EXTRACT).artifacts
        analysis_path = Path(extract["editor_analysis"])
        first_analysis = _load_json(analysis_path)
        items = load_items(extract["items"])
        second_analysis = analyze_auto_export(
            extract["editor_auto_dir"],
            items,
            editor,
            input_hash=str(first_analysis.get("input_hash", "")),
        )
        first_hash = _json_hash(first_analysis)
        second_hash = _json_hash(second_analysis)
        if first_hash != second_hash:
            raise RuntimeError(
                f"原文静态分析结果不确定: {first_hash} != {second_hash}"
            )
        out_of_scope = _out_of_scope_evidence(
            first_analysis, extract["editor_auto_dir"]
        )
        if out_of_scope:
            report.update(
                {
                    "status": "OUT_OF_SCOPE",
                    "source_fingerprint_before": before,
                    "source_fingerprint_after": game_fingerprint(
                        source, str(candidate["kind"])
                    ),
                    "analysis_hash": first_hash,
                    "repeat_analysis_hash": second_hash,
                    "coverage": _coverage(first_analysis),
                    "evidence": out_of_scope,
                }
            )
            return report

        total_items, pseudo_items = _inject_pseudo_translation(pipeline)
        pipeline.run_stage(Stage.VALIDATE)
        pipeline.run_stage(Stage.IMPORT)
        manifest = load_manifest(manifest_path)
        import_artifacts = manifest.version.stage(Stage.IMPORT).artifacts
        protection = _load_json(Path(import_artifacts["import_protection"]))
        replay = protection.get("translated_replay", {})
        structural = protection.get("structural_diff", {})
        after = game_fingerprint(source, str(candidate["kind"]))
        coverage = _coverage(first_analysis)
        pass_reasons = []
        if not _coverage_passes(coverage):
            pass_reasons.append("核心覆盖未达到 100% 或仍有不透明副作用")
        if first_analysis.get("blocking_issues"):
            pass_reasons.append("原文分析存在阻断问题")
        if not isinstance(replay, dict) or not all(
            replay.get(name) is True
            for name in (
                "control_flow_equivalent",
                "data_effects_equivalent",
                "condition_results_equivalent",
                "resource_targets_equivalent",
            )
        ):
            pass_reasons.append("候选译文重放不等价")
        if not isinstance(structural, dict) or structural.get("status") != "passed":
            pass_reasons.append("官方回读结构比较未通过")
        if before != after:
            pass_reasons.append("源游戏逻辑文件哈希变化")
        if pass_reasons:
            raise RuntimeError("；".join(pass_reasons))

        report.update(
            {
                "status": "PASS",
                "source_fingerprint_before": before,
                "source_fingerprint_after": after,
                "analysis_hash": first_hash,
                "repeat_analysis_hash": second_hash,
                "coverage": coverage,
                "items": total_items,
                "pseudo_translations": pseudo_items,
                "safe_to_translate": len(protection.get("safe_to_translate", [])),
                "keep_original": len(protection.get("keep_original", [])),
                "translated_replay": replay,
                "structural_diff": structural,
            }
        )
    except OfficialToolDialogError as error:
        if any(re.search(r"(?:Map Size|Data).*Error", dialog, re.IGNORECASE) for dialog in error.dialogs):
            report.update(
                {
                    "status": "OUT_OF_SCOPE",
                    "source_fingerprint_before": before,
                    "source_fingerprint_after": game_fingerprint(
                        source, str(candidate["kind"])
                    ),
                    "evidence": [
                        {
                            "kind": "official_tool_data_error",
                            "dialogs": list(error.dialogs),
                        }
                    ],
                }
            )
        else:
            report.update(
                {
                    "status": "DEFECT",
                    "error_type": type(error).__name__,
                    "error": str(error),
                    "traceback": traceback.format_exc(),
                }
            )
    except (CancelledError, KeyboardInterrupt, TimeoutError, PermissionError) as error:
        report.update(
            {
                "status": "INCOMPLETE",
                "error_type": type(error).__name__,
                "error": str(error),
                "traceback": traceback.format_exc(),
            }
        )
    except Exception as error:
        # A 3.713-exportable project is a product defect unless positive
        # out-of-scope evidence was already captured in a valid analysis.
        report.update(
            {
                "status": "DEFECT",
                "error_type": type(error).__name__,
                "error": str(error),
                "traceback": traceback.format_exc(),
            }
        )
    finally:
        report["finished_at"] = _now()
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        atomic_write_json(checkpoint, report)
        if report["status"] in _TERMINAL and project_parent.exists():
            # ponytail: successful multi-GB sandboxes add no evidence beyond their hashes;
            # retain failed sandboxes for diagnosis and rebuild passing ones on demand.
            shutil.rmtree(project_parent)
    return report


def _aggregate(manifest: dict[str, object], reports: list[dict[str, object]]) -> dict[str, object]:
    counts = {name: 0 for name in ("PASS", "OUT_OF_SCOPE", "DEFECT", "INCOMPLETE")}
    for report in reports:
        counts[str(report.get("status", "INCOMPLETE"))] += 1
    eligible = counts["PASS"] + counts["DEFECT"]
    return {
        "schema": RUN_SCHEMA,
        "updated_at": _now(),
        "scan_complete": bool(manifest.get("scan_complete")),
        "access_error_count": len(manifest.get("access_errors", [])),
        "candidate_total": len(manifest.get("candidates", [])),
        "eligible_total": eligible,
        "pass_total": counts["PASS"],
        "out_of_scope_total": counts["OUT_OF_SCOPE"],
        "defect_total": counts["DEFECT"],
        "incomplete_total": counts["INCOMPLETE"],
        "reports": [
            {
                "candidate_id": report.get("candidate_id"),
                "path": report.get("path"),
                "status": report.get("status"),
                "elapsed_seconds": report.get("elapsed_seconds"),
                "error": report.get("error", ""),
            }
            for report in reports
        ],
    }


def run_manifest(manifest_path: Path, editor_path: Path, resume: bool, settings_path: str | None) -> dict[str, object]:
    manifest = _load_json(manifest_path)
    if manifest.get("schema") != CORPUS_SCHEMA:
        raise ValueError("不支持的全盘发现清单 schema。")
    editor = inspect_wolf_editor(editor_path)
    if editor.version != VERIFIED_EDITOR_VERSION or editor.sha256 != VERIFIED_EDITOR_SHA256:
        raise ValueError(
            "全盘验收必须使用固定 Editor "
            f"{VERIFIED_EDITOR_VERSION} / {VERIFIED_EDITOR_SHA256}；"
            f"实际为 {editor.version} / {editor.sha256}。"
        )
    settings = SettingsStore(settings_path).load()
    settings.wolf_editor_path = str(editor.path)
    wolf_tool = Path(settings.wolf_tool_path)
    if not wolf_tool.is_file() or not (wolf_tool.parent / "LibXL.dll").is_file():
        raise ValueError("当前设置中的官方翻译工具无效或缺少 LibXL.dll。")

    run_dir = manifest_path.parent / "run"
    if run_dir.exists() and not resume:
        raise FileExistsError(f"QA 运行目录已存在；请使用 --resume 或移走该目录: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    metadata = {
        "schema": RUN_SCHEMA,
        "started_at": _now(),
        "manifest": str(manifest_path.resolve()),
        "manifest_sha256": sha256_file(manifest_path),
        "python": sys.version,
        "git": _git_state(),
        "editor": {
            "path": str(editor.path),
            "version": editor.version,
            "sha256": editor.sha256,
        },
        "wolf_tool": {"path": str(wolf_tool.resolve()), "sha256": sha256_file(wolf_tool)},
    }
    atomic_write_json(run_dir / "environment.json", metadata)

    reports = []
    candidates = manifest.get("candidates", [])
    if not isinstance(candidates, list):
        raise ValueError("发现清单 candidates 不是数组。")
    for index, candidate in enumerate(candidates, start=1):
        if not isinstance(candidate, dict):
            raise ValueError("发现清单候选项不是对象。")
        checkpoint = run_dir / "games" / str(candidate["id"]) / "report.json"
        if resume and checkpoint.is_file():
            previous = _load_json(checkpoint)
            if previous.get("status") in _TERMINAL:
                reports.append(previous)
                print(f"[{index}/{len(candidates)}] {candidate['path']} -> {previous['status']} (resume)", flush=True)
                continue
        print(f"[{index}/{len(candidates)}] {candidate['path']} 开始", flush=True)
        report = _run_candidate(candidate, run_dir, settings, editor)
        reports.append(report)
        print(f"[{index}/{len(candidates)}] {candidate['path']} -> {report['status']}", flush=True)
        aggregate = _aggregate(manifest, reports)
        atomic_write_json(run_dir / "run.json", aggregate)

    aggregate = _aggregate(manifest, reports)
    atomic_write_json(run_dir / "run.json", aggregate)
    return aggregate


def verify(run_dir: Path) -> tuple[bool, list[str], dict[str, object]]:
    aggregate = _load_json(run_dir / "run.json")
    environment = _load_json(run_dir / "environment.json")
    errors = []
    git = environment.get("git", {})
    if not isinstance(git, dict) or not git.get("available"):
        errors.append("无法验证 Git 提交与工作树状态。")
    elif not git.get("worktree_clean"):
        errors.append("QA 运行时工作树不干净。")
    editor = environment.get("editor", {})
    if not isinstance(editor, dict) or (
        editor.get("version") != VERIFIED_EDITOR_VERSION
        or editor.get("sha256") != VERIFIED_EDITOR_SHA256
    ):
        errors.append("QA 运行使用的 Editor 版本或哈希不符合固定基线。")
    if not aggregate.get("scan_complete"):
        errors.append("磁盘发现不完整。")
    if aggregate.get("access_error_count") != 0:
        errors.append(f"存在 {aggregate.get('access_error_count')} 个访问失败。")
    if aggregate.get("defect_total") != 0:
        errors.append(f"存在 {aggregate.get('defect_total')} 个 DEFECT。")
    if aggregate.get("incomplete_total") != 0:
        errors.append(f"存在 {aggregate.get('incomplete_total')} 个 INCOMPLETE。")
    if aggregate.get("eligible_total") != aggregate.get("pass_total"):
        errors.append("eligible_total 与 pass_total 不一致。")
    if aggregate.get("candidate_total") != len(aggregate.get("reports", [])):
        errors.append("逐游戏报告数量与冻结候选数量不一致。")

    for summary in aggregate.get("reports", []):
        if not isinstance(summary, dict):
            errors.append("聚合报告含非法项目。")
            continue
        candidate_id = str(summary.get("candidate_id", ""))
        report_path = run_dir / "games" / candidate_id / "report.json"
        try:
            report = _load_json(report_path)
        except (OSError, ValueError) as error:
            errors.append(f"{candidate_id}: 无法读取逐游戏报告: {error}")
            continue
        if report.get("status") == "PASS":
            if not _coverage_passes(report.get("coverage", {})):
                errors.append(f"{candidate_id}: PASS 报告覆盖门禁不成立。")
            if report.get("source_fingerprint_before") != report.get("source_fingerprint_after"):
                errors.append(f"{candidate_id}: PASS 报告源哈希不一致。")
            if report.get("analysis_hash") != report.get("repeat_analysis_hash"):
                errors.append(f"{candidate_id}: PASS 报告分析不确定。")
            if report.get("structural_diff", {}).get("status") != "passed":
                errors.append(f"{candidate_id}: PASS 报告回读结构未通过。")
            replay = report.get("translated_replay", {})
            if not isinstance(replay, dict) or not all(
                replay.get(name) is True
                for name in (
                    "control_flow_equivalent",
                    "data_effects_equivalent",
                    "condition_results_equivalent",
                    "resource_targets_equivalent",
                )
            ) or replay.get("differences"):
                errors.append(f"{candidate_id}: PASS 报告候选重放未证明等价。")
        elif report.get("status") == "OUT_OF_SCOPE" and not report.get("evidence"):
            errors.append(f"{candidate_id}: OUT_OF_SCOPE 缺少正面证据。")

    lines = [
        "# WOLFLator 3.713 全盘静态安全验收报告",
        "",
        f"- 生成时间：{_now()}",
        f"- 候选：{aggregate.get('candidate_total', 0)}",
        f"- PASS：{aggregate.get('pass_total', 0)}",
        f"- OUT_OF_SCOPE：{aggregate.get('out_of_scope_total', 0)}",
        f"- DEFECT：{aggregate.get('defect_total', 0)}",
        f"- INCOMPLETE：{aggregate.get('incomplete_total', 0)}",
        f"- 结论：{'通过' if not errors else '未通过'}",
    ]
    if errors:
        lines.extend(["", "## 未通过原因", "", *[f"- {error}" for error in errors]])
    atomic_write_text(run_dir / "验收报告.md", "\n".join(lines) + "\n")
    return not errors, errors, aggregate


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="WOLFLator 3.713 full-disk corpus QA")
    subparsers = parser.add_subparsers(dest="command", required=True)
    discover_parser = subparsers.add_parser("discover", help="冻结本地固定磁盘 WOLF 游戏清单")
    discover_parser.add_argument("--all-fixed-drives", action="store_true", required=True)
    discover_parser.add_argument("--output", type=Path, required=True)

    run_parser = subparsers.add_parser("run", help="对冻结清单执行真实流水线安全验收")
    run_parser.add_argument("--manifest", type=Path, required=True)
    run_parser.add_argument("--editor", type=Path, required=True)
    run_parser.add_argument("--resume", action="store_true")
    run_parser.add_argument("--settings", help="覆盖 settings.ini 路径")

    verify_parser = subparsers.add_parser("verify", help="验证运行结果并生成中文报告")
    verify_parser.add_argument("--run", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    QCoreApplication.setApplicationName("WOLFLator")
    QCoreApplication.setOrganizationName("WOLFLator")
    args = build_parser().parse_args(argv)
    if args.command == "discover":
        result = discover(_fixed_drives(), args.output.resolve())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["scan_complete"] else 2
    if args.command == "run":
        result = run_manifest(
            args.manifest.resolve(), args.editor.resolve(), args.resume, args.settings
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["defect_total"] == result["incomplete_total"] == 0 else 1
    passed, errors, result = verify(args.run.resolve())
    print(json.dumps({"passed": passed, "errors": errors, "summary": result}, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
