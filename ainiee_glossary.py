from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from itertools import count
from pathlib import Path
from typing import Callable, Iterable

from ainiee_runtime import _atomic_json, _check_cancel
from ainiee_translation import RULE_DEFAULTS
from models import AppSettings, ImportCategory, TranslationItem


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def _read_response_body(response, deadline: float) -> bytes:
    def set_remaining_timeout(remaining: float) -> None:
        fp = getattr(response, "fp", None)
        raw = getattr(fp, "raw", None)
        sock = getattr(raw, "_sock", None)
        if callable(getattr(sock, "settimeout", None)):
            # ponytail: CPython urllib has no public socket hook; replace the transport if other runtimes are supported.
            sock.settimeout(max(0.001, remaining))

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("API request deadline exceeded")
        return value

    read1 = getattr(response, "read1", None)
    if not callable(getattr(type(response), "read1", None)):
        set_remaining_timeout(remaining())
        data = response.read()
        remaining()
        return data

    chunks: list[bytes] = []
    while True:
        set_remaining_timeout(remaining())
        chunk = read1(64 * 1024)
        remaining()
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


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
        deadline = started + self.timeout
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
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("API request deadline exceeded")
            with urllib.request.urlopen(request, timeout=remaining) as response:
                raw = _read_response_body(response, deadline)
                status = response.status if isinstance(getattr(response, "status", None), int) else 200
        except urllib.error.HTTPError as exc:
            try:
                detail_raw = _read_response_body(exc.fp or exc, deadline)
            except TimeoutError:
                detail_raw = b""
            detail = detail_raw.decode("utf-8", errors="replace")[-2000:]
            if self.diagnostic_log:
                self.diagnostic_log(
                    f"api.error id={request_id} kind=http status={exc.code} "
                    f"duration={time.monotonic() - started:.3f}s body={detail}"
                )
            raise ApiError(f"API HTTP {exc.code}: {detail}", exc.code) from exc
        except TimeoutError as exc:
            if self.diagnostic_log:
                self.diagnostic_log(
                    f"api.error id={request_id} kind=timeout limit={self.timeout}s "
                    f"duration={time.monotonic() - started:.3f}s error={exc}"
                )
            raise ApiError(f"API 请求超过总时限（{self.timeout} 秒）。") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                if self.diagnostic_log:
                    self.diagnostic_log(
                        f"api.error id={request_id} kind=timeout limit={self.timeout}s "
                        f"duration={time.monotonic() - started:.3f}s error={exc}"
                    )
                raise ApiError(f"API 请求超过总时限（{self.timeout} 秒）。") from exc
            if self.diagnostic_log:
                self.diagnostic_log(
                    f"api.error id={request_id} kind=connection error_type={type(exc).__name__} "
                    f"duration={time.monotonic() - started:.3f}s error={exc}"
                )
            raise ApiError(f"API 连接失败: {exc}") from exc
        except OSError as exc:
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


def _chunks(lines: list[str], max_chars: int = 500_000, overlap: int = 10) -> list[str]:
    if max_chars < 1:
        raise ValueError("术语输入分块字符数必须大于 0。")
    chunks: list[str] = []
    start = 0
    while start < len(lines):
        end = start
        size = 0
        while end < len(lines):
            candidate_size = size + (1 if end > start else 0) + len(lines[end])
            if end > start and candidate_size > max_chars:
                break
            size = candidate_size
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
    if not isinstance(data, list):
        raise ValueError("术语模型没有返回 JSON 数组。")
    if not all(isinstance(row, dict) for row in data):
        raise ValueError("术语模型返回的数组包含非对象项。")
    return data


def _repair_invalid_json_escapes(text: str) -> tuple[str, int]:
    output: list[str] = []
    in_string = False
    repairs = 0
    index = 0
    while index < len(text):
        char = text[index]
        if not in_string:
            output.append(char)
            if char == '"':
                in_string = True
            index += 1
            continue
        if char == '"':
            output.append(char)
            in_string = False
            index += 1
            continue
        if char != "\\":
            output.append(char)
            index += 1
            continue
        if index + 1 < len(text):
            escaped = text[index + 1]
            if escaped in '"\\/bfnrt':
                output.append(text[index:index + 2])
                index += 2
                continue
            if (
                escaped == "u"
                and index + 6 <= len(text)
                and all(char in "0123456789abcdefABCDEF" for char in text[index + 2:index + 6])
            ):
                output.append(text[index:index + 6])
                index += 6
                continue
        output.append("\\\\")
        repairs += 1
        index += 1
    return "".join(output), repairs


def _request_chunk(
    client: OpenAICompatibleClient,
    prompt_prefix: str,
    chunk: str,
    *,
    cancel_event: threading.Event | None,
    abort_event: threading.Event | None = None,
    max_tokens: int | None = None,
    split_depth: int = 0,
    diagnostic_log: Callable[[str], None] | None = None,
    request_label: str = "",
) -> list[dict[str, object]]:
    _check_cancel(cancel_event)
    _check_cancel(abort_event)
    last_error: Exception | None = None
    for attempt in range(3):
        _check_cancel(cancel_event)
        _check_cancel(abort_event)
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
            _check_cancel(cancel_event)
            _check_cancel(abort_event)
            try:
                result = _json_list(response_text)
            except json.JSONDecodeError:
                repaired_text, repair_count = _repair_invalid_json_escapes(response_text)
                if not repair_count:
                    raise
                result = _json_list(repaired_text)
                if diagnostic_log:
                    diagnostic_log(
                        f"glossary.json_escape_repaired label={request_label} repairs={repair_count} "
                        f"response_sha256={hashlib.sha256(response_text.encode('utf-8')).hexdigest()[:16]}"
                    )
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
                _check_cancel(cancel_event)
                _check_cancel(abort_event)
                lines = chunk.splitlines()
                midpoint = len(lines) // 2
                if diagnostic_log:
                    diagnostic_log(
                        f"glossary.split label={request_label} split_depth={split_depth} "
                        f"left_lines={midpoint} right_lines={len(lines) - midpoint}"
                    )
                return _request_chunk(
                    client, prompt_prefix, "\n".join(lines[:midpoint]), cancel_event=cancel_event,
                    abort_event=abort_event,
                    max_tokens=max_tokens,
                    split_depth=split_depth + 1, diagnostic_log=diagnostic_log,
                    request_label=request_label + ".left",
                ) + _request_chunk(
                    client, prompt_prefix, "\n".join(lines[midpoint:]), cancel_event=cancel_event,
                    abort_event=abort_event,
                    max_tokens=max_tokens,
                    split_depth=split_depth + 1, diagnostic_log=diagnostic_log,
                    request_label=request_label + ".right",
                )
            if attempt < 2:
                _check_cancel(cancel_event)
                _check_cancel(abort_event)
                if diagnostic_log:
                    diagnostic_log(f"glossary.retry label={request_label} delay={2**attempt}s")
                time.sleep(2**attempt)
                _check_cancel(cancel_event)
                _check_cancel(abort_event)
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
    abort_event = threading.Event()
    worker_count = max(1, min(workers, len(chunks)))
    executor = ThreadPoolExecutor(max_workers=worker_count)
    pending_chunks = iter(enumerate(chunks, 1))
    futures: dict[object, int] = {}

    def submit_next() -> bool:
        try:
            index, chunk = next(pending_chunks)
        except StopIteration:
            return False
        future = executor.submit(
            _request_chunk,
            client,
            prompt,
            chunk,
            cancel_event=cancel_event,
            abort_event=abort_event,
            max_tokens=max_tokens,
            diagnostic_log=diagnostic_log,
            request_label=f"{label}:{index}/{len(chunks)}",
        )
        futures[future] = index
        return True

    try:
        for _ in range(worker_count):
            submit_next()
        while futures:
            _check_cancel(cancel_event)
            done, _ = wait(tuple(futures), return_when=FIRST_COMPLETED)
            for future in done:
                if future.exception() is not None:
                    future.result()
            for future in done:
                index = futures.pop(future)
                result = future.result()
                rows.extend(result)
                if log:
                    log(f"{label}分块 {index}/{len(chunks)} 完成，得到 {len(result)} 条候选。")
            for _ in done:
                submit_next()
    except BaseException:
        abort_event.set()
        for future in futures:
            future.cancel()
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
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
    chunks = _chunks(lines, max_chars=settings.glossary_chunk_chars)
    max_tokens = settings.glossary_api_max_tokens or None
    if diagnostic_log:
        diagnostic_log(
            f"glossary.start source_rows={len(lines)} corpus_chars={len(corpus)} chunks={len(chunks)} "
            f"workers={settings.glossary_api_threads} model={settings.glossary_api_model} "
            f"chunk_chars={settings.glossary_chunk_chars} max_tokens={max_tokens}"
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
