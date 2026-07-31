from __future__ import annotations


PROJECT_SCHEMA = 1
ARTIFACT_EPOCH = 1
QA_SCHEMA = 1


def require_format(
    value: object,
    *,
    kind: str,
    version_key: str,
    version: int,
    label: str,
) -> dict[str, object]:
    if (
        not isinstance(value, dict)
        or value.get("kind") != kind
        or type(value.get(version_key)) is not int
        or value.get(version_key) != version
    ):
        raise ValueError(f"{label}格式不兼容。")
    return value
