from __future__ import annotations

import dataclasses
import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Stage(str, enum.Enum):
    COPY = "copy"
    UNPACK = "unpack"
    EXTRACT = "extract"
    GLOSSARY = "glossary"
    TRANSLATE = "translate"
    VALIDATE = "validate"
    IMPORT = "import"
    RELEASE = "release"


STAGE_ORDER = tuple(Stage)


class StageStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunMode(str, enum.Enum):
    ONE_CLICK = "one_click"
    STEP = "step"


class ImportCategory(str, enum.Enum):
    DISPLAY = "display"
    EXTERNAL = "external"
    OPTIONAL_NAME = "optional_name"
    HALFWIDTH = "halfwidth"
    FILENAME = "filename"
    COPY = "copy"


@dataclass
class ImportScope:
    display: bool = True
    external: bool = False
    optional_name: bool = False
    halfwidth: bool = False
    filename: bool = False

    def allows(self, category: ImportCategory | str) -> bool:
        category = ImportCategory(category)
        if category is ImportCategory.COPY:
            return False
        return bool(getattr(self, category.value))


@dataclass
class AppSettings:
    wolf_tool_path: str = ""
    ainiee_source: str = ""
    ascii_runner_dir: str = ""
    projects_root: str = ""
    api_base_url: str = ""
    api_model: str = ""
    api_key_blob: str = ""
    api_timeout: int = 120
    api_threads: int = 3
    api_rpm: int = 60
    api_tpm: int = 100_000
    license_accepted: bool = False
    last_project: str = ""


@dataclass
class StageRecord:
    status: StageStatus = StageStatus.PENDING
    started_at: str = ""
    finished_at: str = ""
    input_hash: str = ""
    error: str = ""
    artifacts: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StageRecord":
        return cls(
            status=StageStatus(data.get("status", StageStatus.PENDING.value)),
            started_at=str(data.get("started_at", "")),
            finished_at=str(data.get("finished_at", "")),
            input_hash=str(data.get("input_hash", "")),
            error=str(data.get("error", "")),
            artifacts=dict(data.get("artifacts", {})),
        )


@dataclass
class VersionManifest:
    version_id: str
    original_path: str
    created_at: str = field(default_factory=utc_now)
    source_hash: str = ""
    stages: dict[str, StageRecord] = field(default_factory=dict)

    def stage(self, stage: Stage) -> StageRecord:
        if stage.value not in self.stages:
            self.stages[stage.value] = StageRecord()
        return self.stages[stage.value]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VersionManifest":
        item = cls(
            version_id=str(data["version_id"]),
            original_path=str(data["original_path"]),
            created_at=str(data.get("created_at", utc_now())),
            source_hash=str(data.get("source_hash", "")),
        )
        item.stages = {
            key: StageRecord.from_dict(value)
            for key, value in dict(data.get("stages", {})).items()
        }
        return item


@dataclass
class ProjectManifest:
    project_id: str
    name: str
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    active_version: str = ""
    run_mode: RunMode = RunMode.ONE_CLICK
    import_scope: ImportScope = field(default_factory=ImportScope)
    versions: dict[str, VersionManifest] = field(default_factory=dict)

    @property
    def version(self) -> VersionManifest:
        if not self.active_version or self.active_version not in self.versions:
            raise ValueError("项目没有活动版本。")
        return self.versions[self.active_version]

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["run_mode"] = self.run_mode.value
        for version in data["versions"].values():
            for record in version["stages"].values():
                status = record.get("status")
                if isinstance(status, enum.Enum):
                    record["status"] = status.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectManifest":
        item = cls(
            project_id=str(data["project_id"]),
            name=str(data.get("name", data["project_id"])),
            created_at=str(data.get("created_at", utc_now())),
            updated_at=str(data.get("updated_at", utc_now())),
            active_version=str(data.get("active_version", "")),
            run_mode=RunMode(data.get("run_mode", RunMode.ONE_CLICK.value)),
            import_scope=ImportScope(**dict(data.get("import_scope", {}))),
        )
        item.versions = {
            key: VersionManifest.from_dict(value)
            for key, value in dict(data.get("versions", {})).items()
        }
        return item


@dataclass
class TranslationItem:
    key: str
    original: str
    translation: str = ""
    context: str = ""
    stage: int = 0
    code: str = ""
    flag: str = ""
    type: str = ""
    info: str = ""
    category: ImportCategory = ImportCategory.DISPLAY
    control_signature: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["category"] = self.category.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TranslationItem":
        values = dict(data)
        values["category"] = ImportCategory(values.get("category", ImportCategory.DISPLAY.value))
        return cls(**values)


@dataclass
class ToolResult:
    command: list[str]
    return_code: int
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
