from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from models import MAX_EXTERNAL_FILE_LIMIT_KB, ImportScope, ToolResult
from safe_io import atomic_output_path, atomic_write_bytes, replace_with_retry
from process_tools import (
    OfficialArtifactMissingError,
    _emit_log,
    _silent_official_executable,
    hash_directory,
    parse_official_diagnostics,
    parse_official_map_failures,
    resource_path,
    run_process,
    sha256_file,
    verified_vendor_file,
)
from wolf_workbook import SUPPORT_DIR, WORKBOOK_NAME, locate_workbook


GAME_CONFIG_NAME = "WOLF_Translation_Game_Config.ini"

@contextmanager
def temporary_external_filter_view(
    game_root: str | Path,
    temporary_parent: str | Path,
    limit_kb: int,
    *,
    diagnostic_log: Callable[[str], None] | None = None,
) -> Iterator[tuple[Path, list[tuple[str, int]]]]:
    source_root = Path(game_root).resolve()
    source_data = source_root / "Data"
    source_exe = source_root / "Game.exe"
    if not source_exe.is_file() or not source_data.is_dir():
        raise FileNotFoundError("过滤视图需要工作副本中的 Game.exe 和 Data 目录。")
    if type(limit_kb) is not int or not 1 <= limit_kb <= MAX_EXTERNAL_FILE_LIMIT_KB:
        raise ValueError(
            f"外部文件大小上限必须是 1..{MAX_EXTERNAL_FILE_LIMIT_KB} KB 的整数。"
        )

    parent = Path(temporary_parent).resolve()
    parent.mkdir(parents=True, exist_ok=True)
    prefix = ".wolflator-export-view-"
    for stale in parent.glob(prefix + "*"):
        if stale.is_symlink() or stale.is_file():
            stale.unlink(missing_ok=True)
        elif stale.is_dir():
            shutil.rmtree(stale)

    view = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    excluded: list[tuple[str, int]] = []
    linked = 0
    copied = 0

    def link_or_copy(source: Path, target: Path) -> None:
        nonlocal linked, copied
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source, target)
            linked += 1
        except OSError:
            shutil.copy2(source, target)
            copied += 1

    try:
        link_or_copy(source_exe, view / "Game.exe")
        (view / "Data").mkdir()
        byte_limit = limit_kb * 1024
        for source in source_data.rglob("*"):
            relative = source.relative_to(source_root)
            target = view / relative
            if source.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not source.is_file():
                continue
            size = source.stat().st_size
            if source.suffix.lower() in {".txt", ".csv"} and size > byte_limit:
                excluded.append((str(relative), size))
                continue
            link_or_copy(source, target)

        workbook = source_root / SUPPORT_DIR / WORKBOOK_NAME
        if workbook.is_file():
            target = view / SUPPORT_DIR / WORKBOOK_NAME
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(workbook, target)
            copied += 1
        _emit_log(
            diagnostic_log,
            f"external-filter.view path={view} limit_kb={limit_kb} "
            f"excluded={len(excluded)} linked={linked} copied={copied}",
        )
        yield view, excluded
    finally:
        if view.exists():
            shutil.rmtree(view)

def prepare_uberwolf(ascii_dir: str | Path) -> Path:
    target_dir = Path(ascii_dir)
    if not str(target_dir).isascii():
        raise ValueError("UberWolf 执行目录必须是纯 ASCII 路径。")
    target_dir.mkdir(parents=True, exist_ok=True)
    override = os.environ.get("WOLFLATOR_UBERWOLF", "")
    source = Path(override) if override else verified_vendor_file("UberWolfCli.exe", "uberwolf")
    if not source.is_file():
        raise FileNotFoundError("未找到 UberWolfCli.exe。开发环境请运行 scripts/fetch_vendor.ps1。")
    target = target_dir / "UberWolfCli.exe"
    if not target.exists() or sha256_file(target) != sha256_file(source):
        with atomic_output_path(target) as temporary:
            shutil.copy2(source, temporary)
    return target

class UberWolfRunner:
    def __init__(self, executable: str | Path):
        self.executable = Path(executable)

    @staticmethod
    def _run_process(*args, **kwargs) -> ToolResult:
        return run_process(*args, **kwargs)

    def unpack(
        self,
        game_root: str | Path,
        *,
        cancel_event: threading.Event | None = None,
        log: Callable[[str], None] | None = None,
        diagnostic_log: Callable[[str], None] | None = None,
    ) -> ToolResult | None:
        root = Path(game_root)
        data = root / "Data"
        if (data / "BasicData" / "Game.dat").is_file():
            if log:
                log("检测到完整松散 Data，跳过 UberWolf。")
            return None
        game_exe = root / "Game.exe"
        if not game_exe.is_file():
            raise FileNotFoundError("工作副本中没有 Game.exe。")
        archives = sorted(
            (path for path in root.iterdir() if path.is_file() and path.suffix.casefold() == ".wolf"),
            key=lambda path: path.name.casefold(),
        )
        if not archives:
            raise RuntimeError("松散 Data 不完整，且没有可供 UberWolf 解包的 .wolf 文件。")
        had_loose_data = data.is_dir()

        # ponytail: UberWolf treats any Data directory as already unpacked, so use a
        # clean sibling view and overlay the game's loose files after extraction.
        sandbox = Path(tempfile.mkdtemp(prefix=".wolflator-uberwolf-", dir=root.parent))
        merged = root / f".wolflator-data-merged-{os.getpid()}-{time.time_ns()}"
        previous = root / f".wolflator-data-previous-{os.getpid()}-{time.time_ns()}"
        result: ToolResult | None = None
        try:
            shutil.copy2(game_exe, sandbox / game_exe.name)
            for archive in archives:
                shutil.copy2(archive, sandbox / archive.name)
            result = self._run_process(
                [str(self.executable), str(sandbox / game_exe.name)],
                cwd=self.executable.parent,
                cancel_event=cancel_event,
                log=log,
                diagnostic_log=diagnostic_log,
            )
            extracted = sandbox / "Data"
            if not (extracted / "BasicData" / "Game.dat").is_file():
                raise RuntimeError("UberWolf 返回成功，但没有生成 Data/BasicData/Game.dat。")
            shutil.copytree(extracted, merged)
            if had_loose_data:
                shutil.copytree(data, merged, dirs_exist_ok=True, copy_function=shutil.copy2)
            if data.exists():
                replace_with_retry(data, previous)
            try:
                replace_with_retry(merged, data)
            except Exception:
                if not data.exists() and previous.exists():
                    replace_with_retry(previous, data)
                raise
            if previous.exists():
                shutil.rmtree(previous)
            if diagnostic_log:
                diagnostic_log(
                    f"uberwolf.merge archives={len(archives)} loose_overlay={had_loose_data} "
                    f"data={data}"
                )
            return result
        finally:
            shutil.rmtree(sandbox, ignore_errors=True)
            if merged.exists():
                shutil.rmtree(merged, ignore_errors=True)
            if previous.exists() and data.exists():
                shutil.rmtree(previous, ignore_errors=True)

def _official_config_text(scope: ImportScope) -> str:
    include_external = "1" if scope.external else "0"
    include_names = "1" if scope.optional_name else "0"
    values = {
        "LastBackupFile": "",
        "LastDiffFile": "",
        "LastMakeTranslatedDir": "",
        "LastTargetLang": "1",
        **{f"NotTranslatedFlag{i}": "0" for i in range(1, 11)},
        "Original_Language": "1",
        "Tool_A_Get_CSV": include_external,
        "Tool_A_Get_CommonEvent_Name": include_names,
        "Tool_A_Get_DB_DataName": include_names,
        "Tool_A_Get_DB_ItemName": include_names,
        "Tool_A_Get_DB_TypeName": include_names,
        "Tool_A_Get_MapEvent_Name": include_names,
        "Tool_A_Get_TXT": include_external,
        "Tool_A_Include_CDB_Name": include_names,
        "Tool_A_Include_SDB_Name": include_names,
        "Tool_A_Include_UDB_Name": include_names,
        "Tool_A_Sort": "1",
        "Translated_Language_1": "4",
        **{f"Translated_Language_{i}": "0" for i in range(2, 11)},
    }
    return "[System]\r\n" + "".join(f"{key}={value}\r\n" for key, value in values.items())

def write_official_game_config(game_root: str | Path, scope: ImportScope) -> Path:
    support = Path(game_root) / SUPPORT_DIR
    support.mkdir(parents=True, exist_ok=True)
    path = support / GAME_CONFIG_NAME
    atomic_write_bytes(path, b"\xff\xfe" + _official_config_text(scope).encode("utf-16le"))
    return path

def prepare_official_tool(source_exe: str | Path, cache_root: str | Path) -> Path:
    source = Path(source_exe)
    lib = source.parent / "LibXL.dll"
    if not source.is_file() or not lib.is_file():
        raise FileNotFoundError("官方工具 EXE 或同目录 LibXL.dll 不存在。")
    fingerprint = sha256_file(source)[:16]
    target_dir = Path(cache_root) / fingerprint
    target_dir.mkdir(parents=True, exist_ok=True)
    target_exe = target_dir / source.name
    silent_executable = _silent_official_executable(source)
    if not target_exe.is_file() or target_exe.read_bytes() != silent_executable:
        with atomic_output_path(target_exe) as temporary:
            temporary.write_bytes(silent_executable)
            shutil.copystat(source, temporary)
    target_lib = target_dir / lib.name
    if not target_lib.exists() or sha256_file(lib) != sha256_file(target_lib):
        with atomic_output_path(target_lib) as temporary:
            shutil.copy2(lib, temporary)
    return target_exe

class OfficialToolRunner:
    def __init__(self, executable: str | Path, scope: ImportScope):
        self.executable = Path(executable)
        self.scope = scope
        self.diagnostics: list[dict[str, str]] = []
        self.console_outputs: list[dict[str, str]] = []

    @staticmethod
    def _run_process(*args, **kwargs) -> ToolResult:
        return run_process(*args, **kwargs)

    def run(
        self,
        mode: str,
        game_root: str | Path,
        *,
        language_index: int | None = None,
        cancel_event: threading.Event | None = None,
        log: Callable[[str], None] | None = None,
        diagnostic_log: Callable[[str], None] | None = None,
        warning: Callable[[str], None] | None = None,
    ) -> ToolResult:
        root = Path(game_root).resolve()
        write_official_game_config(root, self.scope)
        _emit_log(
            diagnostic_log,
            "official.sound_suppression method=import-redirection source=MessageBeep target=IsWindow",
        )
        slow_warning_callback = None
        if mode in {"EXTRACT", "UPDATE_EXCEL"} and (warning or log):
            sink = warning or log
            slow_warning_callback = lambda _elapsed: sink(
                f"官方工具 {mode} 已运行超过 5 分钟；"
                "请检查“自动排除大文件”是否启用，或适当降低大小上限。"
            )
        lib = self.executable.parent / "LibXL.dll"
        if not lib.is_file():
            raise FileNotFoundError(f"官方工具目录缺少 {lib.name}。")
        with tempfile.TemporaryDirectory(
            prefix="wolflator-official-", dir=self.executable.parent.parent
        ) as temporary_directory:
            isolated_dir = Path(temporary_directory)
            isolated_executable = isolated_dir / self.executable.name
            shutil.copy2(self.executable, isolated_executable)
            shutil.copy2(lib, isolated_dir / lib.name)
            _emit_log(diagnostic_log, f"official.instance isolated={isolated_dir}")
            command = [str(isolated_executable), "-mode", mode]
            if language_index is not None:
                command.append(str(language_index))
            command.extend(["-gamedata", str(root) + os.sep, "-mes_lang", "EN"])
            result = self._run_process(
                command,
                cwd=isolated_dir,
                cancel_event=cancel_event,
                log=log,
                diagnostic_log=diagnostic_log,
                hide_window=True,
                capture_console=True,
                slow_warning_after=(
                    300 if mode in {"EXTRACT", "UPDATE_EXCEL"} else None
                ),
                slow_warning=slow_warning_callback,
            )
        self.console_outputs.append(
            {
                "mode": mode,
                "timeline": "\n".join(result.console_history),
                "final": result.console_output,
            }
        )
        for diagnostic in parse_official_diagnostics(result.console_output):
            diagnostic["mode"] = mode
            self.diagnostics.append(diagnostic)
            _emit_log(
                diagnostic_log,
                "official.diagnostic " + json.dumps(diagnostic, ensure_ascii=False, sort_keys=True),
            )
        return result

    def extract(self, game_root: str | Path, **kwargs) -> Path:
        existing = Path(game_root) / SUPPORT_DIR / WORKBOOK_NAME
        if existing.is_file():
            backup = existing.with_suffix(".pre-extract.bak")
            backup.unlink(missing_ok=True)
            replace_with_retry(existing, backup)
        self.run("EXTRACT", game_root, **kwargs)
        try:
            return locate_workbook(game_root)
        except FileNotFoundError:
            console = self.console_outputs[-1]["final"] if self.console_outputs else ""
            diagnostics = parse_official_map_failures(console)
            if diagnostics:
                raise OfficialArtifactMissingError(
                    Path(game_root) / SUPPORT_DIR / WORKBOOK_NAME,
                    diagnostics,
                ) from None
            raise

    def update_excel(self, game_root: str | Path, **kwargs) -> Path:
        self.run("UPDATE_EXCEL", game_root, **kwargs)
        return locate_workbook(game_root)

    def translate(self, game_root: str | Path, **kwargs) -> Path:
        self.diagnostics.clear()
        self.console_outputs.clear()
        root = Path(game_root)
        for path in root.glob("Translated*_Chinese (Simplified)"):
            if path.is_dir():
                shutil.rmtree(path)
        self.run("CREATE_FOLDER", game_root, language_index=0, **kwargs)
        self.run("TRANSLATE", game_root, language_index=0, **kwargs)
        return locate_translated_game(game_root)

def locate_translated_game(game_root: str | Path) -> Path:
    candidates = []
    for path in Path(game_root).glob("Translated*_Chinese (Simplified)"):
        if (path / "Game.exe").is_file() and (path / "Data").is_dir():
            candidates.append(path)
    if len(candidates) != 1:
        raise FileNotFoundError(
            f"官方工具返回成功，但简体中文 Translated 目录数量为 {len(candidates)}。"
        )
    return candidates[0]
