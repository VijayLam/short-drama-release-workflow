#!/usr/bin/env python3
"""验证短剧二次发行包的图片、脚本引用和时间轴硬门槛。"""
from __future__ import annotations
import argparse, json, re
from pathlib import Path
from PIL import Image, ImageStat

REQUIRED = ["镜头", "人物", "台词", "字幕", "BGM", "音效", "尾帧", "禁止项"]
FORBIDDEN = ("待补", "时间待定", "自行发挥", "见 Excel", "TODO")
TIME_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[—-]\s*(\d+(?:\.\d+)?)\s*秒")

def check_image(path: Path) -> list[str]:
    issues=[]
    try:
        with Image.open(path) as im:
            im.verify()
        with Image.open(path).convert("L") as im:
            mean = ImageStat.Stat(im.resize((64,64))).mean[0]
            if mean < 8:
                issues.append("过暗")
    except Exception as exc:
        issues.append(f"无法读取:{type(exc).__name__}")
    return issues

def validate_package(pkg: Path) -> list[str]:
    issues=[]
    people=pkg/"01_人物图片"; scenes=pkg/"02_场景图片"; scripts=pkg/"03_脚本"
    if not people.is_dir() or not scenes.is_dir() or not scripts.is_dir():
        return ["目录不完整"]
    person_files=sorted(p for p in people.iterdir() if p.suffix.lower() in {".png",".jpg",".jpeg",".webp"})
    role_counts={}
    for p in person_files:
        name=p.stem
        role=name.split("_", 2)[1] if name.startswith("人物_") and name.count("_")>=2 else name
        role_counts[role]=role_counts.get(role,0)+1
        issues.extend(f"{p.name}:{x}" for x in check_image(p))
    for role,count in role_counts.items():
        if count>5: issues.append(f"人物{role}图片超过5张")
    for p in sorted(scenes.iterdir()):
        if p.suffix.lower() in {".png",".jpg",".jpeg",".webp"}:
            issues.extend(f"{p.name}:{x}" for x in check_image(p))
    scripts_files=sorted(scripts.glob("*.md"))
    if not scripts_files: issues.append("缺少Markdown脚本")
    for s in scripts_files:
        text=s.read_text(encoding="utf-8", errors="replace")
        for token in REQUIRED:
            if token not in text: issues.append(f"{s.name}:缺少{token}")
        for token in FORBIDDEN:
            if token in text: issues.append(f"{s.name}:含占位词{token}")
        shot_blocks = [x for x in re.split(r"(?=###\s*镜头)", text) if x.lstrip().startswith("###") and "镜头" in x[:30]]
        for idx, block in enumerate(shot_blocks, 1):
            if "动作" not in block and not re.search(r"抬|放|压|指|转身|走|站|坐|推|拉|举|拍|移|停", block):
                issues.append(f"{s.name}:镜头{idx}缺少可执行动作")
            if not re.search(r"神态|表情|眼神|眉|嘴角|下颌|呼吸|视线|目光", block):
                issues.append(f"{s.name}:镜头{idx}缺少面部/神态细节")
            if "使用图片" not in block or "场景图" not in block:
                issues.append(f"{s.name}:镜头{idx}缺少图片对应关系")
        times=[]
        for a,b in TIME_RE.findall(text):
            times.append((float(a),float(b)))
        if not any(a==0.0 and b>0 for a,b in times): issues.append(f"{s.name}:未从0.0秒开始")
        if not any(b==30.0 for a,b in times): issues.append(f"{s.name}:未覆盖到30.0秒")
        for ref in re.findall(r"(?:使用图片|图片文件名|场景图)[:：]\s*([^\n；;]+)", text):
            ref=ref.strip().strip('`').rstrip('。；;，,')
            if ref and not (people/ref).exists() and not (scenes/ref).exists():
                issues.append(f"{s.name}:引用不存在:{ref}")
    return issues

def main():
    ap=argparse.ArgumentParser(); ap.add_argument("root", type=Path); ap.add_argument("--json", action="store_true"); args=ap.parse_args()
    packages=sorted(p for p in args.root.iterdir() if p.is_dir() and re.match(r"\d{2}_",p.name)) if args.root.is_dir() else []
    result={"root":str(args.root),"package_count":len(packages),"issues":{p.name:validate_package(p) for p in packages}}
    result["issue_count"]=sum(len(v) for v in result["issues"].values())
    print(json.dumps(result,ensure_ascii=False,indent=2) if args.json else f"packages={result['package_count']} issues={result['issue_count']}")
    return 0 if result["issue_count"]==0 else 2
if __name__=="__main__": raise SystemExit(main())
