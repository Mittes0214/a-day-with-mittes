"""部署的固定参考、衣柜外貌与精确地点快照，不依赖工作区外的原图。"""

from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import hashlib
import importlib
import json

from PIL import Image


plugin_module = importlib.import_module("plugins.01_a_day_with_mittes.plugin")
ROOT = Path(__file__).resolve().parents[1]


def test_deployed_reference_manifest_matches_all_files():
    folder = ROOT / "camera/references"
    manifest = json.loads((folder / "references.json").read_text())
    assert manifest["version"] == "mittes-phone-photo-style-v3"
    assert {entry["category"] for entry in manifest["references"]} == {"portrait", "food", "drink", "outdoor", "object"}
    for entry in [*manifest["references"], manifest["scene_reference"], *manifest["identity"].values()]:
        raw = (folder / entry["file"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
        with Image.open(BytesIO(raw)) as image:
            assert image.format == "PNG"
            image.verify()
    assert "d9be7298-4cb1-4923-bbc8-4d4e2154c6a2.png" in manifest["identity"]["head"]["source"]
    assert manifest["scene_reference"]["sha256"] == "a7883f7b5010742b02cc65ce08ff7367ec78157d09b479e8a347e991f66a9a5b"


def test_snapshot_contains_appearance_and_actual_place_without_innerwear():
    plugin = plugin_module.ADayWithMittesPlugin()
    wardrobe = plugin_module.Wardrobe(ROOT / "character/wardrobe.toml")
    wardrobe.load()
    plugin._wardrobe = wardrobe
    plugin._store = SimpleNamespace(place_at=lambda *args: "卧室书桌前")
    segment = SimpleNamespace(slot="22:30-24:00", title="洗澡、写手账", outfit="浅蓝细棉长袖睡衣")
    state = SimpleNamespace(places=[], story="洗完澡回卧室写手账", topic="")
    plugin._current = lambda: (datetime.fromisoformat("2026-10-18T23:30:00+09:00"), date(2026, 10, 18), segment, state)
    snapshot = plugin._photo_snapshot()
    assert snapshot["place"] == "卧室书桌前"
    assert "银白" in snapshot["appearance"]["hair_base"]
    assert "underwear" not in snapshot["outfit_details"]
    assert "干发毛巾" in snapshot["outfit_details"]["hair"]
    snapshot["appearance"]["hair_base"] = "changed"
    assert "银白" in wardrobe.appearance["hair_base"]


def test_everyday_wardrobe_uses_twin_tails_and_activity_keeps_special_style():
    wardrobe = plugin_module.Wardrobe(ROOT / "character/wardrobe.toml")
    wardrobe.load()
    for name in wardrobe.names:
        hair = wardrobe.get(name).parts["hair"]
        assert "双马尾" in hair or name == "白色短款外套配浅樱粉滑冰练习裙"
    assert "高马尾" in wardrobe.get("白色短款外套配浅樱粉滑冰练习裙").parts["hair"]
