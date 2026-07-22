from __future__ import annotations

import functools
import hashlib
import json
import os
import shutil
import struct
import sys
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable

from safe_io import atomic_output_path, atomic_write_json, project_lock, read_text_with_retry


FONT_CODES = ("BASICDATA-3", "BASICDATA-4", "BASICDATA-5", "BASICDATA-6")
FONT_SLOT_NAMES = ("主字体", "副字体 1", "副字体 2", "副字体 3")
FONT_SCHEME_SCHEMA = 1
ORIGINAL_FONTS_SCHEMA = 1
BUNDLED_FONT_ID = "fusion-pixel-12px-proportional-zh_hans.ttf"
BUNDLED_FONT_FAMILY = "Fusion Pixel 12px Prop zh_hans"
BUNDLED_FONT_SHA256 = "5b27e9eb9d9dd93cff727d8919ddd2e7a482b19314b62991cb1e7806852e8734"
FONT_EXTENSIONS = {".ttf", ".otf", ".ttc"}


class FontError(ValueError):
    pass


@dataclass(frozen=True)
class FontCandidate:
    source: str
    family: str
    aliases: tuple[str, ...]
    files: tuple[Path, ...]
    missing: frozenset[str] = frozenset()

    @property
    def label(self) -> str:
        source = {"bundled": "随附", "game": "游戏", "system": "系统"}.get(self.source, self.source)
        return f"[{source}] {self.family}"


def _sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resource_path(relative: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative


def bundled_font_path() -> Path:
    path = _resource_path(f"vendor/fonts/FusionPixel/{BUNDLED_FONT_ID}")
    if not path.is_file():
        raise FileNotFoundError(f"发行资源缺少随附字体: {path}")
    actual = _sha256_file(path)
    if actual != BUNDLED_FONT_SHA256:
        raise FontError(f"随附字体 SHA-256 不匹配: {actual}")
    return path


def default_font_scheme() -> dict[str, object]:
    selection = {
        "mode": "font",
        "family": BUNDLED_FONT_FAMILY,
        "provenance": "bundled",
        "files": [
            {
                "kind": "bundled",
                "id": BUNDLED_FONT_ID,
                "filename": BUNDLED_FONT_ID,
                "sha256": BUNDLED_FONT_SHA256,
            }
        ],
    }
    return {
        "schema": FONT_SCHEME_SCHEMA,
        "origin": "default",
        "slots": [dict(selection) for _ in FONT_CODES],
        "coverage_ack": None,
    }


def font_scheme_path(project_dir: str | Path) -> Path:
    return Path(project_dir) / "font.json"


def original_fonts_path(project_dir: str | Path, version_id: str) -> Path:
    if not version_id or Path(version_id).name != version_id:
        raise FontError("字体原始记录的版本 ID 无效")
    versions = (Path(project_dir) / "versions").resolve()
    path = (versions / version_id / "original-fonts.json").resolve()
    if os.path.commonpath([str(versions), str(path)]) != str(versions):
        raise FontError("字体原始记录路径越界")
    return path


def _validate_original_fonts(value: object, version_id: str) -> dict[str, object]:
    expected = {"schema", "version_id", "source_hash", "workbook_sha256", "slots"}
    if not isinstance(value, dict) or set(value) != expected:
        raise FontError("original-fonts.json 字段不匹配")
    if value.get("schema") != ORIGINAL_FONTS_SCHEMA or value.get("version_id") != version_id:
        raise FontError("字体原始记录版本不匹配")
    source_hash = value.get("source_hash")
    workbook_hash = value.get("workbook_sha256")
    if not isinstance(source_hash, str) or (
        source_hash
        and (
            len(source_hash) != 64
            or any(character not in "0123456789abcdef" for character in source_hash.lower())
        )
    ):
        raise FontError("字体原始记录的源哈希无效")
    if (
        not isinstance(workbook_hash, str)
        or len(workbook_hash) != 64
        or any(character not in "0123456789abcdef" for character in workbook_hash.lower())
    ):
        raise FontError("字体原始记录的工作簿哈希无效")
    slots = value.get("slots")
    if (
        not isinstance(slots, list)
        or len(slots) != len(FONT_CODES)
        or not all(isinstance(slot, str) for slot in slots)
        or not slots[0].strip()
    ):
        raise FontError("字体原始记录必须包含四个槽位且主字体不能为空")
    return {
        "schema": ORIGINAL_FONTS_SCHEMA,
        "version_id": version_id,
        "source_hash": source_hash.lower(),
        "workbook_sha256": workbook_hash.lower(),
        "slots": list(slots),
    }


def load_original_fonts(project_dir: str | Path, version_id: str) -> dict[str, object] | None:
    path = original_fonts_path(project_dir, version_id)
    if not path.is_file():
        return None
    try:
        value = json.loads(read_text_with_retry(path, encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise FontError(f"无法读取字体原始记录: {error}") from error
    return _validate_original_fonts(value, version_id)


def record_original_fonts(
    project_dir: str | Path,
    version_id: str,
    slots: Iterable[str],
    source_hash: str,
    workbook_path: str | Path,
) -> Path:
    with project_lock(project_dir, "record-original-fonts"):
        values = list(slots)
        record = _validate_original_fonts(
            {
                "schema": ORIGINAL_FONTS_SCHEMA,
                "version_id": version_id,
                "source_hash": source_hash,
                "workbook_sha256": _sha256_file(workbook_path),
                "slots": values,
            },
            version_id,
        )
        path = original_fonts_path(project_dir, version_id)
        existing = load_original_fonts(project_dir, version_id)
        if existing is not None:
            if existing["slots"] != record["slots"] or (
                existing["source_hash"]
                and record["source_hash"]
                and existing["source_hash"] != record["source_hash"]
            ):
                raise FontError("同一源版本的原字体记录发生变化，请新建游戏版本")
            return path
        atomic_write_json(path, record)
        return path


def _safe_project_file(project_dir: Path, relative: str) -> Path:
    if not relative or Path(relative).is_absolute():
        raise FontError("字体资产路径必须是项目内相对路径")
    root = project_dir.resolve()
    path = (root / relative).resolve()
    if os.path.commonpath([str(root), str(path)]) != str(root):
        raise FontError(f"字体资产路径越界: {relative}")
    return path


def _validate_file_record(project_dir: Path, record: object, *, check_files: bool) -> dict[str, str]:
    if not isinstance(record, dict):
        raise FontError("字体文件记录不是对象")
    expected = {"kind", "filename", "sha256"}
    kind = record.get("kind")
    if kind == "bundled":
        expected.add("id")
    elif kind == "project":
        expected.add("path")
    else:
        raise FontError(f"未知字体文件来源: {kind}")
    if set(record) != expected or not all(isinstance(record.get(key), str) for key in expected):
        raise FontError("字体文件记录字段不匹配")
    filename = str(record["filename"])
    digest = str(record["sha256"]).lower()
    if (
        Path(filename).name != filename
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise FontError("字体文件名或 SHA-256 无效")
    if kind == "bundled":
        if record["id"] != BUNDLED_FONT_ID or filename != BUNDLED_FONT_ID:
            raise FontError(f"未知随附字体资产: {record['id']}")
        path = bundled_font_path()
    else:
        path = _safe_project_file(project_dir, str(record["path"]))
    if check_files:
        if not path.is_file():
            raise FileNotFoundError(f"字体文件不存在: {path}")
        actual = _sha256_file(path)
        if actual != digest:
            raise FontError(f"字体文件 SHA-256 已变化: {path}")
    return {key: str(record[key]) for key in expected}


def validate_font_scheme(
    project_dir: str | Path,
    value: object,
    *,
    check_files: bool = True,
) -> dict[str, object]:
    root = Path(project_dir)
    if not isinstance(value, dict) or set(value) != {"schema", "origin", "slots", "coverage_ack"}:
        raise FontError("font.json 字段不匹配")
    if value.get("schema") != FONT_SCHEME_SCHEMA:
        raise FontError(f"不支持的字体方案 schema: {value.get('schema')}")
    if value.get("origin") not in {"default", "user"}:
        raise FontError("字体方案 origin 无效")
    slots = value.get("slots")
    if not isinstance(slots, list) or len(slots) != len(FONT_CODES):
        raise FontError("字体方案必须包含四个槽位")
    validated_slots: list[dict[str, object]] = []
    for index, slot in enumerate(slots):
        if not isinstance(slot, dict) or slot.get("mode") not in {"keep", "font"}:
            raise FontError(f"字体槽位 {index + 1} 无效")
        if slot["mode"] == "keep":
            if set(slot) != {"mode"}:
                raise FontError(f"保持当前字体槽位 {index + 1} 含多余字段")
            validated_slots.append({"mode": "keep"})
            continue
        if set(slot) != {"mode", "family", "provenance", "files"}:
            raise FontError(f"字体槽位 {index + 1} 字段不匹配")
        family = slot.get("family")
        provenance = slot.get("provenance")
        files = slot.get("files")
        if not isinstance(family, str) or not family.strip():
            raise FontError(f"字体槽位 {index + 1} 缺少字体族名")
        if provenance not in {"bundled", "game", "system"}:
            raise FontError(f"字体槽位 {index + 1} 来源无效")
        if not isinstance(files, list) or not files:
            raise FontError(f"字体槽位 {index + 1} 没有字体文件")
        validated_slots.append(
            {
                "mode": "font",
                "family": family.strip(),
                "provenance": provenance,
                "files": [
                    _validate_file_record(root, record, check_files=check_files) for record in files
                ],
            }
        )
    acknowledgement = value.get("coverage_ack")
    if acknowledgement is not None:
        if (
            not isinstance(acknowledgement, dict)
            or set(acknowledgement) != {"fingerprint", "missing_count"}
            or not isinstance(acknowledgement.get("fingerprint"), str)
            or type(acknowledgement.get("missing_count")) is not int
            or acknowledgement["missing_count"] < 1
        ):
            raise FontError("字体覆盖确认记录无效")
        acknowledgement = dict(acknowledgement)
    return {
        "schema": FONT_SCHEME_SCHEMA,
        "origin": value["origin"],
        "slots": validated_slots,
        "coverage_ack": acknowledgement,
    }


def load_font_scheme(project_dir: str | Path, *, check_files: bool = True) -> dict[str, object] | None:
    path = font_scheme_path(project_dir)
    if not path.is_file():
        return None
    try:
        value = json.loads(read_text_with_retry(path, encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise FontError(f"无法读取字体方案: {error}") from error
    return validate_font_scheme(project_dir, value, check_files=check_files)


def save_font_scheme(project_dir: str | Path, value: object) -> Path:
    root = Path(project_dir)
    with project_lock(root, "save-font-scheme"):
        scheme = validate_font_scheme(root, value, check_files=True)
        path = font_scheme_path(root)
        atomic_write_json(path, scheme)
        return path


def scheme_hash(scheme: dict[str, object] | None) -> str:
    if scheme is None:
        return ""
    payload = dict(scheme)
    payload["coverage_ack"] = None
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def coverage_fingerprint(required: Iterable[str], scheme: dict[str, object]) -> str:
    payload = {
        "characters": sorted(set(required), key=ord),
        "scheme": scheme_hash(scheme),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def required_characters(texts: Iterable[str]) -> set[str]:
    result: set[str] = set()
    for text in texts:
        for character in text:
            if not character.isspace() and not unicodedata.category(character).startswith("C"):
                result.add(character)
    return result


def resolve_scheme_files(project_dir: str | Path, scheme: dict[str, object]) -> list[list[Path]]:
    root = Path(project_dir)
    result: list[list[Path]] = []
    for slot in scheme["slots"]:
        if slot["mode"] == "keep":
            result.append([])
            continue
        files: list[Path] = []
        for record in slot["files"]:
            if record["kind"] == "bundled":
                path = bundled_font_path()
            else:
                path = _safe_project_file(root, record["path"])
            if _sha256_file(path) != record["sha256"]:
                raise FontError(f"字体文件 SHA-256 已变化: {path}")
            files.append(path)
        result.append(files)
    return result


def materialize_candidate(project_dir: str | Path, candidate: FontCandidate) -> dict[str, object]:
    with project_lock(project_dir, "materialize-font"):
        return _materialize_candidate_locked(project_dir, candidate)


def _materialize_candidate_locked(
    project_dir: str | Path,
    candidate: FontCandidate,
) -> dict[str, object]:
    if candidate.source not in {"bundled", "game", "system"} or not candidate.files:
        raise FontError("字体候选来源无效或没有字体文件")
    records: list[dict[str, str]] = []
    if candidate.source == "bundled":
        for path in candidate.files:
            digest = _sha256_file(path)
            if path.name != BUNDLED_FONT_ID or digest != BUNDLED_FONT_SHA256:
                raise FontError(f"未知随附字体: {path}")
            records.append(
                {
                    "kind": "bundled",
                    "id": BUNDLED_FONT_ID,
                    "filename": path.name,
                    "sha256": digest,
                }
            )
    else:
        root = Path(project_dir).resolve()
        for path in candidate.files:
            digest = _sha256_file(path)
            destination = root / "fonts" / digest / path.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.is_file() and _sha256_file(destination) != digest:
                raise FontError(f"项目字体缓存冲突: {destination}")
            if not destination.exists():
                with atomic_output_path(destination) as temporary:
                    shutil.copy2(path, temporary)
            records.append(
                {
                    "kind": "project",
                    "path": destination.relative_to(root).as_posix(),
                    "filename": path.name,
                    "sha256": digest,
                }
            )
    return {
        "mode": "font",
        "family": candidate.family,
        "provenance": candidate.source,
        "files": records,
    }


def _u16(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 2 > len(data):
        raise FontError("字体数据被截断")
    return struct.unpack_from(">H", data, offset)[0]


def _i16(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 2 > len(data):
        raise FontError("字体数据被截断")
    return struct.unpack_from(">h", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    if offset < 0 or offset + 4 > len(data):
        raise FontError("字体数据被截断")
    return struct.unpack_from(">I", data, offset)[0]


def _font_offsets(data: bytes) -> tuple[int, ...]:
    if data[:4] != b"ttcf":
        return (0,)
    if len(data) < 12:
        raise FontError("TTC 文件头被截断")
    count = _u32(data, 8)
    if count < 1 or 12 + count * 4 > len(data):
        raise FontError("TTC 字体目录无效")
    return tuple(_u32(data, 12 + index * 4) for index in range(count))


def _table(data: bytes, tag: bytes, font_offset: int = 0) -> tuple[int, int]:
    if font_offset < 0 or font_offset + 12 > len(data):
        raise FontError("无效的字体文件头")
    table_count = _u16(data, font_offset + 4)
    for index in range(table_count):
        record = font_offset + 12 + index * 16
        if record + 16 > len(data):
            raise FontError("字体表目录被截断")
        if data[record : record + 4] == tag:
            offset = _u32(data, record + 8)
            length = _u32(data, record + 12)
            if offset + length > len(data):
                raise FontError("字体表范围无效")
            return offset, length
    raise FontError(f"字体缺少 {tag.decode('ascii')} 表")


def _format_12_codepoints(data: bytes, offset: int, limit: int) -> set[int]:
    if offset < 0 or offset + 16 > limit:
        raise FontError("字体 cmap format 12 范围无效")
    length = _u32(data, offset + 4)
    if length < 16 or offset + length > limit:
        raise FontError("字体 cmap format 12 范围无效")
    groups = _u32(data, offset + 12)
    if 16 + groups * 12 > length:
        raise FontError("字体 cmap format 12 分组范围无效")
    result: set[int] = set()
    for index in range(groups):
        group = offset + 16 + index * 12
        start = _u32(data, group)
        end = _u32(data, group + 4)
        glyph = _u32(data, group + 8)
        if end < start or end > 0x10FFFF:
            raise FontError("字体 cmap format 12 分组无效")
        if glyph:
            result.update(range(start, end + 1))
        elif end > start:
            result.update(range(start + 1, end + 1))
    return result


def _format_4_codepoints(data: bytes, offset: int, limit: int) -> set[int]:
    if offset < 0 or offset + 14 > limit:
        raise FontError("字体 cmap format 4 范围无效")
    length = _u16(data, offset + 2)
    if length < 16 or offset + length > limit:
        raise FontError("字体 cmap format 4 范围无效")
    segment_count_x2 = _u16(data, offset + 6)
    if not segment_count_x2 or segment_count_x2 % 2:
        raise FontError("字体 cmap format 4 分段数量无效")
    segment_count = segment_count_x2 // 2
    end_codes = offset + 14
    start_codes = end_codes + segment_count * 2 + 2
    deltas = start_codes + segment_count * 2
    range_offsets = deltas + segment_count * 2
    if range_offsets + segment_count * 2 > offset + length:
        raise FontError("字体 cmap format 4 分段范围无效")
    result: set[int] = set()
    for index in range(segment_count):
        start = _u16(data, start_codes + index * 2)
        end = _u16(data, end_codes + index * 2)
        if end < start:
            raise FontError("字体 cmap format 4 分段无效")
        delta = _i16(data, deltas + index * 2)
        range_offset = _u16(data, range_offsets + index * 2)
        for codepoint in range(start, min(end, 0xFFFE) + 1):
            if range_offset == 0:
                glyph = (codepoint + delta) & 0xFFFF
            else:
                glyph_offset = range_offsets + index * 2 + range_offset + (codepoint - start) * 2
                if glyph_offset + 2 > offset + length:
                    raise FontError("字体 cmap format 4 字形索引越界")
                glyph = _u16(data, glyph_offset)
                if glyph:
                    glyph = (glyph + delta) & 0xFFFF
            if glyph:
                result.add(codepoint)
    return result


def _font_codepoints(data: bytes, font_offset: int) -> set[int]:
    cmap_offset, cmap_length = _table(data, b"cmap", font_offset)
    limit = cmap_offset + cmap_length
    if cmap_length < 4:
        raise FontError("字体 cmap 表被截断")
    record_count = _u16(data, cmap_offset + 2)
    if cmap_offset + 4 + record_count * 8 > limit:
        raise FontError("字体 cmap 记录范围无效")
    subtables: list[tuple[int, int]] = []
    for index in range(record_count):
        record = cmap_offset + 4 + index * 8
        subtable = cmap_offset + _u32(data, record + 4)
        if subtable + 2 > limit:
            raise FontError("字体 cmap 子表范围无效")
        subtables.append((_u16(data, subtable), subtable))
    result: set[int] = set()
    for format_id, subtable in sorted(subtables, key=lambda item: item[0] != 12):
        if format_id == 12:
            result.update(_format_12_codepoints(data, subtable, limit))
        elif format_id == 4:
            result.update(_format_4_codepoints(data, subtable, limit))
    return result


def _decode_name(platform_id: int, raw: bytes) -> str:
    if platform_id not in (0, 1, 3):
        return ""
    try:
        return raw.decode("utf-16-be" if platform_id in (0, 3) else "mac_roman").strip("\0 ")
    except (UnicodeDecodeError, LookupError):
        return ""


def _font_families(data: bytes, font_offset: int) -> set[str]:
    table_offset, table_length = _table(data, b"name", font_offset)
    if table_length < 6:
        raise FontError("字体 name 表被截断")
    count = _u16(data, table_offset + 2)
    strings = table_offset + _u16(data, table_offset + 4)
    if (
        table_offset + 6 + count * 12 > table_offset + table_length
        or strings < table_offset + 6 + count * 12
        or strings > table_offset + table_length
    ):
        raise FontError("字体 name 记录范围无效")
    names: dict[int, set[str]] = {1: set(), 16: set()}
    for index in range(count):
        record = table_offset + 6 + index * 12
        platform_id = _u16(data, record)
        name_id = _u16(data, record + 6)
        if name_id not in names:
            continue
        length = _u16(data, record + 8)
        offset = strings + _u16(data, record + 10)
        if offset < strings or offset + length > table_offset + table_length:
            continue
        name = _decode_name(platform_id, data[offset : offset + length])
        if name:
            names[name_id].add(name)
    return names[16] | names[1]


@functools.lru_cache(maxsize=512)
def _font_info_cached(path: str, size: int, modified_ns: int) -> tuple[tuple[str, ...], frozenset[int]]:
    del size, modified_ns
    data = Path(path).read_bytes()
    families: set[str] = set()
    codepoints: set[int] = set()
    for offset in _font_offsets(data):
        families.update(_font_families(data, offset))
        codepoints.update(_font_codepoints(data, offset))
    if not families or not codepoints:
        raise FontError("字体没有可用的字体族名称或 Unicode cmap")
    return tuple(sorted(families, key=str.casefold)), frozenset(codepoints)


def font_file_info(path: str | Path) -> tuple[tuple[str, ...], frozenset[int]]:
    target = Path(path).resolve()
    if target.suffix.lower() not in FONT_EXTENSIONS:
        raise FontError(f"不支持的字体格式: {target.suffix or '(无扩展名)'}")
    stat = target.stat()
    return _font_info_cached(str(target), stat.st_size, stat.st_mtime_ns)


def _font_paths(
    root: Path | None,
    *,
    recursive: bool,
    cancelled: Callable[[], bool] | None = None,
) -> list[Path]:
    if root is None or not root.is_dir():
        return []
    iterator = root.rglob("*") if recursive else root.iterdir()
    result = []
    for path in iterator:
        if cancelled and cancelled():
            raise InterruptedError("字体扫描已取消")
        if path.is_file() and path.suffix.lower() in FONT_EXTENSIONS:
            result.append(path)
    return result


def _system_font_paths(cancelled: Callable[[], bool] | None = None) -> list[Path]:
    if os.name != "nt":
        return []
    import winreg

    directories = {
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts",
    }
    paths: set[Path] = set()
    keys = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
    )
    for hive, key_name in keys:
        try:
            with winreg.OpenKey(hive, key_name) as key:
                for index in range(winreg.QueryInfoKey(key)[1]):
                    if cancelled and cancelled():
                        raise InterruptedError("字体扫描已取消")
                    _name, value, _kind = winreg.EnumValue(key, index)
                    if not isinstance(value, str):
                        continue
                    direct = Path(value)
                    if direct.is_absolute() and direct.is_file():
                        paths.add(direct.resolve())
                        continue
                    for directory in directories:
                        candidate = directory / value
                        if candidate.is_file():
                            paths.add(candidate.resolve())
                            break
        except InterruptedError:
            raise
        except OSError:
            continue
    return sorted(paths, key=lambda path: str(path).casefold())


def discover_font_candidates(
    game_root: str | Path,
    required: Iterable[str],
    *,
    cancelled: Callable[[], bool] | None = None,
) -> list[FontCandidate]:
    game = Path(game_root)
    sources = (
        ("bundled", [bundled_font_path()]),
        (
            "game",
            _font_paths(game, recursive=False, cancelled=cancelled)
            + _font_paths(game / "Data", recursive=True, cancelled=cancelled),
        ),
        ("system", _system_font_paths(cancelled)),
    )
    grouped: dict[tuple[str, str], dict[str, object]] = {}
    required_set = set(required)
    for source, paths in sources:
        for path in paths:
            if cancelled and cancelled():
                raise InterruptedError("字体扫描已取消")
            try:
                families, codepoints = font_file_info(path)
            except (OSError, FontError):
                continue
            for family in families:
                key = (source, family.casefold())
                item = grouped.setdefault(
                    key,
                    {"source": source, "family": family, "aliases": set(), "files": [], "coverage": set()},
                )
                item["aliases"].update(families)
                if path not in item["files"]:
                    item["files"].append(path)
                item["coverage"].update(codepoints)
    order = {"bundled": 0, "game": 1, "system": 2}
    result = []
    for item in grouped.values():
        if cancelled and cancelled():
            raise InterruptedError("字体扫描已取消")
        missing = frozenset(character for character in required_set if ord(character) not in item["coverage"])
        result.append(
            FontCandidate(
                source=str(item["source"]),
                family=str(item["family"]),
                aliases=tuple(sorted(item["aliases"], key=str.casefold)),
                files=tuple(sorted(item["files"], key=lambda path: str(path).casefold())),
                missing=missing,
            )
        )
    result.sort(key=lambda candidate: (order[candidate.source], len(candidate.missing), candidate.family.casefold()))
    return result


def candidate_for_family(candidates: Iterable[FontCandidate], family: str) -> FontCandidate | None:
    folded = family.casefold()
    for source in ("game", "bundled", "system"):
        for candidate in candidates:
            if candidate.source == source and any(alias.casefold() == folded for alias in candidate.aliases):
                return candidate
    return None


def with_missing(candidate: FontCandidate, required: Iterable[str]) -> FontCandidate:
    coverage: set[int] = set()
    for path in candidate.files:
        coverage.update(font_file_info(path)[1])
    missing = frozenset(character for character in set(required) if ord(character) not in coverage)
    return replace(candidate, missing=missing)
