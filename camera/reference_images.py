"""按用途挑选和冻结相机参考，不让参考图取景覆盖当次拍摄方案。"""

from pathlib import Path
from typing import Any

import hashlib
import json
import tomllib


def build_references(plugin_dir: Path, camera_root: Path, record: dict[str, Any], plan: dict[str, Any]) -> list[bytes]:
    folder = camera_root / record["id"]
    folder.mkdir(parents=True, exist_ok=True)
    root = (plugin_dir / "camera/references").resolve()
    manifest = json.loads((root / "references.json").read_text())
    images: list[bytes] = []
    record["references"] = []
    record["style_version"] = manifest["version"]

    def add(raw: bytes, role: str, kind: str, **detail: Any) -> None:
        name = f"reference-{len(images) + 1}.png"
        (folder / name).write_bytes(raw)
        images.append(raw)
        record["references"].append({
            "file": name, "index": len(images), "kind": kind, "role": role,
            "sha256": hashlib.sha256(raw).hexdigest(), **detail,
        })

    def fixed(entry: dict[str, Any], role: str, kind: str) -> None:
        target = (root / entry["file"]).resolve()
        if not target.is_relative_to(root):
            raise ValueError("固定参考图路径越界")
        raw = target.read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            raise ValueError("固定参考图摘要不一致：" + entry["file"])
        add(raw, role, kind, source=entry["file"])

    primary = next(entry for entry in manifest["references"] if entry["category"] == "portrait")
    fixed(primary, "v4 画风主基准：只控制线条、色彩、阴影与细节密度，不复制构图或人物姿势", "style")
    if plan["style_category"] != "portrait":
        supplement = next(entry for entry in manifest["references"] if entry["category"] == plan["style_category"])
        fixed(supplement, "对应主体的画风补充：只比较画法，不复制物件、构图或光照", "style")
    identity = plan["identity_reference"]
    if identity != "none":
        fixed(manifest["identity"][identity],
              "人物外貌参考：只控制银白浅樱粉头发与可见轮廓；发型随当前活动，不复制脸、衣服、配饰或姿势", "identity")
    if plan["include_outfit"]:
        with (plugin_dir / "character/outfit_images.toml").open("rb") as handle:
            mapping = tomllib.load(handle)["images"]
        name = record["snapshot"]["outfit"]
        if name not in mapping:
            raise ValueError(f"当前穿搭「{name}」缺少设定图，无法保证照片穿搭一致")
        wardrobe_root = (plugin_dir / "character").resolve()
        target = (wardrobe_root / mapping[name]).resolve()
        if not target.is_relative_to(wardrobe_root):
            raise ValueError("服饰参考图路径越界")
        add(target.read_bytes(), "当前穿搭款式参考：仅款式；忽略白底、标注、平铺和整身展示", "outfit", outfit=name)
    if plan["use_previous_photo"]:
        previous = record.get("previous_photo")
        if not previous:
            raise ValueError("拍摄方案要求沿用旧照片，但当前会话没有已发送的照片")
        target = (camera_root / previous["id"] / previous["file"]).resolve()
        if not target.is_relative_to(camera_root.resolve()):
            raise ValueError("旧照片路径越界")
        add(target.read_bytes(), "同一会话上一张照片：仅保持本次连续拍摄对象一致，不固定新机位", "previous")
    return images
