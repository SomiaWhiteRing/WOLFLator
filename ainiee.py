from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import count
from pathlib import Path
from typing import Callable, Iterable

from models import AppSettings, ImportCategory, TranslationItem
from wolf_tools import CancelledError, run_process, sha256_file, verified_vendor_file


AINIEE_VERSION = "V2.7.5"
AINIEE_COMMIT = "b8421fcb2b44d0cfac6411c4aeb9980ade26c972"
AINIEE_TREE = "e4dea9321d0fc549836f56952602886d151312c1"
AINIEE_ARCHIVE_URL = f"https://codeload.github.com/ShadowLoveElysia/AiNiee-Next/zip/{AINIEE_COMMIT}"
AINIEE_ARCHIVE_ETAG = "2a9725a113eb2ddf20a6f911236efce048f4b91880fad16a0e4641399cf0ef25"
AINIEE_EXECUTABLE_FILES = {"Tools/Skills/launcher.sh"}
MAX_ARCHIVE_BYTES = 1_000_000_000
SESSION_PROFILE = "WOLFLator_session"
REQUIRED_PATHS = ("ainiee_cli.py", "pyproject.toml", "uv.lock", "Resource")
RULE_DEFAULTS = {
    "pre_translation_data": [],
    "post_translation_data": [],
    "prompt_dictionary_data": [],
    "exclusion_list_data": [],
    "characterization_data": [],
    "world_building_content": "",
    "world_building_history": [],
    "writing_style_content": "",
    "writing_style_history": [],
    "translation_example_data": [],
    "pre_translation_switch": False,
    "post_translation_switch": False,
    "prompt_dictionary_switch": True,
    "exclusion_list_switch": False,
    "characterization_switch": True,
    "world_building_switch": False,
    "writing_style_switch": False,
    "translation_example_switch": False,
}


def _atomic_json(path: str | Path, value: object) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, output)
    return output


def _check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event and cancel_event.is_set():
        raise CancelledError("任务已取消。")


def validate_ainiee_source(path: str | Path) -> Path:
    root = Path(path).resolve()
    missing = [relative for relative in REQUIRED_PATHS if not (root / relative).exists()]
    if missing:
        raise ValueError(f"AiNiee 运行目录缺少: {', '.join(missing)}")
    cli_text = (root / "ainiee_cli.py").read_text(encoding="utf-8", errors="replace")
    required_flags = ("--rules-profile", "--type", "--api-key", "translate")
    absent = [flag for flag in required_flags if flag not in cli_text]
    if absent:
        raise ValueError(f"AiNiee CLI 不兼容，缺少参数: {', '.join(absent)}")
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
    for candidate in direct:
        try:
            return validate_ainiee_source(candidate)
        except (ValueError, OSError):
            pass
    for cli_path in base.rglob("ainiee_cli.py"):
        try:
            if len(cli_path.relative_to(base).parts) > 6:
                continue
            return validate_ainiee_source(cli_path.parent)
        except (ValueError, OSError):
            continue
    raise FileNotFoundError("所选位置中没有兼容的 AiNiee 运行目录。")


def _download(
    url: str,
    target: Path,
    *,
    expected_etag: str,
    cancel_event: threading.Event | None,
    progress: Callable[[int, int], None] | None,
) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "WOLFLator/1.0"})
    digest = hashlib.sha256()
    received = 0
    with urllib.request.urlopen(request, timeout=60) as response, target.open("wb") as writer:
        final_host = (urllib.parse.urlparse(response.geturl()).hostname or "").lower()
        if final_host != "codeload.github.com":
            raise ValueError(f"AiNiee 下载被重定向到非官方主机: {final_host}")
        etag = str(response.headers.get("ETag", "")).strip().removeprefix("W/").strip('"')
        if etag.lower() != expected_etag.lower():
            raise ValueError(f"AiNiee 源码包 ETag 不匹配: {etag}")
        total = int(response.headers.get("Content-Length", "0") or 0)
        if total > MAX_ARCHIVE_BYTES:
            raise ValueError("AiNiee 源码包超过允许大小。")
        while True:
            _check_cancel(cancel_event)
            chunk = response.read(1024 * 1024)
            if not chunk:
                break
            received += len(chunk)
            if received > MAX_ARCHIVE_BYTES:
                raise ValueError("AiNiee 源码包超过允许大小。")
            digest.update(chunk)
            writer.write(chunk)
            if progress:
                progress(received, total)
    return digest.hexdigest()


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


def _safe_extract(archive: Path, destination: Path) -> Path:
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
            if total_uncompressed > MAX_ARCHIVE_BYTES * 3:
                raise ValueError("AiNiee 压缩包解压体积异常。")
            target = (destination / Path(name)).resolve()
            if os.path.commonpath([str(destination_real), str(target)]) != str(destination_real):
                raise ValueError(f"AiNiee 压缩包路径逃逸: {name}")
        package.extractall(destination)
    roots = [path for path in destination.iterdir() if path.is_dir()]
    if len(roots) != 1:
        raise ValueError("AiNiee 源码包根目录结构异常。")
    return validate_ainiee_source(roots[0])


def install_supported_ainiee(
    packages_root: str | Path,
    *,
    repair: bool = False,
    cancel_event: threading.Event | None = None,
    progress: Callable[[int, int], None] | None = None,
    log: Callable[[str], None] | None = None,
) -> Path:
    packages = Path(packages_root)
    packages.mkdir(parents=True, exist_ok=True)
    final = packages / AINIEE_VERSION
    if final.exists() and not repair:
        return validate_ainiee_source(final)
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
            expected_etag=AINIEE_ARCHIVE_ETAG,
            cancel_event=cancel_event,
            progress=progress,
        )
        archive_tree = _git_tree_from_zip(part)
        if archive_tree != AINIEE_TREE:
            raise ValueError(f"AiNiee 源码树不匹配: {archive_tree}")
        source_root = _safe_extract(part, extract_dir)
        metadata = {
            "version": AINIEE_VERSION,
            "commit": AINIEE_COMMIT,
            "source_url": AINIEE_ARCHIVE_URL,
            "tree": AINIEE_TREE,
            "archive_etag": AINIEE_ARCHIVE_ETAG,
            "archive_sha256": archive_sha256,
            "installed_at": time.time(),
        }
        _atomic_json(source_root / "wolflator-package.json", metadata)
        staged = packages / f".{AINIEE_VERSION}.ready"
        if staged.exists():
            shutil.rmtree(staged)
        shutil.move(str(source_root), staged)
        if final.exists():
            shutil.rmtree(final)
        os.replace(staged, final)
        if log:
            log(f"AiNiee 已安装到 {final}")
        return validate_ainiee_source(final)
    finally:
        part.unlink(missing_ok=True)
        if extract_dir.exists():
            shutil.rmtree(extract_dir, ignore_errors=True)


def _runtime_fingerprint(source: Path) -> str:
    digest = hashlib.sha256()
    for relative in ("ainiee_cli.py", "pyproject.toml", "uv.lock", "Resource/Version/version.json"):
        path = source / relative
        digest.update(relative.encode("ascii"))
        digest.update(path.read_bytes() if path.is_file() else b"")
    return digest.hexdigest()[:20]


def create_managed_runtime(source: str | Path, runtime_root: str | Path) -> Path:
    source_root = locate_ainiee_source(source)
    fingerprint = _runtime_fingerprint(source_root)
    root = Path(runtime_root)
    final = root / fingerprint
    marker = final / ".wolflator-runtime.json"
    if marker.is_file():
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            if data.get("fingerprint") == fingerprint:
                return validate_ainiee_source(final)
        except Exception:
            pass
    root.mkdir(parents=True, exist_ok=True)
    temporary = root / f".{fingerprint}.copying"
    if temporary.exists():
        shutil.rmtree(temporary)
    ignored = shutil.ignore_patterns(".git", ".venv", "__pycache__", "output", "logs", "updatetemp", "*.pyc")
    shutil.copytree(source_root, temporary, ignore=ignored)
    _atomic_json(
        temporary / ".wolflator-runtime.json",
        {"fingerprint": fingerprint, "source": str(source_root), "created_at": time.time()},
    )
    if final.exists():
        shutil.rmtree(final)
    os.replace(temporary, final)
    return validate_ainiee_source(final)


def _managed_runtime_path(source: str | Path, runtime_root: str | Path) -> tuple[Path, str]:
    source_root = locate_ainiee_source(source)
    fingerprint = _runtime_fingerprint(source_root)
    return Path(runtime_root) / fingerprint, fingerprint


def locate_uv() -> Path:
    override = os.environ.get("WOLFLATOR_UV", "")
    candidates = [Path(override)] if override else []
    try:
        candidates.append(verified_vendor_file("uv.exe", "uv", "exe_sha256"))
    except (FileNotFoundError, ValueError):
        pass
    discovered = shutil.which("uv")
    if discovered:
        candidates.append(Path(discovered))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("未找到 uv.exe。开发环境请运行 scripts/fetch_vendor.ps1。")


def sync_runtime(
    runtime: str | Path,
    *,
    force: bool = False,
    cancel_event: threading.Event | None = None,
    log: Callable[[str], None] | None = None,
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
    marker.write_text(lock_hash, encoding="ascii")


def prepare_managed_runtime(
    source: str | Path,
    runtime_root: str | Path,
    *,
    force_sync: bool = False,
    cancel_event: threading.Event | None = None,
    log: Callable[[str], None] | None = None,
) -> Path:
    runtime = create_managed_runtime(source, runtime_root)
    sync_runtime(runtime, force=force_sync, cancel_event=cancel_event, log=log)
    return runtime


def require_managed_runtime(source: str | Path, runtime_root: str | Path) -> Path:
    runtime, fingerprint = _managed_runtime_path(source, runtime_root)
    try:
        validate_ainiee_source(runtime)
        metadata = json.loads((runtime / ".wolflator-runtime.json").read_text(encoding="utf-8"))
        synced_lock = (runtime / ".uv-sync").read_text(encoding="ascii", errors="ignore")
        ready = (
            metadata.get("fingerprint") == fingerprint
            and (runtime / ".venv").is_dir()
            and synced_lock == sha256_file(runtime / "uv.lock")
        )
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        ready = False
    if not ready:
        raise RuntimeError(
            "AiNiee 运行环境尚未准备好。请打开设置，重新选择 AiNiee 目录，"
            "或点击“安装/修复”，并等待依赖安装完成。"
        )
    return runtime


def _rules_name(project_id: str) -> str:
    safe = re.sub(r'[^A-Za-z0-9_.-]+', "_", project_id).strip("._") or "project"
    return f"WOLFLator_{safe[:80]}"


def cleanup_session_profiles(runtime: str | Path) -> None:
    root = Path(runtime)
    profiles = root / "Resource" / "profiles"
    if profiles.is_dir():
        for path in profiles.glob("WOLFLator_session*.json"):
            path.unlink(missing_ok=True)


def _session_profile(settings: AppSettings, api_key: str) -> dict[str, object]:
    base_url = settings.api_base_url.rstrip("/")
    platform = {
        "tag": "custom_openai",
        "group": "custom",
        "name": "WOLFLator OpenAI Compatible",
        "api_url": base_url,
        "api_key": api_key,
        "api_format": "OpenAI",
        "icon": "custom",
        "rpm_limit": max(1, settings.api_rpm),
        "tpm_limit": max(1, settings.api_tpm),
        "model": settings.api_model,
        "model_datas": [settings.api_model],
        "top_p": 1.0,
        "temperature": 0.2,
        "presence_penalty": 0.0,
        "frequency_penalty": 0.0,
        "think_switch": False,
        "think_depth": "low",
        "structured_output_mode": 0,
        "auto_complete": False,
        "key_in_settings": ["api_url", "api_key", "model", "rpm_limit", "tpm_limit"],
    }
    return {
        "interface_language": "zh_CN",
        "source_language": "Japanese",
        "target_language": "Chinese",
        "target_platform": "custom_openai",
        "api_settings": {"translate": "custom_openai", "polish": "custom_openai"},
        "platforms": {"custom_openai": platform},
        "base_url": base_url,
        "model": settings.api_model,
        "api_key": api_key,
        "translation_project": "Paratranz",
        "interactive_mode": False,
        "user_thread_counts": max(1, settings.api_threads),
        "request_timeout": max(10, settings.api_timeout),
        "enable_api_failover": False,
        "enable_session_logging": True,
        "show_detailed_logs": True,
    }


def _report_ainiee_logs(
    output: Path,
    diagnostic_log: Callable[[str], None] | None,
    *,
    include_tail: bool,
) -> None:
    if not diagnostic_log:
        return
    files = sorted(
        (path for path in (output / "logs").glob("*") if path.is_file()),
        key=lambda path: path.stat().st_mtime_ns,
    )
    diagnostic_log(
        f"ainiee.session_logs count={len(files)} paths="
        + json.dumps([str(path) for path in files], ensure_ascii=False)
    )
    if not include_tail or not files:
        return
    latest = files[-1]
    try:
        text = latest.read_text(encoding="utf-8", errors="replace")[-65_536:]
    except OSError as exc:
        diagnostic_log(f"ainiee.session_log.read_failed path={latest} error={exc}")
        return
    diagnostic_log(f"ainiee.session_log.tail path={latest} chars={len(text)}")
    for line in text.splitlines()[-200:]:
        diagnostic_log(f"ainiee.session {line}")


def run_translation(
    runtime: str | Path,
    input_json: str | Path,
    output_dir: str | Path,
    rules: dict[str, object],
    project_id: str,
    settings: AppSettings,
    api_key: str,
    *,
    cancel_event: threading.Event | None = None,
    log: Callable[[str], None] | None = None,
    diagnostic_log: Callable[[str], None] | None = None,
) -> list[dict[str, object]]:
    root = validate_ainiee_source(runtime)
    cleanup_session_profiles(root)
    profiles = root / "Resource" / "profiles"
    rules_profiles = root / "Resource" / "rules_profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    rules_profiles.mkdir(parents=True, exist_ok=True)
    rules_name = _rules_name(project_id)
    profile_path = profiles / f"{SESSION_PROFILE}.json"
    _atomic_json(profile_path, _session_profile(settings, api_key))
    _atomic_json(rules_profiles / f"{rules_name}.json", rules)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    if diagnostic_log:
        diagnostic_log(
            f"ainiee.translate.start runtime={root} input={Path(input_json).resolve()} "
            f"input_bytes={Path(input_json).stat().st_size} output={output.resolve()} "
            f"profile={SESSION_PROFILE} rules_profile={rules_name}"
        )
    try:
        try:
            run_process(
                [
                    str(locate_uv()),
                    "run",
                    "ainiee_cli.py",
                    "translate",
                    str(Path(input_json).resolve()),
                    "-o",
                    str(output.resolve()),
                    "-p",
                    SESSION_PROFILE,
                    "--rules-profile",
                    rules_name,
                    "-s",
                    "Japanese",
                    "-t",
                    "Chinese",
                    "--type",
                    "Paratranz",
                    "--yes",
                ],
                cwd=root,
                timeout=24 * 3600,
                cancel_event=cancel_event,
                log=log,
                diagnostic_log=diagnostic_log,
            )
        except Exception:
            _report_ainiee_logs(output, diagnostic_log, include_tail=True)
            raise
        _report_ainiee_logs(output, diagnostic_log, include_tail=False)
        expected_name = f"{Path(input_json).stem}_translated.json"
        candidates = list(output.rglob(expected_name))
        if diagnostic_log:
            diagnostic_log(
                f"ainiee.translate.outputs expected={expected_name} candidates={len(candidates)} "
                f"paths={json.dumps([str(path) for path in candidates], ensure_ascii=False)}"
            )
        if not candidates:
            raise RuntimeError(f"AiNiee 返回成功，但没有生成 {expected_name}。")
        data = json.loads(max(candidates, key=lambda p: p.stat().st_mtime_ns).read_text(encoding="utf-8-sig"))
        if not isinstance(data, list):
            raise ValueError("AiNiee 输出不是 Paratranz 数组。")
        if diagnostic_log:
            diagnostic_log(f"ainiee.translate.complete rows={len(data)}")
        return data
    finally:
        profile_path.unlink(missing_ok=True)


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class OpenAICompatibleClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout: int = 120,
        diagnostic_log: Callable[[str], None] | None = None,
    ):
        base = base_url.strip().rstrip("/")
        if not base.startswith(("https://", "http://")):
            raise ValueError("API 基础地址必须以 http:// 或 https:// 开头。")
        for suffix in ("/chat/completions", "/completions", "/chat"):
            if base.endswith(suffix):
                base = base[: -len(suffix)].rstrip("/")
                break
        self.url = base + "/chat/completions"
        self.api_key = api_key
        self.model = model
        self.timeout = max(10, timeout)
        self.diagnostic_log = diagnostic_log
        self._request_ids = count(1)

    def _diagnostic_url(self) -> str:
        parsed = urllib.parse.urlsplit(self.url)
        netloc = parsed.netloc.rsplit("@", 1)[-1]
        return urllib.parse.urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))

    def chat(
        self,
        prompt: str,
        *,
        max_tokens: int | None = 4096,
        system_prompt: str = "",
    ) -> str:
        request_id = next(self._request_ids)
        started = time.monotonic()
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})
        body: dict[str, object] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
            "stream": False,
        }
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if "deepseek" in self.model.lower() or "deepseek" in self.url.lower():
            body["thinking"] = {"type": "disabled"}
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "User-Agent": "WOLFLator/1.0",
            },
            method="POST",
        )
        if self.diagnostic_log:
            self.diagnostic_log(
                f"api.request id={request_id} url={self._diagnostic_url()} model={self.model} "
                f"timeout={self.timeout}s prompt_chars={len(prompt)} system_chars={len(system_prompt)} "
                f"payload_bytes={len(payload)} max_tokens={max_tokens}"
            )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
                status = response.status if isinstance(getattr(response, "status", None), int) else 200
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[-2000:]
            if self.diagnostic_log:
                self.diagnostic_log(
                    f"api.error id={request_id} kind=http status={exc.code} "
                    f"duration={time.monotonic() - started:.3f}s body={detail}"
                )
            raise ApiError(f"API HTTP {exc.code}: {detail}", exc.code) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if self.diagnostic_log:
                self.diagnostic_log(
                    f"api.error id={request_id} kind=connection error_type={type(exc).__name__} "
                    f"duration={time.monotonic() - started:.3f}s error={exc}"
                )
            raise ApiError(f"API 连接失败: {exc}") from exc
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            preview = raw.decode("utf-8", errors="replace")[-2000:]
            if self.diagnostic_log:
                self.diagnostic_log(
                    f"api.error id={request_id} kind=response_json status={status} "
                    f"duration={time.monotonic() - started:.3f}s response_bytes={len(raw)} "
                    f"error={exc} body={preview}"
                )
            raise ApiError(f"API 返回的 JSON 无法解析: {exc}") from exc
        try:
            choice = result["choices"][0]
            content = choice["message"].get("content") or ""
            if isinstance(content, list):
                content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
            content = str(content)
            if "</think>" in content:
                content = content.split("</think>", 1)[1]
            if self.diagnostic_log:
                usage = result.get("usage", {}) if isinstance(result, dict) else {}
                self.diagnostic_log(
                    f"api.response id={request_id} status={status} duration={time.monotonic() - started:.3f}s "
                    f"response_bytes={len(raw)} finish_reason={choice.get('finish_reason')} "
                    f"content_chars={len(content)} usage={json.dumps(usage, ensure_ascii=False, sort_keys=True)}"
                )
            if str(choice.get("finish_reason", "")).lower() == "length":
                raise ApiError("模型输出达到上限，响应被截断。")
            return content
        except (KeyError, IndexError, TypeError) as exc:
            if self.diagnostic_log:
                self.diagnostic_log(
                    f"api.error id={request_id} kind=response_shape status={status} "
                    f"duration={time.monotonic() - started:.3f}s error={exc}"
                )
            raise ApiError(f"API 返回格式不兼容: {str(result)[:1000]}") from exc


def test_api(settings: AppSettings, api_key: str, *, glossary: bool = False) -> str:
    base_url = settings.glossary_api_base_url if glossary else settings.api_base_url
    model = settings.glossary_api_model if glossary else settings.api_model
    timeout = settings.glossary_api_timeout if glossary else settings.api_timeout
    client = OpenAICompatibleClient(base_url, api_key, model, timeout)
    response = client.chat(
        "小可爱，你在干嘛",
        max_tokens=None,
        system_prompt="你接下来要扮演我的女朋友，名字叫欣雨，请你以女朋友的方式回复我。",
    )
    if not response.strip():
        raise ApiError("API 测试没有返回内容。")
    return response.strip()


def _chunks(lines: list[str], max_chars: int = 80_000, overlap: int = 10) -> list[str]:
    chunks: list[str] = []
    start = 0
    while start < len(lines):
        end = start
        size = 0
        while end < len(lines) and (size + len(lines[end]) <= max_chars or end == start):
            size += len(lines[end]) + 1
            end += 1
        chunks.append("\n".join(lines[start:end]))
        if end >= len(lines):
            break
        start = max(start + 1, end - overlap)
    return chunks


def _json_list(text: str) -> list[dict[str, object]]:
    clean = text.strip()
    if clean.startswith("```"):
        lines = clean.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines.pop()
        clean = "\n".join(lines).strip()
    data = json.loads(clean)
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list):
                data = value
                break
    if not isinstance(data, list):
        raise ValueError("术语模型没有返回 JSON 数组。")
    return [row for row in data if isinstance(row, dict)]


def _request_chunk(
    client: OpenAICompatibleClient,
    prompt_prefix: str,
    chunk: str,
    *,
    cancel_event: threading.Event | None,
    max_tokens: int | None = None,
    split_depth: int = 0,
    diagnostic_log: Callable[[str], None] | None = None,
    request_label: str = "",
) -> list[dict[str, object]]:
    _check_cancel(cancel_event)
    last_error: Exception | None = None
    for attempt in range(3):
        response_text = ""
        if diagnostic_log:
            diagnostic_log(
                f"glossary.request label={request_label} attempt={attempt + 1}/3 split_depth={split_depth} "
                f"chunk_chars={len(chunk)} chunk_lines={chunk.count(chr(10)) + 1} "
                f"chunk_sha256={hashlib.sha256(chunk.encode('utf-8')).hexdigest()[:16]}"
            )
        try:
            response_text = client.chat(
                prompt_prefix + "\n\n原文语料：\n" + chunk,
                max_tokens=max_tokens,
            )
            result = _json_list(response_text)
            if diagnostic_log:
                diagnostic_log(f"glossary.response label={request_label} rows={len(result)}")
            return result
        except (ApiError, ValueError) as exc:
            last_error = exc
            message = str(exc).lower()
            if diagnostic_log:
                diagnostic_log(
                    f"glossary.error label={request_label} attempt={attempt + 1}/3 "
                    f"error_type={type(exc).__name__} error={exc}"
                )
                if isinstance(exc, ValueError) and response_text:
                    diagnostic_log(
                        f"glossary.invalid_json label={request_label} response_chars={len(response_text)} "
                        f"response_tail={response_text[-4000:]}"
                    )
            context_error = any(
                word in message
                for word in ("context", "too many tokens", "maximum", "请求过长", "输出达到上限")
            )
            if context_error and split_depth < 5 and "\n" in chunk:
                lines = chunk.splitlines()
                midpoint = len(lines) // 2
                if diagnostic_log:
                    diagnostic_log(
                        f"glossary.split label={request_label} split_depth={split_depth} "
                        f"left_lines={midpoint} right_lines={len(lines) - midpoint}"
                    )
                return _request_chunk(
                    client, prompt_prefix, "\n".join(lines[:midpoint]), cancel_event=cancel_event,
                    max_tokens=max_tokens,
                    split_depth=split_depth + 1, diagnostic_log=diagnostic_log,
                    request_label=request_label + ".left",
                ) + _request_chunk(
                    client, prompt_prefix, "\n".join(lines[midpoint:]), cancel_event=cancel_event,
                    max_tokens=max_tokens,
                    split_depth=split_depth + 1, diagnostic_log=diagnostic_log,
                    request_label=request_label + ".right",
                )
            if attempt < 2:
                if diagnostic_log:
                    diagnostic_log(f"glossary.retry label={request_label} delay={2**attempt}s")
                time.sleep(2**attempt)
    raise RuntimeError(str(last_error or "术语请求失败"))


def _parallel_stage(
    client: OpenAICompatibleClient,
    prompt: str,
    chunks: list[str],
    workers: int,
    cancel_event: threading.Event | None,
    log: Callable[[str], None] | None,
    diagnostic_log: Callable[[str], None] | None,
    label: str,
    max_tokens: int | None,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(chunks)))) as executor:
        futures = {
            executor.submit(
                _request_chunk,
                client,
                prompt,
                chunk,
                cancel_event=cancel_event,
                max_tokens=max_tokens,
                diagnostic_log=diagnostic_log,
                request_label=f"{label}:{index}/{len(chunks)}",
            ): index
            for index, chunk in enumerate(chunks, 1)
        }
        for future in as_completed(futures):
            _check_cancel(cancel_event)
            index = futures[future]
            result = future.result()
            rows.extend(result)
            if log:
                log(f"{label}分块 {index}/{len(chunks)} 完成，得到 {len(result)} 条候选。")
    return rows


def _merge_by_key(existing: Iterable[dict[str, object]], generated: Iterable[dict[str, object]], key: str) -> list[dict[str, object]]:
    merged: dict[str, dict[str, object]] = {}
    for source in (existing, generated):
        for row in source:
            identity = str(row.get(key, "")).strip()
            if not identity:
                continue
            folded = identity.casefold()
            if folded not in merged:
                merged[folded] = dict(row)
            else:
                current = merged[folded]
                for name, value in row.items():
                    if not current.get(name) and value:
                        current[name] = value
    return list(merged.values())


def generate_glossary(
    items: list[TranslationItem],
    glossary_path: str | Path,
    settings: AppSettings,
    api_key: str,
    *,
    cancel_event: threading.Event | None = None,
    log: Callable[[str], None] | None = None,
    diagnostic_log: Callable[[str], None] | None = None,
) -> dict[str, object]:
    lines = [
        f"[{item.type} | {item.info}] {item.original}"
        for item in items
        if item.category is not ImportCategory.COPY and item.original.strip()
    ]
    if not lines:
        raise ValueError("工作簿中没有可分析文本。")
    corpus = "\n".join(lines)
    chunks = _chunks(lines)
    max_tokens = settings.glossary_api_max_tokens or None
    if diagnostic_log:
        diagnostic_log(
            f"glossary.start source_rows={len(lines)} corpus_chars={len(corpus)} chunks={len(chunks)} "
            f"workers={settings.glossary_api_threads} model={settings.glossary_api_model} "
            f"max_tokens={max_tokens}"
        )
    client = OpenAICompatibleClient(
        settings.glossary_api_base_url,
        api_key,
        settings.glossary_api_model,
        settings.glossary_api_timeout,
        diagnostic_log,
    )
    character_prompt = """分析日文游戏语料中的人物。只输出 JSON 数组，每项包含：
original_name, translated_name, aliases(字符串数组), gender, age, personality,
speech_style, pronouns, speech_quirks, additional_info。
只收录语料中确实出现的人物；译名使用简体中文；无法判断的字段用空字符串。"""
    characters = _parallel_stage(
        client,
        character_prompt,
        chunks,
        settings.glossary_api_threads,
        cancel_event,
        log,
        diagnostic_log,
        "角色分析",
        max_tokens,
    )
    normalized_characters: list[dict[str, object]] = []
    character_fields = (
        "original_name", "translated_name", "aliases", "gender", "age", "personality",
        "speech_style", "pronouns", "speech_quirks", "additional_info",
    )
    for row in characters:
        original = str(row.get("original_name", "")).strip()
        if not original or original not in corpus:
            continue
        normalized = {name: row.get(name, [] if name == "aliases" else "") for name in character_fields}
        if not isinstance(normalized["aliases"], list):
            normalized["aliases"] = [str(normalized["aliases"])] if normalized["aliases"] else []
        normalized_characters.append(normalized)
    normalized_characters = _merge_by_key([], normalized_characters, "original_name")
    reference = json.dumps(normalized_characters, ensure_ascii=False)[:120_000]
    entity_prompt = f"""分析日文游戏语料中的专有名词、地点、组织、道具、技能和关键概念。
只输出 JSON 数组，每项包含 src, dst, info。src 必须是语料原文，dst 使用简体中文。
不要重复人物；候选词应至少在完整语料中出现两次。
人物参考：{reference}"""
    entities = _parallel_stage(
        client,
        entity_prompt,
        chunks,
        settings.glossary_api_threads,
        cancel_event,
        log,
        diagnostic_log,
        "实体分析",
        max_tokens,
    )
    normalized_entities = []
    for row in entities:
        src = str(row.get("src", "")).strip()
        dst = str(row.get("dst", "")).strip()
        if len(src) < 2 or not dst or corpus.count(src) < 2:
            continue
        normalized_entities.append({"src": src, "dst": dst, "info": str(row.get("info", ""))})
    path = Path(glossary_path)
    existing = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    old_characters = existing.get("characterization_data", []) if isinstance(existing, dict) else []
    old_terms = existing.get("prompt_dictionary_data", []) if isinstance(existing, dict) else []
    merged_characters = _merge_by_key(old_characters, normalized_characters, "original_name")
    character_terms = [
        {
            "src": str(row.get("original_name", "")),
            "dst": str(row.get("translated_name", "")),
            "info": str(row.get("additional_info", "人物")) or "人物",
        }
        for row in merged_characters
        if row.get("original_name") and row.get("translated_name")
    ]
    rules = dict(RULE_DEFAULTS)
    rules["characterization_data"] = merged_characters
    rules["prompt_dictionary_data"] = _merge_by_key(old_terms, character_terms + normalized_entities, "src")
    _atomic_json(path, rules)
    if diagnostic_log:
        diagnostic_log(
            f"glossary.complete path={path.resolve()} characters={len(merged_characters)} "
            f"terms={len(rules['prompt_dictionary_data'])}"
        )
    if log:
        log(f"术语生成完成：人物 {len(merged_characters)}，术语 {len(rules['prompt_dictionary_data'])}。")
    return rules
