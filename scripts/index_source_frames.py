#!/usr/bin/env python3
"""Stream frame PTS/time-base metadata into a source-local JSONL index."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import tempfile
import time
from fractions import Fraction
from pathlib import Path
from typing import Any


PAIR_RE = re.compile(r"([^|=]+)=([^|]*)")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def read_stream_info(ffprobe: str, source: Path) -> dict[str, str]:
    command = [
        ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
        "stream=time_base,r_frame_rate,avg_frame_rate,start_time", "-of", "json", str(source),
    ]
    result = subprocess.run(command, text=True, capture_output=True, check=False, timeout=30)
    if result.returncode != 0:
        raise RuntimeError("ffprobe_stream_metadata_failed")
    streams = json.loads(result.stdout).get("streams", [])
    if not streams:
        raise RuntimeError("source_video_stream_missing")
    info = streams[0]
    time_base = str(info.get("time_base", ""))
    try:
        fraction = Fraction(time_base)
    except (ValueError, ZeroDivisionError):
        raise RuntimeError("source_time_base_missing_or_invalid") from None
    if fraction <= 0:
        raise RuntimeError("source_time_base_missing_or_invalid")
    return {
        "time_base": time_base,
        "r_frame_rate": str(info.get("r_frame_rate", "")),
        "avg_frame_rate": str(info.get("avg_frame_rate", "")),
        "start_time": str(info.get("start_time", "")),
    }


def parse_compact_frame(line: str, frame_order: int, time_base: Fraction) -> dict[str, Any]:
    fields = dict(PAIR_RE.findall(line.strip()))
    raw_pts = fields.get("pts")
    best_effort = fields.get("best_effort_timestamp")
    raw_pts_ticks = int(raw_pts) if raw_pts not in (None, "", "N/A") else None
    best_effort_ticks = int(best_effort) if best_effort not in (None, "", "N/A") else None
    pts_ticks = best_effort_ticks if best_effort_ticks is not None else raw_pts_ticks
    pts_seconds = float(Fraction(pts_ticks) * time_base) if pts_ticks is not None else None
    printed_pts_time = fields.get("best_effort_timestamp_time") or fields.get("pts_time")
    if printed_pts_time not in (None, "", "N/A") and pts_seconds is not None:
        printed_seconds = float(printed_pts_time)
        if not math.isfinite(printed_seconds) or abs(printed_seconds - pts_seconds) > float(time_base) / 2 + 1e-9:
            raise RuntimeError(f"frame_pts_time_mismatch_at_order_{frame_order}")
    best_effort = fields.get("best_effort_timestamp")
    return {
        "frame_order_zero_based": frame_order,
        "pts_ticks": pts_ticks,
        "pts_seconds": pts_seconds,
        "input_pts_ticks": raw_pts_ticks,
        "best_effort_pts_ticks": best_effort_ticks,
        "best_effort_pts_time": float(fields["best_effort_timestamp_time"])
        if fields.get("best_effort_timestamp_time") not in (None, "", "N/A") else None,
        "pict_type": fields.get("pict_type"),
        "key_frame": fields.get("key_frame") == "1",
    }


def load_nearest(index_path: Path, target: float) -> dict[str, Any] | None:
    closest, distance = None, None
    with index_path.open("r", encoding="utf-8") as index_file:
        for line in index_file:
            item = json.loads(line)
            pts_seconds = item.get("pts_seconds")
            if pts_seconds is None:
                continue
            delta = abs(float(pts_seconds) - target)
            if distance is None or delta < distance:
                closest, distance = item, delta
    if closest is not None:
        closest = {**closest, "distance_from_requested_seconds": distance}
    return closest


def build_index(source: Path, source_root: Path, ffprobe: str, timeout: int) -> dict[str, Any]:
    if source.is_symlink():
        raise ValueError("源视频不能是符号链接")
    source = source.expanduser().resolve()
    source_root = source_root.expanduser().resolve()
    if not source.is_file():
        raise ValueError("源视频不存在")
    try:
        source.relative_to(source_root)
    except ValueError:
        raise ValueError("源视频必须位于指定源目录内") from None

    source_sha = sha256_file(source)
    stream_info = read_stream_info(ffprobe, source)
    time_base = Fraction(stream_info["time_base"])
    index_dir = source_root / ".short-drama-cache" / "frame-index"
    index_dir.mkdir(parents=True, exist_ok=True)
    index_path = index_dir / f"{source.stem}-{source_sha[:16]}.jsonl"
    manifest_path = index_path.with_suffix(".manifest.json")
    if index_path.is_symlink() or manifest_path.is_symlink():
        raise ValueError("索引目标不能是符号链接")
    if index_path.is_file() and manifest_path.is_file():
        try:
            cached = json.loads(manifest_path.read_text(encoding="utf-8"))
            if cached.get("source_sha256") == source_sha and cached.get("status") == "ready":
                return {**cached, "index_file": str(index_path), "cache_hit": True}
        except (OSError, json.JSONDecodeError):
            pass

    command = [
        ffprobe, "-v", "error", "-select_streams", "v:0", "-show_frames",
        "-show_entries",
        "frame=pts,pts_time,best_effort_timestamp,best_effort_timestamp_time,pict_type,key_frame",
        "-of", "compact=p=0:nk=0", str(source),
    ]
    started = time.perf_counter()
    temp_path: Path | None = None
    frame_count, missing_pts = 0, 0
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", prefix=".frame-index-", suffix=".jsonl.tmp",
            dir=index_dir, delete=False,
        ) as output_file:
            temp_path = Path(output_file.name)
            with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as raw_output:
                result = subprocess.run(
                    command, stdout=raw_output, stderr=subprocess.PIPE, text=True,
                    check=False, timeout=timeout,
                )
                if result.returncode != 0:
                    raise RuntimeError(f"ffprobe_frame_index_failed:{result.stderr[-500:]}")
                raw_output.seek(0)
                for line in raw_output:
                    if not line.strip():
                        continue
                    item = parse_compact_frame(line, frame_count, time_base)
                    if item["pts_ticks"] is None:
                        missing_pts += 1
                    output_file.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
                    frame_count += 1
            output_file.flush()
        if frame_count == 0:
            raise RuntimeError("no_decoded_frames")
        temp_path.replace(index_path)
    except Exception:
        if temp_path:
            temp_path.unlink(missing_ok=True)
        raise

    result = {
        "schema": 1,
        "source_video": str(source),
        "source_sha256": source_sha,
        "time_base": stream_info["time_base"],
        "r_frame_rate": stream_info["r_frame_rate"],
        "avg_frame_rate": stream_info["avg_frame_rate"],
        "start_time": stream_info["start_time"],
        "frame_count": frame_count,
        "frames_without_pts": missing_pts,
        "index_file": str(index_path),
        "index_format": "jsonl_streamed_from_ffprobe_show_frames",
        "status": "ready" if missing_pts == 0 else "partial_missing_pts",
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    atomic_json(manifest_path, result)
    return {**result, "cache_hit": False}


def main() -> int:
    parser = argparse.ArgumentParser(description="从源视频逐帧建立 PTS/time_base 索引，不按名义 FPS 推帧号")
    parser.add_argument("source", type=Path)
    parser.add_argument("--source-root", type=Path, default=None)
    parser.add_argument("--near-seconds", type=float, action="append", default=None, help="输出最接近时刻的真实 PTS；可重复指定")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--ffprobe", default="ffprobe")
    args = parser.parse_args()
    source = args.source.expanduser()
    source_root = (args.source_root or source.parent).expanduser()
    if args.timeout < 1:
        parser.error("timeout必须大于0")
    if any(not math.isfinite(value) or value < 0 for value in (args.near_seconds or [])):
        parser.error("near-seconds必须是非负有限值")
    try:
        result = build_index(source, source_root, args.ffprobe, args.timeout)
        if args.near_seconds:
            result["nearest_candidates"] = [
                {"requested_seconds": target, "frame": load_nearest(Path(result["index_file"]), target)}
                for target in args.near_seconds
            ]
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
