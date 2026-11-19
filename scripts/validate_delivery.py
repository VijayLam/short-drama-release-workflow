#!/usr/bin/env python3
"""验证短剧二次发行包的图片、脚本引用和时间轴硬门槛。"""
from __future__ import annotations
import argparse, json, re
from pathlib import Path
from PIL import Image, ImageFilter, ImageStat

REQUIRED = ["镜头", "人物", "台词", "字幕", "BGM", "音效", "尾帧", "禁止项"]
FORBIDDEN = ("待补", "时间待定", "自行发挥", "见 Excel", "TODO")
TIME_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[—-]\s*(\d+(?:\.\d+)?)\s*秒")
SHOT_TIME_RE = re.compile(r"^###\s*镜头.*?(\d+(?:\.\d+)?)\s*[—-]\s*(\d+(?:\.\d+)?)\s*秒", re.M)
PERSON_NAME_RE = re.compile(r"^人物_[^_]+_[^_]+_[^_]+_第\d+集_\d+分\d+(?:\.\d+)?秒$")
SCENE_NAME_RE = re.compile(r"^场景_[^_]+_[^_]+_第\d+集_\d+分\d+(?:\.\d+)?秒$")

def check_image(path: Path) -> list[str]:
    issues=[]
    try:
        with Image.open(path) as im:
            im.verify()
        with Image.open(path).convert("L") as im:
            if min(im.size) < 480:
                issues.append(f"分辨率过低:{im.size[0]}x{im.size[1]}")
            small = im.resize((256,256))
            mean = ImageStat.Stat(small.resize((64,64))).mean[0]
            if mean < 8:
                issues.append("过暗")
            edge_std = ImageStat.Stat(small.filter(ImageFilter.FIND_EDGES)).stddev[0]
            if edge_std < 8.0:
                issues.append(f"清晰度不足:边缘锐度{edge_std:.2f}")
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
        if not PERSON_NAME_RE.match(name):
            issues.append(f"{p.name}:人物文件名不符合人物_人物名_动作_神态_集数_时间格式")
        role=name.split("_", 2)[1] if name.startswith("人物_") and name.count("_")>=2 else name
        role_counts[role]=role_counts.get(role,0)+1
        issues.extend(f"{p.name}:{x}" for x in check_image(p))
    for role,count in role_counts.items():
        if count>5: issues.append(f"人物{role}图片超过5张")
    scene_files=sorted(p for p in scenes.iterdir() if p.suffix.lower() in {".png",".jpg",".jpeg",".webp"})
    if len(scene_files) < 3:
        issues.append(f"场景图不足3张:当前{len(scene_files)}张")
    for p in scene_files:
        name=p.stem
        if not SCENE_NAME_RE.match(name):
            issues.append(f"{p.name}:场景文件名不符合场景_地点_状态或道具_集数_时间格式")
        if "原片空间与道具" in name or any(token in name for token in ("人物", "角色", "脸", "眼", "泪", "表情", "说话")):
            issues.append(f"{p.name}:疑似人物近景或泛化场景名，不能作为场景图")
        issues.extend(f"{p.name}:{x}" for x in check_image(p))
    scripts_files=sorted(scripts.glob("*.md"))
    if not scripts_files: issues.append("缺少Markdown脚本")
    for s in scripts_files:
        text=s.read_text(encoding="utf-8", errors="replace")
        for token in REQUIRED:
            if token not in text: issues.append(f"{s.name}:缺少{token}")
        for token in FORBIDDEN:
            if token in text: issues.append(f"{s.name}:含占位词{token}")
        if "【一键复制开始】" not in text or "【一键复制结束】" not in text:
            issues.append(f"{s.name}:缺少一键复制范围标记")
        if text.find("【一键复制开始】") > text.find("【一键复制结束】"):
            issues.append(f"{s.name}:一键复制标记顺序错误")
        if not re.search(r"第一段.*0\.0\s*[—-]\s*15\.0秒", text):
            issues.append(f"{s.name}:缺少第一段0.0—15.0秒标记")
        if not re.search(r"第二段.*15\.0\s*[—-]\s*30\.0秒", text):
            issues.append(f"{s.name}:缺少第二段15.0—30.0秒标记")
        if "第一段尾帧" not in text or "第二段尾帧" not in text:
            issues.append(f"{s.name}:缺少前后15秒尾帧说明")
        if not re.search(r"从上一段尾帧|从同一尾帧|动作连续承接|直接承接", text):
            issues.append(f"{s.name}:缺少第二段承接说明")
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
        shot_times=[(float(a),float(b)) for a,b in SHOT_TIME_RE.findall(text)]
        check_times=shot_times or times
        if not any(a==0.0 and b>0 for a,b in check_times): issues.append(f"{s.name}:未从0.0秒开始")
        if not any(b==30.0 for a,b in check_times): issues.append(f"{s.name}:未覆盖到30.0秒")
        if shot_times:
            if not any(abs(b-15.0)<1e-6 for a,b in shot_times) or not any(abs(a-15.0)<1e-6 for a,b in shot_times):
                issues.append(f"{s.name}:前后15秒没有在15.0秒处衔接")
            for prev, curr in zip(shot_times, shot_times[1:]):
                if abs(prev[1]-curr[0]) > 1e-6:
                    issues.append(f"{s.name}:镜头时间轴有空档或重叠:{prev[1]}->{curr[0]}")
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
