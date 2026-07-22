from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Callable, Iterable

from openpyxl import load_workbook

from models import ImportCategory, ImportScope, ToolResult, TranslationItem


CODE_HEADER = "Code (No Change)"
FLAG_HEADER = "Flag (No Change)"
TYPE_HEADER = "Type"
INFO_HEADER = "Info"
ORIGINAL_HEADER = "Original text (No Change)"
TARGET_PREFIX = "Translated text 1 /"
EXPECTED_TARGET = "Chinese (Simplified)"
SUPPORT_DIR = "WOLF_Translation_Support_Tool_Data"
WORKBOOK_NAME = "WOLF_Translation_Text.xlsx"
GAME_CONFIG_NAME = "WOLF_Translation_Game_Config.ini"
ITEMS_SCHEMA = 1
PUA_START = 0xE100
PUA_END = 0xF7FF
SPECIAL_ESCAPES = set("!.|^<>${}\\")
COPY_FROM_RE = re.compile(r"(?:^|\r?\n)COPY-FROM-([^\r\n]+)", re.IGNORECASE)
SIMPLIFIED_CHINESE_FONTS = {
    "BASICDATA-3": "KaiTi",
    "BASICDATA-4": "Microsoft YaHei",
    "BASICDATA-5": "Microsoft YaHei",
    "BASICDATA-6": "Microsoft YaHei",
}


def full_export_scope() -> ImportScope:
    return ImportScope(display=True, external=True, optional_name=True, halfwidth=True, filename=True)


def name_baseline_scope() -> ImportScope:
    return ImportScope(display=True, external=True, optional_name=False, halfwidth=True, filename=True)


class CancelledError(RuntimeError):
    pass


def resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def verified_vendor_file(filename: str, component: str, hash_field: str = "sha256") -> Path:
    manifest_path = resource_path("vendor/manifest.json")
    target = resource_path(f"vendor/{filename}")
    if not manifest_path.is_file() or not target.is_file():
        raise FileNotFoundError(f"发行资源缺少 {filename} 或 vendor/manifest.json。")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = str(manifest.get(component, {}).get(hash_field, "")).lower()
    actual = sha256_file(target).lower()
    if not expected or actual != expected:
        raise ValueError(f"{filename} SHA-256 不匹配: {actual}")
    return target


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def hash_directory(path: str | Path) -> str:
    root = Path(path).resolve()
    digest = hashlib.sha256()
    for item in sorted((p for p in root.rglob("*") if p.is_file()), key=lambda p: p.as_posix()):
        relative = item.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8", "surrogatepass"))
        digest.update(b"\0")
        digest.update(str(item.stat().st_size).encode("ascii"))
        digest.update(b"\0")
        with item.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _kill_process_tree(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    else:
        process.kill()


def _emit_log(sink: Callable[[str], None] | None, message: str) -> None:
    if not sink:
        return
    try:
        sink(message)
    except UnicodeEncodeError:
        # ponytail: Preserve process execution on narrow consoles; UTF-8 file sinks keep the original text.
        sink(message.encode("ascii", errors="backslashreplace").decode("ascii"))


def run_process(
    command: list[str],
    *,
    cwd: str | Path | None = None,
    timeout: int = 3600,
    cancel_event: threading.Event | None = None,
    log: Callable[[str], None] | None = None,
    diagnostic_log: Callable[[str], None] | None = None,
    env: dict[str, str] | None = None,
) -> ToolResult:
    detail = diagnostic_log or log
    safe_command = " ".join(f'"{arg}"' if " " in arg else arg for arg in command)
    _emit_log(log, f"> {safe_command}")
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        bufsize=1,
    )
    _emit_log(
        detail,
        f"process.start pid={process.pid} cwd={Path(cwd).resolve() if cwd else Path.cwd()} "
        f"timeout={timeout}s command={safe_command}",
    )
    output_queue: queue.Queue[tuple[str, str | None]] = queue.Queue()
    captured: dict[str, list[str]] = {"stdout": [], "stderr": []}

    def read_stream(name: str, stream) -> None:
        try:
            for line in stream:
                output_queue.put((name, line.rstrip("\r\n")))
        finally:
            output_queue.put((name, None))

    readers = [
        threading.Thread(target=read_stream, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=read_stream, args=("stderr", process.stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()
    finished_streams: set[str] = set()
    try:
        while process.poll() is None or len(finished_streams) < len(readers):
            if cancel_event and cancel_event.is_set():
                _emit_log(
                    detail,
                    f"process.cancel pid={process.pid} elapsed={time.monotonic() - started:.3f}s",
                )
                _kill_process_tree(process)
                raise CancelledError("任务已取消。")
            elapsed = time.monotonic() - started
            if elapsed > timeout:
                _emit_log(
                    detail,
                    f"process.timeout pid={process.pid} elapsed={elapsed:.3f}s limit={timeout}s",
                )
                _kill_process_tree(process)
                raise TimeoutError(f"外部工具运行超过 {timeout} 秒。")
            try:
                name, line = output_queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                finished_streams.add(name)
                continue
            captured[name].append(line)
            _emit_log(detail, f"process.{name} pid={process.pid} {line}")
    finally:
        if process.poll() is None:
            _kill_process_tree(process)
        process.wait()
        for reader in readers:
            reader.join(timeout=1)
        for stream in (process.stdout, process.stderr):
            if stream and not stream.closed:
                stream.close()
    stdout = "\n".join(captured["stdout"])
    stderr = "\n".join(captured["stderr"])
    result = ToolResult(command, process.returncode or 0, stdout, stderr, time.monotonic() - started)
    _emit_log(
        detail,
        f"process.exit pid={process.pid} code={result.return_code} duration={result.duration_seconds:.3f}s "
        f"stdout_lines={len(captured['stdout'])} stderr_lines={len(captured['stderr'])}",
    )
    if result.return_code != 0:
        error_detail = (stderr or stdout).strip()[-2000:]
        raise RuntimeError(f"外部工具退出码 {result.return_code}: {error_detail}")
    _emit_log(log, f"外部工具完成，耗时 {result.duration_seconds:.1f} 秒。")
    return result


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
        shutil.copy2(source, target)
    return target


class UberWolfRunner:
    def __init__(self, executable: str | Path):
        self.executable = Path(executable)

    def unpack(
        self,
        game_root: str | Path,
        *,
        cancel_event: threading.Event | None = None,
        log: Callable[[str], None] | None = None,
        diagnostic_log: Callable[[str], None] | None = None,
    ) -> ToolResult | None:
        root = Path(game_root)
        if (root / "Data" / "BasicData" / "Game.dat").is_file() and not next(root.rglob("*.wolf"), None):
            if log:
                log("检测到完整松散 Data，跳过 UberWolf。")
            return None
        game_exe = root / "Game.exe"
        if not game_exe.is_file():
            raise FileNotFoundError("工作副本中没有 Game.exe。")
        result = run_process(
            [str(self.executable), str(game_exe)],
            cwd=self.executable.parent,
            cancel_event=cancel_event,
            log=log,
            diagnostic_log=diagnostic_log,
        )
        if not (root / "Data" / "BasicData" / "Game.dat").is_file():
            raise RuntimeError("UberWolf 返回成功，但没有生成 Data/BasicData/Game.dat。")
        return result


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
    path.write_bytes(b"\xff\xfe" + _official_config_text(scope).encode("utf-16le"))
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
    for source_file, target_file in ((source, target_exe), (lib, target_dir / lib.name)):
        if not target_file.exists() or sha256_file(source_file) != sha256_file(target_file):
            shutil.copy2(source_file, target_file)
    return target_exe


class OfficialToolRunner:
    def __init__(self, executable: str | Path, scope: ImportScope):
        self.executable = Path(executable)
        self.scope = scope

    def run(
        self,
        mode: str,
        game_root: str | Path,
        *,
        language_index: int | None = None,
        cancel_event: threading.Event | None = None,
        log: Callable[[str], None] | None = None,
        diagnostic_log: Callable[[str], None] | None = None,
    ) -> ToolResult:
        root = Path(game_root).resolve()
        write_official_game_config(root, self.scope)
        command = [
            str(self.executable),
            "-mode",
            mode,
        ]
        if language_index is not None:
            command.append(str(language_index))
        command.extend(["-gamedata", str(root) + os.sep, "-mes_lang", "EN"])
        return run_process(
            command,
            cwd=self.executable.parent,
            cancel_event=cancel_event,
            log=log,
            diagnostic_log=diagnostic_log,
        )

    def extract(self, game_root: str | Path, **kwargs) -> Path:
        existing = Path(game_root) / SUPPORT_DIR / WORKBOOK_NAME
        if existing.is_file():
            backup = existing.with_suffix(".pre-extract.bak")
            backup.unlink(missing_ok=True)
            os.replace(existing, backup)
        self.run("EXTRACT", game_root, **kwargs)
        return locate_workbook(game_root)

    def update_excel(self, game_root: str | Path, **kwargs) -> Path:
        self.run("UPDATE_EXCEL", game_root, **kwargs)
        return locate_workbook(game_root)

    def translate(self, game_root: str | Path, **kwargs) -> Path:
        root = Path(game_root)
        for path in root.glob("Translated*_Chinese (Simplified)"):
            if path.is_dir():
                shutil.rmtree(path)
        self.run("CREATE_FOLDER", game_root, language_index=0, **kwargs)
        self.run("TRANSLATE", game_root, language_index=0, **kwargs)
        return locate_translated_game(game_root)


def _header_map(worksheet) -> tuple[int, dict[str, int]]:
    for row_index, row in enumerate(worksheet.iter_rows(min_row=1, max_row=20, values_only=True), start=1):
        if CODE_HEADER in row and ORIGINAL_HEADER in row:
            mapping = {str(value): index + 1 for index, value in enumerate(row) if value is not None}
            missing = {CODE_HEADER, FLAG_HEADER, TYPE_HEADER, INFO_HEADER, ORIGINAL_HEADER} - mapping.keys()
            if missing:
                raise ValueError(f"官方工作簿缺少列: {', '.join(sorted(missing))}")
            targets = [name for name in mapping if name.startswith(TARGET_PREFIX)]
            if not targets or EXPECTED_TARGET not in targets[0]:
                raise ValueError("官方工作簿第一译文列不是简体中文。")
            mapping["__target__"] = mapping[targets[0]]
            return row_index, mapping
    raise ValueError("不是受支持的 WOLF Translation Support Tool 工作簿。")


def locate_workbook(game_root: str | Path) -> Path:
    support = Path(game_root) / SUPPORT_DIR
    workbook_path = support / WORKBOOK_NAME
    if not workbook_path.is_file():
        raise FileNotFoundError(f"官方工具没有生成 {workbook_path}。")
    workbook = load_workbook(workbook_path, read_only=False, data_only=False)
    try:
        _header_map(workbook.active)
    finally:
        workbook.close()
    return workbook_path


def _content_category(code: str, flag: str, type_name: str) -> ImportCategory:
    upper_flag = flag.upper()
    upper_code = code.upper()
    upper_type = type_name.upper()
    if "<FILENAME>" in upper_flag:
        return ImportCategory.FILENAME
    if "<HALF-WIDTH CHARACTERS ONLY>" in upper_flag:
        return ImportCategory.HALFWIDTH
    if upper_code.startswith("NAME-") or upper_code.endswith("-NAME"):
        return ImportCategory.OPTIONAL_NAME
    if upper_code.startswith(("TXT-", "CSV-")) or "TXT" in upper_type or "CSV" in upper_type:
        return ImportCategory.EXTERNAL
    return ImportCategory.DISPLAY


def _category(code: str, flag: str, type_name: str) -> ImportCategory:
    if "COPY-FROM-" in flag.upper():
        return ImportCategory.COPY
    return _content_category(code, flag, type_name)


def stable_key(code: str, flag: str, original: str, ordinal: int) -> str:
    payload = "\0".join((code, flag, original, str(ordinal))).encode("utf-8", "surrogatepass")
    return hashlib.sha256(payload).hexdigest()


def _iter_data_rows(worksheet) -> Iterable[tuple[int, dict[str, str], int]]:
    header_row, headers = _header_map(worksheet)
    counts: Counter[tuple[str, str, str]] = Counter()
    for row_index, row in enumerate(
        worksheet.iter_rows(min_row=header_row + 1, values_only=True),
        start=header_row + 1,
    ):
        def value(column: str):
            index = headers[column] - 1
            return row[index] if index < len(row) else None

        original = value(ORIGINAL_HEADER)
        if original is None:
            continue
        values = {
            "code": str(value(CODE_HEADER) or ""),
            "flag": str(value(FLAG_HEADER) or ""),
            "type": str(value(TYPE_HEADER) or ""),
            "info": str(value(INFO_HEADER) or ""),
            "original": str(original),
            "translation": str(value("__target__") or ""),
        }
        identity = (values["code"], values["flag"], values["original"])
        counts[identity] += 1
        yield row_index, values, counts[identity]


def _scan_control_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    index = 0
    while index < len(text):
        if text[index] != "\\":
            index += 1
            continue
        start = index
        index += 1
        if index >= len(text):
            tokens.append("\\")
            break
        char = text[index]
        if char in SPECIAL_ESCAPES:
            index += 1
        elif char.isascii() and (char.isalnum() or char == "_"):
            index += 1
            while index < len(text) and text[index].isascii() and (text[index].isalnum() or text[index] == "_"):
                index += 1
            while index < len(text) and text[index] == "[":
                depth = 0
                while index < len(text):
                    if text[index] == "[":
                        depth += 1
                    elif text[index] == "]":
                        depth -= 1
                        if depth == 0:
                            index += 1
                            break
                    elif text[index] in "\r\n":
                        break
                    index += 1
        else:
            # ponytail: Unknown backslash forms protect only the slash; upgrade the scanner if WOLF documents more syntax.
            index = start + 1
        tokens.append(text[start:index])
    return tokens


def protect_control_tokens(text: str) -> tuple[str, list[str]]:
    tokens = _scan_control_tokens(text)
    if not tokens:
        return text, []
    cursor = 0
    output: list[str] = []
    for offset, token in enumerate(tokens):
        codepoint = PUA_START + offset
        if codepoint > PUA_END:
            raise ValueError("单条文本的控制符数量超过占位符容量。")
        start = text.find(token, cursor)
        output.append(text[cursor:start])
        output.append(chr(codepoint))
        cursor = start + len(token)
    output.append(text[cursor:])
    return "".join(output), tokens


def restore_control_tokens(text: str, tokens: list[str]) -> str:
    expected = [chr(PUA_START + index) for index in range(len(tokens))]
    actual = [char for char in text if PUA_START <= ord(char) <= PUA_END]
    if actual != expected:
        raise ValueError(
            "控制符占位序列不一致: "
            f"expected={[f'U+{ord(c):04X}' for c in expected]}, "
            f"actual={[f'U+{ord(c):04X}' for c in actual]}"
        )
    restored = text
    for placeholder, token in zip(expected, tokens):
        restored = restored.replace(placeholder, token, 1)
    if _scan_control_tokens(restored) != tokens:
        raise ValueError("译文控制符序列与原文不一致。")
    return restored


def read_translation_items(workbook_path: str | Path) -> list[TranslationItem]:
    # Normal mode releases the underlying ZIP deterministically on Windows;
    # read-only iterators can retain the workbook handle until garbage collection.
    workbook = load_workbook(workbook_path, read_only=False, data_only=False)
    worksheet = workbook.active
    items: list[TranslationItem] = []
    for _row, values, ordinal in _iter_data_rows(worksheet):
        signature = _scan_control_tokens(values["original"])
        category = _category(values["code"], values["flag"], values["type"])
        copy_category = (
            _content_category(values["code"], values["flag"], values["type"])
            if category is ImportCategory.COPY
            else None
        )
        items.append(
            TranslationItem(
                key=stable_key(values["code"], values["flag"], values["original"], ordinal),
                original=values["original"],
                translation=values["translation"],
                context=" | ".join(
                    part for part in (values["type"], values["info"], f"Code={values['code']}", values["flag"])
                    if part
                ),
                stage=1 if values["translation"] else 0,
                code=values["code"],
                flag=values["flag"],
                type=values["type"],
                info=values["info"],
                category=category,
                copy_category=copy_category,
                control_signature=signature,
            )
        )
    workbook.close()
    return items


def is_managed_translation(item: TranslationItem) -> bool:
    return item.code.upper() in SIMPLIFIED_CHINESE_FONTS


def apply_managed_translations(items: list[TranslationItem]) -> None:
    for item in items:
        translation = SIMPLIFIED_CHINESE_FONTS.get(item.code.upper())
        if translation:
            item.translation = translation
            item.stage = 1


def _location_identities(items: list[TranslationItem]) -> list[tuple[str, str, int]]:
    counts: Counter[tuple[str, str]] = Counter()
    identities: list[tuple[str, str, int]] = []
    for item in items:
        identity = (item.code, item.original)
        counts[identity] += 1
        identities.append((item.code, item.original, counts[identity]))
    return identities


def classify_optional_name_delta(
    full_items: list[TranslationItem],
    baseline_items: list[TranslationItem],
) -> int:
    full_identities = _location_identities(full_items)
    full_set = set(full_identities)
    baseline_set = set(_location_identities(baseline_items))
    missing_from_full = baseline_set - full_set
    if missing_from_full:
        raise ValueError(f"基准导出包含全量导出中不存在的行，共 {len(missing_from_full)} 条。")

    optional_count = 0
    for item, identity in zip(full_items, full_identities):
        if identity not in baseline_set:
            if item.category is ImportCategory.COPY:
                item.copy_category = ImportCategory.OPTIONAL_NAME
            else:
                item.category = ImportCategory.OPTIONAL_NAME
            optional_count += 1
        elif item.category is ImportCategory.OPTIONAL_NAME:
            item.category = ImportCategory.DISPLAY
        elif item.category is ImportCategory.COPY and item.copy_category is ImportCategory.OPTIONAL_NAME:
            item.copy_category = ImportCategory.DISPLAY
    return optional_count


def _copy_source(item: TranslationItem, by_code: dict[str, list[TranslationItem]]) -> TranslationItem:
    current = item
    visited: set[str] = set()
    while current.category is ImportCategory.COPY:
        match = COPY_FROM_RE.search(current.flag)
        if not match:
            raise ValueError(f"COPY-FROM 行缺少来源代码: {current.code}")
        source_code = match.group(1)
        marker = f"{current.code}\0{source_code}\0{current.original}"
        if marker in visited:
            raise ValueError(f"COPY-FROM 出现循环引用: {current.code}")
        visited.add(marker)
        candidates = by_code.get(source_code, [])
        exact = [candidate for candidate in candidates if candidate.original == current.original]
        if len(exact) == 1:
            current = exact[0]
        else:
            raise ValueError(f"COPY-FROM 找不到唯一来源: {current.code} -> {source_code}")
    return current


def selected_translation_requirements(
    items: list[TranslationItem],
    scope: ImportScope,
) -> dict[str, set[ImportCategory]]:
    by_code: dict[str, list[TranslationItem]] = {}
    for item in items:
        by_code.setdefault(item.code, []).append(item)

    groups: dict[str, set[ImportCategory]] = {}
    sources: dict[str, TranslationItem] = {}
    for item in items:
        category = item.copy_category if item.category is ImportCategory.COPY else item.category
        if category is None:
            continue
        source = _copy_source(item, by_code) if item.category is ImportCategory.COPY else item
        sources[source.key] = source
        groups.setdefault(source.key, set()).add(category)

    requirements: dict[str, set[ImportCategory]] = {}
    for key, categories in groups.items():
        if is_managed_translation(sources[key]):
            requirements[key] = categories
            continue
        # ponytail: COPY-FROM cannot give duplicate uses different translations. Keep the
        # whole group untouched until dangerous filename/half-width uses are explicitly enabled.
        dangerous = {ImportCategory.FILENAME, ImportCategory.HALFWIDTH} & categories
        if any(not scope.allows(category) for category in dangerous):
            continue
        if any(scope.allows(category) for category in categories):
            requirements[key] = categories
    return requirements


def selected_translation_items(
    items: list[TranslationItem],
    scope: ImportScope,
) -> list[TranslationItem]:
    required = selected_translation_requirements(items, scope)
    return [item for item in items if item.key in required and not is_managed_translation(item)]


def to_paratranz(
    items: list[TranslationItem],
    scope: ImportScope,
) -> list[dict[str, object]]:
    output: list[dict[str, object]] = []
    for item in selected_translation_items(items, scope):
        protected, tokens = protect_control_tokens(item.original)
        if tokens != item.control_signature:
            raise ValueError(f"控制符签名发生变化: {item.code}")
        translation = ""
        if item.translation:
            protected_translation, translated_tokens = protect_control_tokens(item.translation)
            if translated_tokens == tokens:
                translation = protected_translation
        output.append(
            {
                "key": item.key,
                "original": protected,
                "translation": translation,
                "context": item.context,
                "stage": 1 if translation else 0,
            }
        )
    return output


def merge_ainiee_output(
    items: list[TranslationItem],
    translated: list[dict[str, object]],
    scope: ImportScope,
) -> list[TranslationItem]:
    expected = {item.key: item for item in selected_translation_items(items, scope)}
    actual: dict[str, dict[str, object]] = {}
    for row in translated:
        key = str(row.get("key", ""))
        if not key or key in actual:
            raise ValueError(f"AiNiee 输出包含空键或重复键: {key!r}")
        actual[key] = row
    missing = set(expected) - set(actual)
    extra = set(actual) - set(expected)
    if missing or extra:
        raise ValueError(f"AiNiee 输出键集合不一致: missing={len(missing)}, extra={len(extra)}")
    for key, item in expected.items():
        raw = str(actual[key].get("translation", ""))
        if not raw.strip():
            raise ValueError(f"AiNiee 没有生成译文: {item.code} / {item.original[:80]}")
        item.translation = restore_control_tokens(raw, item.control_signature)
        item.stage = 1
    for item in items:
        if item.category is ImportCategory.COPY:
            item.translation = ""
    return items


def reconcile_incremental(
    previous: list[TranslationItem],
    current: list[TranslationItem],
) -> tuple[list[TranslationItem], list[dict[str, object]]]:
    previous_by_key = {item.key: item.translation for item in previous if item.translation}
    previous_by_original: dict[str, set[str]] = {}
    for item in previous:
        if item.translation and item.category is not ImportCategory.COPY:
            previous_by_original.setdefault(item.original, set()).add(item.translation)
    conflicts: list[dict[str, object]] = []
    for item in current:
        if item.category is ImportCategory.COPY:
            item.translation = ""
            continue
        exact = previous_by_key.get(item.key)
        if exact:
            item.translation = exact
            item.stage = 1
            continue
        candidates = sorted(previous_by_original.get(item.original, set()))
        if len(candidates) == 1:
            item.translation = candidates[0]
            item.stage = 1
        elif len(candidates) > 1:
            # ponytail: Ambiguous moved duplicates are retranslated instead of guessing; a future review UI may map candidates.
            item.translation = ""
            item.stage = 0
            conflicts.append(
                {
                    "key": item.key,
                    "code": item.code,
                    "original": item.original,
                    "candidates": candidates,
                }
            )
    return current, conflicts


def _save_workbook_atomic(workbook, output_path: str | Path) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    workbook.save(temporary)
    os.replace(temporary, output)
    return output


def write_full_workbook(
    template_path: str | Path,
    output_path: str | Path,
    items: list[TranslationItem],
) -> Path:
    translations = {item.key: item.translation for item in items}
    workbook = load_workbook(template_path)
    worksheet = workbook.active
    _header_row, headers = _header_map(worksheet)
    for row_index, values, ordinal in _iter_data_rows(worksheet):
        key = stable_key(values["code"], values["flag"], values["original"], ordinal)
        category = _category(values["code"], values["flag"], values["type"])
        worksheet.cell(row_index, headers["__target__"]).value = "" if category is ImportCategory.COPY else translations.get(key, "")
    return _save_workbook_atomic(workbook, output_path)


def _filename_target_exists(game_root: Path, translated_name: str) -> bool:
    name = translated_name.strip().replace("\\", "/").lstrip("/")
    if not name or ".." in Path(name).parts:
        return False
    data_root = game_root / "Data"
    direct = data_root.joinpath(*name.split("/"))
    return direct.is_file()


def write_scoped_workbook(
    full_path: str | Path,
    output_path: str | Path,
    scope: ImportScope,
    game_root: str | Path,
    items: list[TranslationItem],
) -> Path:
    workbook = load_workbook(full_path)
    worksheet = workbook.active
    _header_row, headers = _header_map(worksheet)
    requirements = selected_translation_requirements(items, scope)
    missing_filenames: list[str] = []
    for row_index, values, ordinal in _iter_data_rows(worksheet):
        key = stable_key(values["code"], values["flag"], values["original"], ordinal)
        category = _category(values["code"], values["flag"], values["type"])
        cell = worksheet.cell(row_index, headers["__target__"])
        required_categories = requirements.get(key, set())
        keep = bool(required_categories)
        if category is ImportCategory.COPY or not keep:
            cell.value = ""
            continue
        if ImportCategory.FILENAME in required_categories and cell.value:
            if not _filename_target_exists(Path(game_root), str(cell.value)):
                missing_filenames.append(str(cell.value))
    if missing_filenames:
        sample = ", ".join(missing_filenames[:5])
        raise ValueError(f"文件名译文没有对应真实文件，共 {len(missing_filenames)} 项，例如: {sample}")
    return _save_workbook_atomic(workbook, output_path)


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


def dump_items(path: str | Path, items: list[TranslationItem]) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(
        json.dumps(
            {"schema": ITEMS_SCHEMA, "items": [item.to_dict() for item in items]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    os.replace(temporary, output)
    return output


def load_items(path: str | Path) -> list[TranslationItem]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"schema", "items"}:
        raise ValueError("翻译条目文件结构不匹配。")
    if data["schema"] != ITEMS_SCHEMA:
        raise ValueError(f"不支持的翻译条目 schema: {data['schema']}")
    if not isinstance(data["items"], list):
        raise ValueError("翻译条目 items 不是数组。")
    return [TranslationItem.from_dict(item) for item in data["items"]]
