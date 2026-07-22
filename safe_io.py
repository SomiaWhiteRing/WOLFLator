from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator, TypeVar


TBusyError = TypeVar("TBusyError", bound="ResourceBusyError")
_RETRYABLE_WINDOWS_ERRORS = {5, 32}
_registry_guard = threading.Lock()


@dataclass
class _HeldLock:
    owner_thread: int
    depth: int
    stream: BinaryIO | None
    metadata: dict[str, object]


_held_locks: dict[str, _HeldLock] = {}


class ResourceBusyError(RuntimeError):
    resource_name = "资源"

    def __init__(self, path: str | Path, metadata: dict[str, object] | None = None):
        self.path = Path(path)
        self.metadata = metadata or {}
        details = []
        if self.metadata.get("pid"):
            details.append(f"PID {self.metadata['pid']}")
        if self.metadata.get("operation"):
            details.append(f"操作 {self.metadata['operation']}")
        if self.metadata.get("started_at"):
            details.append(f"开始于 {self.metadata['started_at']}")
        suffix = "（" + "，".join(details) + "）" if details else ""
        super().__init__(f"{self.resource_name}正在被其他任务使用: {self.path}{suffix}")


class ProjectBusyError(ResourceBusyError):
    resource_name = "项目"


class RuntimeBusyError(ResourceBusyError):
    resource_name = "AiNiee 运行时"


def _canonical(path: Path) -> str:
    value = str(path.resolve())
    return os.path.normcase(value) if os.name == "nt" else value


def _retryable_replace_error(error: OSError) -> bool:
    return os.name == "nt" and (
        getattr(error, "winerror", None) in _RETRYABLE_WINDOWS_ERRORS
        or (isinstance(error, PermissionError) and error.errno in {5, 13})
    )


def replace_with_retry(source: str | Path, target: str | Path, *, timeout: float = 2.0) -> None:
    source_path = Path(source)
    target_path = Path(target)
    deadline = time.monotonic() + timeout
    delay = 0.01
    while True:
        try:
            os.replace(source_path, target_path)
            return
        except OSError as error:
            if not _retryable_replace_error(error) or time.monotonic() >= deadline:
                raise
            time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
            delay = min(delay * 2, 0.1)


def read_bytes_with_retry(path: str | Path, *, timeout: float = 2.0) -> bytes:
    source = Path(path)
    deadline = time.monotonic() + timeout
    delay = 0.01
    while True:
        try:
            return source.read_bytes()
        except OSError as error:
            if not _retryable_replace_error(error) or time.monotonic() >= deadline:
                raise
            time.sleep(min(delay, max(0.0, deadline - time.monotonic())))
            delay = min(delay * 2, 0.1)


def read_text_with_retry(
    path: str | Path,
    *,
    encoding: str = "utf-8",
    errors: str | None = None,
    timeout: float = 2.0,
) -> str:
    return read_bytes_with_retry(path, timeout=timeout).decode(encoding, errors or "strict")


def _unique_temporary(path: Path) -> tuple[int, Path]:
    path.parent.mkdir(parents=True, exist_ok=True)
    return tempfile.mkstemp(
        prefix=f".{path.name}.{os.getpid()}.",
        suffix=".tmp",
        dir=path.parent,
    )


def atomic_write_bytes(path: str | Path, value: bytes) -> Path:
    output = Path(path)
    descriptor, temporary = _unique_temporary(output)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        replace_with_retry(temporary, output)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise
    return output


def atomic_write_text(
    path: str | Path,
    value: str,
    *,
    encoding: str = "utf-8",
) -> Path:
    return atomic_write_bytes(path, value.encode(encoding))


def atomic_write_json(path: str | Path, value: object, *, indent: int | None = 2) -> Path:
    return atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=indent),
        encoding="utf-8",
    )


@contextmanager
def atomic_output_path(path: str | Path) -> Iterator[Path]:
    output = Path(path)
    descriptor, temporary = _unique_temporary(output)
    os.close(descriptor)
    temporary_path = Path(temporary)
    try:
        yield temporary_path
        with temporary_path.open("r+b") as stream:
            os.fsync(stream.fileno())
        replace_with_retry(temporary_path, output)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _try_os_lock(stream: BinaryIO) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_os(stream: BinaryIO) -> None:
    stream.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _read_lock_metadata(stream: BinaryIO) -> dict[str, object]:
    try:
        stream.seek(1)
        value = json.loads(stream.read().decode("utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, UnicodeDecodeError, ValueError):
        return {}


class ResourceLock:
    def __init__(
        self,
        lock_path: str | Path,
        operation: str,
        *,
        error_type: type[TBusyError] = ResourceBusyError,
        resource_path: str | Path | None = None,
    ):
        self.lock_path = Path(lock_path).resolve()
        self.operation = operation
        self.error_type = error_type
        self.resource_path = Path(resource_path).resolve() if resource_path else self.lock_path.parent
        self._key = _canonical(self.lock_path)
        self._entries = 0

    def __enter__(self) -> "ResourceLock":
        thread_id = threading.get_ident()
        metadata = {
            "pid": os.getpid(),
            "operation": self.operation,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "path": str(self.resource_path),
        }
        with _registry_guard:
            held = _held_locks.get(self._key)
            if held is not None:
                if held.owner_thread != thread_id:
                    raise self.error_type(self.resource_path, held.metadata)
                held.depth += 1
                self._entries += 1
                return self
            _held_locks[self._key] = _HeldLock(thread_id, 1, None, metadata)

        stream: BinaryIO | None = None
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            stream = self.lock_path.open("a+b", buffering=0)
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                os.fsync(stream.fileno())
            try:
                _try_os_lock(stream)
            except OSError as error:
                owner = _read_lock_metadata(stream)
                raise self.error_type(self.resource_path, owner) from error
            stream.seek(1)
            stream.truncate()
            stream.write(json.dumps(metadata, ensure_ascii=False).encode("utf-8"))
            os.fsync(stream.fileno())
            with _registry_guard:
                _held_locks[self._key].stream = stream
            self._entries += 1
            return self
        except Exception:
            if stream is not None:
                stream.close()
            with _registry_guard:
                _held_locks.pop(self._key, None)
            raise

    def __exit__(self, _type, _value, _traceback) -> None:
        if not self._entries:
            return
        self._entries -= 1
        stream: BinaryIO | None = None
        with _registry_guard:
            held = _held_locks[self._key]
            held.depth -= 1
            if held.depth == 0:
                stream = held.stream
                del _held_locks[self._key]
        if stream is not None:
            try:
                _unlock_os(stream)
            finally:
                stream.close()


def _project_dir(value: str | Path) -> Path:
    path = Path(value).resolve()
    return path.parent if path.name.lower() == "project.json" else path


def project_lock(value: str | Path, operation: str) -> ResourceLock:
    root = _project_dir(value)
    return ResourceLock(
        root / ".wolflator.lock",
        operation,
        error_type=ProjectBusyError,
        resource_path=root,
    )


def runtime_lock(value: str | Path, operation: str) -> ResourceLock:
    root = Path(value).resolve()
    return ResourceLock(
        root / ".wolflator-runtime.lock",
        operation,
        error_type=RuntimeBusyError,
        resource_path=root,
    )


def package_lock(value: str | Path, operation: str) -> ResourceLock:
    root = Path(value).resolve()
    return ResourceLock(
        root / ".wolflator-packages.lock",
        operation,
        error_type=RuntimeBusyError,
        resource_path=root,
    )


def lock_status(lock_path: str | Path) -> tuple[bool, dict[str, object]]:
    path = Path(lock_path).resolve()
    key = _canonical(path)
    with _registry_guard:
        held = _held_locks.get(key)
        if held is not None:
            return True, dict(held.metadata)
    if not path.exists():
        return False, {}
    try:
        stream = path.open("r+b", buffering=0)
    except FileNotFoundError:
        return False, {}
    except PermissionError:
        return True, {}
    try:
        try:
            _try_os_lock(stream)
        except OSError:
            return True, _read_lock_metadata(stream)
        else:
            _unlock_os(stream)
            return False, {}
    finally:
        stream.close()


def project_lock_status(value: str | Path) -> tuple[bool, dict[str, object]]:
    root = _project_dir(value)
    return lock_status(root / ".wolflator.lock")
