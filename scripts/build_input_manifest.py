#!/usr/bin/env python3
"""建立可恢复、可审计的短剧正式分集清单。

默认只扫描源目录的直属视频文件。子目录视频必须显式使用
--include-subfolders，且重复集数、不可读视频和无法识别集数都会阻断
正式发行。单集失败会被记录到 manifest，其他集仍继续检查。
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
EPISODE_PATTERNS = (
    re.compile(r"(?:第|ep|e)\s*[-_ ]?(\d+)\s*(?:集)?", re.I),
    re.compile(r"(?:^|[-_ ])(\d{1,3})(?:[-_ ]|$)"),
)


@dataclass
class EpisodeResult:
    episode_key: str | None
    path: str
    relative_path: str
    size: int | None
    mtime_ns: int | None
    sha256: str | None
    duration: float | None
    width: int | None
    height: int | None
    fps: str | None
    readable: bool
    status: str
    errors: list[str]


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


def ffprobe(path: Path) -> tuple[dict[str, Any], str | None]:
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate:format=duration",
        "-of", "json", str(path),
    ]
    try:
        raw = subprocess.check_output(cmd, text=True, stderr=subprocess.PIPE)
        data = json.loads(raw)
        streams = data.get("streams") or []
        if not streams:
            return {}, "没有可读视频流"
        stream = streams[0]
        fmt = data.get("format") or {}
        probe = {
            "duration": float(fmt["duration"]) if fmt.get("duration") else None,
            "width": int(stream["width"]) if stream.get("width") else None,
            "height": int(stream["height"]) if stream.get("height") else None,
            "fps": stream.get("r_frame_rate"),
        }
        if not probe["duration"] or not probe["width"] or not probe["height"]:
            return probe, "视频元数据不完整"
        return probe, None
    except (OSError, subprocess.CalledProcessError, ValueError, KeyError, TypeError) as exc:
        return {}, f"ffprobe失败:{type(exc).__name__}"


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def episode_key_for(path: Path) -> str | None:
    stem = path.stem
    for pattern in EPISODE_PATTERNS:
        match = pattern.search(stem)
        if match:
            return f"e{int(match.group(1)):03d}"
    return None


def process_one(item: tuple[Path, Path, dict[str, Any], bool]) -> EpisodeResult:
    path, source_root, old, trust_metadata = item
    rel = path.relative_to(source_root).as_posix()
    old_item = old.get("by_relative_path", {}).get(rel, {})
    try:
        stat = path.stat()
        if (
            trust_metadata
            and (old_item.get("size"), old_item.get("mtime_ns")) == (stat.st_size, stat.st_mtime_ns)
            and old_item.get("sha256")
        ):
            digest = old_item["sha256"]
            probe = {k: old_item.get(k) for k in ("duration", "width", "height", "fps")}
            probe_error = None
        else:
            digest = sha256_file(path)
            probe, probe_error = ffprobe(path)
        errors = [probe_error] if probe_error else []
        key = episode_key_for(path)
        if key is None:
            errors.append("无法从文件名识别集数")
        readable = not errors
        return EpisodeResult(
            episode_key=key,
            path=str(path),
            relative_path=rel,
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
            sha256=digest,
            duration=probe.get("duration"),
            width=probe.get("width"),
            height=probe.get("height"),
            fps=probe.get("fps"),
            readable=readable,
            status="ready" if readable else "failed",
            errors=errors,
        )
    except (OSError, ValueError) as exc:
        return EpisodeResult(
            episode_key=episode_key_for(path),
            path=str(path),
            relative_path=rel,
            size=None,
            mtime_ns=None,
            sha256=None,
            duration=None,
            width=None,
            height=None,
            fps=None,
            readable=False,
            status="failed",
            errors=[f"文件读取失败:{type(exc).__name__}"],
        )


def discover_videos(source: Path, include_subfolders: bool) -> tuple[list[Path], list[dict[str, str]]]:
    iterator = source.rglob("*") if include_subfolders else source.iterdir()
    videos: list[Path] = []
    excluded: list[dict[str, str]] = []
    for path in sorted(iterator, key=natural_key):
        if not path.is_file() or path.suffix.lower() not in VIDEO_EXTS:
            continue
        if path.is_symlink():
            excluded.append({"path": str(path), "reason": "符号链接默认不作为正式输入"})
            continue
        videos.append(path)
    return videos, excluded


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="建立短剧正式分集索引和可审计缓存")
    parser.add_argument("source", type=Path, help="包含正式短剧视频的源文件夹")
    parser.add_argument("--cache", type=Path, default=None, help="缓存目录，默认源目录/.short-drama-cache")
    parser.add_argument("--workers", type=int, default=0, help="并行 worker 数，默认自动限制在1—4")
    parser.add_argument("--skill-version", default="2026.11-production-control-v4-ocr-first")
    parser.add_argument("--include-subfolders", action="store_true", help="显式允许扫描子目录视频")
    parser.add_argument("--trust-metadata-cache", action="store_true", help="仅在源文件不会被原地替换时，用大小和mtime跳过SHA256")
    args = parser.parse_args()

    source = args.source.expanduser().resolve()
    if not source.is_dir():
        parser.error(f"源文件夹不存在：{source}")
    cache = (args.cache or (source / ".short-drama-cache")).expanduser().resolve()
    cache_file = cache / "manifest.json"
    old = load_json(cache_file)
    videos, excluded = discover_videos(source, args.include_subfolders)
    workers = args.workers or min(4, max(1, (os.cpu_count() or 2) - 1))
    workers = max(1, min(4, workers))
    started = time.time()
    jobs = [(path, source, old, args.trust_metadata_cache) for path in videos]
    with cf.ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(process_one, jobs))
    results.sort(key=lambda item: natural_key(Path(item.relative_path)))

    duplicate_map: dict[str, list[str]] = {}
    for item in results:
        if item.episode_key:
            duplicate_map.setdefault(item.episode_key, []).append(item.relative_path)
    duplicates = {key: paths for key, paths in duplicate_map.items() if len(paths) > 1}
    for item in results:
        if item.episode_key in duplicates:
            item.status = "blocked"
            item.readable = False
            item.errors.append(f"重复集数:{item.episode_key}")

    result_dicts = [asdict(item) for item in results]
    failed = [item for item in result_dicts if item["status"] != "ready"]
    ready = [item for item in result_dicts if item["status"] == "ready"]
    payload = {
        "schema": 2,
        "skill_version": args.skill_version,
        "source": str(source),
        "scan_mode": "recursive" if args.include_subfolders else "direct_children",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "workers": workers,
        "elapsed_seconds": round(time.time() - started, 3),
        "status": "blocked" if failed or duplicates or not results else "ready",
        "episodes": ready,
        "failed": failed,
        "excluded": excluded,
        "duplicate_episode_keys": duplicates,
        "by_relative_path": {item["relative_path"]: item for item in result_dicts},
    }
    atomic_write_json(cache_file, payload)
    print(json.dumps({
        "source": str(source),
        "ready_episodes": len(ready),
        "failed": len(failed),
        "duplicates": sorted(duplicates),
        "status": payload["status"],
        "reason": "没有发现正式视频" if not results else None,
        "cache": str(cache_file),
        "elapsed_seconds": payload["elapsed_seconds"],
    }, ensure_ascii=False))
    return 0 if payload["status"] == "ready" else 2


if __name__ == "__main__":
    raise SystemExit(main())
