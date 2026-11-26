"""两种照片的额度：露脸每个聊天每天三张，日常分享不限；
额度只按这个聊天今天已发出的照片算，店长、本地预览和每日分享不受限。"""

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import importlib
import json
import sqlite3

import pytest


module = importlib.import_module("plugins.01_a_day_with_mittes.camera.store")
service_module = importlib.import_module("plugins.01_a_day_with_mittes.camera.service")


def request(index, *, stream="group", user="guest", persona="", preview=False):
    return {"stream_id": stream, "source_msg_id": f"message-{index}", "requester": {"platform": "qq", "user_id": user},
            "requester_persona": persona, "preview": preview, "snapshot": {}}


@pytest.fixture
def store(tmp_path):
    result = module.CameraStore(tmp_path / "camera")
    result.initialize()
    return result


def sent(store, index, kind, card, **kwargs):
    """落一张已发出的照片：取景方案里的种类和构图卡就是计数依据。"""
    record = store.create(request(index, **kwargs))
    record["plan"] = {"photo_kind": kind, "card": card}
    record["status"] = "sent"
    store.save(record)
    return record


def service_for(store):
    service = service_module.PhotoService(SimpleNamespace(), Path("."), store.root.parent, AsyncMock())
    service.store = store
    return service


def test_only_sent_photos_of_this_chat_today_count(store):
    sent(store, 1, "face", "s_arm")
    sent(store, 2, "face", "s_timer")
    sent(store, 3, "casual", "c_hold")
    sent(store, 4, "face", "s_arm", stream="other")
    sent(store, 5, "face", "s_arm", persona="Sweyn")  # 店长豁免，不计
    queued = store.create(request(6))
    queued["plan"] = {"photo_kind": "face", "card": "s_arm"}
    store.save(queued)  # 还没发出，不计
    assert store.kinds_sent_today("group") == {"face": 2, "casual": 1}


def test_natural_day_resets_at_tokyo_midnight(store, monkeypatch):
    moment = datetime.fromisoformat("2026-10-17T14:59:59+00:00")  # 东京 23:59:59
    monkeypatch.setattr(module, "datetime", SimpleNamespace(now=lambda tz: moment.astimezone(tz)))
    sent(store, 1, "face", "s_arm")
    assert store.kinds_sent_today("group") == {"face": 1}
    moment = datetime.fromisoformat("2026-10-17T15:00:00+00:00")  # 东京第二天零点
    assert store.kinds_sent_today("group") == {}


def test_face_is_three_per_chat_per_day_then_downgrades(store):
    service = service_for(store)
    assert set(service._photo_options(store.create(request(1)))) == {"casual", "face"}
    for index in range(2, 5):
        assert "face" in service._photo_options(store.create(request(100 + index)))
        sent(store, index, "face", "s_arm")
    assert set(service._photo_options(store.create(request(10)))) == {"casual"}
    # 其他聊天不受影响
    assert "face" in service._photo_options(store.create(request(11, stream="other")))


@pytest.mark.parametrize("kwargs", [{"persona": "Sweyn"}, {"preview": True}])
def test_owner_and_preview_get_every_kind(store, kwargs):
    service = service_for(store)
    for index in range(3):
        sent(store, index, "face", "s_arm")
    record = store.create(request(9, **kwargs))
    assert record["quota"]["exempt"] is True
    assert set(service._photo_options(record)) == {"casual", "face"}


def test_cards_avoid_the_ones_just_used_in_this_chat(store):
    service = service_for(store)
    sent(store, 1, "casual", "c_feet")
    sent(store, 2, "casual", "c_hold")
    sent(store, 3, "face", "s_timer")
    for index in range(20):
        options = service._photo_options(store.create(request(100 + index)))
        assert sorted(options["casual"]) == sorted(service_module.CASUAL_CARDS)
        # 最近两张日常分享卡排到最后（新的在前），此刻只适合它们时仍可选
        assert options["casual"][-2:] == ["c_hold", "c_feet"]
        assert options["face"][-1] == "s_timer"


def test_downgrade_is_explained_to_the_planner():
    record = {"requester_name": "阿铭", "source_msg_id": "9", "source_text": "看看你", "instruction": "看看你",
              "plan": {"photo_kind": "casual", "asked_for": "face"}}
    note = service_module.delivery_note(record)
    assert note.startswith("↑ 这张是给阿铭拍的。对方原本想要露脸照，这次她自己选了拍日常分享")
    assert "不提额度、次数或概率" in note and note.endswith("回应「看看你」(msg_id=9)")
    record["plan"] = {"photo_kind": "face", "asked_for": "face"}
    assert service_module.delivery_note(record) == "↑ 这张是给阿铭拍的，回应「看看你」(msg_id=9)"


def test_reset_frees_today_and_survives_stale_saves(store):
    old = sent(store, 1, "face", "s_arm")
    store.reset_quota()
    assert store.kinds_sent_today("group") == {}
    old["events"].append({"stage": "note"})
    store.save(old)  # 工作中的旧副本不能把重置标记覆盖掉
    assert store.get(old["id"])["quota"]["reset"] is True
    assert store.kinds_sent_today("group") == {}
    sent(store, 2, "face", "s_arm")
    assert store.kinds_sent_today("group") == {"face": 1}


def test_duplicate_returns_the_existing_task(store):
    first = store.create(request(0))
    assert store.create(request(0)) == first
    with sqlite3.connect(store.db) as con:
        assert con.execute("SELECT count(*) FROM tasks").fetchone()[0] == 1


def test_unverified_identity_cannot_create_a_real_task(store):
    with pytest.raises(ValueError, match="真实消息验证"):
        store.create({**request(0), "requester": {"platform": "", "user_id": ""}})
    assert store.create({**request(1, preview=True), "requester": None})["quota"]["exempt"] is True


def test_recovery_marks_unfinished_tasks_without_retrying(store):
    queued = store.create(request(0))
    planning = store.create(request(1))
    planning["status"] = "planning"
    store.save(planning)
    sending = store.create(request(2))
    sending["status"] = "sending"
    store.save(sending)
    module.CameraStore(store.root).recover()
    assert store.get(queued["id"])["status"] == "queued"
    assert store.get(planning["id"])["status"] == "interrupted"
    assert store.get(sending["id"])["status"] == "delivery_unknown"
    assert json.loads(json.dumps(store.get(sending["id"])))["events"][-1]["stage"] == "delivery_unknown"
