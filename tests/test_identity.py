"""认图：公开照片与头像的哈希识别、外貌比对结论与渲染，不请求真实模型或 QQ。"""

from contextlib import closing
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import asyncio
import base64
import hashlib
import importlib
import json
import random
import sqlite3
import sys

from PIL import Image
import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
identity = importlib.import_module("plugins.01_a_day_with_mittes.camera.identity")
store_module = importlib.import_module("plugins.01_a_day_with_mittes.camera.store")
PLUGIN_DIR = Path(__file__).resolve().parents[1]
STREAM = "stream-a"


def pattern(seed: int, size=(1024, 1536)) -> bytes:
    rng = random.Random(seed)
    small = Image.frombytes("RGB", (8, 12), bytes(rng.randrange(256) for _ in range(8 * 12 * 3)))
    output = BytesIO()
    small.resize(size, Image.Resampling.BICUBIC).save(output, format="PNG")
    return output.getvalue()


def degrade(raw: bytes) -> bytes:
    """模拟 QQ 转发：缩到 40% 再压成低质量 JPEG。"""
    with Image.open(BytesIO(raw)) as image:
        small = image.convert("RGB").resize((image.width * 2 // 5, image.height * 2 // 5))
    output = BytesIO()
    small.save(output, format="JPEG", quality=55)
    return output.getvalue()


def add_task(root: Path, status: str, raw: bytes, **extra) -> str:
    task_id = hashlib.md5(raw + status.encode() + json.dumps(extra, sort_keys=True).encode()).hexdigest()
    (root / task_id).mkdir(parents=True)
    (root / task_id / "attempt-1.png").write_bytes(raw)
    record = {
        "id": task_id, "status": status, "stream_id": extra.pop("stream_id", STREAM),
        "final_image": "attempt-1.png",
        "images": [{"file": "attempt-1.png", "sha256": hashlib.sha256(raw).hexdigest()}],
        "snapshot": {"captured_at": "2026-11-23T10:46:00+00:00", "place": "厨房", "title": "两个人做午饭"},
        "plan": {"contains_mittes": True}, "description": "围裙和锅铲", "requester_name": "阿七", **extra,
    }
    with closing(sqlite3.connect(root / "camera.db")) as con, con:
        con.execute("INSERT INTO tasks VALUES (?,?,?,?,?,?,?)",
                    (task_id, record["stream_id"], task_id, status, task_id, task_id, json.dumps(record, ensure_ascii=False)))
    return task_id


def publish_daily(data_dir: Path, task_id: str, status: str = "published") -> None:
    root = data_dir / "daily_share"
    root.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(root / "daily_share.db")) as con, con:
        con.execute("CREATE TABLE IF NOT EXISTS posts (day TEXT PRIMARY KEY, status TEXT, created_at TEXT, updated_at TEXT, record TEXT)")
        con.execute("INSERT INTO posts VALUES (?,?,?,?,?)",
                    (task_id, status, "", "", json.dumps({"camera_task_id": task_id})))


def message(raw: bytes, stream_id: str = STREAM) -> dict:
    return {"session_id": stream_id, "platform": "qq",
            "raw_message": [{"type": "text", "data": "这是你吗"},
                            {"type": "image", "binary_data_base64": base64.b64encode(raw).decode()}],
            "message_info": {"user_info": {"user_id": "10001", "user_nickname": "阿七"}}}


class FakeClient:
    responses: list = []
    calls: list = []

    def __init__(self, base_url, key, timeout):
        pass

    async def json(self, model, system, data, images=None):
        FakeClient.calls.append((model, system, images))
        return FakeClient.responses.pop(0), {}

    async def close(self):
        pass


@pytest.fixture
def service(tmp_path, monkeypatch):
    store_module.CameraStore(tmp_path / "camera").initialize()
    monkeypatch.setattr(identity, "read_provider", lambda name: ("http://cpa", "key"))
    FakeClient.responses, FakeClient.calls = [], []
    avatar = {"raw": pattern(99), "calls": []}

    async def fetch(qq):
        avatar["calls"].append(qq)
        return avatar["raw"]

    target = {}

    async def get_by_id(msg_id, chat_id="", include_binary_data=False):
        return target.get(msg_id)

    async def config_get(key, default=None):
        return {"bot.qq_account": "2000"}.get(key, default)

    async def get_config(key, default=None):
        return default

    ctx = SimpleNamespace(message=SimpleNamespace(get_by_id=get_by_id), config=SimpleNamespace(get=config_get))
    svc = identity.IdentityService(ctx, PLUGIN_DIR, tmp_path, get_config, client_factory=FakeClient, avatar_fetcher=fetch)
    asyncio.run(svc.start())
    return SimpleNamespace(svc=svc, root=tmp_path / "camera", data_dir=tmp_path, target=target, avatar=avatar)


def test_dhash_survives_qq_recompression_but_separates_photos():
    a, b = pattern(1), pattern(2)
    assert identity.distance(identity.dhash(a), identity.dhash(degrade(a))) <= identity.MATCH_DISTANCE
    assert identity.distance(identity.dhash(a), identity.dhash(b)) > identity.MATCH_DISTANCE * 2


def test_sent_photo_is_recognised_with_time_and_place(service):
    raw = pattern(1)
    task_id = add_task(service.root, "sent", raw)
    add_task(service.root, "sent", pattern(2))
    service.target["m1"] = message(degrade(raw))
    result = asyncio.run(service.svc.identify(STREAM, "m1", 0))
    assert result["result"] == "photo" and result["photo_id"] == task_id
    assert "2026-11-23 19:46" in result["content"] and "厨房" in result["content"]
    assert "当时就发在这个聊天里（给阿七拍的）" in result["content"]
    assert FakeClient.calls == []  # 认出照片就不调视觉模型
    # 哈希已缓存
    with closing(sqlite3.connect(service.root / "photo_hashes.db")) as con:
        assert con.execute("SELECT count(*) FROM hashes").fetchone()[0] == 2


def test_other_chat_and_scene_photo_wording(service):
    raw = pattern(3)
    add_task(service.root, "sent", raw, stream_id="stream-b", plan={"contains_mittes": False},
             share={"source": "Niya"})
    service.target["m1"] = message(raw)
    content = asyncio.run(service.svc.identify(STREAM, "m1", 0))["content"]
    assert "之前发在别的聊天里" in content and "阿七" not in content
    assert "Niya帮她拍的" in content and "没有她本人" in content


def test_reused_copies_and_unpublished_daily_shares_are_not_public(service):
    reused, unpublished, published = pattern(4), pattern(5), pattern(6)
    add_task(service.root, "sent", reused, reuse={"photo_id": "x"})
    draft = add_task(service.root, "daily_share", unpublished)
    posted = add_task(service.root, "daily_share", published)
    publish_daily(service.data_dir, draft, status="pending_review")
    publish_daily(service.data_dir, posted)
    ids = {photo["id"]: photo["channel"] for photo in service.svc.archive.published()}
    assert ids == {posted: "qzone"}


def test_current_avatar_is_recognised(service):
    service.target["m1"] = message(degrade(service.avatar["raw"]))
    result = asyncio.run(service.svc.identify(STREAM, "m1", 0))
    assert result["result"] == "avatar" and "QQ 头像" in result["content"]
    assert service.avatar["calls"] == ["2000"]
    assert FakeClient.calls == []


def test_unknown_image_goes_to_appearance_compare_and_is_cached(service):
    FakeClient.responses = [{"verdict": "not_match", "confidence": 0.9, "differences": ["黑色短发"], "reason": "整头黑色短发"}]
    service.target["m1"] = message(pattern(7))
    result = asyncio.run(service.svc.identify(STREAM, "m1", 0))
    assert result["verdict"] == "not_match"
    assert "第一句就明确说不是她" in result["content"] and "90%" in result["content"]
    model, system, images = FakeClient.calls[0]
    assert model == "gpt-6.2-sol" and system == identity.COMPARE and len(images) == 3
    asyncio.run(service.svc.identify(STREAM, "m1", 0))
    assert len(FakeClient.calls) == 1


def test_similar_advice_names_differences(service):
    FakeClient.responses = [{"verdict": "similar", "confidence": 0.6, "differences": ["蓝色眼睛"], "reason": "发色对，瞳色是蓝"}]
    service.target["m1"] = message(pattern(8))
    assert "（蓝色眼睛）" in asyncio.run(service.svc.identify(STREAM, "m1", 0))["content"]


def test_invalid_model_verdict_is_an_error(service):
    FakeClient.responses = [{"verdict": "same", "confidence": 0.9, "differences": [], "reason": "像"}]
    service.target["m1"] = message(pattern(9))
    with pytest.raises(ValueError, match="未知结论"):
        asyncio.run(service.svc.identify(STREAM, "m1", 0))


def test_message_errors(service):
    service.target["other"] = message(pattern(1), stream_id="stream-b")
    with pytest.raises(ValueError, match="不属于当前会话"):
        asyncio.run(service.svc.identify(STREAM, "other", 0))
    service.target["m1"] = message(pattern(1))
    with pytest.raises(ValueError, match="超出范围"):
        asyncio.run(service.svc.identify(STREAM, "m1", 1))
    with pytest.raises(ValueError, match="找不到消息"):
        asyncio.run(service.svc.identify(STREAM, "missing", 0))


def test_view_avatar_targets(service):
    own = asyncio.run(service.svc.view_avatar(STREAM, "", ""))
    assert own["qq_account"] == "2000" and own["content_items"][0]["mime_type"] == "image/png"
    service.target["m1"] = message(pattern(1))
    other = asyncio.run(service.svc.view_avatar(STREAM, "m1", ""))
    assert other["qq_account"] == "10001" and "阿七" in other["content"]
    with pytest.raises(ValueError, match="纯数字"):
        asyncio.run(service.svc.view_avatar(STREAM, "", "abc"))


def test_legacy_selfie_is_recognised_without_inventing_time(service):
    raw = (Path("/home/mittes/Mittes-1.0.0-dev/doc/Mittes人设图") / "0629-20260707-102522_自拍_0.jpg")
    if not raw.is_file():
        pytest.skip("本机没有旧自拍原图")
    service.target["m1"] = message(degrade(raw.read_bytes()))
    result = asyncio.run(service.svc.identify(STREAM, "m1", 0))
    assert result["result"] == "legacy_selfie" and "天台·水手服比耶" in result["content"]
    assert "不要编" in result["content"] and FakeClient.calls == []


def test_rendered_sentences_have_no_doubled_punctuation(service):
    raw = pattern(1)
    add_task(service.root, "sent", raw, description="一杯拿铁。")
    service.target["m1"] = message(raw)
    assert "。。" not in asyncio.run(service.svc.identify(STREAM, "m1", 0))["content"]
    FakeClient.responses = [{"verdict": "match", "confidence": 0.9, "differences": [], "reason": "发色瞳色都对。"}]
    service.target["m2"] = message(pattern(2))
    content = asyncio.run(service.svc.identify(STREAM, "m2", 0))["content"]
    assert "。。" not in content and "不要编拍摄时间地点" in content
