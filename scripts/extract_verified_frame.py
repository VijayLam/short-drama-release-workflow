#!/usr/bin/env python3
"""Extract one exact source frame by sequential decode and record its PTS."""
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


PTS_RE = re.compile(r"\bpts:\s*(-?\d+)\s+pts_time:\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+))")
TIME_BASE_RE = re.compile(r"^(\d+)/(\d+)$")
FILTER_TIME_BASE_RE = re.compile(r"config in time_base:\s*(\d+/\d+)")


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


def inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def time_base_seconds(value: str) -> float:
    match = TIME_BASE_RE.fullmatch(value)
    if not match:
        raise ValueError("time_base 必须是 num/den 格式")
    numerator, denominator = (int(part) for part in match.groups())
    if numerator <= 0 or denominator <= 0:
        raise ValueError("time_base 分子分母必须大于0")
    return numerator / denominator


def probe_dimensions(ffprobe: str, path: Path) -> tuple[int, int]:
    result = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "json", str(path)],
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError("ffprobe_image_failed")
    streams = json.loads(result.stdout).get("streams", [])
    if not streams:
        raise RuntimeError("image_has_no_video_stream")
    return int(streams[0]["width"]), int(streams[0]["height"])


def extract_frame(
    source: Path,
    expected_pts_ticks: int,
    time_base: str,
    output: Path,
    source_root: Path,
    ffmpeg: str,
    ffprobe: str,
) -> dict[str, Any]:
    if not isinstance(expected_pts_ticks, int):
        raise ValueError("源 PTS 必须是整数 ticks，不能从名义帧率换算帧序号")
    tick_seconds = time_base_seconds(time_base)
    expected_pts_seconds = expected_pts_ticks * tick_seconds
    if not math.isfinite(expected_pts_seconds) or expected_pts_seconds < 0:
        raise ValueError("源 PTS 与 time_base 必须对应非负有限时间")
    if source.expanduser().is_symlink() or output.expanduser().is_symlink():
        raise ValueError("拒绝通过符号链接读取源视频或写入输出图片")
    source = source.expanduser().resolve()
    source_root = source_root.expanduser().resolve()
    output = output.expanduser().resolve()
    if not source.is_file() or source.is_symlink():
        raise ValueError("源视频不存在或为符号链接")
    if not inside(source, source_root) or not inside(output, source_root):
        raise ValueError("源视频与输出图片都必须位于指定源目录内")
    if output.suffix.lower() != ".png":
        raise ValueError("输出图片必须使用 .png")
    if output.exists():
        raise FileExistsError(f"拒绝覆盖已有图片: {output}")

    output.parent.mkdir(parents=True, exist_ok=True)
    source_sha = sha256_file(source)
    relative = output.relative_to(source_root).as_posix()
    artifact_key = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:20]
    cache_root = source_root / ".short-drama-cache"
    preview = cache_root / "frame-previews" / f"{artifact_key}.png"
    evidence = cache_root / "frame-evidence" / f"{artifact_key}.json"
    if preview.exists() or evidence.exists():
        raise FileExistsError("该输出路径已有帧预览或提取证据，拒绝覆盖")

    with tempfile.NamedTemporaryFile(prefix=".frame-", suffix=".png", dir=output.parent, delete=False) as temp_file:
        temporary_frame = Path(temp_file.name)
    with tempfile.NamedTemporaryFile(prefix=".preview-", suffix=".png", dir=output.parent, delete=False) as temp_file:
        temporary_preview = Path(temp_file.name)

    try:
        filter_graph = f"select=eq(pts\\,{expected_pts_ticks}),showinfo"
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "info", "-copyts", "-i", str(source),
            "-map", "0:v:0", "-vf", filter_graph, "-frames:v", "1", "-an",
            "-c:v", "png", "-f", "image2", "-update", "1", "-y", str(temporary_frame),
        ]
        result = subprocess.run(command, text=True, capture_output=True, check=False, timeout=300)
        filter_time_bases = FILTER_TIME_BASE_RE.findall(result.stderr)
        if len(filter_time_bases) != 1:
            raise RuntimeError(f"source_time_base_unverified:{len(filter_time_bases)}")
        if Fraction(filter_time_bases[0]) != Fraction(time_base):
            raise RuntimeError(f"source_time_base_mismatch:{filter_time_bases[0]}!={time_base}")
        pts_values = PTS_RE.findall(result.stderr)
        if not pts_values:
            if "Output file is empty" in result.stderr:
                raise RuntimeError("requested_source_pts_not_found")
            raise RuntimeError("sequential_frame_decode_no_pts")
        if result.returncode != 0 or temporary_frame.stat().st_size == 0:
            raise RuntimeError("sequential_frame_decode_failed")
        if len(pts_values) != 1:
            showinfo = "\n".join(line for line in result.stderr.splitlines() if "showinfo" in line)
            raise RuntimeError(f"expected_one_source_pts_got_{len(pts_values)}:{showinfo}")
        actual_pts_ticks = int(pts_values[0][0])
        actual_pts = float(pts_values[0][1])
        if actual_pts_ticks != expected_pts_ticks:
            raise RuntimeError(f"source_pts_mismatch:{actual_pts_ticks}!={expected_pts_ticks}")
        expected_seconds_from_ticks = float(Fraction(expected_pts_ticks) * Fraction(time_base))
        if abs(actual_pts - expected_seconds_from_ticks) > tick_seconds / 2 + 1e-9:
            raise RuntimeError("source_pts_timecode_mismatch")

        preview.parent.mkdir(parents=True, exist_ok=True)
        preview_command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(temporary_frame),
            "-vf", "scale=w='min(480,iw)':h=-2:flags=lanczos", "-frames:v", "1",
            "-update", "1", "-y", str(temporary_preview),
        ]
        preview_result = subprocess.run(preview_command, text=True, capture_output=True, check=False, timeout=30)
        if preview_result.returncode != 0 or temporary_preview.stat().st_size == 0:
            raise RuntimeError("preview_thumbnail_failed")

        width, height = probe_dimensions(ffprobe, temporary_frame)
        preview_width, preview_height = probe_dimensions(ffprobe, temporary_preview)
        if preview_width > width or preview_height > height:
            raise RuntimeError("preview_thumbnail_was_upscaled")
        if sha256_file(source) != source_sha:
            raise RuntimeError("source_changed_during_frame_extraction")
        temporary_frame.replace(output)
        temporary_preview.replace(preview)
        evidence_record = {
            "schema": 1,
            "source_video": str(source),
            "source_sha256": source_sha,
            "extraction_method": "ffmpeg_sequential_decode_select_source_pts_no_seek",
            "expected_source_pts_ticks": expected_pts_ticks,
            "source_time_base": time_base,
            "expected_source_pts_seconds": expected_seconds_from_ticks,
            "actual_source_pts_ticks": actual_pts_ticks,
            "actual_source_pts_seconds": actual_pts,
            "pts_delta_ticks": 0,
            "timecode_hhmmss_mmm": format_timecode(actual_pts),
            "filename_timecode_0_1s": format_filename_timecode(actual_pts),
            "image_file": str(output),
            "image_sha256": sha256_file(output),
            "image_dimensions": [width, height],
            "preview_thumbnail": str(preview),
            "preview_dimensions": [preview_width, preview_height],
            "preview_sha256": sha256_file(preview),
            "visual_review_status": "pending",
            "extracted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "evidence_file": str(evidence),
        }
        atomic_json(evidence, evidence_record)
        return evidence_record
    except Exception:
        output.unlink(missing_ok=True)
        preview.unlink(missing_ok=True)
        evidence.unlink(missing_ok=True)
        raise
    finally:
        temporary_frame.unlink(missing_ok=True)
        temporary_preview.unlink(missing_ok=True)


def format_timecode(seconds: float) -> str:
    millis_total = int(round(seconds * 1000))
    hours, millis_total = divmod(millis_total, 3_600_000)
    minutes, millis_total = divmod(millis_total, 60_000)
    whole_seconds, millis = divmod(millis_total, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}.{millis:03d}"


def format_filename_timecode(seconds: float) -> str:
    minutes = int(seconds // 60)
    remainder = seconds - minutes * 60
    return f"{minutes:02d}分{remainder:04.1f}秒"


def main() -> int:
    parser = argparse.ArgumentParser(description="顺序解码并按源 PTS ticks 精确提取带证据的 PNG")
    parser.add_argument("source", type=Path, help="源视频")
    parser.add_argument("output", type=Path, help="源目录内的 stage 图片路径")
    parser.add_argument("--pts", type=int, required=True, help="ffprobe/showinfo记录的源 PTS ticks")
    parser.add_argument("--time-base", required=True, help="源视频帧时间基，例如 1/90000")
    parser.add_argument("--source-root", type=Path, default=None, help="源视频目录；默认源视频所在目录")
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--ffprobe", default="ffprobe")
    args = parser.parse_args()
    source = args.source.expanduser()
    source_root = (args.source_root or source.parent).expanduser()
    try:
        result = extract_frame(
            source, args.pts, args.time_base, args.output, source_root,
            args.ffmpeg, args.ffprobe,
        )
    except (ValueError, FileExistsError, RuntimeError, OSError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
