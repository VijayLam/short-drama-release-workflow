#!/usr/bin/env python3
"""用本机 Ollama 音频模型按集转写，并在源目录内缓存结果。"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def validate_loopback_host(host: str) -> str:
    parsed = urlsplit(host)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Ollama 地址必须是本机 http(s) 回环地址")
    hostname = parsed.hostname.lower()
    try:
        local = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        local = hostname == "localhost"
    if not local or parsed.username or parsed.password or parsed.path not in {"", "/"}:
        raise ValueError("为保证音频不离开本机，Ollama 地址只允许 localhost 或回环 IP，且不能包含账号信息或路径")
    return f"{parsed.scheme}://{parsed.netloc}"


def request_json(url: str, *, timeout: int = 30) -> dict[str, Any]:
    request = Request(url)
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RuntimeError(f"ollama_http_{exc.code}") from None
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"ollama_connection_{type(exc).__name__}") from None
    except json.JSONDecodeError:
        raise RuntimeError("ollama_invalid_json") from None
    if not isinstance(payload, dict):
        raise RuntimeError("ollama_response_not_object")
    return payload


def get_local_model(host: str, model: str) -> tuple[str, str]:
    base = validate_loopback_host(host)
    tags = request_json(f"{base}/api/tags")
    candidates = tags.get("models", [])
    entry = next((item for item in candidates if item.get("name") == model or item.get("model") == model), None)
    if entry is None:
        available = ", ".join(str(item.get("name", "")) for item in candidates)
        raise ValueError(f"本机 Ollama 未找到模型 {model}。本机现有模型: {available or '(空)'}")
    capabilities = entry.get("capabilities", [])
    if "audio" not in capabilities:
        raise ValueError(f"本机 Ollama 模型 {model} 未报告 audio 能力")
    digest = str(entry.get("digest", ""))
    if not digest:
        raise ValueError(f"本机 Ollama 未返回模型 {model} 的 digest，无法建立可靠缓存键")
    version = request_json(f"{base}/api/version").get("version")
    if not version:
        raise ValueError("本机 Ollama 未返回服务版本，无法建立可靠缓存键")
    return digest, str(version)


def digest_config(source_sha: str, model: str, model_digest: str, ollama_version: str, language: str) -> str:
    value = {
        "source_sha256": source_sha,
        "provider": "local-ollama-audio-transcriptions",
        "model": model,
        "model_digest": model_digest,
        "ollama_version": ollama_version,
        "language": language,
        "response_format": "verbose_json",
        "audio": "ffmpeg-pcm-s16le-mono-16000hz",
        "temperature": 0,
    }
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def cache_matches(cached: dict[str, Any], source_sha: str, fingerprint: str) -> bool:
    return (
        cached.get("status") == "ready"
        and cached.get("source_sha256") == source_sha
        and cached.get("fingerprint") == fingerprint
        and isinstance(cached.get("segments"), list)
    )


def atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def normalize_transcription(payload: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    text = str(payload.get("text", "")).strip()
    raw_segments = payload.get("segments", [])
    if not isinstance(raw_segments, list):
        raw_segments = []
    segments = []
    for item in raw_segments:
        if not isinstance(item, dict):
            continue
        segments.append(
            {
                "start": round(float(item.get("start", 0) or 0), 2),
                "end": round(float(item.get("end", 0) or 0), 2),
                "text": str(item.get("text", "")).strip(),
            }
        )
    if not text:
        text = "".join(segment["text"] for segment in segments).strip()
    if not text:
        raise ValueError("empty_transcription")
    return text, segments


def transcribe_local(curl: str, host: str, audio_path: Path, model: str, language: str, timeout: int) -> dict[str, Any]:
    output_path = audio_path.with_suffix(".transcription.json")
    command = [
        curl,
        "--silent", "--show-error",
        "--max-time", str(timeout),
        "--output", str(output_path),
        "--write-out", "%{http_code}",
        "--form", f"model={model}",
        "--form", f"language={language}",
        "--form", "response_format=verbose_json",
        "--form", "temperature=0",
        "--form", f"file=@{audio_path};type=audio/wav",
        f"{host}/v1/audio/transcriptions",
    ]
    try:
        result = subprocess.run(
            command, text=True, capture_output=True, check=False, timeout=timeout + 5
        )
    except subprocess.TimeoutExpired:
        raise TimeoutError(f"ASR超过{timeout}秒硬时限") from None
    if result.returncode == 28:
        raise TimeoutError(f"ASR超过{timeout}秒硬时限")
    if result.returncode != 0:
        raise RuntimeError(f"curl_local_asr_exit_{result.returncode}")
    if result.stdout.strip() != "200":
        raise RuntimeError(f"ollama_http_{result.stdout.strip() or 'no_status'}")
    try:
        response = json.loads(output_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise RuntimeError("ollama_invalid_json") from None
    if not isinstance(response, dict):
        raise RuntimeError("ollama_response_not_object")
    return response


def transcribe_one(job: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="asr-", dir=settings["cache_root"]) as temp_name:
        wav_path = Path(temp_name) / "audio.wav"
        command = [
            settings["ffmpeg"], "-hide_banner", "-loglevel", "error", "-y",
            "-i", job["source_path"], "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", str(wav_path),
        ]
        try:
            result = subprocess.run(command, text=True, capture_output=True, check=False, timeout=120)
        except subprocess.TimeoutExpired:
            return {"episode_key": job["episode_key"], "status": "failed", "error": "ffmpeg_timeout_120s"}
        if result.returncode != 0 or not wav_path.is_file() or wav_path.stat().st_size <= 44:
            return {"episode_key": job["episode_key"], "status": "failed", "error": "ffmpeg_audio_conversion_failed"}
        response = transcribe_local(
            settings["curl"], settings["host"], wav_path, settings["model"],
            settings["language"], settings["timeout"],
        )
    text, segments = normalize_transcription(response)
    data = {
        "schema": 2,
        "artifact_id": job["artifact_id"],
        "run_id": job["run_id"],
        "episode_key": job["episode_key"],
        "source_video": job["relative_path"],
        "source_sha256": job["source_sha256"],
        "fingerprint": job["fingerprint"],
        "provider": "local-ollama-audio-transcriptions",
        "model": settings["model"],
        "model_digest": settings["model_digest"],
        "ollama_version": settings["ollama_version"],
        "language": settings["language"],
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "text": text,
        "segments": segments,
        "status": "ready",
    }
    atomic_json(Path(job["output_path"]), data)
    return {"episode_key": job["episode_key"], "status": "ready", "elapsed_seconds": data["elapsed_seconds"]}


def main() -> int:
    parser = argparse.ArgumentParser(description="用本机 Ollama 音频模型按集转写并缓存")
    parser.add_argument("source", type=Path)
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--cache", type=Path, default=None)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--model", default="gemma4:12b")
    parser.add_argument("--ollama-host", default=os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434"))
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--curl", default="curl")
    parser.add_argument("--language", default="zh")
    parser.add_argument("--timeout", type=int, default=60, help="每集 ASR 硬超时秒数，最大 120 秒")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--episode", action="append", default=None, help="只处理指定集，可重复传入")
    args = parser.parse_args()

    source = args.source.expanduser().resolve()
    manifest_path = (args.manifest or source / ".short-drama-cache" / "manifest.json").expanduser().resolve()
    cache_root = (args.cache or source / ".short-drama-cache" / "asr").expanduser().resolve()
    for path in (manifest_path, cache_root):
        try:
            path.relative_to(source)
        except ValueError:
            parser.error(f"manifest/ASR缓存必须位于源视频目录内: {path}")
    if not shutil.which(args.ffmpeg):
        parser.error(f"找不到 ffmpeg 可执行文件: {args.ffmpeg}")
    if not shutil.which(args.curl):
        parser.error(f"找不到 curl 可执行文件: {args.curl}")
    try:
        host = validate_loopback_host(args.ollama_host)
        model_digest, ollama_version = get_local_model(host, args.model)
    except (ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    if args.workers < 1 or args.timeout < 1 or args.timeout > 120:
        parser.error("workers 必须大于 0；ASR timeout 必须为 1—120 秒")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "ready":
        parser.error(f"源清单未通过门禁: {manifest.get('status')}")
    run_id = args.run_id or time.strftime("asr-%Y%m%d-%H%M%S")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", run_id):
        parser.error("run-id 仅允许 1—96 位英文、数字、点、下划线和连字符")
    selected = set(args.episode or [])
    jobs, skipped = [], []
    for item in manifest.get("episodes", []):
        if item.get("status") != "ready" or not item.get("episode_key") or not item.get("sha256"):
            continue
        key = str(item["episode_key"])
        if selected and key not in selected:
            continue
        relative = PurePosixPath(str(item.get("relative_path", "")))
        if relative.is_absolute() or ".." in relative.parts:
            parser.error(f"非法相对路径: {relative}")
        video = (source / Path(*relative.parts)).resolve()
        try:
            video.relative_to(source)
        except ValueError:
            parser.error(f"视频路径越出源目录: {video}")
        if not video.is_file():
            parser.error(f"视频不存在: {video}")
        fingerprint = digest_config(str(item["sha256"]), args.model, model_digest, ollama_version, args.language)
        output = cache_root / key / f"{fingerprint}.json"
        if output.exists():
            try:
                cached = json.loads(output.read_text(encoding="utf-8"))
                if cache_matches(cached, str(item["sha256"]), fingerprint):
                    skipped.append(key)
                    continue
            except (OSError, json.JSONDecodeError):
                pass
        jobs.append(
            {
                "artifact_id": f"asr-{key}-{fingerprint[:16]}",
                "run_id": run_id,
                "episode_key": key,
                "relative_path": str(relative),
                "source_path": str(video),
                "source_sha256": str(item["sha256"]),
                "fingerprint": fingerprint,
                "output_path": str(output),
            }
        )

    if not jobs:
        print(json.dumps({"status": "noop", "processed": 0, "skipped": skipped, "cache_root": str(cache_root)}, ensure_ascii=False))
        return 0

    cache_root.mkdir(parents=True, exist_ok=True)
    worker_count = max(1, min(4, int(args.workers), len(jobs)))
    settings = {
        "cache_root": str(cache_root),
        "ffmpeg": shutil.which(args.ffmpeg),
        "curl": shutil.which(args.curl),
        "host": host,
        "model": args.model,
        "model_digest": model_digest,
        "ollama_version": ollama_version,
        "language": args.language,
        "timeout": args.timeout,
    }
    started = time.perf_counter()
    completed, failed = [], []
    with futures.ThreadPoolExecutor(max_workers=worker_count) as pool:
        future_map = {pool.submit(transcribe_one, job, settings): job for job in jobs}
        for future in futures.as_completed(future_map):
            job = future_map[future]
            try:
                result = future.result()
                (completed if result["status"] == "ready" else failed).append(result)
            except Exception as exc:
                failed.append({"episode_key": job["episode_key"], "status": "failed", "error": str(exc)})
    summary = {
        "status": "ready" if not failed else "blocked",
        "run_id": run_id,
        "provider": "local-ollama-audio-transcriptions",
        "host": host,
        "model": args.model,
        "model_digest": model_digest,
        "ollama_version": ollama_version,
        "workers": worker_count,
        "processed": len(completed),
        "skipped": skipped,
        "failures": failed,
        "elapsed_seconds": round(time.perf_counter() - started, 2),
        "cache_root": str(cache_root),
        "external_network_used": False,
    }
    atomic_json(cache_root / "runs" / f"{run_id}.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
