from __future__ import annotations

import dataclasses
import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


MANIFEST_SCHEMA = 7
DEFAULT_EXTERNAL_FILE_LIMIT_KB = 128
MAX_EXTERNAL_FILE_LIMIT_KB = 1_048_576
SUSPICIOUS_IDENTIFIER_ACTIONS = {"ignore", "warn", "protect"}
LOGIC_UNKNOWN_POLICIES = {"block", "warn"}


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
class ImportProtectionRules:
    protect_external_references: bool = True
    protect_paths_and_commands: bool = True
    allow_copy_condition_groups: bool = True
    protect_logic_references: bool = True
    logic_unknown_policy: str = "warn"
    suspicious_identifiers: str = "warn"


def legacy_import_protection() -> ImportProtectionRules:
    # ponytail: Old projects keep their previous AiNiee selection until the user opts in.
    return ImportProtectionRules(allow_copy_condition_groups=False)


def default_export_scope() -> ImportScope:
    return ImportScope(display=True, external=True, optional_name=True, halfwidth=True, filename=True)


def legacy_export_scope() -> ImportScope:
    return ImportScope(display=True, external=False, optional_name=True, halfwidth=True, filename=True)


def _require_fields(data: object, expected: set[str], label: str) -> None:
    if not isinstance(data, dict):
        raise ValueError(f"{label}不是对象。")
    missing = expected - data.keys()
    extra = data.keys() - expected
    if missing or extra:
        raise ValueError(
            f"{label}字段不匹配: missing={sorted(missing)}, extra={sorted(extra)}"
        )


@dataclass
class AppSettings:
    wolf_tool_path: str = ""
    wolf_editor_path: str = ""
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
    translation_chunk_mode: str = "token"
    translation_token_limit: int = 256
    translation_line_limit: int = 8
    translation_retry_min_lines: int = 1
    translation_rounds: int = 6
    glossary_api_base_url: str = ""
    glossary_api_model: str = ""
    glossary_api_key_blob: str = ""
    glossary_api_timeout: int = 600
    glossary_api_threads: int = 3
    glossary_chunk_chars: int = 500_000
    glossary_api_max_tokens: int = 393_216
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
        _require_fields(
            data,
            {"status", "started_at", "finished_at", "input_hash", "error", "artifacts"},
            "阶段记录",
        )
        string_fields = ("started_at", "finished_at", "input_hash", "error")
        if any(not isinstance(data[name], str) for name in string_fields):
            raise ValueError("阶段记录文本字段类型不匹配。")
        artifacts = data["artifacts"]
        if not isinstance(artifacts, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in artifacts.items()
        ):
            raise ValueError("阶段记录 artifacts 必须是字符串映射。")
        return cls(
            status=StageStatus(data["status"]),
            started_at=data["started_at"],
            finished_at=data["finished_at"],
            input_hash=data["input_hash"],
            error=data["error"],
            artifacts=artifacts,
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
        _require_fields(
            data,
            {"version_id", "original_path", "created_at", "source_hash", "stages"},
            "版本清单",
        )
        if any(
            not isinstance(data[name], str)
            for name in ("version_id", "original_path", "created_at", "source_hash")
        ):
            raise ValueError("版本清单文本字段类型不匹配。")
        item = cls(
            version_id=data["version_id"],
            original_path=data["original_path"],
            created_at=data["created_at"],
            source_hash=data["source_hash"],
        )
        stages = data["stages"]
        if not isinstance(stages, dict):
            raise ValueError("版本清单 stages 不是对象。")
        unknown = set(stages) - {stage.value for stage in STAGE_ORDER}
        if unknown:
            raise ValueError(f"版本清单包含未知阶段: {sorted(unknown)}")
        item.stages = {
            key: StageRecord.from_dict(value)
            for key, value in stages.items()
        }
        return item


@dataclass
class ProjectManifest:
    project_id: str
    name: str
    schema: int = MANIFEST_SCHEMA
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    active_version: str = ""
    run_mode: RunMode = RunMode.ONE_CLICK
    export_scope: ImportScope = field(default_factory=default_export_scope)
    exclude_large_external_files: bool = True
    external_file_limit_kb: int = DEFAULT_EXTERNAL_FILE_LIMIT_KB
    translation_scope: ImportScope = field(default_factory=ImportScope)
    import_scope: ImportScope = field(default_factory=ImportScope)
    import_protection: ImportProtectionRules = field(default_factory=ImportProtectionRules)
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
        schema = data.get("schema")
        if type(schema) is not int or schema not in (1, 2, 3, 4, 5, 6, MANIFEST_SCHEMA):
            raise ValueError(f"不支持的项目清单 schema: {schema}")
        fields = {
            "project_id",
            "name",
            "schema",
            "created_at",
            "updated_at",
            "active_version",
            "run_mode",
            "translation_scope",
            "import_scope",
            "versions",
        }
        if schema >= 2:
            fields.add("export_scope")
        if schema >= 3:
            fields.update({"exclude_large_external_files", "external_file_limit_kb"})
        if schema >= 4:
            fields.add("import_protection")
        _require_fields(
            data,
            fields,
            "项目清单",
        )
        scope_fields = {"display", "external", "optional_name", "halfwidth", "filename"}
        export_scope_data = (
            data["export_scope"] if schema >= 2 else dataclasses.asdict(legacy_export_scope())
        )
        import_scope_data = data["import_scope"]
        translation_scope_data = data["translation_scope"]
        if any(
            not isinstance(scope, dict)
            for scope in (export_scope_data, import_scope_data, translation_scope_data)
        ):
            raise ValueError("项目范围不是对象。")
        _require_fields(export_scope_data, scope_fields, "导出范围")
        _require_fields(import_scope_data, scope_fields, "导入范围")
        _require_fields(translation_scope_data, scope_fields, "翻译范围")
        if any(
            type(value) is not bool
            for scope in (export_scope_data, import_scope_data, translation_scope_data)
            for value in scope.values()
        ):
            raise ValueError("项目范围值必须是布尔值。")
        if any(
            not isinstance(data[name], str)
            for name in ("project_id", "name", "created_at", "updated_at", "active_version")
        ):
            raise ValueError("项目清单文本字段类型不匹配。")
        exclude_large_external_files = data.get("exclude_large_external_files", True)
        external_file_limit_kb = data.get(
            "external_file_limit_kb", DEFAULT_EXTERNAL_FILE_LIMIT_KB
        )
        if type(exclude_large_external_files) is not bool:
            raise ValueError("大文件自动排除开关必须是布尔值。")
        if (
            type(external_file_limit_kb) is not int
            or not 1 <= external_file_limit_kb <= MAX_EXTERNAL_FILE_LIMIT_KB
        ):
            raise ValueError(
                f"外部文件大小上限必须是 1..{MAX_EXTERNAL_FILE_LIMIT_KB} KB 的整数。"
            )
        protection_data = (
            data["import_protection"]
            if schema >= 4
            else dataclasses.asdict(legacy_import_protection())
        )
        if schema >= 4 and isinstance(protection_data, dict):
            protection_data = dict(protection_data)
            # ponytail: Schema 4's standalone condition rule is subsumed by the
            # Editor-backed logic rule and is discarded on the next save.
            protection_data.pop("protect_standalone_conditions", None)
            protection_data.setdefault("protect_logic_references", True)
            if schema < 6:
                protection_data.setdefault("logic_unknown_policy", "block")
        protection_fields = {
            "protect_external_references",
            "protect_paths_and_commands",
            "allow_copy_condition_groups",
            "protect_logic_references",
            "logic_unknown_policy",
            "suspicious_identifiers",
        }
        _require_fields(protection_data, protection_fields, "导入保护规则")
        if any(
            type(protection_data[name]) is not bool
            for name in protection_fields - {"logic_unknown_policy", "suspicious_identifiers"}
        ):
            raise ValueError("导入保护规则开关必须是布尔值。")
        if protection_data["logic_unknown_policy"] not in LOGIC_UNKNOWN_POLICIES:
            raise ValueError("未知事件逻辑策略必须是 block 或 warn。")
        if protection_data["suspicious_identifiers"] not in SUSPICIOUS_IDENTIFIER_ACTIONS:
            raise ValueError("可疑标识符策略必须是 ignore、warn 或 protect。")
        item = cls(
            project_id=data["project_id"],
            name=data["name"],
            schema=MANIFEST_SCHEMA,
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            active_version=data["active_version"],
            run_mode=RunMode(data["run_mode"]),
            export_scope=ImportScope(**export_scope_data),
            exclude_large_external_files=exclude_large_external_files,
            external_file_limit_kb=external_file_limit_kb,
            translation_scope=ImportScope(**translation_scope_data),
            import_scope=ImportScope(**import_scope_data),
            import_protection=ImportProtectionRules(**protection_data),
        )
        versions = data["versions"]
        if not isinstance(versions, dict):
            raise ValueError("项目清单 versions 不是对象。")
        if any(not isinstance(key, str) for key in versions):
            raise ValueError("项目清单版本键不是字符串。")
        item.versions = {
            key: VersionManifest.from_dict(value)
            for key, value in versions.items()
        }
        mismatched = [key for key, version in item.versions.items() if key != version.version_id]
        if mismatched:
            raise ValueError(f"版本键与 version_id 不一致: {mismatched}")
        if item.active_version not in item.versions:
            raise ValueError("项目活动版本不存在。")
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
    copy_category: ImportCategory | None = None
    control_signature: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = dataclasses.asdict(self)
        data["category"] = self.category.value
        data["copy_category"] = self.copy_category.value if self.copy_category else None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TranslationItem":
        _require_fields(
            data,
            {
                "key",
                "original",
                "translation",
                "context",
                "stage",
                "code",
                "flag",
                "type",
                "info",
                "category",
                "copy_category",
                "control_signature",
            },
            "翻译条目",
        )
        string_fields = (
            "key", "original", "translation", "context", "code", "flag", "type", "info"
        )
        if any(not isinstance(data[name], str) for name in string_fields):
            raise ValueError("翻译条目文本字段类型不匹配。")
        if type(data["stage"]) is not int:
            raise ValueError("翻译条目 stage 不是整数。")
        signature = data["control_signature"]
        if not isinstance(signature, list) or not all(isinstance(token, str) for token in signature):
            raise ValueError("翻译条目 control_signature 不是字符串数组。")
        if data["copy_category"] is not None and not isinstance(data["copy_category"], str):
            raise ValueError("翻译条目 copy_category 类型不匹配。")
        values = dict(data)
        values["category"] = ImportCategory(values["category"])
        copy_category = values["copy_category"]
        values["copy_category"] = ImportCategory(copy_category) if copy_category else None
        return cls(**values)


@dataclass
class ToolResult:
    command: list[str]
    return_code: int
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    console_output: str = ""
    console_history: list[str] = field(default_factory=list)
