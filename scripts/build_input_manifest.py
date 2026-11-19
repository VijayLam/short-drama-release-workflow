#!/usr/bin/env python3
"""短剧工作流的轻量扫描、哈希缓存和受控并行运行器。

它只建立可复用的输入索引，不生成低质量正式图片，也不替代剧情分析。
下游执行器可读取 manifest.json，并按 episode_key 增量处理。
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}

@dataclass
class Episode:
    episode_key: str
    path: str
    relative_path: str
    size: int
    mtime_ns: int
    sha256: str
    duration: float | None
    width: int | None
    height: int | None
    fps: str | None


def natural_key(path: Path) -> tuple[Any, ...]:
    parts = re.split(r"(\d+)", path.name)
    return tuple(int(x) if x.isdigit() else x.lower() for x in parts)


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def ffprobe(path: Path) -> dict[str, Any]:
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate:format=duration",
        "-of", "json", str(path),
    ]
    try:
        raw = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
        data = json.loads(raw)
        stream = (data.get("streams") or [{}])[0]
        fmt = data.get("format") or {}
        return {
            "duration": float(fmt["duration"]) if fmt.get("duration") else None,
            "width": int(stream["width"]) if stream.get("width") else None,
            "height": int(stream["height"]) if stream.get("height") else None,
            "fps": stream.get("r_frame_rate"),
        }
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError, TypeError):
        return {"duration": None, "width": None, "height": None, "fps": None}


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def process_one(item: tuple[Path, Path, dict[str, Any], bool]) -> Episode:
    path, source_root, old, trust_metadata = item
    stat = path.stat()
    rel = path.relative_to(source_root).as_posix()
    old_item = old.get("by_relative_path", {}).get(rel, {})
    if trust_metadata and (old_item.get("size"), old_item.get("mtime_ns")) == (stat.st_size, stat.st_mtime_ns) and old_item.get("sha256"):
        digest = old_item["sha256"]
        probe = {k: old_item.get(k) for k in ("duration", "width", "height", "fps")}
    else:
        digest = sha256_file(path)
        probe = ffprobe(path)
    stem = path.stem
    match = re.search(r"(?:第|ep|e)?\s*(\d+)\s*(?:集)?", stem, re.I)
    episode_key = f"e{int(match.group(1)):03d}" if match else stem
    return Episode(episode_key, str(path), rel, stat.st_size, stat.st_mtime_ns, digest, probe.get("duration"), probe.get("width"), probe.get("height"), probe.get("fps"))


def main() -> int:
    parser = argparse.ArgumentParser(description="建立短剧分集索引和可复用缓存")
    parser.add_argument("source", type=Path, help="包含视频的源文件夹")
    parser.add_argument("--cache", type=Path, default=None, help="缓存目录，默认源目录/.short-drama-cache")
    parser.add_argument("--workers", type=int, default=0, help="并行 worker 数，默认自动限制在 1—4")
    parser.add_argument("--skill-version", default="2026.11-quality-first-speed-v1")
    parser.add_argument("--trust-metadata-cache", action="store_true", help="仅在确认源文件不会被原地替换时，允许用大小和修改时间跳过 SHA256")
    args = parser.parse_args()
    source = args.source.expanduser().resolve()
    if not source.is_dir():
        parser.error(f"源文件夹不存在：{source}")
    cache = (args.cache or (source / ".short-drama-cache")).expanduser().resolve()
    cache.mkdir(parents=True, exist_ok=True)
    cache_file = cache / "manifest.json"
    old = load_json(cache_file)
    videos = sorted([p for p in source.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXTS], key=natural_key)
    workers = args.workers or min(4, max(1, (os.cpu_count() or 2) - 1))
    workers = max(1, min(4, workers))
    started = time.time()
    jobs = [(p, source, old, args.trust_metadata_cache) for p in videos]
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        episodes = list(pool.map(process_one, jobs))
    episodes.sort(key=lambda x: natural_key(Path(x.relative_path)))
    payload = {
        "schema": 1,
        "skill_version": args.skill_version,
        "source": str(source),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "workers": workers,
        "elapsed_seconds": round(time.time() - started, 3),
        "episodes": [asdict(x) for x in episodes],
        "by_relative_path": {x.relative_path: asdict(x) for x in episodes},
    }
    tmp = cache_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(cache_file)
    print(json.dumps({"source": str(source), "episodes": len(episodes), "workers": workers, "cache": str(cache_file), "elapsed_seconds": payload["elapsed_seconds"]}, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
