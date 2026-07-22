from __future__ import annotations

import argparse
import json
import signal
import sys
import time
import traceback
from pathlib import Path
from typing import Sequence

from PySide6.QtCore import QCoreApplication

from ainiee import (
    install_supported_ainiee,
    locate_ainiee_source,
    prepare_managed_runtime,
    test_api,
)
from models import MAX_EXTERNAL_FILE_LIMIT_KB, STAGE_ORDER, AppSettings, ImportScope, Stage
from pipeline import Pipeline, add_version, create_project, load_manifest
from safe_io import ResourceBusyError, project_lock, project_lock_status
from settings import SettingsStore, local_data_dir, validate_settings
from wolf_tools import CancelledError


def _stage(value: str) -> Stage:
    try:
        return Stage(value.lower())
    except ValueError as exc:
        choices = ", ".join(stage.value for stage in STAGE_ORDER)
        raise argparse.ArgumentTypeError(f"未知阶段 {value!r}，可选值：{choices}") from exc


def _external_file_limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("外部文件大小上限必须是整数 KB。") from exc
    if not 1 <= limit <= MAX_EXTERNAL_FILE_LIMIT_KB:
        raise argparse.ArgumentTypeError(
            f"外部文件大小上限必须在 1..{MAX_EXTERNAL_FILE_LIMIT_KB} KB 之间。"
        )
    return limit


def _settings_store(args: argparse.Namespace) -> SettingsStore:
    return SettingsStore(args.settings)


def _load_settings(args: argparse.Namespace) -> tuple[SettingsStore, AppSettings]:
    store = _settings_store(args)
    return store, store.load()


def _api_key(settings: AppSettings) -> str:
    if not settings.api_base_url.strip() or not settings.api_model.strip():
        raise RuntimeError("请在设置中填写 API 基础地址和模型。")
    key = SettingsStore.api_key(settings)
    if not key:
        raise RuntimeError("请在设置中填写 API 密钥。")
    return key


def _glossary_api_key(settings: AppSettings) -> str:
    if not settings.glossary_api_base_url.strip() or not settings.glossary_api_model.strip():
        raise RuntimeError("请在设置中填写术语生成 API 基础地址和模型。")
    key = SettingsStore.glossary_api_key(settings)
    if not key:
        raise RuntimeError("请在设置中填写术语生成 API 密钥。")
    return key


def _check_settings(settings: AppSettings) -> tuple[str, str]:
    errors = validate_settings(settings)
    if errors:
        raise RuntimeError("设置未完成：\n" + "\n".join(f"- {error}" for error in errors))
    return _api_key(settings), _glossary_api_key(settings)


def _print_progress(current: int, total: int, stage: Stage) -> None:
    print(f"progress stage={stage.value} {current}/{total}", flush=True)


def _print_log(message: str) -> None:
    print(f"log {message}", flush=True)


def _pipeline(
    args: argparse.Namespace,
    *,
    full_run: bool = False,
    stage: Stage | None = None,
) -> Pipeline:
    store, settings = _load_settings(args)
    api_key = ""
    glossary_api_key = ""
    if full_run:
        api_key, glossary_api_key = _check_settings(settings)
    elif stage is Stage.TRANSLATE:
        api_key = _api_key(settings)
    elif stage is Stage.GLOSSARY:
        glossary_api_key = _glossary_api_key(settings)
    return Pipeline(
        args.manifest,
        settings,
        api_key,
        local_data_dir(),
        glossary_api_key=glossary_api_key,
        log=_print_log,
        progress=_print_progress,
    )


def _status(args: argparse.Namespace) -> int:
    manifest = load_manifest(args.manifest)
    busy, owner = project_lock_status(args.manifest)
    safe_owner = {
        key: owner[key]
        for key in ("pid", "operation", "started_at")
        if key in owner
    }
    version = manifest.version
    records = {
        stage.value: {
            "status": version.stage(stage).status.value,
            "error": version.stage(stage).error,
            "artifacts": version.stage(stage).artifacts,
        }
        for stage in STAGE_ORDER
    }
    result = {
        "project": manifest.project_id,
        "name": manifest.name,
        "manifest": str(Path(args.manifest).resolve()),
        "active_version": manifest.active_version,
        "run_mode": manifest.run_mode.value,
        "export_scope": manifest.export_scope.__dict__,
        "translation_scope": manifest.translation_scope.__dict__,
        "import_scope": manifest.import_scope.__dict__,
        "busy": busy,
        "lock": safe_owner if busy else {},
        "stages": records,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"项目：{manifest.name} ({manifest.project_id})")
        print(f"版本：{manifest.active_version}")
        if busy:
            details = "，".join(
                str(value)
                for value in (
                    f"PID {safe_owner['pid']}" if safe_owner.get("pid") else "",
                    f"操作 {safe_owner['operation']}" if safe_owner.get("operation") else "",
                    f"开始于 {safe_owner['started_at']}" if safe_owner.get("started_at") else "",
                )
                if value
            )
            print(f"占用：是（{details or '占用者信息不可用'}）")
        for stage in STAGE_ORDER:
            record = version.stage(stage)
            suffix = f"：{record.error}" if record.error else ""
            if record.artifacts.get("official_warning_count"):
                suffix = f"（{record.artifacts['official_warning_count']} 个官方警告）"
            print(f"{stage.value:10} {record.status.value}{suffix}")
    return 0


def _settings_check(args: argparse.Namespace) -> int:
    _, settings = _load_settings(args)
    errors = validate_settings(settings, require_api=not args.no_api)
    result = {"valid": not errors, "errors": errors}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if errors:
            print("设置无效：", file=sys.stderr)
            for error in errors:
                print(f"- {error}", file=sys.stderr)
        else:
            print("设置有效。")
    return 0 if not errors else 2


def _api_test(args: argparse.Namespace) -> int:
    _, settings = _load_settings(args)
    glossary = args.target == "glossary"
    key = _glossary_api_key(settings) if glossary else _api_key(settings)
    response = test_api(settings, key, glossary=glossary)
    print(response)
    return 0


def _progress_download(received: int, total: int) -> None:
    now = time.monotonic()
    previous = getattr(_progress_download, "last", 0.0)
    if total and (received == total or now - previous >= 0.5):
        _progress_download.last = now
        print(f"download {received}/{total}", flush=True)


def _prepare_ainiee(args: argparse.Namespace) -> int:
    store, settings = _load_settings(args)
    if args.source:
        source = locate_ainiee_source(args.source)
    else:
        source = install_supported_ainiee(
            local_data_dir() / "packages" / "ainiee",
            repair=args.repair,
            progress=_progress_download,
            log=_print_log,
        )
    runtime = prepare_managed_runtime(
        source,
        local_data_dir() / "runtime" / "ainiee",
        force_sync=args.repair,
        log=_print_log,
    )
    settings.ainiee_source = str(source)
    store.save(settings)
    print(f"source={source}")
    print(f"runtime={runtime}")
    print("AiNiee 运行时已就绪。")
    return 0


def _create_project(args: argparse.Namespace) -> int:
    store, settings = _load_settings(args)
    projects_root = args.projects_root or settings.projects_root
    path = create_project(projects_root, args.game, args.name or "")
    settings.last_project = str(path)
    store.save(settings)
    print(path)
    return 0


def _add_version(args: argparse.Namespace) -> int:
    manifest = add_version(args.manifest, args.game)
    print(f"active_version={manifest.active_version}")
    return 0


def _run(args: argparse.Namespace) -> int:
    stage = args.stage
    pipeline = _pipeline(
        args,
        full_run=stage is None,
        stage=stage,
    )
    interrupted = False

    def cancel(_signum, _frame) -> None:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            print("正在取消当前阶段...", file=sys.stderr, flush=True)
            pipeline.cancel()
        raise CancelledError("任务已取消。")

    previous_handler = signal.signal(signal.SIGINT, cancel)
    try:
        result = pipeline.run_stage(stage) if stage is not None else pipeline.run()
    finally:
        signal.signal(signal.SIGINT, previous_handler)
    print(f"result={result}")
    release = pipeline.last_release()
    if release:
        print(f"release={release}")
    return 0


def _retry(args: argparse.Namespace) -> int:
    pipeline = _pipeline(args)
    pipeline.retry_failed()
    print("failed stages reset")
    return 0


def _scope(args: argparse.Namespace) -> int:
    filter_requested = (
        args.exclude_large_external is not None
        or args.external_size_limit_kb is not None
    )
    if filter_requested and args.target != "export":
        raise ValueError("大文件自动排除选项仅适用于 --target export。")
    with project_lock(args.manifest, f"set-{args.target}-scope"):
        pipeline = _pipeline(args)
        current = {
            "export": pipeline.manifest.export_scope,
            "translation": pipeline.manifest.translation_scope,
            "import": pipeline.manifest.import_scope,
        }[args.target]
        values = {
            name: getattr(args, name) if getattr(args, name) is not None else getattr(current, name)
            for name in ("display", "external", "optional_name", "halfwidth", "filename")
        }
        scope = ImportScope(**values)
        if args.target == "export":
            exclude = pipeline.manifest.exclude_large_external_files
            if args.exclude_large_external is not None:
                exclude = args.exclude_large_external
            limit_kb = (
                args.external_size_limit_kb
                if args.external_size_limit_kb is not None
                else pipeline.manifest.external_file_limit_kb
            )
            pipeline.set_export_scope(
                scope,
                exclude_large_external_files=exclude,
                external_file_limit_kb=limit_kb,
            )
        elif args.target == "translation":
            pipeline.set_translation_scope(scope)
        else:
            pipeline.set_import_scope(scope)
    result = {"target": args.target, **values}
    if args.target == "export":
        result.update(
            {
                "exclude_large_external_files": exclude,
                "external_file_limit_kb": limit_kb,
            }
        )
    print(json.dumps(result, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wolflator-cli",
        description="WOLFLator 无界面流水线入口。默认读取当前用户的持久化设置。",
    )
    parser.add_argument("--settings", help="自定义 settings.ini 路径")
    subparsers = parser.add_subparsers(dest="command", required=True)

    settings = subparsers.add_parser("settings-check", help="检查工具、目录、API 和许可证配置")
    settings.add_argument("--no-api", action="store_true", help="只检查工具配置，不要求 API 密钥")
    settings.add_argument("--json", action="store_true", help="使用 JSON 输出")

    api_test = subparsers.add_parser("api-test", help="使用持久化 API 配置发送一次测试请求")
    api_test.add_argument(
        "--target",
        choices=("translation", "glossary"),
        default="translation",
        help="测试 AiNiee 翻译或术语生成 API（默认 translation）",
    )

    ainiee = subparsers.add_parser("ainiee-prepare", help="安装或选择 AiNiee，并准备隔离依赖运行时")
    ainiee.add_argument("--source", help="已有 AiNiee GUI、安装目录或源码目录")
    ainiee.add_argument("--repair", action="store_true", help="强制重新同步 uv 依赖")

    create = subparsers.add_parser("project-create", help="从 WOLF 游戏目录创建项目")
    create.add_argument("game", help="包含 Game.exe 和 Data.wolf/Data 的游戏目录")
    create.add_argument("--name", help="项目显示名称")
    create.add_argument("--projects-root", help="覆盖持久化项目目录")

    version = subparsers.add_parser("project-add-version", help="向项目添加源版本")
    version.add_argument("manifest", help="project.json")
    version.add_argument("game", help="新版本游戏目录")

    status = subparsers.add_parser("status", help="查看活动版本和八个阶段状态")
    status.add_argument("manifest", help="project.json")
    status.add_argument("--json", action="store_true", help="使用 JSON 输出")

    run = subparsers.add_parser("run", help="运行一键流程，或只运行一个阶段")
    run.add_argument("manifest", help="project.json")
    run.add_argument("--stage", type=_stage, help="只运行一个阶段，例如 translate")

    retry = subparsers.add_parser("retry", help="重置失败/取消阶段及其后续阶段")
    retry.add_argument("manifest", help="project.json")

    scope = subparsers.add_parser("scope", help="查看或修改导出、翻译或导入范围")
    scope.add_argument("manifest", help="project.json")
    scope.add_argument(
        "--target",
        choices=("export", "translation", "import"),
        default="import",
        help="要修改的范围（默认 import）",
    )
    for name in ("display", "external", "optional_name", "halfwidth", "filename"):
        scope.add_argument(
            f"--{name.replace('_', '-')}",
            action=argparse.BooleanOptionalAction,
            default=None,
        )
    scope.add_argument(
        "--exclude-large-external",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="导出时自动排除超过上限的 TXT/CSV",
    )
    scope.add_argument(
        "--external-size-limit-kb",
        type=_external_file_limit,
        help="自动排除的大小上限（KB）",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(errors="backslashreplace")
    QCoreApplication.setApplicationName("WOLFLator")
    QCoreApplication.setOrganizationName("WOLFLator")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "settings-check":
            return _settings_check(args)
        if args.command == "api-test":
            return _api_test(args)
        if args.command == "ainiee-prepare":
            return _prepare_ainiee(args)
        if args.command == "project-create":
            return _create_project(args)
        if args.command == "project-add-version":
            return _add_version(args)
        if args.command == "status":
            return _status(args)
        if args.command == "run":
            return _run(args)
        if args.command == "retry":
            return _retry(args)
        if args.command == "scope":
            return _scope(args)
    except CancelledError:
        print("已取消。", file=sys.stderr)
        return 130
    except KeyboardInterrupt:
        print("已取消。", file=sys.stderr)
        return 130
    except ResourceBusyError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1
    parser.error(f"未知命令：{args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
