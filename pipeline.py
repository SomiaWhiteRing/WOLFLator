from __future__ import annotations

import json
import hashlib
import os
import re
import shutil
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from ainiee import (
    generate_glossary,
    require_managed_runtime,
    run_translation,
)
from fonts import (
    FONT_CODES,
    FONT_SLOT_NAMES,
    candidate_for_family,
    coverage_fingerprint,
    default_font_scheme,
    discover_font_candidates,
    font_file_info,
    load_font_scheme,
    load_original_fonts,
    record_original_fonts,
    required_characters,
    resolve_scheme_files,
    save_font_scheme,
    scheme_hash,
)
from models import (
    MAX_EXTERNAL_FILE_LIMIT_KB,
    STAGE_ORDER,
    AppSettings,
    ImportScope,
    ProjectManifest,
    RunMode,
    Stage,
    StageStatus,
    VersionManifest,
    utc_now,
)
from safe_io import (
    atomic_output_path,
    atomic_write_json,
    project_lock,
    read_text_with_retry,
    replace_with_retry,
)
from wolf_tools import (
    CancelledError,
    OfficialToolRunner,
    UberWolfRunner,
    classify_optional_name_delta,
    dump_items,
    final_display_texts,
    full_export_scope,
    hash_directory,
    load_items,
    locate_workbook,
    merge_ainiee_output,
    name_baseline_scope,
    prepare_official_tool,
    prepare_uberwolf,
    read_translation_items,
    read_font_slots,
    reconcile_incremental,
    retryable_translation_errors,
    selected_translation_items,
    selected_translation_requirements,
    sha256_file,
    temporary_external_filter_view,
    to_paratranz,
    write_full_workbook,
    write_font_workbook,
    write_scoped_workbook,
    WORKBOOK_NAME,
    SUPPORT_DIR,
)


EXPORT_SCHEMA = 3


def _atomic_json(path: Path, value: object) -> None:
    atomic_write_json(path, value)


@dataclass(frozen=True)
class PipelineStateEvent:
    stage: Stage
    status: StageStatus
    current: int
    total: int
    detail: str = ""
    warnings: int = 0


def _slug(value: str) -> str:
    clean = re.sub(r"[^\w.-]+", "-", value, flags=re.UNICODE).strip("-._")
    return clean[:64] or "wolf-project"


def _validate_game(path: str | Path) -> Path:
    root = Path(path).resolve()
    game_exe = root / "Game.exe"
    has_data = bool(list(root.glob("Data*.wolf"))) or (root / "Data" / "BasicData" / "Game.dat").is_file()
    if not game_exe.is_file() or not has_data:
        raise ValueError("所选目录不是可识别的 WOLF 游戏（需要 Game.exe 和 Data.wolf 或松散 Data）。")
    return root


def create_project(projects_root: str | Path, game_path: str | Path, name: str = "") -> Path:
    game = _validate_game(game_path)
    display_name = name.strip() or game.name
    base_id = _slug(display_name)
    root = Path(projects_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    project_id = base_id
    suffix = 2
    while (root / project_id / "project.json").exists():
        project_id = f"{base_id}-{suffix}"
        suffix += 1
    project_dir = root / project_id
    version_id = utc_now().replace(":", "").replace("+00:00", "Z")
    manifest = ProjectManifest(project_id=project_id, name=display_name, active_version=version_id)
    manifest.versions[version_id] = VersionManifest(version_id=version_id, original_path=str(game))
    project_dir.mkdir(parents=True)
    save_font_scheme(project_dir, default_font_scheme())
    _atomic_json(project_dir / "project.json", manifest.to_dict())
    return project_dir / "project.json"


def load_manifest(path: str | Path) -> ProjectManifest:
    data = json.loads(read_text_with_retry(path, encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("项目清单根节点不是对象。")
    manifest = ProjectManifest.from_dict(data)
    reset = False
    for stage in STAGE_ORDER:
        record = manifest.version.stage(stage)
        if record.artifacts.get("skipped") == "true":
            reset = True
            record.artifacts = {}
        if reset:
            record.status = StageStatus.PENDING
            record.error = ""
    # ponytail: removed skip markers vanish on the next ordinary manifest save.
    return manifest


def add_version(manifest_path: str | Path, game_path: str | Path) -> ProjectManifest:
    path = Path(manifest_path)
    with project_lock(path, "add-version"):
        manifest = load_manifest(path)
        game = _validate_game(game_path)
        version_id = utc_now().replace(":", "").replace("+00:00", "Z")
        counter = 2
        base = version_id
        while version_id in manifest.versions:
            version_id = f"{base}-{counter}"
            counter += 1
        manifest.versions[version_id] = VersionManifest(version_id=version_id, original_path=str(game))
        manifest.active_version = version_id
        manifest.updated_at = utc_now()
        _atomic_json(path, manifest.to_dict())
        return manifest


class Pipeline:
    def __init__(
        self,
        manifest_path: str | Path,
        settings: AppSettings,
        api_key: str,
        cache_root: str | Path,
        *,
        glossary_api_key: str,
        log: Callable[[str], None] | None = None,
        progress: Callable[[int, int, Stage], None] | None = None,
        state: Callable[[PipelineStateEvent], None] | None = None,
    ):
        self.manifest_path = Path(manifest_path).resolve()
        self.project_dir = self.manifest_path.parent
        self.settings = settings
        self.api_key = api_key
        self.glossary_api_key = glossary_api_key
        self.cache_root = Path(cache_root)
        self._log_sink = log or (lambda _message: None)
        self._log_lock = threading.Lock()
        parsed_api_url = urlsplit(settings.api_base_url)
        self._safe_api_url = urlunsplit(
            (parsed_api_url.scheme, parsed_api_url.netloc.rsplit("@", 1)[-1], parsed_api_url.path, "", "")
        )
        parsed_glossary_api_url = urlsplit(settings.glossary_api_base_url)
        self._safe_glossary_api_url = urlunsplit(
            (
                parsed_glossary_api_url.scheme,
                parsed_glossary_api_url.netloc.rsplit("@", 1)[-1],
                parsed_glossary_api_url.path,
                "",
                "",
            )
        )
        self._sensitive_values = {
            value
            for value in (
                api_key,
                self.glossary_api_key,
                parsed_api_url.password or "",
                parsed_glossary_api_url.password or "",
                *(value for _name, value in parse_qsl(parsed_api_url.query, keep_blank_values=False)),
                *(
                    value
                    for _name, value in parse_qsl(parsed_glossary_api_url.query, keep_blank_values=False)
                ),
            )
            if value
        }
        self.log_path: Path | None = None
        self.progress = progress or (lambda _current, _total, _stage: None)
        self.state = state or (lambda _event: None)
        self.cancel_event = threading.Event()
        self._project_lock_depth = 0
        self._project_lock_owner: int | None = None
        self.manifest = load_manifest(self.manifest_path)

    @property
    def version_dir(self) -> Path:
        return self.project_dir / "versions" / self.manifest.active_version

    @property
    def source_dir(self) -> Path:
        return self.version_dir / "source"

    @property
    def work_dir(self) -> Path:
        return self.version_dir / "work"

    @property
    def artifacts_dir(self) -> Path:
        return self.version_dir / "artifacts"

    @property
    def release_dir(self) -> Path:
        return self.version_dir / "release"

    def save(self) -> None:
        if self._project_lock_depth < 1 or self._project_lock_owner != threading.get_ident():
            raise RuntimeError("保存项目清单前必须持有项目锁。")
        self.manifest.updated_at = utc_now()
        self.detail(f"manifest.save.start path={self.manifest_path}")
        try:
            _atomic_json(self.manifest_path, self.manifest.to_dict())
        except Exception:
            self.detail("manifest.save.failed\n" + traceback.format_exc())
            raise
        self.detail("manifest.save.complete")
        legacy_temporary = self.manifest_path.with_name("project.json.tmp")
        if legacy_temporary.is_file():
            try:
                legacy_temporary.unlink(missing_ok=True)
            except OSError as error:
                self.detail(f"manifest.legacy_tmp.cleanup_failed path={legacy_temporary} error={error}")

    @contextmanager
    def _mutation(self, operation: str):
        current_thread = threading.get_ident()
        if self._project_lock_depth and self._project_lock_owner == current_thread:
            yield
            return
        with project_lock(self.project_dir, operation):
            self._project_lock_depth = 1
            self._project_lock_owner = current_thread
            self.manifest = load_manifest(self.manifest_path)
            try:
                yield
            finally:
                self._project_lock_depth = 0
                self._project_lock_owner = None

    def _emit_state(
        self,
        stage: Stage,
        status: StageStatus,
        current: int,
        total: int,
        detail: str = "",
    ) -> None:
        record = self.manifest.version.stage(stage)
        warnings = sum(
            int(value) if str(value).isdigit() else 0
            for value in (
                record.artifacts.get("official_warning_count", "0"),
                record.artifacts.get("font_warning_count", "0"),
            )
        )
        self.state(PipelineStateEvent(stage, status, current, total, detail, warnings))

    def set_log_sink(self, sink: Callable[[str], None]) -> None:
        self._log_sink = sink

    def _start_run_log(self, operation: str) -> None:
        log_dir = self.artifacts_dir / "logs"
        timestamp = datetime.now().astimezone()
        name = f"{timestamp.strftime('%Y%m%d-%H%M%S-%f')[:-3]}-{operation}.log"
        path = log_dir / name
        header = "\n".join(
            (
                "WOLFLator run log",
                f"started_at={timestamp.isoformat(timespec='seconds')}",
                f"project={self.manifest.project_id}",
                f"version={self.manifest.active_version}",
                f"operation={operation}",
                f"api_url={self._safe_api_url}",
                f"api_model={self.settings.api_model}",
                f"glossary_api_url={self._safe_glossary_api_url}",
                f"glossary_api_model={self.settings.glossary_api_model}",
                f"wolf_tool={self.settings.wolf_tool_path}",
                f"ainiee_source={self.settings.ainiee_source}",
                f"ascii_runner_dir={self.settings.ascii_runner_dir}",
                f"cache_root={self.cache_root}",
                f"process_id={os.getpid()}",
                f"python={sys.version.replace(chr(10), ' ')}",
                "log_format=2",
                "",
            )
        )
        for secret in self._sensitive_values:
            header = header.replace(secret, "[REDACTED]")
        try:
            # ponytail: per-run logs are retained indefinitely; add age/size cleanup if volume becomes material.
            log_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(header, encoding="utf-8-sig")
        except OSError as exc:
            self._log_sink(f"[ERROR] 无法创建外部日志: {exc}")
            return
        self.log_path = path
        self.log(f"日志文件: {path}")

    def _write_log_file(self, message: str, level: str) -> str:
        text = str(message).rstrip("\r\n")
        if self.settings.api_base_url:
            text = text.replace(self.settings.api_base_url, self._safe_api_url)
        if self.settings.glossary_api_base_url:
            text = text.replace(self.settings.glossary_api_base_url, self._safe_glossary_api_url)
        for secret in self._sensitive_values:
            text = text.replace(secret, "[REDACTED]")
        try:
            timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S.%f%z")
            lines = text.splitlines() or [""]
            with self._log_lock:
                path = self.log_path
                if path is None:
                    return text
                with path.open("a", encoding="utf-8") as stream:
                    for line in lines:
                        stream.write(f"{timestamp} [{level}] {line}\n")
        except OSError as exc:
            failed_path = path
            self.log_path = None
            self._log_sink(f"[ERROR] 外部日志写入失败 ({failed_path}): {exc}")
        return text

    def log(self, message: str) -> None:
        text = self._write_log_file(message, "INFO")
        self._log_sink(text)

    def warning(self, message: str) -> None:
        text = self._write_log_file(message, "WARNING")
        self._log_sink(f"[WARNING] {text}")

    def error(self, message: str) -> None:
        text = self._write_log_file(message, "ERROR")
        self._log_sink(f"[ERROR] {text}")

    def detail(self, message: str) -> None:
        self._write_log_file(message, "DETAIL")

    def cancel(self) -> None:
        self.cancel_event.set()

    def set_run_mode(self, mode: RunMode) -> None:
        with self._mutation("set-run-mode"):
            self.manifest.run_mode = mode
            self.save()

    def set_import_scope(self, scope: ImportScope) -> None:
        with self._mutation("set-import-scope"):
            if self.manifest.import_scope == scope:
                return
            self.manifest.import_scope = scope
            for stage in STAGE_ORDER[STAGE_ORDER.index(Stage.IMPORT):]:
                record = self.manifest.version.stage(stage)
                record.status = StageStatus.PENDING
                record.error = ""
            self.save()

    def set_export_scope(
        self,
        scope: ImportScope,
        *,
        exclude_large_external_files: bool | None = None,
        external_file_limit_kb: int | None = None,
    ) -> None:
        with self._mutation("set-export-scope"):
            exclude = (
                self.manifest.exclude_large_external_files
                if exclude_large_external_files is None
                else exclude_large_external_files
            )
            limit_kb = (
                self.manifest.external_file_limit_kb
                if external_file_limit_kb is None
                else external_file_limit_kb
            )
            if type(exclude) is not bool:
                raise ValueError("大文件自动排除开关必须是布尔值。")
            if type(limit_kb) is not int or not 1 <= limit_kb <= MAX_EXTERNAL_FILE_LIMIT_KB:
                raise ValueError(
                    f"外部文件大小上限必须是 1..{MAX_EXTERNAL_FILE_LIMIT_KB} KB 的整数。"
                )
            if (
                self.manifest.export_scope == scope
                and self.manifest.exclude_large_external_files == exclude
                and self.manifest.external_file_limit_kb == limit_kb
            ):
                return
            self.manifest.export_scope = scope
            self.manifest.exclude_large_external_files = exclude
            self.manifest.external_file_limit_kb = limit_kb
            for stage in STAGE_ORDER[STAGE_ORDER.index(Stage.EXTRACT):]:
                record = self.manifest.version.stage(stage)
                record.status = StageStatus.PENDING
                record.error = ""
            self.save()

    def set_translation_scope(self, scope: ImportScope) -> None:
        with self._mutation("set-translation-scope"):
            if self.manifest.translation_scope == scope:
                return
            self.manifest.translation_scope = scope
            for stage in STAGE_ORDER[STAGE_ORDER.index(Stage.GLOSSARY):]:
                record = self.manifest.version.stage(stage)
                record.status = StageStatus.PENDING
                record.error = ""
            self.save()

    def set_font_scheme(self, scheme: dict[str, object]) -> None:
        with self._mutation("set-font-scheme"):
            save_font_scheme(self.project_dir, scheme)
            record = self.manifest.version.stage(Stage.RELEASE)
            record.status = StageStatus.PENDING
            record.error = ""
            self.save()

    def set_glossary(self, glossary: dict[str, object]) -> None:
        with self._mutation("set-glossary"):
            _atomic_json(self.project_dir / "glossary.json", glossary)
            for stage in STAGE_ORDER[STAGE_ORDER.index(Stage.TRANSLATE):]:
                record = self.manifest.version.stage(stage)
                record.status = StageStatus.PENDING
                record.error = ""
            self.save()

    def _safe_remove(self, path: Path) -> None:
        resolved = path.resolve()
        if os.path.commonpath([str(self.project_dir), str(resolved)]) != str(self.project_dir):
            raise ValueError(f"拒绝删除项目目录外路径: {resolved}")
        if resolved.is_dir():
            shutil.rmtree(resolved)
        elif resolved.exists():
            resolved.unlink()

    def _stage_input_hash(self, stage: Stage) -> str:
        if stage is Stage.COPY:
            return hash_directory(self.manifest.version.original_path)
        previous = STAGE_ORDER[STAGE_ORDER.index(stage) - 1]
        record = self.manifest.version.stage(previous)
        extra: dict[str, object] = {}
        if stage is Stage.UNPACK:
            extra["ascii_runner_dir"] = self.settings.ascii_runner_dir
        elif stage is Stage.EXTRACT:
            tool = Path(self.settings.wolf_tool_path)
            extra["wolf_tool"] = str(tool.resolve()) if tool.exists() else str(tool)
            extra["wolf_tool_size"] = tool.stat().st_size if tool.is_file() else 0
            extra["export_schema"] = EXPORT_SCHEMA
            extra["export_scope"] = self.manifest.export_scope.__dict__
            extra["exclude_large_external_files"] = self.manifest.exclude_large_external_files
            extra["external_file_limit_kb"] = self.manifest.external_file_limit_kb
        elif stage is Stage.GLOSSARY:
            extra.update(
                {
                    "api_url": self.settings.glossary_api_base_url,
                    "model": self.settings.glossary_api_model,
                    "threads": self.settings.glossary_api_threads,
                    "timeout": self.settings.glossary_api_timeout,
                    "chunk_chars": self.settings.glossary_chunk_chars,
                    "max_tokens": self.settings.glossary_api_max_tokens,
                    "translation_scope": self.manifest.translation_scope.__dict__,
                }
            )
        elif stage is Stage.TRANSLATE:
            glossary = self.project_dir / "glossary.json"
            extra.update(
                {
                    "api_url": self.settings.api_base_url,
                    "model": self.settings.api_model,
                    "threads": self.settings.api_threads,
                    "glossary_sha256": hashlib.sha256(glossary.read_bytes()).hexdigest() if glossary.is_file() else "",
                    "translation_scope": self.manifest.translation_scope.__dict__,
                }
            )
        elif stage is Stage.IMPORT:
            extra["import_scope"] = self.manifest.import_scope.__dict__
        elif stage is Stage.RELEASE:
            scheme = load_font_scheme(self.project_dir)
            extra["font_scheme"] = scheme_hash(scheme)
            original_fonts = self._original_font_record(required=False)
            if original_fonts is not None:
                extra["original_fonts"] = original_fonts
            if scheme is not None:
                resolved = resolve_scheme_files(self.project_dir, scheme)
                extra["font_files"] = [
                    sha256_file(path) for files in resolved for path in files
                ]
                items_path = self.manifest.version.stage(Stage.VALIDATE).artifacts.get("items", "")
                if items_path and Path(items_path).is_file():
                    corpus = final_display_texts(
                        load_items(items_path), self.manifest.import_scope
                    )
                    extra["font_corpus_sha256"] = hashlib.sha256(
                        json.dumps(corpus, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                    ).hexdigest()
        payload = json.dumps(
            {"input_hash": record.input_hash, "artifacts": record.artifacts, "extra": extra},
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _invalidate_changed_inputs(self) -> None:
        invalid = False
        for stage in STAGE_ORDER[1:]:
            record = self.manifest.version.stage(stage)
            if invalid:
                record.status = StageStatus.PENDING
                record.error = ""
                continue
            if record.status is StageStatus.COMPLETED and record.input_hash != self._stage_input_hash(stage):
                invalid = True
                record.status = StageStatus.PENDING
                record.error = ""
        if invalid:
            self.log("检测到工具、API、术语或范围变化，已重置受影响的下游阶段。")
            self.save()

    def _check_source_unchanged(self) -> None:
        version = self.manifest.version
        if not version.source_hash:
            return
        current = hash_directory(version.original_path)
        if current != version.source_hash:
            raise RuntimeError("原始游戏内容已变化。请把它作为新的源版本添加，现有版本不会被覆盖。")

    def _mark_running(self, stage: Stage, input_hash: str) -> None:
        record = self.manifest.version.stage(stage)
        record.status = StageStatus.RUNNING
        record.started_at = utc_now()
        record.finished_at = ""
        record.input_hash = input_hash
        record.error = ""
        self.detail(f"stage.state stage={stage.value} status=running input_hash={input_hash}")
        self.save()

    def _mark_done(self, stage: Stage, artifacts: dict[str, str]) -> None:
        record = self.manifest.version.stage(stage)
        record.status = StageStatus.COMPLETED
        record.finished_at = utc_now()
        record.artifacts = artifacts
        record.error = ""
        self.detail(
            f"stage.state stage={stage.value} status=completed artifacts="
            + json.dumps(artifacts, ensure_ascii=False, sort_keys=True)
        )
        self.save()

    def _mark_failed(self, stage: Stage, error: Exception) -> None:
        record = self.manifest.version.stage(stage)
        record.status = StageStatus.CANCELLED if isinstance(error, CancelledError) else StageStatus.FAILED
        record.finished_at = utc_now()
        record.error = str(error)
        self.detail(
            f"stage.state stage={stage.value} status={record.status.value} "
            f"error_type={type(error).__name__} error={error}"
        )
        self.save()

    def _reset_downstream(self, stage: Stage) -> None:
        for downstream in STAGE_ORDER[STAGE_ORDER.index(stage) + 1 :]:
            record = self.manifest.version.stage(downstream)
            record.status = StageStatus.PENDING
            record.error = ""

    def _previous_version(self) -> VersionManifest | None:
        keys = list(self.manifest.versions)
        index = keys.index(self.manifest.active_version)
        return self.manifest.versions[keys[index - 1]] if index > 0 else None

    def _official_runner(self, scope: ImportScope) -> OfficialToolRunner:
        executable = prepare_official_tool(
            self.settings.wolf_tool_path,
            self.cache_root / "tools" / "wolf-official",
        )
        return OfficialToolRunner(executable, scope)

    def _run_scoped_export(self, runner: OfficialToolRunner, mode: str) -> Path:
        operation = runner.update_excel if mode == "UPDATE_EXCEL" else runner.extract
        kwargs = {
            "cancel_event": self.cancel_event,
            "log": self.log,
            "diagnostic_log": self.detail,
            "warning": self.warning,
        }
        if not (
            self.manifest.export_scope.external
            and self.manifest.exclude_large_external_files
        ):
            return operation(self.work_dir, **kwargs)

        limit_kb = self.manifest.external_file_limit_kb
        with temporary_external_filter_view(
            self.work_dir,
            self.version_dir,
            limit_kb,
            diagnostic_log=self.detail,
        ) as (view, excluded):
            total_bytes = sum(size for _path, size in excluded)
            if excluded:
                sample = "、".join(path for path, _size in excluded[:5])
                suffix = " 等" if len(excluded) > 5 else ""
                self.warning(
                    f"已临时排除超过 {limit_kb} KB 的 TXT/CSV：{len(excluded)} 个，"
                    f"共 {total_bytes / 1024 / 1024:.2f} MiB；{sample}{suffix}"
                )
                for relative, size in excluded:
                    self.detail(
                        f"external-filter.excluded path={relative} bytes={size}"
                    )
            else:
                self.detail(f"external-filter.excluded none limit_kb={limit_kb}")
            workbook = operation(view, **kwargs)
            target = self.work_dir / SUPPORT_DIR / WORKBOOK_NAME
            target.parent.mkdir(parents=True, exist_ok=True)
            with atomic_output_path(target) as temporary:
                shutil.copy2(workbook, temporary)
            return target

    def _copy(self) -> dict[str, str]:
        original = _validate_game(self.manifest.version.original_path)
        self.version_dir.mkdir(parents=True, exist_ok=True)
        for path in (self.source_dir, self.work_dir):
            if path.exists():
                self._safe_remove(path)
        self.log("正在复制原始游戏到版本工作区...")
        shutil.copytree(original, self.source_dir)
        shutil.copytree(self.source_dir, self.work_dir)
        source_hash = hash_directory(original)
        if hash_directory(self.source_dir) != source_hash:
            raise RuntimeError("原始游戏副本哈希校验失败。")
        self.manifest.version.source_hash = source_hash
        self.detail(f"copy.complete source={original} source_hash={source_hash}")
        return {"source": str(self.source_dir), "work": str(self.work_dir)}

    def _unpack(self) -> dict[str, str]:
        executable = prepare_uberwolf(self.settings.ascii_runner_dir)
        UberWolfRunner(executable).unpack(
            self.work_dir,
            cancel_event=self.cancel_event,
            log=self.log,
            diagnostic_log=self.detail,
        )
        self.detail(f"unpack.complete data={self.work_dir / 'Data'}")
        return {"data": str(self.work_dir / "Data"), "uberwolf": str(executable)}

    def _extract(self) -> dict[str, str]:
        runner = self._official_runner(self.manifest.export_scope)
        self.log("正在按导出范围生成 WOLF 翻译工作簿...")
        previous = self._previous_version()
        previous_full = None
        conflicts: list[dict[str, object]] = []
        if previous:
            previous_full = previous.stage(Stage.VALIDATE).artifacts.get("full_workbook")
        if previous_full and Path(previous_full).is_file():
            support = self.work_dir / SUPPORT_DIR
            support.mkdir(parents=True, exist_ok=True)
            shutil.copy2(previous_full, support / WORKBOOK_NAME)
            workbook = self._run_scoped_export(runner, "UPDATE_EXCEL")
            current_items = read_translation_items(workbook)
            current_items, conflicts = reconcile_incremental(read_translation_items(previous_full), current_items)
            write_full_workbook(workbook, workbook, current_items)
            self.log("已通过官方 UPDATE_EXCEL 迁移上一版本译文。")
        else:
            workbook = self._run_scoped_export(runner, "EXTRACT")
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        extracted = self.artifacts_dir / "source.xlsx"
        shutil.copy2(workbook, extracted)

        self.log("正在生成名称分类基准工作簿...")
        baseline_workbook = self._official_runner(name_baseline_scope(self.manifest.export_scope)).extract(
            self.work_dir,
            cancel_event=self.cancel_event,
            log=self.log,
            diagnostic_log=self.detail,
            warning=self.warning,
        )
        baseline = self.artifacts_dir / "source-baseline.xlsx"
        shutil.copy2(baseline_workbook, baseline)
        shutil.copy2(extracted, self.work_dir / SUPPORT_DIR / WORKBOOK_NAME)

        items = read_translation_items(extracted)
        baseline_items = read_translation_items(baseline)
        optional_name_rows = classify_optional_name_delta(items, baseline_items)
        items_path = dump_items(self.artifacts_dir / "items-extracted.json", items)
        original_fonts = record_original_fonts(
            self.project_dir,
            self.manifest.active_version,
            read_font_slots(items),
            self.manifest.version.source_hash,
            extracted,
        )
        categories: dict[str, int] = {}
        for item in items:
            categories[item.category.value] = categories.get(item.category.value, 0) + 1
        self.detail(
            f"extract.complete workbook={extracted} baseline={baseline} rows={len(items)} "
            f"baseline_rows={len(baseline_items)} optional_name_rows={optional_name_rows} categories="
            + json.dumps(categories, ensure_ascii=False, sort_keys=True)
        )
        artifacts = {
            "workbook": str(extracted),
            "baseline_workbook": str(baseline),
            "items": str(items_path),
            "optional_name_rows": str(optional_name_rows),
            "original_fonts": str(original_fonts),
        }
        if conflicts:
            conflicts_path = self.artifacts_dir / "incremental-conflicts.json"
            _atomic_json(conflicts_path, conflicts)
            artifacts["incremental_conflicts"] = str(conflicts_path)
            artifacts["incremental_conflict_count"] = str(len(conflicts))
            self.log(f"发现 {len(conflicts)} 条无法安全迁移的重复原文，已清空旧译并交给 AiNiee 重新翻译。")
        return artifacts

    def _glossary(self) -> dict[str, str]:
        items_path = self.manifest.version.stage(Stage.EXTRACT).artifacts["items"]
        glossary_path = self.project_dir / "glossary.json"
        items = selected_translation_items(load_items(items_path), self.manifest.translation_scope)
        rules = generate_glossary(
            items,
            glossary_path,
            self.settings,
            self.glossary_api_key,
            cancel_event=self.cancel_event,
            log=self.log,
            diagnostic_log=self.detail,
        )
        return {"glossary": str(glossary_path), "term_count": str(len(rules["prompt_dictionary_data"]))}

    def _translate(self) -> dict[str, str]:
        extract = self.manifest.version.stage(Stage.EXTRACT).artifacts
        items = load_items(extract["items"])
        paratranz = to_paratranz(items, self.manifest.translation_scope)
        input_path = self.artifacts_dir / "ainiee-input.json"
        _atomic_json(input_path, paratranz)
        self.detail(
            f"translate.input extracted_rows={len(items)} translatable_rows={len(paratranz)} input={input_path}"
        )
        retry_artifacts: dict[str, str] = {}
        if paratranz:
            runtime = require_managed_runtime(
                self.settings.ainiee_source,
                self.cache_root / "runtime" / "ainiee",
            )
            glossary = json.loads((self.project_dir / "glossary.json").read_text(encoding="utf-8"))
            raw = run_translation(
                runtime,
                input_path,
                self.artifacts_dir / "ainiee-output",
                glossary,
                self.manifest.project_id,
                self.settings,
                self.api_key,
                cancel_event=self.cancel_event,
                log=self.log,
                diagnostic_log=self.detail,
            )
            retry_errors = retryable_translation_errors(
                items, raw, self.manifest.translation_scope
            )
            if retry_errors:
                first_output_path = self.artifacts_dir / "ainiee-output-pass1.json"
                retry_input_path = self.artifacts_dir / "ainiee-retry-input.json"
                retry_reasons_path = self.artifacts_dir / "ainiee-retry-reasons.json"
                retry_output_path = self.artifacts_dir / "ainiee-retry-output.json"
                retry_report_path = self.artifacts_dir / "ainiee-retry-result.json"
                _atomic_json(first_output_path, raw)
                retry_input = [row for row in paratranz if str(row.get("key", "")) in retry_errors]
                if len(retry_input) != len(retry_errors):
                    raise RuntimeError("无法按失败键完整生成 AiNiee 重跑输入。")
                selected_by_key = {
                    item.key: item
                    for item in selected_translation_items(items, self.manifest.translation_scope)
                }
                retry_reasons = [
                    {
                        "key": row["key"],
                        "code": selected_by_key[str(row["key"])].code,
                        "context": selected_by_key[str(row["key"])].context,
                        "error": retry_errors[str(row["key"])],
                    }
                    for row in retry_input
                ]
                _atomic_json(retry_input_path, retry_input)
                _atomic_json(retry_reasons_path, retry_reasons)
                self.log(
                    f"AiNiee 首轮有 {len(retry_input)} 条未通过 WOLFLator 校验，"
                    f"新建会话定向重跑，最多 {self.settings.translation_rounds} 轮。"
                )
                self.detail(
                    f"translate.retry.start rows={len(retry_input)} input={retry_input_path} "
                    f"reasons={retry_reasons_path}"
                )
                retry_raw = run_translation(
                    runtime,
                    retry_input_path,
                    self.artifacts_dir / "ainiee-retry-output",
                    glossary,
                    f"{self.manifest.project_id}-retry",
                    self.settings,
                    self.api_key,
                    cancel_event=self.cancel_event,
                    log=self.log,
                    diagnostic_log=self.detail,
                )
                _atomic_json(retry_output_path, retry_raw)
                retry_items = [selected_by_key[str(row["key"])] for row in retry_input]
                remaining_errors = retryable_translation_errors(
                    retry_items, retry_raw, self.manifest.translation_scope
                )
                retry_by_key = {str(row["key"]): row for row in retry_raw}
                combined_by_key = {
                    str(row["key"]): row
                    for row in raw
                    if str(row.get("key", "")) not in retry_errors
                }
                combined_by_key.update(retry_by_key)
                raw = [
                    combined_by_key[str(row["key"])]
                    for row in paratranz
                    if str(row["key"]) in combined_by_key
                ]
                _atomic_json(
                    retry_report_path,
                    {
                        "first_pass_failed": len(retry_errors),
                        "retry_output_rows": len(retry_raw),
                        "remaining_failed": len(remaining_errors),
                        "remaining_errors": remaining_errors,
                    },
                )
                self.detail(
                    f"translate.retry.complete output_rows={len(retry_raw)} "
                    f"remaining={len(remaining_errors)} output={retry_output_path}"
                )
                retry_artifacts = {
                    "ainiee_first_output": str(first_output_path),
                    "ainiee_retry_input": str(retry_input_path),
                    "ainiee_retry_reasons": str(retry_reasons_path),
                    "ainiee_retry_output": str(retry_output_path),
                    "ainiee_retry_result": str(retry_report_path),
                }
        else:
            self.log("当前翻译范围没有需要交给 AiNiee 的文本。")
            raw = []
        raw_path = self.artifacts_dir / "ainiee-output.json"
        _atomic_json(raw_path, raw)
        merged = merge_ainiee_output(items, raw, self.manifest.translation_scope)
        items_path = dump_items(self.artifacts_dir / "items-translated.json", merged)
        translated_count = sum(bool(item.translation) for item in merged)
        self.detail(
            f"translate.complete raw_rows={len(raw)} merged_rows={len(merged)} "
            f"translated_rows={translated_count} output={raw_path}"
        )
        return {
            "ainiee_input": str(input_path),
            "ainiee_output": str(raw_path),
            "items": str(items_path),
            **retry_artifacts,
        }

    def _validate(self) -> dict[str, str]:
        template = self.manifest.version.stage(Stage.EXTRACT).artifacts["workbook"]
        items = load_items(self.manifest.version.stage(Stage.TRANSLATE).artifacts["items"])
        full = write_full_workbook(template, self.artifacts_dir / "translated-full.xlsx", items)
        reread = read_translation_items(full)
        if len(reread) != len(items):
            raise RuntimeError("完整工作簿回读行数不一致。")
        required = selected_translation_requirements(items, self.manifest.translation_scope)
        reread_by_key = {item.key: item for item in reread}
        missing = [
            item
            for item in items
            if item.key in required and not reread_by_key.get(item.key, item).translation
        ]
        self.detail(
            f"validate.result expected_rows={len(items)} workbook_rows={len(reread)} missing={len(missing)} "
            f"workbook={full}"
        )
        if missing:
            raise RuntimeError(f"完整工作簿仍有 {len(missing)} 条空译文。")
        return {"full_workbook": str(full), "items": self.manifest.version.stage(Stage.TRANSLATE).artifacts["items"]}

    def _import(self) -> dict[str, str]:
        full = self.manifest.version.stage(Stage.VALIDATE).artifacts["full_workbook"]
        items = load_items(self.manifest.version.stage(Stage.VALIDATE).artifacts["items"])
        required = selected_translation_requirements(items, self.manifest.import_scope)
        missing = [item for item in items if item.key in required and not item.translation]
        if missing:
            sample = "、".join(item.original[:30] for item in missing[:3])
            raise RuntimeError(
                f"导入范围中有 {len(missing)} 条内容没有译文，请扩大翻译范围并重新翻译。"
                + (f"例如：{sample}" if sample else "")
            )
        self.detail(
            "import.start scope="
            + json.dumps(self.manifest.import_scope.__dict__, ensure_ascii=False, sort_keys=True)
        )
        scoped = write_scoped_workbook(
            full,
            self.artifacts_dir / "import-scoped.xlsx",
            self.manifest.import_scope,
            self.work_dir,
            items,
        )
        support = self.work_dir / SUPPORT_DIR
        support.mkdir(parents=True, exist_ok=True)
        shutil.copy2(scoped, support / WORKBOOK_NAME)
        runner = self._official_runner(full_export_scope())
        translated = runner.translate(
            self.work_dir,
            cancel_event=self.cancel_event,
            log=self.log,
            diagnostic_log=self.detail,
        )
        artifacts = {"scoped_workbook": str(scoped), "translated_game": str(translated)}
        diagnostics = runner.diagnostics
        originals_by_code: dict[str, set[str]] = {}
        for item in items:
            originals_by_code.setdefault(item.code, set()).add(item.original)
        for diagnostic in diagnostics:
            originals = originals_by_code.get(diagnostic["code"], set())
            if len(originals) == 1:
                diagnostic["source"] = next(iter(originals))
        diagnostics_path = self.artifacts_dir / "official-diagnostics.json"
        if diagnostics:
            _atomic_json(diagnostics_path, diagnostics)
            artifacts["official_warning_count"] = str(len(diagnostics))
            artifacts["official_warnings"] = str(diagnostics_path)
            self.warning(f"官方工具完成，但报告了 {len(diagnostics)} 条警告；详情已保存。")
        else:
            diagnostics_path.unlink(missing_ok=True)
        if runner.console_outputs:
            console_path = self.artifacts_dir / "official-console.txt"
            console_path.write_text(
                "\n\n".join(
                    f"===== {entry['mode']} TIMELINE =====\n{entry['timeline']}\n\n"
                    f"===== {entry['mode']} FINAL SCREEN =====\n{entry['final']}"
                    for entry in runner.console_outputs
                ),
                encoding="utf-8",
            )
            artifacts["official_console"] = str(console_path)
        return artifacts

    def _font_coverage_warnings(
        self,
        scheme: dict[str, object],
        original_slots: list[str],
        required: set[str],
        game_root: Path,
    ) -> tuple[list[dict[str, object]], list[list[Path]]]:
        resolved = resolve_scheme_files(self.project_dir, scheme)
        candidates = (
            discover_font_candidates(game_root, required)
            if any(slot["mode"] == "keep" for slot in scheme["slots"])
            else []
        )
        warnings: list[dict[str, object]] = []
        coverage_files: list[list[Path]] = []
        for index, slot in enumerate(scheme["slots"]):
            family = original_slots[index] if slot["mode"] == "keep" else str(slot["family"])
            files = resolved[index]
            if slot["mode"] == "keep":
                candidate = candidate_for_family(candidates, family) if family else None
                files = list(candidate.files) if candidate else []
            coverage_files.append(files)
            coverage: set[int] = set()
            for path in files:
                coverage.update(font_file_info(path)[1])
            missing = sorted(
                (character for character in required if ord(character) not in coverage),
                key=ord,
            )
            if missing:
                warnings.append(
                    {
                        "slot": index,
                        "slot_name": FONT_SLOT_NAMES[index],
                        "family": family,
                        "files": [str(path) for path in files],
                        "missing_count": len(missing),
                        "missing": missing,
                    }
                )
        return warnings, coverage_files

    def _original_font_record(self, *, required: bool = True) -> dict[str, object] | None:
        record = load_original_fonts(self.project_dir, self.manifest.active_version)
        if record is not None:
            return record
        extract = self.manifest.version.stage(Stage.EXTRACT).artifacts
        items_path = extract.get("items", "")
        workbook_path = extract.get("workbook", "")
        if not items_path or not workbook_path or not Path(items_path).is_file() or not Path(workbook_path).is_file():
            if required:
                raise RuntimeError("当前版本缺少可用于记录原字体的导出产物。")
            return None
        source_items = load_items(items_path)
        record_original_fonts(
            self.project_dir,
            self.manifest.active_version,
            read_font_slots(source_items),
            self.manifest.version.source_hash,
            workbook_path,
        )
        return load_original_fonts(self.project_dir, self.manifest.active_version)

    def _copy_font_files(
        self,
        destination: Path,
        scheme: dict[str, object],
        resolved: list[list[Path]],
    ) -> list[dict[str, str]]:
        copied: dict[str, dict[str, str]] = {}
        for slot, files in zip(scheme["slots"], resolved, strict=True):
            if slot["mode"] == "keep":
                continue
            for source in files:
                digest = sha256_file(source)
                previous = copied.get(source.name.casefold())
                if previous and previous["sha256"] != digest:
                    raise RuntimeError(f"两个字体文件同名但内容不同: {source.name}")
                target = destination / source.name
                if target.is_file() and sha256_file(target) != digest:
                    raise RuntimeError(f"发布目录已有同名但内容不同的字体文件: {source.name}")
                if not target.exists():
                    shutil.copy2(source, target)
                copied[source.name.casefold()] = {
                    "filename": source.name,
                    "sha256": digest,
                    "family": str(slot["family"]),
                }
        return sorted(copied.values(), key=lambda item: item["filename"].casefold())

    def _build_font_release(
        self,
        translated: Path,
        temporary: Path,
        scheme: dict[str, object],
    ) -> dict[str, str]:
        validated_items = load_items(self.manifest.version.stage(Stage.VALIDATE).artifacts["items"])
        original_record = self._original_font_record()
        if original_record is None:
            raise RuntimeError("无法建立当前版本的原字体记录。")
        original_slots = list(original_record["slots"])
        desired_slots = [
            original_slots[index] if slot["mode"] == "keep" else str(slot["family"])
            for index, slot in enumerate(scheme["slots"])
        ]
        if not desired_slots[0].strip():
            raise RuntimeError("项目原始主字体为空，无法建立可验证的字体方案。")
        required = required_characters(final_display_texts(validated_items, self.manifest.import_scope))
        warnings, resolved = self._font_coverage_warnings(
            scheme, original_slots, required, translated
        )
        fingerprint = coverage_fingerprint(required, scheme)
        self.detail(
            f"font.coverage characters={len(required)} warnings={len(warnings)} "
            f"fingerprint={fingerprint}"
        )
        warning_path = self.artifacts_dir / "font-warnings.json"
        if warnings:
            _atomic_json(
                warning_path,
                {
                    "coverage_fingerprint": fingerprint,
                    "acknowledged": (
                        isinstance(scheme.get("coverage_ack"), dict)
                        and scheme["coverage_ack"].get("fingerprint") == fingerprint
                    ),
                    "warnings": warnings,
                },
            )
        else:
            warning_path.unlink(missing_ok=True)

        font_base = self.version_dir / ".font-base"
        if font_base.exists():
            self._safe_remove(font_base)
        shutil.copytree(translated, font_base)
        copied_files: list[dict[str, str]] = []
        console_path = self.artifacts_dir / "official-font-console.txt"
        console_path.unlink(missing_ok=True)
        try:
            runner = self._official_runner(full_export_scope())
            self.log("正在导出字体修改前的文本基线...")
            baseline_workbook = runner.extract(
                font_base,
                cancel_event=self.cancel_event,
                log=self.log,
                diagnostic_log=self.detail,
                warning=self.warning,
            )
            baseline_items = read_translation_items(baseline_workbook)
            font_workbook = write_font_workbook(
                baseline_workbook,
                self.artifacts_dir / "font-import.xlsx",
                desired_slots,
            )
            support = font_base / SUPPORT_DIR
            support.mkdir(parents=True, exist_ok=True)
            shutil.copy2(font_workbook, support / WORKBOOK_NAME)
            self.log("正在通过官方工具应用四槽位字体方案...")
            generated = runner.translate(
                font_base,
                cancel_event=self.cancel_event,
                log=self.log,
                diagnostic_log=self.detail,
            )
            copied_files = self._copy_font_files(generated, scheme, resolved)
            verification_workbook = runner.extract(
                generated,
                cancel_event=self.cancel_event,
                log=self.log,
                diagnostic_log=self.detail,
                warning=self.warning,
            )
            verification_items = read_translation_items(verification_workbook)
            actual_slots = read_font_slots(verification_items)
            if actual_slots != desired_slots:
                raise RuntimeError(
                    "字体导入回读不一致: "
                    + json.dumps({"expected": desired_slots, "actual": actual_slots}, ensure_ascii=False)
                )
            baseline_non_font = [
                (item.code, item.flag, item.type, item.info, item.original)
                for item in baseline_items
                if item.code not in FONT_CODES
            ]
            verification_non_font = [
                (item.code, item.flag, item.type, item.info, item.original)
                for item in verification_items
                if item.code not in FONT_CODES
            ]
            if verification_non_font != baseline_non_font:
                raise RuntimeError("字体修改导致四个字体字段以外的导出文本发生变化。")
            for item in copied_files:
                target = generated / item["filename"]
                if not target.is_file() or sha256_file(target) != item["sha256"]:
                    raise RuntimeError(f"发布字体文件校验失败: {item['filename']}")
            if runner.console_outputs:
                console_path.write_text(
                    "\n\n".join(
                        f"===== {entry['mode']} TIMELINE =====\n{entry['timeline']}\n\n"
                        f"===== {entry['mode']} FINAL SCREEN =====\n{entry['final']}"
                        for entry in runner.console_outputs
                    ),
                    encoding="utf-8",
                )
            support = generated / SUPPORT_DIR
            if support.exists():
                shutil.rmtree(support)
            replace_with_retry(generated, temporary)
        finally:
            if font_base.exists():
                self._safe_remove(font_base)

        result_path = self.artifacts_dir / "font-result.json"
        _atomic_json(
            result_path,
            {
                "scheme_sha256": scheme_hash(scheme),
                "coverage_fingerprint": fingerprint,
                "required_character_count": len(required),
                "original_slots": original_slots,
                "applied_slots": desired_slots,
                "copied_files": copied_files,
            },
        )
        artifacts = {
            "font_scheme_sha256": scheme_hash(scheme),
            "font_result": str(result_path),
            "font_warning_count": str(len(warnings)),
        }
        if warnings:
            artifacts["font_warnings"] = str(warning_path)
            self.warning(f"字体方案有 {len(warnings)} 个槽位存在缺字；发布继续。")
            for warning in warnings:
                missing = list(warning["missing"])
                sample = json.dumps("".join(missing[:24]), ensure_ascii=False)
                suffix = "（仅显示前 24 个）" if len(missing) > 24 else ""
                self.warning(
                    f"字体缺字：{warning['slot_name']} / {warning['family']}，"
                    f"缺少 {warning['missing_count']} 个字符，样例 {sample}{suffix}"
                )
        if console_path.is_file():
            artifacts["official_font_console"] = str(console_path)
        return artifacts

    def _release(self) -> dict[str, str]:
        translated = Path(self.manifest.version.stage(Stage.IMPORT).artifacts["translated_game"])
        if not (translated / "Game.exe").is_file() or not (translated / "Data").is_dir():
            raise RuntimeError("官方发布目录不完整。")
        temporary = self.version_dir / ".release-ready"
        previous = self.version_dir / ".release-previous"
        for path in (temporary, previous):
            if path.exists():
                self._safe_remove(path)
        scheme = load_font_scheme(self.project_dir)
        font_artifacts = (
            self._build_font_release(translated, temporary, scheme)
            if scheme is not None
            else {}
        )
        if scheme is None:
            shutil.copytree(translated, temporary)
        try:
            if self.release_dir.exists():
                replace_with_retry(self.release_dir, previous)
            replace_with_retry(temporary, self.release_dir)
            if previous.exists():
                self._safe_remove(previous)
        except Exception as error:
            if not self.release_dir.exists() and previous.exists():
                try:
                    replace_with_retry(previous, self.release_dir)
                except Exception as restore_error:
                    raise RuntimeError(
                        f"发布替换失败，旧发布目录保留在 {previous}，但无法恢复到 {self.release_dir}: "
                        f"{restore_error}"
                    ) from restore_error
            if isinstance(error, PermissionError) or getattr(error, "winerror", None) in {5, 32}:
                raise RuntimeError(f"发布目录正在使用，无法替换: {self.release_dir}") from error
            raise
        self._check_source_unchanged()
        self.detail(f"release.complete path={self.release_dir}")
        return {"release": str(self.release_dir), **font_artifacts}

    def _execute(self, stage: Stage) -> dict[str, str]:
        functions = {
            Stage.COPY: self._copy,
            Stage.UNPACK: self._unpack,
            Stage.EXTRACT: self._extract,
            Stage.GLOSSARY: self._glossary,
            Stage.TRANSLATE: self._translate,
            Stage.VALIDATE: self._validate,
            Stage.IMPORT: self._import,
            Stage.RELEASE: self._release,
        }
        return functions[stage]()

    def run(self) -> str:
        with self._mutation("one-click"):
            return self._run_locked()

    def _run_locked(self) -> str:
        self.cancel_event.clear()
        self._start_run_log("one-click")
        copy_record = self.manifest.version.stage(Stage.COPY)
        if copy_record.status is StageStatus.COMPLETED:
            self._check_source_unchanged()
            self._invalidate_changed_inputs()
        for index, stage in enumerate(STAGE_ORDER, start=1):
            record = self.manifest.version.stage(stage)
            self.progress(index - 1, len(STAGE_ORDER), stage)
            if record.status is StageStatus.COMPLETED:
                self.detail(f"stage.skip stage={stage.value} reason=already_completed")
                continue
            input_hash = self._stage_input_hash(stage)
            self._mark_running(stage, input_hash)
            self._emit_state(stage, StageStatus.RUNNING, index - 1, len(STAGE_ORDER), "正在执行")
            self.log(f"[{index}/{len(STAGE_ORDER)}] {stage.value} 开始")
            started = time.monotonic()
            try:
                artifacts = self._execute(stage)
                self._mark_done(stage, artifacts)
            except Exception as exc:
                self.detail(
                    f"stage.exception stage={stage.value} elapsed={time.monotonic() - started:.3f}s\n"
                    + traceback.format_exc()
                )
                self._mark_failed(stage, exc)
                self._emit_state(
                    stage,
                    self.manifest.version.stage(stage).status,
                    index - 1,
                    len(STAGE_ORDER),
                    str(exc),
                )
                self.error(f"[{index}/{len(STAGE_ORDER)}] {stage.value} 出现错误: {exc}")
                raise
            self.progress(index, len(STAGE_ORDER), stage)
            self._emit_state(stage, StageStatus.COMPLETED, index, len(STAGE_ORDER), "已完成")
            self.detail(f"stage.complete stage={stage.value} elapsed={time.monotonic() - started:.3f}s")
            self.log(f"[{index}/{len(STAGE_ORDER)}] {stage.value} 完成")
        return "completed"

    def run_stage(self, stage: Stage) -> str:
        with self._mutation(stage.value):
            return self._run_stage_locked(stage)

    def _run_stage_locked(self, stage: Stage) -> str:
        self.cancel_event.clear()
        self._start_run_log(stage.value)
        self._reset_downstream(stage)
        input_hash = self._stage_input_hash(stage)
        self._mark_running(stage, input_hash)
        self._emit_state(stage, StageStatus.RUNNING, 0, 1, "正在执行")
        self.progress(0, 0, stage)
        self.log(f"[{STAGE_ORDER.index(stage) + 1}/{len(STAGE_ORDER)}] {stage.value} 开始")
        started = time.monotonic()
        try:
            artifacts = self._execute(stage)
            self._mark_done(stage, artifacts)
        except Exception as exc:
            self.detail(
                f"stage.exception stage={stage.value} elapsed={time.monotonic() - started:.3f}s\n"
                + traceback.format_exc()
            )
            self._mark_failed(stage, exc)
            self._emit_state(stage, self.manifest.version.stage(stage).status, 0, 1, str(exc))
            self.error(
                f"[{STAGE_ORDER.index(stage) + 1}/{len(STAGE_ORDER)}] {stage.value} 出现错误: {exc}"
            )
            raise
        self.progress(1, 1, stage)
        self._emit_state(stage, StageStatus.COMPLETED, 1, 1, "已完成")
        self.detail(f"stage.complete stage={stage.value} elapsed={time.monotonic() - started:.3f}s")
        self.log(f"[{STAGE_ORDER.index(stage) + 1}/{len(STAGE_ORDER)}] {stage.value} 完成")
        return "completed"

    def retry_failed(self) -> None:
        with self._mutation("retry-failed"):
            found = False
            for stage in STAGE_ORDER:
                record = self.manifest.version.stage(stage)
                if record.status in {StageStatus.FAILED, StageStatus.CANCELLED}:
                    found = True
                if found and record.status is not StageStatus.COMPLETED:
                    record.status = StageStatus.PENDING
                    record.error = ""
            self.save()

    def last_release(self) -> Path | None:
        value = self.manifest.version.stage(Stage.RELEASE).artifacts.get("release")
        return Path(value) if value and Path(value).is_dir() else None
