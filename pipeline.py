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
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from ainiee import (
    generate_glossary,
    require_managed_runtime,
    run_translation,
)
from models import (
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
from wolf_tools import (
    CancelledError,
    OfficialToolRunner,
    UberWolfRunner,
    apply_managed_translations,
    classify_optional_name_delta,
    dump_items,
    full_export_scope,
    hash_directory,
    load_items,
    locate_workbook,
    merge_ainiee_output,
    name_baseline_scope,
    prepare_official_tool,
    prepare_uberwolf,
    read_translation_items,
    reconcile_incremental,
    selected_translation_items,
    selected_translation_requirements,
    to_paratranz,
    write_full_workbook,
    write_scoped_workbook,
    WORKBOOK_NAME,
    SUPPORT_DIR,
)


EXPORT_SCHEMA = 2


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


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
    _atomic_json(project_dir / "project.json", manifest.to_dict())
    return project_dir / "project.json"


def load_manifest(path: str | Path) -> ProjectManifest:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return ProjectManifest.from_dict(data)


def add_version(manifest_path: str | Path, game_path: str | Path) -> ProjectManifest:
    path = Path(manifest_path)
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
        glossary_api_key: str | None = None,
        log: Callable[[str], None] | None = None,
        progress: Callable[[int, int, Stage], None] | None = None,
    ):
        self.manifest_path = Path(manifest_path).resolve()
        self.project_dir = self.manifest_path.parent
        self.settings = settings
        self.api_key = api_key
        self.glossary_api_key = api_key if glossary_api_key is None else glossary_api_key
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
        self.cancel_event = threading.Event()
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
        self.manifest.updated_at = utc_now()
        self.detail(f"manifest.save.start path={self.manifest_path}")
        try:
            _atomic_json(self.manifest_path, self.manifest.to_dict())
        except Exception:
            self.detail("manifest.save.failed\n" + traceback.format_exc())
            raise
        self.detail("manifest.save.complete")

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
            self._log_sink(f"无法创建外部日志: {exc}")
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
            self._log_sink(f"外部日志写入失败 ({failed_path}): {exc}")
        return text

    def log(self, message: str) -> None:
        text = self._write_log_file(message, "INFO")
        self._log_sink(text)

    def detail(self, message: str) -> None:
        self._write_log_file(message, "DETAIL")

    def cancel(self) -> None:
        self.cancel_event.set()

    def set_run_mode(self, mode: RunMode) -> None:
        self.manifest.run_mode = mode
        self.save()

    def set_import_scope(self, scope: ImportScope) -> None:
        if self.manifest.import_scope == scope:
            return
        self.manifest.import_scope = scope
        for stage in STAGE_ORDER[STAGE_ORDER.index(Stage.IMPORT):]:
            record = self.manifest.version.stage(stage)
            record.status = StageStatus.PENDING
            record.error = ""
        self.save()

    def set_translation_scope(self, scope: ImportScope) -> None:
        if self.manifest.translation_scope == scope:
            return
        self.manifest.translation_scope = scope
        for stage in STAGE_ORDER[STAGE_ORDER.index(Stage.GLOSSARY):]:
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
        elif stage is Stage.GLOSSARY:
            extra.update(
                {
                    "api_url": self.settings.glossary_api_base_url,
                    "model": self.settings.glossary_api_model,
                    "threads": self.settings.glossary_api_threads,
                    "timeout": self.settings.glossary_api_timeout,
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
            self.log("检测到工具、API、术语或导入范围变化，已重置受影响的下游阶段。")
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
            if record.artifacts.get("skipped") == "true":
                record.artifacts = {}

    def _reset_skipped_for_one_click(self) -> None:
        reset = False
        for stage in STAGE_ORDER:
            record = self.manifest.version.stage(stage)
            if record.artifacts.get("skipped") == "true":
                reset = True
                record.artifacts = {}
            if reset:
                record.status = StageStatus.PENDING
                record.error = ""
        if reset:
            self.log("一键模式将执行分步模式中手动跳过的阶段。")
            self.save()

    def _previous_version(self) -> VersionManifest | None:
        keys = list(self.manifest.versions)
        index = keys.index(self.manifest.active_version)
        return self.manifest.versions[keys[index - 1]] if index > 0 else None

    def _official_runner(self, scope: ImportScope | None = None) -> OfficialToolRunner:
        executable = prepare_official_tool(
            self.settings.wolf_tool_path,
            self.cache_root / "tools" / "wolf-official",
        )
        return OfficialToolRunner(executable, scope or full_export_scope())

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
        runner = self._official_runner(full_export_scope())
        self.log("正在生成全量 WOLF 翻译工作簿...")
        previous = self._previous_version()
        previous_full = None
        conflicts: list[dict[str, object]] = []
        if previous:
            previous_full = previous.stage(Stage.VALIDATE).artifacts.get("full_workbook")
        if previous_full and Path(previous_full).is_file():
            support = self.work_dir / SUPPORT_DIR
            support.mkdir(parents=True, exist_ok=True)
            shutil.copy2(previous_full, support / WORKBOOK_NAME)
            workbook = runner.update_excel(
                self.work_dir,
                cancel_event=self.cancel_event,
                log=self.log,
                diagnostic_log=self.detail,
            )
            current_items = read_translation_items(workbook)
            current_items, conflicts = reconcile_incremental(read_translation_items(previous_full), current_items)
            write_full_workbook(workbook, workbook, current_items)
            self.log("已通过官方 UPDATE_EXCEL 迁移上一版本译文。")
        else:
            workbook = runner.extract(
                self.work_dir,
                cancel_event=self.cancel_event,
                log=self.log,
                diagnostic_log=self.detail,
            )
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        extracted = self.artifacts_dir / "source.xlsx"
        shutil.copy2(workbook, extracted)

        self.log("正在生成名称分类基准工作簿...")
        baseline_workbook = self._official_runner(name_baseline_scope()).extract(
            self.work_dir,
            cancel_event=self.cancel_event,
            log=self.log,
            diagnostic_log=self.detail,
        )
        baseline = self.artifacts_dir / "source-baseline.xlsx"
        shutil.copy2(baseline_workbook, baseline)
        shutil.copy2(extracted, self.work_dir / SUPPORT_DIR / WORKBOOK_NAME)

        items = read_translation_items(extracted)
        baseline_items = read_translation_items(baseline)
        optional_name_rows = classify_optional_name_delta(items, baseline_items)
        items_path = dump_items(self.artifacts_dir / "items-extracted.json", items)
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
        apply_managed_translations(items)
        paratranz = to_paratranz(items, self.manifest.translation_scope)
        input_path = self.artifacts_dir / "ainiee-input.json"
        _atomic_json(input_path, paratranz)
        self.detail(
            f"translate.input extracted_rows={len(items)} translatable_rows={len(paratranz)} input={input_path}"
        )
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
        return {"ainiee_input": str(input_path), "ainiee_output": str(raw_path), "items": str(items_path)}

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
        translated = self._official_runner().translate(
            self.work_dir,
            cancel_event=self.cancel_event,
            log=self.log,
            diagnostic_log=self.detail,
        )
        return {"scoped_workbook": str(scoped), "translated_game": str(translated)}

    def _release(self) -> dict[str, str]:
        translated = Path(self.manifest.version.stage(Stage.IMPORT).artifacts["translated_game"])
        if not (translated / "Game.exe").is_file() or not (translated / "Data").is_dir():
            raise RuntimeError("官方发布目录不完整。")
        temporary = self.version_dir / ".release-ready"
        previous = self.version_dir / ".release-previous"
        for path in (temporary, previous):
            if path.exists():
                self._safe_remove(path)
        shutil.copytree(translated, temporary)
        try:
            if self.release_dir.exists():
                os.replace(self.release_dir, previous)
            os.replace(temporary, self.release_dir)
            if previous.exists():
                self._safe_remove(previous)
        except Exception:
            if not self.release_dir.exists() and previous.exists():
                os.replace(previous, self.release_dir)
            raise
        self._check_source_unchanged()
        self.detail(f"release.complete path={self.release_dir}")
        return {"release": str(self.release_dir)}

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
        self.cancel_event.clear()
        self._start_run_log("one-click")
        self._reset_skipped_for_one_click()
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
                self.log(f"[{index}/{len(STAGE_ORDER)}] {stage.value} 出现错误: {exc}")
                raise
            self.progress(index, len(STAGE_ORDER), stage)
            self.detail(f"stage.complete stage={stage.value} elapsed={time.monotonic() - started:.3f}s")
            self.log(f"[{index}/{len(STAGE_ORDER)}] {stage.value} 完成")
        return "completed"

    def run_stage(self, stage: Stage) -> str:
        self.cancel_event.clear()
        self._start_run_log(stage.value)
        self._reset_downstream(stage)
        record = self.manifest.version.stage(stage)
        if record.artifacts.get("skipped") == "true":
            record.artifacts = {}
        input_hash = self._stage_input_hash(stage)
        self._mark_running(stage, input_hash)
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
            self.log(
                f"[{STAGE_ORDER.index(stage) + 1}/{len(STAGE_ORDER)}] {stage.value} 出现错误: {exc}"
            )
            raise
        self.progress(1, 1, stage)
        self.detail(f"stage.complete stage={stage.value} elapsed={time.monotonic() - started:.3f}s")
        self.log(f"[{STAGE_ORDER.index(stage) + 1}/{len(STAGE_ORDER)}] {stage.value} 完成")
        return "completed"

    def skip_stage(self, stage: Stage) -> None:
        self._start_run_log(f"{stage.value}-skip")
        self._reset_downstream(stage)
        record = self.manifest.version.stage(stage)
        record.status = StageStatus.COMPLETED
        record.started_at = ""
        record.finished_at = utc_now()
        record.input_hash = ""
        record.error = ""
        record.artifacts = {"skipped": "true"}
        self.log(f"[{STAGE_ORDER.index(stage) + 1}/{len(STAGE_ORDER)}] {stage.value} 已手动跳过")
        self.save()

    def retry_failed(self) -> None:
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
