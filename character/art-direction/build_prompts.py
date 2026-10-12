"""从衣柜生成服饰图片任务；只输出提示词，不调用图像 API。"""

from argparse import ArgumentParser
from hashlib import sha256
from pathlib import Path

import json
import tomllib


ROOT = Path(__file__).resolve().parent
CHARACTER = ROOT.parent
LABELS = {
    "top": "上身", "bottom": "下身", "legs": "鞋袜中的袜子",
    "feet": "鞋", "accessories": "配饰", "optional": "可选单品",
}


def build(part: str, excluded: set[str]) -> dict:
    references = json.loads((ROOT / "references.json").read_text(encoding="utf-8"))
    for ref in references["references"]:
        actual = sha256((ROOT / ref["filename"]).read_bytes()).hexdigest()
        if actual != ref["sha256"]:
            raise ValueError(f"固定画风参考已变动：{ref['filename']}")
    wardrobe_path = CHARACTER / "wardrobe.toml"
    raw = wardrobe_path.read_bytes()
    wardrobe = tomllib.loads(raw.decode("utf-8"))["wardrobe"]
    unknown = excluded - wardrobe.keys()
    if unknown:
        raise ValueError(f"衣柜没有这些套装：{sorted(unknown)}")
    parts = ("outer", "inner") if part == "both" else (part,)
    images = []
    for number, (name, outfit) in enumerate(wardrobe.items(), 1):
        if name in excluded:
            continue
        for layer in parts:
            description = (
                "\n".join(f"{label}：{outfit[key]}" for key, label in LABELS.items() if outfit.get(key))
                if layer == "outer" else outfit["underwear"].strip()
            )
            if layer == "outer" and outfit.get("hair"):
                description += "\n发饰规则（只提取发饰，不画头发）：" + outfit["hair"]
            prompt = (ROOT / f"{layer}.prompt.txt").read_text(encoding="utf-8").format(
                name=name, description=description,
            )
            images.append({
                "number": number, "outfit": name, "part": layer,
                "filename": f"{number:02d}_{name}_{'外搭' if layer == 'outer' else '内搭'}.png",
                "references": [str(ROOT / f"reference-{layer}.png")],
                "reference_role": "fixed style and presentation only",
                "prompt": prompt,
            })
    return {
        "style_version": references["style_version"], "tool": "imagegen (built-in)",
        "wardrobe_sha256": sha256(raw).hexdigest(), "images": images,
    }


def main() -> None:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--part", choices=("outer", "inner", "both"), default="both")
    parser.add_argument("--exclude", action="append", default=[])
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    result = json.dumps(build(args.part, set(args.exclude)), ensure_ascii=False, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(result, encoding="utf-8")
    else:
        print(result, end="")


if __name__ == "__main__":
    main()
