"""每日分享：选题 → 相机配图 → 配文 → 审核 → 到点发布，不请求真实模型或 QQ 空间。"""

from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import asyncio
import importlib
import json
import sqlite3
import sys

import pytest

from test_camera import setup  # noqa: F401 —— 复用相机测试的假模型与夹具


sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
daily = importlib.import_module("plugins.01_a_day_with_mittes.daily.service")
daily_store = importlib.import_module("plugins.01_a_day_with_mittes.daily.store")
prompts = importlib.import_module("plugins.01_a_day_with_mittes.daily.prompts")
viewer = importlib.import_module("plugins.01_a_day_with_mittes.frontend.viewer")
JST = daily.JST
DAY = date(2026, 11, 17)
SEGMENTS = [
    {"slot": "02:00-07:30", "title": "睡眠", "place": "三楼卧室", "kind": "睡眠", "company": "一个人", "helpers": [],
     "outfit": "睡裙", "story": "", "topic": "", "mood": ""},
    {"slot": "15:30-17:00", "title": "放学去旧书店", "place": "神保町", "kind": "外出", "company": "Niya", "helpers": ["Niya"],
     "outfit": "校服", "story": "和 Niya 去旧书店淘书", "topic": "淘到一本旧诗集", "mood": "雀跃"},
    {"slot": "22:00-26:00", "title": "自由", "place": "三楼卧室", "kind": "自由", "company": "一个人", "helpers": [],
     "outfit": "睡裙", "story": "", "topic": "", "mood": ""},
]
PICK = {"skip": False, "reason": "雨天旧书店很有画面", "slot": "15:30-17:00", "time": "16:10",
        "subject": "旧书店门口雨棚下，手里捧着刚淘到的旧诗集", "angle": "雨天淘到喜欢的书"}
CAPTION = {"title": "雨天淘到一本旧诗集📚", "body": "放学和 Niya 去了旧书店。\n\n外面下着雨，书页有一点潮潮的味道，翻到一本旧诗集就舍不得放下了～\n\n今天的小确幸就是它啦。",
           "tags": ["日常碎片", "放学路上", "旧书店"]}


class Clock:
    def __init__(self, at):
        self.at = at

    def __call__(self):
        return self.at


@pytest.fixture
def share(setup, tmp_path, monkeypatch):  # noqa: F811
    camera, ctx, client, _ = setup
    monkeypatch.setattr(daily, "read_provider", lambda *args: ("http://test/v1", "secret"))
    client.picks = [dict(PICK)]
    client.captions = [dict(CAPTION)]
    client.caption_inputs = []
    original = client.json

    async def json_call(model, system, data, images=None):
        if system == prompts.PICK:
            client.pick_input = data
            return client.picks.pop(0), {}
        if system == prompts.CAPTION:
            client.caption_inputs.append((data, images))
            return client.captions.pop(0), {}
        return await original(model, system, data, images)

    client.json = json_call
    clock = Clock(datetime(2026, 11, 17, 0, 1, tzinfo=JST))
    publisher = AsyncMock(return_value={"status": "ok", "retcode": 0, "data": {"tid": "tid-1"}})
    config = {"photo.enabled": True, "daily_share.enabled": True}

    async def get_config(key, default=None):
        return config.get(key, default)

    def snapshot_at(moment):
        return {"captured_at": moment.isoformat(), "outfit": "校服", "place": "神保町", "helpers": ["Niya"],
                "company": "Niya", "title": "放学去旧书店", "story": "和 Niya 去旧书店淘书", "share_options": []}

    material = {"day": DAY.isoformat(), "weekday": "二", "weather": "小雨", "outline": "", "segments": SEGMENTS}
    service = daily.DailyShareService(camera, tmp_path / "data", get_config,
                                      lambda day: material if day == DAY else None, snapshot_at,
                                      now=clock, publisher=publisher)
    service.store.initialize()
    return service, camera, ctx, client, clock, publisher, config


async def until_review(service, camera):
    await service.tick()                       # 自动开始 + 选题 + 交给相机
    record = service.store.get(DAY.isoformat())
    assert record["status"] == "shooting"
    await camera.process(camera.store.next_queued())
    await service.tick()                       # 拿到配图
    await service.tick()                       # 写配文
    return service.store.get(DAY.isoformat())


def test_full_flow_waits_for_review_and_publishes_after_segment(share):
    async def run():
        service, camera, ctx, client, clock, publisher, _ = share
        record = await until_review(service, camera)
        assert record["status"] == "pending_review"
        assert record["caption"]["tags"] == CAPTION["tags"]
        # 配图走相机流程，但不发到任何聊天、不触发 planner、不占额度
        photo = camera.store.get(record["camera_task_id"])
        assert photo["status"] == "daily_share" and photo["quota"]["exempt"]
        assert "requester" not in client.planning_inputs and "earlier_photos" not in client.planning_inputs
        # 每日分享不受照片额度限制，三种照片都可选
        assert set(client.planning_inputs["photo_options"]) == {"casual", "selfie", "face"}
        assert client.planning_inputs["snapshot"]["captured_at"].startswith("2026-11-17T16:10")
        ctx.send.image.assert_not_awaited()
        ctx.maisaka.proactive.trigger.assert_not_awaited()
        # 配文看得到成品和检查描述
        data, images = client.caption_inputs[0]
        assert data["description"] == "桌上的杯子" and len(images) == 1
        assert "Niya" in client.pick_input["segments"][1]["helpers"]

        edited = dict(CAPTION, title="改过的标题✨")
        record = await service.approve(DAY, edited)
        assert record["status"] == "approved" and record["caption_edited"]
        publish_at = datetime.fromisoformat(record["publish_at"])
        assert datetime(2026, 11, 17, 17, 10, tzinfo=JST) <= publish_at <= datetime(2026, 11, 17, 17, 40, tzinfo=JST)
        clock.at = datetime(2026, 11, 17, 17, 5, tzinfo=JST)
        await service.tick()
        publisher.assert_not_awaited()
        clock.at = publish_at + timedelta(seconds=1)
        await service.tick()
        publisher.assert_awaited_once()
        _, text, image = publisher.await_args.args
        assert text.startswith("改过的标题✨\n\n") and text.endswith("#日常碎片 #放学路上 #旧书店")
        assert image[:8] == b"\x89PNG\r\n\x1a\n"
        record = service.store.get(DAY.isoformat())
        assert record["status"] == "published" and record["published"]["tid"] == "tid-1"
        await service.tick()
        publisher.assert_awaited_once()
    asyncio.run(run())


def test_late_approval_publishes_now(share):
    async def run():
        service, camera, *_, clock, publisher, _ = share
        await until_review(service, camera)
        clock.at = datetime(2026, 11, 17, 21, 0, tzinfo=JST)
        record = await service.approve(DAY, dict(CAPTION))
        assert datetime.fromisoformat(record["publish_at"]) == clock.at
        await service.tick()
        publisher.assert_awaited_once()
    asyncio.run(run())


def test_auto_start_waits_for_run_at_and_runs_once(share):
    async def run():
        service, *_, clock, _, config = share
        config["daily_share.run_at"] = "00:30"
        await service.tick()
        assert service.store.get(DAY.isoformat()) is None
        clock.at = datetime(2026, 11, 17, 0, 30, tzinfo=JST)
        await service.tick()
        assert service.store.get(DAY.isoformat())["status"] == "shooting"
        await service.tick()
        assert service.store.get(DAY.isoformat())["attempt"] == 1
    asyncio.run(run())


def test_disabled_does_not_auto_start_but_manual_works(share):
    async def run():
        service, *_, config = share
        config["daily_share.enabled"] = False
        await service.tick()
        assert service.store.get(DAY.isoformat()) is None
        await service.generate(DAY, "手动")
        await service.tick()
        assert service.store.get(DAY.isoformat())["status"] == "shooting"
    asyncio.run(run())


@pytest.mark.parametrize("pick, message", [
    ({"slot": "02:00-07:30", "time": "03:00"}, "睡眠"),
    ({"slot": "22:00-26:00", "time": "22:30"}, "跨零点"),
    ({"slot": "15:30-17:00", "time": "17:30"}, "不在所选时段"),
    ({"slot": "09:00-10:00"}, "不存在"),
])
def test_invalid_pick_fails_without_photo(share, pick, message):
    async def run():
        service, camera, _, client, *_ = share
        client.picks = [dict(PICK, **pick)]
        await service.tick()
        record = service.store.get(DAY.isoformat())
        assert record["status"] == "failed" and message in record["error"]
        assert camera.store.next_queued() is None
    asyncio.run(run())


def test_skip_day_without_photo(share):
    async def run():
        service, camera, _, client, *_ = share
        client.picks = [{"skip": True, "reason": "一整天都在家睡觉"}]
        await service.tick()
        assert service.store.get(DAY.isoformat())["status"] == "skipped"
        assert camera.store.next_queued() is None
    asyncio.run(run())


def test_camera_decline_marks_share_failed(share):
    async def run():
        service, camera, ctx, client, *_ = share
        client.plan_overrides = {"feasible": False, "rejection_code": "helper_absent", "absent_helper": "梧桐",
                                 "moment": "她在店里", "acting": "说梧桐不在"}
        await service.tick()
        await camera.process(camera.store.next_queued())
        await service.tick()
        record = service.store.get(DAY.isoformat())
        assert record["status"] == "failed" and "declined" in record["error"]
        ctx.maisaka.proactive.trigger.assert_not_awaited()
    asyncio.run(run())


def test_invalid_caption_fails_instead_of_template(share):
    async def run():
        service, camera, _, client, *_ = share
        client.captions = [dict(CAPTION, body="太短")]
        record = await until_review(service, camera)
        assert record["status"] == "failed" and "正文" in record["error"]
    asyncio.run(run())


def test_recaption_regenerate_and_review_guards(share):
    async def run():
        service, camera, _, client, *_ = share
        await until_review(service, camera)
        client.captions = [dict(CAPTION, title="第二版")]
        await service.recaption(DAY, "少用 emoji")
        await service.tick()
        record = service.store.get(DAY.isoformat())
        assert record["status"] == "pending_review" and record["caption"]["title"] == "第二版"
        assert client.caption_inputs[-1][0]["note"] == "少用 emoji"
        await service.approve(DAY, record["caption"])
        with pytest.raises(ValueError):
            await service.generate(DAY, "重拍")
        await service.withdraw(DAY)
        # 待审核时重拍：旧版记进历史，新一版重新选题，相机任务不复用
        client.picks = [dict(PICK)]
        record = await service.generate(DAY, "重拍")
        assert record["attempt"] == 2 and record["history"][0]["status"] == "rejected"
        await service.tick()
        assert service.store.get(DAY.isoformat())["camera_task_id"] != record["history"][0]["camera_task_id"]
    asyncio.run(run())


def test_publish_unknown_is_never_retried(share):
    async def run():
        service, camera, *_, clock, publisher, _ = share
        await until_review(service, camera)
        clock.at = datetime(2026, 11, 17, 21, 0, tzinfo=JST)
        await service.approve(DAY, dict(CAPTION))
        publisher.side_effect = daily.PublishUnconfirmed("回执超时")
        await service.tick()
        await service.tick()
        publisher.assert_awaited_once()
        assert service.store.get(DAY.isoformat())["status"] == "publish_unknown"
        with pytest.raises(ValueError):
            await service.generate(DAY, "重拍")
    asyncio.run(run())


def test_rejected_publish_is_recorded(share):
    async def run():
        service, camera, *_, clock, publisher, _ = share
        await until_review(service, camera)
        clock.at = datetime(2026, 11, 17, 21, 0, tzinfo=JST)
        await service.approve(DAY, dict(CAPTION))
        publisher.return_value = {"status": "failed", "retcode": 1, "wording": "未登录"}
        await service.tick()
        record = service.store.get(DAY.isoformat())
        assert record["status"] == "publish_failed" and "未登录" in record["error"]
    asyncio.run(run())


def test_restart_during_publish_becomes_unknown(tmp_path):
    store = daily_store.DailyShareStore(tmp_path)
    store.initialize()
    record = store.start("2026-11-17", "测试")
    record["status"] = "publishing"
    store.save(record)
    store.recover()
    assert store.get("2026-11-17")["status"] == "publish_unknown"


@pytest.mark.parametrize("caption", [
    dict(CAPTION, title=""), dict(CAPTION, tags=[]), dict(CAPTION, tags=["两个 词"]), dict(CAPTION, body="x" * 500),
])
def test_caption_validation(caption):
    with pytest.raises(ValueError):
        daily.validate_caption(caption)


def test_caption_tags_drop_hash():
    assert daily.validate_caption(dict(CAPTION, tags=["#日常", "＃碎片"]))["tags"] == ["日常", "碎片"]


def test_viewer_queues_only_valid_share_actions(tmp_path):
    conn = sqlite3.connect(tmp_path / "s.db")
    conn.execute("CREATE TABLE admin_jobs (id INTEGER PRIMARY KEY, action TEXT, target_date TEXT, slot TEXT, payload TEXT)")
    job = viewer._enqueue_job(conn, {"action": "daily_share_approve", "date": "2026-11-17", "caption": CAPTION})
    payload = json.loads(conn.execute("SELECT payload FROM admin_jobs WHERE id=?", (job,)).fetchone()[0])
    assert payload["caption"]["title"] == CAPTION["title"]
    with pytest.raises(ValueError):
        viewer._enqueue_job(conn, {"action": "daily_share_approve", "date": "2026-11-17", "caption": dict(CAPTION, body="短")})
    with pytest.raises(ValueError):
        viewer._enqueue_job(conn, {"action": "daily_share_publish_now", "date": "2026-11-17"})


def test_viewer_serves_share_image_only_from_camera_folder(tmp_path):
    store = daily_store.DailyShareStore(tmp_path / "daily_share")
    store.initialize()
    record = store.start("2026-11-17", "测试")
    record["photo"] = {"task_id": "abc123", "file": "attempt-1.png"}
    store.save(record)
    (tmp_path / "camera/abc123").mkdir(parents=True)
    (tmp_path / "camera/abc123/attempt-1.png").write_bytes(b"png")
    assert viewer._share_image(store.db, tmp_path / "camera", "2026-11-17").read_bytes() == b"png"
    record["photo"] = {"task_id": "../x", "file": "attempt-1.png"}
    store.save(record)
    assert viewer._share_image(store.db, tmp_path / "camera", "2026-11-17") is None
