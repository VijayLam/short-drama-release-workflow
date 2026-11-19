#!/usr/bin/env python3
"""短剧二次发行的 fail-closed 验收器。

验收以生成清单.json声明的包集合为准，不以目录前缀猜测包。
默认严格要求：注册表有真实扫描证据、人物/场景资产有审核证据、
脚本有8—12个镜头、前后15秒连续、引用图片真实存在，且包集合双向一致。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from PIL import Image, ImageFilter, ImageStat

REQUIRED = ["镜头", "人物", "台词", "字幕", "BGM", "音效", "尾帧", "禁止项"]
FORBIDDEN = ("待补", "时间待定", "自行发挥", "见 Excel", "TODO")
TIME_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[—-]\s*(\d+(?:\.\d+)?)\s*秒")
SHOT_HEAD_RE = re.compile(r"^###\s*镜头[^\\n]*?(\d+(?:\.\d+)?)\s*[—-]\s*(\d+(?:\.\d+)?)\s*秒", re.M)
PERSON_NAME_RE = re.compile(r"^人物_.+?_.+?_.+?_第\d+集_\d+分\d+(?:\.\d+)?秒$")
SCENE_NAME_RE = re.compile(r"^场景_.+?_.+?_第\d+集_\d+分\d+(?:\.\d+)?秒$")
IMAGE_REF_RE = re.compile(r"[\w\u4e00-\u9fff（）()【】\[\]、，,。._\-]+\\.(?:png|jpg|jpeg|webp)", re.I)


def normalize_role(value: str) -> str:
    return str(value or "").strip().replace("-", "_")


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def file_identity(path: Path) -> tuple[int, int, int]:
    st = path.stat()
    return (st.st_dev, st.st_ino, st.st_size)


def check_image(path: Path, cache: dict[tuple[int, int, int], list[str]]) -> list[str]:
    key = file_identity(path)
    if key in cache:
        return list(cache[key])
    issues: list[str] = []
    try:
        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as opened:
            if min(opened.size) < 480:
                issues.append(f"分辨率过低:{opened.size[0]}x{opened.size[1]}")
            with opened.convert("L") as im:
                small = im.resize((256, 256))
                mean = ImageStat.Stat(small.resize((64, 64))).mean[0]
                if mean < 8:
                    issues.append("过暗")
                edge_std = ImageStat.Stat(small.filter(ImageFilter.FIND_EDGES)).stddev[0]
                if edge_std < 8.0:
                    issues.append(f"清晰度不足:边缘锐度{edge_std:.2f}")
    except Exception as exc:
        issues.append(f"无法读取:{type(exc).__name__}")
    cache[key] = list(issues)
    return issues


def validate_source_evidence(root: Path, manifest: dict[str, Any]) -> list[str]:
    issues: list[str] = []
    if not manifest:
        return ["缺少或无法读取生成清单.json"]
    if manifest.get("schema", 0) < 2:
        issues.append("生成清单schema低于2：缺少生产级运行证据")
    if not manifest.get("run_id"):
        issues.append("生成清单缺少run_id")
    if not manifest.get("source_manifest"):
        issues.append("生成清单缺少source_manifest，无法绑定源视频指纹")
    if not manifest.get("config_fingerprint"):
        issues.append("生成清单缺少config_fingerprint")
    if str(manifest.get("registry_gate") or "").lower() not in {"passed", "通过"}:
        issues.append(f"人物注册表交付门禁未通过:{manifest.get('registry_gate')}")
    packages = manifest.get("packages")
    if not isinstance(packages, list) or not packages:
        issues.append("生成清单packages不是非空列表")
    package_names: list[str] = []
    for item in packages or []:
        if not isinstance(item, dict) or not item.get("package"):
            issues.append("生成清单含无效package记录")
            continue
        name = str(item["package"])
        if Path(name).name != name or name in {".", ".."}:
            issues.append(f"package名称存在路径穿越风险:{name}")
        package_names.append(name)
        if not item.get("episode") or not item.get("variant"):
            issues.append(f"package缺少episode或variant:{name}")
        if not item.get("artifact_id"):
            issues.append(f"package缺少artifact_id:{name}")
    if len(package_names) != len(set(package_names)):
        issues.append("生成清单存在重复package名称")
    return issues


def validate_registry(root: Path, source_count: int | None) -> list[str]:
    path = root / "人物角色注册表.json"
    if not path.exists():
        return ["缺少人物角色注册表.json"]
    data = read_json(path)
    if not data:
        return ["人物角色注册表.json无法读取或为空"]
    issues: list[str] = []
    chars = data.get("characters", {})
    if isinstance(chars, list):
        chars = {str(item.get("人物名", "")): item for item in chars if isinstance(item, dict)}
    if not isinstance(chars, dict):
        return ["人物角色注册表缺少characters对象"]
    required_evidence = ("run_id", "source_manifest_sha256", "episode_scans", "scanned_episodes")
    for key in required_evidence:
        if not data.get(key):
            issues.append(f"人物注册表缺少真实证据字段:{key}")
    scanned = data.get("scanned_episodes", [])
    if source_count is not None:
        try:
            expected = set(range(1, int(source_count) + 1))
            actual = {int(x) for x in scanned}
            if actual != expected:
                issues.append(f"逐集扫描集合不一致:缺少={sorted(expected-actual)} 多出={sorted(actual-expected)}")
        except (TypeError, ValueError):
            issues.append("scanned_episodes格式错误")
    pending = data.get("pending_new_characters", [])
    unmatched = data.get("unmatched_candidates", [])
    if pending:
        issues.append(f"存在{len(pending)}个待核验人物")
    if unmatched:
        issues.append(f"存在{len(unmatched)}条未处理人物候选")
    scans = data.get("episode_scans", {})
    if not isinstance(scans, dict):
        issues.append("episode_scans不是对象")
    else:
        for episode, scan in scans.items():
            if not isinstance(scan, dict) or scan.get("status") in {"failed", "blocked"}:
                issues.append(f"{episode}:逐集扫描未成功")
            if isinstance(scan, dict) and (scan.get("未登记人物") or scan.get("待核验人物")):
                issues.append(f"{episode}:仍有未登记或待核验人物")
    for key, record in chars.items():
        if not isinstance(record, dict):
            issues.append(f"角色{key}:注册记录不是对象")
            continue
        name = str(record.get("人物名") or key)
        status = str(record.get("状态") or record.get("status") or "")
        if status in {"待核验", "pending", "blocked"}:
            issues.append(f"角色{name}:状态未通过:{status}")
        assets = record.get("资产文件", record.get("assets", []))
        if isinstance(assets, list) and len(assets) > 5:
            issues.append(f"角色{name}:资产超过5张")
        if status == "无合格单人物图" and not (record.get("排除证据") or record.get("exclusion_evidence")):
            issues.append(f"角色{name}:无合格单人物图但缺少逐图排除证据")
        review = record.get("review") or record.get("审核记录")
        if status == "已确认" and not review:
            issues.append(f"角色{name}:已确认但缺少审核记录")
    return issues


def package_entries(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(item["package"]): item
        for item in manifest.get("packages", [])
        if isinstance(item, dict) and item.get("package")
    }


def validate_script_diversity(root: Path, expected: dict[str, dict[str, Any]]) -> list[str]:
    """Reject title-only variants and duplicate script bodies."""
    issues: list[str] = []
    by_episode: dict[str, list[tuple[str, str]]] = {}
    for package_name, item in expected.items():
        script_rel = str(item.get("script") or "03_脚本/Seedance_一键复制.md")
        path = root / package_name / script_rel
        if not path.exists():
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        episode = str(item.get("episode") or "")
        by_episode.setdefault(episode, []).append((package_name, digest))
    for episode, rows in by_episode.items():
        seen: dict[str, list[str]] = {}
        for package_name, digest in rows:
            seen.setdefault(digest, []).append(package_name)
        if any(len(names) > 1 for names in seen.values()):
            duplicates = [names for names in seen.values() if len(names) > 1]
            issues.append(f"第{episode}集五种版本脚本存在重复内容哈希:{duplicates}")
        if len(rows) == 5 and len(seen) != 5:
            issues.append(f"第{episode}集五种版本未形成五个不同脚本内容")
    all_hashes: dict[str, list[str]] = {}
    for rows in by_episode.values():
        for package_name, digest in rows:
            all_hashes.setdefault(digest, []).append(package_name)
    for digest, names in all_hashes.items():
        if len(names) > 1:
            issues.append(f"跨包脚本内容重复:{names[:8]}")
    return issues


def validate_package(
    pkg: Path,
    expected: dict[str, Any] | None,
    registry_roles: set[str],
    registry_status: dict[str, str],
    image_cache: dict[tuple[int, int, int], list[str]],
) -> list[str]:
    issues: list[str] = []
    people = pkg / "01_人物图片"
    scenes = pkg / "02_场景图片"
    scripts = pkg / "03_脚本"
    if not people.is_dir() or not scenes.is_dir() or not scripts.is_dir():
        return ["目录不完整"]
    if expected and expected.get("script"):
        expected_script = pkg / str(expected["script"])
        if not expected_script.exists():
            issues.append(f"生成清单声明脚本不存在:{expected_script.relative_to(pkg)}")
    person_files = sorted(p for p in people.iterdir() if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"})
    scene_files = sorted(p for p in scenes.iterdir() if p.is_file() and p.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"})
    role_counts: dict[str, int] = {}
    for p in person_files:
        if not PERSON_NAME_RE.match(p.stem):
            issues.append(f"{p.name}:人物文件名不符合规范")
        role = p.stem.split("_", 3)[1] if p.stem.startswith("人物_") else p.stem
        role_counts[role] = role_counts.get(role, 0) + 1
        if registry_roles and normalize_role(role) not in registry_roles:
            issues.append(f"{p.name}:人物角色未登记:{role}")
        elif registry_status.get(normalize_role(role)) != "已确认":
            issues.append(f"{p.name}:人物角色未完成身份确认:{role}")
        issues.extend(f"{p.name}:{x}" for x in check_image(p, image_cache))
    for role, count in role_counts.items():
        if count > 5:
            issues.append(f"人物{role}图片超过5张")
    if len(scene_files) > 5:
        issues.append(f"场景图超过5张:{len(scene_files)}")
    for p in scene_files:
        if not SCENE_NAME_RE.match(p.stem):
            issues.append(f"{p.name}:场景文件名不符合规范")
        if any(token in p.stem for token in ("人物", "角色", "脸", "眼", "泪", "表情", "说话", "原片空间与道具")):
            issues.append(f"{p.name}:疑似人物近景或泛化场景名")
        issues.extend(f"{p.name}:{x}" for x in check_image(p, image_cache))
    scripts_files = sorted(scripts.glob("*.md"))
    if len(scripts_files) != 1:
        issues.append(f"脚本文件数量必须为1:{len(scripts_files)}")
    for script in scripts_files:
        text = script.read_text(encoding="utf-8", errors="replace")
        if not scene_files and "无合格场景图" not in text:
            issues.append(f"{script.name}:场景为空但未说明原因")
        for token in REQUIRED:
            if token not in text:
                issues.append(f"{script.name}:缺少{token}")
        for token in FORBIDDEN:
            if token in text:
                issues.append(f"{script.name}:含占位词{token}")
        if "【一键复制开始】" not in text or "【一键复制结束】" not in text:
            issues.append(f"{script.name}:缺少一键复制范围")
        if text.count("【一键复制开始】") != 1 or text.count("【一键复制结束】") != 1:
            issues.append(f"{script.name}:一键复制标记不唯一")
        if text.find("【一键复制开始】") > text.find("【一键复制结束】"):
            issues.append(f"{script.name}:一键复制标记顺序错误")
        shot_blocks = [
            x for x in re.split(r"(?=^###\s*镜头)", text, flags=re.M)
            if x.lstrip().startswith("###") and "镜头" in x[:40]
        ]
        if not 8 <= len(shot_blocks) <= 12:
            issues.append(f"{script.name}:镜头数必须为8—12，实际{len(shot_blocks)}")
        shot_times = [(float(a), float(b)) for a, b in SHOT_HEAD_RE.findall(text)]
        if len(shot_times) != len(shot_blocks):
            issues.append(f"{script.name}:每个镜头必须有明确起止时间")
        if not shot_times or shot_times[0][0] != 0.0 or shot_times[-1][1] != 30.0:
            issues.append(f"{script.name}:镜头时间轴必须覆盖0.0—30.0秒")
        for prev, curr in zip(shot_times, shot_times[1:]):
            if abs(prev[1] - curr[0]) > 1e-6:
                issues.append(f"{script.name}:镜头时间轴有空档或重叠:{prev[1]}->{curr[0]}")
        if not any(abs(a - 15.0) < 1e-6 or abs(b - 15.0) < 1e-6 for a, b in shot_times):
            issues.append(f"{script.name}:缺少15.0秒前后承接点")
        if "第一段尾帧" not in text or "第二段" not in text or "从上一段尾帧" not in text:
            issues.append(f"{script.name}:缺少前后15秒连续承接证据")
        if len(re.findall(r"权力变化|立场变化", text)) < 3:
            issues.append(f"{script.name}:权力/立场变化少于3次")
        if len(re.findall(r"转折", text)) < 2:
            issues.append(f"{script.name}:转折少于2次")
        if "讨论问题" not in text or "尾帧问题" not in text:
            issues.append(f"{script.name}:缺少讨论问题或尾帧问题")
        for idx, block in enumerate(shot_blocks, 1):
            if not re.search(r"动作|抬|放|压|指|转身|走|站|坐|推|拉|举|拍|移|停", block):
                issues.append(f"{script.name}:镜头{idx}缺少可执行动作")
            if not re.search(r"神态|表情|眼神|眉|嘴角|下颌|呼吸|视线|目光", block):
                issues.append(f"{script.name}:镜头{idx}缺少面部/神态细节")
            if "使用图片" not in block or "场景图" not in block:
                issues.append(f"{script.name}:镜头{idx}缺少人物/场景图片绑定")
            for ref in IMAGE_REF_RE.findall(block):
                ref_path = people / ref
                if not ref_path.exists():
                    ref_path = scenes / ref
                if not ref_path.exists():
                    issues.append(f"{script.name}:镜头{idx}引用不存在:{ref}")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description="严格验收短剧二次发行包")
    parser.add_argument("root", type=Path)
    parser.add_argument("--manifest", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    manifest_path = (args.manifest or root / "生成清单.json").expanduser().resolve()
    manifest = read_json(manifest_path)
    root_issues = validate_source_evidence(root, manifest)
    source_count = manifest.get("source_count") if manifest else None
    root_issues.extend(validate_registry(root, source_count))
    expected = package_entries(manifest)
    actual_dirs = {
        p.name: p for p in root.iterdir()
        if p.is_dir() and not p.name.startswith(".") and p.name not in {"__pycache__"}
    } if root.is_dir() else {}
    actual_packages = {name: path for name, path in actual_dirs.items() if re.match(r"^\d{2}_", name)}
    expected_names = set(expected)
    actual_names = set(actual_packages)
    for name in sorted(expected_names - actual_names):
        root_issues.append(f"缺少清单声明的发行包:{name}")
    for name in sorted(actual_names - expected_names):
        root_issues.append(f"存在清单未声明的发行包:{name}")
    if manifest.get("package_count") is not None and int(manifest["package_count"]) != len(expected_names):
        root_issues.append("package_count与packages集合数量不一致")
    registry = read_json(root / "人物角色注册表.json")
    chars = registry.get("characters", {}) if isinstance(registry, dict) else {}
    if isinstance(chars, list):
        chars = {str(x.get("人物名", "")): x for x in chars if isinstance(x, dict)}
    registry_roles = {normalize_role(k) for k in chars} if isinstance(chars, dict) else set()
    registry_status: dict[str, str] = {}
    if isinstance(chars, dict):
        for key, value in chars.items():
            if isinstance(value, dict):
                status = str(value.get("状态") or value.get("status") or "")
                registry_status[normalize_role(key)] = status
                if value.get("人物名"):
                    registry_status[normalize_role(value["人物名"])] = status
    image_cache: dict[tuple[int, int, int], list[str]] = {}
    issues: dict[str, list[str]] = {}
    for name in sorted(expected_names & actual_names):
        issues[name] = validate_package(actual_packages[name], expected.get(name), registry_roles, registry_status, image_cache)
    root_issues.extend(validate_script_diversity(root, expected))
    result = {
        "root": str(root),
        "manifest": str(manifest_path),
        "package_count": len(actual_packages),
        "expected_package_count": len(expected_names),
        "unique_image_checks": len(image_cache),
        "root_issues": root_issues,
        "issues": issues,
        "issue_count": len(root_issues) + sum(len(v) for v in issues.values()),
        "status": "passed" if not root_issues and not any(issues.values()) else "blocked",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else f"packages={result['package_count']} issues={result['issue_count']} status={result['status']}")
    return 0 if result["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
