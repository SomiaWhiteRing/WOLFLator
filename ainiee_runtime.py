from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from contextlib import nullcontext
from pathlib import Path
from typing import Callable

from formats import ARTIFACT_EPOCH, require_format
from process_tools import CancelledError, run_process, sha256_file, verified_vendor_file
from safe_io import atomic_write_bytes, atomic_write_json, package_lock, replace_with_retry, runtime_lock


AINIEE_VERSION = "V2.7.5"


AINIEE_COMMIT = "b8421fcb2b44d0cfac6411c4aeb9980ade26c972"


AINIEE_TREE = "e4dea9321d0fc549836f56952602886d151312c1"


AINIEE_ARCHIVE_URL = f"https://codeload.github.com/ShadowLoveElysia/AiNiee-Next/zip/{AINIEE_COMMIT}"


AINIEE_ARCHIVE_ETAG = "2a9725a113eb2ddf20a6f911236efce048f4b91880fad16a0e4641399cf0ef25"


AINIEE_ARCHIVE_SHA256 = "782ce8a8b32711aafbe1d3f82d2195b7eb2e5796afaa144c556e9f4924db0862"


AINIEE_SOURCE_SHA256 = "4e3671dd2a0711a1f1ce1568a6adf9de8b1bd52677f2e7f95176db78ebb9793f"


AINIEE_WEB_DIST_URL = f"https://github.com/ShadowLoveElysia/AiNiee-Next/releases/download/{AINIEE_VERSION}/web-dist.zip"


AINIEE_WEB_DIST_SHA256 = "09872794c798fd8cecd23cb5bbb21a4943e0de3dac4b74063429b878ca6f4645"


AINIEE_WEB_DIST_SIZE = 335_689


AINIEE_EXECUTABLE_FILES = {"Tools/Skills/launcher.sh"}


MAX_ARCHIVE_BYTES = 1_000_000_000


REQUIRED_PATHS = ("ainiee_cli.py", "pyproject.toml", "uv.lock", "Resource")


SOURCE_HASH_EXCLUDED = {".git", ".venv", "__pycache__", "output", "logs", "updatetemp"}


def _atomic_json(path: str | Path, value: object) -> Path:
    return atomic_write_json(path, value)


def _atomic_bytes(path: str | Path, value: bytes) -> Path:
    return atomic_write_bytes(path, value)


def _check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event and cancel_event.is_set():
        raise CancelledError("任务已取消。")


def _source_code_hash(root: Path) -> str:
    paths = [
        path
        for path in root.rglob("*.py")
        if not SOURCE_HASH_EXCLUDED.intersection(path.relative_to(root).parts)
    ]
    paths.extend(
        (
            root / "pyproject.toml",
            root / "uv.lock",
            root / "Resource" / "Version" / "version.json",
        )
    )
    digest = hashlib.sha256()
    for source in sorted(set(paths), key=lambda item: item.relative_to(root).as_posix()):
        if not source.is_file():
            raise ValueError(f"AiNiee 兼容文件不存在: {source.relative_to(root)}")
        relative = source.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(source.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def validate_ainiee_source(
    path: str | Path,
    *,
    expected_source_sha256: str = AINIEE_SOURCE_SHA256,
) -> Path:
    root = Path(path).resolve()
    missing = [relative for relative in REQUIRED_PATHS if not (root / relative).exists()]
    if missing:
        raise ValueError(f"AiNiee 运行目录缺少: {', '.join(missing)}")
    actual = _source_code_hash(root)
    if actual != expected_source_sha256:
        raise ValueError(f"AiNiee 源码版本不兼容: {actual}")
    return root


def locate_ainiee_source(selected: str | Path) -> Path:
    path = Path(selected).resolve()
    base = path.parent if path.is_file() else path
    direct = [
        base,
        base / "ainiee-runtime",
        base / "resources" / "ainiee-runtime",
        base / "Resources" / "ainiee-runtime",
    ]
    possible = {candidate.resolve() for candidate in direct}
    if base.is_dir():
        possible.update(
            cli_path.parent.resolve()
            for cli_path in base.rglob("ainiee_cli.py")
            if len(cli_path.relative_to(base).parts) <= 6
        )
    compatible: list[Path] = []
    for candidate in sorted(possible, key=str):
        try:
            compatible.append(validate_ainiee_source(candidate))
        except (ValueError, OSError):
            continue
    compatible = list(dict.fromkeys(compatible))
    if len(compatible) != 1:
        raise FileNotFoundError(
            f"所选位置中兼容的 AiNiee 运行目录数量为 {len(compatible)}。"
        )
    return compatible[0]


def _download(
    url: str,
    target: Path,
    *,
    allowed_hosts: set[str],
    expected_etag: str = "",
    expected_sha256: str = "",
    expected_size: int = 0,
    max_bytes: int = MAX_ARCHIVE_BYTES,
    cancel_event: threading.Event | None,
    progress: Callable[[int, int], None] | None,
) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "WOLFLator/1.0"})
    digest = hashlib.sha256()
    received = 0
    with urllib.request.urlopen(request, timeout=60) as response, target.open("wb") as writer:
        final_host = (urllib.parse.urlparse(response.geturl()).hostname or "").lower()
        if final_host not in allowed_hosts:
            raise ValueError(f"AiNiee 下载被重定向到非官方主机: {final_host}")
        etag = str(response.headers.get("ETag", "")).strip().removeprefix("W/").strip('"')
        if expected_etag and etag.lower() != expected_etag.lower():
            raise ValueError(f"AiNiee 源码包 ETag 不匹配: {etag}")
        total = int(response.headers.get("Content-Length", "0") or 0)
        if total > max_bytes:
            raise ValueError("AiNiee 下载包超过允许大小。")
        if expected_size and total and total != expected_size:
            raise ValueError(f"AiNiee 下载包大小不匹配: {total}")
        while True:
            _check_cancel(cancel_event)
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            received += len(chunk)
            if received > max_bytes:
                raise ValueError("AiNiee 下载包超过允许大小。")
            digest.update(chunk)
            writer.write(chunk)
            if progress:
                progress(received, total)
    actual_sha256 = digest.hexdigest()
    if expected_size and received != expected_size:
        raise ValueError(f"AiNiee 下载包大小不匹配: {received}")
    if expected_sha256 and actual_sha256.lower() != expected_sha256.lower():
        raise ValueError(f"AiNiee 下载包 SHA-256 不匹配: {actual_sha256}")
    return actual_sha256


def _git_tree_from_zip(archive: Path) -> str:
    tree: dict[str, object] = {}
    with zipfile.ZipFile(archive) as package:
        members = [member for member in package.infolist() if member.filename]
        if not members:
            raise ValueError("AiNiee 源码包为空。")
        root_name = members[0].filename.split("/", 1)[0] + "/"
        for member in members:
            if member.is_dir():
                continue
            if not member.filename.startswith(root_name):
                raise ValueError("AiNiee 源码包包含多个根目录。")
            relative = member.filename[len(root_name):]
            if not relative:
                continue
            node = tree
            parts = relative.split("/")
            for part in parts[:-1]:
                child = node.setdefault(part, {})
                if not isinstance(child, dict):
                    raise ValueError(f"AiNiee 源码包路径冲突: {relative}")
                node = child
            data = package.read(member)
            blob = hashlib.sha1(b"blob " + str(len(data)).encode("ascii") + b"\0" + data).hexdigest()
            mode = "100755" if relative in AINIEE_EXECUTABLE_FILES else "100644"
            node[parts[-1]] = (mode, blob)

    def tree_hash(node: dict[str, object]) -> str:
        entries: list[bytes] = []
        ordered = sorted(
            node.items(),
            key=lambda item: (item[0] + ("/" if isinstance(item[1], dict) else "")).encode("utf-8"),
        )
        for name, value in ordered:
            if isinstance(value, dict):
                mode, object_hash = "40000", tree_hash(value)
            else:
                mode, object_hash = value
            entries.append(
                mode.encode("ascii") + b" " + name.encode("utf-8") + b"\0" + bytes.fromhex(object_hash)
            )
        payload = b"".join(entries)
        return hashlib.sha1(b"tree " + str(len(payload)).encode("ascii") + b"\0" + payload).hexdigest()

    return tree_hash(tree)


def _extract_zip_checked(archive: Path, destination: Path, *, max_uncompressed: int) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    destination_real = destination.resolve()
    with zipfile.ZipFile(archive) as package:
        total_uncompressed = 0
        for member in package.infolist():
            name = member.filename.replace("\\", "/")
            parts = Path(name).parts
            if not name or name.startswith("/") or ".." in parts or re.match(r"^[A-Za-z]:", name):
                raise ValueError(f"AiNiee 压缩包包含越界路径: {name}")
            file_type = (member.external_attr >> 16) & 0o170000
            if file_type == 0o120000:
                raise ValueError(f"AiNiee 压缩包包含符号链接: {name}")
            total_uncompressed += member.file_size
            if total_uncompressed > max_uncompressed:
                raise ValueError("AiNiee 压缩包解压体积异常。")
            target = (destination / Path(name)).resolve()
            if os.path.commonpath([str(destination_real), str(target)]) != str(destination_real):
                raise ValueError(f"AiNiee 压缩包路径逃逸: {name}")
        package.extractall(destination)


def _safe_extract(archive: Path, destination: Path) -> Path:
    _extract_zip_checked(archive, destination, max_uncompressed=MAX_ARCHIVE_BYTES * 3)
    roots = [path for path in destination.iterdir() if path.is_dir()]
    if len(roots) != 1:
        raise ValueError("AiNiee 源码包根目录结构异常。")
    return validate_ainiee_source(roots[0])


def _safe_extract_web_dist(archive: Path, destination: Path) -> Path:
    _extract_zip_checked(archive, destination, max_uncompressed=20 * 1024 * 1024)
    dist = destination / "dist"
    if not (dist / "index.html").is_file() or not (dist / "assets").is_dir():
        raise ValueError("AiNiee web-dist.zip 结构不兼容。")
    extra_roots = [path.name for path in destination.iterdir() if path.name != "dist"]
    if extra_roots:
        raise ValueError(f"AiNiee web-dist.zip 包含意外根路径: {extra_roots}")
    return dist


def _web_dist_ready(root: Path) -> bool:
    assets = root / "Tools" / "WebServer" / "dist" / "assets"
    return (assets.parent / "index.html").is_file() and assets.is_dir() and any(assets.iterdir())


def _ensure_web_dist(
    root: Path,
    *,
    cancel_event: threading.Event | None = None,
    progress: Callable[[int, int], None] | None = None,
    log: Callable[[str], None] | None = None,
) -> None:
    if _web_dist_ready(root):
        return
    web_root = root / "Tools" / "WebServer"
    web_root.mkdir(parents=True, exist_ok=True)
    part = web_root / "web-dist.zip.part"
    extracting = web_root / ".web-dist.extracting"
    staged = web_root / ".dist.ready"
    if log:
        log(f"正在安装 AiNiee-Next {AINIEE_VERSION} 官方 Web 资源...")
    try:
        part.unlink(missing_ok=True)
        for path in (extracting, staged):
            if path.exists():
                shutil.rmtree(path)
        _download(
            AINIEE_WEB_DIST_URL,
            part,
            allowed_hosts={"github.com", "release-assets.githubusercontent.com"},
            expected_sha256=AINIEE_WEB_DIST_SHA256,
            expected_size=AINIEE_WEB_DIST_SIZE,
            max_bytes=20 * 1024 * 1024,
            cancel_event=cancel_event,
            progress=progress,
        )
        extracted = _safe_extract_web_dist(part, extracting)
        shutil.move(str(extracted), staged)
        target = web_root / "dist"
        if target.exists():
            shutil.rmtree(target)
        replace_with_retry(staged, target)
    finally:
        part.unlink(missing_ok=True)
        for path in (extracting, staged):
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)


def _validate_managed_package(
    path: Path,
    *,
    validate_source: Callable[[str | Path], Path] = validate_ainiee_source,
) -> Path:
    root = validate_source(path)
    metadata_path = root / "wolflator-package.json"
    if not metadata_path.is_file():
        raise ValueError("AiNiee 托管包缺少安装元数据。")
    metadata = require_format(
        json.loads(metadata_path.read_text(encoding="utf-8")),
        kind="ainiee-package",
        version_key="epoch",
        version=ARTIFACT_EPOCH,
        label="AiNiee 托管包元数据",
    )
    expected = {
        "version": AINIEE_VERSION,
        "commit": AINIEE_COMMIT,
        "source_url": AINIEE_ARCHIVE_URL,
        "tree": AINIEE_TREE,
        "archive_etag": AINIEE_ARCHIVE_ETAG,
        "archive_sha256": AINIEE_ARCHIVE_SHA256,
        "web_dist_url": AINIEE_WEB_DIST_URL,
        "web_dist_sha256": AINIEE_WEB_DIST_SHA256,
    }
    mismatched = {
        key: {"expected": value, "actual": metadata.get(key)}
        for key, value in expected.items()
        if metadata.get(key) != value
    }
    if mismatched:
        raise ValueError(f"AiNiee 托管包元数据不匹配: {mismatched}")
    if set(metadata) != {"kind", "epoch", *expected, "installed_at"} or not isinstance(
        metadata["installed_at"], (int, float)
    ):
        raise ValueError("AiNiee 托管包元数据字段不匹配。")
    if not _web_dist_ready(root):
        raise ValueError("AiNiee 托管包缺少 Web 资源。")
    return root


def install_supported_ainiee(
    packages_root: str | Path,
    *,
    repair: bool = False,
    cancel_event: threading.Event | None = None,
    progress: Callable[[int, int], None] | None = None,
    log: Callable[[str], None] | None = None,
) -> Path:
    with package_lock(packages_root, "install-ainiee"):
        return _install_supported_ainiee_locked(
            packages_root,
            repair=repair,
            cancel_event=cancel_event,
            progress=progress,
            log=log,
        )


def _install_supported_ainiee_locked(
    packages_root: str | Path,
    *,
    repair: bool,
    cancel_event: threading.Event | None,
    progress: Callable[[int, int], None] | None,
    log: Callable[[str], None] | None,
) -> Path:
    packages = Path(packages_root)
    packages.mkdir(parents=True, exist_ok=True)
    final = packages / AINIEE_VERSION
    if final.exists() and not repair:
        try:
            return _validate_managed_package(final)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    part = packages / f"{AINIEE_VERSION}.zip.part"
    extract_dir = packages / f".{AINIEE_VERSION}.extracting"
    if log:
        log(f"正在下载 AiNiee-Next {AINIEE_VERSION} ({AINIEE_COMMIT[:12]})...")
    try:
        part.unlink(missing_ok=True)
        if extract_dir.exists():
            shutil.rmtree(extract_dir)
        archive_sha256 = _download(
            AINIEE_ARCHIVE_URL,
            part,
            allowed_hosts={"codeload.github.com"},
            expected_etag=AINIEE_ARCHIVE_ETAG,
            expected_sha256=AINIEE_ARCHIVE_SHA256,
            cancel_event=cancel_event,
            progress=progress,
        )
        archive_tree = _git_tree_from_zip(part)
        if archive_tree != AINIEE_TREE:
            raise ValueError(f"AiNiee 源码树不匹配: {archive_tree}")
        source_root = _safe_extract(part, extract_dir)
        _ensure_web_dist(
            source_root,
            cancel_event=cancel_event,
            progress=progress,
            log=log,
        )
        metadata = {
            "kind": "ainiee-package",
            "epoch": ARTIFACT_EPOCH,
            "version": AINIEE_VERSION,
            "commit": AINIEE_COMMIT,
            "source_url": AINIEE_ARCHIVE_URL,
            "tree": AINIEE_TREE,
            "archive_etag": AINIEE_ARCHIVE_ETAG,
            "archive_sha256": archive_sha256,
            "web_dist_url": AINIEE_WEB_DIST_URL,
            "web_dist_sha256": AINIEE_WEB_DIST_SHA256,
            "installed_at": time.time(),
        }
        _atomic_json(source_root / "wolflator-package.json", metadata)
        staged = packages / f".{AINIEE_VERSION}.ready"
        if staged.exists():
            shutil.rmtree(staged)
        shutil.move(str(source_root), staged)
        if final.exists():
            shutil.rmtree(final)
        replace_with_retry(staged, final)
        if log:
            log(f"AiNiee 已安装到 {final}")
        return _validate_managed_package(final)
    finally:
        part.unlink(missing_ok=True)
        if extract_dir.exists():
            shutil.rmtree(extract_dir, ignore_errors=True)


def _runtime_fingerprint(source: Path) -> str:
    return _source_code_hash(source)[:20]


def _load_runtime_metadata(path: Path) -> dict[str, object]:
    value = require_format(
        json.loads(path.read_text(encoding="utf-8")),
        kind="ainiee-runtime",
        version_key="epoch",
        version=ARTIFACT_EPOCH,
        label="AiNiee 运行时元数据",
    )
    if set(value) != {"kind", "epoch", "fingerprint", "source", "created_at"} or not (
        isinstance(value["fingerprint"], str)
        and isinstance(value["source"], str)
        and isinstance(value["created_at"], (int, float))
    ):
        raise ValueError("AiNiee 运行时元数据字段不匹配。")
    return value


def create_managed_runtime(
    source: str | Path,
    runtime_root: str | Path,
    *,
    refresh: bool = False,
) -> Path:
    with runtime_lock(runtime_root, "create-runtime"):
        return _create_managed_runtime_locked(source, runtime_root, refresh=refresh)


def _create_managed_runtime_locked(
    source: str | Path,
    runtime_root: str | Path,
    *,
    refresh: bool,
) -> Path:
    source_root = locate_ainiee_source(source)
    fingerprint = _runtime_fingerprint(source_root)
    root = Path(runtime_root)
    final = root / fingerprint
    marker = final / ".wolflator-runtime.json"
    if marker.is_file() and not refresh:
        try:
            data = _load_runtime_metadata(marker)
            if data.get("fingerprint") == fingerprint:
                return validate_ainiee_source(final)
        except Exception:
            pass
    root.mkdir(parents=True, exist_ok=True)
    for candidate in root.iterdir():
        candidate_marker = candidate / ".wolflator-runtime.json"
        if candidate == final or not candidate_marker.is_file():
            continue
        try:
            data = _load_runtime_metadata(candidate_marker)
            same_source = Path(str(data["source"])).resolve() == source_root
        except (KeyError, OSError, ValueError, json.JSONDecodeError):
            same_source = False
        if same_source:
            shutil.rmtree(candidate)
    temporary = root / f".{fingerprint}.copying"
    if temporary.exists():
        shutil.rmtree(temporary)
    ignored = shutil.ignore_patterns(".git", ".venv", "__pycache__", "output", "logs", "updatetemp", "*.pyc")
    shutil.copytree(source_root, temporary, ignore=ignored)
    _atomic_json(
        temporary / ".wolflator-runtime.json",
        {
            "kind": "ainiee-runtime",
            "epoch": ARTIFACT_EPOCH,
            "fingerprint": fingerprint,
            "source": str(source_root),
            "created_at": time.time(),
        },
    )
    if final.exists():
        shutil.rmtree(final)
    replace_with_retry(temporary, final)
    return validate_ainiee_source(final)


def _managed_runtime_path(source: str | Path, runtime_root: str | Path) -> tuple[Path, str]:
    source_root = locate_ainiee_source(source)
    fingerprint = _runtime_fingerprint(source_root)
    return Path(runtime_root) / fingerprint, fingerprint


def locate_uv() -> Path:
    override = os.environ.get("WOLFLATOR_UV", "")
    if override:
        candidate = Path(override)
        if not candidate.is_file():
            raise FileNotFoundError(f"WOLFLATOR_UV 指向的文件不存在: {candidate}")
        return candidate
    return verified_vendor_file("uv.exe", "uv", "exe_sha256")


def sync_runtime(
    runtime: str | Path,
    *,
    force: bool = False,
    cancel_event: threading.Event | None = None,
    log: Callable[[str], None] | None = None,
) -> None:
    root = Path(runtime).resolve()
    with runtime_lock(root.parent, "sync-runtime"):
        _sync_runtime_locked(
            root,
            force=force,
            cancel_event=cancel_event,
            log=log,
        )


def _sync_runtime_locked(
    runtime: str | Path,
    *,
    force: bool,
    cancel_event: threading.Event | None,
    log: Callable[[str], None] | None,
) -> None:
    root = validate_ainiee_source(runtime)
    lock_hash = sha256_file(root / "uv.lock")
    marker = root / ".uv-sync"
    if not force and marker.is_file() and marker.read_text(encoding="ascii", errors="ignore") == lock_hash and (root / ".venv").is_dir():
        return
    run_process(
        [str(locate_uv()), "sync", "--frozen"],
        cwd=root,
        timeout=3600,
        cancel_event=cancel_event,
        log=log,
    )
    atomic_write_bytes(marker, lock_hash.encode("ascii"))


def prepare_managed_runtime(
    source: str | Path,
    runtime_root: str | Path,
    *,
    force_sync: bool = False,
    cancel_event: threading.Event | None = None,
    log: Callable[[str], None] | None = None,
) -> Path:
    source_root = locate_ainiee_source(source)
    package_context = (
        package_lock(source_root.parent, "prepare-runtime-source")
        if (source_root / "wolflator-package.json").is_file()
        else nullcontext()
    )
    with package_context:
        with runtime_lock(runtime_root, "prepare-runtime"):
            runtime = _create_managed_runtime_locked(
                source_root,
                runtime_root,
                refresh=force_sync,
            )
            _ensure_web_dist(runtime, cancel_event=cancel_event, log=log)
            _sync_runtime_locked(
                runtime,
                force=force_sync,
                cancel_event=cancel_event,
                log=log,
            )
            return runtime


def remove_managed_ainiee(
    source: str | Path,
    packages_root: str | Path,
    runtime_root: str | Path,
) -> None:
    with package_lock(packages_root, "remove-ainiee"):
        with runtime_lock(runtime_root, "remove-ainiee"):
            _remove_managed_ainiee_locked(source, packages_root, runtime_root)


def _remove_managed_ainiee_locked(
    source: str | Path,
    packages_root: str | Path,
    runtime_root: str | Path,
) -> None:
    source_root = Path(source).resolve()
    packages = Path(packages_root).resolve()
    if source_root == packages or os.path.commonpath((source_root, packages)) != str(packages):
        raise ValueError("拒绝移除 WOLFLator 托管目录以外的 AiNiee。")
    runtimes = Path(runtime_root)
    if runtimes.is_dir():
        for candidate in runtimes.iterdir():
            marker = candidate / ".wolflator-runtime.json"
            if not marker.is_file():
                continue
            try:
                metadata = _load_runtime_metadata(marker)
                same_source = Path(str(metadata["source"])).resolve() == source_root
            except (KeyError, OSError, ValueError, json.JSONDecodeError):
                same_source = False
            if same_source:
                shutil.rmtree(candidate)
    shutil.rmtree(source_root)


def require_managed_runtime(source: str | Path, runtime_root: str | Path) -> Path:
    runtime, fingerprint = _managed_runtime_path(source, runtime_root)
    try:
        validate_ainiee_source(runtime)
        metadata = _load_runtime_metadata(runtime / ".wolflator-runtime.json")
        synced_lock = (runtime / ".uv-sync").read_text(encoding="ascii", errors="ignore")
        ready = (
            metadata.get("fingerprint") == fingerprint
            and (runtime / ".venv").is_dir()
            and synced_lock == sha256_file(runtime / "uv.lock")
            and _web_dist_ready(runtime)
        )
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        ready = False
    if not ready:
        raise RuntimeError(
            "AiNiee 运行环境尚未准备好。请打开设置，重新选择 AiNiee 目录，"
            "或点击“安装/修复”，并等待依赖安装完成。"
        )
    return runtime
