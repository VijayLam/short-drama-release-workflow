#!/usr/bin/env python3
"""限制正式产物路径，防止图片和发行包散落到源目录之外。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


class OutputPathError(ValueError):
    pass


def resolve_output_path(source: Path, requested: Path | None = None) -> Path:
    source = source.expanduser().resolve()
    output = (requested or source / "二次创作发行").expanduser().resolve()
    try:
        output.relative_to(source)
    except ValueError as exc:
        raise OutputPathError(f"正式输出必须位于源视频文件夹内:{output}") from exc
    if output == source:
        raise OutputPathError("正式输出不能直接写入源视频文件夹根目录")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="检查短剧正式输出路径")
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    try:
        output = resolve_output_path(args.source, args.output)
        print(json.dumps({"status": "passed", "source": str(args.source.expanduser().resolve()), "output": str(output)}, ensure_ascii=False))
        return 0
    except OutputPathError as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

