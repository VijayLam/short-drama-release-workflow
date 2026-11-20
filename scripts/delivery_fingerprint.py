#!/usr/bin/env python3
"""计算正式发行包的内容指纹，用于零工作复用前的安全检查。"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path
from typing import Any


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def compute(root: Path) -> dict[str, Any]:
    manifest = read_json(root / "生成清单.json")
    package_names = [
        str(item.get("package"))
        for item in manifest.get("packages", [])
        if isinstance(item, dict) and item.get("package")
    ]
    entries: list[tuple[str, str]] = []
    for package in sorted(set(package_names)):
        package_root = root / package
        if not package_root.is_dir():
            entries.append((f"{package}/<missing>", "MISSING"))
            continue
        for path in sorted(p for p in package_root.rglob("*") if p.is_file()):
            rel = path.relative_to(root).as_posix()
            entries.append((rel, sha256_file(path)))
    canonical = "\n".join(f"{path}\t{digest}" for path, digest in entries).encode()
    fingerprint = hashlib.sha256(canonical).hexdigest()
    return {"schema": 1, "fingerprint": fingerprint, "file_count": len(entries)}


def main() -> int:
    parser = argparse.ArgumentParser(description="计算正式发行包内容指纹")
    parser.add_argument("root", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    started = time.perf_counter()
    result = compute(args.root.expanduser().resolve())
    result["elapsed_seconds"] = round(time.perf_counter() - started, 4)
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else result["fingerprint"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

