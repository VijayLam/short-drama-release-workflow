#!/usr/bin/env python3
"""短剧工作流的零工作快速路径。

它只读取 manifest、生成清单和运行清单。命中 noop 时不启动任何模型，
也不读取视频、图片、ASR 正文或脚本正文。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from plan_run import DEFAULTS, build_plan


def main() -> int:
    parser = argparse.ArgumentParser(description="检查短剧是否可以零工作恢复")
    parser.add_argument("source", type=Path)
    parser.add_argument("--cache", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--verify", action="store_true", help="noop时再运行严格验收器")
    args = parser.parse_args()
    source = args.source.expanduser().resolve()
    cache = (args.cache or source / ".short-drama-cache").expanduser().resolve()
    output = (args.output or source / "二次创作发行").expanduser().resolve()
    plan = build_plan(source, cache, output, DEFAULTS)
    result = {"mode": plan["mode"], "reason": plan["reason"], "changed_episodes": plan["changed_episodes"], "elapsed_seconds": plan["elapsed_seconds"]}
    if args.verify and plan["mode"] == "noop":
        validator = Path(__file__).with_name("validate_delivery.py")
        check = subprocess.run(
            [sys.executable, str(validator), str(output), "--json"],
            text=True,
            capture_output=True,
            check=False,
        )
        result["validation_exit"] = check.returncode
        try:
            payload = json.loads(check.stdout)
            result["validation"] = {
                "status": payload.get("status"),
                "package_count": payload.get("package_count"),
                "expected_package_count": payload.get("expected_package_count"),
                "issue_count": payload.get("issue_count"),
                "unique_image_checks": payload.get("unique_image_checks"),
                "root_issue_sample": (payload.get("root_issues") or [])[:5],
            }
        except json.JSONDecodeError:
            result["validation"] = {"status": "invalid_validator_output"}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if plan["mode"] in {"noop", "incremental", "full"} else 2


if __name__ == "__main__":
    raise SystemExit(main())

