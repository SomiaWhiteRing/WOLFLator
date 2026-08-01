from __future__ import annotations

import threading
import traceback
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from ainiee import install_supported_ainiee, locate_ainiee_source, prepare_managed_runtime, test_api
from fonts import (
    FontCandidate, discover_font_candidates, font_file_faces, load_font_scheme,
    load_original_fonts, record_original_fonts, required_characters, resolve_scheme_files,
)
from models import Stage, StageStatus
from pipeline import Pipeline, load_manifest
from proofread import run_project_proofread
from wolf_analysis import load_editor_analysis
from wolf_editor import analyze_translation_safety, install_supported_editor
from wolf_tools import (
    imported_display_texts, load_import_protection, load_items, read_font_slots,
    selected_translation_requirements,
)


def _load_editor_analysis(manifest) -> dict[str, object] | None:
    path = manifest.version.stage(Stage.EXTRACT).artifacts.get("editor_analysis", "")
    if not path or not Path(path).is_file():
        return None
    return load_editor_analysis(path)


def _translation_safety_for_manifest(
    manifest, items, *, policy: str | None = None
) -> dict[str, object]:
    analysis = _load_editor_analysis(manifest)
    if analysis is None:
        raise RuntimeError("缺少 Editor 分析报告，请重新执行导出文本。")
    rules = manifest.import_protection
    required = selected_translation_requirements(
        items,
        manifest.import_scope,
        allow_copy_condition_groups=rules.allow_copy_condition_groups,
    )
    return analyze_translation_safety(
        manifest.version.stage(Stage.EXTRACT).artifacts["editor_auto_dir"],
        items,
        {
            item.key: item.translation
            for item in items
            if item.key in required and item.translation
        },
        policy or rules.logic_unknown_policy,
        analysis=analysis,
    )


def _completed_import_protection(manifest, items_path: Path) -> dict[str, object] | None:
    record = manifest.version.stage(Stage.IMPORT)
    path_value = record.artifacts.get("import_protection", "")
    path = Path(path_value) if path_value else None
    if (
        record.status is not StageStatus.COMPLETED
        or path is None
        or not path.is_file()
        or path.stat().st_mtime_ns < items_path.stat().st_mtime_ns
    ):
        return None
    try:
        value = load_import_protection(path)
    except ValueError:
        return None
    return value


def _font_required_characters(
    manifest,
    items,
    protection: dict[str, object] | None,
) -> tuple[set[str], bool]:
    options = {
        "allow_copy_condition_groups": (
            manifest.import_protection.allow_copy_condition_groups
        )
    }
    if protection is not None:
        texts = imported_display_texts(
            items,
            manifest.import_scope,
            protected_keys=set(protection["protected_keys"]),
            **options,
        )
        return required_characters(texts), True
    return required_characters(
        imported_display_texts(items, manifest.import_scope, **options)
    ), False


class PipelineThread(QThread):
    log_line = Signal(str)
    stage_progress = Signal(int, int, str)
    stage_state = Signal(object)
    result_ready = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        pipeline: Pipeline,
        stage: Stage | None = None,
        stages: tuple[Stage, ...] = (),
    ):
        super().__init__()
        if stage is not None and stages:
            raise ValueError("不能同时执行单个阶段和连续阶段。")
        self.pipeline = pipeline
        self.stage = stage
        self.stages = stages
        self.pipeline.set_log_sink(self.log_line.emit)
        self.pipeline.progress = lambda current, total, stage: self.stage_progress.emit(current, total, stage.value)
        self.pipeline.state = self.stage_state.emit

    def run(self) -> None:
        try:
            if self.stages:
                result = self.pipeline.run_stages(self.stages)
            elif self.stage is not None:
                result = self.pipeline.run_stage(self.stage)
            else:
                result = self.pipeline.run()
            self.result_ready.emit(result)
        except Exception:
            detail = traceback.format_exc()
            self.pipeline.detail("pipeline.thread.exception\n" + detail)
            self.failed.emit(detail)


class ProofreadThread(QThread):
    progress_event = Signal(object)
    log_line = Signal(str)
    succeeded = Signal(str)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, manifest_path: Path, settings, api_key: str, cache_root: Path):
        super().__init__()
        self.manifest_path = manifest_path
        self.settings = settings
        self.api_key = api_key
        self.cache_root = cache_root
        self.cancel_event = threading.Event()

    def cancel(self) -> None:
        self.cancel_event.set()

    def run(self) -> None:
        try:
            manifest = load_manifest(self.manifest_path)
            path = run_project_proofread(
                self.manifest_path,
                manifest,
                self.settings,
                self.api_key,
                self.cache_root,
                cancel_event=self.cancel_event,
                progress=self.progress_event.emit,
                log=self.log_line.emit,
            )
            self.succeeded.emit(str(path))
        except Exception as exc:
            from wolf_tools import CancelledError

            if isinstance(exc, CancelledError):
                self.cancelled.emit()
            else:
                self.failed.emit(traceback.format_exc())


class InstallThread(QThread):
    progress_changed = Signal(int, int)
    log_line = Signal(str)
    installed = Signal(str)
    failed = Signal(str)

    def __init__(self, packages_root: Path, runtime_root: Path, repair: bool, source: str = ""):
        super().__init__()
        self.packages_root = packages_root
        self.runtime_root = runtime_root
        self.repair = repair
        self.source = source

    def run(self) -> None:
        try:
            if self.source:
                path = locate_ainiee_source(self.source)
            else:
                path = install_supported_ainiee(
                    self.packages_root,
                    repair=self.repair,
                    progress=self.progress_changed.emit,
                    log=self.log_line.emit,
                )
            self.log_line.emit("正在安装 AiNiee 依赖，首次准备可能需要较长时间...")
            prepare_managed_runtime(
                path,
                self.runtime_root,
                force_sync=self.repair,
                log=self.log_line.emit,
            )
            self.installed.emit(str(path))
        except Exception:
            self.failed.emit(traceback.format_exc())


class EditorInstallThread(QThread):
    progress_changed = Signal(int, int)
    log_line = Signal(str)
    installed = Signal(str)
    failed = Signal(str)

    def __init__(self, packages_root: Path):
        super().__init__()
        self.packages_root = packages_root

    def run(self) -> None:
        try:
            path = install_supported_editor(
                self.packages_root,
                progress=self.progress_changed.emit,
                log=self.log_line.emit,
            )
            self.installed.emit(str(path))
        except Exception:
            self.failed.emit(traceback.format_exc())


class ApiTestThread(QThread):
    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(self, settings, api_key: str, *, glossary: bool = False):
        super().__init__()
        self.settings = settings
        self.api_key = api_key
        self.glossary = glossary

    def run(self) -> None:
        try:
            self.succeeded.emit(test_api(self.settings, self.api_key, glossary=self.glossary))
        except Exception as exc:
            self.failed.emit(str(exc))


class FontScanThread(QThread):
    succeeded = Signal(object)
    failed = Signal(str, str)

    def __init__(self, manifest_path: Path, *, refresh: bool = False):
        super().__init__()
        self.manifest_path = manifest_path
        self.refresh = refresh

    def run(self) -> None:
        try:
            manifest = load_manifest(self.manifest_path)
            if self.isInterruptionRequested():
                return
            record = manifest.version.stage(Stage.VALIDATE)
            items_path = record.artifacts.get("items", "")
            if record.status is not StageStatus.COMPLETED or not items_path or not Path(items_path).is_file():
                raise RuntimeError("完成“校验译文”后才能检查和修改字体。")
            items = load_items(items_path)
            if self.isInterruptionRequested():
                return
            extract = manifest.version.stage(Stage.EXTRACT).artifacts
            original_record = load_original_fonts(
                self.manifest_path.parent, manifest.active_version
            )
            if original_record is None:
                source_items = load_items(extract["items"])
                record_original_fonts(
                    self.manifest_path.parent,
                    manifest.active_version,
                    read_font_slots(source_items),
                    manifest.version.source_hash,
                    extract["workbook"],
                )
                original_record = load_original_fonts(
                    self.manifest_path.parent, manifest.active_version
                )
            if self.isInterruptionRequested():
                return
            if original_record is None:
                raise RuntimeError("无法建立当前版本的原字体记录。")
            original_slots = list(original_record["slots"])
            version_dir = self.manifest_path.parent / "versions" / manifest.active_version
            game_root = version_dir / "work"
            if not game_root.is_dir():
                game_root = version_dir / "source"
            if not game_root.is_dir():
                game_root = Path(manifest.version.original_path)
            protection = _completed_import_protection(manifest, Path(items_path))
            required, exact_coverage = _font_required_characters(
                manifest,
                items,
                protection,
            )
            if self.isInterruptionRequested():
                return
            candidates = discover_font_candidates(
                game_root,
                required,
                cancelled=self.isInterruptionRequested,
                refresh=self.refresh,
            )
            if self.isInterruptionRequested():
                return
            scheme = load_font_scheme(self.manifest_path.parent)
            if scheme is not None:
                resolved = resolve_scheme_files(self.manifest_path.parent, scheme)
                for slot, files in zip(scheme["slots"], resolved, strict=True):
                    if self.isInterruptionRequested():
                        return
                    if slot["mode"] != "font":
                        continue
                    if any(
                        candidate.source == slot["provenance"]
                        and candidate.family.casefold() == str(slot["family"]).casefold()
                        for candidate in candidates
                    ):
                        continue
                    missing = {ord(character) for character in required}
                    aliases: set[str] = {str(slot["family"])}
                    matched_face = None
                    for path in files:
                        if self.isInterruptionRequested():
                            return
                        for face in font_file_faces(path):
                            names = {
                                face.family,
                                face.preview_family,
                                *face.aliases,
                                *face.typographic_aliases,
                            }
                            if matched_face is None or any(
                                name.casefold() == str(slot["family"]).casefold()
                                for name in names
                                if name
                            ):
                                matched_face = face
                            aliases.update(face.aliases)
                            missing.difference_update(face.codepoints)
                    family = matched_face.family if matched_face else str(slot["family"])
                    candidates.append(
                        FontCandidate(
                            source=str(slot["provenance"]),
                            family=family,
                            aliases=tuple(sorted(aliases, key=str.casefold)),
                            files=tuple(files),
                            preview_family=(matched_face.preview_family if matched_face else family),
                            style=matched_face.style if matched_face else "",
                            weight=matched_face.weight if matched_face else 400,
                            weight_range=(matched_face.weight_range if matched_face else None),
                            missing=frozenset(map(chr, missing)),
                        )
                    )
            release_record = manifest.version.stage(Stage.RELEASE)
            self.succeeded.emit(
                {
                    "manifest": str(self.manifest_path),
                    "scheme": scheme,
                    "original_slots": original_slots,
                    "required": required,
                    "exact_coverage": exact_coverage,
                    "candidates": candidates,
                    "release_status": release_record.status.value,
                    "font_warning_count": release_record.artifacts.get("font_warning_count", "0"),
                    "font_warnings": release_record.artifacts.get("font_warnings", ""),
                }
            )
        except InterruptedError:
            return
        except Exception as exc:
            self.failed.emit(str(self.manifest_path), str(exc))
