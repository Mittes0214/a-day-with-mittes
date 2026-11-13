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
    assert manifest["version"] == "mittes-phone-photo-style-v6"
    assert {entry["category"] for entry in manifest["references"]} == {"portrait"}
    entries = [*manifest["references"], manifest["scene_reference"], *manifest["identity"].values(), *manifest["scene_supplements"], *manifest["clothing_supplements"]]
    assert {entry["category"] for entry in manifest["scene_supplements"]} == {"street", "terrace", "train", "cafe", "night_desk", "classroom"}
    assert {entry["category"] for entry in manifest["clothing_supplements"]} == {"socks", "shoes"}
    shoes = next(entry for entry in manifest["clothing_supplements"] if entry["category"] == "shoes")
    assert shoes["sha256"] == "f9b8791d49c4e27f512a25cf2fe26b6f36b82ff848d63c834570da8120f7bc16"
    assert {str(path.relative_to(folder)) for path in folder.rglob("*.png")} == {entry["file"] for entry in entries}
    for entry in entries:
        raw = (folder / entry["file"]).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == entry["sha256"]
        with Image.open(BytesIO(raw)) as image:
            assert image.format == "PNG"
            image.verify()
    assert "d9be7298-4cb1-4923-bbc8-4d4e2154c6a2.png" in manifest["identity"]["head"]["source"]
    assert manifest["scene_reference"]["sha256"] == "a7883f7b5010742b02cc65ce08ff7367ec78157d09b479e8a347e991f66a9a5b"


def test_snapshot_contains_appearance_and_actual_place_without_innerwear():
    plugin = plugin_module.ADayWithMittesPlugin()
    wardrobe = plugin_module.Wardrobe(ROOT / "character/wardrobe.toml", ROOT / "character/seasons")
    wardrobe.load()
    plugin._wardrobe = wardrobe
    segment = SimpleNamespace(slot="22:30-24:00", title="洗澡、写手账", outfit="浅雾蓝荷叶领长袖睡裙", place="二楼浴室 / 三楼卧室",
                              company="一个人", kind="自由", start_minutes=22 * 60 + 30)
    plugin._store = SimpleNamespace(
        place_at=lambda *args: "卧室书桌前", resolve_moment=lambda moment: (date(2026, 10, 18), 23 * 60 + 30),
        archived_segments=lambda day: [(segment, state)],
        db=SimpleNamespace(day_weather=lambda day: "晴"))
    state = SimpleNamespace(places=[], story="洗完澡回卧室写手账", topic="")
    plugin._current = lambda: (datetime.fromisoformat("2026-10-18T23:30:00+09:00"), date(2026, 10, 18), segment, state)
    snapshot = plugin._photo_snapshot()
    assert snapshot["place"] == "卧室书桌前"
    assert snapshot["skeleton_place"] == "二楼浴室 / 三楼卧室"
    assert snapshot["weather"] == "晴" and snapshot["company"] == "一个人"
    # 一个人待着的时段不会有别人帮拍的照片，不列为分享来源
    assert snapshot["share_options"] == [] and snapshot["helpers"] == []
    segment.company = "Niya、老师"
    snapshot = plugin._photo_snapshot()
    assert snapshot["helpers"] == ["Niya"]
    assert [o["logical_day"] for o in snapshot["share_options"]] == [f"2026-10-{d}" for d in range(18, 11, -1)]
    assert "银白" in snapshot["appearance"]["hair_base"]
    assert "underwear" not in snapshot["outfit_details"]
    assert "缎带" in snapshot["outfit_details"]["hair"]
    snapshot["appearance"]["hair_base"] = "changed"
    assert "银白" in wardrobe.appearance["hair_base"]


def test_every_outfit_states_hair_and_activity_keeps_special_style():
    wardrobe = plugin_module.Wardrobe(ROOT / "character/wardrobe.toml", ROOT / "character/seasons")
    wardrobe.load()
    for name in wardrobe.names:
        assert wardrobe.get(name).parts["hair"]
    assert "高马尾" in wardrobe.get("红色运动外套配白色体操衫和运动长裤").parts["hair"]


def test_archived_season_outfits_resolve_only_for_looking_back():
    wardrobe = plugin_module.Wardrobe(ROOT / "character/wardrobe.toml", ROOT / "character/seasons")
    wardrobe.load()
    # 秋季女仆装已换季：当季查询查不到，回看过去的照片时仍能取到当时的细节
    assert "黑白女仆装" not in wardrobe.names and wardrobe.get("黑白女仆装") is None
    assert wardrobe.get_any("黑白女仆装").parts["top"]
    assert wardrobe.get_any("黑色长袖长裙女仆装配白色长围裙") is wardrobe.get("黑色长袖长裙女仆装配白色长围裙")


def test_share_option_keeps_the_outfit_archived_for_that_day():
    plugin = plugin_module.ADayWithMittesPlugin()
    wardrobe = plugin_module.Wardrobe(ROOT / "character/wardrobe.toml", ROOT / "character/seasons")
    wardrobe.load()
    plugin._wardrobe = wardrobe
    autumn = plugin_module.Segment(start="17:00", end="19:00", title="换上女仆装接客", place="一楼营业区",
                                   outfit="黑白女仆装", company="客人、Sweyn、Niya", kind="工作")
    state = SimpleNamespace(places=[], story="放学后的客人", topic="")
    plugin._store = SimpleNamespace(archived_segments=lambda day: [(autumn, state)] if day == date(2026, 11, 13) else [],
                                    db=SimpleNamespace(day_weather=lambda day: "晴"))
    options = plugin._share_options(date(2026, 11, 14), 10 * 60)
    assert [(o["logical_day"], o["slot"], o["outfit"]) for o in options] == [("2026-11-13", "17:00-19:00", "黑白女仆装")]
    assert options[0]["outfit_details"]["top"] == wardrobe.get_any("黑白女仆装").parts["top"]


def test_outfit_images_cover_current_and_archived_outfits():
    module = importlib.import_module("plugins.01_a_day_with_mittes.camera.reference_images")
    mapping = module._outfit_image_mapping(ROOT / "character")
    wardrobe = plugin_module.Wardrobe(ROOT / "character/wardrobe.toml", ROOT / "character/seasons")
    wardrobe.load()
    for name in [*wardrobe.names, "黑白女仆装", "白色长袖水手服配藏青百褶裙"]:
        assert (ROOT / "character" / mapping[name]).is_file()


def test_archived_segments_read_the_snapshot_stored_with_that_day(tmp_path):
    store = plugin_module.ScheduleStore(ROOT / "schedule/skeleton.toml", tmp_path)
    store.load_skeleton()
    store.open_db()
    try:
        autumn = plugin_module.Segment(start="14:30", end="15:30", title="换装", place="一楼员工更衣空间",
                                       outfit="黑白女仆装", company="Niya", kind="工作")
        store.db.upsert_segment(date(2026, 11, 13), 0, autumn, plugin_module.SegmentState(story="和 Niya 互相整理蝴蝶结", manner="", mood=""))
        # 现行冬季骨架没有 14:30-15:30 这一段，归档照样按当天记下的快照返回
        assert all(segment.slot != "14:30-15:30" for segment in store.segments_of(date(2026, 11, 13)))
        [(segment, state)] = store.archived_segments(date(2026, 11, 13))
        assert segment == autumn and segment.start_minutes == 14 * 60 + 30
        assert state.story == "和 Niya 互相整理蝴蝶结"
        assert store.archived_segments(date(2026, 11, 12)) == []
    finally:
        store.close_db()
