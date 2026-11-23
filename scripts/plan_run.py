#!/usr/bin/env python3
"""在启动任何模型前，计算一次短剧运行计划。

输出只包含增量决策，不读取转写正文或图片内容。命中 noop 时，调用方不得
启动 Whisper、视觉模型、剧情模型或脚本生成器。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from path_guard import OutputPathError, resolve_output_path

DEFAULTS = {
    "skill_version": "2026.11-production-control-v4-ocr-first",
    "image_rule_version": "2026.11-image-selection-v2",
    "script_template_version": "2026.11-seedance-prompt-v1-platform-boundary",
    "analysis_model_version": "vision-caption-ocr-v1-optional-ollama-asr",
}


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def episode_hashes(data: dict[str, Any]) -> dict[str, str]:
    return {
        str(item.get("episode_key")): str(item.get("sha256"))
        for item in data.get("episodes", [])
        if isinstance(item, dict) and item.get("episode_key") and item.get("sha256")
    }


def config_fingerprint(values: dict[str, str]) -> str:
    raw = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def build_plan(
    source: Path,
    cache: Path,
    output: Path,
    versions: dict[str, str],
) -> dict[str, Any]:
    started = time.perf_counter()
    cache_manifest = read_json(cache / "manifest.json")
    generation = read_json(output / "生成清单.json")
    run_manifest = read_json(output / "run_manifest.json")
    previous_source = read_json(output / "source_manifest.json")
    current_hashes = episode_hashes(cache_manifest)
    previous_hashes = episode_hashes(previous_source)
    changed = sorted(
        key for key in set(current_hashes) | set(previous_hashes)
        if current_hashes.get(key) != previous_hashes.get(key)
    )
    output_ready = (
        output.is_dir()
        and generation.get("status") == "current"
        and run_manifest.get("status") == "current"
    )
    version_changes = {
        "skill_version": generation.get("skill_version") != versions["skill_version"],
        "image_rule_version": generation.get("image_rule_version") != versions["image_rule_version"],
        "script_template_version": generation.get("script_template_version") != versions["script_template_version"],
        "analysis_model_version": generation.get("analysis_model_version") != versions["analysis_model_version"],
    }
    source_ready = cache_manifest.get("status") == "ready" and bool(current_hashes)
    if not source_ready:
        mode = "blocked"
        stages = []
        reason = "source_manifest_not_ready"
    elif not output_ready or not previous_hashes:
        mode = "full"
        stages = ["asr", "facts", "assets", "scripts", "packages", "verify"]
        reason = "no_verified_current_output"
    elif changed:
        mode = "incremental"
        stages = ["asr", "facts", "assets", "scripts", "packages", "verify"]
        reason = "source_episode_changed"
    elif not generation.get("delivery_fingerprint"):
        mode = "verify_required"
        stages = ["fast_verify"]
        reason = "delivery_fingerprint_missing"
    elif version_changes["analysis_model_version"]:
        mode = "incremental"
        stages = ["facts", "assets", "scripts", "packages", "verify"]
        reason = "analysis_model_changed"
    elif version_changes["image_rule_version"]:
        mode = "incremental"
        stages = ["assets", "scripts", "packages", "verify"]
        reason = "image_rule_changed"
    elif version_changes["script_template_version"]:
        mode = "incremental"
        stages = ["scripts", "packages", "verify"]
        reason = "script_template_changed"
    elif version_changes["skill_version"]:
        mode = "incremental"
        stages = ["verify"]
        reason = "skill_version_changed"
    else:
        mode = "noop"
        stages = ["fast_verify"]
        reason = "all_fingerprints_match"
    return {
        "schema": 1,
        "source": str(source),
        "output": str(output),
        "mode": mode,
        "reason": reason,
        "changed_episodes": changed,
        "stages": stages,
        "versions": versions,
        "config_fingerprint": config_fingerprint(versions),
        "cache_episodes": len(current_hashes),
        "previous_episodes": len(previous_hashes),
        "elapsed_seconds": round(time.perf_counter() - started, 4),
        "model_calls_allowed": mode not in {"noop", "verify_required"},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="计算短剧增量运行计划，不启动模型")
    parser.add_argument("source", type=Path)
    parser.add_argument("--cache", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--skill-version", default=DEFAULTS["skill_version"])
    parser.add_argument("--image-rule-version", default=DEFAULTS["image_rule_version"])
    parser.add_argument("--script-template-version", default=DEFAULTS["script_template_version"])
    parser.add_argument("--analysis-model-version", default=DEFAULTS["analysis_model_version"])
    args = parser.parse_args()
    source = args.source.expanduser().resolve()
    cache = (args.cache or source / ".short-drama-cache").expanduser().resolve()
    try:
        output = resolve_output_path(source, args.output)
    except OutputPathError as exc:
        print(json.dumps({"mode": "blocked", "reason": "output_outside_source", "detail": str(exc)}, ensure_ascii=False, indent=2))
        return 2
    versions = {
        "skill_version": args.skill_version,
        "image_rule_version": args.image_rule_version,
        "script_template_version": args.script_template_version,
        "analysis_model_version": args.analysis_model_version,
    }
    plan = build_plan(source, cache, output, versions)
    print(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0 if plan["mode"] != "blocked" else 2


if __name__ == "__main__":
    raise SystemExit(main())

