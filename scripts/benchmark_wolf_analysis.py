from __future__ import annotations

import argparse
import json
import multiprocessing
import statistics
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from wolf_editor import EditorInfo, analyze_auto_export, analyze_translation_safety  # noqa: E402
from wolf_tools import load_items  # noqa: E402


_IGNORED_REPORT_FIELDS = frozenset({"schema", "engine", "metrics"})


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Cold-process benchmark for WOLFLator Auto analysis."
    )
    parser.add_argument("--auto-dir", type=Path, required=True)
    parser.add_argument("--items", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--report-output", type=Path)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--analysis-only", action="store_true")
    parser.add_argument("--median-limit", type=float, default=35.0)
    parser.add_argument("--max-limit", type=float, default=40.0)
    parser.add_argument("--expected-safe", type=int)
    parser.add_argument("--expected-protected", type=int)
    parser.add_argument("--child-result", type=Path, help=argparse.SUPPRESS)
    return parser


def _normalized(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: _normalized(item)
            for key, item in value.items()
            if key not in _IGNORED_REPORT_FIELDS
        }
    if isinstance(value, list):
        return [_normalized(item) for item in value]
    return value


def _single_run(args: argparse.Namespace) -> dict[str, object]:
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    editor_data = baseline["editor"]
    version = str(editor_data["version"])
    editor = EditorInfo(
        Path(str(editor_data["path"])),
        version,
        tuple(map(int, version.split("."))),
        str(editor_data["sha256"]),
    )
    items = load_items(args.items)
    candidates = {
        item.key: item.translation
        for item in items
        if item.translation and item.translation != item.original
    }
    started = time.perf_counter()
    analysis = analyze_auto_export(
        args.auto_dir,
        items,
        editor,
        input_hash=str(baseline["input_hash"]),
    )
    safety = None
    if not args.analysis_only:
        safety = analyze_translation_safety(
            args.auto_dir,
            items,
            candidates,
            "warn",
            analysis=analysis,
        )
    json.dumps(
        {"analysis": analysis, "safety": safety},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    duration = time.perf_counter() - started
    return {"duration": duration, "analysis": analysis, "safety": safety}


def _child_command(args: argparse.Namespace, output: Path) -> list[str]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--auto-dir",
        str(args.auto_dir),
        "--items",
        str(args.items),
        "--baseline",
        str(args.baseline),
        "--child-result",
        str(output),
    ]
    if args.analysis_only:
        command.append("--analysis-only")
    return command


def _reason_distribution(safety: object) -> dict[str, int]:
    if not isinstance(safety, dict) or not isinstance(safety.get("reasons"), dict):
        return {}
    return dict(sorted(Counter(
        str(reason)
        for reasons in safety["reasons"].values()
        if isinstance(reasons, list)
        for reason in reasons
    ).items()))


def main() -> int:
    args = _parser().parse_args()
    if args.child_result:
        payload = _single_run(args)
        args.child_result.parent.mkdir(parents=True, exist_ok=True)
        args.child_result.write_text(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        return 0
    if args.runs < 1:
        raise SystemExit("--runs must be positive")

    payloads: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory(prefix="wolflator-benchmark-") as directory:
        root = Path(directory)
        for index in range(args.runs):
            child_output = root / f"run-{index + 1}.json"
            completed = subprocess.run(
                _child_command(args, child_output),
                text=True,
                capture_output=True,
                check=False,
            )
            if completed.returncode:
                sys.stderr.write(completed.stdout)
                sys.stderr.write(completed.stderr)
                return completed.returncode
            payloads.append(json.loads(child_output.read_text(encoding="utf-8")))

    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    analyses = [payload["analysis"] for payload in payloads]
    safeties = [payload["safety"] for payload in payloads]
    durations = [float(payload["duration"]) for payload in payloads]
    normalized = [_normalized(analysis) for analysis in analyses]
    report_equal = all(report == _normalized(baseline) for report in normalized)
    deterministic = all(report == normalized[0] for report in normalized[1:])
    safety_deterministic = all(safety == safeties[0] for safety in safeties[1:])
    last_analysis = analyses[-1]
    last_safety = safeties[-1]
    replay = last_safety.get("replay", {}) if isinstance(last_safety, dict) else {}
    safe = int(replay.get("safe_changes", 0)) if isinstance(replay, dict) else 0
    protected = int(replay.get("protected_changes", 0)) if isinstance(replay, dict) else 0
    equivalence_fields = (
        "control_flow_equivalent",
        "data_effects_equivalent",
        "condition_results_equivalent",
        "resource_targets_equivalent",
    )
    replay_equivalent = isinstance(replay, dict) and all(
        replay.get(field) is True for field in equivalence_fields
    ) and not replay.get("differences")
    median = statistics.median(durations)
    maximum = max(durations)
    result = {
        "runs": durations,
        "median_seconds": median,
        "max_seconds": maximum,
        "baseline_equal": report_equal,
        "reports_deterministic": deterministic,
        "safety_deterministic": safety_deterministic,
        "replay_equivalent": replay_equivalent,
        "dependencies": len(last_analysis.get("dependencies", ())),
        "block_evaluations": dict(last_analysis.get("global_string_flow", {})).get(
            "block_evaluations", 0
        ),
        "safe_changes": safe,
        "protected_changes": protected,
        "reason_distribution": _reason_distribution(last_safety),
    }
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    if args.report_output:
        args.report_output.parent.mkdir(parents=True, exist_ok=True)
        args.report_output.write_text(
            json.dumps(last_analysis, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

    accepted = (
        report_equal
        and deterministic
        and safety_deterministic
        and replay_equivalent
        and median <= args.median_limit
        and maximum <= args.max_limit
    )
    if args.expected_safe is not None:
        accepted &= safe == args.expected_safe
    if args.expected_protected is not None:
        accepted &= protected == args.expected_protected
    return 0 if accepted else 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
